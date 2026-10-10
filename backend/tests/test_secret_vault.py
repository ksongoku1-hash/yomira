from __future__ import annotations

import errno
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app.errors import ErrorCode
from app.persistence.storage_gate import SharedStorageGate
from app.secrets.protector import DataProtectionUnavailable, WindowsDpapiProtector
from app.secrets.reference import SecretReference
from app.secrets.vault import (
    MAX_SECRET_BYTES,
    SecretVaultError,
    WindowsDpapiSecretVault,
)
from app.tracing import activate_trace_id, reset_trace_id


class ReversibleTestProtector:
    def protect(self, data: bytes, *, entropy: bytes) -> bytes:
        protected = bytes(
            value ^ entropy[index % len(entropy)]
            for index, value in enumerate(data)
        )
        return b"test-cipher\0" + protected

    def unprotect(self, data: bytes, *, entropy: bytes) -> bytes:
        prefix = b"test-cipher\0"
        if not data.startswith(prefix):
            raise DataProtectionUnavailable
        ciphertext = data[len(prefix) :]
        return bytes(
            value ^ entropy[index % len(entropy)]
            for index, value in enumerate(ciphertext)
        )


class UnavailableTestProtector:
    def protect(self, data: bytes, *, entropy: bytes) -> bytes:
        raise DataProtectionUnavailable("DPAPI failure with private-cookie-value")

    def unprotect(self, data: bytes, *, entropy: bytes) -> bytes:
        raise DataProtectionUnavailable("DPAPI failure with private-cookie-value")


class FakeClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def _vault(
    root: Path,
    *,
    protector: ReversibleTestProtector | UnavailableTestProtector | None = None,
    clock: Callable[[], datetime] | None = None,
    storage_gate: SharedStorageGate | None = None,
) -> WindowsDpapiSecretVault:
    return WindowsDpapiSecretVault(
        root,
        protector=protector,
        clock=clock,
        storage_gate=storage_gate,
    )


def test_secret_reference_is_opaque_canonical_and_rejects_paths() -> None:
    reference = SecretReference.generate()

    assert reference.value == f"yomira-secret:v1:{reference.id}"
    assert UUID(str(reference.id)) == reference.id
    assert "yomira-secret:v1:" not in repr(reference)

    with pytest.raises(ValueError):
        SecretReference.parse("yomira-secret:v1:..\\outside")


def test_secret_vault_stores_ciphertext_and_reads_only_by_reference(tmp_path: Path) -> None:
    secret = b"session-cookie=do-not-persist-in-plain-text"
    vault = _vault(tmp_path, protector=ReversibleTestProtector())

    reference = vault.store(secret)
    record = tmp_path / "secrets" / f"{reference.id}.dpapi"

    assert record.is_file()
    assert secret not in record.read_bytes()
    assert vault.retrieve(reference) == secret
    with pytest.raises(TypeError):
        vault.retrieve(reference.value)  # type: ignore[arg-type]


def test_secret_vault_record_survives_vault_restart_and_backup_copy(tmp_path: Path) -> None:
    secret = b"restart-safe-token"
    vault = _vault(tmp_path, protector=ReversibleTestProtector())
    reference = vault.store(secret)
    restored_root = tmp_path / "restored"
    shutil.copytree(tmp_path / "secrets", restored_root / "secrets")

    restarted_vault = _vault(tmp_path, protector=ReversibleTestProtector())
    restored_vault = _vault(restored_root, protector=ReversibleTestProtector())

    assert restarted_vault.retrieve(reference) == secret
    assert restored_vault.retrieve(reference) == secret


def test_expired_secret_fails_closed_and_preserves_ciphertext(tmp_path: Path) -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    clock = FakeClock(now)
    vault = _vault(tmp_path, protector=ReversibleTestProtector(), clock=clock)
    reference = vault.store(b"expired-cookie", expires_at=now + timedelta(seconds=1))
    record = tmp_path / "secrets" / f"{reference.id}.dpapi"
    original_ciphertext = record.read_bytes()
    clock.current += timedelta(seconds=1)

    with pytest.raises(SecretVaultError) as error:
        vault.retrieve(reference)

    assert error.value.code is ErrorCode.AUTH_SECRET_EXPIRED
    assert record.read_bytes() == original_ciphertext


def test_corrupt_decrypted_payload_is_reported_without_deleting_or_overwriting(
    tmp_path: Path,
) -> None:
    protector = ReversibleTestProtector()
    vault = _vault(tmp_path, protector=protector)
    reference = vault.store(b"do-not-lose-this-secret")
    record = tmp_path / "secrets" / f"{reference.id}.dpapi"
    entropy = b"Yomira.SecretVault.v1\0" + reference.id.bytes
    record.write_bytes(protector.protect(b"invalid-payload", entropy=entropy))
    corrupt_record = record.read_bytes()

    with pytest.raises(SecretVaultError) as error:
        vault.retrieve(reference)

    assert error.value.code is ErrorCode.AUTH_SECRET_CORRUPT
    assert record.read_bytes() == corrupt_record


def test_unavailable_dpapi_fails_closed_and_preserves_encrypted_record(tmp_path: Path) -> None:
    secret = b"protected-cookie"
    reference = _vault(tmp_path, protector=ReversibleTestProtector()).store(secret)
    record = tmp_path / "secrets" / f"{reference.id}.dpapi"
    original_ciphertext = record.read_bytes()
    unavailable_vault = _vault(tmp_path, protector=UnavailableTestProtector())

    with pytest.raises(SecretVaultError) as error:
        unavailable_vault.retrieve(reference)

    assert error.value.code is ErrorCode.AUTH_SECRET_UNAVAILABLE
    assert "private-cookie-value" not in str(error.value)
    assert error.value.cause_type == "DataProtectionUnavailable"
    assert record.read_bytes() == original_ciphertext


def test_vault_failure_uses_active_trace_and_uuidv4_without_context(tmp_path: Path) -> None:
    reference = _vault(tmp_path, protector=ReversibleTestProtector()).store(b"trace-fixture")
    vault = _vault(tmp_path, protector=UnavailableTestProtector())
    trace_id = "43f4361a-891e-4c10-a6f4-d2b935b81974"
    context_token = activate_trace_id(trace_id)
    try:
        with pytest.raises(SecretVaultError) as contextual_error:
            vault.retrieve(reference)
    finally:
        reset_trace_id(context_token)

    assert contextual_error.value.trace_id == trace_id
    assert contextual_error.value.operation.value == "READ"
    assert contextual_error.value.retryable is True

    with pytest.raises(SecretVaultError) as background_error:
        vault.retrieve(reference)
    assert UUID(background_error.value.trace_id).version == 4


def test_failed_atomic_publish_removes_encrypted_temporary_and_returns_no_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path, protector=ReversibleTestProtector())

    def fail_rename(_source: Path, _destination: Path) -> None:
        raise PermissionError("private-cookie-value")

    monkeypatch.setattr("app.secrets.vault.os.rename", fail_rename)
    with pytest.raises(SecretVaultError) as error:
        vault.store(b"never-return-reference-before-commit")

    assert error.value.code is ErrorCode.AUTH_SECRET_STORE_FAILED
    assert error.value.cause_type == "PermissionError"
    records = list((tmp_path / "secrets").iterdir())
    assert records == []
    assert "private-cookie-value" not in str(error.value)


@pytest.mark.parametrize(
    ("error_number", "winerror"),
    [
        (errno.ENOSPC, None),
        (0, 39),
        (0, 112),
    ],
    ids=["enospc", "winerror-39", "winerror-112"],
)
def test_disk_full_during_secret_store_closes_gate_and_preserves_encrypted_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    winerror: int | None,
) -> None:
    gate = SharedStorageGate()
    vault = _vault(
        tmp_path,
        protector=ReversibleTestProtector(),
        storage_gate=gate,
    )
    previous_reference = vault.store(b"preserve-existing-secret")
    previous_record = tmp_path / "secrets" / f"{previous_reference.id}.dpapi"
    previous_ciphertext = previous_record.read_bytes()

    def fail_fsync(_descriptor: int) -> None:
        error = OSError(error_number, "disk full")
        if winerror is not None:
            error.winerror = winerror
        raise error

    monkeypatch.setattr("app.secrets.vault.os.fsync", fail_fsync)
    with pytest.raises(SecretVaultError) as error:
        vault.store(b"new-secret-must-not-be-lost")

    assert error.value.code is ErrorCode.AUTH_SECRET_STORE_FAILED
    assert gate.closed is True
    assert gate.closed_reason == "FILESYSTEM_DISK_FULL"
    assert previous_record.read_bytes() == previous_ciphertext
    pending = list((tmp_path / "secrets").glob(".pending-*.tmp"))
    assert len(pending) == 1
    assert b"new-secret-must-not-be-lost" not in pending[0].read_bytes()


def test_disk_full_creating_secret_directory_closes_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = SharedStorageGate()
    vault = _vault(
        tmp_path,
        protector=ReversibleTestProtector(),
        storage_gate=gate,
    )
    secret_directory = tmp_path / "secrets"
    original_mkdir = secret_directory.__class__.mkdir

    def fail_secret_directory_mkdir(
        candidate: Path,
        mode: int = 0o777,
        *,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if candidate == secret_directory:
            raise OSError(errno.ENOSPC, "disk full")
        original_mkdir(candidate, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(secret_directory.__class__, "mkdir", fail_secret_directory_mkdir)
    with pytest.raises(SecretVaultError) as error:
        vault.store(b"directory-full-secret")

    assert error.value.code is ErrorCode.AUTH_SECRET_STORE_FAILED
    assert gate.closed is True
    assert gate.closed_reason == "FILESYSTEM_DISK_FULL"
    assert secret_directory.exists() is False


def test_disk_full_during_secret_revoke_closes_gate_and_preserves_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = SharedStorageGate()
    vault = _vault(
        tmp_path,
        protector=ReversibleTestProtector(),
        storage_gate=gate,
    )
    reference = vault.store(b"preserve-this-secret")
    record = tmp_path / "secrets" / f"{reference.id}.dpapi"
    original_ciphertext = record.read_bytes()
    original_unlink = record.__class__.unlink

    def fail_record_unlink(
        candidate: Path,
        *,
        missing_ok: bool = False,
    ) -> None:
        if candidate == record:
            raise OSError(errno.ENOSPC, "disk full")
        original_unlink(candidate, missing_ok=missing_ok)

    monkeypatch.setattr(record.__class__, "unlink", fail_record_unlink)
    with pytest.raises(SecretVaultError) as error:
        vault.revoke(reference)

    assert error.value.code is ErrorCode.AUTH_SECRET_REVOKE_FAILED
    assert gate.closed is True
    assert gate.closed_reason == "FILESYSTEM_DISK_FULL"
    assert record.read_bytes() == original_ciphertext


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is available only on Windows.")
def test_unprotect_integrity_failure_is_ambiguous_and_preserves_record(tmp_path: Path) -> None:
    vault = WindowsDpapiSecretVault(tmp_path)
    reference = vault.store(b"preserve-when-integrity-check-fails")
    record = tmp_path / "secrets" / f"{reference.id}.dpapi"
    corrupted_ciphertext = bytearray(record.read_bytes())
    corrupted_ciphertext[-1] ^= 0x01
    record.write_bytes(bytes(corrupted_ciphertext))
    observed_ciphertext = record.read_bytes()

    with pytest.raises(SecretVaultError) as error:
        vault.retrieve(reference)

    assert error.value.code is ErrorCode.AUTH_SECRET_UNAVAILABLE
    assert record.read_bytes() == observed_ciphertext


def test_secret_quota_and_value_size_are_bounded_without_truncation(tmp_path: Path) -> None:
    vault = _vault(tmp_path, protector=ReversibleTestProtector())

    with pytest.raises(SecretVaultError) as oversize:
        vault.store(b"x" * (MAX_SECRET_BYTES + 1))

    assert oversize.value.code is ErrorCode.AUTH_SECRET_STORE_FAILED
    assert list((tmp_path / "secrets").glob("*.dpapi")) == []


def test_secret_record_count_is_bounded_without_removing_existing_records(tmp_path: Path) -> None:
    vault = _vault(tmp_path, protector=ReversibleTestProtector())
    references = [vault.store(f"fixture-{index}".encode()) for index in range(128)]
    records_before = sorted((tmp_path / "secrets").glob("*.dpapi"))

    with pytest.raises(SecretVaultError) as error:
        vault.store(b"one-too-many")

    assert error.value.code is ErrorCode.AUTH_SECRET_STORE_FAILED
    assert len(records_before) == 128
    assert sorted((tmp_path / "secrets").glob("*.dpapi")) == records_before
    assert vault.retrieve(references[0]) == b"fixture-0"


def test_revoke_is_reference_scoped_idempotent_and_does_not_return_secret(tmp_path: Path) -> None:
    vault = _vault(tmp_path, protector=ReversibleTestProtector())
    reference = vault.store(b"revoke-me")
    record = tmp_path / "secrets" / f"{reference.id}.dpapi"

    assert vault.revoke(reference) is True
    assert record.exists() is False
    assert vault.revoke(reference) is False
    with pytest.raises(SecretVaultError) as error:
        vault.retrieve(reference)
    assert error.value.code is ErrorCode.AUTH_SECRET_NOT_FOUND


def test_store_failure_never_falls_back_to_plaintext(tmp_path: Path) -> None:
    vault = _vault(tmp_path, protector=UnavailableTestProtector())

    with pytest.raises(SecretVaultError) as error:
        vault.store(b"must-never-be-written-clear")

    assert error.value.code is ErrorCode.AUTH_SECRET_UNAVAILABLE
    assert not (tmp_path / "secrets").exists() or list((tmp_path / "secrets").iterdir()) == []


def test_support_bundle_spec_excludes_vault_ciphertext_and_references() -> None:
    project_root = Path(__file__).resolve().parents[2]
    support_bundle_spec = (project_root / "docs" / "SUPPORT_BUNDLE_SPEC.md").read_text(
        encoding="utf-8"
    )

    assert "`data_root/secrets`" in support_bundle_spec
    assert "referencias opacas" in support_bundle_spec


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is available only on Windows.")
def test_windows_dpapi_encrypts_and_decrypts_for_current_user(tmp_path: Path) -> None:
    protector = WindowsDpapiProtector()
    plaintext = b"dpapi-fixture-secret"
    entropy = b"yomira-test-entropy"

    ciphertext = protector.protect(plaintext, entropy=entropy)

    assert ciphertext != plaintext
    assert plaintext not in ciphertext
    assert protector.unprotect(ciphertext, entropy=entropy) == plaintext

    vault = WindowsDpapiSecretVault(tmp_path)
    reference = vault.store(plaintext)
    restarted = WindowsDpapiSecretVault(tmp_path)
    assert restarted.retrieve(reference) == plaintext

    restored_root = tmp_path / "restored-dpapi"
    shutil.copytree(tmp_path / "secrets", restored_root / "secrets")
    restored = WindowsDpapiSecretVault(restored_root)
    assert restored.retrieve(reference) == plaintext

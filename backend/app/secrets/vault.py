"""Atomic, bounded local storage for Windows-protected connector credentials."""

from __future__ import annotations

import os
import struct
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.errors import ErrorCode
from app.persistence.storage_gate import (
    SharedStorageGate,
    StorageGateClosedError,
    is_disk_full_os_error,
)
from app.secrets.errors import SecretVaultError, SecretVaultOperation
from app.secrets.protector import (
    DataProtectionUnavailable,
    DataProtector,
    WindowsDpapiProtector,
)
from app.secrets.reference import SecretReference

MAX_SECRET_BYTES = 64 * 1024
MAX_SECRET_RECORDS = 128
_MAX_PROTECTED_RECORD_BYTES = MAX_SECRET_BYTES + 4096
_PAYLOAD_HEADER = struct.Struct(">Bq")
_PAYLOAD_VERSION = 1
_APP_ENTROPY_PREFIX = b"Yomira.SecretVault.v1\0"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class SecretVault(Protocol):
    def store(
        self,
        secret: bytes,
        *,
        expires_at: datetime | None = None,
    ) -> SecretReference: ...

    def retrieve(self, reference: SecretReference) -> bytes: ...

    def revoke(self, reference: SecretReference) -> bool: ...


class WindowsDpapiSecretVault:
    """Stores opaque secret records beneath the application's data root."""

    def __init__(
        self,
        data_root: Path,
        *,
        protector: DataProtector | None = None,
        clock: Callable[[], datetime] | None = None,
        storage_gate: SharedStorageGate | None = None,
    ) -> None:
        self._directory = Path(data_root) / "secrets"
        self._protector = protector if protector is not None else WindowsDpapiProtector()
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._storage_gate = storage_gate if storage_gate is not None else SharedStorageGate()
        self._lock = threading.RLock()

    def store(
        self,
        secret: bytes,
        *,
        expires_at: datetime | None = None,
    ) -> SecretReference:
        if type(secret) is not bytes or not secret or len(secret) > MAX_SECRET_BYTES:
            raise self._error(ErrorCode.AUTH_SECRET_STORE_FAILED, SecretVaultOperation.STORE)

        expiry_us = self._expiry_timestamp(expires_at)
        payload = _PAYLOAD_HEADER.pack(_PAYLOAD_VERSION, expiry_us) + secret
        reference = SecretReference.generate()
        try:
            protected = self._protector.protect(
                payload,
                entropy=_APP_ENTROPY_PREFIX + reference.id.bytes,
            )
        except Exception as error:
            code = (
                ErrorCode.AUTH_SECRET_UNAVAILABLE
                if isinstance(error, DataProtectionUnavailable)
                else ErrorCode.AUTH_SECRET_STORE_FAILED
            )
            raise self._error(
                code,
                SecretVaultOperation.STORE,
                cause_type=type(error).__name__,
            ) from None

        if not protected or len(protected) > _MAX_PROTECTED_RECORD_BYTES:
            raise self._error(ErrorCode.AUTH_SECRET_STORE_FAILED, SecretVaultOperation.STORE)

        with self._lock:
            try:
                with self._storage_gate.mutation():
                    try:
                        self._directory.mkdir(parents=True, exist_ok=True)
                        self._ensure_storage_directory(SecretVaultOperation.STORE)
                        if self._record_count() >= MAX_SECRET_RECORDS:
                            raise self._error(
                                ErrorCode.AUTH_SECRET_STORE_FAILED,
                                SecretVaultOperation.STORE,
                            )
                        self._write_new_record(reference, protected)
                    except OSError as error:
                        if is_disk_full_os_error(error):
                            self._storage_gate.close("FILESYSTEM_DISK_FULL")
                        raise
            except StorageGateClosedError as error:
                raise self._error(
                    ErrorCode.AUTH_SECRET_STORE_FAILED,
                    SecretVaultOperation.STORE,
                    cause_type=type(error).__name__,
                ) from None
            except SecretVaultError:
                raise
            except OSError as error:
                raise self._error(
                    ErrorCode.AUTH_SECRET_STORE_FAILED,
                    SecretVaultOperation.STORE,
                    cause_type=type(error).__name__,
                ) from None
            return reference

    def retrieve(self, reference: SecretReference) -> bytes:
        self._require_reference(reference)
        path = self._record_path(reference)
        with self._lock:
            self._ensure_storage_directory(SecretVaultOperation.READ)
            try:
                if path.is_symlink():
                    raise self._error(
                        ErrorCode.AUTH_SECRET_UNAVAILABLE,
                        SecretVaultOperation.READ,
                    )
                protected = path.read_bytes()
            except FileNotFoundError:
                raise self._error(
                    ErrorCode.AUTH_SECRET_NOT_FOUND,
                    SecretVaultOperation.READ,
                ) from None
            except SecretVaultError:
                raise
            except OSError as error:
                raise self._error(
                    ErrorCode.AUTH_SECRET_UNAVAILABLE,
                    SecretVaultOperation.READ,
                    cause_type=type(error).__name__,
                ) from None

            if not protected or len(protected) > _MAX_PROTECTED_RECORD_BYTES:
                raise self._error(ErrorCode.AUTH_SECRET_CORRUPT, SecretVaultOperation.READ)
            try:
                payload = self._protector.unprotect(
                    protected,
                    entropy=_APP_ENTROPY_PREFIX + reference.id.bytes,
                )
            except Exception as error:
                raise self._error(
                    ErrorCode.AUTH_SECRET_UNAVAILABLE,
                    SecretVaultOperation.READ,
                    cause_type=type(error).__name__,
                ) from None

            secret, expiry_us = self._decode_payload(payload)
            if expiry_us and _to_epoch_microseconds(self._now()) >= expiry_us:
                raise self._error(ErrorCode.AUTH_SECRET_EXPIRED, SecretVaultOperation.READ)
            return secret

    def revoke(self, reference: SecretReference) -> bool:
        self._require_reference(reference)
        path = self._record_path(reference)
        with self._lock:
            try:
                with self._storage_gate.mutation():
                    try:
                        self._ensure_storage_directory(SecretVaultOperation.REVOKE)
                        if path.is_symlink():
                            raise OSError("Secret record must not be a symbolic link.")
                        existed = path.exists()
                        path.unlink(missing_ok=True)
                    except OSError as error:
                        if is_disk_full_os_error(error):
                            self._storage_gate.close("FILESYSTEM_DISK_FULL")
                        raise
            except StorageGateClosedError as error:
                raise self._error(
                    ErrorCode.AUTH_SECRET_REVOKE_FAILED,
                    SecretVaultOperation.REVOKE,
                    cause_type=type(error).__name__,
                ) from None
            except OSError as error:
                raise self._error(
                    ErrorCode.AUTH_SECRET_REVOKE_FAILED,
                    SecretVaultOperation.REVOKE,
                    cause_type=type(error).__name__,
                ) from None
            return existed

    def _write_new_record(self, reference: SecretReference, protected: bytes) -> None:
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".pending-",
                suffix=".tmp",
                dir=self._directory,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as target:
                target.write(protected)
                target.flush()
                os.fsync(target.fileno())
            os.rename(temporary_path, self._record_path(reference))
        except FileExistsError as error:
            self._cleanup_temporary(temporary_path)
            raise self._error(
                ErrorCode.AUTH_SECRET_STORE_FAILED,
                SecretVaultOperation.STORE,
                cause_type=type(error).__name__,
            ) from None
        except OSError as error:
            if is_disk_full_os_error(error):
                self._storage_gate.close("FILESYSTEM_DISK_FULL")
            else:
                self._cleanup_temporary(temporary_path)
            raise self._error(
                ErrorCode.AUTH_SECRET_STORE_FAILED,
                SecretVaultOperation.STORE,
                cause_type=type(error).__name__,
            ) from None

    def _cleanup_temporary(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            if is_disk_full_os_error(error):
                self._storage_gate.close("FILESYSTEM_DISK_FULL")

    def _record_count(self) -> int:
        return sum(1 for path in self._directory.glob("*.dpapi") if path.is_file())

    def _ensure_storage_directory(self, operation: SecretVaultOperation) -> None:
        if not self._directory.exists():
            return
        is_junction = getattr(self._directory, "is_junction", lambda: False)
        if self._directory.is_symlink() or is_junction():
            raise self._error(
                ErrorCode.AUTH_SECRET_UNAVAILABLE,
                operation,
            )
        if not self._directory.is_dir():
            raise self._error(
                ErrorCode.AUTH_SECRET_UNAVAILABLE,
                operation,
            )

    def _expiry_timestamp(self, expires_at: datetime | None) -> int:
        if expires_at is None:
            return 0
        if (
            type(expires_at) is not datetime
            or expires_at.tzinfo is None
            or expires_at.utcoffset() is None
        ):
            raise self._error(ErrorCode.AUTH_SECRET_STORE_FAILED, SecretVaultOperation.STORE)
        normalized_expiry = expires_at.astimezone(UTC)
        if normalized_expiry <= self._now():
            raise self._error(ErrorCode.AUTH_SECRET_STORE_FAILED, SecretVaultOperation.STORE)
        return _to_epoch_microseconds(normalized_expiry)

    def _now(self) -> datetime:
        now = self._clock()
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("Vault clock must return a timezone-aware datetime.")
        return now.astimezone(UTC)

    @staticmethod
    def _decode_payload(payload: bytes) -> tuple[bytes, int]:
        if len(payload) <= _PAYLOAD_HEADER.size:
            raise SecretVaultError(
                code=ErrorCode.AUTH_SECRET_CORRUPT,
                operation=SecretVaultOperation.READ,
            )
        version, expiry_us = _PAYLOAD_HEADER.unpack(payload[: _PAYLOAD_HEADER.size])
        secret = payload[_PAYLOAD_HEADER.size :]
        if version != _PAYLOAD_VERSION or expiry_us < 0 or len(secret) > MAX_SECRET_BYTES:
            raise SecretVaultError(
                code=ErrorCode.AUTH_SECRET_CORRUPT,
                operation=SecretVaultOperation.READ,
            )
        return secret, expiry_us

    def _record_path(self, reference: SecretReference) -> Path:
        return self._directory / f"{reference.id}.dpapi"

    @staticmethod
    def _require_reference(reference: SecretReference) -> None:
        if type(reference) is not SecretReference:
            raise TypeError("reference must be a SecretReference")

    @staticmethod
    def _error(
        code: ErrorCode,
        operation: SecretVaultOperation,
        *,
        cause_type: str | None = None,
    ) -> SecretVaultError:
        return SecretVaultError(code=code, operation=operation, cause_type=cause_type)


def _to_epoch_microseconds(value: datetime) -> int:
    delta = value.astimezone(UTC) - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds

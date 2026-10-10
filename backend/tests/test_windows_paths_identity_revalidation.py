from pathlib import Path

import pytest

from app.persistence import windows_paths
from app.persistence.windows_paths import DataRootIdentity, DataRootIdentityError


def _count_path_calls(calls: list[Path], path: Path) -> int:
    return sum(called_path == path for called_path in calls)


def test_identity_reuses_volume_mount_but_rechecks_drive_and_file_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volume_path_calls: list[Path] = []
    drive_type_calls: list[str] = []
    file_identity_calls: list[Path] = []
    current_drive_type = 3  # DRIVE_FIXED
    expected_file_identity = (0x1234, bytes(range(16)))
    current_file_identity = expected_file_identity

    def get_volume_path_name(path: Path) -> str:
        volume_path_calls.append(path)
        return "C:\\"

    def get_drive_type(volume_root: str) -> int:
        drive_type_calls.append(volume_root)
        return current_drive_type

    def get_file_id_info(path: Path) -> tuple[int, bytes]:
        file_identity_calls.append(path)
        return current_file_identity

    monkeypatch.setattr(windows_paths, "_get_volume_path_name", get_volume_path_name)
    monkeypatch.setattr(windows_paths, "_get_drive_type", get_drive_type)
    monkeypatch.setattr(windows_paths, "_get_file_id_info", get_file_id_info)

    stable_root = tmp_path / "stable-root"
    stable_root.mkdir()
    stable_identity = DataRootIdentity.capture(stable_root)
    stable_path = stable_identity.canonical_path
    drive_calls_after_capture = len(drive_type_calls)
    file_calls_after_capture = len(file_identity_calls)

    stable_identity.assert_current()
    stable_identity.assert_current()

    assert len(drive_type_calls) - drive_calls_after_capture == 2
    assert _count_path_calls(file_identity_calls, stable_path) == file_calls_after_capture + 2

    unsupported_root = tmp_path / "unsupported-root"
    unsupported_root.mkdir()
    current_drive_type = 3
    current_file_identity = expected_file_identity
    unsupported_identity = DataRootIdentity.capture(unsupported_root)
    drive_calls_before_unsupported_check = len(drive_type_calls)
    file_calls_before_unsupported_check = len(file_identity_calls)
    current_drive_type = 0  # DRIVE_UNKNOWN

    with pytest.raises(DataRootIdentityError):
        unsupported_identity.assert_current()
    calls_after_unsupported_failure = (
        len(volume_path_calls),
        len(drive_type_calls),
        len(file_identity_calls),
    )
    assert len(drive_type_calls) == drive_calls_before_unsupported_check + 1
    assert len(file_identity_calls) == file_calls_before_unsupported_check

    with pytest.raises(DataRootIdentityError):
        unsupported_identity.assert_current()
    assert calls_after_unsupported_failure == (
        len(volume_path_calls),
        len(drive_type_calls),
        len(file_identity_calls),
    )

    for index, mismatched_file_identity in enumerate(
        (
            (expected_file_identity[0] + 1, expected_file_identity[1]),
            (expected_file_identity[0], bytes(reversed(range(16)))),
        ),
        start=1,
    ):
        mismatch_root = tmp_path / f"mismatch-root-{index}"
        mismatch_root.mkdir()
        current_drive_type = 3
        current_file_identity = expected_file_identity
        mismatch_identity = DataRootIdentity.capture(mismatch_root)
        drive_calls_before_mismatch_check = len(drive_type_calls)
        file_calls_before_mismatch_check = len(file_identity_calls)
        current_file_identity = mismatched_file_identity

        with pytest.raises(DataRootIdentityError):
            mismatch_identity.assert_current()
        calls_after_mismatch = (
            len(volume_path_calls),
            len(drive_type_calls),
            len(file_identity_calls),
        )
        assert len(drive_type_calls) == drive_calls_before_mismatch_check + 1
        assert len(file_identity_calls) == file_calls_before_mismatch_check + 1

        with pytest.raises(DataRootIdentityError):
            mismatch_identity.assert_current()
        assert calls_after_mismatch == (
            len(volume_path_calls),
            len(drive_type_calls),
            len(file_identity_calls),
        )

    assert _count_path_calls(volume_path_calls, stable_path) == 1, (
        "GetVolumePathNameW must be sampled during capture and reused by later checks"
    )

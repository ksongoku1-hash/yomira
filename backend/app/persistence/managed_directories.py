"""Shared safe creation of managed directories (extracted from DownloadWorker)."""

import os
import stat
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from app.domain.asset import validate_relative_path
from app.persistence.filesystem_retry import (
    RetryingFileStream,
    retry_filesystem_operation,
    retry_lstat,
)
from app.persistence.storage_gate import (
    SharedStorageGate,
    StorageGateClosedError,
    is_disk_full_os_error,
)
from app.persistence.windows_paths import DataRootIdentity

_REPARSE_POINT_ATTRIBUTE = 0x0400


def ensure_managed_directory(
    relative_path: str,
    *,
    data_root_identity: DataRootIdentity,
    storage_gate: SharedStorageGate,
    component_name: str,
    job_id: UUID | None = None,
    trace_id: str,
) -> Path:
    validate_relative_path(relative_path)
    root = data_root_identity.canonical_path

    def is_regular_directory(information: os.stat_result) -> bool:
        attributes = getattr(information, "st_file_attributes", 0)
        return not (
            stat.S_ISLNK(information.st_mode)
            or attributes & _REPARSE_POINT_ATTRIBUTE
            or not stat.S_ISDIR(information.st_mode)
        )

    def checked_lstat(
        path: Path,
        *,
        stage: str,
        expected_parent_identity: tuple[int, int] | None = None,
    ) -> os.stat_result:
        return retry_lstat(
            path,
            component=component_name,
            stage=stage,
            trace_id=trace_id,
            job_id=str(job_id) if job_id is not None else None,
            validate_path=data_root_identity.assert_current,
            expected_parent_identity=expected_parent_identity,
        )

    root_information = checked_lstat(root, stage="data_directory_root_lstat")
    if not is_regular_directory(root_information):
        raise OSError("output data root is not a regular directory")
    parent_identity = RetryingFileStream.identity_of(root_information)
    if parent_identity is None:
        raise OSError("output data root identity is unavailable")
    data_root_identity.assert_current()

    current = root
    for component in relative_path.split("/"):
        if component in {"", ".", ".."}:
            raise ValueError("relative directory path is invalid")
        current = current / component
        try:
            information = checked_lstat(
                current,
                stage="data_directory_lstat",
                expected_parent_identity=parent_identity,
            )
        except FileNotFoundError:
            expected_parent_identity = parent_identity

            def unchanged_prestate(
                current_path: Path = current,
                expected_parent: tuple[int, int] = expected_parent_identity,
            ) -> bool:
                try:
                    data_root_identity.assert_current()
                    parent_information = current_path.parent.lstat()
                except Exception:
                    return False
                if (
                    not is_regular_directory(parent_information)
                    or RetryingFileStream.identity_of(parent_information) != expected_parent
                ):
                    return False
                try:
                    current_path.lstat()
                except FileNotFoundError:
                    return True
                except Exception:
                    return False
                return False

            def applied_state(
                current_path: Path = current,
                expected_parent: tuple[int, int] = expected_parent_identity,
            ) -> bool:
                try:
                    data_root_identity.assert_current()
                    parent_information = current_path.parent.lstat()
                    if (
                        not is_regular_directory(parent_information)
                        or RetryingFileStream.identity_of(parent_information) != expected_parent
                    ):
                        return False
                    information_after = current_path.lstat()
                except Exception:
                    return False
                return is_regular_directory(information_after)

            def create_directory(
                current_path: Path = current,
                unchanged_check: Callable[[], bool] = unchanged_prestate,
                applied_check: Callable[[], bool] = applied_state,
            ) -> None:
                data_root_identity.assert_current()
                if not unchanged_check():
                    raise OSError("Cannot prove the data-directory create pre-state")
                try:
                    with storage_gate.mutation():
                        try:
                            current_path.mkdir()
                        except FileExistsError:
                            if applied_check():
                                return
                            raise
                        except OSError as error:
                            if is_disk_full_os_error(error):
                                storage_gate.close("FILESYSTEM_DISK_FULL")
                            raise
                except StorageGateClosedError:
                    raise
                if not applied_check():
                    raise OSError("Cannot prove the data-directory create result")

            try:
                retry_filesystem_operation(
                    create_directory,
                    unchanged=unchanged_prestate,
                    component=component_name,
                    stage="data_directory_mkdir",
                    trace_id=trace_id,
                    job_id=str(job_id) if job_id is not None else None,
                )
            except StorageGateClosedError:
                raise
            information = checked_lstat(
                current,
                stage="data_directory_lstat_after_mkdir",
                expected_parent_identity=expected_parent_identity,
            )
        if not is_regular_directory(information):
            raise OSError("output directory is not a regular directory")
        parent_identity = RetryingFileStream.identity_of(information)
        if parent_identity is None:
            raise OSError("output directory identity is unavailable")
    data_root_identity.assert_current()
    return current

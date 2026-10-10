"""Bounded per-job disk reservations for the managed Yomira data volume."""

from __future__ import annotations

import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from shutil import disk_usage
from threading import RLock
from typing import Literal
from uuid import UUID

from app.errors import ErrorCode
from app.persistence.database import SQLiteDatabase
from app.persistence.windows_paths import DataRootIdentity

MINIMUM_RESERVED_BYTES = 512 * 1024 * 1024
PAGE_TEMP_RESERVATION_BYTES = 2 * 1024 * 1024 * 1024
CBZ_RESERVATION_BYTES = 2 * 1024 * 1024 * 1024
_MAX_SQLITE_BYTES = (1 << 63) - 1

ReservationCategory = Literal["pages", "cbz"]
DiskUsageProvider = Callable[[Path], object]


def _is_sqlite_database_instance(value: object) -> bool:
    return isinstance(value, SQLiteDatabase)


@dataclass(frozen=True, slots=True)
class DiskBudgetSnapshot:
    volume_serial: int | None
    capacity_bytes: int | None
    free_bytes: int | None
    database_bytes: int | None
    wal_bytes: int | None
    recovery_demand_bytes: int | None
    reserved_bytes: int
    minimum_reserved_bytes: int
    required_bytes: int | None
    safe: bool
    reason: str | None = None


class DiskBudgetUnavailableError(RuntimeError):
    code = ErrorCode.SYS_DISK_BUDGET_EXCEEDED
    retryable = True

    def __init__(
        self,
        reason: str,
        *,
        free_bytes: int | None,
        required_bytes: int | None,
    ) -> None:
        self.reason = reason
        self.free_bytes = free_bytes
        self.required_bytes = required_bytes
        super().__init__("The managed volume cannot safely admit this disk reservation.")


@dataclass(slots=True)
class _JobReservation:
    pages_remaining_bytes: int
    cbz_remaining_bytes: int
    pages_in_flight_bytes: int = 0
    cbz_in_flight_bytes: int = 0
    active_writes: int = 0

    @property
    def remaining_bytes(self) -> int:
        return (
            self.pages_remaining_bytes
            + self.cbz_remaining_bytes
            + self.pages_in_flight_bytes
            + self.cbz_in_flight_bytes
        )


class DiskBudgetManager:
    """Atomically reserve the bounded page and CBZ growth for active jobs."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        disk_usage_provider: DiskUsageProvider = disk_usage,
    ) -> None:
        if not _is_sqlite_database_instance(database):
            raise TypeError("database must be a SQLiteDatabase")
        if not callable(disk_usage_provider):
            raise TypeError("disk_usage_provider must be callable")
        self._database = database
        self._identity: DataRootIdentity = database.data_root_identity
        self._disk_usage_provider = disk_usage_provider
        self._lock = RLock()
        self._reservations: dict[UUID, _JobReservation] = {}

    @property
    def database(self) -> SQLiteDatabase:
        return self._database

    def snapshot(self) -> DiskBudgetSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def reserve(
        self,
        job_id: UUID,
        *,
        existing_page_bytes: int = 0,
        existing_cbz_bytes: int = 0,
    ) -> None:
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        self._validate_existing_bytes(existing_page_bytes, PAGE_TEMP_RESERVATION_BYTES)
        self._validate_existing_bytes(existing_cbz_bytes, CBZ_RESERVATION_BYTES)
        self.reserve_bounded_operation(
            job_id,
            page_bytes=PAGE_TEMP_RESERVATION_BYTES - existing_page_bytes,
            cbz_bytes=CBZ_RESERVATION_BYTES - existing_cbz_bytes,
        )

    def reserve_bounded_operation(
        self, reservation_id: UUID, *, page_bytes: int, cbz_bytes: int = 0
    ) -> None:
        """Reserve bounded core operations, including whole-manga exports."""
        if type(reservation_id) is not UUID or reservation_id.version != 4:
            raise ValueError("reservation_id must be a UUIDv4")
        self._validate_existing_bytes(page_bytes, 8 * 1024 * 1024 * 1024)
        self._validate_existing_bytes(cbz_bytes, 8 * 1024 * 1024 * 1024)
        job_id = reservation_id

        with self._lock:
            if job_id in self._reservations:
                raise ValueError("job already owns a disk reservation")
            snapshot = self._snapshot_locked()
            requested_bytes = page_bytes + cbz_bytes
            required_bytes = (
                None
                if snapshot.required_bytes is None
                else snapshot.required_bytes + requested_bytes
            )
            if (
                not snapshot.safe
                or snapshot.free_bytes is None
                or required_bytes is None
                or snapshot.free_bytes < required_bytes
            ):
                raise DiskBudgetUnavailableError(
                    snapshot.reason or "INSUFFICIENT_SPACE",
                    free_bytes=snapshot.free_bytes,
                    required_bytes=required_bytes,
                )
            self._reservations[job_id] = _JobReservation(
                pages_remaining_bytes=page_bytes,
                cbz_remaining_bytes=cbz_bytes,
            )

    def write_reserved(
        self,
        job_id: UUID,
        *,
        category: ReservationCategory,
        requested_bytes: int,
        write: Callable[[], int],
    ) -> int:
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        if category not in {"pages", "cbz"}:
            raise ValueError("category must be pages or cbz")
        if type(requested_bytes) is not int or requested_bytes < 0:
            raise ValueError("requested_bytes must be a non-negative integer")
        if not callable(write):
            raise TypeError("write must be callable")

        with self._lock:
            reservation = self._reservations.get(job_id)
            if reservation is None:
                raise DiskBudgetUnavailableError(
                    "RESERVATION_MISSING",
                    free_bytes=None,
                    required_bytes=None,
                )
            attribute = "pages_remaining_bytes" if category == "pages" else "cbz_remaining_bytes"
            in_flight_attribute = (
                "pages_in_flight_bytes" if category == "pages" else "cbz_in_flight_bytes"
            )
            remaining_bytes = getattr(reservation, attribute)
            if requested_bytes > remaining_bytes:
                raise DiskBudgetUnavailableError(
                    "RESERVATION_EXHAUSTED",
                    free_bytes=None,
                    required_bytes=requested_bytes,
                )

            setattr(reservation, attribute, remaining_bytes - requested_bytes)
            setattr(
                reservation,
                in_flight_attribute,
                getattr(reservation, in_flight_attribute) + requested_bytes,
            )
            reservation.active_writes += 1

        try:
            written_bytes = write()
        except BaseException:
            with self._lock:
                self._finish_write(
                    job_id,
                    in_flight_attribute=in_flight_attribute,
                    requested_bytes=requested_bytes,
                    refund_bytes=requested_bytes,
                )
            raise

        with self._lock:
            if (
                type(written_bytes) is not int
                or written_bytes < 0
                or written_bytes > requested_bytes
            ):
                self._finish_write(
                    job_id,
                    in_flight_attribute=in_flight_attribute,
                    requested_bytes=requested_bytes,
                    refund_bytes=requested_bytes,
                )
                raise RuntimeError("filesystem write returned an invalid byte count")
            self._finish_write(
                job_id,
                in_flight_attribute=in_flight_attribute,
                requested_bytes=requested_bytes,
                refund_bytes=requested_bytes - written_bytes,
            )
            return written_bytes

    def _finish_write(
        self,
        job_id: UUID,
        *,
        in_flight_attribute: str,
        requested_bytes: int,
        refund_bytes: int,
    ) -> None:
        reservation = self._reservations[job_id]
        setattr(
            reservation,
            in_flight_attribute,
            getattr(reservation, in_flight_attribute) - requested_bytes,
        )
        reservation.active_writes -= 1
        if refund_bytes:
            remaining_attribute = in_flight_attribute.replace("_in_flight_", "_remaining_")
            setattr(
                reservation,
                remaining_attribute,
                getattr(reservation, remaining_attribute) + refund_bytes,
            )

    def release(self, job_id: UUID, *, quiescent: bool) -> bool:
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        if type(quiescent) is not bool:
            raise TypeError("quiescent must be a bool")
        if not quiescent:
            raise RuntimeError("disk reservation cannot be released before worker quiescence")
        if self._database.storage_gate.active_mutations:
            raise RuntimeError("disk reservation cannot be released while storage mutations remain")
        with self._lock:
            reservation = self._reservations.get(job_id)
            if reservation is not None and reservation.active_writes:
                raise RuntimeError(
                    "disk reservation cannot be released while writes remain in flight"
                )
            return self._reservations.pop(job_id, None) is not None

    def release_bounded_operation(self, operation_id: UUID) -> bool:
        """Release a completed bounded operation without waiting for other jobs."""
        if type(operation_id) is not UUID or operation_id.version != 4:
            raise ValueError("operation_id must be a UUIDv4")
        with self._lock:
            reservation = self._reservations.get(operation_id)
            if reservation is not None and reservation.active_writes:
                raise RuntimeError(
                    "bounded operation reservation cannot be released "
                    "while its writes remain in flight"
                )
            return self._reservations.pop(operation_id, None) is not None

    @staticmethod
    def _validate_existing_bytes(value: int, limit: int) -> None:
        if type(value) is not int or not 0 <= value <= limit:
            raise DiskBudgetUnavailableError(
                "EXISTING_ARTIFACT_SIZE_UNKNOWN_OR_OUT_OF_RANGE",
                free_bytes=None,
                required_bytes=None,
            )

    def _snapshot_locked(self) -> DiskBudgetSnapshot:
        reserved_bytes = sum(
            reservation.remaining_bytes for reservation in self._reservations.values()
        )
        try:
            self._identity.assert_current()
            root = self._identity.canonical_path
            usage = self._disk_usage_provider(root)
            capacity_bytes = getattr(usage, "total", None)
            used_bytes = getattr(usage, "used", None)
            free_bytes = getattr(usage, "free", None)
            if (
                type(capacity_bytes) is not int
                or type(used_bytes) is not int
                or type(free_bytes) is not int
                or capacity_bytes <= 0
                or used_bytes < 0
                or free_bytes < 0
                or used_bytes > capacity_bytes
                or free_bytes > capacity_bytes
            ):
                raise ValueError("volume capacity measurement is invalid")

            database_bytes = self._file_size(self._database.path, allow_missing=False)
            wal_bytes = self._file_size(
                self._database.path.with_name(self._database.path.name + "-wal"),
                allow_missing=True,
            )
            recovery_demand_bytes = database_bytes + max(database_bytes, wal_bytes)
            required_bytes = reserved_bytes + recovery_demand_bytes + MINIMUM_RESERVED_BYTES
            self._identity.assert_current()
            return DiskBudgetSnapshot(
                volume_serial=self._identity.volume_serial,
                capacity_bytes=capacity_bytes,
                free_bytes=free_bytes,
                database_bytes=database_bytes,
                wal_bytes=wal_bytes,
                recovery_demand_bytes=recovery_demand_bytes,
                reserved_bytes=reserved_bytes,
                minimum_reserved_bytes=MINIMUM_RESERVED_BYTES,
                required_bytes=required_bytes,
                safe=free_bytes >= required_bytes,
            )
        except Exception as error:
            return DiskBudgetSnapshot(
                volume_serial=None,
                capacity_bytes=None,
                free_bytes=None,
                database_bytes=None,
                wal_bytes=None,
                recovery_demand_bytes=None,
                reserved_bytes=reserved_bytes,
                minimum_reserved_bytes=MINIMUM_RESERVED_BYTES,
                required_bytes=None,
                safe=False,
                reason=type(error).__name__,
            )

    def _file_size(self, path: Path, *, allow_missing: bool) -> int:
        self._identity.assert_current()
        try:
            information = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return 0
            raise
        attributes = getattr(information, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(information.st_mode)
            or attributes & 0x0400
            or not stat.S_ISREG(information.st_mode)
            or type(information.st_size) is not int
            or not 0 <= information.st_size <= _MAX_SQLITE_BYTES
        ):
            raise ValueError("database space inventory is unsafe")
        return information.st_size

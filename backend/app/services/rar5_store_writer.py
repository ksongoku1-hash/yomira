"""Write the bounded RAR5 store-only subset used by Yomira CBR exports."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import UUID

from PIL import Image

from app.domain.asset import validate_relative_path
from app.errors import ErrorCode
from app.persistence.atomic_file_publisher import AtomicFilePublishError
from app.persistence.filesystem_retry import RetryingFileStream
from app.persistence.storage_gate import SharedStorageGate, is_disk_full_os_error
from app.persistence.windows_paths import DataRootIdentity
from app.tracing import create_trace_id, current_trace_id

logger = logging.getLogger("yomira.rar5_store_writer")

RAR5_SIGNATURE = b"Rar!\x1a\x07\x01\x00"
MAX_CBR_BYTES = 8 * 1024 * 1024 * 1024
MAX_CBR_PAGE_COUNT = 10_000
MAX_CBR_MEMBER_NAME_BYTES = 255
_COPY_CHUNK_BYTES = 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


class _Publisher(Protocol):
    def publish_no_replace(
        self,
        source: Path,
        destination: Path,
        *,
        job_id: UUID | str | None = None,
    ) -> None: ...


class _Hasher(Protocol):
    def update(self, data: bytes, /) -> None: ...


class _WritableBinary(Protocol):
    def write(self, data: bytes, /) -> int: ...


class Rar5WriteError(RuntimeError):
    """Sanitized failure while producing a store-only RAR5 archive."""

    def __init__(
        self,
        stage: str,
        *,
        trace_id: str,
        error_code: ErrorCode = ErrorCode.EXP_SOURCE_INVALID,
        cause_type: str | None = None,
    ) -> None:
        self.stage = stage
        self.trace_id = trace_id
        self.error_code = error_code
        self.cause_type = cause_type
        super().__init__(f"RAR5 archive creation failed during {stage}.")


@dataclass(frozen=True, slots=True)
class Rar5StoreFile:
    member_name: str
    source_path: Path
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if type(self.member_name) is not str:
            raise TypeError("member_name must be a string")
        try:
            source_path = Path(self.source_path)
        except (TypeError, ValueError):
            raise ValueError("source_path must be an absolute Path") from None
        if not source_path.is_absolute():
            raise ValueError("source_path must be an absolute Path")
        object.__setattr__(self, "source_path", source_path)
        if type(self.size_bytes) is not int or self.size_bytes <= 0:
            raise ValueError("size_bytes must be a positive integer")
        if type(self.sha256) is not str or _SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise ValueError("sha256 must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class Rar5ArchiveMember:
    name: str
    size_bytes: int
    sha256: str
    crc32: int
    data_offset: int


@dataclass(frozen=True, slots=True)
class Rar5WriteResult:
    path: Path
    size_bytes: int
    sha256: str
    page_count: int


@dataclass(frozen=True, slots=True)
class _PreparedFile:
    source: Rar5StoreFile
    encoded_name: bytes
    sha256: str
    crc32: int
    identity: tuple[int, int]


class _BodyReader:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.offset = 0

    def read(self, length: int) -> bytes:
        end = self.offset + length
        if length < 0 or end > len(self.body):
            raise ValueError("RAR5 block body is truncated")
        result = self.body[self.offset:end]
        self.offset = end
        return result

    def read_vint(self) -> int:
        value = 0
        shift = 0
        raw = bytearray()
        for _ in range(10):
            byte = self.read(1)[0]
            raw.append(byte)
            value |= (byte & 0x7F) << shift
            if byte & 0x80 == 0:
                if _encode_vint(value) != bytes(raw):
                    raise ValueError("RAR5 integer is not canonically encoded")
                return value
            shift += 7
        raise ValueError("RAR5 integer exceeds 64 bits")

    def ensure_consumed(self) -> None:
        if self.offset != len(self.body):
            raise ValueError("RAR5 block has unsupported fields")


def _encode_vint(value: int) -> bytes:
    if type(value) is not int or value < 0 or value > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("RAR5 integer is outside the unsigned 64-bit range")
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _header_block(body: bytes) -> bytes:
    sized_body = _encode_vint(len(body)) + body
    checksum = zlib.crc32(sized_body) & 0xFFFFFFFF
    return struct.pack("<I", checksum) + sized_body


def _safe_member_name(value: str) -> bytes:
    try:
        validate_relative_path(value)
        encoded = value.encode("utf-8", errors="strict")
    except (UnicodeError, ValueError):
        raise ValueError("member name is invalid") from None
    if (
        "/" in value
        or "\\" in value
        or len(encoded) > MAX_CBR_MEMBER_NAME_BYTES
        or not value.lower().endswith(".png")
    ):
        raise ValueError("member name is outside the PNG basename profile")
    return encoded


def _file_header(entry: _PreparedFile) -> bytes:
    name = entry.encoded_name
    body = b"".join(
        (
            _encode_vint(2),  # File header.
            _encode_vint(0x0002),  # Data area present.
            _encode_vint(entry.source.size_bytes),
            _encode_vint(0x0004),  # Data CRC32 present.
            _encode_vint(entry.source.size_bytes),
            _encode_vint(0),  # Host attributes.
            struct.pack("<I", entry.crc32),
            _encode_vint(0),  # RAR5 compression method 0: store, version 0.
            _encode_vint(0),  # Host OS: Windows.
            _encode_vint(len(name)),
            name,
        )
    )
    return _header_block(body)


def _main_header() -> bytes:
    return _header_block(_encode_vint(1) + _encode_vint(0) + _encode_vint(0))


def _end_header() -> bytes:
    return _header_block(_encode_vint(5) + _encode_vint(0) + _encode_vint(0))


def _managed_path(
    identity: DataRootIdentity,
    path: Path,
    *,
    role: str,
    allow_missing_leaf: bool = False,
) -> Path:
    identity.assert_current()
    root = identity.canonical_path
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"{role} is outside the managed root") from None
    current = root
    for index, component in enumerate(relative.parts):
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and index + 1 == len(relative.parts):
                break
            raise
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & _REPARSE_POINT_ATTRIBUTE:
            raise ValueError(f"{role} contains a reparse point")
        last = index + 1 == len(relative.parts)
        if last:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{role} is not a regular file")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{role} contains a non-directory component")
    return candidate


def _source_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(info.st_mode)
        or attributes & _REPARSE_POINT_ATTRIBUTE
        or not stat.S_ISREG(info.st_mode)
        or type(info.st_dev) is not int
        or type(info.st_ino) is not int
        or info.st_size <= 0
    ):
        raise ValueError("source is not a regular file")
    return info.st_dev, info.st_ino


def _read_exactly(stream: BinaryIO, length: int) -> bytes:
    result = stream.read(length)
    if len(result) != length:
        raise ValueError("RAR5 archive ended unexpectedly")
    return result


def _read_vint(stream: BinaryIO) -> tuple[int, bytes]:
    value = 0
    shift = 0
    raw = bytearray()
    for _ in range(10):
        byte = _read_exactly(stream, 1)[0]
        raw.append(byte)
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            encoded = bytes(raw)
            if _encode_vint(value) != encoded:
                raise ValueError("RAR5 integer is not canonically encoded")
            return value, encoded
        shift += 7
    raise ValueError("RAR5 integer exceeds 64 bits")


def _read_header(stream: BinaryIO) -> bytes:
    checksum = struct.unpack("<I", _read_exactly(stream, 4))[0]
    body_size, size_bytes = _read_vint(stream)
    if not 1 <= body_size <= 1024 * 1024:
        raise ValueError("RAR5 header size is outside the supported bound")
    body = _read_exactly(stream, body_size)
    if zlib.crc32(size_bytes + body) & 0xFFFFFFFF != checksum:
        raise ValueError("RAR5 header CRC does not match")
    return body


def inspect_rar5_store_archive(path: Path) -> tuple[Rar5ArchiveMember, ...]:
    """Read and validate only the RAR5 store-only profile emitted by this writer."""
    try:
        path = Path(path)
    except (TypeError, ValueError):
        raise ValueError("path must be an absolute Path") from None
    if not path.is_absolute():
        raise ValueError("path must be an absolute Path")
    members: list[Rar5ArchiveMember] = []
    aggregate_bytes = 0
    try:
        with path.open("rb") as stream:
            if _read_exactly(stream, len(RAR5_SIGNATURE)) != RAR5_SIGNATURE:
                raise ValueError("RAR5 signature is invalid")
            body_reader = _BodyReader(_read_header(stream))
            if (body_reader.read_vint(), body_reader.read_vint(), body_reader.read_vint()) != (
                1,
                0,
                0,
            ):
                raise ValueError("RAR5 main header is outside the supported profile")
            body_reader.ensure_consumed()

            while True:
                body_reader = _BodyReader(_read_header(stream))
                block_type = body_reader.read_vint()
                flags = body_reader.read_vint()
                if block_type == 5:
                    if flags != 0 or body_reader.read_vint() != 0:
                        raise ValueError("RAR5 end marker is outside the supported profile")
                    body_reader.ensure_consumed()
                    if stream.read(1):
                        raise ValueError("RAR5 archive contains trailing bytes")
                    break
                if block_type != 2 or flags != 0x0002:
                    raise ValueError("RAR5 contains an unsupported block")

                packed_size = body_reader.read_vint()
                file_flags = body_reader.read_vint()
                unpacked_size = body_reader.read_vint()
                attributes = body_reader.read_vint()
                if file_flags != 0x0004 or packed_size != unpacked_size or attributes != 0:
                    raise ValueError("RAR5 file header is outside the store-only profile")
                expected_crc32 = struct.unpack("<I", body_reader.read(4))[0]
                if body_reader.read_vint() != 0 or body_reader.read_vint() != 0:
                    raise ValueError("RAR5 file uses unsupported compression or host OS")
                name_size = body_reader.read_vint()
                if not 1 <= name_size <= MAX_CBR_MEMBER_NAME_BYTES:
                    raise ValueError("RAR5 member name size is invalid")
                name_bytes = body_reader.read(name_size)
                body_reader.ensure_consumed()
                name = name_bytes.decode("utf-8", errors="strict")
                if _safe_member_name(name) != name_bytes:
                    raise ValueError("RAR5 member name is not canonical")
                if any(member.name == name for member in members):
                    raise ValueError("RAR5 member name is duplicated")
                if len(members) >= MAX_CBR_PAGE_COUNT:
                    raise ValueError("RAR5 page limit is exceeded")
                aggregate_bytes += packed_size
                if aggregate_bytes > MAX_CBR_BYTES:
                    raise ValueError("RAR5 archive exceeds its aggregate byte limit")

                data_offset = stream.tell()
                crc32 = 0
                digest = hashlib.sha256()
                remaining = packed_size
                while remaining:
                    chunk = stream.read(min(_COPY_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ValueError("RAR5 member data is truncated")
                    remaining -= len(chunk)
                    crc32 = zlib.crc32(chunk, crc32)
                    digest.update(chunk)
                crc32 &= 0xFFFFFFFF
                if crc32 != expected_crc32:
                    raise Rar5WriteError(
                        "member_data_crc",
                        trace_id=current_trace_id() or create_trace_id(),
                        error_code=ErrorCode.EXP_HASH_MISMATCH,
                    )
                members.append(
                    Rar5ArchiveMember(
                        name=name,
                        size_bytes=packed_size,
                        sha256=digest.hexdigest(),
                        crc32=crc32,
                        data_offset=data_offset,
                    )
                )
            if not members:
                raise ValueError("RAR5 archive contains no PNG pages")
    except Rar5WriteError:
        raise
    except Exception as error:
        raise Rar5WriteError(
            "archive_verify",
            trace_id=current_trace_id() or create_trace_id(),
            error_code=ErrorCode.EXP_HASH_MISMATCH,
            cause_type=type(error).__name__,
        ) from None
    return tuple(members)


class Rar5StoreWriter:
    """Create a verified RAR5 archive containing only stored PNG members."""

    def __init__(
        self,
        *,
        publisher: _Publisher,
        data_root_identity: DataRootIdentity,
        max_archive_bytes: int = MAX_CBR_BYTES,
        storage_gate: SharedStorageGate | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not callable(getattr(publisher, "publish_no_replace", None)):
            raise TypeError("publisher must implement publish_no_replace")
        if type(data_root_identity) is not DataRootIdentity:
            raise TypeError("data_root_identity must be a DataRootIdentity")
        if type(max_archive_bytes) is not int or not 1 <= max_archive_bytes <= MAX_CBR_BYTES:
            raise ValueError("max_archive_bytes must be within the approved CBR bound")
        if storage_gate is not None and type(storage_gate) is not SharedStorageGate:
            raise TypeError("storage_gate must be a SharedStorageGate or None")
        if sleep is not None and not callable(sleep):
            raise TypeError("sleep must be callable or None")
        self._publisher = publisher
        self._identity = data_root_identity
        self._max_archive_bytes = max_archive_bytes
        self._storage_gate = storage_gate
        self._sleep = sleep

    def write(
        self,
        files: tuple[Rar5StoreFile, ...],
        *,
        temporary_path: Path,
        destination_path: Path,
        operation_id: UUID | None = None,
    ) -> Rar5WriteResult:
        trace_id = current_trace_id() or create_trace_id()
        temporary_created = False
        temporary_identity: tuple[int, int] | None = None
        try:
            prepared = self._prepare(files, trace_id=trace_id)
            if operation_id is not None and (
                type(operation_id) is not UUID or operation_id.version != 4
            ):
                raise Rar5WriteError("operation_id_invalid", trace_id=trace_id)
            temp = _managed_path(
                self._identity,
                temporary_path,
                role="temporary path",
                allow_missing_leaf=True,
            )
            destination = _managed_path(
                self._identity,
                destination_path,
                role="destination path",
                allow_missing_leaf=True,
            )
            if temp == destination:
                raise Rar5WriteError("path_conflict", trace_id=trace_id)
            if temp.exists():
                raise Rar5WriteError("temporary_conflict", trace_id=trace_id)
            if destination.exists():
                raise Rar5WriteError(
                    "destination_conflict",
                    trace_id=trace_id,
                    error_code=ErrorCode.EXP_DESTINATION_CONFLICT,
                )
            expected_size = self._estimate_size(prepared)
            if expected_size > self._max_archive_bytes:
                raise Rar5WriteError(
                    "archive_size_limit_preflight",
                    trace_id=trace_id,
                    error_code=ErrorCode.EXP_SOURCE_INVALID,
                )

            output = RetryingFileStream.create(
                temp,
                component="rar5_store_writer",
                stage="rar5_staging",
                trace_id=trace_id,
                job_id=str(operation_id) if operation_id is not None else None,
                validate_path=lambda: self._validate_owned_parent(temp),
                sleep=self._sleep,
                storage_gate=self._storage_gate,
            )
            temporary_created = True
            info = os.fstat(output.fileno())
            temporary_identity = (info.st_dev, info.st_ino)
            output_error: BaseException | None = None
            try:
                digest = hashlib.sha256()
                self._write_all(output, RAR5_SIGNATURE, digest)
                self._write_all(output, _main_header(), digest)
                for entry in prepared:
                    self._write_all(output, _file_header(entry), digest)
                    self._copy_entry(output, entry, digest, trace_id=trace_id)
                self._write_all(output, _end_header(), digest)
                output.sync()
            except BaseException as error:
                output_error = error
            try:
                output.close()
            except BaseException as error:
                if output_error is None:
                    output_error = error
                else:
                    output_error.add_note(
                        f"RAR5 staging close also failed ({type(error).__name__})."
                    )
            if output_error is not None:
                raise output_error

            info = _managed_path(self._identity, temp, role="temporary archive").stat()
            if info.st_size != expected_size:
                raise Rar5WriteError(
                    "archive_size_mismatch",
                    trace_id=trace_id,
                    error_code=ErrorCode.EXP_HASH_MISMATCH,
                )
            verified = inspect_rar5_store_archive(temp)
            if (
                len(verified) != len(prepared)
                or any(
                    member.name != entry.source.member_name
                    or member.size_bytes != entry.source.size_bytes
                    or member.sha256 != entry.sha256
                    or member.crc32 != entry.crc32
                    for member, entry in zip(verified, prepared, strict=True)
                )
            ):
                raise Rar5WriteError(
                    "archive_member_mismatch",
                    trace_id=trace_id,
                    error_code=ErrorCode.EXP_HASH_MISMATCH,
                )
            self._publisher.publish_no_replace(
                temp,
                destination,
                job_id=operation_id,
            )
            published = _managed_path(self._identity, destination, role="published archive")
            result_size, result_sha256 = self._hash_file(published)
            if result_size != expected_size:
                raise Rar5WriteError(
                    "published_size_mismatch",
                    trace_id=trace_id,
                    error_code=ErrorCode.EXP_HASH_MISMATCH,
                )
            return Rar5WriteResult(
                path=published,
                size_bytes=result_size,
                sha256=result_sha256,
                page_count=len(prepared),
            )
        except Rar5WriteError as error:
            self._log_failure(error)
            raise
        except AtomicFilePublishError as error:
            failure = Rar5WriteError(
                "publication",
                trace_id=error.trace_id,
                error_code=(
                    ErrorCode.EXP_DESTINATION_CONFLICT
                    if error.stage == "destination_conflict"
                    else ErrorCode.EXP_PUBLISH_FAILED
                ),
                cause_type=error.cause_type,
            )
            self._log_failure(failure)
            raise failure from None
        except Exception as error:
            code = (
                ErrorCode.EXP_DISK_FULL
                if isinstance(error, OSError) and is_disk_full_os_error(error)
                else ErrorCode.EXP_COPY_FAILED
            )
            failure = Rar5WriteError(
                "write",
                trace_id=trace_id,
                error_code=code,
                cause_type=type(error).__name__,
            )
            self._log_failure(failure)
            raise failure from None
        finally:
            if temporary_created and temporary_identity is not None:
                self._remove_owned_temporary(temporary_path, temporary_identity)

    def _prepare(
        self,
        files: tuple[Rar5StoreFile, ...],
        *,
        trace_id: str,
    ) -> tuple[_PreparedFile, ...]:
        if (
            type(files) is not tuple
            or not 1 <= len(files) <= MAX_CBR_PAGE_COUNT
            or any(type(entry) is not Rar5StoreFile for entry in files)
        ):
            raise Rar5WriteError("page_count_invalid", trace_id=trace_id)
        prepared: list[_PreparedFile] = []
        names: set[str] = set()
        aggregate_bytes = 0
        for entry in files:
            try:
                name = _safe_member_name(entry.member_name)
            except ValueError as error:
                raise Rar5WriteError(
                    "member_name_invalid",
                    trace_id=trace_id,
                    error_code=ErrorCode.EXP_SOURCE_INVALID,
                    cause_type=type(error).__name__,
                ) from None
            try:
                path = _managed_path(self._identity, entry.source_path, role="source page")
                identity = _source_identity(path)
                if path.stat().st_size != entry.size_bytes:
                    raise ValueError("source size differs from its metadata")
                if entry.member_name in names:
                    raise Rar5WriteError("member_name_invalid", trace_id=trace_id)
                names.add(entry.member_name)
                digest, crc32 = self._inspect_png(path, entry.size_bytes)
                if digest != entry.sha256:
                    raise ValueError("source hash differs from its metadata")
                aggregate_bytes += entry.size_bytes
                if aggregate_bytes > self._max_archive_bytes:
                    raise Rar5WriteError(
                        "staged_png_size_limit",
                        trace_id=trace_id,
                        error_code=ErrorCode.EXP_SOURCE_INVALID,
                    )
                prepared.append(
                    _PreparedFile(
                        source=entry,
                        encoded_name=name,
                        sha256=digest,
                        crc32=crc32,
                        identity=identity,
                    )
                )
            except Rar5WriteError:
                raise
            except Exception as error:
                raise Rar5WriteError(
                    "source_verify",
                    trace_id=trace_id,
                    error_code=ErrorCode.EXP_SOURCE_INVALID,
                    cause_type=type(error).__name__,
                ) from None
        return tuple(prepared)

    @staticmethod
    def _inspect_png(path: Path, expected_size: int) -> tuple[str, int]:
        digest = hashlib.sha256()
        crc32 = 0
        size = 0
        with path.open("rb") as stream:
            signature = stream.read(len(_PNG_SIGNATURE))
            if signature != _PNG_SIGNATURE:
                raise ValueError("staged page is not a PNG")
            stream.seek(0)
            while chunk := stream.read(_COPY_CHUNK_BYTES):
                size += len(chunk)
                if size > expected_size:
                    raise ValueError("source grew while being inspected")
                digest.update(chunk)
                crc32 = zlib.crc32(chunk, crc32)
        if size != expected_size:
            raise ValueError("source size changed while being inspected")
        with Image.open(path) as image:
            if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                raise ValueError("staged page format is not a single-frame PNG")
            image.verify()
        return digest.hexdigest(), crc32 & 0xFFFFFFFF

    @staticmethod
    def _estimate_size(files: tuple[_PreparedFile, ...]) -> int:
        size = len(RAR5_SIGNATURE) + len(_main_header()) + len(_end_header())
        for entry in files:
            header = _file_header(entry)
            size += len(header) + entry.source.size_bytes
        return size

    @staticmethod
    def _write_all(stream: _WritableBinary, value: bytes, digest: _Hasher) -> None:
        offset = 0
        while offset < len(value):
            written = stream.write(value[offset:])
            if type(written) is not int or written <= 0:
                raise OSError("RAR5 writer made no forward progress")
            digest.update(value[offset : offset + written])
            offset += written

    def _copy_entry(
        self,
        output: _WritableBinary,
        entry: _PreparedFile,
        archive_digest: _Hasher,
        *,
        trace_id: str,
    ) -> None:
        path = _managed_path(self._identity, entry.source.source_path, role="source page")
        if _source_identity(path) != entry.identity:
            raise Rar5WriteError(
                "source_changed",
                trace_id=trace_id,
                error_code=ErrorCode.EXP_SOURCE_INVALID,
            )
        source_digest = hashlib.sha256()
        source_crc32 = 0
        copied_bytes = 0
        with path.open("rb") as source:
            handle_info = os.fstat(source.fileno())
            if (handle_info.st_dev, handle_info.st_ino) != entry.identity:
                raise Rar5WriteError("source_changed", trace_id=trace_id)
            while chunk := source.read(_COPY_CHUNK_BYTES):
                copied_bytes += len(chunk)
                if copied_bytes > entry.source.size_bytes:
                    raise Rar5WriteError("source_changed", trace_id=trace_id)
                self._write_all(output, chunk, archive_digest)
                source_digest.update(chunk)
                source_crc32 = zlib.crc32(chunk, source_crc32)
            final_info = os.fstat(source.fileno())
        if (
            copied_bytes != entry.source.size_bytes
            or source_digest.hexdigest() != entry.sha256
            or source_crc32 & 0xFFFFFFFF != entry.crc32
            or (final_info.st_dev, final_info.st_ino) != entry.identity
            or _source_identity(path) != entry.identity
        ):
            raise Rar5WriteError("source_changed", trace_id=trace_id)

    def _validate_owned_parent(self, path: Path) -> None:
        self._identity.assert_current()
        parent = path.parent
        info = parent.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(info.st_mode)
            or attributes & _REPARSE_POINT_ATTRIBUTE
            or not stat.S_ISDIR(info.st_mode)
        ):
            raise OSError("RAR5 staging directory changed")

    def _remove_owned_temporary(
        self,
        path: Path,
        expected_identity: tuple[int, int],
    ) -> None:
        try:
            info = path.lstat()
            attributes = getattr(info, "st_file_attributes", 0)
            if (
                stat.S_ISREG(info.st_mode)
                and not attributes & _REPARSE_POINT_ATTRIBUTE
                and (info.st_dev, info.st_ino) == expected_identity
            ):
                path.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            logger.warning(
                "rar5_store_writer.temporary_cleanup_failed",
                extra={
                    "stage": "temporary_cleanup",
                    "cause_type": type(error).__name__,
                    "trace_id": current_trace_id() or create_trace_id(),
                },
            )

    def _log_failure(self, error: Rar5WriteError) -> None:
        logger.error(
            "rar5_store_writer.failed",
            extra={
                "error_code": error.error_code.value,
                "stage": error.stage,
                "retryable": error.error_code is ErrorCode.EXP_DISK_FULL,
                "cause_type": error.cause_type or type(error).__name__,
                "trace_id": error.trace_id,
            },
        )

    @staticmethod
    def _hash_file(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(_COPY_CHUNK_BYTES):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

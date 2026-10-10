"""Strict, bounded, versioned JSON representation for one download job."""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import cast
from uuid import UUID

from app.domain.download_page import DownloadPage

CURRENT_MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_PAGE_COUNT = 10_000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_JSON_DEPTH = 32
MAX_MANIFEST_JSON_TOKENS = 430_000
MAX_MANIFEST_JSON_NUMBER_CHARS = 20
MAX_SQLITE_INTEGER = (1 << 63) - 1

_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "source_id",
        "chapter_id",
        "expected_pages",
        "pages",
    }
)
_PAGE_FIELDS = frozenset(
    {
        "ordinal",
        "remote_reference",
        "temporary_path",
        "status",
        "size",
        "sha256",
        "width",
        "height",
        "mime",
        "attempts",
    }
)
_JSON_SHORT_ESCAPES = frozenset({'"', "\\", "\b", "\t", "\n", "\f", "\r"})


class ManifestError(ValueError):
    """Base class for invalid or unsupported manifest data."""


class InvalidManifestError(ManifestError):
    """Raised when a v1 manifest violates its shape or field invariants."""


class UnsupportedManifestVersion(ManifestError):
    """Raised when a manifest uses a schema version this runtime cannot read."""


class ManifestPageStatus(StrEnum):
    PENDING = "PENDING"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    VALIDATED = "VALIDATED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


def _validate_page_status_fields(
    page: DownloadPage,
    status: ManifestPageStatus,
) -> None:
    for field_name, value in (
        ("size", page.size_bytes),
        ("width", page.width),
        ("height", page.height),
        ("attempts", page.attempts),
    ):
        if value is not None and value > MAX_SQLITE_INTEGER:
            raise InvalidManifestError(f"{field_name} exceeds the SQLite integer range.")

    if status is ManifestPageStatus.DOWNLOADED:
        if (
            page.temporary_path is None
            or not page.temporary_path.casefold().endswith(".part")
            or page.size_bytes is None
            or page.sha256 is None
        ):
            raise InvalidManifestError(
                "DOWNLOADED pages require a .part path, size, and SHA-256."
            )
    elif status is ManifestPageStatus.VALIDATED and (
        page.temporary_path is None
        or page.temporary_path.casefold().endswith(".part")
        or page.size_bytes is None
        or page.sha256 is None
        or page.width is None
        or page.height is None
        or page.mime is None
    ):
        raise InvalidManifestError(
            "VALIDATED pages require a staged path and complete image metadata."
        )


def _json_string_encoded_size(value: str) -> int:
    """Return a bounded exact UTF-8 size for one JSON string token."""
    size = 2  # surrounding quotes
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise InvalidManifestError("Manifest contains invalid Unicode.")
        if character in _JSON_SHORT_ESCAPES:
            size += 2
        elif codepoint < 0x20:
            size += 6  # \u00xx
        elif codepoint <= 0x7F:
            size += 1
        elif codepoint <= 0x7FF:
            size += 2
        elif codepoint <= 0xFFFF:
            size += 3
        else:
            size += 4
        if size > MAX_MANIFEST_BYTES:
            return MAX_MANIFEST_BYTES + 1
    return size


def _bounded_json_encoded_size(value: object) -> int:
    """Measure canonical JSON without allocating an over-limit output string."""
    if value is None:
        return 4
    if type(value) is bool:
        return 4 if value else 5
    if type(value) is int:
        return len(str(value))
    if type(value) is str:
        return _json_string_encoded_size(value)
    if type(value) is list:
        size = 2
        items = cast(list[object], value)
        for index, item in enumerate(items):
            if index:
                size += 1
            size += _bounded_json_encoded_size(item)
            if size > MAX_MANIFEST_BYTES:
                return MAX_MANIFEST_BYTES + 1
        return size
    if type(value) is dict:
        size = 2
        mapping = cast(dict[object, object], value)
        if any(type(key) is not str for key in mapping):
            raise InvalidManifestError("Manifest object keys must be strings.")
        string_mapping = cast(dict[str, object], mapping)
        for index, key in enumerate(sorted(string_mapping)):
            if index:
                size += 1
            size += _json_string_encoded_size(key)
            size += 1  # colon
            size += _bounded_json_encoded_size(string_mapping[key])
            if size > MAX_MANIFEST_BYTES:
                return MAX_MANIFEST_BYTES + 1
        return size
    raise InvalidManifestError("Manifest contains an unsupported JSON value.")


@dataclass(frozen=True, slots=True)
class DownloadJobManifest:
    job_id: UUID
    source_id: UUID
    chapter_id: UUID
    expected_pages: int
    pages: tuple[DownloadPage, ...]
    schema_version: int = CURRENT_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("job_id", "source_id", "chapter_id"):
            value = getattr(self, name)
            if type(value) is not UUID or value.version != 4:
                raise InvalidManifestError(f"{name} must be a UUIDv4.")
        if type(self.schema_version) is not int:
            raise InvalidManifestError("schema_version must be an exact integer.")
        if self.schema_version != CURRENT_MANIFEST_SCHEMA_VERSION:
            raise UnsupportedManifestVersion("Unsupported manifest schema version.")
        if (
            type(self.expected_pages) is not int
            or not 0 <= self.expected_pages <= MAX_MANIFEST_PAGE_COUNT
        ):
            raise InvalidManifestError("expected_pages is outside the v1 limit.")
        if type(self.pages) is not tuple:
            raise InvalidManifestError("pages must be an immutable tuple.")
        if len(self.pages) != self.expected_pages:
            raise InvalidManifestError("expected_pages does not match pages length.")

        temporary_path_keys: set[str] = set()
        for ordinal, page in enumerate(self.pages, start=1):
            if type(page) is not DownloadPage or page.job_id != self.job_id:
                raise InvalidManifestError("Each page must belong to this job.")
            if page.ordinal != ordinal:
                raise InvalidManifestError("Page ordinals must be contiguous from one.")
            if page.temporary_path is not None:
                temporary_path_key = page.temporary_path.casefold()
                if temporary_path_key in temporary_path_keys:
                    raise InvalidManifestError(
                        "Pages must not share a case-insensitive temporary_path."
                    )
                temporary_path_keys.add(temporary_path_key)
            try:
                status = ManifestPageStatus(page.status)
            except ValueError:
                raise InvalidManifestError("Page status is not supported by schema v1.") from None
            _validate_page_status_fields(page, status)


def manifest_to_bytes(manifest: DownloadJobManifest) -> bytes:
    """Serialize a validated manifest as deterministic, bounded UTF-8 JSON."""
    if type(manifest) is not DownloadJobManifest:
        raise TypeError("manifest must be a DownloadJobManifest.")

    payload = {
        "schema_version": manifest.schema_version,
        "job_id": str(manifest.job_id),
        "source_id": str(manifest.source_id),
        "chapter_id": str(manifest.chapter_id),
        "expected_pages": manifest.expected_pages,
        "pages": [
            {
                "ordinal": page.ordinal,
                "remote_reference": page.remote_reference,
                "temporary_path": page.temporary_path,
                "status": page.status,
                "size": page.size_bytes,
                "sha256": page.sha256,
                "width": page.width,
                "height": page.height,
                "mime": page.mime,
                "attempts": page.attempts,
            }
            for page in manifest.pages
        ],
    }
    if _bounded_json_encoded_size(payload) > MAX_MANIFEST_BYTES:
        raise InvalidManifestError("Manifest exceeds the v1 byte limit.")
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError):
        raise InvalidManifestError("Manifest contains a value that cannot be encoded.") from None
    if len(serialized) > MAX_MANIFEST_BYTES:
        raise InvalidManifestError("Manifest exceeds the v1 byte limit.")
    return serialized


def manifest_from_bytes(serialized: bytes) -> DownloadJobManifest:
    """Parse a bounded UTF-8 manifest without accepting future or lossy data."""
    if type(serialized) is not bytes:
        raise TypeError("serialized must be bytes.")
    if len(serialized) > MAX_MANIFEST_BYTES:
        raise InvalidManifestError("Manifest exceeds the v1 byte limit.")

    try:
        decoded = serialized.decode("utf-8")
        _preflight_json_complexity(decoded)
        payload: object = json.loads(
            decoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonfinite_number,
        )
    except InvalidManifestError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InvalidManifestError("Manifest JSON is malformed.") from None

    if type(payload) is not dict:
        raise InvalidManifestError("Manifest root must be a JSON object.")
    root_payload = cast(dict[str, object], payload)
    raw_version = root_payload.get("schema_version")
    if type(raw_version) is not int:
        raise InvalidManifestError("schema_version must be an exact integer.")
    if raw_version != CURRENT_MANIFEST_SCHEMA_VERSION:
        raise UnsupportedManifestVersion("Unsupported manifest schema version.")
    root = _require_exact_fields(root_payload, _ROOT_FIELDS, "manifest")

    job_id = _parse_canonical_uuid4(root["job_id"], "job_id")
    source_id = _parse_canonical_uuid4(root["source_id"], "source_id")
    chapter_id = _parse_canonical_uuid4(root["chapter_id"], "chapter_id")
    expected_pages = _require_exact_type(root["expected_pages"], int, "expected_pages")
    if not 0 <= expected_pages <= MAX_MANIFEST_PAGE_COUNT:
        raise InvalidManifestError("expected_pages is outside the v1 limit.")

    raw_pages = root["pages"]
    if type(raw_pages) is not list:
        raise InvalidManifestError("pages must be a JSON array.")
    page_values = cast(list[object], raw_pages)
    if len(page_values) > MAX_MANIFEST_PAGE_COUNT or len(page_values) != expected_pages:
        raise InvalidManifestError("Page count does not match the v1 manifest limits.")

    pages = tuple(_parse_page(job_id, raw_page) for raw_page in page_values)
    return DownloadJobManifest(
        schema_version=raw_version,
        job_id=job_id,
        source_id=source_id,
        chapter_id=chapter_id,
        expected_pages=expected_pages,
        pages=pages,
    )


def _parse_page(job_id: UUID, raw_page: object) -> DownloadPage:
    page = _require_exact_fields(raw_page, _PAGE_FIELDS, "page")
    ordinal = _require_exact_type(page["ordinal"], int, "ordinal")
    remote_reference = _require_utf8_string(page["remote_reference"], "remote_reference")
    temporary_path = _require_optional_utf8_string(page["temporary_path"], "temporary_path")
    status = _require_utf8_string(page["status"], "status")
    size_bytes = _require_optional_exact_type(page["size"], int, "size")
    sha256 = _require_optional_utf8_string(page["sha256"], "sha256")
    width = _require_optional_exact_type(page["width"], int, "width")
    height = _require_optional_exact_type(page["height"], int, "height")
    mime = _require_optional_utf8_string(page["mime"], "mime")
    attempts = _require_exact_type(page["attempts"], int, "attempts")
    try:
        return DownloadPage.new_local(
            job_id=job_id,
            ordinal=ordinal,
            remote_reference=remote_reference,
            temporary_path=temporary_path,
            status=status,
            size_bytes=size_bytes,
            sha256=sha256,
            width=width,
            height=height,
            mime=mime,
            attempts=attempts,
        )
    except ValueError:
        raise InvalidManifestError("Page fields violate the v1 domain invariants.") from None


def _require_exact_fields(
    value: object,
    expected_fields: frozenset[str],
    field_name: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise InvalidManifestError(f"{field_name} has missing or unknown fields.")
    mapping = cast(dict[object, object], value)
    if (
        any(type(key) is not str for key in mapping)
        or frozenset(mapping) != expected_fields
    ):
        raise InvalidManifestError(f"{field_name} has missing or unknown fields.")
    return cast(dict[str, object], mapping)


def _parse_canonical_uuid4(value: object, field_name: str) -> UUID:
    text = _require_utf8_string(value, field_name)
    try:
        parsed = UUID(text)
    except ValueError:
        raise InvalidManifestError(f"{field_name} must be a canonical UUIDv4.") from None
    if parsed.version != 4 or str(parsed) != text:
        raise InvalidManifestError(f"{field_name} must be a canonical UUIDv4.")
    return parsed


def _require_exact_type[T](
    value: object,
    expected_type: type[T],
    field_name: str,
) -> T:
    if type(value) is not expected_type:
        raise InvalidManifestError(f"{field_name} has an invalid type.")
    return cast(T, value)


def _require_optional_exact_type[T](
    value: object,
    expected_type: type[T],
    field_name: str,
) -> T | None:
    if value is None:
        return None
    return _require_exact_type(value, expected_type, field_name)


def _require_utf8_string(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise InvalidManifestError(f"{field_name} must be a string.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidManifestError(f"{field_name} is not valid Unicode.") from None
    return value


def _require_optional_utf8_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_utf8_string(value, field_name)


class _InvalidJsonValue(ValueError):
    pass


def _preflight_json_complexity(decoded: str) -> None:
    """Bound JSON nesting and token count before materializing Python objects."""
    depth = 0
    token_count = 0
    inside_string = False
    escaped = False
    previous_significant: str | None = None

    for index, character in enumerate(decoded):
        if inside_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                inside_string = False
            continue

        if character in " \t\r\n":
            continue
        if character == '"':
            token_count += 1
            inside_string = True
        elif character in "[{":
            token_count += 1
            depth += 1
            if depth > MAX_MANIFEST_JSON_DEPTH:
                raise InvalidManifestError("Manifest JSON exceeds the v1 nesting limit.")
        elif character in "]}":
            token_count += 1
            depth -= 1
        elif character in ":,":
            token_count += 1
        elif character in "-0123456789tfnNI":
            if previous_significant is None or previous_significant in "[:,":
                token_count += 1
                if character in "-0123456789":
                    token_end = index + 1
                    while (
                        token_end < len(decoded)
                        and decoded[token_end] not in " \t\r\n,]}:"
                    ):
                        token_end += 1
                    if token_end - index > MAX_MANIFEST_JSON_NUMBER_CHARS:
                        raise InvalidManifestError(
                            "Manifest JSON contains an overlong numeric token."
                        )

        if token_count > MAX_MANIFEST_JSON_TOKENS:
            raise InvalidManifestError("Manifest JSON exceeds the v1 token limit.")
        previous_significant = character


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _InvalidJsonValue
        value[key] = item
    return value


def _reject_nonfinite_number(_value: str) -> None:
    raise _InvalidJsonValue

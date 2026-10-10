import re
from dataclasses import dataclass
from uuid import UUID

_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
# K101 permits the initial request plus its three bounded retries.
MAX_DOWNLOAD_PAGE_ATTEMPTS = 4


def validate_temporary_path(temporary_path: str) -> None:
    if not temporary_path:
        raise ValueError("temporary_path must not be empty when provided")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in temporary_path):
        raise ValueError("temporary_path must not contain Unicode surrogate code points")
    if temporary_path.startswith("/") or "\\" in temporary_path or ":" in temporary_path:
        raise ValueError("temporary_path must be relative and use POSIX separators")

    components = temporary_path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("temporary_path must not escape its root or contain empty components")

    for component in components:
        utf16_code_units = sum(
            2 if ord(character) > 0xFFFF else 1 for character in component
        )
        if utf16_code_units > 255:
            raise ValueError(
                "temporary_path components must not exceed 255 UTF-16 code units"
            )
        if component.endswith((" ", ".")):
            raise ValueError("temporary_path components must not end with a space or dot")
        if any(
            ord(character) < 32 or character in _WINDOWS_INVALID_CHARACTERS
            for character in component
        ):
            raise ValueError("temporary_path contains a character invalid on Windows")

        device_basename = component.split(".", maxsplit=1)[0].rstrip(" .").upper()
        if (
            device_basename in _WINDOWS_RESERVED_NAMES
            or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", device_basename) is not None
        ):
            raise ValueError("temporary_path contains a reserved Windows device name")


@dataclass(frozen=True, slots=True)
class DownloadPage:
    job_id: UUID
    ordinal: int
    remote_reference: str
    temporary_path: str | None
    status: str
    size_bytes: int | None
    sha256: str | None
    width: int | None
    height: int | None
    mime: str | None
    attempts: int

    def __post_init__(self) -> None:
        if self.job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        if type(self.ordinal) is not int or self.ordinal <= 0:
            raise ValueError("ordinal must be a positive integer")
        if type(self.remote_reference) is not str or not self.remote_reference:
            raise ValueError("remote_reference must not be empty")
        if type(self.status) is not str or not self.status:
            raise ValueError("status must be a non-empty string")

        if self.temporary_path is not None:
            if type(self.temporary_path) is not str:
                raise ValueError("temporary_path must be a relative string or None")
            validate_temporary_path(self.temporary_path)
        if self.size_bytes is not None and (
            type(self.size_bytes) is not int or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer or None")
        if self.sha256 is not None and _SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters or None")
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height must both be present or both be None")
        if self.width is not None and self.height is not None:
            if (
                type(self.width) is not int
                or type(self.height) is not int
                or self.width <= 0
                or self.height <= 0
            ):
                raise ValueError("width and height must be positive integers")
        if self.mime is not None and (type(self.mime) is not str or not self.mime):
            raise ValueError("mime must not be empty when provided")
        if type(self.attempts) is not int or self.attempts < 0:
            raise ValueError("attempts must be a non-negative integer")

    @classmethod
    def new_local(
        cls,
        job_id: UUID,
        ordinal: int,
        remote_reference: str,
        *,
        temporary_path: str | None = None,
        status: str = "PENDING",
        size_bytes: int | None = None,
        sha256: str | None = None,
        width: int | None = None,
        height: int | None = None,
        mime: str | None = None,
        attempts: int = 0,
    ) -> DownloadPage:
        return cls(
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

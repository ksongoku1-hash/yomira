import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from app.domain.timestamps import normalise_utc

_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class AssetType(StrEnum):
    COVER = "COVER"
    THUMBNAIL = "THUMBNAIL"
    CBZ = "CBZ"
    CBR = "CBR"
    PAGE_CACHE = "PAGE_CACHE"
    TEMP = "TEMP"
    EXPORT_COPY = "EXPORT_COPY"


def validate_relative_path(relative_path: object) -> None:
    if not isinstance(relative_path, str):
        raise ValueError("relative_path must be a string")
    if not relative_path:
        raise ValueError("relative_path must not be empty")
    if relative_path.startswith("/") or "\\" in relative_path or ":" in relative_path:
        raise ValueError("relative_path must be a relative POSIX-style path")

    components = relative_path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("relative_path must not contain empty, dot, or parent components")

    for component in components:
        if component.endswith((" ", ".")):
            raise ValueError("relative_path components must not end with a space or dot")
        if any(
            ord(character) < 32 or character in _WINDOWS_INVALID_CHARACTERS
            for character in component
        ):
            raise ValueError("relative_path contains a character invalid on Windows")

        device_basename = component.split(".", maxsplit=1)[0].rstrip(" .").upper()
        if (
            device_basename in _WINDOWS_RESERVED_NAMES
            or re.fullmatch(r"(?:COM|LPT)[1-9]", device_basename) is not None
        ):
            raise ValueError("relative_path contains a reserved Windows device name")


@dataclass(frozen=True, slots=True)
class Asset:
    asset_id: UUID
    asset_type: AssetType
    relative_path: str
    sha256: str
    size_bytes: int
    created_at: datetime
    verified_at: datetime | None

    def __post_init__(self) -> None:
        if self.asset_id.version != 4:
            raise ValueError("asset_id must be a UUIDv4")

        try:
            asset_type = AssetType(self.asset_type)
        except (TypeError, ValueError) as error:
            raise ValueError("asset_type must use a supported AssetType") from error
        object.__setattr__(self, "asset_type", asset_type)

        validate_relative_path(self.relative_path)
        if _SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")

        object.__setattr__(self, "created_at", normalise_utc(self.created_at, "created_at"))
        if self.verified_at is not None:
            object.__setattr__(
                self,
                "verified_at",
                normalise_utc(self.verified_at, "verified_at"),
            )

    @classmethod
    def new_local(
        cls,
        asset_type: AssetType,
        relative_path: str,
        sha256: str,
        size_bytes: int,
        *,
        created_at: datetime | None = None,
        verified_at: datetime | None = None,
        asset_id: UUID | None = None,
    ) -> Asset:
        return cls(
            asset_id=asset_id if asset_id is not None else uuid4(),
            asset_type=asset_type,
            relative_path=relative_path,
            sha256=sha256,
            size_bytes=size_bytes,
            created_at=created_at if created_at is not None else datetime.now(UTC),
            verified_at=verified_at,
        )

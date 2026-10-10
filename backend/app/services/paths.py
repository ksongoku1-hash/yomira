"""Canonical resolution of library-relative paths without file mutation."""

from enum import StrEnum
from pathlib import Path

from app.domain.asset import validate_relative_path
from app.errors import ErrorCode


class LibraryPathErrorReason(StrEnum):
    INVALID_RELATIVE_PATH = "invalid_relative_path"
    ROOT_UNAVAILABLE = "root_unavailable"
    PATH_OUTSIDE_ROOT = "path_outside_root"
    RESOLUTION_FAILED = "resolution_failed"


_ERROR_MESSAGES = {
    LibraryPathErrorReason.INVALID_RELATIVE_PATH: "The library-relative path is invalid.",
    LibraryPathErrorReason.ROOT_UNAVAILABLE: "The configured library root is unavailable.",
    LibraryPathErrorReason.PATH_OUTSIDE_ROOT: "The resolved path is outside the library root.",
    LibraryPathErrorReason.RESOLUTION_FAILED: "The library-relative path could not be resolved.",
}


class LibraryPathError(RuntimeError):
    """A sanitized failure to resolve a path beneath the configured library root."""

    code = ErrorCode.LIB_PATH_UNAVAILABLE
    retryable = False

    def __init__(self, reason: LibraryPathErrorReason) -> None:
        self.reason = reason
        super().__init__(_ERROR_MESSAGES[reason])


class LibraryPathMapper:
    """Resolve canonical library-relative paths while preserving root containment."""

    def __init__(self, library_root: Path) -> None:
        if not library_root.is_absolute():
            raise ValueError("library_root must be an absolute path")
        self._library_root = library_root

    def resolve(self, relative_path: str) -> Path:
        try:
            validate_relative_path(relative_path)
        except (TypeError, ValueError):
            raise LibraryPathError(LibraryPathErrorReason.INVALID_RELATIVE_PATH) from None

        try:
            root = self._library_root.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise LibraryPathError(LibraryPathErrorReason.ROOT_UNAVAILABLE) from None
        if not root.is_dir():
            raise LibraryPathError(LibraryPathErrorReason.ROOT_UNAVAILABLE)

        try:
            target = root.joinpath(*relative_path.split("/")).resolve(strict=False)
            if not target.is_relative_to(root):
                raise LibraryPathError(LibraryPathErrorReason.PATH_OUTSIDE_ROOT)
            return target
        except LibraryPathError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise LibraryPathError(LibraryPathErrorReason.RESOLUTION_FAILED) from None

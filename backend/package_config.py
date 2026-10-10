"""Validated resource inputs for the Windows PyInstaller bundle."""

from dataclasses import dataclass
from pathlib import Path


class PackageInputError(RuntimeError):
    """Raised when a required, repository-owned bundle input is unavailable."""


@dataclass(frozen=True, slots=True)
class PackageInput:
    source: Path
    destination: str


def _require_file(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PackageInputError(f"Required {label} is missing.") from error
    if not resolved.is_file() or path.is_symlink():
        raise PackageInputError(f"Required {label} is not a regular file.")
    return resolved


def _require_directory(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PackageInputError(f"Required {label} is missing.") from error
    if not resolved.is_dir() or path.is_symlink():
        raise PackageInputError(f"Required {label} is not a regular directory.")
    return resolved


def discover_package_inputs(repository_root: Path) -> tuple[PackageInput, ...]:
    """Resolve only the web build and Alembic files the frozen app needs."""
    try:
        root = repository_root.resolve(strict=True)
    except OSError as error:
        raise PackageInputError("Repository root is missing.") from error
    if not root.is_dir():
        raise PackageInputError("Repository root is not a directory.")

    frontend = _require_directory(
        root / "frontend" / "dist",
        label="frontend build directory",
    )
    _require_file(frontend / "index.html", label="frontend build index")
    _require_directory(frontend / "assets", label="frontend assets directory")

    backend = root / "backend"
    alembic_config = _require_file(backend / "alembic.ini", label="Alembic config")
    migrations = _require_directory(
        backend / "app" / "persistence" / "migrations",
        label="migration directory",
    )
    _require_file(migrations / "env.py", label="Alembic environment")
    versions = _require_directory(
        migrations / "versions",
        label="migration revisions directory",
    )
    if not any(path.is_file() and path.suffix == ".py" for path in versions.iterdir()):
        raise PackageInputError("Required migration revisions are missing.")

    return (
        PackageInput(alembic_config, "."),
        PackageInput(migrations, "app/persistence/migrations"),
        PackageInput(frontend, "app/web"),
    )

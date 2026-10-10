import os
import subprocess
from pathlib import Path

import pytest

from app.errors import ErrorCode
from app.services.paths import (
    LibraryPathError,
    LibraryPathErrorReason,
    LibraryPathMapper,
)


def test_maps_relative_path_beneath_root_without_creating_files(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir()
    mapper = LibraryPathMapper(library_root)

    mapped = mapper.resolve("Naruto/日本語 📚.cbz")

    assert mapped == library_root.resolve() / "Naruto" / "日本語 📚.cbz"
    assert mapped.is_absolute()
    assert not mapped.exists()
    assert not mapped.parent.exists()


def test_rejects_unsafe_relative_paths_without_exposing_path_values(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir()
    mapper = LibraryPathMapper(library_root)

    for relative_path in (
        "../outside.cbz",
        r"..\outside.cbz",
        "/outside.cbz",
        "folder//chapter.cbz",
        "folder/CON.cbz",
        "folder/chapter:stream.cbz",
    ):
        with pytest.raises(LibraryPathError) as raised:
            mapper.resolve(relative_path)

        assert raised.value.code is ErrorCode.LIB_PATH_UNAVAILABLE
        assert raised.value.reason is LibraryPathErrorReason.INVALID_RELATIVE_PATH
        assert relative_path not in str(raised.value)
        assert str(library_root) not in str(raised.value)


def test_requires_an_absolute_configured_root() -> None:
    with pytest.raises(ValueError, match="library_root must be an absolute path"):
        LibraryPathMapper(Path("relative-library"))


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_rejects_an_unavailable_or_non_directory_root(
    tmp_path: Path,
    root_kind: str,
) -> None:
    library_root = tmp_path / "library"
    if root_kind == "file":
        library_root.write_text("not a directory", encoding="utf-8")
    mapper = LibraryPathMapper(library_root)

    with pytest.raises(LibraryPathError) as raised:
        mapper.resolve("series/chapter.cbz")

    assert raised.value.reason is LibraryPathErrorReason.ROOT_UNAVAILABLE


def test_allows_a_symlink_that_resolves_inside_the_library_root(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    canonical_directory = library_root / "canonical"
    canonical_directory.mkdir(parents=True)
    alias = library_root / "alias"
    _create_directory_symlink(alias, canonical_directory)
    mapper = LibraryPathMapper(library_root)

    assert mapper.resolve("alias/chapter.cbz") == canonical_directory.resolve() / "chapter.cbz"


def test_rejects_existing_symlink_escape_and_nonexistent_descendant_escape(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    outside_root = tmp_path / "outside"
    library_root.mkdir()
    outside_root.mkdir()
    outside_file = outside_root / "chapter.cbz"
    outside_file.write_bytes(b"preserve this file")
    escape = library_root / "escape"
    _create_directory_symlink(escape, outside_root)
    mapper = LibraryPathMapper(library_root)

    for relative_path in ("escape/chapter.cbz", "escape/new/subdir/chapter.cbz"):
        with pytest.raises(LibraryPathError) as raised:
            mapper.resolve(relative_path)

        assert raised.value.reason is LibraryPathErrorReason.PATH_OUTSIDE_ROOT

    assert outside_file.read_bytes() == b"preserve this file"


def _create_directory_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (NotImplementedError, OSError):
        if os.name == "nt":
            try:
                environment = os.environ.copy()
                environment["YOMIRA_TEST_LINK"] = str(link)
                environment["YOMIRA_TEST_TARGET"] = str(target)
                result = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "$ErrorActionPreference='Stop'; try { New-Item "
                        "-ItemType Junction -Path $env:YOMIRA_TEST_LINK "
                        "-Target $env:YOMIRA_TEST_TARGET | Out-Null } catch { exit 1 }",
                    ],
                    capture_output=True,
                    check=False,
                    env=environment,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as error:
                pytest.skip(f"directory links are unavailable: {type(error).__name__}")
            if result.returncode == 0 and link.exists():
                return

        pytest.skip("directory symlinks and junctions are unavailable")

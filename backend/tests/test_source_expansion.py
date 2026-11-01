"""Keep source identities, network permissions and shipped catalogs consistent."""

import asyncio
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.fmd2_bridge import FMD2_PUBLIC_SOURCES, Fmd2Connector

SUPPORT = Path(__file__).resolve().parents[2] / "third_party/fmd2"


def test_catalog_configuration_contains_only_the_pinned_public_bootstrap_url() -> None:
    configuration = (SUPPORT / "config.json").read_bytes()
    assert json.loads(configuration) == {
        "config": {
            "db_url": "https://raw.githubusercontent.com/dazedcat19/FMD2-DB/master/7z/<website>.7z"
        }
    }
    pin = json.loads((SUPPORT / "upstream.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(configuration).hexdigest() == pin["catalog_configuration_sha256"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows release packaging")
@pytest.mark.parametrize("configuration_state", ["absent", "private", "tampered"])
def test_release_packages_the_public_configuration_from_its_own_build_root(
    tmp_path: Path, configuration_state: str
) -> None:
    repository = tmp_path / "checkout"
    distribution = tmp_path / "distribution"
    output = tmp_path / "release"
    configuration = (SUPPORT / "config.json").read_bytes()
    pin = {
        "reference_executable_sha256": hashlib.sha256(b"motor").hexdigest(),
        "novelcool_source_sha256": hashlib.sha256(b"module").hexdigest(),
        "catalog_configuration_sha256": hashlib.sha256(configuration).hexdigest(),
        "additional_modules_sha256": {"Fixture": hashlib.sha256(b"module").hexdigest()},
        "supporting_templates_sha256": {},
        "public_catalogs_sha256": {},
    }
    files = {
        "scripts/package-release.ps1": (
            SUPPORT.parents[1] / "scripts/package-release.ps1"
        ).read_text(encoding="utf-8"),
        "third_party/fmd2/upstream.json": json.dumps(pin),
        "third_party/fmd2/lua/templates/NovelCool.lua": "module",
        "third_party/fmd2/lua/modules/Fixture.lua": "module",
        "backend/app/version.py": 'APP_VERSION = "0.0.0"',
        "backend/build_windows.ps1": (
            "param([string]$OutputDirectory, [string]$Python)\n"
            "New-Item -ItemType Directory -Path (Join-Path $OutputDirectory 'Yomira') "
            "-Force | Out-Null\n"
        ),
        "installer/build_installer.ps1": (
            "param([string]$BundleDirectory, [string]$ISCCPath, [string]$OutputDirectory)\n"
            "New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null\n"
            "Set-Content -LiteralPath (Join-Path $OutputDirectory 'setup.exe') -Value 'fixture'\n"
        ),
        "scripts/backup-release.ps1": "param([string]$ReleaseOutput, [string]$Version)\n",
        "npm.cmd": "@exit /b 0\n",
        "installer/LEEME.txt": "fixture",
        "installer/Abrir Yomira.cmd": "fixture",
        "installer/Iniciar Yomira.ps1": "fixture",
    }
    for relative, content in files.items():
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    (repository / "third_party/fmd2/config.json").write_bytes(
        configuration + (b"\n" if configuration_state == "tampered" else b"")
    )
    for relative in ("Motor/licenses", "Motor/lua/templates", "Motor/lua/modules", "Licencias"):
        (distribution / relative).mkdir(parents=True, exist_ok=True)
    (distribution / "Motor/fmd.exe").write_bytes(b"motor")
    if configuration_state == "private":
        (distribution / "Motor/config.json").write_text(
            '{"config":{"db_url":"https://private.invalid","token":"synthetic"}}',
            encoding="utf-8",
        )
    powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    assert powershell is not None
    result = subprocess.run(
        [
            powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(repository / "scripts/package-release.ps1"),
            "-OutputDirectory", str(output), "-MotorDistribution", str(distribution),
            "-ISCCPath", "unused", "-NpmCmd", str(repository / "npm.cmd"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if configuration_state == "tampered":
        assert result.returncode != 0
        assert "public catalog configuration" in result.stderr
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert (output / "bundle/Yomira/Motor/config.json").read_bytes() == configuration
        with zipfile.ZipFile(output / "Yomira-Windows-x64-0.0.0.zip") as archive:
            assert archive.read("Motor/config.json") == configuration


def test_native_inventory_and_modules_match_the_core() -> None:
    source = (SUPPORT / "baseunits/uYomiraApi.pas").read_text(encoding="utf-8")
    expected = {
        "IDS": list(FMD2_PUBLIC_SOURCES),
        "NAMES": [item[0] for item in FMD2_PUBLIC_SOURCES.values()],
        "ROOTS": [item[1] for item in FMD2_PUBLIC_SOURCES.values()],
    }
    for field, values in expected.items():
        match = re.search(r"API_SOURCE_" + field + r":.*?= \((.*?)\);", source, re.S)
        assert match is not None
        assert re.findall(r"'([^']+)'", match[1]) == values
    count = re.search(r"API_PUBLIC_SOURCE_COUNT = (\d+);", source)
    assert count is not None and int(count[1]) == len(FMD2_PUBLIC_SOURCES)
    pin = json.loads((SUPPORT / "upstream.json").read_text(encoding="utf-8"))
    assert (
        hashlib.sha256((SUPPORT / "yomira.patch").read_bytes()).hexdigest() == pin["patch_sha256"]
    )
    assert (
        hashlib.sha256((SUPPORT / "baseunits/uYomiraApi.pas").read_bytes()).hexdigest()
        == pin["api_source_sha256"]
    )
    for module, digest in pin["additional_modules_sha256"].items():
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,79}", module)
        assert (
            hashlib.sha256((SUPPORT / f"lua/modules/{module}.lua").read_bytes()).hexdigest()
            == digest
        )
    for template, digest in pin["supporting_templates_sha256"].items():
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,79}", template)
        assert (
            hashlib.sha256((SUPPORT / f"lua/templates/{template}.lua").read_bytes()).hexdigest()
            == digest
        )


@pytest.mark.parametrize(
    "source_id",
    [
        "0611830535274dfa9f0d879dc1968fdb",
        "422d5e83e2ae4b3b84742122b00926ef",
        "36b0f19e144a4642a2839ec9cb728c3a",
    ],
)
def test_seed_catalogs_are_valid_and_contain_only_source_relative_links(source_id: str) -> None:
    pin = json.loads((SUPPORT / "upstream.json").read_text(encoding="utf-8"))
    catalog = SUPPORT / f"catalogs/{source_id}.db"
    assert catalog.stat().st_size < 64 * 1024 * 1024
    assert (
        hashlib.sha256(catalog.read_bytes()).hexdigest() == pin["public_catalogs_sha256"][source_id]
    )
    with sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        records = connection.execute("SELECT link,title FROM masterlist").fetchall()
    assert records
    for link, title in records:
        assert link.startswith("/") and not link.startswith("//")
        assert not any(part in link for part in ("://", "\\", "..", "\x00", "\n", "\r"))
        assert title and len(title) <= 200


@pytest.mark.parametrize(
    ("source_id", "cover"),
    [
        ("abbde9b6468f45939e5603416d73ac47", "https://temp.compsci88.com/cover/fallback/abc.jpg"),
        (
            "889f027efe74448883db0f3fecf3d279",
            "https://cdn.readdetectiveconan.com/file/mangapill/i/123.webp",
        ),
    ],
)
def test_new_cover_hosts_are_scoped_to_their_source(
    source_id: str, cover: str, tmp_path: Path
) -> None:
    connector = Fmd2Connector(tmp_path / "fmd.exe", tmp_path / "staging")
    metadata = connector.normalize_metadata(
        {"source_id": source_id, "title": "Sample", "cover_url": cover}
    )
    assert metadata.cover_url == cover
    other = connector.normalize_metadata(
        {"source_id": "0611830535274dfa9f0d879dc1968fdb", "title": "Sample", "cover_url": cover}
    )
    assert other.cover_url is None
    unsafe = connector.normalize_metadata(
        {
            "source_id": source_id,
            "title": "Sample",
            "cover_url": "https://unexpected.example/cover.jpg",
        }
    )
    assert unsafe.cover_url is None
    asyncio.run(connector.close())

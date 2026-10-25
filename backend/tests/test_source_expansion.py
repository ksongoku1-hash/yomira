"""Keep source identities, network permissions and shipped catalogs consistent."""

import asyncio
import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

from app.fmd2_bridge import FMD2_PUBLIC_SOURCES, Fmd2Connector

SUPPORT = Path(__file__).resolve().parents[2] / "third_party/fmd2"


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

import asyncio
import hashlib
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from sqlalchemy import func, select
from support.sqlite import open_test_database

from app.connectors.http_client import ConnectorHttpSession
from app.connectors.mangadex import (
    MANGADEX_COVER_ONLY_ORIGINS,
    MANGADEX_METADATA,
    MANGADEX_NETWORK_ORIGINS,
    MangaDexConnector,
)
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.domain.metadata import MetadataField
from app.domain.series import Series
from app.persistence.atomic_file_publisher import ADR_0087_RETRY_POLICY, AtomicFilePublisher
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.download_job_archive import DownloadJobArchiveRow
from app.persistence.windows_paths import DataRootIdentity
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.cbz_builder import CBZBuilder
from app.services.cover_assets import CoverAssetService
from app.services.library import LibraryService
from app.services.local_import import LocalImportService
from app.services.metadata import MetadataService
from app.services.reader_asset import ReaderAssetLimits, ReaderAssetService


def _image(color: str) -> bytes:
    with Image.new("RGB", (120, 180), color) as image, BytesIO() as output:
        image.save(output, "PNG")
        return output.getvalue()


def _fixture(tmp_path: Path):
    data = tmp_path / "data"
    database = open_test_database(data)
    MigrationRunner(database, data / "backups").upgrade_to_head()
    root = tmp_path / "library"
    root.mkdir()
    identity = DataRootIdentity.capture(root)
    disk = DiskBudgetManager(database)

    def publisher(root_identity: DataRootIdentity) -> AtomicFilePublisher:
        return AtomicFilePublisher(
            data_root_identity=root_identity,
            retry_policy=ADR_0087_RETRY_POLICY,
            sleep=time.sleep,
            clock=time.monotonic,
            storage_gate=database.storage_gate,
        )

    reader = ReaderAssetService(
        database,
        data_root_identity=database.data_root_identity,
        library_root_identity=identity,
        limits=ReaderAssetLimits(
            max_cbz_bytes=2_147_483_648, max_page_bytes=67_108_864, chunk_bytes=65_536
        ),
    )
    remote_id, cover_id, author_id = str(uuid4()), str(uuid4()), str(uuid4())
    catalog = {
        "id": remote_id,
        "type": "manga",
        "attributes": {
            "title": {"en": "Naruto"},
            "year": 1999,
            "description": {"es": "Sinopsis de Naruto"},
            "tags": [{"attributes": {"name": {"en": "Action"}}}],
            "originalLanguage": "ja",
        },
        "relationships": [
            {"id": author_id, "type": "author", "attributes": {"name": "Masashi Kishimoto"}},
            {"id": cover_id, "type": "cover_art", "attributes": {"fileName": f"{cover_id}.png"}},
        ],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.mangadex.org":
            search = request.url.path == "/manga"
            return httpx.Response(
                200,
                json={
                    "result": "ok",
                    "response": "collection" if search else "entity",
                    "data": [catalog] if search else catalog,
                    "total": 1,
                    "limit": 100,
                    "offset": 0,
                },
            )
        return httpx.Response(200, headers={"content-type": "image/png"}, content=_image("navy"))

    http = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))

    def connector(ctx: ConnectorHostContext) -> MangaDexConnector:
        assert ctx.http_client is not None
        return MangaDexConnector(ctx.http_client)

    registry = ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(data)),
        registrations=(
            ConnectorRegistration(
                MANGADEX_METADATA, connector, MANGADEX_NETWORK_ORIGINS, MANGADEX_COVER_ONLY_ORIGINS
            ),
        ),
        http_client_factory=http.scoped_client,
    )
    covers = CoverAssetService(
        database=database,
        registry=registry,
        http_session=http,
        reader=reader,
        disk_budget=disk,
        publisher=publisher(database.data_root_identity),
        admission_allowed=lambda: True,
    )
    builder = CBZBuilder(
        publisher=publisher(identity),
        max_archive_bytes=2_147_483_648,
        storage_gate=database.storage_gate,
        disk_budget_manager=disk,
    )

    def importer_factory(additional_roots: tuple[Path, ...] = ()) -> LocalImportService:
        return LocalImportService(
            database=database,
            library_root=identity,
            builder=builder,
            covers=covers,
            disk_budget=disk,
            admission_allowed=lambda: True,
            additional_roots=additional_roots,
        )

    return database, root, reader, covers, http, importer_factory


def _comic(root: Path, kind: str, first: bytes, second: bytes) -> Path:
    manga = root / "Naruto (1999)"
    manga.mkdir(exist_ok=True)
    if kind == "images":
        chapter = manga / "Capítulo 001"
        chapter.mkdir()
        (chapter / "2.png").write_bytes(first)
        (chapter / "10.png").write_bytes(second)
        return chapter
    file = manga / f"Capítulo 001.{kind}"
    with zipfile.ZipFile(file, "w") as archive:
        if kind != "epub":
            archive.writestr("10.png", second)
            archive.writestr("2.png", first)
        else:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr(
                "META-INF/container.xml",
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
            )
            archive.writestr(
                "OEBPS/content.opf",
                '<package xmlns="http://www.idpf.org/2007/opf">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<dc:title>Capítulo 001</dc:title><dc:language>und</dc:language></metadata>"
                '<manifest><item id="a" href="second.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="b" href="first.xhtml" media-type="application/xhtml+xml"/></manifest>'
                '<spine><itemref idref="a"/><itemref idref="b"/></spine></package>',
            )
            archive.writestr(
                "OEBPS/second.xhtml",
                '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
                '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                '<body><img src="images/2.png"/></body></html>',
            )
            archive.writestr(
                "OEBPS/first.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                '<body><img src="images/10.png"/></body></html>',
            )
            archive.writestr("OEBPS/images/10.png", second)
            archive.writestr("OEBPS/images/2.png", first)
    return file


def test_folder_status_and_open_reject_a_replaced_library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The shared fixture normally identifies folders by path; this check needs inode identity.
    monkeypatch.setattr(
        "app.persistence.windows_paths._get_file_id_info",
        lambda path: (0x594F4D495241, Path(path).stat().st_ino.to_bytes(16, "little")),
    )
    database, root, _reader, covers, http, importer_factory = _fixture(tmp_path)
    importer = importer_factory()
    opened = []
    monkeypatch.setattr(
        "app.services.local_import.os.startfile",
        lambda path, operation: opened.append((Path(path), operation)),
    )
    try:
        folders = importer.status()["folders"]
        assert [folder["kind"] for folder in folders] == ["library", "inbox", "downloads"]
        assert all(folder["problem"] is None for folder in folders)
        importer.open_folder("inbox")
        assert opened == [(root / "Para añadir", "explore")]
        (root / "Para añadir").rmdir()
        importer.open_folder("inbox")
        assert (root / "Para añadir").is_dir()
        with pytest.raises(KeyError):
            importer.open_folder("../outside")
        moved = tmp_path / "library-moved"
        root.rename(moved)
        root.mkdir()
        assert all(folder["problem"] for folder in importer.status()["folders"])
        with pytest.raises(OSError):
            importer.open_folder("library")
        assert len(opened) == 2
        assert not list(root.iterdir())
    finally:
        asyncio.run(covers.close())
        asyncio.run(http.close())
        database.close()


def test_folder_setup_remains_readable_when_storage_writes_are_blocked(tmp_path: Path) -> None:
    database, root, _reader, covers, http, importer_factory = _fixture(tmp_path)
    try:
        database.storage_gate.close("FILESYSTEM_DISK_FULL")
        importer = importer_factory()
        folders = importer.status()["folders"]
        assert folders[0]["problem"] is None
        assert all(folder["problem"] for folder in folders[1:])
        assert not list(root.iterdir())
    finally:
        asyncio.run(covers.close())
        asyncio.run(http.close())
        database.close()


@pytest.mark.parametrize("kind", ["images", "cbz", "zip", "epub"])
@pytest.mark.parametrize("inbox", [False, True])
def test_import_is_readable_ordered_enriched_and_idempotent(
    tmp_path: Path, kind: str, inbox: bool
) -> None:
    database, root, reader, covers, http, importer_factory = _fixture(tmp_path)
    importer = importer_factory()
    first, second = _image("red"), _image("green")
    original = _comic(root / "Para añadir" if inbox else root, kind, first, second)
    original_hash = (
        hashlib.sha256(original.read_bytes()).hexdigest() if original.is_file() else None
    )

    async def exercise() -> None:
        await importer.scan_once()
        assert LibraryService(database).list_series(limit=100, offset=0).total == 0
        await importer.scan_once()
        page = LibraryService(database).list_series(limit=100, offset=0)
        assert page.total == 1
        series_id = page.items[0].series_id
        resolved = {
            item.field: item.value
            for item in MetadataService(database).resolve_series(series_id) or ()
        }
        assert resolved[MetadataField.AUTHOR] == "Masashi Kishimoto"
        assert resolved[MetadataField.DESCRIPTION] == "Sinopsis de Naruto"
        assert resolved[MetadataField.GENRES] == ("Action",)
        assert resolved[MetadataField.COVER] is not None
        chapters = reader.list_chapters_for_series(series_id).items
        assert len(chapters) == 1 and chapters[0].page_count == 2
        assert chapters[0].display_label == "Capítulo 001"
        assert b"".join(reader.open_page(chapters[0].chapter_id, 1).iter_chunks()) == first
        assert b"".join(reader.open_page(chapters[0].chapter_id, 2).iter_chunks()) == second
        await importer.scan_once()
        # Restarting the scanner also deduplicates by verified content, including a new filename.
        if original.is_file():
            (original.parent / f"Copia.{kind}").write_bytes(original.read_bytes())
        restarted = importer_factory()
        await restarted.scan_once()
        await restarted.scan_once()
        assert len(reader.list_chapters_for_series(series_id).items) == 1
        assert importer.status()["errors"] == []
        assert importer.status()["pending_metadata"] == 0
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 1
            assert session.scalar(select(func.count()).select_from(DownloadJobArchiveRow)) == 1
        assert not list((tmp_path / "data" / "temp" / "local-imports").glob("*"))
        await covers.close()
        await http.close()

    try:
        asyncio.run(exercise())
        if original_hash:
            assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    finally:
        database.close()


@pytest.mark.parametrize("unavailable", [False, True])
def test_metadata_lookup_finishes_or_stops_after_three_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: bool
) -> None:
    database, root, _reader, covers, http, importer_factory = _fixture(tmp_path)
    importer = importer_factory()
    _comic(root, "cbz", _image("red"), _image("green"))
    original_lookup = covers.ensure_cover
    calls = 0

    async def unresolved(_series_id):
        nonlocal calls
        calls += 1
        if unavailable:
            raise OSError("Source unavailable")
        return False

    monkeypatch.setattr(covers, "ensure_cover", unresolved)

    async def exercise() -> None:
        await importer.scan_once()
        await importer.scan_once()
        series_id = LibraryService(database).list_series(limit=1, offset=0).items[0].series_id
        if unavailable:
            assert importer.status()["pending_metadata"] == 1
            await importer._fill_metadata(series_id)
            await importer._fill_metadata(series_id)
        assert calls == (3 if unavailable else 1)
        assert importer.status()["pending_metadata"] == 0
        assert importer.status()["metadata_review"] == 1
        await importer.scan_once()
        assert calls == (3 if unavailable else 1)
        await original_lookup(series_id)
        await importer.scan_once()
        assert importer.status()["metadata_review"] == 0
        await covers.close()
        await http.close()

    try:
        asyncio.run(exercise())
    finally:
        database.close()


def test_truncated_page_reports_its_number_and_preserves_original(tmp_path: Path) -> None:
    database, root, _reader, covers, http, importer_factory = _fixture(tmp_path)
    importer = importer_factory()
    file = root / "Broken.cbz"
    with Image.new("RGB", (120, 180), "red") as image, BytesIO() as output:
        image.save(output, "WEBP")
        broken = output.getvalue()[:30]
    with zipfile.ZipFile(file, "w") as archive:
        archive.writestr("1.png", _image("blue"))
        archive.writestr("2.webp", broken)
    before = file.read_bytes()

    async def exercise() -> None:
        await importer.scan_once()
        await importer.scan_once()
        assert importer.status()["errors"] == [
            {
                "file": "Broken.cbz",
                "message": "No se pudo validar la página 2. "
                "La imagen puede estar incompleta, dañada o superar los límites admitidos. "
                "Vuelve a copiar o descargar el capítulo completo.",
            }
        ]
        assert LibraryService(database).list_series(limit=1, offset=0).total == 0
        assert not list((root / "chapters").rglob("*.cbz"))
        assert file.read_bytes() == before
        await covers.close()
        await http.close()

    try:
        asyncio.run(exercise())
    finally:
        database.close()


@pytest.mark.parametrize("copy_state", ["ready", "missing", "ambiguous"])
def test_rejected_original_links_only_one_available_copy(tmp_path: Path, copy_state: str) -> None:
    database, root, reader, covers, http, importer_factory = _fixture(tmp_path)
    importer = importer_factory()
    _comic(root, "cbz", _image("red"), _image("green"))
    broken = root / "Naruto (1999)" / "Capítulo 001.epub"

    async def exercise():
        await importer.scan_once()
        await importer.scan_once()
        series_id = LibraryService(database).list_series(limit=1, offset=0).items[0].series_id
        chapter_id = reader.list_chapters_for_series(series_id).items[0].chapter_id
        if copy_state == "ambiguous":
            _comic(root, "images", _image("blue"), _image("navy"))
        broken.write_bytes(b"incomplete original")
        await importer.scan_once()
        await importer.scan_once()
        if copy_state == "missing":
            for archive in (root / "chapters").rglob("*.cbz"):
                archive.unlink()
        errors = importer.status()["errors"]
        assert len(errors) == 1
        assert errors[0].get("chapter_id") == (str(chapter_id) if copy_state == "ready" else None)
        assert broken.read_bytes() == b"incomplete original"
        await covers.close()
        await http.close()

    try:
        asyncio.run(exercise())
    finally:
        database.close()


def test_rejected_archive_does_not_hide_valid_comic_and_can_retry(tmp_path: Path) -> None:
    database, root, reader, covers, http, importer_factory = _fixture(tmp_path)
    importer = importer_factory()
    original = _comic(root, "cbz", _image("red"), _image("green"))
    bad = root / "Broken.cbz"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../escape.png", _image("blue"))
    malicious = root / "Entities.zip"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr(
            "ComicInfo.xml",
            '<!DOCTYPE x [<!ENTITY a "expanded">]><ComicInfo><Series>&a;</Series></ComicInfo>',
        )
        archive.writestr("1.png", _image("blue"))

    async def exercise() -> None:
        await importer.scan_once()
        await importer.scan_once()
        assert len(cast(list[object], importer.status()["errors"])) == 2
        assert importer.status()["imported_chapters"] == 1
        assert not (tmp_path / "escape.png").exists()
        bad.write_bytes(original.read_bytes())
        importer.retry_failed()
        await importer.scan_once()
        await importer.scan_once()
        assert len(cast(list[object], importer.status()["errors"])) == 1
        series = LibraryService(database).list_series(limit=100, offset=0).items
        assert len(series) == 2
        for item in series:
            assert reader.list_chapters_for_series(item.series_id).items
        await covers.close()
        await http.close()

    try:
        asyncio.run(exercise())
    finally:
        database.close()


def test_stability_wait_and_explicit_edition_keep_existing_series(tmp_path: Path) -> None:
    database, root, reader, covers, http, importer_factory = _fixture(tmp_path)
    importer = importer_factory()
    library = LibraryService(database)
    expected = Series.new_local("Naruto (1999)", "Naruto (1999)", author="Autor local")
    other = Series.new_local("Naruto (2012)", "Naruto (2012)")
    library.add_series(expected)
    library.add_series(other)
    original = _comic(root, "images", _image("red"), _image("green"))

    async def exercise() -> None:
        await importer.scan_once()
        (original / "10.png").write_bytes(_image("blue"))
        await importer.scan_once()
        assert reader.list_chapters_for_series(expected.series_id).items == ()
        await importer.scan_once()
        chapters = reader.list_chapters_for_series(expected.series_id).items
        assert len(chapters) == 1
        assert reader.list_chapters_for_series(other.series_id).items == ()
        assert library.list_series(limit=100, offset=0).total == 2
        resolved = MetadataService(database).resolve_series(expected.series_id) or ()
        assert (
            next(item for item in resolved if item.field is MetadataField.AUTHOR).value
            == "Autor local"
        )
        assert b"".join(reader.open_page(chapters[0].chapter_id, 2).iter_chunks()) == _image("blue")
        await covers.close()
        await http.close()

    try:
        asyncio.run(exercise())
    finally:
        database.close()


@pytest.mark.parametrize(
    "language,author,joins",
    [
        ("es", "Kishimoto Masashi", True),
        ("en", "Masashi Kishimoto", False),
        ("es", "Another Writer", False),
    ],
)
def test_local_import_respects_translation_and_author(
    tmp_path: Path, language: str, author: str, joins: bool
) -> None:
    database, root, reader, covers, http, importer_factory = _fixture(tmp_path)
    expected = Series.new_local("Naruto", "Naruto", author="Masashi Kishimoto", language="es")
    LibraryService(database).add_series(expected)
    original = _comic(root, "cbz", _image("red"), _image("green"))
    with zipfile.ZipFile(original, "a") as archive:
        archive.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Series>Naruto (1999)</Series><Number>1</Number>"
            f"<Writer>{author}</Writer><LanguageISO>{language}</LanguageISO></ComicInfo>",
        )
    before = original.read_bytes()
    importer = importer_factory()

    async def exercise() -> None:
        await importer.scan_once()
        await importer.scan_once()
        page = LibraryService(database).list_series(limit=100, offset=0)
        assert page.total == (1 if joins else 2)
        target = (
            expected.series_id
            if joins
            else next(item.series_id for item in page.items if item.series_id != expected.series_id)
        )
        chapters = reader.list_chapters_for_series(target).items
        assert len(chapters) == 1
        with database.read_session() as session:
            saved = session.get(ChapterRow, str(chapters[0].chapter_id))
            assert saved is not None and saved.language == language
        assert original.read_bytes() == before
        assert importer.status()["errors"] == []
        await covers.close()
        await http.close()

    try:
        asyncio.run(exercise())
    finally:
        database.close()


def test_original_download_roots_join_existing_series_and_keep_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, root, reader, covers, http, importer_factory = _fixture(tmp_path)
    legacy = tmp_path / "original-downloads"
    legacy.mkdir()
    original = _comic(legacy, "epub", _image("red"), _image("green"))
    original = original.rename(original.with_name("Capítulo 001,364.epub"))
    before = original.read_bytes()
    expected = Series.new_local("Naruto (1999)", "Naruto (1999)", author="Autor local")
    LibraryService(database).add_series(expected)
    importer = importer_factory((legacy, tmp_path / "absent"))

    async def exercise() -> None:
        await importer.scan_once()
        # An unrelated download may be writing while this bounded import finishes.
        with database.storage_gate.mutation():
            await importer.scan_once()
        chapters = reader.list_chapters_for_series(expected.series_id).items
        assert len(chapters) == 1
        assert chapters[0].display_label == "Capítulo 1364"
        assert b"".join(reader.open_page(chapters[0].chapter_id, 1).iter_chunks()) == _image("red")
        assert original.read_bytes() == before
        assert len(list((root / "chapters").rglob("*.cbz"))) == 1
        with monkeypatch.context() as context:

            def unexpected_decode(*args, **kwargs):
                raise AssertionError("Unchanged original must use the verified import record")

            context.setattr("app.services.local_import.validate_image_file", unexpected_decode)
            restarted = importer_factory((legacy,))
            await restarted.scan_once()
            await restarted.scan_once()
            assert restarted.status()["errors"] == []
        copied = root / original.parent.name / original.name
        copied.parent.mkdir()
        copied.write_bytes(before)
        await importer.scan_once()
        await importer.scan_once()
        assert len(reader.list_chapters_for_series(expected.series_id).items) == 1
        assert LibraryService(database).list_series(limit=100, offset=0).total == 1
        assert importer.status()["errors"] == []
        outside = tmp_path / "outside.epub"
        outside.write_bytes(before)
        with pytest.raises(ValueError, match="fuera"):
            importer._fingerprint(outside)
        await covers.close()
        await http.close()

    try:
        asyncio.run(exercise())
    finally:
        database.close()

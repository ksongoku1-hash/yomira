import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from support.sqlite import open_test_database

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.http_client import ConnectorHttpSession
from app.connectors.mangadex import (
    MANGADEX_METADATA,
    MANGADEX_NETWORK_ORIGINS,
    MangaDexCandidate,
    MangaDexConnector,
    MangaDexLookup,
)
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.connectors.search import ConnectorSearchQuery, ConnectorSearchResult
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.chapter import Chapter
from app.domain.chapter_source import ChapterSource
from app.domain.metadata import MetadataField
from app.domain.series import Series
from app.domain.series_source import SeriesSource
from app.domain.source import Source, SourceHealthStatus
from app.errors import ErrorCode
from app.fmd2_bridge import FMD2_METADATA
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.series_source import series_source_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.series_source import SeriesSourceRow
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.external_metadata import (
    ExternalMetadataService,
    MetadataAssociationConflict,
    match_candidate,
)
from app.services.library import LibraryService
from app.services.metadata import MetadataService


def _candidate(
    title: str = "Naruto", year: int | None = 1999, *, description: str | None = None
) -> MangaDexCandidate:
    return MangaDexCandidate(
        remote_id=str(uuid4()),
        title=title,
        aliases=(),
        year=year,
        metadata=ConnectorSeriesMetadata(
            title=title, author="Catalog author", description=description
        ),
    )


@pytest.mark.parametrize(
    "local,remote,year,expected",
    [
        ("Naruto (1999)", "Naruto", 1999, True),
        ("Naruto (1999)", "Naruto", 2012, False),
        ("Naruto (1999)", "Naruto", None, False),
        ("Naruto", "Naruto (Official Colored)", 1999, False),
        (" NARUTO  ", "Naruto", 1999, True),
        ("Ｎａｒｕｔｏ", "Naruto", 1999, True),
        ("Naruto 2", "Naruto", 1999, False),
    ],
)
def test_exact_identity_keeps_editions_and_explicit_year(
    local: str, remote: str, year: int | None, expected: bool
) -> None:
    assert match_candidate(local, _candidate(remote, year)) is expected


def _fixture(tmp_path: Path, lookup: MangaDexLookup | SourceConnectorError):
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    series = Series.new_local("Naruto (1999)", "Naruto (1999)")
    LibraryService(database).add_series(series)
    calls: list[str] = []

    class Catalog(MangaDexConnector):
        async def lookup(self, query: ConnectorSearchQuery) -> MangaDexLookup:
            calls.append(query.query)
            if isinstance(lookup, SourceConnectorError):
                raise lookup
            return lookup

    http = ConnectorHttpSession(test_transport=httpx.MockTransport(lambda _r: httpx.Response(500)))

    def create_catalog(ctx: ConnectorHostContext) -> Catalog:
        assert ctx.http_client is not None
        return Catalog(ctx.http_client)

    registry = ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(tmp_path)),
        registrations=(
            ConnectorRegistration(
                MANGADEX_METADATA,
                create_catalog,
                MANGADEX_NETWORK_ORIGINS,
            ),
        ),
        http_client_factory=http.scoped_client,
    )
    return database, series, ExternalMetadataService(database, registry), http, calls


@pytest.mark.parametrize(
    "case",
    [
        "exact",
        "ambiguous",
        "different_metadata",
        "language",
        "author",
        "dated",
        "disabled",
        "bound_elsewhere",
        "nonlocal",
        "unavailable",
        "existing_source",
    ],
)
def test_local_source_metadata_never_guesses_or_rebinds(tmp_path: Path, case: str) -> None:
    database, series, service, http, _ = _fixture(tmp_path, MangaDexLookup((), 0))
    remote_id = "fmd2:v1:" + (b"K042a85566244b7e836679491ce67ot0\n/manga/test").hex()
    series = replace(
        series,
        canonical_title="Naruto",
        display_title="Naruto",
        language="en" if case == "language" else None,
        author="Local author" if case == "author" else None,
    )
    if case == "dated":
        series = replace(series, canonical_title="Naruto (1999)")
    with database.write_session() as session:
        from app.persistence.repositories.series import SeriesRepository

        SeriesRepository().update(session, series)
        if case != "nonlocal":
            source = Source.new_local(
                "yomira.local.import", "1", "Local", False, SourceHealthStatus.DISABLED
            )
            chapter = Chapter.new_local(series.series_id, "1", "Capítulo 1", chapter_sort_key=(1,))
            session.add(source_to_row(source))
            session.add(chapter_to_row(chapter))
            session.flush()
            session.add(
                chapter_source_to_row(
                    ChapterSource.new_local(
                        chapter.chapter_id, source.source_id, remote_id="local-file", available=True
                    )
                )
            )
        if case in {"disabled", "bound_elsewhere", "existing_source"}:
            source = Source.new_local(
                FMD2_METADATA.connector_id,
                "1.0.0",
                "FMD",
                case != "disabled",
                SourceHealthStatus.DISABLED if case == "disabled" else SourceHealthStatus.HEALTHY,
            )
            session.add(source_to_row(source))
            session.flush()
            if case != "disabled":
                target_id = series.series_id
                if case == "bound_elsewhere":
                    target = Series.new_local("Other edition", "Other edition")
                    from app.persistence.mappers.series import series_to_row

                    session.add(series_to_row(target))
                    session.flush()
                    target_id = target.series_id
                session.add(
                    series_source_to_row(
                        SeriesSource.new_local(target_id, source.source_id, remote_id=remote_id)
                    )
                )
    metadata = ConnectorSeriesMetadata(
        title="Different" if case == "different_metadata" else "Naruto",
        author="Source author",
        description="Source synopsis",
        language="es",
    )
    calls = []

    class SourceConnector:
        async def search(self, query):
            calls.append(query.query)
            if case == "unavailable":
                raise SourceConnectorError(
                    code=ErrorCode.SRC_CONNECTOR_FAILED, operation=SourceConnectorOperation.SEARCH
                )
            matches = [
                ConnectorSearchResult(remote_id, "https://www.manhwaweb.com/manga/test", "Naruto")
            ]
            if case == "ambiguous":
                matches.append(ConnectorSearchResult(remote_id + "00", None, "Naruto"))
            return matches

        async def get_series(self, reference):
            assert reference == remote_id
            return metadata

        def normalize_metadata(self, raw):
            return raw

    service._registry = SimpleNamespace(
        connector_ids=(FMD2_METADATA.connector_id,),
        resolve=lambda *args, **kwargs: SourceConnector(),
    )

    async def exercise():
        saved = await service.ensure_source_missing(series.series_id)
        assert saved is (case == "exact")
        sources = MetadataService(database).list_sources(series.series_id)
        if saved:
            assert len(sources) == 1 and sources[0].remote_id == remote_id
            values = MetadataService(database).resolve_series(series.series_id)
            assert (
                next(v.value for v in values if v.field is MetadataField.DESCRIPTION)
                == "Source synopsis"
            )
            assert not await service.ensure_source_missing(series.series_id)
            assert calls == ["Naruto"]
        else:
            assert len(sources) == (1 if case == "existing_source" else 0)
        await http.close()

    try:
        asyncio.run(exercise())
    finally:
        database.close()


@pytest.mark.parametrize("kind", ["ambiguous", "incomplete", "none", "edition"])
def test_auto_lookup_never_guesses_identity(tmp_path: Path, kind: str) -> None:
    candidates = (
        (_candidate(), _candidate())
        if kind == "ambiguous"
        else (
            _candidate(
                "Naruto (Official Colored)"
                if kind == "edition"
                else "Other"
                if kind == "none"
                else "Naruto"
            ),
        )
    )
    lookup = MangaDexLookup(101 if kind == "incomplete" else len(candidates), candidates)
    database, series, service, http, calls = _fixture(tmp_path, lookup)

    async def exercise() -> None:
        assert await service.ensure_missing(series.series_id) is False
        assert await service.ensure_missing(series.series_id) is False
        await http.close()

    try:
        asyncio.run(exercise())
        assert len(calls) == 1  # bounded negative lookup cooldown
        assert MetadataService(database).list_sources(series.series_id) == ()
    finally:
        database.close()


def test_catalog_is_provenanced_idempotent_and_primary_null_override_win(tmp_path: Path) -> None:
    candidate = _candidate(description="Catalog description")
    database, series, service, http, calls = _fixture(tmp_path, MangaDexLookup(1, (candidate,)))
    primary = Source.new_local(
        "yomira.fmd2.webtoons", "1.0.0", "WEBTOON", True, SourceHealthStatus.HEALTHY
    )
    association = SeriesSource.new_local(
        series.series_id, primary.source_id, remote_id="original", remote_author="Primary author"
    )
    with database.write_session() as session:
        session.add(source_to_row(primary))
        session.flush()
        session.add(series_source_to_row(association))
    metadata = MetadataService(database)
    metadata.set_user_override(series.series_id, MetadataField.DESCRIPTION, None)

    async def exercise() -> None:
        assert await service.ensure_missing(series.series_id)
        assert await service.ensure_missing(series.series_id) is False
        await http.close()

    try:
        asyncio.run(exercise())
        assert calls == ["Naruto"]
        sources = metadata.list_sources(series.series_id)
        assert len(sources) == 2
        catalog = next(
            s for s in sources if s.source_connector_id == MANGADEX_METADATA.connector_id
        )
        assert (
            catalog.remote_id == candidate.remote_id and catalog.remote_url == candidate.remote_url
        )
        assert catalog.source_display_name == "MangaDex (metadatos)"
        resolved = {r.field: r for r in metadata.resolve_series(series.series_id) or ()}
        assert resolved[MetadataField.AUTHOR].value == "Primary author"
        assert resolved[MetadataField.DESCRIPTION].has_user_override
        assert resolved[MetadataField.DESCRIPTION].value is None
    finally:
        database.close()


def test_external_binding_conflict_preserves_both_local_series(tmp_path: Path) -> None:
    candidate = _candidate()
    database, first, service, http, _calls = _fixture(tmp_path, MangaDexLookup(1, (candidate,)))
    second = Series.new_local("Second", "Second")
    LibraryService(database).add_series(second)
    try:
        assert service.save_candidate(first.series_id, candidate)
        assert service.save_candidate(first.series_id, candidate) is False
        with pytest.raises(MetadataAssociationConflict):
            service.save_candidate(second.series_id, candidate)
        with pytest.raises(MetadataAssociationConflict):
            service.save_candidate(first.series_id, replace(candidate, remote_id=str(uuid4())))
        with database.read_session() as session:
            assert len(tuple(session.scalars(select(SeriesSourceRow)))) == 1
        assert LibraryService(database).get_series(second.series_id) == second
    finally:
        asyncio.run(http.close())
        database.close()


def test_disabled_catalog_makes_no_requests(tmp_path: Path) -> None:
    database, series, service, http, calls = _fixture(tmp_path, MangaDexLookup(1, (_candidate(),)))
    disabled = Source.new_local(
        MANGADEX_METADATA.connector_id, "1.0.0", "MangaDex", False, SourceHealthStatus.DISABLED
    )
    with database.write_session() as session:
        session.add(source_to_row(disabled))
    try:
        assert asyncio.run(service.ensure_missing(series.series_id)) is False
        assert calls == []
    finally:
        asyncio.run(http.close())
        database.close()


@pytest.mark.parametrize("kind", ["exact", "alias", "ambiguous", "incomplete", "edition", "failed"])
def test_catalog_cover_preview_is_unique_cached_and_does_not_bind(
    tmp_path: Path, kind: str
) -> None:
    candidate = _candidate("Other" if kind in {"alias", "edition"} else "Naruto")
    url = f"https://uploads.mangadex.org/covers/{candidate.remote_id}/{uuid4()}.jpg"
    candidate = replace(
        candidate,
        aliases=("Naruto",) if kind == "alias" else (),
        metadata=replace(candidate.metadata, cover_url=url),
    )
    candidates = (candidate, _candidate()) if kind == "ambiguous" else (candidate,)
    lookup = (
        SourceConnectorError(
            code=ErrorCode.SRC_CONNECTOR_FAILED,
            operation=SourceConnectorOperation.SEARCH,
            retryable=True,
        )
        if kind == "failed"
        else MangaDexLookup(101 if kind == "incomplete" else len(candidates), candidates)
    )
    database, series, service, http, calls = _fixture(tmp_path, lookup)

    async def exercise() -> None:
        expected = url if kind in {"exact", "alias"} else None
        assert await service.cover_for_title("Naruto (1999)") == expected
        assert await service.cover_for_title(" NARUTO (1999) ") == expected
        assert calls == ["Naruto"]
        assert MetadataService(database).list_sources(series.series_id) == ()
        assert await service.cover_for_title(" ") is None
        assert await service.cover_for_title("x" * 201) is None
        disabled = Source.new_local(
            MANGADEX_METADATA.connector_id, "1.0.0", "MangaDex", False, SourceHealthStatus.DISABLED
        )
        with database.write_session() as session:
            session.add(source_to_row(disabled))
        assert await service.cover_for_title("Naruto (1999)") is None
        assert calls == ["Naruto"]

    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(http.close())
        database.close()

"""Match library entries without confusing catalog language with translation language."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.persistence.models.chapter import ChapterRow
from app.persistence.models.series import SeriesRow
from app.persistence.repositories.metadata import MetadataRepository
from app.services.chapter_fallback import _authors  # pyright: ignore[reportPrivateUsage]
from app.services.external_metadata import (  # pyright: ignore[reportPrivateUsage]
    _query_title,  # pyright: ignore[reportPrivateUsage]
    _title_key,  # pyright: ignore[reportPrivateUsage]
)


def matching_library_series(
    session: Session, *, title: str, author: str | None = None, language: str | None = None
) -> list[SeriesRow]:
    base, year = _query_title(title)
    key = _title_key(base)
    if not key:
        return []
    repository = MetadataRepository()
    matches: list[SeriesRow] = []
    for row in session.scalars(select(SeriesRow)):
        titles = (row.canonical_title, row.display_title)
        if not any(_title_key(_query_title(value)[0]) == key for value in titles):
            continue
        sources = repository.list_series_sources(session, UUID(row.series_id))
        years = {
            value_year
            for value in (*titles, *(s.remote_title for s in sources if s.remote_title))
            if (value_year := _query_title(value)[1]) is not None
        }
        if year is not None and years - {year}:
            continue
        authors = [value for value in (row.author, *(s.remote_author for s in sources)) if value]
        overrides = repository.list_overrides(session, UUID(row.series_id))
        for override in overrides:
            if override.field.value == "author" and isinstance(override.value, str):
                authors = [override.value]
        if author and any(
            not (_authors(author.replace(",", " ")) & _authors(value.replace(",", " ")))
            for value in authors
        ):
            continue
        languages = {
            value
            for value in session.scalars(
                select(ChapterRow.language).where(ChapterRow.series_id == row.series_id).distinct()
            )
            if value and value != "und"
        }
        if row.language and row.language != "und":
            languages.add(row.language)
        # MangaDex describes the original publication, not the language of local pages.
        languages.update(
            s.remote_language
            for s in sources
            if s.source_connector_id != "yomira.metadata.mangadex"
            and s.remote_language
            and s.remote_language != "und"
        )
        if language and language != "und" and languages - {language}:
            continue
        matches.append(row)
    return matches

import json
from typing import cast
from unittest.mock import Mock
from uuid import UUID

import pytest

import app.domain.job_manifest as job_manifest_module
from app.domain.download_page import DownloadPage
from app.domain.job_manifest import (
    CURRENT_MANIFEST_SCHEMA_VERSION,
    MAX_MANIFEST_BYTES,
    MAX_MANIFEST_PAGE_COUNT,
    DownloadJobManifest,
    InvalidManifestError,
    ManifestPageStatus,
    UnsupportedManifestVersion,
    manifest_from_bytes,
    manifest_to_bytes,
)

JOB_ID = UUID("00000000-0000-4000-8000-000000000001")
SOURCE_ID = UUID("00000000-0000-4000-8000-000000000002")
CHAPTER_ID = UUID("00000000-0000-4000-8000-000000000003")
MAX_SQLITE_INTEGER = (1 << 63) - 1
MAX_JSON_DEPTH = 32
MAX_JSON_TOKENS = 430_000


def _page(
    ordinal: int,
    *,
    status: object = ManifestPageStatus.PENDING.value,
    **overrides: object,
) -> DownloadPage:
    """Build a page; casts keep deliberate invalid test overrides at this seam."""
    values: dict[str, object] = {
        "job_id": JOB_ID,
        "ordinal": ordinal,
        "remote_reference": "opaque/page-1?token=kept-out-of-logs",
        "temporary_path": None,
        "status": status,
        "size_bytes": None,
        "sha256": None,
        "width": None,
        "height": None,
        "mime": None,
        "attempts": 0,
    }
    values.update(overrides)
    return DownloadPage(
        job_id=cast(UUID, values["job_id"]),
        ordinal=cast(int, values["ordinal"]),
        remote_reference=cast(str, values["remote_reference"]),
        temporary_path=cast(str | None, values["temporary_path"]),
        status=cast(str, values["status"]),
        size_bytes=cast(int | None, values["size_bytes"]),
        sha256=cast(str | None, values["sha256"]),
        width=cast(int | None, values["width"]),
        height=cast(int | None, values["height"]),
        mime=cast(str | None, values["mime"]),
        attempts=cast(int, values["attempts"]),
    )


def _manifest(*pages: DownloadPage) -> DownloadJobManifest:
    return DownloadJobManifest(
        job_id=JOB_ID,
        source_id=SOURCE_ID,
        chapter_id=CHAPTER_ID,
        expected_pages=len(pages),
        pages=pages,
    )


def _payload(*pages: DownloadPage) -> dict[str, object]:
    return cast(dict[str, object], json.loads(manifest_to_bytes(_manifest(*pages))))


def _page_data(payload: dict[str, object]) -> dict[str, object]:
    pages = payload["pages"]
    if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
        raise AssertionError("test payload must contain one page object")
    return cast(dict[str, object], pages[0])


def _encode_payload(payload: object) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _status_payload(status: str) -> dict[str, object]:
    if status == ManifestPageStatus.DOWNLOADED.value:
        page = _page(
            1,
            status=status,
            temporary_path="job-1/pages/001.jpg.part",
            size_bytes=42,
            sha256="a" * 64,
        )
    elif status == ManifestPageStatus.VALIDATED.value:
        page = _page(
            1,
            status=status,
            temporary_path="job-1/pages/001.jpg",
            size_bytes=42,
            sha256="a" * 64,
            width=8,
            height=12,
            mime="image/jpeg",
        )
    else:
        page = _page(1, status=status)
    return _payload(page)


def test_manifest_round_trip_is_lossless_and_canonical() -> None:
    first = _page(1, remote_reference=" 雪/ページ?key=秘密 ")
    second = _page(
        2,
        remote_reference=first.remote_reference,
        temporary_path="job-1/pages/002.jpg",
        status=ManifestPageStatus.VALIDATED.value,
        size_bytes=42,
        sha256="a" * 64,
        width=8,
        height=12,
        mime="image/jpeg",
        attempts=1,
    )
    manifest = _manifest(first, second)

    serialized = manifest_to_bytes(manifest)

    assert serialized == manifest_to_bytes(manifest)
    assert serialized.startswith(b"{")
    assert not serialized.startswith(b"\xef\xbb\xbf")
    assert b'"size":42' in serialized
    assert b'"size_bytes"' not in serialized
    document = json.loads(serialized)
    assert document["schema_version"] == CURRENT_MANIFEST_SCHEMA_VERSION
    assert document["expected_pages"] == 2
    assert [page["ordinal"] for page in document["pages"]] == [1, 2]
    assert [page["remote_reference"] for page in document["pages"]] == [
        first.remote_reference,
        first.remote_reference,
    ]
    assert manifest_from_bytes(serialized) == manifest


def test_manifest_round_trip_preserves_temporary_path_unicode_codepoints() -> None:
    temporary_path = "jobs/cafe\u0301-雪-😀.jpg"
    manifest = _manifest(_page(1, temporary_path=temporary_path))

    round_tripped = manifest_from_bytes(manifest_to_bytes(manifest))
    retained_path = round_tripped.pages[0].temporary_path

    assert round_tripped == manifest
    assert retained_path is not None
    assert retained_path == temporary_path
    assert "e\u0301" in retained_path
    assert "\u00e9" not in retained_path
    assert "雪" in retained_path
    assert "😀" in retained_path


def test_manifest_serialization_uses_sorted_compact_keys_and_optional_nulls() -> None:
    serialized = manifest_to_bytes(_manifest(_page(1)))
    expected = (
        b'{"chapter_id":"00000000-0000-4000-8000-000000000003",'
        b'"expected_pages":1,"job_id":"00000000-0000-4000-8000-000000000001",'
        b'"pages":[{"attempts":0,"height":null,"mime":null,"ordinal":1,'
        b'"remote_reference":"opaque/page-1?token=kept-out-of-logs",'
        b'"sha256":null,"size":null,"status":"PENDING",'
        b'"temporary_path":null,"width":null}],"schema_version":1,'
        b'"source_id":"00000000-0000-4000-8000-000000000002"}'
    )

    assert serialized == expected
    assert manifest_from_bytes(serialized) == _manifest(_page(1))


def test_manifest_counts_serialized_utf8_bytes_for_escaped_and_unicode_text() -> None:
    remote_reference = 'quote" slash\\ nul\x00 snow雪 face😀'
    manifest = _manifest(_page(1, remote_reference=remote_reference))
    expected = (
        '{"chapter_id":"00000000-0000-4000-8000-000000000003",'
        '"expected_pages":1,"job_id":"00000000-0000-4000-8000-000000000001",'
        '"pages":[{"attempts":0,"height":null,"mime":null,"ordinal":1,'
        '"remote_reference":"quote\\\" slash\\\\ nul\\u0000 snow雪 face😀",'
        '"sha256":null,"size":null,"status":"PENDING",'
        '"temporary_path":null,"width":null}],"schema_version":1,'
        '"source_id":"00000000-0000-4000-8000-000000000002"}'
    )
    expected_bytes = expected.encode("utf-8")
    serialized = manifest_to_bytes(manifest)

    assert serialized == expected_bytes
    assert len(serialized) == len(expected.encode("utf-8"))
    assert len(serialized) != len(expected)


def test_empty_manifest_is_representable_but_has_no_pages() -> None:
    manifest = _manifest()

    assert manifest.expected_pages == 0
    assert manifest.pages == ()
    assert manifest_from_bytes(manifest_to_bytes(manifest)) == manifest


def test_unknown_manifest_version_is_rejected_without_downgrade() -> None:
    payload = json.loads(manifest_to_bytes(_manifest()))
    payload["schema_version"] = 2

    with pytest.raises(UnsupportedManifestVersion) as error:
        manifest_from_bytes(json.dumps(payload).encode("utf-8"))
    assert type(error.value) is UnsupportedManifestVersion


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_id", "ABCDEF00-0000-4000-8000-000000000001"),
        ("source_id", "00000000000040008000000000000002"),
        ("chapter_id", "00000000-0000-1000-8000-000000000003"),
    ],
)
def test_manifest_parser_rejects_noncanonical_or_non_v4_uuids(
    field: str,
    value: str,
) -> None:
    payload = _payload()
    payload[field] = value

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(_encode_payload(payload))
    assert type(error.value) is InvalidManifestError


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "unknown": True},
        {
            "schema_version": 1,
            "job_id": str(JOB_ID),
            "source_id": str(SOURCE_ID),
            "chapter_id": str(CHAPTER_ID),
            "expected_pages": 0,
            "pages": [],
            "future_field": "not supported",
        },
    ],
)
def test_manifest_rejects_unknown_fields(payload: dict[str, object]) -> None:
    with pytest.raises(InvalidManifestError):
        manifest_from_bytes(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize(
    ("operation", "field"),
    [("add", "future_page_field"), ("delete", "mime")],
)
def test_manifest_rejects_unknown_or_missing_page_fields(
    operation: str,
    field: str,
) -> None:
    payload = _payload(_page(1))
    if operation == "add":
        _page_data(payload)[field] = "unsupported"
    else:
        del _page_data(payload)[field]

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(_encode_payload(payload))
    assert type(error.value) is InvalidManifestError


def test_manifest_parser_rejects_utf8_bom() -> None:
    serialized = manifest_to_bytes(_manifest())

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(b"\xef\xbb\xbf" + serialized)
    assert type(error.value) is InvalidManifestError


@pytest.mark.parametrize(
    "serialized",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":1,"job_id":"duplicate","job_id":"again"}',
        b'{"schema_version":NaN}',
        b"\xff",
    ],
)
def test_manifest_rejects_duplicate_keys_nonfinite_and_invalid_utf8(
    serialized: bytes,
) -> None:
    with pytest.raises(InvalidManifestError):
        manifest_from_bytes(serialized)


def test_manifest_rejects_duplicate_page_keys_and_unpaired_unicode() -> None:
    serialized = manifest_to_bytes(_manifest(_page(1)))
    duplicate_ordinal = serialized.replace(
        b'"ordinal":1,',
        b'"ordinal":1,"ordinal":1,',
        1,
    )
    assert duplicate_ordinal.count(b'"ordinal":1') == 2
    with pytest.raises(InvalidManifestError) as duplicate_error:
        manifest_from_bytes(duplicate_ordinal)
    assert type(duplicate_error.value) is InvalidManifestError

    payload = _payload(_page(1))
    _page_data(payload)["remote_reference"] = "\ud800"
    with pytest.raises(InvalidManifestError) as unicode_error:
        manifest_from_bytes(_encode_payload(payload))
    assert type(unicode_error.value) is InvalidManifestError


def test_manifest_parser_accepts_exact_byte_limit_and_serializer_checks_exact_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = manifest_to_bytes(_manifest(_page(1, remote_reference="x")))
    exact_reference = "x" * (1 + MAX_MANIFEST_BYTES - len(baseline))
    exact_manifest = _manifest(_page(1, remote_reference=exact_reference))
    exact_bytes = manifest_to_bytes(exact_manifest)

    assert len(exact_bytes) == MAX_MANIFEST_BYTES
    assert manifest_from_bytes(exact_bytes) == exact_manifest
    del exact_bytes, exact_manifest, exact_reference

    oversized_manifest = _manifest(
        _page(
            1,
            remote_reference="x" * (2 + MAX_MANIFEST_BYTES - len(baseline)),
        ),
    )
    dumps_spy = Mock(wraps=json.dumps)
    monkeypatch.setattr(job_manifest_module.json, "dumps", dumps_spy)

    with pytest.raises(InvalidManifestError) as serializer_error:
        manifest_to_bytes(oversized_manifest)
    assert type(serializer_error.value) is InvalidManifestError
    dumps_spy.assert_not_called()


def test_manifest_rejects_oversize_before_json_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    valid = manifest_to_bytes(_manifest())
    oversized = valid + b" " * (MAX_MANIFEST_BYTES + 1 - len(valid))
    loads_spy = Mock(wraps=json.loads)
    monkeypatch.setattr(job_manifest_module.json, "loads", loads_spy)

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(oversized)

    assert type(error.value) is InvalidManifestError
    loads_spy.assert_not_called()


def test_manifest_rejects_page_limit_and_count_mismatch() -> None:
    with pytest.raises(InvalidManifestError):
        DownloadJobManifest(
            job_id=JOB_ID,
            source_id=SOURCE_ID,
            chapter_id=CHAPTER_ID,
            expected_pages=MAX_MANIFEST_PAGE_COUNT + 1,
            pages=(),
        )

    payload = json.loads(manifest_to_bytes(_manifest()))
    payload["expected_pages"] = 1
    with pytest.raises(InvalidManifestError):
        manifest_from_bytes(json.dumps(payload).encode("utf-8"))


def test_manifest_accepts_exact_page_count_limit() -> None:
    pages = tuple(
        _page(ordinal)
        for ordinal in range(1, MAX_MANIFEST_PAGE_COUNT + 1)
    )
    manifest = _manifest(*pages)
    serialized = manifest_to_bytes(manifest)

    assert manifest.expected_pages == MAX_MANIFEST_PAGE_COUNT
    assert manifest_from_bytes(serialized) == manifest


def test_manifest_parser_rejects_10001_pages() -> None:
    payload = _payload()
    page_template = _page_data(_payload(_page(1)))
    payload["expected_pages"] = MAX_MANIFEST_PAGE_COUNT
    payload["pages"] = [
        {**page_template, "ordinal": ordinal}
        for ordinal in range(1, MAX_MANIFEST_PAGE_COUNT + 2)
    ]

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(_encode_payload(payload))
    assert type(error.value) is InvalidManifestError


def test_manifest_rejects_noncontiguous_or_reordered_ordinals() -> None:
    with pytest.raises(InvalidManifestError):
        _manifest(_page(2))
    with pytest.raises(InvalidManifestError):
        _manifest(_page(1), _page(3))


@pytest.mark.parametrize("status", ["UNKNOWN", "pending", ""])
def test_manifest_rejects_unknown_page_status(status: str) -> None:
    payload = _payload(_page(1))
    _page_data(payload)["status"] = status

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(_encode_payload(payload))
    assert type(error.value) is InvalidManifestError


@pytest.mark.parametrize(
    "status",
    [status.value for status in ManifestPageStatus],
)
def test_manifest_parser_accepts_every_v1_status(status: str) -> None:
    payload = _status_payload(status)

    assert manifest_from_bytes(_encode_payload(payload)).pages[0].status == status


@pytest.mark.parametrize(
    ("status", "field", "value"),
    [
        ("DOWNLOADED", "temporary_path", None),
        ("DOWNLOADED", "temporary_path", "job-1/pages/001.jpg"),
        ("DOWNLOADED", "size", None),
        ("DOWNLOADED", "sha256", None),
        ("VALIDATED", "temporary_path", None),
        ("VALIDATED", "temporary_path", "job-1/pages/001.jpg.part"),
        ("VALIDATED", "size", None),
        ("VALIDATED", "sha256", None),
        ("VALIDATED", "width", None),
        ("VALIDATED", "mime", None),
    ],
)
def test_manifest_rejects_status_without_required_observations(
    status: str,
    field: str,
    value: object,
) -> None:
    payload = _status_payload(status)
    page = _page_data(payload)
    page[field] = value
    if status == ManifestPageStatus.VALIDATED.value and field == "width":
        page["height"] = None

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(_encode_payload(payload))
    assert type(error.value) is InvalidManifestError


@pytest.mark.parametrize(
    ("status", "overrides"),
    [
        (
            ManifestPageStatus.DOWNLOADED.value,
            {"temporary_path": "job-1/pages/001.jpg.part", "size_bytes": 42},
        ),
        (
            ManifestPageStatus.VALIDATED.value,
            {
                "temporary_path": "job-1/pages/001.jpg",
                "size_bytes": 42,
                "sha256": "a" * 64,
                "width": 8,
                "height": 12,
                "mime": None,
            },
        ),
    ],
)
def test_manifest_model_rejects_status_without_required_observations(
    status: str,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(InvalidManifestError) as error:
        _manifest(_page(1, status=status, **overrides))
    assert type(error.value) is InvalidManifestError


@pytest.mark.parametrize(
    "overrides",
    [
        {"temporary_path": "../outside.jpg"},
        {"sha256": "A" * 64},
        {"width": 10},
        {"attempts": True},
    ],
)
def test_manifest_reuses_page_path_and_metadata_invariants(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _manifest(_page(1, **overrides))


def test_manifest_parser_requires_exact_integer_types() -> None:
    payload = _payload(_page(1))
    payload["schema_version"] = True
    with pytest.raises(InvalidManifestError):
        manifest_from_bytes(_encode_payload(payload))

    payload = _payload(_page(1))
    _page_data(payload)["attempts"] = True
    with pytest.raises(InvalidManifestError):
        manifest_from_bytes(_encode_payload(payload))


@pytest.mark.parametrize(
    ("location", "field", "value"),
    [
        ("root", "schema_version", 1.0),
        ("root", "expected_pages", True),
        ("root", "expected_pages", 1.0),
        ("page", "ordinal", True),
        ("page", "ordinal", 1.0),
        ("page", "size", True),
        ("page", "size", 1.0),
        ("page", "width", True),
        ("page", "width", 1.0),
        ("page", "height", True),
        ("page", "height", 1.0),
        ("page", "attempts", 1.0),
    ],
)
def test_manifest_parser_rejects_non_exact_integer_types(
    location: str,
    field: str,
    value: object,
) -> None:
    payload = _payload(_page(1))
    target = payload if location == "root" else _page_data(payload)
    target[field] = value
    if location == "page" and field == "width":
        target["height"] = 1
    if location == "page" and field == "height":
        target["width"] = 1

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(_encode_payload(payload))
    assert type(error.value) is InvalidManifestError


@pytest.mark.parametrize(
    ("field", "width", "height"),
    [
        ("size", None, None),
        ("width", MAX_SQLITE_INTEGER + 1, 1),
        ("height", 1, MAX_SQLITE_INTEGER + 1),
        ("attempts", None, None),
    ],
)
def test_manifest_parser_rejects_sqlite_integer_overflow(
    field: str,
    width: int | None,
    height: int | None,
) -> None:
    base = (
        _status_payload(ManifestPageStatus.VALIDATED.value)
        if field in {"width", "height"}
        else _status_payload(ManifestPageStatus.DOWNLOADED.value)
    )
    payload = base
    page = _page_data(payload)
    if field == "size":
        page["size"] = MAX_SQLITE_INTEGER + 1
    elif field == "attempts":
        page["attempts"] = MAX_SQLITE_INTEGER + 1
    else:
        page["width"] = width
        page["height"] = height

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(_encode_payload(payload))
    assert type(error.value) is InvalidManifestError


@pytest.mark.parametrize(
    ("status", "overrides"),
    [
        (
            ManifestPageStatus.DOWNLOADED.value,
            {
                "temporary_path": "job-1/pages/001.jpg.part",
                "size_bytes": MAX_SQLITE_INTEGER + 1,
                "sha256": "a" * 64,
            },
        ),
        (
            ManifestPageStatus.VALIDATED.value,
            {
                "temporary_path": "job-1/pages/001.jpg",
                "size_bytes": 42,
                "sha256": "a" * 64,
                "width": MAX_SQLITE_INTEGER + 1,
                "height": 1,
                "mime": "image/jpeg",
            },
        ),
        (
            ManifestPageStatus.VALIDATED.value,
            {
                "temporary_path": "job-1/pages/001.jpg",
                "size_bytes": 42,
                "sha256": "a" * 64,
                "width": 1,
                "height": MAX_SQLITE_INTEGER + 1,
                "mime": "image/jpeg",
            },
        ),
        (
            ManifestPageStatus.DOWNLOADED.value,
            {
                "temporary_path": "job-1/pages/001.jpg.part",
                "size_bytes": 42,
                "sha256": "a" * 64,
                "attempts": MAX_SQLITE_INTEGER + 1,
            },
        ),
    ],
)
def test_manifest_model_rejects_sqlite_integer_overflow(
    status: str,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(InvalidManifestError) as error:
        _manifest(_page(1, status=status, **overrides))
    assert type(error.value) is InvalidManifestError


def test_manifest_accepts_maximum_sqlite_integer_values() -> None:
    manifest = _manifest(
        _page(
            1,
            status=ManifestPageStatus.VALIDATED.value,
            temporary_path="job-1/pages/001.jpg",
            size_bytes=MAX_SQLITE_INTEGER,
            sha256="a" * 64,
            width=MAX_SQLITE_INTEGER,
            height=MAX_SQLITE_INTEGER,
            mime="image/jpeg",
            attempts=MAX_SQLITE_INTEGER,
        ),
    )

    assert manifest_from_bytes(manifest_to_bytes(manifest)) == manifest


@pytest.mark.parametrize(
    "path",
    ["../outside.jpg", "/rooted.jpg", "C:/outside.jpg", "folder\\outside.jpg"],
)
def test_manifest_parser_normalizes_invalid_paths(path: str) -> None:
    payload = _payload(_page(1))
    _page_data(payload)["temporary_path"] = path

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(_encode_payload(payload))
    assert type(error.value) is InvalidManifestError


def test_manifest_parser_rejects_duplicate_temporary_paths() -> None:
    payload = _payload(
        _page(1, temporary_path="job-1/pages/001.jpg"),
        _page(2, temporary_path="job-1/pages/002.jpg"),
    )
    pages = cast(list[object], payload["pages"])
    first_page = cast(dict[str, object], pages[0])
    second_page = cast(dict[str, object], pages[1])
    second_page["temporary_path"] = first_page["temporary_path"]

    with pytest.raises(InvalidManifestError):
        manifest_from_bytes(_encode_payload(payload))


@pytest.mark.parametrize(
    ("first_path", "second_path"),
    [
        pytest.param("jobs/A.jpg", "jobs/a.jpg", id="ascii-casefold"),
        pytest.param("jobs/Straße.jpg", "jobs/STRASSE.jpg", id="unicode-casefold"),
    ],
)
def test_manifest_model_rejects_casefold_colliding_temporary_paths(
    first_path: str,
    second_path: str,
) -> None:
    with pytest.raises(InvalidManifestError):
        _manifest(
            _page(1, temporary_path=first_path),
            _page(2, temporary_path=second_path),
        )


@pytest.mark.parametrize(
    ("first_path", "second_path"),
    [
        pytest.param("jobs/A.jpg", "jobs/a.jpg", id="ascii-casefold"),
        pytest.param("jobs/Straße.jpg", "jobs/STRASSE.jpg", id="unicode-casefold"),
    ],
)
def test_manifest_parser_rejects_casefold_colliding_temporary_paths(
    first_path: str,
    second_path: str,
) -> None:
    payload = _payload(
        _page(1, temporary_path="jobs/original-one.jpg"),
        _page(2, temporary_path="jobs/original-two.jpg"),
    )
    pages = cast(list[object], payload["pages"])
    first_page = cast(dict[str, object], pages[0])
    second_page = cast(dict[str, object], pages[1])
    first_page["temporary_path"] = first_path
    second_page["temporary_path"] = second_path

    with pytest.raises(InvalidManifestError):
        manifest_from_bytes(_encode_payload(payload))


@pytest.mark.parametrize(
    "reserved_name",
    ["COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"],
)
def test_download_page_rejects_superscript_windows_device_names(
    reserved_name: str,
) -> None:
    with pytest.raises(ValueError):
        DownloadPage.new_local(
            JOB_ID,
            1,
            "opaque/page-1",
            temporary_path=f"job-1/pages/{reserved_name}.jpg",
        )


@pytest.mark.parametrize(
    "reserved_name",
    ["COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"],
)
def test_manifest_parser_rejects_superscript_windows_device_names(
    reserved_name: str,
) -> None:
    payload = _payload(_page(1))
    _page_data(payload)["temporary_path"] = f"job-1/pages/{reserved_name}.jpg"

    with pytest.raises(InvalidManifestError):
        manifest_from_bytes(_encode_payload(payload))


@pytest.mark.parametrize(
    ("component", "expected_units"),
    [
        pytest.param("a" * 255, 255, id="bmp-255-units"),
        pytest.param("😀" * 127 + "a", 255, id="astral-255-units"),
    ],
)
def test_download_page_accepts_component_at_255_utf16_units(
    component: str,
    expected_units: int,
) -> None:
    assert len(component.encode("utf-16-le")) // 2 == expected_units

    page = DownloadPage.new_local(
        JOB_ID,
        1,
        "opaque/page-1",
        temporary_path=f"job-1/pages/{component}",
    )

    assert page.temporary_path == f"job-1/pages/{component}"


@pytest.mark.parametrize(
    "component",
    [pytest.param("a" * 256, id="bmp-256-units"), pytest.param("😀" * 128, id="astral-256-units")],
)
def test_download_page_rejects_component_at_256_utf16_units(component: str) -> None:
    assert len(component.encode("utf-16-le")) // 2 == 256

    with pytest.raises(ValueError):
        DownloadPage.new_local(
            JOB_ID,
            1,
            "opaque/page-1",
            temporary_path=f"job-1/pages/{component}",
        )


@pytest.mark.parametrize(
    ("component", "expected_units"),
    [
        pytest.param("a" * 255, 255, id="bmp-255-units"),
        pytest.param("😀" * 127 + "a", 255, id="astral-255-units"),
    ],
)
def test_manifest_parser_accepts_component_at_255_utf16_units(
    component: str,
    expected_units: int,
) -> None:
    assert len(component.encode("utf-16-le")) // 2 == expected_units
    payload = _payload(_page(1))
    expected_path = f"job-1/pages/{component}"
    _page_data(payload)["temporary_path"] = expected_path

    manifest = manifest_from_bytes(_encode_payload(payload))

    assert manifest.pages[0].temporary_path == expected_path


@pytest.mark.parametrize(
    "component",
    [pytest.param("a" * 256, id="bmp-256-units"), pytest.param("😀" * 128, id="astral-256-units")],
)
def test_manifest_parser_rejects_component_at_256_utf16_units(component: str) -> None:
    assert len(component.encode("utf-16-le")) // 2 == 256
    payload = _payload(_page(1))
    _page_data(payload)["temporary_path"] = f"job-1/pages/{component}"

    with pytest.raises(InvalidManifestError):
        manifest_from_bytes(_encode_payload(payload))


def test_manifest_rejects_over_depth_json_before_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized = b"[" * (MAX_JSON_DEPTH + 1) + b"0" + b"]" * (MAX_JSON_DEPTH + 1)
    loads_spy = Mock(wraps=json.loads)
    monkeypatch.setattr(job_manifest_module.json, "loads", loads_spy)

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(serialized)

    assert type(error.value) is InvalidManifestError
    loads_spy.assert_not_called()


def test_manifest_allows_depth_limit_to_reach_json_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized = b"[" * MAX_JSON_DEPTH + b"0" + b"]" * MAX_JSON_DEPTH
    loads_spy = Mock(wraps=json.loads)
    monkeypatch.setattr(job_manifest_module.json, "loads", loads_spy)

    with pytest.raises(InvalidManifestError):
        manifest_from_bytes(serialized)

    loads_spy.assert_called_once()


def test_manifest_rejects_over_token_json_before_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized = b"[" + b"0," * MAX_JSON_TOKENS + b"0]"
    assert len(serialized) < MAX_MANIFEST_BYTES
    loads_spy = Mock(wraps=json.loads)
    monkeypatch.setattr(job_manifest_module.json, "loads", loads_spy)

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(serialized)

    assert type(error.value) is InvalidManifestError
    loads_spy.assert_not_called()


def test_manifest_allows_20_character_number_to_reach_json_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized = b'{"schema_version":' + b"1" * 20 + b"}"
    loads_spy = Mock(wraps=json.loads)
    monkeypatch.setattr(job_manifest_module.json, "loads", loads_spy)

    with pytest.raises(UnsupportedManifestVersion) as error:
        manifest_from_bytes(serialized)

    assert type(error.value) is UnsupportedManifestVersion
    loads_spy.assert_called_once()


def test_manifest_rejects_21_character_number_before_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized = b'{"schema_version":' + b"1" * 21 + b"}"
    loads_spy = Mock(wraps=json.loads)
    monkeypatch.setattr(job_manifest_module.json, "loads", loads_spy)

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(serialized)

    assert type(error.value) is InvalidManifestError
    loads_spy.assert_not_called()


def test_manifest_rejects_5000_digit_integer_before_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = (
        f'{{"schema_version":1,"job_id":"{JOB_ID}",'
        f'"source_id":"{SOURCE_ID}","chapter_id":"{CHAPTER_ID}",'
        '"expected_pages":'
    )
    serialized = (prefix + "1" * 5_000 + ',"pages":[]}').encode("ascii")
    loads_spy = Mock(wraps=json.loads)
    monkeypatch.setattr(job_manifest_module.json, "loads", loads_spy)

    with pytest.raises(InvalidManifestError) as error:
        manifest_from_bytes(serialized)

    assert type(error.value) is InvalidManifestError
    loads_spy.assert_not_called()

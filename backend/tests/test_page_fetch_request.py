from dataclasses import FrozenInstanceError, fields

import pytest

from app.connectors.pages import PageFetchRequest
from app.secrets.reference import SecretReference


def test_page_fetch_request_accepts_absolute_http_url_and_optional_referer() -> None:
    request = PageFetchRequest(
        url="https://cdn.example.invalid/pages/001.jpg?signature=temporary",
        referer="https://reader.example.invalid/chapter/001",
    )

    assert request.url == "https://cdn.example.invalid/pages/001.jpg?signature=temporary"
    assert request.referer == "https://reader.example.invalid/chapter/001"
    assert request.secret_reference is None


def test_page_fetch_request_accepts_opaque_vault_reference_without_secret_bytes() -> None:
    reference = SecretReference.generate()

    request = PageFetchRequest(
        url="http://127.0.0.1:43127/pages/001.jpg",
        secret_reference=reference,
    )

    assert request.secret_reference is reference


@pytest.mark.parametrize(
    "url",
    [
        "pages/001.jpg",
        "//cdn.example.invalid/pages/001.jpg",
        "file:///C:/pages/001.jpg",
        "ftp://cdn.example.invalid/pages/001.jpg",
        "https:///pages/001.jpg",
        "https://",
        "https://cdn.example.invalid:bad/page.jpg",
        "https://cdn.example.invalid:/page.jpg",
        "https://[not-ipv6]/page.jpg",
        "https://user@cdn.example.invalid/page.jpg",
        "https://:password@cdn.example.invalid/page.jpg",
        "https://cdn.example.invalid/page.jpg#fragment",
        "https://cdn.example.invalid/page.jpg#",
        "https://cdn.example.invalid /page.jpg",
        "https://cdn.example.invalid/page\t.jpg",
        "https://cdn.example.invalid/page\n.jpg",
        "https://cdn.example.invalid/page\\.jpg",
        "https://cdn.example.invalid/%zz.jpg",
    ],
)
def test_page_fetch_request_rejects_unsafe_or_invalid_urls(url: str) -> None:
    with pytest.raises(ValueError):
        PageFetchRequest(url=url)


@pytest.mark.parametrize(
    "referer",
    [
        "chapter/001",
        "file:///C:/reader.html",
        "https://user:password@reader.example.invalid/chapter/001",
        "https://reader.example.invalid/chapter/001#fragment",
        "https://reader.example.invalid /chapter/001",
    ],
)
def test_page_fetch_request_rejects_invalid_referers(referer: str) -> None:
    with pytest.raises(ValueError):
        PageFetchRequest(url="https://cdn.example.invalid/page.jpg", referer=referer)


@pytest.mark.parametrize("value", [None, 7, b"https://cdn.example.invalid/page.jpg"])
def test_page_fetch_request_requires_string_url(value: object) -> None:
    with pytest.raises(TypeError):
        PageFetchRequest(url=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [7, b"https://reader.example.invalid/"])
def test_page_fetch_request_referer_is_optional_string(value: object) -> None:
    with pytest.raises(TypeError):
        PageFetchRequest(url="https://cdn.example.invalid/page.jpg", referer=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["opaque-secret-text", b"secret-bytes", object()])
def test_page_fetch_request_rejects_plaintext_or_untyped_secret_values(value: object) -> None:
    with pytest.raises(TypeError):
        PageFetchRequest(url="https://cdn.example.invalid/page.jpg", secret_reference=value)  # type: ignore[arg-type]


def test_page_fetch_request_is_immutable_and_redacts_url_query_in_repr() -> None:
    request = PageFetchRequest(
        url="https://cdn.example.invalid/page.jpg?token=do-not-print",
        referer="https://reader.example.invalid/chapter/001?session=do-not-print-either",
    )

    with pytest.raises(FrozenInstanceError):
        request.url = "https://other.example.invalid/page.jpg"  # type: ignore[misc]

    assert "do-not-print" not in repr(request)
    assert "token=" not in repr(request)
    assert "session=" not in repr(request)


def test_page_fetch_request_has_no_header_cookie_password_or_token_fields() -> None:
    field_names = {item.name for item in fields(PageFetchRequest)}

    assert field_names == {"url", "referer", "secret_reference"}

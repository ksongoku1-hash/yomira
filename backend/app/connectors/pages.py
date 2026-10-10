"""Opaque remote page reference contract for source connectors."""

from dataclasses import dataclass, field
from ipaddress import AddressValueError, IPv6Address
from urllib.parse import urlsplit

from app.secrets.reference import SecretReference


@dataclass(frozen=True, slots=True)
class ConnectorPageReference:
    """Remote identifier or URL-like value used to retrieve one page."""

    remote_reference: str

    def __post_init__(self) -> None:
        if type(self.remote_reference) is not str or not self.remote_reference:
            raise ValueError("remote_reference must not be empty")


def _validate_http_url(value: str, *, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if not value:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    if any(
        character.isspace()
        or ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise ValueError(f"{field_name} must not contain whitespace or control characters")
    if "\\" in value:
        raise ValueError(f"{field_name} must not contain backslashes")
    if "#" in value:
        raise ValueError(f"{field_name} must not contain a fragment")

    index = 0
    while index < len(value):
        if value[index] == "%":
            if index + 2 >= len(value) or any(
                character not in "0123456789abcdefABCDEF"
                for character in value[index + 1 : index + 3]
            ):
                raise ValueError(f"{field_name} contains an invalid percent escape")
            index += 3
        else:
            index += 1

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError(f"{field_name} must be a valid absolute HTTP(S) URL") from None

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not contain embedded credentials")
    if parsed.netloc.endswith(":"):
        raise ValueError(f"{field_name} must not contain an empty port")
    if port is not None and not 0 <= port <= 65535:
        raise ValueError(f"{field_name} contains an invalid port")
    if "%" in hostname:
        raise ValueError(f"{field_name} must not contain percent-encoded host data")
    if "[" in parsed.netloc:
        try:
            IPv6Address(hostname)
        except AddressValueError:
            raise ValueError(f"{field_name} must contain a valid IPv6 address") from None


@dataclass(frozen=True, slots=True)
class PageFetchRequest:
    """Transient page request; never log or persist its URL or query string."""

    url: str = field(repr=False)
    referer: str | None = field(default=None, repr=False)
    secret_reference: SecretReference | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_http_url(self.url, field_name="url")
        if self.referer is not None:
            _validate_http_url(self.referer, field_name="referer")
        if self.secret_reference is not None and type(self.secret_reference) is not SecretReference:
            raise TypeError("secret_reference must be a SecretReference")

    def __repr__(self) -> str:
        referer_state = "present" if self.referer is not None else "absent"
        secret_state = "present" if self.secret_reference is not None else "absent"
        return (
            "PageFetchRequest(url=<redacted>, "
            f"referer=<{referer_state}>, secret_reference=<{secret_state}>)"
        )

import hashlib
import ipaddress
import re
import secrets
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pricehunter.domain.errors import InvalidProductUrlError


def validate_url(url: str, allowed_domains: set[str]) -> SplitResult:
    if len(url) > 2048 or re.search(r"[\s\\\x00-\x1f\x7f]", url):
        raise InvalidProductUrlError()
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError
        if parsed.port not in (None, 443) or not host or host.endswith("."):
            raise ValueError
        if host == "localhost" or host.endswith((".local", ".localhost", ".internal")):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise InvalidProductUrlError()  # Literal IPs are never retailer URLs.
        if host not in allowed_domains:
            raise ValueError
        return parsed
    except (ValueError, UnicodeError) as exc:
        raise InvalidProductUrlError() from exc


def canonical_url(url: str, allowed_domains: set[str]) -> str:
    parts = validate_url(url, allowed_domains)
    return urlunsplit(("https", parts.hostname or "", parts.path, "", ""))


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_api_key() -> str:
    return "ph_" + secrets.token_urlsafe(32)

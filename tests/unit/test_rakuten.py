import asyncio
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.core.xml import parse_xml
from pricehunter.domain.discovery import Capability, DiscoveryQuery
from pricehunter.domain.errors import (
    ProviderUnavailableError,
    RateLimitExceededError,
    UnsupportedStoreError,
)
from pricehunter.providers.affiliate.rakuten import RakutenProvider, RakutenTokenManager
from pricehunter.providers.http import ProviderHTTP

XML = (Path(__file__).parents[1] / "fixtures/rakuten_search.xml").read_bytes()


def adapter(client, redis, **options):
    http = ProviderHTTP(client, timeout=2, max_bytes=20000)
    limiter = RateLimiter(redis, Settings(_env_file=None, provider_rate_limits={"rakuten": 20}))
    tokens = RakutenTokenManager(http, "fixture-client", "fixture-secret", "123")
    return RakutenProvider(http, tokens, limiter, {"BE": ["12345"]}, **options)


async def test_token_concurrent_refresh_expiry_and_rejected_token(redis):
    requests = []

    async def handler(request):
        requests.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(
            200, json={"access_token": f"token-{len(requests)}", "expires_in": 3600}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = adapter(client, redis)
        tokens = provider.tokens
        assert await asyncio.gather(*(tokens.token() for _ in range(12))) == ["token-1"] * 12
        assert requests[0].url == "https://api.linksynergy.com/token"
        assert requests[0].headers["Authorization"].startswith("Bearer ")
        assert requests[0].content == b"scope=123"
        assert (
            await asyncio.gather(*(tokens.token(rejected="token-1") for _ in range(12)))
            == ["token-2"] * 12
        )
        assert len(requests) == 2
        tokens._expires_at = 0
        assert await tokens.token() == "token-3"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"access_token": "", "expires_in": 3600},
        {"access_token": "x", "expires_in": 0},
        {"access_token": "x", "expires_in": "bad"},
    ],
)
async def test_invalid_token_response(payload, redis):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ProviderUnavailableError):
            await adapter(client, redis).tokens.token()


async def test_search_pagination_and_normalization(redis):
    calls = []

    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        calls.append(request)
        page = request.url.params["pagenumber"]
        body = (
            XML
            if page == "1"
            else XML.replace(b"<PageNumber>1", b"<PageNumber>2").replace(
                b"merchant-sku-1", b"merchant-sku-2"
            )
        )
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = adapter(client, redis)
        rows = await provider.search("cotton shirt", country="BE", currency="EUR")
        assert len(rows) == 2 and len(calls) == 2
        a = rows[0]
        assert a.price == Decimal("39.95") and a.original_price == Decimal("49.99")
        assert a.upc == "036000291452" and a.mpn is None and a.brand is None
        assert a.availability == "unknown" and a.direct_url is None
        assert a.affiliate_network == "rakuten" and a.affiliate_url.startswith(
            "https://click.linksynergy.com/"
        )
        assert a.external_merchant_id == "12345" and a.store_name == "Fixture Clothing"
        assert a.store_slug == rows[1].store_slug == "rakuten_12345"
        assert not provider.supports_url(a.url)
        with pytest.raises(UnsupportedStoreError):
            await provider.resolve_url(a.url)
        assert Capability.SEARCH_GTIN not in provider.capabilities
        assert Capability.REFRESH not in provider.capabilities
        assert await provider.search("shirt", country="US") == []
        assert await provider.search("shirt", country=None) == []
        assert await provider.search("&?", country="BE") == []
        assert await provider.search("shirt", country="BE", currency="USD") == []
        await provider.discover(DiscoveryQuery("model", "Sony WH-1000XM6", "BE", "EUR"))
        assert calls[-1].url.params["exact"] == "Sony WH 1000XM6"
        assert "gtin" not in calls[-1].url.params
        assert all(int(c.url.params["max"]) <= 100 for c in calls)


@pytest.mark.parametrize(
    "retail,sale,current,original",
    [
        ("49.99", "0", "49.99", None),
        ("49.99", "-1", "49.99", None),
        ("49.99", "59.99", "59.99", None),
        ("49.99", "39.95", "39.95", "49.99"),
    ],
)
def test_sale_rules(retail, sale, current, original):
    root = parse_xml(
        XML.replace(b">49.99<", f">{retail}<".encode()).replace(b">39.95<", f">{sale}<".encode())
    )
    row = RakutenProvider._normalize(root.find("item"), "BE", "12345")
    assert row.price == Decimal(current)
    assert row.original_price == (Decimal(original) if original else None)


@pytest.mark.parametrize(
    "old,new",
    [
        (b">49.99<", b">0<"),
        (b">49.99<", b">NaN<"),
        (b">39.95<", b">bad<"),
        (b'currency="EUR"', b'currency="BADDD"'),
        (b"https://click.linksynergy.com", b"http://click.linksynergy.com"),
        (b"https://click.linksynergy.com", b"https://evil.example"),
        (b"<mid>12345", b"<mid>67890"),
    ],
)
async def test_malformed_item_does_not_drop_valid_neighbor(old, new, redis):
    root = parse_xml(XML)
    from xml.etree.ElementTree import tostring

    item = tostring(root.find("item"))
    body = XML.replace(b"</result>", item.replace(old, new) + b"</result>")

    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert len(await adapter(client, redis, max_pages=1).search("shirt", country="BE")) == 1


def test_identifier_and_metadata_bounds_and_currency_mismatch():
    body = XML.replace(b"036000291452", b"036000291453").replace(
        b'<saleprice currency="EUR">', b'<saleprice currency="USD">'
    )
    item = parse_xml(body).find("item")
    item.find("description/short").text = "x" * 10000
    row = RakutenProvider._normalize(item, "BE", "12345")
    assert row.upc is None and row.metadata["raw_upc"] == "036000291453"
    assert row.price == Decimal("49.99") and row.original_price is None
    assert len(row.metadata["description"]) == 1000


async def test_auth_retry_and_shared_concurrent_request_limit(redis):
    gets, tokens = [], []

    def handler(request):
        if request.url.path == "/token":
            tokens.append(request)
            return httpx.Response(200, json={"access_token": f"t{len(tokens)}", "expires_in": 3600})
        gets.append(request)
        if request.headers["Authorization"] == "Bearer t1":
            return httpx.Response(401)
        return httpx.Response(200, content=XML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = adapter(client, redis, max_pages=1)
        assert len(await provider.search("shirt", country="BE")) == 1
        assert len(gets) == 2 and len(tokens) == 2
        provider.limiter.settings.provider_rate_limits["rakuten"] = 4
        results = await asyncio.gather(
            *(provider.search("shirt", country="BE") for _ in range(8)), return_exceptions=True
        )
        assert len(gets) <= 4
        assert any(isinstance(r, RateLimitExceededError) for r in results)


@pytest.mark.parametrize(
    "body",
    [
        b"<broken",
        b"<error/>",
        b'<!DOCTYPE result [<!ENTITY x SYSTEM "file:///etc/passwd">]><result>&x;</result>',
        b"x" * 25000,
    ],
)
async def test_xml_security_and_size(body, redis):
    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUnavailableError):
            await adapter(client, redis).search("shirt", country="BE")

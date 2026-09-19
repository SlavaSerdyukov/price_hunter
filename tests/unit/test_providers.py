import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from pricehunter.domain.errors import InvalidProductUrlError, ProviderUnavailableError
from pricehunter.providers.base import OfferReference
from pricehunter.providers.ebay import EbayBrowseProvider
from pricehunter.providers.http import ProviderHTTP


@pytest.fixture
def ebay_item():
    return json.loads((Path(__file__).parents[1] / "fixtures/ebay_item.json").read_text())


async def test_ebay_lookup_search_refresh(respx_mock, ebay_item):
    token = respx_mock.post("https://api.ebay.com/identity/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "fake", "expires_in": 7200}),
    )
    lookup = respx_mock.get("https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id").mock(
        return_value=httpx.Response(200, json=ebay_item),
    )
    search = respx_mock.get("https://api.ebay.com/buy/browse/v1/item_summary/search").mock(
        return_value=httpx.Response(200, json={"itemSummaries": [ebay_item]}),
    )
    respx_mock.get("https://api.ebay.com/buy/browse/v1/item/v1%7C123456789012%7C0").mock(
        return_value=httpx.Response(200, json=ebay_item),
    )
    async with httpx.AsyncClient() as client:
        provider = EbayBrowseProvider(
            ProviderHTTP(client, timeout=5, max_bytes=10000), "client", "secret", ["DE"]
        )
        result = await provider.resolve_url("https://www.ebay.de/itm/123456789012")
        assert result.price == Decimal("89.99")
        assert result.gtin == "4006381333931"
        assert result.currency == "EUR"
        assert result.availability == "in_stock"
        assert lookup.calls[0].request.url.params["legacy_item_id"] == "123456789012"
        assert lookup.calls[0].request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_DE"
        assert await provider.search("4006381333931", country="DE") == [result]
        assert search.calls[0].request.url.params["gtin"] == "4006381333931"
        assert await provider.search("studio", country="US") == []
        assert (
            await provider.refresh_offer(
                OfferReference(result.url, result.external_id, result.store_slug, 0, {})
            )
            == result
        )
        assert token.call_count == 1
        with pytest.raises(InvalidProductUrlError):
            await provider.resolve_url("https://www.ebay.de.evil.com/itm/123456789012")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "http://169.254.169.254/"}),
        httpx.Response(429),
        httpx.Response(500),
        httpx.Response(200, text="bad json"),
        httpx.Response(200, content=b"a" * 10001),
    ],
)
async def test_http_bounds_and_redirects(respx_mock, response):
    route = respx_mock.get("https://api.ebay.com/test").mock(return_value=response)
    async with httpx.AsyncClient() as client:
        http = ProviderHTTP(client, timeout=1, max_bytes=10000)
        with pytest.raises(ProviderUnavailableError):
            await http.json("GET", "https://api.ebay.com/test", domains={"api.ebay.com"})
    assert route.call_count == 1


async def test_malformed_provider_price(respx_mock, ebay_item):
    ebay_item["price"]["value"] = "-1"
    async with httpx.AsyncClient() as client:
        provider = EbayBrowseProvider(
            ProviderHTTP(client, timeout=1, max_bytes=10000), "", "", ["DE"]
        )
        with pytest.raises(ProviderUnavailableError):
            provider._normalize(ebay_item, "DE")
        ebay_item["price"]["value"] = "89.99"
        ebay_item["gtin"] = "Does not apply"
        normalized = provider._normalize(ebay_item, "DE")
        assert normalized.price == Decimal("89.99") and normalized.gtin is None

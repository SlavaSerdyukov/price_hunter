import copy
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from pricehunter.apps.check_ebay import check
from pricehunter.core.config import Settings
from pricehunter.domain.errors import ProviderHTTPError, ProviderUnavailableError
from pricehunter.domain.products import ProductMatcher
from pricehunter.providers.base import OfferReference
from pricehunter.providers.ebay import MARKETPLACES, EbayBrowseProvider
from pricehunter.providers.http import ProviderHTTP


@pytest.fixture
def item():
    return json.loads((Path(__file__).parents[1] / "fixtures/ebay_item.json").read_text())


def provider(client, countries=None, locale="nl-BE"):
    return EbayBrowseProvider(
        ProviderHTTP(client, timeout=5, max_bytes=10000),
        "client",
        "secret",
        countries or list(MARKETPLACES),
        belgium_locale=locale,
    )


@pytest.mark.parametrize("country", ["BE", "DE", "FR", "NL", "IT", "ES", "AT", "IE", "PL"])
async def test_eu_marketplaces(respx_mock, item, country):
    host, marketplace = MARKETPLACES[country]
    item["itemWebUrl"] = f"https://www.{host}/itm/123456789012"
    respx_mock.post("https://api.ebay.com/identity/v1/oauth2/token").respond(
        200,
        json={"access_token": "fake", "expires_in": 7200},
    )
    route = respx_mock.get("https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id").respond(
        200,
        json=item,
    )
    async with httpx.AsyncClient() as client:
        adapter = provider(client, [country])
        offer = await adapter.resolve_url(item["itemWebUrl"])
    assert offer.store_slug == f"ebay_{country.lower()}"
    assert route.calls[0].request.headers["X-EBAY-C-MARKETPLACE-ID"] == marketplace


@pytest.mark.parametrize(
    "host,locale",
    [
        ("ebay.be", "nl-BE"),
        ("www.ebay.be", "nl-BE"),
        ("ebay.com.be", "nl-BE"),
        ("www.ebay.com.be", "nl-BE"),
        ("benl.ebay.be", "nl-BE"),
        ("www.benl.ebay.be", "nl-BE"),
        ("befr.ebay.be", "fr-BE"),
        ("www.befr.ebay.be", "fr-BE"),
    ],
)
async def test_belgian_aliases_and_variation_refresh(respx_mock, item, host, locale):
    item["itemWebUrl"] = f"https://{host}/itm/headphones/123456789012?var=123456789013"
    item["itemId"] = "v1|123456789012|123456789013"
    respx_mock.post("https://api.ebay.com/identity/v1/oauth2/token").respond(
        200,
        json={"access_token": "fake", "expires_in": 7200},
    )
    lookup = respx_mock.get(
        "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id"
    ).respond(
        200,
        json=item,
    )
    refresh = respx_mock.get(
        "https://api.ebay.com/buy/browse/v1/item/v1%7C123456789012%7C123456789013"
    ).respond(
        200,
        json=item,
    )
    async with httpx.AsyncClient() as client:
        adapter = provider(client, ["BE"])
        result = await adapter.resolve_url(item["itemWebUrl"])
        await adapter.refresh_offer(
            OfferReference(
                result.url,
                result.external_id,
                result.store_slug,
                0,
                {},
            )
        )
    assert lookup.calls[0].request.url.params["legacy_variation_id"] == "123456789013"
    for route in (lookup, refresh):
        assert route.calls[0].request.headers["Accept-Language"] == locale
        assert route.calls[0].request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_BE"


async def test_french_search_and_partial_results(respx_mock, item):
    item["itemWebUrl"] = "https://befr.ebay.be/itm/123456789012"
    respx_mock.post("https://api.ebay.com/identity/v1/oauth2/token").respond(
        200,
        json={"access_token": "fake", "expires_in": 7200},
    )
    route = respx_mock.get("https://api.ebay.com/buy/browse/v1/item_summary/search").respond(
        200,
        json={"itemSummaries": [{"price": None}, "bad", item]},
    )
    async with httpx.AsyncClient() as client:
        results = await provider(client, ["BE", "DE"], "fr-BE").search("headphones")
    assert len(results) == 1 and results[0].country == "BE"
    request = route.calls[0].request
    assert request.headers["Accept-Language"] == "fr-BE"
    assert request.url.params["filter"] == "buyingOptions:{FIXED_PRICE}"


@pytest.mark.parametrize("status,expected_calls", [(401, 2), (403, 1), (429, 1)])
async def test_token_recovery_is_bounded(respx_mock, status, expected_calls):
    token = respx_mock.post("https://api.ebay.com/identity/v1/oauth2/token").respond(
        200,
        json={"access_token": "fake", "expires_in": 7200},
    )
    route = respx_mock.get("https://api.ebay.com/buy/browse/v1/item_summary/search").respond(status)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderHTTPError) as error:
            await provider(client).search("headphones")
    assert error.value.http_status == status
    assert route.call_count == token.call_count == expected_calls


async def test_rejected_token_is_replaced(respx_mock, item):
    token = respx_mock.post("https://api.ebay.com/identity/v1/oauth2/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "old", "expires_in": 7200}),
            httpx.Response(200, json={"access_token": "new", "expires_in": 7200}),
        ]
    )
    route = respx_mock.get("https://api.ebay.com/buy/browse/v1/item_summary/search").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"itemSummaries": [item]}),
        ]
    )
    async with httpx.AsyncClient() as client:
        assert len(await provider(client, ["DE"]).search("headphones")) == 1
    assert token.call_count == 2
    assert route.calls[1].request.headers["Authorization"] == "Bearer new"


async def test_native_currency_and_localized_variants(item):
    item["price"] = {
        "value": "115.20",
        "currency": "EUR",
        "convertedFromValue": "99.99",
        "convertedFromCurrency": "GBP",
    }
    item["localizedAspects"] = [{"name": "Größe", "value": "42"}, {"name": "Farbe", "value": "Rot"}]
    async with httpx.AsyncClient() as client:
        adapter = provider(client)
        first = adapter._normalize(item, "DE")
        other = copy.deepcopy(item)
        other["localizedAspects"][0]["value"] = "44"
        second = adapter._normalize(other, "DE")
        assert first.price == Decimal("99.99") and first.currency == "GBP"
        assert first.variant["size"] == "42" and first.variant["color"] == "Rot"
        assert not ProductMatcher().match(first, second).matched
        del item["price"]["convertedFromCurrency"]
        with pytest.raises(ProviderUnavailableError):
            adapter._normalize(item, "DE")


@pytest.mark.parametrize("token", [None, "", 123])
async def test_invalid_oauth_response(respx_mock, token):
    respx_mock.post("https://api.ebay.com/identity/v1/oauth2/token").respond(
        200,
        json={"access_token": token, "expires_in": 7200},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderUnavailableError):
            await provider(client).search("headphones")


async def test_diagnostic_missing_keys_makes_no_requests(capsys):
    def forbidden(request):
        pytest.fail("Missing keys must never cause a network request")

    result = await check(
        Settings(_env_file=None),
        country="BE",
        query="headphones",
        transport=httpx.MockTransport(forbidden),
    )
    assert result == 2
    assert "EBAY_CLIENT_ID" in capsys.readouterr().out


async def test_diagnostic_distinguishes_oauth_rejection_from_browse_access(capsys):
    calls = []

    def response(request):
        calls.append(request.url.path)
        return httpx.Response(401, json={"error": "invalid_client"})

    result = await check(
        Settings(
            _env_file=None, ebay_client_id=SecretStr("id"), ebay_client_secret=SecretStr("secret")
        ),
        country="BE",
        query="headphones",
        transport=httpx.MockTransport(response),
    )
    assert result == 1
    assert calls == ["/identity/v1/oauth2/token"]
    assert "OAuth failed: HTTP 401" in capsys.readouterr().out


@pytest.mark.parametrize("status", [200, 401, 403, 429])
async def test_diagnostic_and_secret_redaction(item, capsys, status):
    secret = "never-print-this-secret"
    calls = []

    def response(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": secret, "expires_in": 7200})
        if status != 200:
            return httpx.Response(status, text=f"private server error {secret}")
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"itemSummaries": [item]})
        return httpx.Response(200, json=item)

    result = await check(
        Settings(
            _env_file=None, ebay_client_id=SecretStr(secret), ebay_client_secret=SecretStr(secret)
        ),
        country="DE",
        query="headphones",
        transport=httpx.MockTransport(response),
    )
    output = capsys.readouterr().out
    assert secret not in output
    assert result == (0 if status == 200 else 1)
    if status == 200:
        assert '"refresh": "ok"' in output and len(calls) == 4
    else:
        assert f"HTTP {status}" in output

import copy
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from pricehunter.core.config import Settings
from pricehunter.core.container import Container
from pricehunter.domain.discovery import DiscoveryQuery
from pricehunter.domain.errors import (
    InvalidProductUrlError,
    ProductNotFoundError,
    ProviderHTTPError,
    ProviderUnavailableError,
    UnsupportedProductError,
)
from pricehunter.providers.amazon import AmazonCreatorsProvider
from pricehunter.providers.base import OfferReference
from pricehunter.providers.http import ProviderHTTP


@pytest.fixture
def item():
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/amazon_creators_item.json").read_text()
    )


def provider(client):
    return AmazonCreatorsProvider(
        ProviderHTTP(client, timeout=5, max_bytes=10000),
        "client-id",
        "client-secret",
        {"BE": {"partner_tag": "test-be-21"}, "DE": {"partner_tag": "test-de-21"}},
    )


def token_route(respx_mock):
    return respx_mock.post("https://api.amazon.co.uk/auth/o2/token").respond(
        200, json={"access_token": "test-token", "expires_in": 3600, "token_type": "bearer"}
    )


@pytest.mark.parametrize("envelope", ["itemsResult", "itemResults"])
async def test_resolve_search_refresh_and_oauth_json(respx_mock, item, envelope):
    token = token_route(respx_mock)
    get = respx_mock.post(AmazonCreatorsProvider.api + "getItems").respond(
        200, json={envelope: {"items": [item]}}
    )
    search = respx_mock.post(AmazonCreatorsProvider.api + "searchItems").respond(
        200, json={"searchResult": {"items": [item]}}
    )
    async with httpx.AsyncClient() as client:
        adapter = provider(client)
        offer = await adapter.resolve_url(item["detailPageURL"])
        assert offer.price == Decimal("29.99") and offer.original_price == Decimal("39.99")
        assert offer.variant == {"size": "M", "color": "Blue"}
        assert offer.availability == "in_stock" and offer.asin == "B012345678"
        assert await adapter.search("shirt", country="BE", currency="EUR") == [offer]
        assert (
            await adapter.refresh_offer(
                OfferReference(offer.url, offer.external_id, offer.store_slug, 0, offer.metadata)
            )
            == offer
        )
        assert await adapter.search("shirt", country="US") == []
    assert token.call_count == 1 and get.call_count == 2
    assert json.loads(token.calls[0].request.content) == {
        "grant_type": "client_credentials",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "scope": "creatorsapi::default",
    }
    for route in [get, search]:
        request = route.calls[0].request
        assert request.headers["x-marketplace"] == "www.amazon.com.be"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["content-type"] == "application/json"
        payload = json.loads(request.content)
        assert payload["partnerTag"] == "test-be-21" and payload["condition"] == "New"
        assert "offersV2.listings.price" in payload["resources"]


async def test_discovery_asin_details_currency_and_no_result(respx_mock, item):
    token_route(respx_mock)
    route = respx_mock.post(AmazonCreatorsProvider.api + "getItems").respond(
        200, json={"itemsResult": {"items": [item]}}
    )
    respx_mock.post(AmazonCreatorsProvider.api + "searchItems").respond(
        200, json={"searchResult": {"items": [item]}}
    )
    async with httpx.AsyncClient() as client:
        adapter = provider(client)
        query = DiscoveryQuery("asin", "B012345678", "BE", "EUR")
        assert len(await adapter.discover(query)) == 1
        assert await adapter.discover(DiscoveryQuery("asin", query.text, "BE", "USD")) == []
        assert len(await adapter.discover(DiscoveryQuery("model", "shirt", "BE", "EUR"))) == 1
        route.respond(200, json={"itemsResult": {"items": []}})
        assert await adapter.discover(query) == []


@pytest.mark.parametrize("status,calls", [(401, 2), (403, 1), (429, 1)])
async def test_bounded_auth_recovery_without_secret_errors(respx_mock, status, calls):
    token = token_route(respx_mock)
    route = respx_mock.post(AmazonCreatorsProvider.api + "getItems").respond(
        status, text="private response client-secret test-token"
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderHTTPError) as error:
            await provider(client).resolve_url("https://amazon.com.be/dp/B012345678")
    assert token.call_count == route.call_count == calls
    assert "client-secret" not in str(error.value) and "test-token" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://amazon.com.be/dp/B012345678",
        "https://amazon.com.be.evil.test/dp/B012345678",
        "https://127.0.0.1/dp/B012345678",
        "https://amzn.eu/d/test",
        "https://amazon.com.be/cart",
        "https://user:pass@amazon.com.be/dp/B012345678",
    ],
)
async def test_invalid_urls_never_fetch(respx_mock, url):
    async with httpx.AsyncClient() as client:
        with pytest.raises(InvalidProductUrlError):
            await provider(client).resolve_url(url)
    assert not respx_mock.calls


@pytest.mark.parametrize(
    "change",
    [
        {"violatesMAP": True},
        {"type": "SUBSCRIBE_AND_SAVE"},
        {"dealDetails": {"accessType": "PRIME_EXCLUSIVE"}},
        {"dealDetails": {"accessType": "ALL"}},
        {"condition": {"value": "Used"}},
        {"isBuyBoxWinner": False},
    ],
)
async def test_conditional_prices_are_not_general_prices(respx_mock, item, change):
    token_route(respx_mock)
    item["offersV2"]["listings"][0].update(change)
    respx_mock.post(AmazonCreatorsProvider.api + "getItems").respond(
        200, json={"itemsResult": {"items": [item]}}
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UnsupportedProductError):
            await provider(client).resolve_url(item["detailPageURL"])


async def test_search_skips_bad_items_and_preserves_native_currency(respx_mock, item):
    token_route(respx_mock)
    respx_mock.post(AmazonCreatorsProvider.api + "searchItems").respond(
        200, json={"searchResult": {"items": [None, {"asin": "bad"}, item]}}
    )
    async with httpx.AsyncClient() as client:
        adapter = provider(client)
        assert len(await adapter.search("shirt", country="BE")) == 1
        assert await adapter.search("shirt", currency="USD") == []


@pytest.mark.parametrize("mutation", ["asin", "marketplace", "missing_price", "duplicate"])
async def test_wrong_or_unusable_offers_never_overwrite_tracked_price(respx_mock, item, mutation):
    token_route(respx_mock)
    data = copy.deepcopy(item)
    if mutation == "asin":
        data["asin"] = "B099999999"
    elif mutation == "marketplace":
        data["detailPageURL"] = "https://www.amazon.de/dp/B012345678"
    elif mutation == "missing_price":
        del data["offersV2"]["listings"][0]["price"]
    rows = [data, data] if mutation == "duplicate" else [data]
    respx_mock.post(AmazonCreatorsProvider.api + "getItems").respond(
        200, json={"itemsResult": {"items": rows}}
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderUnavailableError):
            await provider(client).resolve_url(item["detailPageURL"])


async def test_inaccessible_item_and_error_envelope(respx_mock):
    token_route(respx_mock)
    route = respx_mock.post(AmazonCreatorsProvider.api + "getItems")
    async with httpx.AsyncClient() as client:
        adapter = provider(client)
        route.respond(200, json={"errors": [{"code": "ItemNotAccessible"}]})
        with pytest.raises(ProductNotFoundError):
            await adapter.resolve_url("https://amazon.com.be/dp/B012345678")
        route.respond(200, json={"errors": [{"code": "AccessDenied"}]})
        with pytest.raises(ProviderUnavailableError):
            await adapter.resolve_url("https://amazon.com.be/dp/B012345678")


@pytest.mark.parametrize("approval,credentials", [(False, False), (False, True), (True, False)])
def test_enabling_requires_both_approval_and_credentials(approval, credentials):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            amazon_enabled=True,
            amazon_price_tracking_approved=approval,
            amazon_creator_client_id=SecretStr("id" if credentials else ""),
            amazon_creator_client_secret=SecretStr("secret" if credentials else ""),
            amazon_marketplaces={"BE": {"partner_tag": "test-be-21"}},
        )


async def test_disabled_amazon_not_registered_or_requested(respx_mock):
    container = Container(Settings(_env_file=None))
    try:
        assert "amazon" not in container.registry.providers
        assert not respx_mock.calls
    finally:
        await container.close()


async def test_disabled_diagnostic_never_calls_amazon(respx_mock, capsys):
    from pricehunter.apps.check_amazon import check

    assert await check(Settings(_env_file=None), country="BE", query="shirt") == 2
    assert not respx_mock.calls
    assert "No requests made" in capsys.readouterr().out

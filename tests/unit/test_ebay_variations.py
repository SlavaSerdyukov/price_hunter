import copy
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from pricehunter.domain.errors import (
    InvalidProductUrlError,
    ProviderHTTPError,
    ProviderUnavailableError,
    VariantSelectionRequiredError,
)
from pricehunter.domain.products import ProductMatcher
from pricehunter.providers.base import OfferReference
from pricehunter.providers.ebay import EbayBrowseProvider
from pricehunter.providers.http import ProviderHTTP

LOOKUP = "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id"
GROUP = "https://api.ebay.com/buy/browse/v1/item/get_items_by_item_group"
PARENT = "https://www.ebay.de/itm/167526377039"


def group_fixture():
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/ebay_variation_group.json").read_text()
    )


def provider(client):
    return EbayBrowseProvider(
        ProviderHTTP(client, timeout=5, max_bytes=50000), "id", "secret", ["DE", "BE"]
    )


def oauth(respx_mock):
    respx_mock.post("https://api.ebay.com/identity/v1/oauth2/token").respond(
        200, json={"access_token": "fake", "expires_in": 3600}
    )


async def test_real_group_url_prompts_specific_sizes_without_following_error_href(respx_mock):
    oauth(respx_mock)
    respx_mock.get(LOOKUP).respond(
        400,
        json={
            "errors": [
                {
                    "errorId": 11006,
                    "message": "private secret text",
                    "parameters": [{"name": "itemGroupHref", "value": "https://127.0.0.1/admin"}],
                }
            ]
        },
    )
    group = respx_mock.get(GROUP).respond(200, json=group_fixture())
    async with httpx.AsyncClient() as client:
        with pytest.raises(VariantSelectionRequiredError) as error:
            await provider(client).resolve_url(PARENT + "?itmprp=ad-data&itmmeta=ignored")
    assert len(error.value.options) == 11
    assert error.value.options[0].label == "Uk Schuhgröße: 5,5"
    assert error.value.options[1].label == "Uk Schuhgröße: 6"
    assert error.value.options[0].url == PARENT + "?var=467150657985"
    assert dict(group.calls[0].request.url.params) == {"item_group_id": "167526377039"}
    assert len(respx_mock.calls) == 3  # OAuth, legacy lookup, fixed group endpoint.


async def test_selected_size_identity_title_and_refresh(respx_mock):
    oauth(respx_mock)
    item = group_fixture()["items"][0]
    lookup = respx_mock.get(LOOKUP).respond(200, json=item)
    refresh = respx_mock.get(
        "https://api.ebay.com/buy/browse/v1/item/v1%7C167526377039%7C467150657985"
    ).respond(200, json=item)
    async with httpx.AsyncClient() as client:
        adapter = provider(client)
        offer = await adapter.resolve_url(item["itemWebUrl"])
        assert offer.price == Decimal("239.95") and offer.currency == "EUR"
        assert offer.variant["size_uk"] == "5,5"
        assert offer.variant["ebay_variation_id"] == "467150657985"
        assert "5,5" in offer.title and "5-12" not in offer.title and "38-47" not in offer.title
        assert lookup.calls[0].request.url.params["legacy_variation_id"] == "467150657985"
        assert (
            await adapter.refresh_offer(
                OfferReference(offer.url, offer.external_id, offer.store_slug, 0, {})
            )
            == offer
        )
        other = adapter._normalize(group_fixture()["items"][1], "DE")
        assert not ProductMatcher().match(offer, other).matched
    assert refresh.call_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"errors": [{"errorId": 11001, "message": "private"}]},
        {"errors": [{"errorId": "11006"}]},
        {"errors": "11006"},
    ],
)
async def test_unrelated_errors_never_request_a_group(respx_mock, payload):
    oauth(respx_mock)
    respx_mock.get(LOOKUP).respond(400, json=payload)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderHTTPError) as error:
            await provider(client).resolve_url(PARENT)
    assert error.value.http_status == 400
    assert "private" not in str(error.value) and "private" not in repr(vars(error.value))
    assert len(respx_mock.calls) == 2


@pytest.mark.parametrize("body", [b"bad json", b"x" * 50001])
async def test_error_body_parsing_remains_bounded_and_private(respx_mock, body):
    respx_mock.get(LOOKUP).respond(400, content=body)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderHTTPError) as error:
            await provider(client).http.json("GET", LOOKUP, domains={"api.ebay.com"})
    assert error.value.error_codes == ()


async def test_group_rejects_another_listings_variants(respx_mock):
    oauth(respx_mock)
    respx_mock.get(LOOKUP).respond(400, json={"errors": [{"errorId": 11006}]})
    rows = group_fixture()
    for row in rows["items"]:
        row["primaryItemGroup"]["itemGroupId"] = "999999999999"
    respx_mock.get(GROUP).respond(200, json=rows)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderUnavailableError):
            await provider(client).resolve_url(PARENT)


async def test_provider_cannot_replace_explicitly_selected_size(respx_mock):
    oauth(respx_mock)
    row = copy.deepcopy(group_fixture()["items"][0])
    row["itemId"] = "v1|167526377039|467150657986"
    respx_mock.get(LOOKUP).respond(200, json=row)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderUnavailableError):
            await provider(client).resolve_url(PARENT + "?var=467150657985")


async def test_multiple_selection_parameters_rejected_before_request(respx_mock):
    async with httpx.AsyncClient() as client:
        with pytest.raises(InvalidProductUrlError):
            await provider(client).resolve_url(PARENT + "?var=467150657985&var=467150657986")
    assert not respx_mock.calls

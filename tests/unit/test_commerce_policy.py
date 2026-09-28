import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from pricehunter.core.config import Settings
from pricehunter.core.xml import parse_xml
from pricehunter.db.models import Store, StoreOffer
from pricehunter.domain.errors import (
    InvalidProductUrlError,
    ProductNotFoundError,
    ProviderPolicyError,
    ProviderUnavailableError,
)
from pricehunter.domain.markets import validate_country
from pricehunter.domain.products import ProductMatcher, ProductOfferData
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY, ProviderDataPolicy
from pricehunter.providers.ebay import EbayBrowseProvider
from pricehunter.providers.http import ProviderHTTP
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.services.fx_service import parse_ecb
from pricehunter.services.outbound_service import OutboundLinkService, validate_redirect_settings

FIXTURES = Path(__file__).parents[1] / "fixtures"
NOW = datetime(2026, 9, 21, 18, tzinfo=UTC)


@pytest.mark.parametrize(
    "base,quote,rate,amount",
    [
        ("EUR", "USD", Decimal("1.15"), Decimal("115.00")),
        ("USD", "EUR", Decimal(1) / Decimal("1.15"), Decimal("86.96")),
        ("GBP", "PLN", Decimal("4.25") / Decimal("0.86"), Decimal("494.19")),
        ("EUR", "EUR", Decimal(1), Decimal("100.00")),
        ("EUR", "JPY", Decimal("166.1234"), Decimal("16612")),
        ("EUR", "XTS", None, None),
    ],
)
def test_reference_fx_decimal_cross_rates_weekend(base, quote, rate, amount):
    snapshot = parse_ecb((FIXTURES / "ecb_daily.xml").read_bytes(), NOW)
    assert snapshot.effective_date == date(2026, 9, 18)
    assert snapshot.fetched_at == NOW and snapshot.source == "ECB"
    assert snapshot.cross_rate(base, quote) == rate
    assert snapshot.convert(Decimal("100"), base, quote) == amount
    assert snapshot.rates["USD"] == Decimal("1.15")


@pytest.mark.parametrize("rate", ["0", "-1", "NaN", "Infinity", "bad", "1e100", "0.0000000000001"])
def test_fx_rejects_invalid_rate(rate):
    payload = (FIXTURES / "ecb_daily.xml").read_bytes().replace(b'"1.15"', f'"{rate}"'.encode())
    with pytest.raises(ProviderUnavailableError):
        parse_ecb(payload, NOW)


@pytest.mark.parametrize(
    "payload",
    [
        b"<a/>",
        b'<a time="2030-01-01"><c currency="USD" rate="1"/></a>',
        b'<a time="2026-09-18"/>',
        b'<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>',
    ],
)
def test_invalid_fx_envelope(payload):
    with pytest.raises(ProviderUnavailableError):
        parse_ecb(payload, NOW)


def rows():
    from pricehunter.db.base import utcnow

    store = Store(
        id=uuid4(),
        slug="ebay_de",
        domain="ebay.de",
        country="DE",
        name="eBay",
        provider_type="ebay",
        active=True,
        supported=True,
    )
    offer = StoreOffer(
        id=uuid4(),
        store_id=store.id,
        product_id=uuid4(),
        price=Decimal("100"),
        currency="EUR",
        direct_url="https://www.ebay.de/itm/123456789012",
        affiliate_url="https://www.ebay.de/itm/123456789012?campid=1234567890",
        affiliate_network="ebay_epn",
        last_checked_at=utcnow(),
    )
    return store, offer


def link_settings(**options):
    return Settings(_env_file=None, provider_data_policies={"ebay": SYNTHETIC_POLICY}, **options)


def test_affiliate_selection_and_signed_token_identity():
    store, offer = rows()
    plain = OutboundLinkService(link_settings())
    assert plain.destination(offer, store) == (offer.direct_url, None)
    settings = link_settings(
        ebay_epn_campaign_id="1234567890",
        public_base_url="https://prices.example",
        redirect_signing_secret=SecretStr("x" * 32),
    )
    validate_redirect_settings(settings)
    service = OutboundLinkService(settings)
    assert service.destination(offer, store) == (offer.affiliate_url, "ebay_epn")
    token = service.sign(offer.id, surface="telegram", market_country="DE", now=NOW)
    identity = service.verify(token, now=NOW)
    assert identity[:3] == (offer.id, "telegram", "DE")
    assert len(identity[3]) == 32 and "ebay" not in token
    assert service.link(offer, store).startswith("https://prices.example/r/")
    for broken in (
        token[:-1] + ("A" if token[-1] != "A" else "B"),
        "https://evil.example",
        "",
        token + "?url=https://evil.example",
    ):
        with pytest.raises(ProductNotFoundError):
            service.verify(broken, now=NOW)
    with pytest.raises(ProductNotFoundError):
        service.verify(token, now=NOW + timedelta(days=2))
    store.active = False
    with pytest.raises(ProductNotFoundError):
        service.destination(offer, store)


@pytest.mark.parametrize("host", ["www.ebay.be", "benl.ebay.be", "www.befr.ebay.be"])
def test_ebay_belgian_alias_destinations_remain_usable(host):
    store, offer = rows()
    store.domain = "ebay.com.be"
    offer.direct_url = f"https://{host}/itm/123456789012"
    assert OutboundLinkService(link_settings()).destination(offer, store)[0] == offer.direct_url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,test",
        "http://www.ebay.de/itm/1",
        "https://evil.example/",
        "https://www.ebay.de@evil.example/",
        "https://www.ebay.de\\@evil.example/",
        "https://127.0.0.1/",
        "https://localhost/",
        "https://www.ebay.de/\r\nLocation:evil",
    ],
)
def test_unsafe_destinations_cannot_be_injected(url):
    store, offer = rows()
    offer.direct_url = url
    with pytest.raises(InvalidProductUrlError):
        OutboundLinkService(link_settings()).destination(offer, store)


def test_affiliate_only_offer_no_fabricated_direct_url_and_neutral_match():
    original = MockStoreProvider()._offer("sony-a")
    data = original.model_dump(exclude={"direct_url", "affiliate_url", "affiliate_network", "url"})
    data.update(
        affiliate_url="https://click.linksynergy.com/link?id=fixture",
        affiliate_network="rakuten",
        direct_url=None,
        affiliate_metadata={"commission": "99%"},
    )
    offer = ProductOfferData(**data)
    assert offer.direct_url is None and offer.url == offer.affiliate_url
    assert ProductMatcher().match(original, offer).matched
    data.update(affiliate_url=None)
    with pytest.raises(ValidationError):
        ProductOfferData(**data)
    store, saved = rows()
    saved.direct_url = None
    with pytest.raises(ProviderPolicyError):
        OutboundLinkService(link_settings()).destination(saved, store)


@pytest.mark.parametrize(
    "options",
    [
        {"tracking_allowed": True},
        {"reviewed": True, "catalog_persistence_allowed": True},
        {"reviewed": True, "review_reference": "fixture", "price_history_allowed": True},
        {"affiliate_required": True},
        {"max_cache_seconds": 0},
    ],
)
def test_unreviewed_or_inconsistent_policies_rejected(options):
    with pytest.raises(ValidationError):
        ProviderDataPolicy(**options)


@pytest.mark.parametrize(
    "options",
    [
        {"public_base_url": "https://prices.example"},
        {"redirect_signing_secret": SecretStr("x" * 32)},
        {
            "public_base_url": "http://prices.example",
            "redirect_signing_secret": SecretStr("x" * 32),
        },
        {
            "public_base_url": "https://prices.example?url=x",
            "redirect_signing_secret": SecretStr("x" * 32),
        },
        {
            "public_base_url": "https://prices.example",
            "redirect_signing_secret": SecretStr("short"),
        },
    ],
)
def test_redirect_configuration_fails_closed(options):
    with pytest.raises((ValueError, InvalidProductUrlError)):
        validate_redirect_settings(link_settings(**options))


@pytest.mark.parametrize(
    "country", ["BE", "DE", "FR", "NL", "IT", "ES", "AT", "IE", "PL", "GB", "US", "CA", "JP"]
)
def test_international_iso_markets(country):
    assert validate_country(country) == country


@pytest.mark.parametrize("country", ["ZZ", "EU", "be", "USX", "12"])
def test_invalid_market_country(country):
    with pytest.raises(ValueError):
        validate_country(country)


@pytest.mark.parametrize("campaign", ["", "1234567890"])
async def test_ebay_official_epn_context_and_url(campaign):
    payload = json.loads((FIXTURES / "ebay_item.json").read_text())
    payload["itemAffiliateWebUrl"] = "https://www.ebay.de/itm/123456789012?campid=1234567890"
    calls = []

    def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fixture", "expires_in": 3600})
        calls.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = EbayBrowseProvider(
            ProviderHTTP(client, timeout=2, max_bytes=20000),
            "id",
            "secret",
            ["DE"],
            epn_campaign_id=campaign,
            delivery_country="BE",
            delivery_postal_code="1000",
        )
        offer = await adapter.resolve_url("https://www.ebay.de/itm/123456789012")
        header = calls[0].headers["X-EBAY-C-ENDUSERCTX"]
        assert "contextualLocation=country%3DBE%2Czip%3D1000" in header
        assert "affiliateReferenceId" not in header
        assert ("affiliateCampaignId=1234567890" in header) == bool(campaign)
        assert offer.direct_url == payload["itemWebUrl"]
        assert offer.affiliate_url == (payload["itemAffiliateWebUrl"] if campaign else None)
        assert offer.affiliate_network == ("ebay_epn" if campaign else None)


def test_xml_node_budget():
    with pytest.raises(ProviderUnavailableError):
        parse_xml(b"<a>" + b"<b/>" * 20001 + b"</a>")

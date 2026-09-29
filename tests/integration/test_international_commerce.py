import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update

from pricehunter.api.app import create_app
from pricehunter.core.config import Settings
from pricehunter.core.container import Container
from pricehunter.core.xml import parse_xml
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BestPriceEvent,
    FxRate,
    OutboundClick,
    PriceObservation,
    ProductDiscovery,
    ProductWatch,
    Store,
    StoreOffer,
)
from pricehunter.domain.errors import (
    CountryRequiredError,
    ProductNotFoundError,
    ProviderPolicyError,
)
from pricehunter.domain.provider_policy import ProviderDataPolicy
from pricehunter.providers.affiliate.rakuten import RakutenProvider, RakutenTokenManager
from pricehunter.providers.http import ProviderHTTP
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.schemas.api import TrackerCreate, UserSettingsPatch
from pricehunter.schemas.watches import WatchCreate
from pricehunter.services.notification_service import NotificationService
from tests.integration.test_api import api_client
from tests.integration.test_comparison import accept_price, event_types, persist, setup_watch

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures"


async def test_market_is_durable_and_same_currency_watches_coexist(container):
    user = await container.users.telegram(777)
    offer = await persist(container, "sony-a")
    with pytest.raises(CountryRequiredError):
        await container.watches.create(
            user.id, WatchCreate(product_id=offer.product_id, currency="EUR")
        )
    await container.products.persist(
        MockStoreProvider()._offer("sony-a").model_copy(update={"country": "BE"})
    )
    await container.users.settings(user.id, UserSettingsPatch(country_code="BE"))
    be = await container.watches.create(
        user.id, WatchCreate(product_id=offer.product_id, currency="EUR")
    )
    await container.users.settings(user.id, UserSettingsPatch(country_code="DE"))
    de = await container.watches.create(
        user.id, WatchCreate(product_id=offer.product_id, currency="EUR")
    )
    assert be.market_country == "BE" and de.market_country == "DE" and be.id != de.id
    again = await container.watches.create(
        user.id, WatchCreate(product_id=offer.product_id, currency="EUR", market_country="BE")
    )
    assert again.id == be.id
    other = await container.users.telegram(778)
    with pytest.raises(ProductNotFoundError):
        await container.watches.get(other.id, be.id)
    assert await container.discovery.synchronize() == 2
    await container.users.settings(user.id, UserSettingsPatch(country_code="FR"))
    assert await container.discovery.synchronize() == 0
    async with container.sessions() as session:
        assert set(await session.scalars(select(ProductWatch.market_country))) == {"BE", "DE"}
        assert set(await session.scalars(select(ProductDiscovery.country))) == {"BE", "DE"}


async def test_watch_api_requires_country_and_exposes_persisted_market(container):
    offer = await container.products.persist(
        MockStoreProvider()._offer("sony-a").model_copy(update={"country": "CA"})
    )
    async with await api_client(container, 777, country=None) as client:
        data = {"product_id": str(offer.product_id), "currency": "EUR"}
        response = await client.post("/api/v1/product-watches", json=data)
        assert response.status_code == 400 and response.json()["error"] == "country_required"
        response = await client.post(
            "/api/v1/product-watches", json={**data, "market_country": "CA"}
        )
        assert response.status_code == 201 and response.json()["market_country"] == "CA"
        assert (await client.get("/api/v1/product-watches")).json()[0]["market_country"] == "CA"
        assert (
            await client.post("/api/v1/product-watches", json={**data, "market_country": "ZZ"})
        ).status_code == 422


async def test_redirect_resolves_live_policy_and_appends_no_pii(container):
    container.settings.public_base_url = "https://prices.example"
    container.settings.redirect_signing_secret = SecretStr("fixture-signing-secret-32-characters")
    offer = await persist(container, "sony-a")
    token = container.outbound.sign(offer.id, surface="telegram", market_country="DE")
    app = create_app(container.settings, container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://testserver", follow_redirects=False
    ) as client:
        response = await client.get(
            "/r/" + token, headers={"User-Agent": "private-test", "X-Forwarded-For": "192.0.2.1"}
        )
        assert response.status_code == 302
        assert response.headers["location"] == "https://mock.pricehunter.test/products/sony-a"
        assert response.headers["cache-control"] == "no-store"
        assert (await client.get("/r/" + token + "?url=https://evil.example")).status_code == 404
        assert (await client.get("/r/" + container.outbound.sign(uuid4()))).status_code == 404
        expired = container.outbound.sign(offer.id, now=utcnow() - timedelta(days=2))
        assert (await client.get("/r/" + expired)).status_code == 404
        bad = token[:-1] + ("A" if token[-1] != "A" else "B")
        assert (await client.get("/r/" + bad)).status_code == 404
        async with container.sessions.begin() as session:
            await session.execute(update(Store).values(active=False))
        assert (await client.get("/r/" + token)).status_code == 404
    async with container.sessions() as session:
        click = (await session.scalars(select(OutboundClick))).one()
        assert (
            click.offer_id == offer.id
            and click.surface == "telegram"
            and click.market_country == "DE"
        )
        assert click.affiliate_network is None
        assert set(OutboundClick.__table__.columns.keys()) == {
            "id",
            "offer_id",
            "store_id",
            "affiliate_network",
            "surface",
            "market_country",
            "created_at",
            "opaque_click_reference",
        }
    async with container.sessions.begin() as session:
        await session.execute(
            update(OutboundClick).values(created_at=utcnow() - timedelta(days=31))
        )
    await container.outbound.retain(container.sessions)
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OutboundClick)) == 0


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,x",
        "http://mock.pricehunter.test/",
        "https://evil.example/",
    ],
)
async def test_redirect_revalidates_stored_destination(container, url):
    offer = await persist(container, "sony-a")
    container.settings.public_base_url = "https://prices.example"
    container.settings.redirect_signing_secret = SecretStr("x" * 32)
    token = container.outbound.sign(offer.id)
    async with container.sessions.begin() as session:
        await session.execute(update(StoreOffer).values(direct_url=url))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(container.settings, container)),
        base_url="http://testserver",
    ) as client:
        assert (await client.get("/r/" + token)).status_code == 400
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OutboundClick)) == 0


async def test_epn_persistence_and_affiliate_neutral_ranking_notifications(container):
    user, a, b, watch = await setup_watch(container)
    before = await container.products.product(a.product_id, user.id)
    async with container.sessions.begin() as session:
        # Higher-priced seller has lucrative affiliate metadata, and the cheaper
        # seller has no affiliate destination. Neither affects authoritative order.
        offer = await session.get(StoreOffer, b.id)
        offer.affiliate_network = "ebay_epn"
        offer.affiliate_url = "https://www.ebay.de/itm/123456789012?campid=1234567890"
        offer.affiliate_metadata = {"commission": "99%"}
    container.settings.ebay_epn_campaign_id = "1234567890"
    after = await container.products.product(a.product_id, user.id)
    assert [o.offer_id for o in before.offers] == [o.offer_id for o in after.offers]
    assert after.best_available_offer.offer_id == a.id
    assert await event_types(container) == []
    await accept_price(container, b, "sony-b", "300")
    assert await event_types(container) == ["merchant_became_cheapest"]
    container.settings.public_base_url = "https://prices.example"
    container.settings.redirect_signing_secret = SecretStr("x" * 32)

    class Sender:
        async def send(self, delivery):
            return 1

    delivery = await NotificationService(
        container.sessions, Sender(), container.entitlements, container.settings
    ).claim()
    assert delivery.url.startswith("https://prices.example/r/")
    assert container.outbound.verify(delivery.url.rsplit("/", 1)[1])[:3] == (
        b.id,
        "notification",
        "DE",
    )
    import json

    from pricehunter.providers.ebay import EbayBrowseProvider

    adapter = EbayBrowseProvider(
        ProviderHTTP(container.http, timeout=2, max_bytes=20000),
        "id",
        "secret",
        ["DE"],
        epn_campaign_id="1234567890",
    )
    payload = json.loads((FIXTURES / "ebay_item.json").read_text())
    payload["itemAffiliateWebUrl"] = "https://www.ebay.de/itm/123456789012?campid=1234567890"
    view = await container.products.persist(adapter._normalize(payload, "DE"))
    async with container.sessions() as session:
        row = await session.get(StoreOffer, view.id)
        assert (
            row.affiliate_url == payload["itemAffiliateWebUrl"]
            and row.direct_url == payload["itemWebUrl"]
        )
        assert row.affiliate_click_id is None


def search_policy():
    return ProviderDataPolicy(
        reviewed=True,
        review_reference="Synthetic Rakuten fixtures only",
        catalog_persistence_allowed=True,
        affiliate_allowed=True,
        affiliate_required=True,
        max_cache_seconds=3600,
    )


async def test_rakuten_ingests_partner_merchant_without_stock_or_history(container):
    container.settings.provider_data_policies["rakuten"] = search_policy()
    container.settings.rakuten_enabled = True
    raw = (FIXTURES / "rakuten_search.xml").read_bytes()
    data = RakutenProvider._normalize(parse_xml(raw).find("item"), "BE", "12345")
    saved = await container.products.persist(data)
    user = await container.users.telegram(777)
    comparison = await container.products.product(saved.product_id, user.id, market_country="BE")
    assert comparison.best_available_offer is None
    assert comparison.offers[0].store == "Fixture Clothing"
    assert (
        comparison.offers[0].availability == "unknown"
        and comparison.offers[0].url == data.affiliate_url
    )
    async with container.sessions() as session:
        row = await session.get(StoreOffer, saved.id)
        assert row.direct_url is None and row.affiliate_click_id is None
        merchant = await session.get(Store, row.store_id)
        assert merchant.external_merchant_id == "12345" and merchant.provider_type == "rakuten"
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 0
        assert await session.scalar(select(func.count()).select_from(BestPriceEvent)) == 0
    with pytest.raises(ProviderPolicyError):
        await container.trackers.create(user.id, TrackerCreate(store_offer_id=saved.id))
    with pytest.raises(ProviderPolicyError):
        await container.products.history(saved.id, user.id)
    with pytest.raises(ProviderPolicyError):
        await container.watches.create(
            user.id, WatchCreate(product_id=saved.product_id, currency="EUR", market_country="BE")
        )
    assert (
        await container.comparison_operations.request_refresh(
            saved.product_id, user.id, market_country="BE"
        )
    ).queued_count == 0
    assert await container.price_checks.claim_due() == []
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer).values(last_checked_at=utcnow() - timedelta(hours=2))
        )
    assert (
        await container.products.product(saved.product_id, user.id, market_country="BE")
    ).offers == []
    from pricehunter.services.catalog_policy_maintenance import CatalogPolicyMaintenance

    maintenance = CatalogPolicyMaintenance(container.sessions, container.settings)
    assert await maintenance.purge() == 1
    assert await maintenance.purge() == 0
    with pytest.raises(ProductNotFoundError):
        await container.products.offer(saved.id)


async def test_unreviewed_source_persistence_and_tracking_fail_closed(container):
    data = MockStoreProvider()._offer("sony-a").model_copy(update={"provider": "unreviewed"})
    with pytest.raises(ProviderPolicyError):
        await container.products.persist(data)
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 0


async def test_rakuten_search_partial_failure_and_no_per_user_rate_limit_bypass(container):
    container.settings.provider_data_policies["rakuten"] = search_policy()
    container.settings.rakuten_enabled = True
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "fixture", "expires_in": 3600})
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        http = ProviderHTTP(client, timeout=2, max_bytes=20000)
        provider = RakutenProvider(
            http,
            RakutenTokenManager(http, "id", "secret", "123"),
            container.limiter,
            {"DE": ["12345"]},
        )
        container.registry.providers["rakuten"] = provider
        user = await container.users.telegram(777)
        result = await container.search.search("Sony", user.id, country="DE")
        assert result.products and result.unavailable_providers == ["rakuten"]
        assert calls.count("/productsearch/1.0") == 1


async def test_fx_shared_fetch_staleness_and_native_price_unchanged(container):
    container.settings.fx_enabled = True
    payload = (
        (FIXTURES / "ecb_daily.xml")
        .read_bytes()
        .replace(b"2026-09-18", (utcnow().date() - timedelta(days=2)).isoformat().encode())
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        http = ProviderHTTP(client, timeout=2, max_bytes=20000)
        assert (
            sum(
                await asyncio.gather(
                    *(container.fx.refresh(http, container.redis) for _ in range(5))
                )
            )
            == 1
        )
        assert len(calls) == 1
        async with container.sessions() as session:
            snapshot = await container.fx.snapshot(session)
            assert snapshot and snapshot.convert(Decimal("100"), "GBP", "PLN") == Decimal("494.19")
            assert await container.fx.snapshot(session, now=utcnow() + timedelta(days=10)) is None
            assert await session.scalar(select(func.count()).select_from(FxRate)) == 4
        user = await container.users.telegram(777)
        await container.users.settings(
            user.id, UserSettingsPatch(country_code="DE", preferred_currency="USD")
        )
        offer = await persist(container, "sony-a")
        comparison = await container.products.product(offer.product_id, user.id)
        best = comparison.best_available_offer
        assert best.price == Decimal("329") and best.currency == "EUR"
        assert best.reference_price == Decimal("378.35") and best.reference_currency == "USD"
        assert best.fx_source == "ECB" and best.fx_effective_date == snapshot.effective_date
        assert best.fx_fetched_at == snapshot.fetched_at
        async with container.sessions() as session:
            native = await session.get(StoreOffer, offer.id)
            assert native.price == Decimal("329") and native.currency == "EUR"
        await container.redis.delete("ph:fx:fetch")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(503))
        ) as broken:
            assert not await container.fx.refresh(
                ProviderHTTP(broken, timeout=2, max_bytes=20000), container.redis
            )
        assert 0 < await container.redis.ttl("ph:fx:fetch") <= 300
        async with container.sessions() as session:
            assert (await container.fx.snapshot(session)).rates == snapshot.rates


async def test_rakuten_registration_requires_policy_and_credentials(sessions, redis):
    settings = Settings(
        _env_file=None,
        rakuten_enabled=True,
        rakuten_client_id=SecretStr("id"),
        rakuten_client_secret=SecretStr("secret"),
        rakuten_account_id=SecretStr("123"),
        rakuten_advertisers={"DE": ["12345"]},
        provider_data_policies={"rakuten": search_policy()},
    )
    container = Container(settings, sessions=sessions, redis=redis)
    try:
        assert isinstance(container.registry.get("rakuten"), RakutenProvider)
    finally:
        await container.close()

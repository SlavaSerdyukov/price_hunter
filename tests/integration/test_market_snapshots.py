import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from pricehunter.core.xml import parse_xml
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BestPriceEvent,
    NotificationEvent,
    PriceObservation,
    Product,
    ProductWatch,
    Store,
    StoreOffer,
    User,
)
from pricehunter.domain.discovery import DiscoveryMismatch
from pricehunter.domain.ingestion import IngestionMode
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY, ProviderDataPolicy
from pricehunter.providers.affiliate.rakuten import RakutenProvider
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.schemas.api import UserSettingsPatch
from pricehunter.schemas.watches import WatchCreate
from pricehunter.services.comparison_service import ComparisonReader
from pricehunter.services.notification_service import NotificationService
from pricehunter.services.price_check_service import RefreshClaim
from tests.integration.test_api import api_client
from tests.integration.test_international_commerce import search_policy

pytestmark = pytest.mark.integration


def market_offer(country, price, merchant="a"):
    return (
        MockStoreProvider()
        ._offer("sony-a")
        .model_copy(
            update={
                "country": country,
                "store_slug": f"market_{merchant}",
                "store_name": f"Store {merchant.upper()}",
                "external_id": f"{country}:sony",
                "price": Decimal(price),
            }
        )
    )


async def test_comparison_uses_offer_market_before_native_currency_ranking(container):
    user = await container.users.telegram(777)
    be = await container.products.persist(market_offer("BE", "329"))
    await container.products.persist(market_offer("BE", "340", "b"))
    de = await container.products.persist(market_offer("DE", "299"))
    await container.products.persist(market_offer("DE", "315", "c"))
    assert be.product_id == de.product_id
    comparison = await container.products.comparisons.get(
        be.product_id, user.id, market_country="BE"
    )
    assert comparison.best_available_offer.price == Decimal("329")
    assert comparison.offer_count == 2
    assert {offer.store_country for offer in comparison.offers} == {"BE"}


class SnapshotProvider(RakutenProvider):
    def __init__(self, data):
        self.data = data

    async def search(self, query, *, country=None, currency=None):
        return [self.data]


async def test_repeat_search_updates_one_snapshot_without_history(container):
    container.settings.provider_data_policies["rakuten"] = search_policy()
    container.settings.rakuten_enabled = True
    raw = (Path(__file__).parents[1] / "fixtures/rakuten_search.xml").read_bytes()
    data = RakutenProvider._normalize(parse_xml(raw).find("item"), "BE", "12345")
    provider = SnapshotProvider(data.model_copy(update={"price": Decimal("349")}))
    container.registry.providers = {provider.name: provider}
    user = await container.users.telegram(777)
    await container.users.settings(user.id, UserSettingsPatch(country_code="BE"))
    first = (await container.search.search("fixture", user.id, country="BE")).products[0].offers[0]
    provider.data = data.model_copy(
        update={
            "price": Decimal("299"),
            "affiliate_url": "https://click.linksynergy.com/fs-bin/click?id=fixture&offerid=new",
        }
    )
    await asyncio.gather(
        *(container.search.search("fixture", user.id, country="BE") for _ in range(2))
    )
    async with container.sessions() as session:
        saved = await session.get(StoreOffer, first.offer_id)
        assert saved.price == Decimal("299")
        assert saved.affiliate_url == provider.data.affiliate_url
        assert saved.last_checked_at > first.last_checked_at
        assert saved.observation_count == 1
        assert saved.minimum_price == saved.maximum_price == saved.total_price == Decimal("299")
        for model, count in (
            (Product, 1),
            (StoreOffer, 1),
            (PriceObservation, 0),
            (NotificationEvent, 0),
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == count


async def accept_market_price(container, view, data):
    token = uuid4()
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == view.id)
            .values(lease_token=token, lease_until=utcnow() + timedelta(minutes=2))
        )
    assert await container.price_checks.accept(RefreshClaim(view.id, token), data)


async def test_independent_watch_transitions_and_history_for_same_merchant_markets(container):
    user = await container.users.telegram(777)
    views = [
        await container.products.persist(market_offer(*args))
        for args in (("BE", "329", "a"), ("BE", "340", "b"), ("DE", "299", "a"), ("DE", "315", "c"))
    ]
    product = views[0].product_id
    watches = {
        country: await container.watches.create(
            user.id, WatchCreate(product_id=product, market_country=country, currency="EUR")
        )
        for country in ("BE", "DE")
    }
    assert watches["BE"].best_price == 329 and watches["DE"].best_price == 299
    async with container.sessions() as session:
        merchant = await session.scalar(select(Store).where(Store.slug == "market_a"))
        assert merchant.country == "BE"
        assert (await session.get(StoreOffer, views[2].id)).store_id == merchant.id
        be_history = await session.scalar(
            select(func.count())
            .select_from(BestPriceEvent)
            .where(BestPriceEvent.market_country == "BE")
        )
    await accept_market_price(container, views[2], market_offer("DE", "289"))
    assert (await container.watches.get(user.id, watches["BE"].id)).best_price == 329
    assert (await container.watches.get(user.id, watches["DE"].id)).best_price == 289
    async with container.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(BestPriceEvent)
                .where(BestPriceEvent.market_country == "BE")
            )
            == be_history
        )
        event = (await session.scalars(select(NotificationEvent))).one()
        assert (
            event.product_watch_id == watches["DE"].id and event.snapshot["market_country"] == "DE"
        )
    await accept_market_price(container, views[1], market_offer("BE", "300", "b"))
    assert (await container.watches.get(user.id, watches["BE"].id)).best_price == 300
    assert (await container.watches.get(user.id, watches["DE"].id)).best_price == 289
    for country, price in (("BE", 300), ("DE", 289)):
        history = await container.best_prices.history(
            product, user.id, "EUR", market_country=country
        )
        assert history.market_country == country and history.current_best.price == price
        assert all(p.price >= price for p in history.points if p.price)


async def test_market_scheduling_and_manual_refresh_do_not_touch_other_market(container):
    user = await container.users.telegram(777)
    be = await container.products.persist(market_offer("BE", "329"))
    de = await container.products.persist(market_offer("DE", "299"))
    later = utcnow() + timedelta(days=1)
    async with container.sessions.begin() as session:
        await session.execute(update(StoreOffer).values(next_check_at=later))
    await container.watches.create(
        user.id, WatchCreate(product_id=be.product_id, market_country="BE", currency="EUR")
    )
    async with container.sessions() as session:
        assert (await session.get(StoreOffer, be.id)).next_check_at < later
        assert (await session.get(StoreOffer, de.id)).next_check_at == later
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer).values(last_checked_at=utcnow() - timedelta(days=4))
        )
    result = await container.comparison_operations.request_refresh(
        be.product_id, user.id, market_country="BE"
    )
    assert result.queued_count == 1 and result.market_country == "BE"
    assert {c.offer_id for c in await container.price_checks.claim_due()} == {be.id}
    async with container.sessions() as session:
        assert (await session.get(StoreOffer, de.id)).next_check_at == later


async def test_watch_delivery_cancels_corrupted_foreign_market_offer(container):
    user = await container.users.telegram(777)
    be = await container.products.persist(market_offer("BE", "329"))
    de = await container.products.persist(market_offer("DE", "299"))
    watch = await container.watches.create(
        user.id, WatchCreate(product_id=be.product_id, market_country="BE", currency="EUR")
    )
    async with container.sessions.begin() as session:
        session.add(
            NotificationEvent(
                product_watch_id=watch.id,
                event_type="new_best_price",
                price=299,
                currency="EUR",
                deduplication_key="corrupt-market",
                snapshot={"offer_id": str(de.id), "title": "Sony", "market_country": "BE"},
            )
        )
    notifier = NotificationService(
        container.sessions, None, container.entitlements, container.settings
    )
    assert await notifier.claim() is None
    async with container.sessions() as session:
        assert (await session.scalars(select(NotificationEvent))).one().status == "cancelled"


async def test_evaluation_shares_summaries_for_one_hundred_watchers(container, monkeypatch):
    view = await container.products.persist(market_offer("BE", "329"))
    async with container.sessions.begin() as session:
        for _ in range(100):
            user = User(id=uuid4())
            session.add(user)
            await session.flush()
            session.add(
                ProductWatch(
                    user_id=user.id,
                    product_id=view.product_id,
                    market_country="BE",
                    currency="EUR",
                    best_offer_id=view.id,
                    best_price=329,
                )
            )
    calls = []
    summary = ComparisonReader.summary

    async def counted(self, *args, **kwargs):
        calls.append((self.permission, self.market_country))
        return await summary(self, *args, **kwargs)

    monkeypatch.setattr(ComparisonReader, "summary", counted)
    async with container.sessions.begin() as session:
        await session.get(Product, view.product_id, with_for_update=True)
        await container.watches.evaluate(session, view.product_id, market_country="BE")
    assert calls == [("tracking_allowed", "BE"), ("price_history_allowed", "BE")]


async def test_existing_refresh_offer_rejects_generic_search_overwrite(container):
    data = market_offer("BE", "329")
    first = await container.products.persist(data)
    again = await container.products.persist(data.model_copy(update={"price": Decimal("299")}))
    assert first.id == again.id and again.price == 329
    with pytest.raises(ValueError, match="cannot accept search snapshots"):
        await container.products.persist(data, mode=IngestionMode.SEARCH_SNAPSHOT)


async def test_authorized_snapshot_history_stale_versions_and_identity_guard(container):
    container.settings.provider_data_policies["rakuten"] = SYNTHETIC_POLICY
    data = market_offer("BE", "349").model_copy(update={"provider": "rakuten"})
    provider = SnapshotProvider(data)
    container.registry.providers["rakuten"] = provider
    t1 = utcnow() - timedelta(hours=2)
    first = await container.products.persist(
        data.model_copy(update={"source_updated_at": t1}), mode=IngestionMode.SEARCH_SNAPSHOT
    )
    next_data = data.model_copy(
        update={"price": Decimal("299"), "source_updated_at": t1 + timedelta(hours=1)}
    )
    await asyncio.gather(
        *(
            container.products.persist(next_data, mode=IngestionMode.SEARCH_SNAPSHOT)
            for _ in range(4)
        )
    )
    await container.products.persist(
        data.model_copy(update={"source_updated_at": t1}), mode=IngestionMode.SEARCH_SNAPSHOT
    )
    with pytest.raises(DiscoveryMismatch):
        await container.products.persist(
            next_data.model_copy(update={"gtin": "5901234123457"}),
            mode=IngestionMode.SEARCH_SNAPSHOT,
        )
    with pytest.raises(DiscoveryMismatch):
        await container.products.persist(
            next_data.model_copy(update={"currency": "USD"}), mode=IngestionMode.SEARCH_SNAPSHOT
        )
    async with container.sessions() as session:
        saved = await session.get(StoreOffer, first.id)
        assert saved.price == 299 and saved.observation_count == 2
        assert saved.total_price == 648
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 2


async def test_same_external_listing_id_and_merchant_can_have_two_markets(container):
    be, de = [
        await container.products.persist(
            market_offer(country, price).model_copy(update={"external_id": "shared-external-id"})
        )
        for country, price in (("BE", "329"), ("DE", "299"))
    ]
    assert be.id != de.id and be.product_id == de.product_id
    async with container.sessions() as session:
        rows = (await session.scalars(select(StoreOffer))).all()
        assert len({row.store_id for row in rows}) == 1
        assert {row.market_country for row in rows} == {"BE", "DE"}


async def test_search_country_isolation_survives_shared_canonical_cache(container):
    class MarketProvider(MockStoreProvider):
        async def search(self, query, *, country=None, currency=None):
            return [market_offer(country, "329" if country == "BE" else "299")]

    container.registry.providers = {"mock": MarketProvider()}
    user = await container.users.telegram(777)
    await container.users.settings(user.id, UserSettingsPatch(country_code="US"))
    ids = set()
    for country, price in (("BE", 329), ("DE", 299), ("BE", 329)):
        product = (await container.search.search("Sony", user.id, country=country)).products[0]
        ids.add(product.id)
        assert product.market_country == country and product.best_available_offer.price == price
        assert product.offer_count == 1 and {o.store_country for o in product.offers} == {country}
    assert len(ids) == 1


async def test_api_country_required_validated_and_saved_country_fallback(container):
    be = await container.products.persist(market_offer("BE", "329"))
    await container.products.persist(market_offer("DE", "299"))
    prefix = f"/api/v1/products/{be.product_id}"
    async with await api_client(container, 777, country=None) as client:
        for method, path, params in (
            ("GET", prefix, {}),
            ("GET", prefix + "/offers", {}),
            ("GET", prefix + "/best-price-history", {"currency": "EUR"}),
            ("POST", prefix + "/refresh", {}),
            ("GET", "/api/v1/search", {"q": "Sony"}),
        ):
            response = await client.request(method, path, params=params)
            assert response.status_code == 400 and response.json()["error"] == "country_required"
            assert (
                await client.request(method, path, params={**params, "country": "ZZ"})
            ).status_code == 422
        await client.patch("/api/v1/users/me/settings", json={"country_code": "DE"})
        assert (await client.get(prefix)).json()["market_country"] == "DE"
        for path in (prefix, prefix + "/offers"):
            product = (await client.get(path, params={"country": "BE"})).json()
            assert product["market_country"] == "BE" and product["offer_count"] == 1
            assert Decimal(product["best_available_offer"]["price"]) == 329
        history = (
            await client.get(
                prefix + "/best-price-history", params={"country": "BE", "currency": "EUR"}
            )
        ).json()
        assert history["market_country"] == "BE"
        assert all(Decimal(p["price"]) == 329 for p in history["points"] if p["price"])


async def test_catalog_tracking_and_history_permissions_are_independently_market_scoped(container):
    user = await container.users.telegram(777)
    tracked = await container.products.persist(market_offer("BE", "300"))
    watch = await container.watches.create(
        user.id, WatchCreate(product_id=tracked.product_id, market_country="BE", currency="EUR")
    )
    for provider, history_allowed, country, price in (
        ("catalog_only", False, "BE", "250"),
        ("history_only", True, "BE", "275"),
        ("catalog_only", False, "DE", "100"),
    ):
        container.settings.provider_data_policies[provider] = ProviderDataPolicy(
            reviewed=True,
            review_reference="Synthetic surface permissions",
            catalog_persistence_allowed=True,
            price_history_allowed=history_allowed,
        )
        await container.products.persist(
            market_offer(country, price, provider).model_copy(update={"provider": provider})
        )
    product = await container.products.product(tracked.product_id, user.id, market_country="BE")
    assert product.best_available_offer.price == 250
    trackable = await container.products.comparisons.get(
        tracked.product_id, user.id, market_country="BE", permission="tracking_allowed"
    )
    assert trackable.best_available_offer.price == 300
    assert (await container.watches.get(user.id, watch.id)).best_price == 300
    history = await container.best_prices.history(
        tracked.product_id, user.id, "EUR", market_country="BE"
    )
    assert history.current_best.price == 275
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NotificationEvent)) == 0


async def test_outbound_comparison_keeps_offer_market_after_profile_change(container):
    from pydantic import SecretStr

    container.settings.public_base_url = "https://prices.example"
    container.settings.redirect_signing_secret = SecretStr("fixture-signing-secret-32-characters")
    user = await container.users.telegram(777)
    be = await container.products.persist(market_offer("BE", "329"))
    await container.users.settings(user.id, UserSettingsPatch(country_code="DE"))
    product = await container.products.product(be.product_id, user.id, market_country="BE")
    token = product.offers[0].url.rsplit("/", 1)[1]
    assert container.outbound.verify(token)[:3] == (be.id, "comparison", "BE")


@pytest.mark.parametrize("strong_identity", [True, False])
async def test_concurrent_initial_snapshot_ingestion_is_one_listing(container, strong_identity):
    container.settings.provider_data_policies["rakuten"] = search_policy()
    data = market_offer("BE", "349").model_copy(update={"provider": "rakuten"})
    if not strong_identity:
        data = data.model_copy(
            update={key: None for key in ("brand", "model", "mpn", "gtin", "ean", "upc", "asin")}
        )
    container.registry.providers["rakuten"] = SnapshotProvider(data)
    results = await asyncio.gather(
        *(container.products.persist(data, mode=IngestionMode.SEARCH_SNAPSHOT) for _ in range(5))
    )
    assert len({r.id for r in results}) == len({r.product_id for r in results}) == 1
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 1
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 1
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 0
    with pytest.raises(DiscoveryMismatch):
        await container.products.persist(
            data.model_copy(update={"source_updated_at": utcnow() + timedelta(hours=1)}),
            mode=IngestionMode.SEARCH_SNAPSHOT,
        )


async def test_legacy_baseline_rebuild_finishes_for_quota_paused_watches(container):
    user = await container.users.telegram(777)
    views = [
        await container.products.persist(market_offer(country, price))
        for country, price in (("BE", "329"), ("DE", "299"), ("FR", "340"))
    ]
    async with container.sessions.begin() as session:
        for view in views:
            session.add(
                ProductWatch(
                    user_id=user.id,
                    product_id=view.product_id,
                    market_country=view.market_country,
                    currency="EUR",
                    best_price=1,
                    best_offer_id=views[0].id,
                    best_absence_reason="market_rebuild",
                )
            )
    # Free permits fewer active schedules than these retained legacy watches.
    watches = await container.watches.list(user.id)
    assert any(not w.scheduled for w in watches)
    assert await container.comparison_operations.maintain() == 3
    assert await container.comparison_operations.maintain() == 0
    async with container.sessions() as session:
        watches = (await session.scalars(select(ProductWatch))).all()
        assert {w.market_country: w.best_price for w in watches} == {
            "BE": 329,
            "DE": 299,
            "FR": 340,
        }
        assert all(w.best_absence_reason is None for w in watches)
        assert await session.scalar(select(func.count()).select_from(NotificationEvent)) == 0

"""M5B acceptance written before delivery implementation; no external credentials."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BestPriceEvent,
    NotificationEvent,
    PriceObservation,
    ProductWatch,
    Store,
)
from pricehunter.domain.products import ProductOfferData
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.schemas.api import UserSettingsPatch
from pricehunter.schemas.watches import WatchCreate
from tests.support import market_user

pytestmark = pytest.mark.integration


async def listing(container, slug, price, shipping=None, **changes):
    data = MockStoreProvider()._offer(slug).model_dump()
    data.update(
        country="BE",
        price=Decimal(price),
        shipping_price=Decimal(shipping) if shipping is not None else None,
        delivery_country="BE",
        delivery_scope="country",
        tax_status="included",
        delivery_availability="in_stock",
        delivery_quoted_at=utcnow(),
        delivery_expires_at=utcnow() + timedelta(minutes=10),
    )
    data.update(changes)
    return await container.products.persist(ProductOfferData.model_validate(data))


async def delivered(container, product_id, country="BE", postal_code=None):
    from pricehunter.domain.delivery import DeliveryContext

    user = await market_user(container, 55001, country="BE")
    return await container.products.comparisons.get(
        product_id,
        user.id,
        delivery_context=DeliveryContext(country=country, postal_code=postal_code),
    )


async def test_unknown_shipping_cannot_beat_complete_delivered_offer(container):
    a = await listing(container, "sony-a", "299")
    b = await listing(container, "sony-b", "309", "0")
    view = await delivered(container, a.product_id)
    assert view.best_available_offer.offer_id == a.id
    group = view.currency_groups[0]
    assert group.best_delivered_offer.offer_id == b.id
    assert group.best_delivered_offer.delivered_total == Decimal("309")
    assert next(o for o in view.offers if o.offer_id == a.id).delivered_total is None


async def test_shipping_flips_winner_without_redefining_item_best(container):
    a = await listing(container, "sony-a", "299", "25")
    b = await listing(container, "sony-b", "309", "0")
    view = await delivered(container, a.product_id)
    assert view.best_available_offer.offer_id == a.id
    assert view.currency_groups[0].best_delivered_offer.offer_id == b.id
    assert view.currency_groups[0].delivered_price_spread == Decimal("15")


async def test_free_shipping_zero_is_valid(container):
    a = await listing(container, "sony-a", "299", "0")
    view = await delivered(container, a.product_id)
    assert view.currency_groups[0].best_delivered_offer.delivered_total == Decimal("299")
    assert view.currency_groups[0].best_delivered_offer.shipping_price == Decimal("0")


async def test_unknown_tax_prevents_delivered_total(container):
    a = await listing(container, "sony-a", "299", "0", tax_status="unknown")
    view = await delivered(container, a.product_id)
    assert view.best_available_offer.offer_id == a.id
    assert view.currency_groups[0].best_delivered_offer is None
    assert view.offers[0].delivery_quote_status == "incomplete"


async def test_stale_quote_cannot_win_even_with_fresh_item(container):
    a = await listing(
        container,
        "sony-a",
        "299",
        "0",
        delivery_quoted_at=utcnow() - timedelta(hours=1),
        delivery_expires_at=utcnow() - timedelta(minutes=1),
    )
    view = await delivered(container, a.product_id)
    assert view.best_available_offer.offer_id == a.id
    assert view.currency_groups[0].best_delivered_offer is None
    assert view.offers[0].delivery_quote_status == "stale"


async def test_destination_mismatch_does_not_reuse_country_quote(container):
    a = await listing(container, "sony-a", "299", "0")
    view = await delivered(container, a.product_id, country="NL", postal_code="1012")
    assert view.best_available_offer.offer_id == a.id
    assert view.currency_groups[0].best_delivered_offer is None
    assert view.offers[0].delivered_total is None


async def test_destination_setting_change_preserves_existing_watch_and_history(container):
    a = await listing(container, "sony-a", "299", "25")
    user = await market_user(container, 55002, country="BE")
    await container.users.settings(user.id, UserSettingsPatch(delivery_country="BE"))
    watch = await container.watches.create(
        user.id, WatchCreate(product_id=a.product_id, currency="EUR", market_country="BE")
    )
    async with container.sessions() as session:
        original = (await session.get(ProductWatch, watch.id)).__dict__.copy()
        before = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (BestPriceEvent, PriceObservation, NotificationEvent)
        ]
    changed = await container.users.settings(
        user.id, UserSettingsPatch(delivery_country="NL", delivery_postal_code="1012")
    )
    assert changed.country_code == "BE"
    async with container.sessions() as session:
        current = await session.get(ProductWatch, watch.id)
        for field in ("market_country", "best_offer_id", "best_merchant_id", "best_price"):
            assert getattr(current, field) == original[field]
        assert before == [
            await session.scalar(select(func.count()).select_from(model))
            for model in (BestPriceEvent, PriceObservation, NotificationEvent)
        ]


async def test_same_merchant_can_choose_different_delivery_source(container):
    a = await listing(container, "sony-a", "299")
    b = await listing(container, "sony-b", "305", "0")
    async with container.sessions.begin() as session:
        from pricehunter.db.models import StoreOffer

        first = await session.get(StoreOffer, a.id)
        second = await session.get(StoreOffer, b.id)
        merchant_id = (await session.get(Store, first.store_id)).merchant_id
        await session.execute(
            update(Store).where(Store.id == second.store_id).values(merchant_id=merchant_id)
        )
    view = await delivered(container, a.product_id)
    assert view.store_count == 1
    assert view.best_available_offer.offer_id == a.id
    best = view.currency_groups[0].best_delivered_offer
    assert best.offer_id == b.id and best.delivered_total == Decimal("305")
    assert best.url == "https://mock.pricehunter.test/products/sony-b"
    assert len(view.delivered_offers) == 1

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import event, func, insert, select, update
from structlog.testing import capture_logs

from pricehunter.db.base import utcnow
from pricehunter.db.models import DeliveryQuote, PriceObservation, StoreOffer
from pricehunter.domain.delivery import DeliveryContext
from tests.integration.test_api import api_client
from tests.integration.test_delivery_acceptance import delivered, listing
from tests.support import market_user

pytestmark = pytest.mark.integration


async def dynamic(container):
    provider = container.registry.get("mock")
    provider.quote_delivery = AsyncMock(wraps=provider.quote_delivery)
    offers = [
        await container.products.persist(provider._offer(slug))
        for slug in ("delivery-alpha", "delivery-beta")
    ]
    user = await market_user(container, 56001, "BE")
    return provider, offers, user


async def count(container, model):
    async with container.sessions() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_explicit_quotes_cache_and_normal_reads_never_call_provider(container):
    provider, (a, b), user = await dynamic(container)
    context = DeliveryContext(country="BE", postal_code="2000")
    initial = await container.products.comparisons.get(
        a.product_id, user.id, delivery_context=context
    )
    assert initial.currency_groups[0].best_delivered_offer is None
    assert provider.quote_delivery.await_count == 0
    for _ in range(2):
        result = await container.delivery.request(a.product_id, user.id, context)
        assert result.best_available_offer.offer_id == a.id
        assert result.currency_groups[0].best_delivered_offer.offer_id == b.id
        assert result.currency_groups[0].best_delivered_offer.delivered_total == 309
    assert provider.quote_delivery.await_count == 2
    assert await count(container, DeliveryQuote) == 2
    assert await count(container, PriceObservation) == 2
    for page in (0, 1, 2):
        await container.products.comparisons.get(
            a.product_id, user.id, page=page, delivery_context=context
        )
    assert provider.quote_delivery.await_count == 2


async def test_quote_expiration_refresh_and_item_change_do_not_create_observations(container):
    provider, (a, _), user = await dynamic(container)
    context = DeliveryContext(country="BE", postal_code="2000")
    await container.delivery.request(a.product_id, user.id, context)
    async with container.sessions.begin() as session:
        await session.execute(
            update(DeliveryQuote).values(
                quoted_at=utcnow() - timedelta(minutes=10),
                expires_at=utcnow() - timedelta(seconds=1),
            )
        )
    stale = await container.products.comparisons.get(
        a.product_id, user.id, delivery_context=context
    )
    assert stale.best_available_offer.offer_id == a.id
    assert stale.currency_groups[0].best_delivered_offer is None
    assert all(o.delivery_quote_status == "stale" for o in stale.delivered_offers)
    fresh = await container.delivery.request(a.product_id, user.id, context)
    assert fresh.currency_groups[0].best_delivered_offer is not None
    assert provider.quote_delivery.await_count == 4
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer).where(StoreOffer.id == a.id).values(price=Decimal("250"))
        )
    changed = await container.products.comparisons.get(
        a.product_id, user.id, delivery_context=context
    )
    assert next(o for o in changed.offers if o.offer_id == a.id).delivered_total is None
    refresh = await container.delivery.request(a.product_id, user.id, context)
    assert refresh.currency_groups[0].best_delivered_offer.delivered_total == 275
    assert provider.quote_delivery.await_count == 5
    assert await count(container, PriceObservation) == 2


async def test_cross_border_and_exact_postal_cache_do_not_change_general_stock(container):
    provider, (a, b), user = await dynamic(container)
    await container.delivery.request(
        a.product_id, user.id, DeliveryContext(country="BE", postal_code="2000")
    )
    wrong = await container.products.comparisons.get(
        a.product_id, user.id, delivery_context=DeliveryContext(country="BE", postal_code="3500")
    )
    assert wrong.currency_groups[0].best_delivered_offer is None
    nl = await container.delivery.request(
        a.product_id, user.id, DeliveryContext(country="NL", postal_code="1012")
    )
    assert nl.market_country == "BE" and nl.best_available_offer.offer_id == a.id
    assert nl.currency_groups[0].best_delivered_offer.offer_id == b.id
    assert nl.currency_groups[0].best_delivered_offer.delivered_total == 317
    assert next(o for o in nl.offers if o.offer_id == a.id).delivery_quote_status == "unsupported"
    assert all(o.availability == "in_stock" for o in nl.offers)
    assert provider.quote_delivery.await_count == 4


@pytest.mark.parametrize(
    "kind",
    ["exception", "wrong_currency", "wrong_offer", "wrong_source", "unavailable", "unknown_tax"],
)
async def test_delivery_failure_is_local_and_provider_responses_are_private(container, kind):
    provider, (a, _), user = await dynamic(container)
    original = provider.quote_delivery
    private = "PRIVATE-POSTAL-9876"

    async def respond(ref, context):
        if kind == "exception":
            raise RuntimeError(private)
        quote = await original(ref, context)
        change = {
            "wrong_currency": {"currency": "GBP"},
            "wrong_offer": {"offer_id": uuid4()},
            "wrong_source": {"source": "another"},
            "unavailable": {"availability": "out_of_stock"},
            "unknown_tax": {"tax_status": "unknown"},
        }[kind]
        return quote.model_copy(update=change)

    provider.quote_delivery = respond
    with capture_logs() as logs:
        result = await container.delivery.request(
            a.product_id, user.id, DeliveryContext(country="BE", postal_code=private)
        )
    assert result.best_available_offer.offer_id == a.id
    assert result.currency_groups[0].best_delivered_offer is None
    expected = "incomplete" if kind in ("unavailable", "unknown_tax") else "failed"
    assert all(o.delivery_quote_status == expected for o in result.delivered_offers)
    assert all(o.availability == "in_stock" for o in result.offers)
    assert private not in json.dumps(logs)
    assert private not in result.model_dump_json()
    assert await count(container, PriceObservation) == 2


async def test_display_only_adapter_returns_request_scoped_results(container):
    provider, (a, b), user = await dynamic(container)
    provider.delivery_quote_cacheable = False
    result = await container.delivery.request(a.product_id, user.id, DeliveryContext(country="BE"))
    assert result.currency_groups[0].best_delivered_offer.offer_id == b.id
    assert await count(container, DeliveryQuote) == 0
    normal = await container.products.comparisons.get(
        a.product_id, user.id, delivery_context=DeliveryContext(country="BE")
    )
    assert normal.currency_groups[0].best_delivered_offer is None


async def test_dynamic_specific_quote_supersedes_static_without_mixing_evidence(container):
    provider, (a, _), user = await dynamic(container)
    async with container.sessions.begin() as session:
        offer = await session.get(StoreOffer, a.id)
        source = provider._offer("delivery-alpha").model_dump()
        from pricehunter.domain.products import ProductOfferData

        data = ProductOfferData.model_validate(
            source
            | {
                "shipping_price": Decimal("0"),
                "delivery_country": "BE",
                "delivery_scope": "country",
                "tax_status": "included",
                "delivery_availability": "in_stock",
                "delivery_quoted_at": utcnow() - timedelta(minutes=1),
                "delivery_expires_at": utcnow() + timedelta(minutes=5),
            }
        )
        for key, value in data.delivery_values().items():
            setattr(offer, key, value)
    before = await container.products.comparisons.get(
        a.product_id, user.id, delivery_context=DeliveryContext(country="BE", postal_code="2000")
    )
    assert before.currency_groups[0].best_delivered_offer.delivered_total == 299
    after = await container.delivery.request(
        a.product_id, user.id, DeliveryContext(country="BE", postal_code="2000")
    )
    assert next(o for o in after.offers if o.offer_id == a.id).delivered_total == 324
    assert next(o for o in after.offers if o.offer_id == a.id).shipping_price == 25


async def test_api_delivery_authorization_preferences_clearing_and_shared_budget(container):
    _, (a, _), _ = await dynamic(container)
    client = await api_client(container, 56002)
    async with client:
        endpoint = f"/api/v1/products/{a.product_id}/delivery-quote"
        assert (
            await client.post(endpoint, json={"country": "BE"}, headers={"Authorization": ""})
        ).status_code == 401
        assert (await client.post(endpoint)).status_code == 400
        assert (
            await client.patch("/api/v1/users/me/settings", json={"delivery_postal_code": "2000"})
        ).status_code == 400
        r = await client.patch(
            "/api/v1/users/me/settings",
            json={"delivery_country": "BE", "delivery_postal_code": " 2000 "},
        )
        assert r.status_code == 200 and r.json()["delivery_postal_code"] == "2000"
        assert (await client.get("/api/v1/users/me/settings")).json()["delivery_country"] == "BE"
        quote = await client.post(endpoint)
        assert (
            quote.status_code == 200
            and quote.json()["currency_groups"][0]["best_delivered_offer"] is not None
        )
        assert "2000" not in quote.text
        ordinary = await client.get(f"/api/v1/products/{a.product_id}")
        assert ordinary.json()["currency_groups"][0]["best_delivered_offer"] is None
        r = await client.patch("/api/v1/users/me/settings", json={"delivery_country": "NL"})
        assert r.json()["delivery_postal_code"] is None and r.json()["country_code"] == "BE"
        r = await client.patch("/api/v1/users/me/settings", json={"delivery_country": None})
        assert r.json()["delivery_country"] is None and r.json()["delivery_postal_code"] is None
        assert (
            await client.post(endpoint, json={"country": "BE", "postal_code": "2000"})
        ).status_code == 200
        assert (
            await client.post(
                endpoint.replace("quote", "comparison"),
                json={"country": "BE", "postal_code": "2000"},
            )
        ).status_code == 200
        container.settings.user_requests_per_minute = 1
        assert (await client.post(endpoint, json={"country": "BE"})).status_code == 429


@pytest.mark.parametrize("n", [100, 500, 1000])
async def test_delivery_query_and_global_call_bound_with_large_raw_catalog(container, n):
    provider, (a, _), user = await dynamic(container)
    # Many raw listings for one merchant must not cause N queries or N remote calls.
    async with container.sessions.begin() as session:
        original = await session.get(StoreOffer, a.id)
        columns = [c.name for c in StoreOffer.__table__.columns if c.name != "id"]
        await session.execute(
            insert(StoreOffer),
            [
                {
                    **{key: getattr(original, key) for key in columns},
                    "id": uuid4(),
                    "external_id": f"raw-{i}",
                }
                for i in range(n - 2)
            ],
        )
    calls = []
    engine = container.sessions.kw["bind"].sync_engine

    def listener(*args):
        calls.append(args[2])

    event.listen(engine, "before_cursor_execute", listener)
    try:
        result = await container.delivery.request(
            a.product_id, user.id, DeliveryContext(country="BE")
        )
        assert provider.quote_delivery.await_count <= container.settings.delivery_quote_limit
        assert result.store_count == 2
        assert len(calls) <= 30
        calls.clear()
        for page in (0, 1):
            await container.products.comparisons.get(
                a.product_id, user.id, page=page, delivery_context=DeliveryContext(country="BE")
            )
        assert len(calls) <= 30
    finally:
        event.remove(engine, "before_cursor_execute", listener)


async def test_commission_metadata_does_not_change_item_delivered_or_candidates(container):
    provider, (a, _), user = await dynamic(container)
    before = await container.delivery.request(a.product_id, user.id, DeliveryContext(country="BE"))
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer).values(
                affiliate_metadata={"commission": 999999, "payout": "preferred", "network": "z"}
            )
        )
    after = await container.products.comparisons.get(
        a.product_id, user.id, delivery_context=DeliveryContext(country="BE")
    )
    assert after.best_available_offer.offer_id == before.best_available_offer.offer_id
    assert (
        after.currency_groups[0].best_delivered_offer.offer_id
        == before.currency_groups[0].best_delivered_offer.offer_id
    )
    assert provider.quote_delivery.await_count == 2


async def test_native_currency_groups_are_never_numerically_ranked(container):
    a = await listing(container, "sony-a", "299", "25")
    await listing(container, "sony-b", "10", "0", currency="GBP")
    result = await delivered(container, a.product_id)
    assert result.best_available_offer is None
    assert {g.currency: g.best_delivered_offer.delivered_total for g in result.currency_groups} == {
        "EUR": 324,
        "GBP": 10,
    }


async def test_real_feed_sources_choose_separate_canonical_merchant_representatives(container):
    from pricehunter.domain.delivery import DeliveryEvidence, delivered_total
    from tests.integration.test_merchant_identity import retailers

    user, (a, b, _) = await retailers(container)
    now = utcnow()
    async with container.sessions.begin() as session:
        await session.execute(update(StoreOffer).where(StoreOffer.id == a.id).values(price=299))
        evidence = DeliveryEvidence(
            country="BE",
            scope="country",
            destination_key=DeliveryContext(country="BE").fingerprint,
            currency="EUR",
            shipping_price=Decimal("0"),
            tax_status="included",
            availability="in_stock",
            quoted_at=now,
            expires_at=now + timedelta(minutes=5),
            source="cj",
        )
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == b.id)
            .values(
                price=305,
                shipping_price=evidence.shipping_price,
                tax_status=evidence.tax_status,
                delivery_scope=evidence.scope,
                delivery_country="BE",
                delivery_currency="EUR",
                delivery_destination_key=evidence.destination_key,
                delivery_availability="in_stock",
                delivery_quoted_at=now,
                delivery_expires_at=evidence.expires_at,
                delivery_item_price=305,
                delivery_total=delivered_total(Decimal("305"), "EUR", evidence),
            )
        )
    result = await container.products.comparisons.get(
        a.product_id, user.id, delivery_context=DeliveryContext(country="BE")
    )
    assert result.best_available_offer.offer_id == a.id and result.best_available_offer.price == 299
    best = result.currency_groups[0].best_delivered_offer
    assert best.offer_id == b.id and best.price == best.delivered_total == 305
    assert best.store == "Coolblue" and "kqzyfj.com" in best.url
    assert [o.store for o in result.delivered_offers].count("Coolblue") == 1


async def test_unsupported_provider_never_gets_a_shipping_call(container):
    provider, (a, _), user = await dynamic(container)
    from pricehunter.domain.discovery import Capability

    provider.capabilities = provider.capabilities - {Capability.DELIVERY_QUOTE}
    result = await container.delivery.request(a.product_id, user.id, DeliveryContext(country="BE"))
    assert result.best_available_offer.offer_id == a.id
    assert provider.quote_delivery.await_count == 0


async def test_expired_quote_cleanup_is_bounded_and_preserves_item_history(container):
    _, (a, _), user = await dynamic(container)
    await container.delivery.request(a.product_id, user.id, DeliveryContext(country="BE"))
    async with container.sessions.begin() as session:
        await session.execute(
            update(DeliveryQuote).values(
                quoted_at=utcnow() - timedelta(minutes=2),
                expires_at=utcnow() - timedelta(seconds=1),
            )
        )
    container.settings.batch_size = 1
    assert await container.delivery.purge_expired() == 1
    assert await count(container, DeliveryQuote) == 1
    assert await count(container, PriceObservation) == 2


async def test_smaller_quote_ttl_does_not_let_old_exact_quote_hide_fresh_country_quote(container):
    _, (a, _), user = await dynamic(container)
    context = DeliveryContext(country="BE", postal_code="2000")
    await container.delivery.request(a.product_id, user.id, context)
    now = utcnow()
    async with container.sessions.begin() as session:
        old = await session.scalar(select(DeliveryQuote).where(DeliveryQuote.offer_id == a.id))
        old.quoted_at = now - timedelta(minutes=2)
        old.expires_at = now + timedelta(minutes=5)
        values = {c.name: getattr(old, c.name) for c in DeliveryQuote.__table__.columns}
        values.update(
            id=uuid4(),
            destination_key=context.country_key,
            scope="country",
            quoted_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        await session.execute(insert(DeliveryQuote).values(values))
    container.settings.delivery_quote_ttl_seconds = 30
    result = await container.products.comparisons.get(
        a.product_id, user.id, delivery_context=context
    )
    alpha = next(o for o in result.offers if o.offer_id == a.id)
    assert alpha.delivery_quote_status == "complete" and alpha.delivered_total == 324


async def test_total_quote_timeout_leaves_item_prices_intact_and_no_open_db_transaction(container):
    provider, (a, _), user = await dynamic(container)
    never = asyncio.Event()
    checkouts = []

    async def hang(ref, context):
        checkouts.append(container.sessions.kw["bind"].sync_engine.pool.checkedout())
        await never.wait()

    provider.quote_delivery = hang
    container.settings.delivery_operation_timeout_seconds = 1
    result = await container.delivery.request(a.product_id, user.id, DeliveryContext(country="BE"))
    assert result.best_available_offer.offer_id == a.id
    assert all(o.delivery_quote_status == "failed" for o in result.delivered_offers)
    assert checkouts and all(value == 0 for value in checkouts)
    assert await count(container, PriceObservation) == 2


async def test_failed_dynamic_quote_does_not_erase_current_static_authoritative_evidence(container):
    provider, (a, _), user = await dynamic(container)
    now = utcnow()
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == a.id)
            .values(
                shipping_price=Decimal("0"),
                delivery_country="BE",
                delivery_scope="country",
                delivery_destination_key=DeliveryContext(country="BE").fingerprint,
                delivery_currency="EUR",
                delivery_availability="in_stock",
                tax_status="included",
                delivery_quoted_at=now,
                delivery_expires_at=now + timedelta(minutes=5),
                delivery_item_price=299,
                delivery_total=299,
            )
        )
    provider.quote_delivery = AsyncMock(side_effect=RuntimeError("private provider error"))
    result = await container.delivery.request(
        a.product_id, user.id, DeliveryContext(country="BE", postal_code="2000")
    )
    assert result.currency_groups[0].best_delivered_offer.offer_id == a.id
    assert result.currency_groups[0].best_delivered_offer.delivered_total == 299

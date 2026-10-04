"""Merchant acceptance uses real feed policies, persistence and PostgreSQL readers."""

import json
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, func, insert, select, update

from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BestPriceEvent,
    MerchantProgram,
    NotificationEvent,
    OutboundClick,
    ProductBestState,
    ProductWatch,
    Store,
    StoreOffer,
)
from pricehunter.domain.feeds import MerchantProgramInput
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY
from pricehunter.providers.feeds.local import FeedStoreProvider
from pricehunter.schemas.watches import WatchCreate
from pricehunter.services.comparison_service import ComparisonReader
from tests.integration.test_commerce_feeds import row, setup_feeds, source
from tests.integration.test_comparison import persist
from tests.support import market_user

pytestmark = pytest.mark.integration


async def test_same_price_source_switch_updates_provenance_without_history_or_alert(container):
    user, (a, b, _) = await retailers(container)
    watch = await container.watches.create(
        user.id, WatchCreate(product_id=a.product_id, currency="EUR")
    )
    async with container.sessions.begin() as session:
        before = await session.scalar(select(func.count()).select_from(BestPriceEvent))
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == a.id)
            .values(last_checked_at=utcnow() - timedelta(seconds=10))
        )
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == b.id)
            .values(price=329, last_checked_at=utcnow())
        )
        await container.watches.evaluate(session, a.product_id, market_country="BE")
        await container.watches.evaluate(session, a.product_id, market_country="BE")
        state = (await session.scalars(select(ProductBestState))).one()
        stored = await session.get(ProductWatch, watch.id)
        assert stored.best_offer_id == state.store_offer_id == b.id
        assert stored.best_merchant_id == state.merchant_id
        assert await session.scalar(select(func.count()).select_from(BestPriceEvent)) == before
        assert await session.scalar(select(func.count()).select_from(NotificationEvent)) == 0


@pytest.mark.parametrize("availability", ["unknown", "out_of_stock"])
async def test_available_source_wins_over_cheaper_same_merchant_source(container, availability):
    user, (a, b, _) = await retailers(container)
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == a.id)
            .values(price=299, availability=availability)
        )
    product = await container.products.product(a.product_id, user.id)
    assert product.best_available_offer.offer_id == b.id
    assert product.best_available_offer.availability == "in_stock"


async def test_representative_url_and_click_attribution_never_borrow_another_source(container):
    from pydantic import SecretStr

    user, (a, b, _) = await retailers(container)
    container.settings.public_base_url = "https://pricehunter.example.com"
    container.settings.redirect_signing_secret = SecretStr(
        "synthetic-signing-secret-with-at-least-32-characters"
    )
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == b.id)
            .values(affiliate_metadata={"commission": 999999, "payout": "preferred"})
        )
    product = await container.products.product(a.product_id, user.id)
    best = product.best_available_offer
    assert best.offer_id == a.id and best.price == 329
    destination = await container.outbound.redirect(container.sessions, best.url.rsplit("/", 1)[-1])
    assert "awin1.com" in destination
    async with container.sessions.begin() as session:
        click = (await session.scalars(select(OutboundClick))).one()
        selected = await session.get(StoreOffer, a.id)
        assert click.offer_id == a.id and click.store_id == selected.store_id
        assert click.merchant_id == best.merchant_id and click.affiliate_network == "awin"
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == a.id)
            .values(affiliate_url=None, direct_url=None)
        )
    product = await container.products.product(a.product_id, user.id)
    assert product.best_available_offer.offer_id == a.id
    assert product.best_available_offer.url is None


async def test_three_networks_linked_to_one_merchant_keep_raw_coverage_and_resolve_candidates(
    container,
):
    user, (a, _, c) = await retailers(container)
    async with container.sessions() as session:
        source_offer = await session.get(StoreOffer, c.id)
        store = await session.get(Store, source_offer.store_id)
        first_offer = await session.get(StoreOffer, a.id)
        target_store = await session.get(Store, first_offer.store_id)
    await container.merchants.reassign(
        store.id,
        target_store.merchant_id,
        expected_source_merchant_id=store.merchant_id,
        expected_source_version=1,
        expected_version=1,
        reason="Explicit three-network review",
        confirm=True,
    )
    # Materialize 100 snapshots through all three real network source identities,
    # preserving the original feed item/StoreOffer IDs instead of deleting rows.
    _, sync, _, base = await setup_feeds(container)
    container.settings.discovery_result_limit = 100
    container.registry.providers = {
        network: FeedStoreProvider(network, container.sessions, container.settings)
        for network in ("awin", "cj", "tradedoubler")
    }
    async with container.sessions() as session:
        programs = list(await session.scalars(select(MerchantProgram)))
        selected = {offer.store_id: offer for offer in await session.scalars(select(StoreOffer))}
    for program in programs:
        original = selected[program.store_id]
        count = 34 if program.network == "awin" else 33
        feed = source(
            base,
            [
                row(
                    external_id="sony" if i == 0 else f"sony-{i}",
                    price=str(original.price),
                    direct_url=original.direct_url,
                    affiliate_url=original.affiliate_url,
                )
                for i in range(count)
            ],
        )
        feed.name = program.network
        await sync.run(program.id, feed)
        provider = FeedStoreProvider(program.network, container.sessions, container.settings)
        for data in await provider.search("Sony WH-1000XM6", country="BE", program_id=program.id):
            materialized = await container.products.persist(data)
            assert materialized.product_id == a.product_id
    product = await container.products.product(a.product_id, user.id)
    assert product.offer_count == product.store_count == len(product.offers) == 1
    assert await container.coverage.duplicate_merchants() == []
    report = await container.coverage.report()
    assert report["merchant_coverage"]["BE"] == dict(
        canonical_merchants=1,
        source_stores=3,
        merchant_programs=3,
        raw_source_offers=100,
        effective_offers=1,
    )


async def retailers(container, *, policy_a=SYNTHETIC_POLICY):
    programs, sync, _, base = await setup_feeds(container)
    container.settings.cj_enabled = container.settings.tradedoubler_enabled = True
    offers = []
    for network, remote, price, name, policy in (
        ("awin", "101", "329", "Coolblue", policy_a),
        ("cj", "202", "335", "Coolblue", SYNTHETIC_POLICY),
        ("tradedoubler", "303", "339", "MediaMarkt", SYNTHETIC_POLICY),
    ):
        p = await programs.save(
            MerchantProgramInput(
                network=network,
                external_merchant_id=remote,
                market_country="BE",
                display_name=name,
                domain="example.com",
                currency="EUR",
                external_feed_id=remote,
            )
        )
        p = await programs.review(p.id, policy, expected_version=p.version, reason="Fixture review")
        p = await programs.activate(p.id, expected_version=p.version, reason="Fixture activation")
        affiliate = {
            "awin": "https://www.awin1.com/cread.php?source=awin",
            "cj": "https://www.kqzyfj.com/click-source-cj",
            "tradedoubler": "https://clk.tradedoubler.com/click?source=td",
        }[network]
        feed = source(
            base,
            [
                row(
                    price=price,
                    direct_url=f"https://example.com/{network}",
                    affiliate_url=affiliate,
                )
            ],
        )
        feed.name = network
        await sync.run(p.id, feed)
        provider = FeedStoreProvider(network, container.sessions, container.settings)
        data = (await provider.search("Sony WH-1000XM6", country="BE"))[0]
        offers.append(await container.products.persist(data))
    async with container.sessions.begin() as session:
        a = await session.get(StoreOffer, offers[0].id)
        b = await session.get(StoreOffer, offers[1].id)
        left = await session.get(Store, a.store_id)
        right = await session.get(Store, b.store_id)
        # Explicit fixture reconciliation, never name/domain inference. On the old
        # model this unmapped attribute does nothing, exposing the current bug.
        right.merchant_id = left.id
    user = await market_user(container, 790001, "BE")
    return user, offers


async def test_two_networks_linked_to_one_merchant_have_one_effective_offer(container):
    user, (a, _, _) = await retailers(container)
    product = await container.products.product(a.product_id, user.id)
    assert product.store_count == product.offer_count == 2
    assert [(o.store, o.price) for o in product.offers] == [
        ("Coolblue", Decimal("329")),
        ("MediaMarkt", Decimal("339")),
    ]
    assert product.price_spread == 10
    assert product.currency_groups[0].fresh_offer_count == 2
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 3


async def test_stale_cheaper_source_cannot_represent_fresh_merchant(container):
    user, (a, b, _) = await retailers(container)
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == a.id)
            .values(price=299, last_checked_at=utcnow() - timedelta(days=4))
        )
    product = await container.products.product(a.product_id, user.id)
    coolblue = [o for o in product.offers if o.store == "Coolblue"]
    assert len(coolblue) == 1 and coolblue[0].offer_id == b.id
    assert coolblue[0].price == 335 and not coolblue[0].stale
    assert product.currency_groups[0].stale_offer_count == 0


async def test_failed_cheaper_source_cannot_represent_fresh_merchant(container):
    user, (a, b, _) = await retailers(container)
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer).where(StoreOffer.id == a.id).values(price=299, failure_count=1)
        )
    product = await container.products.product(a.product_id, user.id)
    assert product.best_available_offer.offer_id == b.id
    assert product.currency_groups[0].failed_offer_count == 0


async def test_source_ties_use_confidence_time_and_uuid_not_source_slug(container):
    user, (a, b, _) = await retailers(container)
    now = utcnow()
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer).where(StoreOffer.id.in_([a.id, b.id])).values(price=329)
        )
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == a.id)
            .values(
                match_confidence="0.90",
                last_checked_at=now,
            )
        )
        await session.execute(
            update(StoreOffer)
            .where(StoreOffer.id == b.id)
            .values(
                match_confidence=1,
                last_checked_at=now - timedelta(seconds=10),
            )
        )
    assert (
        await container.products.product(a.product_id, user.id)
    ).best_available_offer.offer_id == b.id
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer).where(StoreOffer.id == a.id).values(match_confidence=1)
        )
    assert (
        await container.products.product(a.product_id, user.id)
    ).best_available_offer.offer_id == a.id
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer).where(StoreOffer.id == b.id).values(last_checked_at=now)
        )
        offers = list(
            await session.scalars(select(StoreOffer).where(StoreOffer.id.in_([a.id, b.id])))
        )
        expected = min(offers, key=lambda o: (o.store_id, o.id))
        for offer in offers:
            store = await session.get(Store, offer.store_id)
            store.slug = "z-source" if offer.id == expected.id else "a-source"
    assert (
        await container.products.product(a.product_id, user.id)
    ).best_available_offer.offer_id == expected.id


async def test_canonical_disable_hides_feed_sources_without_changing_contracts(container):
    user, (a, _, c) = await retailers(container)
    async with container.sessions() as session:
        before = {
            p.id: (p.version, p.active, p.policy_data)
            for p in await session.scalars(select(MerchantProgram))
        }
        selected = await session.get(StoreOffer, a.id)
        store = await session.get(Store, selected.store_id)
    await container.merchants.change(
        store.merchant_id,
        expected_version=1,
        reason="Retailer suspended",
        changes={"active": False},
        confirm=True,
    )
    product = await container.products.product(a.product_id, user.id)
    assert product.store_count == product.offer_count == 1 and product.offers[0].offer_id == c.id
    for network in ("awin", "cj"):
        provider = FeedStoreProvider(network, container.sessions, container.settings)
        assert await provider.search("Sony WH-1000XM6", country="BE") == []
    async with container.sessions() as session:
        assert before == {
            p.id: (p.version, p.active, p.policy_data)
            for p in await session.scalars(select(MerchantProgram))
        }
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 3


@pytest.mark.parametrize("same_merchant", [True, False])
async def test_source_switch_is_not_a_merchant_switch(container, same_merchant):
    user, (a, b, c) = await retailers(container)
    watch = await container.watches.create(
        user.id, WatchCreate(product_id=a.product_id, currency="EUR")
    )
    selected = b if same_merchant else c
    async with container.sessions.begin() as session:
        await session.execute(
            update(StoreOffer).where(StoreOffer.id == selected.id).values(price=319)
        )
        await container.watches.evaluate(session, a.product_id, market_country="BE")
        stored = await session.get(ProductWatch, watch.id)
        assert stored.best_offer_id == selected.id and stored.best_price == 319
        types = list(await session.scalars(select(NotificationEvent.event_type)))
        assert types == ["new_best_price" if same_merchant else "merchant_became_cheapest"]
        latest = (
            await session.scalars(select(BestPriceEvent).order_by(BestPriceEvent.sequence.desc()))
        ).first()
        assert latest.event_type == ("price_changed" if same_merchant else "merchant_changed")
        assert latest.merchant_id == stored.best_merchant_id
        snapshot = (await session.scalars(select(NotificationEvent.snapshot))).one()
        assert snapshot["merchant_id"] == str(stored.best_merchant_id)
        assert snapshot["offer_id"] == str(selected.id)


async def test_merchant_selection_keeps_source_tracking_policy_independent(container):
    catalog_only = SYNTHETIC_POLICY.model_copy(update={"tracking_allowed": False})
    user, (a, b, _) = await retailers(container, policy_a=catalog_only)
    async with container.sessions.begin() as session:
        await session.execute(update(StoreOffer).where(StoreOffer.id == a.id).values(price=299))
    catalog = await container.products.product(a.product_id, user.id)
    assert catalog.offer_count == 2 and catalog.best_available_offer.offer_id == a.id
    async with container.sessions() as session:
        tracking = await ComparisonReader(container.settings, "tracking_allowed", "BE").summary(
            session, a.product_id, utcnow(), "EUR"
        )
        assert tracking.groups[0].best_available_offer.offer_id == b.id
    watch = await container.watches.create(
        user.id, WatchCreate(product_id=a.product_id, currency="EUR")
    )
    assert watch.best_offer_id == b.id and watch.best_price == 335


@pytest.mark.parametrize("raw_count", [100, 500, 1000])
async def test_duplicate_source_load_uses_bounded_queries_and_effective_pages(container, raw_count):
    a = await persist(container, "sony-a")
    now = utcnow()
    async with container.sessions.begin() as session:
        original = await session.get(StoreOffer, a.id)
        original.price, original.last_checked_at = Decimal(100), now
        stores = [await session.get(Store, original.store_id)]
        for i in range(1, raw_count):
            s = Store(
                id=uuid4(),
                slug=f"load-{i}",
                name=f"Merchant {i // 5}",
                domain="example.com",
                provider_type="mock",
                country="DE",
            )
            session.add(s)
            stores.append(s)
        await session.flush()
        for i, s in enumerate(stores):
            s.merchant_id = stores[i - i % 5].id
        await session.execute(
            insert(StoreOffer),
            [
                dict(
                    product_id=a.product_id,
                    store_id=s.id,
                    external_id=f"raw-{i}",
                    market_country="DE",
                    url="https://example.com/item",
                    direct_url="https://example.com/item",
                    title="Load fixture",
                    price=100 + i,
                    currency="EUR",
                    availability="in_stock",
                    minimum_price=100 + i,
                    maximum_price=100 + i,
                    total_price=100 + i,
                    last_checked_at=now,
                )
                for i, s in enumerate(stores)
                if i
            ],
        )
    statements = []
    aggregate = []
    engine = container.sessions.kw["bind"].sync_engine

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)
        if "count(distinct(stores.merchant_id))" in statement:
            aggregate.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        async with container.sessions() as session:
            reader = container.products.comparisons
            first = await reader.build(session, a.product_id, market_country="DE", size=10)
            assert len(statements) <= 6
            assert first.store_count == first.offer_count == raw_count // 5
            assert len(first.offers) == 10
            second = await reader.build(session, a.product_id, market_country="DE", page=1, size=10)
            assert len(statements) <= 12 and len(second.offers) == 10
            assert not {o.merchant_id for o in first.offers} & {
                o.merchant_id for o in second.offers
            }
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    # Check work as well as round trips: stale planner statistics must not cause
    # a semijoin to evaluate the whole representative window per outer offer.
    async with container.sessions.kw["bind"].connect() as connection:
        statement, parameters = aggregate[0]
        plan = (
            await connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + statement,
                parameters,
            )
        ).scalar_one()
        if isinstance(plan, str):
            plan = json.loads(plan)
        windows = []

        def visit(node):
            if node["Node Type"] == "WindowAgg":
                windows.append(node["Actual Loops"])
            for child in node.get("Plans", []):
                visit(child)

        visit(plan[0]["Plan"])
        assert windows == [1]

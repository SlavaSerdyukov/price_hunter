import argparse
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import DBAPIError

from pricehunter.apps import merchant_admin
from pricehunter.bot.comparison import show_offers
from pricehunter.db.models import (
    Merchant,
    MerchantAudit,
    Product,
    ProductWatch,
    Store,
    StoreOffer,
    Tracker,
)
from pricehunter.domain.errors import ProductNotFoundError
from pricehunter.domain.merchants import MerchantInput
from pricehunter.providers.mock import MockStoreProvider
from pricehunter.schemas.api import TrackerCreate
from pricehunter.schemas.watches import WatchCreate
from tests.integration.test_comparison import event_types, persist
from tests.support import market_user

pytestmark = pytest.mark.integration


async def source_pair(container):
    a, b = await persist(container, "sony-a"), await persist(container, "sony-b")
    async with container.sessions() as session:
        offers = [await session.get(StoreOffer, x.id) for x in (a, b)]
        stores = [await session.get(Store, o.store_id) for o in offers]
    return a, b, stores


async def link(container, stores, *, dry_run=False):
    target = await container.merchants.get(stores[0].merchant_id)
    previous = await container.merchants.get(stores[1].merchant_id)
    return await container.merchants.reassign(
        stores[1].id,
        target.id,
        expected_source_merchant_id=previous.id,
        expected_source_version=previous.version,
        expected_version=target.version,
        reason="Explicit synthetic retailer reconciliation",
        dry_run=dry_run,
        confirm=not dry_run,
    )


async def test_same_names_domains_never_auto_link_and_repeated_creation_has_no_orphans(container):
    original = MockStoreProvider()._offer("sony-a")
    second = original.model_copy(
        update={
            "external_id": "unrelated",
            "title": "Unrelated item",
            "brand": None,
            "model": None,
            "gtin": None,
        }
    )
    await asyncio.gather(container.products.persist(original), container.products.persist(second))
    await container.products.persist(original)
    await container.products.persist(
        original.model_copy(update={"store_slug": "other-acquisition"})
    )
    async with container.sessions() as session:
        stores = list(await session.scalars(select(Store)))
        assert len(stores) == 2 and len({s.merchant_id for s in stores}) == 2
        assert await session.scalar(select(func.count()).select_from(Merchant)) == 2
        assert await session.scalar(select(func.count()).select_from(MerchantAudit)) == 2
        assert all(s.merchant_id == s.id and s.merchant.display_name == s.name for s in stores)


async def test_preview_dry_run_confirm_versions_and_both_sides_audited(container):
    a, b, stores = await source_pair(container)
    before = await container.merchants.list_merchants()
    preview = await container.merchants.preview(stores[1].id, stores[0].merchant_id)
    assert preview["raw_source_offers"] == 1 and preview["automatic_linking"] is False
    await link(container, stores, dry_run=True)
    assert await container.merchants.list_merchants() == before
    target = await link(container, stores)
    assert target.version == 2
    source = await container.merchants.get(stores[1].merchant_id)
    assert source.version == 2
    assert (await container.merchants.history(target.id))[0]["action"] == "source_linked"
    assert (await container.merchants.history(source.id))[0]["action"] == "source_unlinked"
    assert (await container.merchants.show(target.id))["sources"] and (
        await container.merchants.show(source.id)
    )["sources"] == []
    with pytest.raises(ValueError, match="version_conflict"):
        await container.merchants.change(
            target.id,
            expected_version=1,
            reason="Stale metadata",
            changes={"display_name": "Wrong"},
            confirm=True,
        )
    with pytest.raises(ValueError, match="version_conflict"):
        await container.merchants.reassign(
            stores[1].id,
            target.id,
            expected_source_merchant_id=source.id,
            expected_source_version=2,
            expected_version=2,
            reason="Stale source assignment",
            confirm=True,
        )
    async with container.sessions() as session:
        assert await session.get(StoreOffer, a.id) and await session.get(StoreOffer, b.id)


async def test_unlink_creates_distinct_safe_identity_without_touching_exact_tracker(container):
    a, b, stores = await source_pair(container)
    user = await market_user(container, 791001, "DE")
    tracker = await container.trackers.create(user.id, TrackerCreate(store_offer_id=b.id))
    linked = await link(container, stores)
    detached = await container.merchants.reassign(
        stores[1].id,
        None,
        expected_source_merchant_id=linked.id,
        expected_source_version=linked.version,
        expected_version=0,
        reason="Separate legal retailer confirmed",
        confirm=True,
    )
    assert detached.id not in {s.merchant_id for s in stores} and detached.active
    async with container.sessions() as session:
        source = await session.get(Store, stores[1].id)
        exact = await session.get(Tracker, tracker.id)
        assert source.merchant_id == detached.id and exact.store_offer_id == b.id
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 2
    assert (await container.products.product(a.product_id, user.id)).store_count == 2


async def test_disable_reenable_is_reversible_preserves_sources_and_blocks_customer_links(
    container,
):
    a, b, stores = await source_pair(container)
    user = await market_user(container, 791002, "DE")
    tracker = await container.trackers.create(user.id, TrackerCreate(store_offer_id=a.id))
    target = await link(container, stores)
    disabled = await container.merchants.change(
        target.id,
        expected_version=target.version,
        reason="Retailer suspended",
        changes={"active": False},
        confirm=True,
    )
    assert (await container.products.product(a.product_id, user.id)).offers == []
    with pytest.raises(ProductNotFoundError):
        await container.products.offer(a.id)
    assert not (await container.trackers.get(user.id, tracker.id)).scheduled
    with pytest.raises(ProductNotFoundError):
        await container.products.resolve("https://mock.pricehunter.test/products/sony-a", user.id)
    restored = await container.merchants.change(
        target.id,
        expected_version=disabled.version,
        reason="Retailer restored",
        changes={"active": True},
        confirm=True,
    )
    assert (
        restored.active
        and (await container.products.product(a.product_id, user.id)).store_count == 1
    )
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Store)) == 2
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 2
    assert await event_types(container) == []


async def test_concurrent_operator_receipts_allow_only_one_change(container):
    _, _, stores = await source_pair(container)
    outcomes = await asyncio.gather(
        link(container, stores), link(container, stores), return_exceptions=True
    )
    # Both calls read their receipts before mutation. Only the first receipt can commit.
    assert len([o for o in outcomes if isinstance(o, Merchant)]) == 1
    assert len([o for o in outcomes if isinstance(o, ValueError)]) == 1
    assert (await container.merchants.get(stores[0].merchant_id)).version == 2


async def test_link_waits_for_inflight_product_evaluation_and_rebases_current_identity(container):
    a, b, stores = await source_pair(container)
    user = await market_user(container, 791005, "DE")
    watch = await container.watches.create(
        user.id, WatchCreate(product_id=a.product_id, currency="EUR")
    )
    started = asyncio.Event()
    engine = container.sessions.kw["bind"].sync_engine

    def capture(connection, cursor, statement, parameters, context, many):
        if statement.startswith("SELECT products.id") and "FOR UPDATE OF products" in statement:
            started.set()

    async with container.sessions.begin() as session:
        await session.get(Product, a.product_id, with_for_update=True)
        event.listen(engine, "before_cursor_execute", capture)
        task = asyncio.create_task(link(container, stores))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            assert not task.done()
            await session.execute(update(StoreOffer).where(StoreOffer.id == b.id).values(price=319))
            await container.watches.evaluate(session, a.product_id, market_country="DE")
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    target = await task
    assert await event_types(container) == ["merchant_became_cheapest"]
    async with container.sessions.begin() as session:
        await session.get(Product, a.product_id, with_for_update=True)
        await container.watches.evaluate(session, a.product_id, market_country="DE")
        current = await session.get(ProductWatch, watch.id)
        assert current.best_offer_id == b.id and current.best_merchant_id == target.id
    assert await event_types(container) == ["merchant_became_cheapest"]


async def test_concurrent_same_explicit_slug_creates_one_audited_identity(container):
    data = MerchantInput(slug="same-retailer", display_name="Retailer")
    outcomes = await asyncio.gather(
        *(
            container.merchants.create(data, expected_version=0, reason="Review", confirm=True)
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert len([o for o in outcomes if isinstance(o, Merchant)]) == 1
    assert len([o for o in outcomes if isinstance(o, ValueError)]) == 1
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MerchantAudit)) == 1


async def test_operator_rejects_unknown_and_disabled_target_without_source_changes(container):
    _, _, stores = await source_pair(container)
    with pytest.raises(ValueError, match="Unknown"):
        await container.merchants.get(uuid4())
    with pytest.raises(ValueError, match="Unknown"):
        await container.merchants.preview(uuid4(), stores[0].merchant_id)
    await container.merchants.change(
        stores[0].merchant_id,
        expected_version=1,
        reason="Suspended",
        changes={"active": False},
        confirm=True,
    )
    with pytest.raises(ValueError, match="active"):
        await link(container, stores)
    with pytest.raises(ValueError, match="separately"):
        await container.merchants.change(
            stores[0].merchant_id,
            expected_version=2,
            reason="Combined",
            changes={"active": True, "display_name": "Changed"},
            confirm=True,
        )
    assert (await container.merchants.get(stores[1].merchant_id)).version == 1


async def test_linked_sources_keep_market_currency_and_product_variant_boundaries(container):
    a, _, stores = await source_pair(container)
    await link(container, stores)
    provider = MockStoreProvider()
    for data in (
        provider._offer("sony-b").model_copy(
            update={
                "external_id": "be-listing",
                "country": "BE",
                "price": 20,
            }
        ),
        provider._offer("sony-b").model_copy(
            update={
                "external_id": "usd-listing",
                "currency": "USD",
                "price": 1,
            }
        ),
    ):
        assert (await container.products.persist(data)).product_id == a.product_id
    white = await container.products.persist(provider._offer("sony-white"))
    assert white.product_id != a.product_id
    user = await market_user(container, 791003, "DE")
    de = await container.products.product(a.product_id, user.id)
    assert de.store_count == 1 and de.offer_count == 2
    assert {g.currency: g.best_available_offer.price for g in de.currency_groups} == {
        "EUR": 329,
        "USD": 1,
    }
    assert all(o.store_country == "DE" for o in de.offers)
    be = await container.products.product(a.product_id, user.id, market_country="BE")
    assert be.store_count == be.offer_count == 1 and be.best_available_offer.price == 20
    assert be.offers[0].merchant_id == de.offers[0].merchant_id
    assert (await container.products.product(white.product_id, user.id)).offer_count == 1


async def test_telegram_offer_page_has_one_canonical_row_and_selected_source_link(container):
    a, _, stores = await source_pair(container)
    await link(container, stores)
    user = await market_user(container, 791004, "DE")
    message = SimpleNamespace(answer=AsyncMock())
    await show_offers(message, container, user, a.product_id, 0)
    content = "\n".join(c.args[0] for c in message.answer.await_args_list)
    assert content.count("Demo Alpha") == 1 and "Demo Beta" not in content
    keyboard = message.answer.await_args.kwargs["reply_markup"].inline_keyboard
    urls = [button.url for buttons in keyboard for button in buttons if button.url]
    assert urls == ["https://mock.pricehunter.test/products/sony-a"]


async def test_explicit_create_metadata_stable_slug_and_history(container):
    data = MerchantInput(
        slug="explicit-retailer", display_name="Retailer", primary_domain="example.com"
    )
    trial = await container.merchants.create(
        data, expected_version=0, reason="Review", dry_run=True
    )
    assert trial.slug == data.slug and await container.merchants.list_merchants() == []
    created = await container.merchants.create(
        data, expected_version=0, reason="Review", confirm=True
    )
    with pytest.raises(ValueError, match="slug_conflict"):
        await container.merchants.create(data, expected_version=0, reason="Duplicate", confirm=True)
    await container.merchants.change(
        created.id,
        expected_version=1,
        reason="Preview",
        changes={"display_name": "Updated"},
        dry_run=True,
    )
    assert (await container.merchants.get(created.id)).display_name == "Retailer"
    changed = await container.merchants.change(
        created.id,
        expected_version=1,
        reason="Reviewed name",
        changes={"display_name": "Updated", "primary_domain": None},
        confirm=True,
    )
    assert changed.slug == data.slug and changed.version == 2
    assert [a["action"] for a in await container.merchants.history(created.id)] == [
        "metadata_changed",
        "created",
    ]
    unchanged = await container.merchants.change(
        created.id,
        expected_version=2,
        reason="Already correct",
        changes={"display_name": "Updated"},
        confirm=True,
    )
    assert unchanged.version == 2


@pytest.mark.parametrize(
    "statement", ["UPDATE merchant_audits SET reason='tamper'", "DELETE FROM merchant_audits"]
)
async def test_merchant_audit_is_append_only(container, statement):
    await persist(container, "sony-a")
    with pytest.raises(DBAPIError, match="append-only"):
        async with container.sessions.begin() as session:
            await session.execute(text(statement))


async def test_source_cannot_be_left_without_identity(container):
    await persist(container, "sony-a")
    with pytest.raises(DBAPIError):
        async with container.sessions.begin() as session:
            await session.execute(text("UPDATE stores SET merchant_id=NULL"))


@pytest.mark.parametrize(
    "options,error",
    [
        ({}, "confirmation"),
        ({"dry_run": True, "confirm": True}, "Choose"),
        ({"reason": " ", "confirm": True}, "reason"),
        ({"expected_version": 4, "confirm": True}, "version_conflict"),
    ],
)
async def test_operator_create_fails_closed(container, options, error):
    kwargs = dict(expected_version=0, reason="Reviewed") | options
    with pytest.raises(ValueError, match=error):
        await container.merchants.create(
            MerchantInput(slug="retailer", display_name="Retailer"), **kwargs
        )
    assert await container.merchants.list_merchants() == []


async def test_operator_cli_read_and_metadata_commands(container, tmp_path, capsys):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    merchant_admin.add_commands(commands)
    document = tmp_path / "merchant.json"
    document.write_text(json.dumps(dict(slug="cli-retailer", display_name="CLI retailer")))

    async def run(*tokens):
        await merchant_admin.run(container, parser.parse_args(tokens))
        output = capsys.readouterr().out
        assert output

    await run(
        "merchant-create",
        str(document),
        "--expected-version",
        "0",
        "--reason",
        "Review",
        "--dry-run",
    )
    await run(
        "merchant-create",
        str(document),
        "--expected-version",
        "0",
        "--reason",
        "Review",
        "--confirm",
    )
    retailer = (await container.merchants.list_merchants())[0]
    identifier = str(retailer["id"])
    for command in ("merchant-show", "merchant-history"):
        await run(command, identifier)
    await run("merchant-list")
    await run("merchant-duplicate-candidates")
    for command, version, fields in (
        ("merchant-metadata", "1", ("--display-name", "Updated")),
        ("merchant-disable", "2", ()),
        ("merchant-enable", "3", ()),
    ):
        await run(
            command,
            identifier,
            "--expected-version",
            version,
            "--reason",
            "Reviewed",
            "--confirm",
            *fields,
        )
    _, _, stores = await source_pair(container)
    source_id = str(stores[1].id)
    await run("merchant-link-preview", source_id, identifier)
    await run(
        "merchant-source-link",
        source_id,
        identifier,
        "--expected-version",
        "4",
        "--expected-source-merchant-id",
        str(stores[1].merchant_id),
        "--expected-source-version",
        "1",
        "--reason",
        "Reviewed",
        "--confirm",
    )
    await run(
        "merchant-source-unlink",
        source_id,
        "--expected-version",
        "5",
        "--expected-source-merchant-id",
        identifier,
        "--reason",
        "Reviewed",
        "--confirm",
    )

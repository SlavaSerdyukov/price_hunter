from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from pricehunter.db.base import utcnow
from pricehunter.db.models import FeedSyncState, Product, Store, StoreOffer
from pricehunter.domain.feeds import MerchantProgramInput
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY
from pricehunter.providers.feeds.local import FeedStoreProvider
from pricehunter.providers.mock import MockStoreProvider
from tests.integration.test_commerce_feeds import program, row, setup_feeds, source
from tests.support import market_user

pytestmark = pytest.mark.integration


async def test_stale_operator_write_cannot_replace_review(container):
    programs, _, _, _ = await setup_feeds(container)
    p = await program(programs, "401")
    old_version = p.version
    changed = await programs.change_policy(
        p.id,
        SYNTHETIC_POLICY.model_copy(update={"tracking_allowed": False}),
        expected_version=old_version,
        reason="Tracking grant revoked",
    )
    with pytest.raises(ValueError, match="version_conflict"):
        await programs.change_policy(
            p.id, SYNTHETIC_POLICY, expected_version=old_version, reason="Stale review"
        )
    stored = await programs.get(p.id)
    assert stored.version == changed.version == old_version + 1
    assert not stored.policy_data["tracking_allowed"]


async def test_malformed_item_does_not_make_successful_network_unavailable(container, monkeypatch):
    valid = MockStoreProvider()._offer("sony-a")

    class Results(MockStoreProvider):
        async def search(self, *args, **kwargs):
            return [valid.model_copy(update={"external_id": str(i)}) for i in range(11)]

    container.registry.providers = {"mock": Results()}
    original = container.products.persist

    async def persist(data, **kwargs):
        if data.external_id == "10":
            raise ValueError("malformed fixture")
        return await original(data, **kwargs)

    monkeypatch.setattr(container.products, "persist", persist)
    user = await market_user(container, 780001, "DE")
    result = await container.search.search("Sony WH-1000XM6", user.id)
    assert result.unavailable_providers == []
    assert len(result.products) == 1
    outcome = result.provider_outcomes[0]
    assert outcome.status == "PARTIAL"
    assert (outcome.accepted_count, outcome.rejected_count) == (10, 1)
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 10


async def test_four_network_same_gtin_preserves_market_and_best_price(container):
    programs, sync, _, base = await setup_feeds(container)
    container.settings.tradedoubler_enabled = container.settings.cj_enabled = True
    container.settings.provider_data_policies["ebay"] = SYNTHETIC_POLICY

    class Direct(MockStoreProvider):
        name = "ebay"

        async def search(self, *args, country=None, **kwargs):
            if country != "BE":
                return []
            return [
                row(price="339", metadata={"commission": 999999, "payout": "preferred"}).offer(
                    program_id=None,
                    network="ebay",
                    merchant_id="77",
                    name="Direct merchant",
                    domain="example.com",
                    market="BE",
                )
            ]

    container.registry.providers = {"ebay": Direct()}
    for network in ("awin", "tradedoubler", "cj"):
        container.registry.providers[network] = FeedStoreProvider(
            network, container.sessions, container.settings
        )
    for network, merchant, country, price in (
        ("awin", "1", "BE", "329"),
        ("tradedoubler", "2", "BE", "325"),
        ("cj", "3", "BE", "319"),
        ("cj", "3", "DE", "299"),
    ):
        p = await programs.save(
            MerchantProgramInput(
                network=network,
                external_merchant_id=merchant,
                market_country=country,
                display_name=f"Merchant {merchant}",
                domain="example.com",
                currency="EUR",
                external_feed_id=merchant,
            )
        )
        p = await programs.review(
            p.id, SYNTHETIC_POLICY, expected_version=p.version, reason="Synthetic review"
        )
        p = await programs.activate(p.id, expected_version=p.version, reason="Synthetic activation")
        feed = source(
            base,
            [
                row(
                    price=price,
                    metadata={
                        "commission": 1000 if network == "awin" else 0,
                        "affiliate_program": network + "-fixture",
                    },
                )
            ],
        )
        feed.name = network
        await sync.run(p.id, feed)
    user = await market_user(container, 780002, "BE")
    be = await container.search.search("Sony WH-1000XM6", user.id, country="BE")
    de = await container.search.search("Sony WH-1000XM6", user.id, country="DE")
    assert be.products[0].id == de.products[0].id
    assert be.products[0].currency_groups[0].best_available_offer.price == Decimal("319")
    assert de.products[0].currency_groups[0].best_available_offer.price == Decimal("299")
    from pricehunter.db.models import ProductWatch
    from pricehunter.schemas.watches import WatchCreate

    watch = await container.watches.create(
        user.id, WatchCreate(product_id=be.products[0].id, market_country="BE", currency="EUR")
    )
    async with container.sessions.begin() as session:
        await container.watches.evaluate(session, be.products[0].id, market_country="BE")
        stored = await session.get(ProductWatch, watch.id)
        assert stored.best_price == Decimal("319")
        best = await session.get(StoreOffer, stored.best_offer_id)
        assert best.market_country == "BE" and best.price == 319
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 1
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 5
        assert {"awin", "cj", "tradedoubler"}.isdisjoint(
            set(await session.scalars(select(Store.name)))
        )
    weak = row(
        external_id="case",
        price="9",
        title="Sony WH-1000XM6 compatible carrying case",
        gtin=None,
        brand=None,
        model=None,
    )
    weak_offer = await container.products.persist(
        weak.offer(
            program_id=None,
            network="ebay",
            merchant_id="77",
            name="Direct merchant",
            domain="example.com",
            market="BE",
        )
    )
    assert weak_offer.product_id != be.products[0].id


async def test_twenty_due_programs_are_fair_despite_failures(container):
    programs, sync, _, base = await setup_feeds(container)
    merchants = [await program(programs, str(i + 500)) for i in range(20)]
    async with container.sessions.begin() as session:
        await session.execute(
            update(FeedSyncState).values(next_sync_at=utcnow() - timedelta(days=1))
        )
    seen = []
    for _ in range(20):
        claim = await sync.claim()
        assert claim is not None
        seen.append(claim[0])
        fail = claim[0] in {p.id for p in merchants[:3]}
        if fail:
            with pytest.raises(RuntimeError):
                await sync.run(claim[0], source(base, [], fail=True), token=claim[1])
        else:
            await sync.run(claim[0], source(base, [row()]), token=claim[1])
    assert seen == sorted(p.id for p in merchants)
    assert await sync.claim() is None
    async with container.sessions() as session:
        states = list(await session.scalars(select(FeedSyncState)))
        assert sum(s.status == "complete" for s in states) == 17
        assert sum(s.failure_count == 1 for s in states) == 3


async def test_lifecycle_import_is_idempotent_and_audit_cannot_be_edited(container):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from pricehunter.db.models import MerchantProgramAudit

    programs, _, _, _ = await setup_feeds(container)
    data = MerchantProgramInput(
        network="awin",
        external_merchant_id="601",
        external_feed_id="601",
        market_country="BE",
        display_name="Merchant",
        domain="example.com",
        currency="EUR",
    )
    pending = await programs.save(data)
    assert pending.version == 1 and not pending.approved and not pending.active
    with pytest.raises(ValueError, match="review"):
        await programs.activate(pending.id, expected_version=1, reason="Not reviewed")
    reviewed = await programs.review(
        pending.id, SYNTHETIC_POLICY, expected_version=1, reason="Signed terms"
    )
    reimport = await programs.save(
        data.model_copy(
            update={
                "display_name": "Overwrite",
                "active": True,
                "approved": True,
                "policy": SYNTHETIC_POLICY,
            }
        )
    )
    assert reimport.version == reviewed.version and reimport.display_name == "Merchant"
    assert reimport.policy_data == reviewed.policy_data and not reimport.active
    preview = await programs.activate(
        pending.id, expected_version=reviewed.version, reason="Preview", dry_run=True
    )
    assert not preview.active and preview.version == 2
    active = await programs.activate(pending.id, expected_version=2, reason="Approved pilot")
    metadata = await programs.update_metadata(
        active.id, expected_version=3, reason="Correct spelling", display_name="Merchant corrected"
    )
    changed = await programs.change_feed_reference(
        metadata.id, "602", expected_version=4, reason="New feed needs new review"
    )
    assert not changed.active and not changed.approved and not changed.policy_data["reviewed"]
    assert changed.version == 5
    history = await programs.history(pending.id)
    assert [a.action for a in history] == [
        "feed_reference_changed",
        "metadata_changed",
        "activated",
        "reviewed",
        "created",
    ]
    assert all(a.new_version == a.previous_version + 1 for a in history)
    for statement in (
        "UPDATE merchant_program_audits SET reason='tamper'",
        "DELETE FROM merchant_program_audits",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with container.sessions.begin() as session:
                await session.execute(text(statement))
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MerchantProgramAudit)) == 5


async def test_policy_changes_immediately_change_watch_outbound_and_comparison(container):
    from pricehunter.db.models import PriceObservation, ProductWatch
    from pricehunter.schemas.watches import WatchCreate
    from pricehunter.services.comparison_service import ComparisonReader

    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "603")
    await sync.run(p.id, source(base, [row(direct_url="https://example.com/sony")]))
    user = await market_user(container, 780003, "BE")
    product = (await container.search.search("Sony", user.id)).products[0]
    watch = await container.watches.create(
        user.id, WatchCreate(product_id=product.id, market_country="BE", currency="EUR")
    )
    policy = SYNTHETIC_POLICY.model_copy(
        update={"tracking_allowed": False, "affiliate_allowed": False}
    )
    p = await programs.change_policy(
        p.id, policy, expected_version=p.version, reason="Tracking and affiliate rights revoked"
    )
    async with container.sessions.begin() as session:
        await container.watches.evaluate(session, product.id, market_country="BE")
        stored_watch = await session.get(ProductWatch, watch.id)
        assert stored_watch.best_offer_id is None
        summary = await ComparisonReader(container.settings, market_country="BE").summary(
            session, product.id, utcnow()
        )
        assert summary.groups[0].best_available_offer.price == 329
        assert summary.groups[0].best_available_offer.url == "https://example.com/sony"
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 1
    policy = policy.model_copy(
        update={
            "catalog_persistence_allowed": False,
            "tracking_allowed": False,
            "price_history_allowed": False,
            "refresh_allowed": False,
        }
    )
    await programs.change_policy(
        p.id, policy, expected_version=p.version, reason="Catalog permission revoked"
    )
    assert await provider.search("Sony", country="BE") == []
    async with container.sessions() as session:
        summary = await ComparisonReader(container.settings, market_country="BE").summary(
            session, product.id, utcnow()
        )
        assert summary.groups == []
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) == 1
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 1
        assert await session.scalar(select(func.count()).select_from(ProductWatch)) == 1


async def test_disable_reactivate_requires_new_complete_feed_and_preserves_ids(container):
    from pricehunter.db.models import PriceObservation

    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "604")
    await sync.run(p.id, source(base, [row()]))
    offer = await container.products.persist((await provider.search("Sony", country="BE"))[0])
    p = await programs.disable(p.id, expected_version=p.version, reason="Pause merchant")
    assert await provider.search("Sony", country="BE") == []
    p = await programs.activate(p.id, expected_version=p.version, reason="Resume reviewed merchant")
    assert await provider.search("Sony", country="BE") == []
    with pytest.raises(RuntimeError):
        await sync.run(p.id, source(base, [row()], fail=True))
    assert await provider.search("Sony", country="BE") == []
    await sync.run(p.id, source(base, [row()]))
    repeated = await container.products.persist((await provider.search("Sony", country="BE"))[0])
    assert (repeated.id, repeated.product_id) == (offer.id, offer.product_id)
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PriceObservation)) >= 1


async def test_coverage_reports_and_duplicate_merchants_are_read_only(container):
    programs, sync, provider, base = await setup_feeds(container)
    p = await program(programs, "605")
    await sync.run(p.id, source(base, [row()]))
    await container.products.persist((await provider.search("Sony", country="BE"))[0])
    await programs.save(
        MerchantProgramInput(
            network="cj",
            external_merchant_id="605",
            external_feed_id="605",
            market_country="BE",
            display_name=p.display_name,
            domain="www.example.com",
            currency="EUR",
        )
    )
    report = await container.coverage.report()
    assert report["providers"]["awin"]["enabled"]
    assert not report["providers"]["cj"]["enabled"] and not report["providers"]["cj"]["configured"]
    market = report["markets"]["BE"]["awin"]
    assert market["staged_active"] == 1 and market["materialized_offers"] == 1
    assert (
        market["fresh_offers"] == 1
        and market["tracking_eligible_offers"] == 1
        and market["history_eligible_offers"] == 1
    )
    candidates = await container.coverage.product("Sony", "BE")
    assert candidates["results"][0]["candidate_count"] == 1
    assert candidates["results"][0]["materialized_offers"] == 1
    assert len(await container.coverage.duplicate_merchants()) == 1
    async with container.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Store)) == 2
        assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 1


async def test_operator_cli_requires_confirmation_and_templates_fail_closed(
    container, capsys, tmp_path
):
    from argparse import ArgumentParser

    from pricehunter.apps import feed_admin
    from pricehunter.domain.feeds import FeedReference

    programs, _, _, base = await setup_feeds(container)

    class Visible(base):
        name = "awin"

        async def stream_items(self, program):
            for item in []:
                yield item

        async def discover_feeds(self):
            return [FeedReference("606", "606", "Visible merchant", "BE", "v1", "EUR")]

    container.feed_sources = {"awin": Visible()}
    parser = ArgumentParser()
    feed_admin.add_commands(parser.add_subparsers(dest="command"))
    await feed_admin.run(container, parser.parse_args(["merchant-candidates", "awin"]))
    assert "Visible merchant" in capsys.readouterr().out
    await feed_admin.run(
        container,
        parser.parse_args(["merchant-program-template", "awin", "606", "--feed-id", "606"]),
    )
    import json

    template = json.loads(capsys.readouterr().out)
    assert (
        not template["active"] and not template["approved"] and not template["policy"]["reviewed"]
    )
    assert template["domain"] == "REVIEW_REQUIRED"
    template["domain"] = "example.com"
    path = tmp_path / "program.json"
    path.write_text(json.dumps(template))
    with pytest.raises(ValueError, match="confirm"):
        await feed_admin.run(container, parser.parse_args(["merchant-program-import", str(path)]))
    await feed_admin.run(
        container, parser.parse_args(["merchant-program-import", str(path), "--dry-run"])
    )
    async with container.sessions() as session:
        from pricehunter.db.models import MerchantProgram

        assert await session.scalar(select(func.count()).select_from(MerchantProgram)) == 0
    await feed_admin.run(
        container, parser.parse_args(["merchant-program-import", str(path), "--confirm"])
    )
    async with container.sessions() as session:
        p = (await session.scalars(select(MerchantProgram))).one()
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(SYNTHETIC_POLICY.model_dump_json())
    await feed_admin.run(
        container,
        parser.parse_args(
            [
                "merchant-program-review",
                str(p.id),
                "--expected-version",
                "1",
                "--reason",
                "Signed review",
                "--policy-file",
                str(policy_file),
                "--confirm",
            ]
        ),
    )
    p = await programs.get(p.id)
    assert p.approved and not p.active and p.version == 2


async def test_bounded_dedup_keeps_variants_and_market_rejections_diagnostic(
    container, monkeypatch
):
    container.settings.search_provider_candidate_limit = 10
    container.settings.search_persistence_limit = 3
    container.settings.search_comparison_limit = 1
    valid = MockStoreProvider()._offer("sony-a").model_copy(update={"external_id": "0"})

    class Many(MockStoreProvider):
        async def search(self, *args, **kwargs):
            return [
                valid,
                valid,
                valid.model_copy(update={"variant": {"color": "red"}}),
                valid.model_copy(update={"country": "BE"}),
            ] + [valid.model_copy(update={"external_id": f"z{i}"}) for i in range(30)]

    container.registry.providers = {"mock": Many()}
    calls = []
    original = container.products.persist

    async def persist(data, **kwargs):
        calls.append((data.external_id, data.country, data.variant))
        return await original(data, **kwargs)

    monkeypatch.setattr(container.products, "persist", persist)
    user = await market_user(container, 780004, "DE")
    result = await container.search.search("Sony", user.id)
    assert len(calls) <= 3 and len(result.products) <= 1
    assert result.unavailable_providers == []
    outcome = result.provider_outcomes[0]
    assert outcome.duplicate_count == 1 and outcome.status == "PARTIAL"
    assert "candidate_limit" in outcome.errors and "persistence_limit" in outcome.errors
    assert len(calls) == 2 and {tuple(d[2].items()) for d in calls} == {
        (("color", "black"),),
        (("color", "red"),),
    }
    assert "market_mismatch" in outcome.errors


async def test_request_failure_does_not_hide_other_network_and_health_recovers(container):
    class Broken(MockStoreProvider):
        name = "cj"

        async def search(self, *args, **kwargs):
            raise RuntimeError("secret-token must not surface")

    container.registry.providers = {"mock": MockStoreProvider(), "cj": Broken()}
    user = await market_user(container, 780005, "DE")
    result = await container.search.search("Sony", user.id)
    assert result.products and result.unavailable_providers == ["cj"]
    health = await container.search.health.read("cj")
    assert health["consecutive_failures"] == "1" and health["status"] == "NETWORK_FAILED"

    class Empty(Broken):
        async def search(self, *args, **kwargs):
            return []

    container.registry.providers["cj"] = Empty()
    result = await container.search.search("Sony", user.id)
    assert result.unavailable_providers == []
    health = await container.search.health.read("cj")
    assert health["consecutive_failures"] == "0" and health["status"] == "EMPTY"
    assert "last_failure" in health and "last_success" in health


async def test_one_merchant_policy_rejection_does_not_hide_its_network(container):
    programs, sync, provider, base = await setup_feeds(container)
    p1, p2 = await program(programs, "607"), await program(programs, "608")
    for p in (p1, p2):
        await sync.run(p.id, source(base, [row()]))
    candidates = await provider.search("Sony", country="BE")
    await programs.change_policy(
        p1.id,
        SYNTHETIC_POLICY.model_copy(
            update={
                "catalog_persistence_allowed": False,
                "tracking_allowed": False,
                "price_history_allowed": False,
                "refresh_allowed": False,
            }
        ),
        expected_version=p1.version,
        reason="Merchant A permission revoked after retrieval",
    )

    class Captured(FeedStoreProvider):
        async def search(self, *args, **kwargs):
            return candidates

    container.registry.providers = {
        "awin": Captured("awin", container.sessions, container.settings)
    }
    user = await market_user(container, 780006, "BE")
    result = await container.search.search("Sony", user.id)
    assert result.unavailable_providers == [] and len(result.products) == 1
    assert result.provider_outcomes[0].status == "PARTIAL"
    assert (
        result.provider_outcomes[0].accepted_count == 1
        and result.provider_outcomes[0].rejected_count == 1
    )
    assert result.products[0].best_available_offer.store_slug == "awin-608"


async def test_cj_fake_http_partial_feed_materializes_and_request_failure_preserves_generation(
    container, redis
):
    from pydantic import SecretStr

    from pricehunter.domain.feeds import FeedError
    from pricehunter.providers.feeds.cj import CJFeedSource
    from tests.unit.test_cj_adapter import fixture, page, response
    from tests.unit.test_feed_adapters import transport

    container.settings.cj_enabled = True
    programs = container.merchant_programs
    p = await programs.save(
        MerchantProgramInput(
            network="cj",
            external_merchant_id="101",
            external_feed_id="501",
            market_country="BE",
            display_name="Example shop",
            domain="example.com",
            currency="EUR",
        )
    )
    p = await programs.review(
        p.id, SYNTHETIC_POLICY, expected_version=p.version, reason="Synthetic CJ review"
    )
    p = await programs.activate(p.id, expected_version=p.version, reason="Synthetic CJ activation")
    failed = False

    def handler(request):
        if failed:
            import httpx

            from tests.unit.test_feed_adapters import Stream

            return httpx.Response(401, stream=Stream(b"secret-remote-message"))
        if "shoppingProductFeeds" in request.content.decode():
            return response(fixture("feeds"))
        return response(
            page(
                [fixture("electronics") | {"id": str(i)} for i in range(10)]
                + [fixture("invalid_upc")]
            )
        )

    client, http = transport(redis, handler)
    adapter = CJFeedSource(http, SecretStr("fixture-pat"), "123", "456")
    provider = FeedStoreProvider("cj", container.sessions, container.settings)
    container.registry.providers = {"cj": provider}
    async with client:
        report = await container.feed_sync.run(p.id, adapter)
        assert report.valid_rows == 10 and report.invalid_rows == 1
        user = await market_user(container, 780007, "BE")
        result = await container.search.search("Sony", user.id)
        assert result.unavailable_providers == [] and len(result.products) == 1
        async with container.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(StoreOffer)) == 10
            generation = (await session.get(FeedSyncState, p.id)).generation
        failed = True
        with pytest.raises(FeedError, match="http_401"):
            await container.feed_sync.run(p.id, adapter)
        assert len(await provider.search("Sony", country="BE")) == 10
        async with container.sessions() as session:
            state = await session.get(FeedSyncState, p.id)
            assert state.generation == generation and state.last_failure_at


async def test_duplicate_version_choice_ignores_affiliate_payout_and_timezone_spelling(container):
    from datetime import datetime

    base = MockStoreProvider()._offer("sony-a")
    old = base.model_copy(
        update={
            "price": Decimal("329"),
            "source_updated_at": datetime.fromisoformat("2026-01-01T11:00:00+02:00"),
            "affiliate_metadata": {"commission": 9999},
        }
    )
    newer = base.model_copy(
        update={
            "price": Decimal("319"),
            "source_updated_at": datetime.fromisoformat("2026-01-01T10:00:00+00:00"),
            "affiliate_metadata": {"commission": 0},
        }
    )

    class Versions(MockStoreProvider):
        reverse = False

        async def search(self, *args, **kwargs):
            values = [old, newer]
            return values[::-1] if self.reverse else values

    provider = Versions()
    container.registry.providers = {"mock": provider}
    user = await market_user(container, 780008, "DE")
    first = await container.search.search("Sony", user.id)
    assert first.products[0].best_available_offer.price == 319
    provider.reverse = True
    old.affiliate_metadata["commission"], newer.affiliate_metadata["commission"] = 0, 999999
    repeated = await container.search.search("Sony", user.id)
    assert repeated.products[0].best_available_offer.price == 319
    assert (
        first.provider_outcomes[0].duplicate_count
        == repeated.provider_outcomes[0].duplicate_count
        == 1
    )

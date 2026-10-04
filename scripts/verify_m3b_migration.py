import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from uuid import uuid4

import fakeredis.aioredis
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from pricehunter.core.config import Settings
from pricehunter.core.container import Container
from pricehunter.db.base import utcnow
from pricehunter.db.models import (
    BestPriceEvent,
    NotificationEvent,
    PaymentEvent,
    ProductBestState,
    ProductDiscovery,
    ProductIdentifier,
    Subscription,
)

name = "pricehunter_m4a1_" + uuid4().hex[:10] + "_test"
test_url = make_url(os.environ["TEST_DATABASE_URL"])
if not (test_url.database or "").endswith("_test"):
    raise RuntimeError("TEST_DATABASE_URL must name a dedicated database ending _test")
scratch_url = test_url.set(database=name).render_as_string(hide_password=False)
env = dict(os.environ, DATABASE_URL=scratch_url, PYTHONPATH="src")
ids = [uuid4() for _ in range(6)]


def migrate(*args, success=True, contains="Export best-price history"):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True
    )
    if success:
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
    else:
        assert result.returncode and contains in result.stderr


async def main():
    admin = create_async_engine(test_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(scratch_url, hide_parameters=True)
    try:
        migrate("upgrade", "124441561d5e")
        async with engine.begin() as c:
            await c.execute(
                text("""INSERT INTO users
                (id, language_code, preferred_currency, timezone, last_active_at)
                VALUES (:id, 'fr', 'EUR', 'Europe/Brussels', now())"""),
                {"id": ids[0]},
            )
            await c.execute(
                text("""INSERT INTO stores
                (id,slug,name,domain,provider_type,country,supported,active)
                VALUES (:id,'legacy','Legacy','legacy.test','mock','BE',true,true)"""),
                {"id": ids[1]},
            )
            await c.execute(
                text("""INSERT INTO products
                (id,identity_key,canonical_name,brand,model,variant,metadata)
                VALUES (:id, :key,'Legacy model','Sony','WH1000XM6','{}','{}')"""),
                {"id": ids[2], "key": "a" * 64},
            )
            await c.execute(
                text("""INSERT INTO store_offers
                (id,product_id,store_id,external_id,url,direct_url,title,price,currency,availability,
                metadata,last_checked_at,next_check_at,failure_count,refresh_sequence,minimum_price,
                maximum_price,total_price,observation_count)
                VALUES (:id,:product,:store,'legacy-1','https://legacy.test/1','https://legacy.test/1',
                'Legacy model',329,'EUR','in_stock','{}',now(),now(),0,0,329,329,329,1)"""),
                {"id": ids[3], "product": ids[2], "store": ids[1]},
            )
            await c.execute(
                text("""INSERT INTO trackers (id,user_id,store_offer_id,baseline_price,
                notify_on_any_drop,notify_on_target,notify_on_back_in_stock,notify_on_historical_low,
                enabled,check_interval_seconds)
                VALUES (:id,:user,:offer,329,true,true,true,true,true,43200)"""),
                {"id": ids[4], "user": ids[0], "offer": ids[3]},
            )
            await c.execute(
                text("""INSERT INTO price_observations
                (id,store_offer_id,refresh_key,price,currency,availability,checked_at)
                VALUES (:id,:offer,'migration-test',329,'EUR','in_stock',now())"""),
                {"id": ids[5], "offer": ids[3]},
            )
        watch_id, subscription_id = uuid4(), uuid4()
        async with engine.begin() as c:
            await c.execute(
                text("""INSERT INTO product_watches
                (id,user_id,product_id,currency,notify_on_new_best,notify_on_price_drop,
                 enabled,best_offer_id,best_price,evaluation_sequence)
                VALUES (:id,:user,:product,'EUR',true,true,true,:offer,329,0)"""),
                {"id": watch_id, "user": ids[0], "product": ids[2], "offer": ids[3]},
            )
            await c.execute(
                insert(NotificationEvent).values(
                    id=uuid4(),
                    product_watch_id=watch_id,
                    event_type="new_best_price",
                    price=329,
                    currency="EUR",
                    deduplication_key="migration-watch",
                    snapshot={"store": "Legacy"},
                )
            )
            await c.execute(
                insert(Subscription).values(
                    id=subscription_id,
                    user_id=ids[0],
                    plan="pro",
                    provider="legacy",
                    status="active",
                    valid_until=utcnow() + timedelta(days=30),
                )
            )
            await c.execute(
                insert(PaymentEvent).values(
                    id=uuid4(),
                    user_id=ids[0],
                    subscription_id=subscription_id,
                    provider="legacy",
                    external_event_id="migration-payment",
                    event_type="purchase",
                    amount=250,
                    currency="XTR",
                    status="completed",
                )
            )

        async def snapshot(extra=()):
            async with engine.connect() as c:
                return {
                    table: [
                        dict(r)
                        for r in (
                            await c.execute(text(f"SELECT * FROM {table} ORDER BY 1"))
                        ).mappings()
                    ]
                    for table in (
                        "users",
                        "stores",
                        "products",
                        "store_offers",
                        "trackers",
                        "price_observations",
                        "product_watches",
                        "notification_events",
                        "subscriptions",
                        "payment_events",
                        "product_identifiers",
                        "product_discoveries",
                        "product_best_states",
                        "best_price_events",
                    )
                    + extra
                }

        # Create a real M3B-shaped database before upgrading M4A. Models below
        # are unchanged tables; old watch inserts deliberately omit market_country.
        migrate("upgrade", "2872920b653a")
        german_user, german_watch = uuid4(), uuid4()
        async with engine.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO users (id, language_code, country_code, preferred_currency, "
                    "timezone, last_active_at) VALUES (:id, 'de', 'DE', 'EUR', 'UTC', now())"
                ),
                {"id": german_user},
            )
            await c.execute(
                text(
                    "INSERT INTO product_watches (id,user_id,product_id,currency,"
                    "notify_on_new_best,"
                    "notify_on_price_drop,enabled,best_offer_id,best_price,evaluation_sequence) "
                    "VALUES (:id,:user,:product,'EUR',true,true,true,:offer,329,0)"
                ),
                {"id": german_watch, "user": german_user, "product": ids[2], "offer": ids[3]},
            )
            await c.execute(
                insert(ProductIdentifier).values(
                    id=uuid4(),
                    product_id=ids[2],
                    kind="brand_model",
                    value="sony:wh1000xm6",
                    source="legacy",
                    confidence="0.95",
                )
            )
            await c.execute(
                insert(ProductDiscovery).values(
                    id=uuid4(), product_id=ids[2], provider="mock", country="BE", currency="EUR"
                )
            )
            await c.execute(
                insert(ProductBestState).values(
                    id=uuid4(),
                    product_id=ids[2],
                    currency="EUR",
                    store_offer_id=ids[3],
                    price=329,
                    sequence=1,
                    next_evaluation_at=utcnow() + timedelta(minutes=5),
                )
            )
            await c.execute(
                insert(BestPriceEvent).values(
                    id=uuid4(),
                    product_id=ids[2],
                    currency="EUR",
                    store_offer_id=ids[3],
                    price=329,
                    store="Legacy",
                    event_type="initial_best",
                    sequence=1,
                    source_observation_id=ids[5],
                )
            )
        original = await snapshot()
        migrate("upgrade", "2c125500eaf6")
        await engine.dispose()
        m4a = await snapshot()
        for table, rows in original.items():
            for before, current in zip(rows, m4a[table], strict=True):
                assert {k: v for k, v in before.items() if k != "updated_at"} == {
                    k: current[k] for k in before if k != "updated_at"
                }, table
        # The same merchant already serves two catalog markets at the M4A boundary.
        belgian_offer = uuid4()
        async with engine.begin() as c:
            await c.execute(
                text('UPDATE store_offers SET metadata = \'{"market_country":"DE"}\'::jsonb')
            )
            columns = (
                (
                    await c.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='store_offers' ORDER BY ordinal_position"
                        )
                    )
                )
                .scalars()
                .all()
            )
            names = ",".join('"' + name + '"' for name in columns)
            overrides = {
                "id": ":id",
                "external_id": "'legacy-be'",
                "metadata": '\'{"market_country":"ZZ"}\'::jsonb',
                "price": "340",
            }
            values = ",".join(overrides.get(name, '"' + name + '"') for name in columns)
            await c.execute(
                text(
                    f"INSERT INTO store_offers ({names}) SELECT {values} "
                    "FROM store_offers WHERE id=:source"
                ),
                {"id": belgian_offer, "source": ids[3]},
            )
        original = await snapshot()
        migrate("upgrade", "b7c21a48d903")
        await engine.dispose()
        async with engine.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO fx_rates "
                    "(id,base_currency,quote_currency,rate,effective_date,fetched_at,source) "
                    "VALUES (:id,'EUR','USD',1.15,current_date,now(),'ECB')"
                ),
                {"id": uuid4()},
            )
            await c.execute(
                text(
                    "INSERT INTO outbound_clicks "
                    "(id,offer_id,store_id,affiliate_network,surface,market_country,"
                    "created_at,opaque_click_reference) "
                    "VALUES (:id,:offer,:store,'fixture','comparison','BE',now(),"
                    "'migration-fixture')"
                ),
                {"id": uuid4(), "offer": ids[3], "store": ids[1]},
            )
        before_feeds = await snapshot(("fx_rates", "outbound_clicks"))
        migrate("upgrade", "431e7de33df9")
        legacy_program_id = uuid4()
        async with engine.begin() as c:
            await c.execute(
                text("""INSERT INTO merchant_programs
                (id,network,external_merchant_id,market_country,store_id,display_name,domain,
                currency,active,approved,feed_mode,external_feed_id,feed_language,policy_data,
                review_reference,last_reviewed_at,created_at,updated_at)
                VALUES (:id,'awin','998','BE',:store,'Existing reviewed merchant','legacy.test',
                'EUR',true,true,'full','998','en',jsonb_build_object('reviewed',true),
                'Existing signed review',now(),now(),now())"""),
                {"id": legacy_program_id, "store": ids[1]},
            )
            legacy_program = dict(
                (
                    await c.execute(
                        text("SELECT * FROM merchant_programs WHERE id=:id"),
                        {"id": legacy_program_id},
                    )
                )
                .mappings()
                .one()
            )
        migrate("upgrade", "9f62c7d40a11")
        await engine.dispose()
        merchant_baseline = await snapshot(
            (
                "stores",
                "store_offers",
                "products",
                "trackers",
                "product_watches",
                "price_observations",
                "best_price_events",
                "notification_events",
                "outbound_clicks",
                "merchant_programs",
                "subscriptions",
                "payment_events",
            )
        )
        migrate("upgrade", "6bde2194a7c0")
        await engine.dispose()
        m4d_tables = tuple(merchant_baseline) + (
            "merchants",
            "merchant_audits",
            "merchant_program_audits",
            "feed_sync_states",
            "merchant_feed_items",
            "fx_rates",
        )
        m4d_before = await snapshot(m4d_tables)
        migrate("upgrade", "head")
        await engine.dispose()
        m4e_after = await snapshot(m4d_tables)
        for table, rows in m4d_before.items():
            for before, current in zip(rows, m4e_after[table], strict=True):
                assert before == {key: current[key] for key in before}, table
        async with engine.begin() as c:
            assert await c.scalar(text("SELECT count(*) FROM merchant_program_validations")) == 0
            assert await c.scalar(text("SELECT count(*) FROM feed_publication_audits")) == 0
            await c.execute(
                text("""INSERT INTO merchant_program_validations
                (id,merchant_program_id,status,configuration_fingerprint,started_at,completed_at,
                valid_rows,invalid_rows,duplicate_rows,sampled_rows,warnings,metrics,adapter_revision)
                VALUES (gen_random_uuid(),:p,'passed',repeat('a',64),now(),now(),
                    1,0,0,0,'[]','{}','migration-fixture')"""),
                {"p": legacy_program_id},
            )
            await c.execute(
                text("""INSERT INTO feed_publication_audits
                (id,merchant_program_id,generation,previous_rows,candidate_rows,invalid_rows,guard,reason)
                VALUES (gen_random_uuid(),:p,1,1000,10,0,'quality_shrink','Synthetic override')"""),
                {"p": legacy_program_id},
            )
        for table in ("merchant_program_validations", "feed_publication_audits"):
            for operation in (f"UPDATE {table} SET created_at=now()", f"DELETE FROM {table}"):
                try:
                    async with engine.begin() as c:
                        await c.execute(text(operation))
                except Exception as exc:
                    assert "append-only" in str(exc)
                else:
                    raise AssertionError("Pilot evidence mutation was allowed")
        migrate(
            "downgrade", "6bde2194a7c0", success=False, contains="Export merchant pilot evidence"
        )
        # Export only synthetic evidence in this disposable scratch database.
        async with engine.begin() as c:
            await c.execute(text("TRUNCATE merchant_program_validations, feed_publication_audits"))
        migrate("downgrade", "6bde2194a7c0")
        migrate("upgrade", "head")
        await engine.dispose()
        merchant_after = await snapshot(tuple(merchant_baseline))
        for table, rows in merchant_baseline.items():
            for before, current in zip(rows, merchant_after[table], strict=True):
                assert before == {key: current[key] for key in before}, table
        async with engine.connect() as c:
            stores = (
                await c.execute(text("SELECT id,merchant_id,name FROM stores ORDER BY id"))
            ).all()
            merchants = (
                await c.execute(text("SELECT id,display_name FROM merchants ORDER BY id"))
            ).all()
            assert len(stores) == len(merchants)
            assert [(s.id, s.name) for s in stores] == [tuple(m) for m in merchants]
            assert all(s.id == s.merchant_id for s in stores)
            assert await c.scalar(text("SELECT merchant_id FROM outbound_clicks")) == ids[1]
            assert (
                await c.scalar(
                    text("SELECT best_merchant_id FROM product_watches WHERE id=:id"),
                    {"id": watch_id},
                )
                == ids[1]
            )
        for statement in (
            "UPDATE merchant_audits SET reason='tamper'",
            "DELETE FROM merchant_audits",
        ):
            try:
                async with engine.begin() as c:
                    await c.execute(text(statement))
            except Exception as exc:
                assert "append-only" in str(exc)
            else:
                raise AssertionError("Canonical merchant audit mutation was allowed")
        async with engine.connect() as c:
            upgraded = dict(
                (
                    await c.execute(
                        text("SELECT * FROM merchant_programs WHERE id=:id"),
                        {"id": legacy_program_id},
                    )
                )
                .mappings()
                .one()
            )
            assert upgraded.pop("version") == 1 and upgraded == legacy_program
            audit = (
                await c.execute(
                    text(
                        "SELECT action,previous_version,new_version FROM merchant_program_audits "
                        "WHERE merchant_program_id=:id"
                    ),
                    {"id": legacy_program_id},
                )
            ).one()
            assert tuple(audit) == ("migrated", 0, 1)
        for statement in (
            "UPDATE merchant_program_audits SET reason='tamper'",
            "DELETE FROM merchant_program_audits",
        ):
            try:
                async with engine.begin() as c:
                    await c.execute(text(statement))
            except Exception as exc:
                assert "append-only" in str(exc)
            else:
                raise AssertionError("Audit mutation was allowed")
        after_feeds = await snapshot(("fx_rates", "outbound_clicks"))
        for table, rows in before_feeds.items():
            for before, current in zip(rows, after_feeds[table], strict=True):
                assert before == {key: current[key] for key in before}, table
        # Remove only these synthetic rows after proving M4B preserved them.
        async with engine.begin() as c:
            await c.execute(
                text("DELETE FROM outbound_clicks WHERE opaque_click_reference='migration-fixture'")
            )
            await c.execute(text("DELETE FROM fx_rates"))
        settings = Settings(_env_file=None, database_url=scratch_url)
        container = Container(settings, redis=fakeredis.aioredis.FakeRedis())
        try:
            # Before reconciliation the migrated 1:1 retail identities retain the
            # existing customer's prices, source IDs, labels and market counts.
            baseline_stores = {s["id"]: s for s in merchant_baseline["stores"]}
            for market in ("BE", "DE"):
                old_offers = [
                    o
                    for o in merchant_baseline["store_offers"]
                    if o["product_id"] == ids[2] and o["market_country"] == market
                ]
                async with container.sessions() as session:
                    comparison = await container.products.comparisons.build(
                        session, ids[2], market_country=market
                    )
                assert comparison.offer_count == len(old_offers) == 1
                assert comparison.store_count == len({o["store_id"] for o in old_offers})
                old = old_offers[0]
                current = comparison.offers[0]
                assert (
                    current.offer_id,
                    current.source_store_id,
                    current.store,
                    current.price,
                    current.currency,
                    current.store_country,
                ) == (
                    old["id"],
                    old["store_id"],
                    baseline_stores[old["store_id"]]["name"],
                    old["price"],
                    old["currency"],
                    market,
                )
                assert current.merchant_id == old["store_id"]
                assert comparison.best_available_offer.price == old["price"]
                assert comparison.price_spread == 0
            after = await snapshot()
            for table, rows in original.items():
                for before, current in zip(rows, after[table], strict=True):
                    derived = {"updated_at"}
                    if table == "product_best_states":
                        derived.add("next_evaluation_at")
                    if table == "product_watches":
                        derived.add("best_absence_reason")
                    assert {k: v for k, v in before.items() if k not in derived} == {
                        k: current[k] for k in before if k not in derived
                    }, table
            assert all(row["market_country"] is None for row in after["best_price_events"])
            assert all(row["market_country"] is None for row in after["product_best_states"])
            assert await container.comparison_operations.maintain() == 2
            assert await container.comparison_operations.maintain() == 0
            async with engine.connect() as c:
                offer_markets = dict(
                    (await c.execute(text("SELECT id,market_country FROM store_offers"))).all()
                )
                assert offer_markets[ids[3]] == "DE" and offer_markets[belgian_offer] == "BE"
                watches = dict(
                    (
                        await c.execute(
                            text("SELECT market_country,best_price FROM product_watches")
                        )
                    ).all()
                )
                assert watches == {"BE": 340, "DE": 329}
                assert await c.scalar(text("SELECT count(*) FROM notification_events")) == 1
                assert (
                    await c.scalar(
                        text("SELECT count(*) FROM best_price_events WHERE market_country IS NULL")
                    )
                    == 1
                )
                assert (
                    await c.scalar(
                        text(
                            "SELECT count(*) FROM best_price_events "
                            "WHERE market_country IS NOT NULL"
                        )
                    )
                    == 2
                )
            from pricehunter.domain.feeds import MerchantProgramInput

            program = await container.merchant_programs.save(
                MerchantProgramInput(
                    network="awin",
                    external_merchant_id="999",
                    market_country="BE",
                    display_name="Migration fixture",
                    domain="example.com",
                    currency="EUR",
                    external_feed_id="999",
                )
            )
            # Exercise operator-used downgrade guard, then export/reconcile only the
            # synthetic scratch identity. Production audit is never erased by migration.
            from pricehunter.domain.merchants import MerchantInput

            canonical = await container.merchants.create(
                MerchantInput(slug="migration-retailer", display_name="Explicit retailer"),
                expected_version=0,
                reason="Synthetic operator review",
                confirm=True,
            )
            migrate(
                "downgrade",
                "9f62c7d40a11",
                success=False,
                contains="Export/reconcile canonical merchant",
            )
            async with engine.begin() as c:
                await c.execute(text("TRUNCATE merchant_audits"))
                await c.execute(text("DELETE FROM merchants WHERE id=:id"), {"id": canonical.id})
            migrate("downgrade", "9f62c7d40a11")
            migrate("upgrade", "head")
            await engine.dispose()
            await container.sessions.kw["bind"].dispose()
            migrate(
                "downgrade",
                "b7c21a48d903",
                success=False,
                contains="Export merchant program audit",
            )
            async with engine.begin() as c:
                # Synthetic audit export verified above; administrative cleanup only in scratch DB.
                await c.execute(text("TRUNCATE merchant_program_audits"))
                await c.execute(text("TRUNCATE merchant_audits"))
                await c.execute(
                    text("DELETE FROM merchant_programs WHERE id=:id"), {"id": legacy_program_id}
                )
            migrate(
                "downgrade",
                "b7c21a48d903",
                success=False,
                contains="Remove/export merchant programs",
            )
            async with engine.begin() as c:
                await c.execute(
                    text("DELETE FROM feed_sync_states WHERE merchant_program_id=:id"),
                    {"id": program.id},
                )
                await c.execute(
                    text("DELETE FROM merchant_programs WHERE id=:id"), {"id": program.id}
                )
                await c.execute(text("DELETE FROM stores WHERE id=:id"), {"id": program.store_id})
                await c.execute(
                    text("DELETE FROM merchants WHERE id=:id"), {"id": program.store_id}
                )
            migrate(
                "downgrade", "2c125500eaf6", success=False, contains="Export market-scoped history"
            )
            async with engine.begin() as c:
                await c.execute(
                    text("DELETE FROM best_price_events WHERE market_country IS NOT NULL")
                )
            assert await container.discovery.synchronize() == 1
            async with engine.connect() as c:
                markets = dict(
                    (await c.execute(text("SELECT id, market_country FROM product_watches"))).all()
                )
                assert markets[watch_id] == "BE"  # Only legacy missing-country rows get BE.
                assert markets[german_watch] == "DE"
                assert (
                    await c.scalar(
                        text(
                            "SELECT is_nullable FROM information_schema.columns "
                            "WHERE table_name='store_offers' AND column_name='direct_url'"
                        )
                    )
                    == "YES"
                )
                assert await c.scalar(text("SELECT count(*) FROM outbound_clicks")) == 0
                assert await c.scalar(text("SELECT count(*) FROM fx_rates")) == 0
            async with engine.begin() as c:
                await c.execute(
                    text(
                        "INSERT INTO fx_rates (id,base_currency,quote_currency,rate,effective_date,"
                        "fetched_at,source) VALUES (:id,'EUR','USD',1.15,current_date,now(),'ECB')"
                    ),
                    {"id": uuid4()},
                )
            migrate("downgrade", "2872920b653a", success=False, contains="Export or reconcile M4A")
            async with engine.begin() as c:
                assert await c.scalar(text("SELECT count(*) FROM fx_rates")) == 1
                await c.execute(text("DELETE FROM fx_rates"))
            migrate("downgrade", "124441561d5e", success=False)
            async with engine.begin() as c:
                await c.execute(text("DELETE FROM best_price_events"))
            migrate("downgrade", "124441561d5e")
            migrate("upgrade", "head")
            migrate("check")
        finally:
            await container.close()
        print(
            "PASS: M3A -> real M3B -> M4A -> M4A.1 -> M4B -> M4C -> M4D -> M4E; "
            "users/products/identifiers/stores/offers/"
            "observations/trackers/watches/discoveries/best states/history/outbox/"
            "subscriptions/payments/FX/outbound clicks preserved; "
            "BE/DE market migration; nullable direct URL; "
            "ambiguous history preserved; scoped baselines rebuilt without alerts; "
            "existing merchant review preserved; version/audit backfill; append-only guard; "
            "canonical merchant 1:1 backfill and source IDs preserved; "
            "pre-link BE/DE customer comparisons equivalent; "
            "canonical append-only audit and used-identity downgrade guard; "
            "M4D data/audits preserved; immutable pilot reports/override audit; "
            "evidence downgrade guard; "
            "guarded downgrade/re-upgrade; schema check"
        )
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


if __name__ == "__main__":
    asyncio.run(main())

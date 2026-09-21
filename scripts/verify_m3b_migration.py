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
from pricehunter.db.models import NotificationEvent, PaymentEvent, Subscription

name = "pricehunter_m3b_" + uuid4().hex[:10] + "_test"
test_url = make_url(os.environ["TEST_DATABASE_URL"])
if not (test_url.database or "").endswith("_test"):
    raise RuntimeError("TEST_DATABASE_URL must name a dedicated database ending _test")
scratch_url = test_url.set(database=name).render_as_string(hide_password=False)
env = dict(os.environ, DATABASE_URL=scratch_url, PYTHONPATH="src")
ids = [uuid4() for _ in range(6)]


def migrate(*args, success=True):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True
    )
    if success:
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
    else:
        assert result.returncode and "Export best-price history" in result.stderr


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

        async def snapshot():
            async with engine.connect() as c:
                return {
                    table: [
                        dict(r)
                        for r in (
                            await c.execute(text(f"SELECT * FROM {table} ORDER BY id"))
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
                    )
                }

        original = await snapshot()
        migrate("upgrade", "head")
        await engine.dispose()
        settings = Settings(_env_file=None, database_url=scratch_url)
        container = Container(settings, redis=fakeredis.aioredis.FakeRedis())
        try:
            after = await snapshot()
            for table, rows in original.items():
                for before, current in zip(rows, after[table], strict=True):
                    assert {k: v for k, v in before.items() if k != "updated_at"} == {
                        k: current[k] for k in before if k != "updated_at"
                    }, table
            assert await container.comparison_operations.maintain() == 1
            assert await container.comparison_operations.maintain() == 0
            assert await container.discovery.synchronize() == 1
            migrate("downgrade", "124441561d5e", success=False)
            async with engine.begin() as c:
                await c.execute(text("DELETE FROM best_price_events"))
            migrate("downgrade", "124441561d5e")
            migrate("upgrade", "head")
            migrate("check")
        finally:
            await container.close()
        print(
            "PASS: fresh migration chain; M3A users/offers/trackers/watches/history/"
            "outbox/subscription/payment preserved; best-state backfill idempotent; "
            "guarded downgrade/re-upgrade; schema check"
        )
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


if __name__ == "__main__":
    asyncio.run(main())

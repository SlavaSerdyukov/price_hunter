import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import structlog
from aiogram import Bot
from aiogram.types import Update
from redis.exceptions import ConnectionError
from sqlalchemy import select, text, update

from pricehunter.api.app import create_app
from pricehunter.bot.app import create_dispatcher
from pricehunter.db.base import utcnow
from pricehunter.db.models import FeedSyncState, MerchantProgramValidation
from pricehunter.domain.feeds import FeedError
from pricehunter.localization.messages import tr
from pricehunter.providers.feeds.base import FeedSource
from tests.integration.test_api import api_client
from tests.integration.test_commerce_feeds import row
from tests.integration.test_merchant_pilot import reviewed, validation
from tests.telegram import FakeTelegramSession

pytestmark = pytest.mark.integration


def telegram_update(number, identity=123, **fields):
    sender = {"id": identity, "is_bot": False, "first_name": "Private", "language_code": "en"}
    if "pre_checkout_query" in fields:
        return Update.model_validate(
            {
                "update_id": number,
                "pre_checkout_query": {
                    "id": str(number),
                    "from": sender,
                    **fields["pre_checkout_query"],
                },
            }
        )
    return Update.model_validate(
        {
            "update_id": number,
            "message": {
                "message_id": number,
                "date": int(utcnow().timestamp()),
                "chat": {"id": identity, "type": "private"},
                "from": sender,
                **fields,
            },
        }
    )


async def test_remote_validation_has_no_open_session_transaction(container, monkeypatch):
    program = await reviewed(container)
    opened = []
    factory = container.sessions

    class RecordingSessions:
        def __call__(self):
            session = factory()
            opened.append(session)
            return session

        @asynccontextmanager
        async def begin(self):
            async with self() as session, session.begin():
                yield session

    monkeypatch.setattr(container.feed_validation, "sessions", RecordingSessions())

    class Remote(FeedSource):
        name = "awin"

        async def source_version(self, program):
            assert not any(s.in_transaction() for s in opened)
            return "v1"

        async def stream_items(self, program):
            assert not any(s.in_transaction() for s in opened)
            yield row()

    result = await container.feed_validation.run(program.id, Remote())
    assert result.status == "passed"


async def test_expired_validation_is_fenced_after_newer_success(container):
    assert "validation_token" in FeedSyncState.__table__.columns
    program = await reviewed(container)
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(FeedSource):
        name = "awin"

        async def stream_items(self, program):
            entered.set()
            await release.wait()
            yield row()

    task = asyncio.create_task(container.feed_validation.run(program.id, Slow()))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        async with container.sessions.begin() as session:
            await session.execute(
                update(FeedSyncState)
                .where(FeedSyncState.merchant_program_id == program.id)
                .values(validation_lease_until=utcnow() - timedelta(seconds=1))
            )
        newer = await validation(container, program)
    finally:
        release.set()
    with pytest.raises(FeedError, match="validation_lease_lost"):
        await task
    async with container.sessions() as session:
        assert list(await session.scalars(select(MerchantProgramValidation.id))) == [newer.id]
    assert (
        await container.merchant_programs.activate(
            program.id, expected_version=program.version, reason="Fenced evidence"
        )
    ).active


@pytest.mark.parametrize("incompatible", ["behind", "ahead"])
async def test_schema_mismatch_fails_shared_preflight_without_schema_mutation(
    container, incompatible
):
    from pricehunter.services.runtime import RuntimePreflight, RuntimePreflightError

    async with container.sessions.begin() as session:
        await session.execute(
            text("UPDATE alembic_version SET version_num=:version"), {"version": incompatible}
        )
    try:
        with pytest.raises(RuntimePreflightError, match="schema_mismatch"):
            await RuntimePreflight(container).run()
        app = create_app(container.settings, container)
        with pytest.raises(RuntimePreflightError, match="schema_mismatch"):
            async with app.router.lifespan_context(app):
                pytest.fail("stale schema started")
    finally:
        from pricehunter.core.schema import EXPECTED_SCHEMA_HEAD

        async with container.sessions.begin() as session:
            await session.execute(
                text("UPDATE alembic_version SET version_num=:head"), {"head": EXPECTED_SCHEMA_HEAD}
            )


async def test_telegram_and_api_share_three_request_budget(container):
    container.settings.user_requests_per_minute = 3
    session = FakeTelegramSession()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=session)
    dispatcher = create_dispatcher(container)
    async with await api_client(container, 123) as client:
        await dispatcher.feed_update(bot, telegram_update(1, text="/plans"))
        assert (await client.get("/api/v1/subscriptions/me")).status_code == 200
        assert (await client.get("/api/v1/subscriptions/me")).status_code == 200
        await dispatcher.feed_update(bot, telegram_update(4, text="/plans"))
        assert session.sent[-1].text == tr("en", "rate_limit")
        response = await client.get("/api/v1/subscriptions/me")
        assert response.status_code == 429 and response.json() == {"error": "rate_limit"}
        assert response.headers["Retry-After"] == "60"
    async with await api_client(container, 124) as client:
        assert (await client.get("/api/v1/subscriptions/me")).status_code == 200
    await dispatcher.storage.close()
    await bot.session.close()


async def test_main_router_command_and_callback_consume_one_budget_each(container):
    from pricehunter.bot.keyboards import Action

    container.settings.user_requests_per_minute = 3
    session = FakeTelegramSession()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=session)
    dispatcher = create_dispatcher(container)
    async with await api_client(container, 123) as client:
        user = await container.users.telegram(123)
        await dispatcher.feed_update(bot, telegram_update(1, text="/start"))
        assert int(await container.redis.get(f"ph:rate:user:{user.id}")) == 1
        callback = Update.model_validate(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "2",
                    "from": {"id": 123, "is_bot": False, "first_name": "Private"},
                    "chat_instance": "test",
                    "data": Action(action="my").pack(),
                    "message": session.sent[-1].model_dump(mode="json"),
                },
            }
        )
        await dispatcher.feed_update(bot, callback)
        assert int(await container.redis.get(f"ph:rate:user:{user.id}")) == 2
        assert (await client.get("/api/v1/subscriptions/me")).status_code == 200
        assert (await client.get("/api/v1/subscriptions/me")).status_code == 429
    await dispatcher.storage.close()
    await bot.session.close()


async def test_financial_updates_bypass_limiter_and_fsm(container, monkeypatch):
    limiter = AsyncMock(side_effect=ConnectionError("SECRET"))
    monkeypatch.setattr(container.limiter, "user", limiter)
    intake = AsyncMock(return_value="receipt")
    monkeypatch.setattr(container.billing_intake, "receive", intake)
    monkeypatch.setattr(container.billing_intake, "process", AsyncMock(return_value=None))
    monkeypatch.setattr(container.billing_intake, "status", AsyncMock(return_value="processed"))
    bot = Bot(
        container.settings.telegram_bot_token.get_secret_value(), session=FakeTelegramSession()
    )
    dispatcher = create_dispatcher(container)
    payment = {
        "currency": "XTR",
        "total_amount": 250,
        "invoice_payload": "ph2:" + "a" * 32,
        "telegram_payment_charge_id": "charge",
        "provider_payment_charge_id": "",
    }
    await dispatcher.feed_update(
        bot,
        telegram_update(
            1,
            pre_checkout_query={
                "currency": "XTR",
                "total_amount": 250,
                "invoice_payload": "invalid",
            },
        ),
    )
    await dispatcher.feed_update(bot, telegram_update(2, successful_payment=payment))
    await dispatcher.feed_update(bot, telegram_update(3, refunded_payment=payment))
    limiter.assert_not_awaited()
    assert intake.await_count == 2
    await dispatcher.feed_update(bot, telegram_update(4, text="/plans"))
    limiter.assert_awaited_once()
    await dispatcher.storage.close()
    await bot.session.close()


async def test_redis_outage_is_safe_on_live_ready_and_authenticated_api(container, monkeypatch):
    async with await api_client(container, 123) as client:
        monkeypatch.setattr(
            container.redis, "eval", AsyncMock(side_effect=ConnectionError("SECRET redis URL"))
        )
        monkeypatch.setattr(
            container.redis, "ping", AsyncMock(side_effect=ConnectionError("SECRET"))
        )
        assert (await client.get("/health/live")).status_code == 200
        response = await client.get("/health/ready")
        assert response.status_code == 503 and response.json() == {"status": "not_ready"}
        response = await client.get("/api/v1/subscriptions/me")
        assert response.status_code == 503 and response.json() == {"error": "service_unavailable"}
        assert "SECRET" not in response.text


async def test_concurrent_http_context_and_error_headers_are_isolated(container):
    app = create_app(container.settings, container)
    contexts = []
    arrived = asyncio.Event()

    @app.get("/context-test")
    async def context_test():
        contexts.append(dict(structlog.contextvars.get_contextvars()))
        if len(contexts) == 2:
            arrived.set()
        await asyncio.wait_for(arrived.wait(), 3)
        return structlog.contextvars.get_contextvars()

    structlog.contextvars.clear_contextvars()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        first, second = await asyncio.gather(
            client.get("/context-test", headers={"X-Request-ID": "PRIVATE arbitrary"}),
            client.get("/context-test"),
        )
        for response in (first, second):
            assert response.status_code == 200
            assert str(UUID(response.headers["X-Request-ID"])) == response.json()["request_id"]
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Referrer-Policy"] == "no-referrer"
        assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
        assert structlog.contextvars.get_contextvars() == {}
        denied = await client.get("/api/v1/subscriptions/me")
        assert denied.status_code == 401 and denied.headers["Cache-Control"] == "no-store"
        assert "X-Request-ID" in denied.headers

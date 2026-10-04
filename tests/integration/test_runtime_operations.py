import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import structlog
from aiogram import Bot
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from redis.exceptions import ConnectionError
from sqlalchemy import text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pricehunter.api.app import create_app
from pricehunter.bot.app import create_dispatcher
from pricehunter.core.config import Settings
from pricehunter.core.container import Container
from pricehunter.core.schema import EXPECTED_SCHEMA_HEAD
from pricehunter.db.base import utcnow
from pricehunter.db.models import FeedSyncState, NotificationEvent
from pricehunter.jobs import worker
from pricehunter.localization.languages import SUPPORTED_LANGUAGES
from pricehunter.localization.messages import tr
from pricehunter.schemas.api import TrackerCreate, UserSettingsPatch
from pricehunter.services.notification_service import NotificationService
from pricehunter.services.runtime import RuntimePreflightError, RuntimeStatus
from tests.integration.test_api import api_client
from tests.integration.test_merchant_pilot import reviewed, validation
from tests.integration.test_runtime_acceptance import telegram_update
from tests.telegram import FakeTelegramSession

pytestmark = pytest.mark.integration


def production_settings(**values):
    return Settings(
        _env_file=None,
        environment="production",
        mock_provider_enabled=False,
        allowed_hosts=["testserver", "127.0.0.1"],
        **values,
    )


async def test_real_production_lifespan_only_checks_local_dependencies(
    sessions, redis, monkeypatch
):
    resources = Container(production_settings(), sessions=sessions, redis=redis)
    network = AsyncMock(side_effect=AssertionError("startup attempted external I/O"))
    monkeypatch.setattr(resources.http, "send", network)
    app = create_app(resources.settings, resources)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                assert (await client.get("/health/live")).status_code == 200
                assert (await client.get("/health/ready")).json() == {"status": "ready"}
                assert (await client.get("/docs")).status_code == 404
                assert (await client.get("/openapi.json")).status_code == 404
                network.assert_not_awaited()
        assert not resources.http.is_closed  # Injected container remains caller-owned.
    finally:
        await resources.close()


async def test_disposable_behind_schema_refuses_boot_then_upgrade_succeeds(redis):
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.database.endswith("_test")
    name = "pricehunter_m5a_" + uuid4().hex[:12] + "_test"
    scratch = url.set(database=name).render_as_string(hide_password=False)
    admin = create_async_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    engine = create_async_engine(scratch, hide_parameters=True)
    settings = production_settings(database_url=SecretStr(scratch))
    resources = Container(
        settings, sessions=async_sessionmaker(engine, expire_on_commit=False), redis=redis
    )
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))

    async def migrate(target):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            target,
            env={**os.environ, "DATABASE_URL": scratch, "ENVIRONMENT": "test"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        assert process.returncode == 0

    try:
        scripts = ScriptDirectory.from_config(Config("alembic.ini"))
        previous = scripts.get_revision(EXPECTED_SCHEMA_HEAD).down_revision
        await migrate(previous)
        app = create_app(settings, resources)
        with pytest.raises(RuntimePreflightError, match="schema_mismatch"):
            await resources.runtime.run()
        with pytest.raises(RuntimePreflightError, match="schema_mismatch"):
            async with app.router.lifespan_context(app):
                pytest.fail("behind schema booted")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/health/ready")
            assert response.status_code == 503 and response.json() == {"status": "not_ready"}
        await engine.dispose()
        await migrate("head")
        async with app.router.lifespan_context(app):
            await resources.runtime.run()
        # New guard protects an in-flight validation without changing existing evidence guards.
        async with engine.begin() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == EXPECTED_SCHEMA_HEAD
            )
    finally:
        await resources.close()
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_preflight_failure_is_bounded_and_safe(container, monkeypatch):
    monkeypatch.setattr(
        container.runtime, "schema_versions", AsyncMock(side_effect=OSError("SECRET database URL"))
    )
    assert not await container.runtime.ready()
    monkeypatch.setattr(
        container.runtime, "schema_versions", AsyncMock(return_value=[EXPECTED_SCHEMA_HEAD])
    )
    monkeypatch.setattr(container.redis, "ping", AsyncMock(return_value=False))
    with pytest.raises(RuntimePreflightError, match="redis_unavailable"):
        await container.runtime.run()
    monkeypatch.setattr(container.redis, "ping", AsyncMock(return_value=True))
    monkeypatch.setattr(container, "validate_feeds", AsyncMock(side_effect=ValueError("SECRET")))
    with pytest.raises(RuntimePreflightError, match="configuration_invalid"):
        await container.runtime.run()


async def test_preflight_timeout_and_cancellation_do_not_swallow_each_other(container, monkeypatch):
    class ImmediateTimeout:
        async def __aenter__(self):
            raise TimeoutError("SECRET")

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(
        "pricehunter.services.runtime.asyncio.timeout", lambda seconds: ImmediateTimeout()
    )
    with pytest.raises(RuntimePreflightError, match="dependency_timeout"):
        await container.runtime.run()


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_telegram_redis_outage_is_localized_and_context_clean(
    container, monkeypatch, language
):
    user = await container.users.telegram(123)
    await container.users.settings(user.id, UserSettingsPatch(language_code=language))
    session = FakeTelegramSession()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=session)
    dispatcher = create_dispatcher(container)
    monkeypatch.setattr(container.redis, "eval", AsyncMock(side_effect=ConnectionError("SECRET")))
    await dispatcher.feed_update(bot, telegram_update(901, text="/plans"))
    assert session.sent[-1].text == tr(language, "service_unavailable")
    assert "telegram_update_id" not in structlog.contextvars.get_contextvars()
    await dispatcher.storage.close()
    await bot.session.close()


async def test_telegram_fsm_outage_is_safe_and_does_not_run_command(container, monkeypatch):
    session = FakeTelegramSession()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=session)
    dispatcher = create_dispatcher(container)
    monkeypatch.setattr(
        dispatcher.fsm.storage, "get_state", AsyncMock(side_effect=ConnectionError("SECRET"))
    )
    await dispatcher.feed_update(bot, telegram_update(901, text="/plans"))
    assert session.sent[-1].text == tr("en", "service_unavailable")
    await dispatcher.storage.close()
    await bot.session.close()


async def test_runtime_status_is_aggregate_read_only_and_exposes_expired_validation(container):
    program = await reviewed(container)
    await validation(container, program)
    async with container.sessions.begin() as session:
        await session.execute(
            update(FeedSyncState)
            .where(FeedSyncState.merchant_program_id == program.id)
            .values(
                validation_token=uuid4(),
                validation_lease_until=utcnow() - timedelta(seconds=1),
                failure_count=1,
                rejected_report={
                    "report": {
                        "quality_error": "quality_shrink",
                        "failure_kind": "publication_quality_failed",
                    }
                },
                last_failure_at=utcnow(),
            )
        )
    report = await RuntimeStatus(container).report()
    assert report["schema"]["compatible"] and report["redis"]["available"]
    assert report["counts"]["expired_validation_leases"] == 1
    assert report["counts"]["failed_feed_syncs"] == 1
    assert report["counts"]["last_quality_rejected_programs"] == [str(program.id)]
    assert report["counts"]["programs_without_current_validation"] == 0
    assert "validation_token" not in json.dumps(report)
    async with container.sessions() as session:
        assert (await session.get(FeedSyncState, program.id)).validation_token is not None
    await container.merchant_programs.change_feed_reference(
        program.id, "992", expected_version=program.version, reason="Configuration change"
    )
    assert (await RuntimeStatus(container).report())["counts"][
        "programs_without_current_validation"
    ] == 1


async def test_runtime_status_survives_redis_and_schema_failure(container, monkeypatch):
    monkeypatch.setattr(container.redis, "ping", AsyncMock(side_effect=ConnectionError("SECRET")))
    report = await RuntimeStatus(container).report()
    assert report["schema"]["compatible"] and not report["redis"]["available"]
    assert "counts" in report and "SECRET" not in json.dumps(report)
    monkeypatch.setattr(
        "pricehunter.services.runtime.RuntimePreflight.schema_versions",
        AsyncMock(return_value=["ahead"]),
    )
    report = await RuntimeStatus(container).report()
    assert not report["schema"]["compatible"] and "counts" not in report


async def test_stale_notification_startup_recovery_never_resends_uncertain(container):
    user = await container.users.telegram(123)
    offer = await container.products.resolve(
        "https://mock.pricehunter.test/products/headphones", user.id
    )
    await container.trackers.create(user.id, TrackerCreate(store_offer_id=offer.id))
    await container.price_checks.refresh((await container.price_checks.claim_due())[0])
    async with container.sessions.begin() as session:
        await session.execute(
            update(NotificationEvent).values(
                status="sending", attempt_started_at=utcnow() - timedelta(minutes=3)
            )
        )
    sender = AsyncMock()
    service = NotificationService(
        container.sessions, sender, container.entitlements, container.settings
    )
    assert await service.recover_stale() == 1
    assert await service.recover_stale() == 0
    assert await service.send_pending() == 0
    sender.send.assert_not_awaited()


async def test_owned_container_and_worker_shutdown_are_idempotent(container, monkeypatch):
    external_redis = AsyncMock()
    engine = container.sessions.kw["bind"]
    disposed = []
    original_dispose = type(engine).dispose

    async def dispose(target, *args, **kwargs):
        disposed.append(target)
        await original_dispose(target, *args, **kwargs)

    monkeypatch.setattr(container.redis, "aclose", external_redis)
    monkeypatch.setattr(type(engine), "dispose", dispose)
    await container.close()
    await container.close()
    assert container.http.is_closed
    external_redis.assert_not_awaited()
    assert engine not in disposed
    resources = Container(Settings(_env_file=None))
    owned_redis, owned_http = AsyncMock(), AsyncMock()
    owned_engine = resources.sessions.kw["bind"]
    monkeypatch.setattr(resources.redis, "aclose", owned_redis)
    monkeypatch.setattr(resources.http, "aclose", owned_http)
    bot = AsyncMock()
    ctx = {"container": resources, "bot": bot}
    await worker.shutdown(ctx)
    await worker.shutdown(ctx)
    owned_redis.assert_awaited_once()
    assert disposed.count(owned_engine) == 1
    owned_http.assert_awaited_once()
    bot.session.close.assert_awaited_once()


async def test_worker_and_bot_startup_use_preflight_and_close_on_failure(container, monkeypatch):
    from pricehunter.apps import bot as bot_app

    check = AsyncMock(side_effect=RuntimePreflightError("schema_mismatch"))
    close = AsyncMock()
    monkeypatch.setattr(container.runtime, "run", check)
    monkeypatch.setattr(container, "close", close)
    monkeypatch.setattr(worker, "get_settings", lambda: container.settings)
    monkeypatch.setattr(worker, "Container", lambda settings: container)
    monkeypatch.setattr(bot_app, "get_settings", lambda: container.settings)
    monkeypatch.setattr(bot_app, "Container", lambda settings: container)
    for action in (worker.startup({}), bot_app.main()):
        with pytest.raises(RuntimePreflightError, match="schema_mismatch"):
            await action
    assert check.await_count == 2 and close.await_count == 2


async def test_operator_commands_use_shared_preflight_and_safe_nonzero_failure(
    container, monkeypatch, capsys
):
    from pricehunter.apps import admin

    monkeypatch.setattr(admin, "Container", lambda settings: container)
    monkeypatch.setattr(admin, "get_settings", lambda: container.settings)
    monkeypatch.setattr(container, "close", AsyncMock())
    await admin.run(argparse.Namespace(command="runtime-preflight"))
    assert json.loads(capsys.readouterr().out) == {"status": "ready"}
    await admin.run(argparse.Namespace(command="runtime-status"))
    assert json.loads(capsys.readouterr().out)["schema"]["compatible"]
    monkeypatch.setattr(
        container.runtime, "run", AsyncMock(side_effect=RuntimePreflightError("redis_unavailable"))
    )
    with pytest.raises(SystemExit) as exc:
        await admin.run(argparse.Namespace(command="runtime-preflight"))
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "not_ready",
        "reason": "redis_unavailable",
    }


async def test_private_error_responses_keep_headers_and_safe_logs(container, capsys):
    app = create_app(container.settings, container)

    @app.get("/api/error-test")
    async def broken():
        structlog.get_logger().info("request_context_probe")
        raise RuntimeError("SECRET bearer or URL")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/api/error-test", headers={"Authorization": "Bearer SECRET", "X-Request-ID": "SECRET"}
        )
        assert response.status_code == 500 and response.json() == {"error": "unexpected_error"}
        assert response.headers["Cache-Control"] == "no-store"
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert all(event["request_id"] == response.headers["X-Request-ID"] for event in events)
        assert "SECRET" not in json.dumps(events)
    assert "request_id" not in structlog.contextvars.get_contextvars()


@pytest.mark.parametrize(
    "path,parameters",
    [
        ("/api/v1/trackers", {"size": 51}),
        ("/api/v1/product-watches", {"page": 10001}),
        ("/api/v1/search", {"q": "x" * 201}),
        ("/api/v1/search", {"q": ""}),
    ],
)
async def test_user_controlled_body_and_query_bounds_retained(container, path, parameters):
    async with await api_client(container, 123) as client:
        assert (await client.get(path, params=parameters)).status_code == 422
        response = await client.post("/api/v1/products/resolve", content=b"x" * 65537)
        assert response.status_code == 413
        assert response.headers["Cache-Control"] == "no-store"


async def test_search_consumes_exactly_one_ingress_budget(container):
    container.settings.user_requests_per_minute = 2
    async with await api_client(container, 123) as client:
        assert (await client.get("/api/v1/search", params={"q": "headphones"})).status_code == 200
        assert (await client.get("/api/v1/subscriptions/me")).status_code == 200
        assert (await client.get("/api/v1/subscriptions/me")).status_code == 429


async def test_telegram_update_and_worker_contexts_are_independent(container, monkeypatch):
    contexts = []

    async def retry():
        contexts.append(structlog.contextvars.get_contextvars())
        return 0

    monkeypatch.setattr(container.billing_intake, "retry_pending", retry)
    monkeypatch.setattr(container.billing, "expire", AsyncMock())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="caller")
    for _ in range(2):
        await worker.maintain_billing({"container": container, "job_id": "stable-job"})
    assert contexts[0] == contexts[1]
    assert contexts[0]["operation"] == "maintain_billing" and "request_id" not in contexts[0]
    assert structlog.contextvars.get_contextvars() == {"request_id": "caller"}
    structlog.contextvars.clear_contextvars()


async def test_validation_cancellation_releases_only_own_lease(container):
    from pricehunter.providers.feeds.base import FeedSource

    program = await reviewed(container)
    entered = asyncio.Event()

    class Never(FeedSource):
        name = "awin"

        async def stream_items(self, program):
            entered.set()
            await asyncio.Event().wait()
            yield None

    task = asyncio.create_task(container.feed_validation.run(program.id, Never()))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await validation(container, program)).status == "passed"


async def test_api_owned_lifespan_closes_resources_even_on_startup_failure(container, monkeypatch):
    monkeypatch.setattr("pricehunter.api.app.Container", lambda settings: container)
    close = AsyncMock(wraps=container.close)
    monkeypatch.setattr(container, "close", close)
    monkeypatch.setattr(
        container.runtime, "run", AsyncMock(side_effect=RuntimePreflightError("schema_mismatch"))
    )
    app = create_app(container.settings)
    with pytest.raises(RuntimePreflightError, match="schema_mismatch"):
        async with app.router.lifespan_context(app):
            pytest.fail("unexpected startup")
    close.assert_awaited_once()
    assert container.http.is_closed


async def test_worker_successful_startup_and_shutdown_closes_telegram(container, monkeypatch):
    session = FakeTelegramSession()
    session.close = AsyncMock()
    bot = Bot(container.settings.telegram_bot_token.get_secret_value(), session=session)
    monkeypatch.setattr(worker, "Container", lambda settings: container)
    monkeypatch.setattr(worker, "get_settings", lambda: container.settings)
    monkeypatch.setattr(worker, "create_bot", lambda resources: bot)
    ctx = {}
    await worker.startup(ctx)
    assert ctx["container"] is container and ctx["notifications"] is not None
    await worker.shutdown(ctx)
    await worker.shutdown(ctx)
    session.close.assert_awaited_once()
    assert container.http.is_closed


async def test_worker_exception_logs_safe_reason_and_cleans_scope(container, monkeypatch, capsys):
    monkeypatch.setattr(
        container.billing_intake,
        "retry_pending",
        AsyncMock(side_effect=OSError("SECRET URL/payment")),
    )
    with pytest.raises(RuntimeError, match="worker_operation_failed"):
        await worker.maintain_billing({"container": container, "job_id": "safe-correlation"})
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert "SECRET" not in json.dumps(events)
    assert all(event["job_id"] == events[0]["job_id"] for event in events)
    assert structlog.contextvars.get_contextvars() == {}

from unittest.mock import AsyncMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from pricehunter.core.config import Settings
from pricehunter.core.schema import EXPECTED_SCHEMA_HEAD
from pricehunter.db.session import create_sessions
from pricehunter.jobs.worker import WorkerSettings


def test_packaged_schema_contract_matches_single_alembic_head():
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [EXPECTED_SCHEMA_HEAD]


@pytest.mark.parametrize(
    "values",
    [
        {"mock_provider_enabled": True},
        {"allowed_hosts": ["*"]},
        {"allowed_hosts": ["*.example.com"]},
        {"allowed_hosts": []},
        {"telegram_mode": "webhook"},
        {"telegram_mode": "webhook", "telegram_webhook_secret": "x" * 32},
        {"ebay_enabled": True},
        {"rakuten_enabled": True},
        {"awin_enabled": True},
        {"tradedoubler_enabled": True},
        {"awin_enabled": True, "awin_feed_api_key": "test"},
        {"awin_enabled": True, "awin_feed_api_key": "test", "feed_program_ids": ["bad"]},
        {"validation_timeout_seconds": 7200, "validation_lease_seconds": 7200},
    ],
)
def test_unsafe_production_configuration_rejected(values):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            **(
                {
                    "environment": "production",
                    "mock_provider_enabled": False,
                    "allowed_hosts": ["api.example.com"],
                }
                | values
            ),
        )


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"ebay_enabled": True, "ebay_client_id": "test", "ebay_client_secret": "test"},
        {"woocommerce_stores": ["pine64_eu"]},
    ],
)
def test_minimal_production_providers_are_optional(values):
    assert (
        Settings(
            _env_file=None,
            environment="production",
            mock_provider_enabled=False,
            allowed_hosts=["api.example.com"],
            **values,
        ).environment
        == "production"
    )


async def test_database_pool_limits_and_timeout_are_explicit(monkeypatch):
    from pricehunter.db import session

    settings = Settings(
        _env_file=None,
        database_pool_size=3,
        database_max_overflow=2,
        database_pool_timeout_seconds=7,
        database_pool_recycle_seconds=600,
        database_command_timeout_seconds=120,
    )
    original = session.create_async_engine
    captured = {}

    def create(url, **options):
        captured.update(options)
        return original(url, **options)

    monkeypatch.setattr(session, "create_async_engine", create)
    sessions = create_sessions(settings)
    try:
        assert captured["pool_size"] == 3 and captured["max_overflow"] == 2
        assert captured["pool_timeout"] == 7 and captured["pool_recycle"] == 600
        assert captured["connect_args"] == {"command_timeout": 120}
        assert captured["hide_parameters"]
    finally:
        await sessions.kw["bind"].dispose()


def test_worker_timeouts_are_per_operation_and_bound_notification_batch():
    timeouts = {
        getattr(job, "name", getattr(job, "__name__", "")): getattr(job, "timeout_s", None)
        for job in WorkerSettings.functions
    }
    assert WorkerSettings.job_timeout == 60
    assert timeouts["sync_feed"] == 7200
    assert timeouts["send_notifications"] == 300
    assert timeouts["refresh_offer"] == 120 and timeouts["discover_product"] == 180
    notification_cron = next(
        job for job in WorkerSettings.cron_jobs if "send_notifications" in job.name
    )
    assert notification_cron.timeout_s == 300


@pytest.mark.parametrize("operation", ["provider", "offer_refresh"])
async def test_other_redis_limits_fail_with_safe_domain_error(redis, monkeypatch, operation):
    from redis.exceptions import ConnectionError

    from pricehunter.core.limits import RateLimiter
    from pricehunter.domain.errors import ServiceUnavailableError

    limiter = RateLimiter(redis, Settings(_env_file=None))
    failure = AsyncMock(side_effect=ConnectionError("SECRET URL"))
    monkeypatch.setattr(redis, "eval" if operation == "provider" else "set", failure)
    with pytest.raises(ServiceUnavailableError):
        async with getattr(limiter, operation)("test"):
            pytest.fail("outage accepted")


def test_environment_validation_errors_cannot_print_credentials(monkeypatch):
    from pricehunter.core import config

    error = ValidationError.from_exception_data(
        "Settings",
        [
            {
                "type": "int_parsing",
                "loc": ("database_pool_size",),
                "input": "SECRET-env-value",
            }
        ],
    )

    def invalid():
        raise error

    monkeypatch.setattr(config, "Settings", invalid)
    config.get_settings.cache_clear()
    try:
        with pytest.raises(config.RuntimeConfigurationError) as exc:
            config.get_settings()
        assert str(exc.value) == "configuration_invalid"
    finally:
        config.get_settings.cache_clear()

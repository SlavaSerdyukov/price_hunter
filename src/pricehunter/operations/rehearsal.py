"""Repeatable synthetic operator runs. No .env, production data or external APIs."""

import asyncio
import json
import logging
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import structlog
import uvicorn
from pydantic import SecretStr

from pricehunter.api.app import create_app
from pricehunter.core.config import Settings
from pricehunter.core.container import Container
from pricehunter.domain.provider_policy import SYNTHETIC_POLICY
from pricehunter.operations.crashes import crash_rehearsal
from pricehunter.operations.errors import RecoveryError
from pricehunter.operations.integrity import probe_immutability
from pricehunter.operations.load import PROFILES, run_load
from pricehunter.operations.recovery import Recovery, database_url
from pricehunter.operations.synthetic import redis_loss, replay, restored_container, seed


def synthetic_settings(source: str, redis_url: str) -> Settings:
    url = database_url(source)
    redis = urlsplit(redis_url)
    if (
        url.host not in ("localhost", "127.0.0.1")
        or not (url.database or "").endswith("_test")
        or redis.scheme != "redis"
        or redis.hostname not in ("localhost", "127.0.0.1")
        or redis.path != "/14"
    ):
        raise RecoveryError("synthetic_local_dependencies_required")
    return Settings(
        _env_file=None,
        environment="test",
        database_url=SecretStr(source),
        redis_url=SecretStr(redis_url),
        telegram_bot_token=SecretStr("123456789:TEST_TOKEN_FOR_LOCAL_TESTS_ONLY_12345"),
        telegram_mode="polling",
        mock_provider_enabled=True,
        provider_data_policies={"mock": SYNTHETIC_POLICY},
        ebay_enabled=False,
        amazon_enabled=False,
        rakuten_enabled=False,
        awin_enabled=False,
        tradedoubler_enabled=False,
        cj_enabled=False,
        woocommerce_stores=[],
        feed_program_ids=[],
        fx_enabled=False,
        log_level="ERROR",
        allowed_hosts=["localhost", "127.0.0.1", "rehearsal", "testserver"],
    )


def dependencies() -> tuple[str, str]:
    source = os.environ.get("REHEARSAL_DATABASE_URL", "")
    redis = os.environ.get("REHEARSAL_REDIS_URL", "")
    if not source or not redis:
        raise RecoveryError("rehearsal_environment_missing")
    return source, redis


async def recovery_rehearsal() -> dict[str, object]:
    source, redis = dependencies()
    target = os.environ.get("RESTORE_DATABASE_URL", "")
    if not target:
        raise RecoveryError("target_environment_missing")
    container = Container(synthetic_settings(source, redis))
    try:
        await container.runtime.run()
        await container.redis.flushdb()  # dedicated synthetic loopback Redis DB 14 only
        dataset = await seed(container)
        with tempfile.TemporaryDirectory(prefix="ph-rehearsal-") as directory:
            path = Path(directory) / "snapshot.dump"
            recovery = Recovery(source)
            await recovery.create(path, revision=os.environ.get("GITHUB_SHA"))
            report = await recovery.restore(path, target, confirm_disposable=True)
            async with restored_container(container, target) as restored:
                await probe_immutability(restored.sessions)
                await replay(restored, dataset)
                await redis_loss(restored)
                crashes = await crash_rehearsal(restored, dataset)
            return {
                "recovery": report.model_dump(),
                "replay_idempotent": True,
                "redis_loss_passed": True,
                "crashes": crashes,
            }
    finally:
        await container.close()


async def http_rehearsal(profile: str) -> dict[str, object]:
    source, redis = dependencies()
    if profile not in PROFILES:
        raise RecoveryError("load_profile_invalid")
    container = Container(synthetic_settings(source, redis))
    try:
        await container.runtime.run()
        await container.redis.flushdb()  # reset only the explicitly dedicated rehearsal DB
        dataset = await seed(container)
        app = create_app(container.settings, container)
        # Real TCP HTTP on loopback, ephemeral port, normal application lifespan/auth/routes.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(
                    app, log_level="critical", access_log=False, lifespan="on", ws="none"
                )
            )

            async def serve() -> None:
                try:
                    await server.serve(sockets=[listener])
                except SystemExit:
                    raise RecoveryError("load_server_failed") from None

            task = asyncio.create_task(serve())
            try:
                async with asyncio.timeout(10):
                    while not server.started:
                        if task.done():
                            await task
                            raise RecoveryError("load_server_failed")
                        await asyncio.sleep(0.01)
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30
                ) as client:
                    report = await run_load(container, dataset, client, profile)
                if (
                    report.unexpected_failures
                    or not report.response_bounds_passed
                    or (
                        report.checked_out_connections
                        or report.requests != PROFILES[profile].requests
                    )
                ):
                    raise RecoveryError("load_semantic_gate_failed")
                return {"load": report.model_dump()}
            finally:
                server.should_exit = True
                try:
                    await asyncio.wait_for(task, 10)
                except TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    finally:
        await container.close()


def entrypoint(kind: str) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Synthetic local-only M5C rehearsal")
    parser.add_argument("--report", type=Path, required=True)
    if kind == "load":
        parser.add_argument("--profile", choices=tuple(PROFILES), default="smoke")
    arguments = parser.parse_args()
    # Suppress normal synthetic service lifecycle logs (they contain synthetic row IDs).
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        report = asyncio.run(
            http_rehearsal(arguments.profile) if kind == "load" else recovery_rehearsal()
        )
        arguments.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"report": str(arguments.report), "passed": True}))
    except RecoveryError as exc:
        print(json.dumps({"error": exc.code}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({"error": "rehearsal_failed"}))
        raise SystemExit(1) from None

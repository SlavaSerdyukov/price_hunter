"""Real PostgreSQL acceptance tests; never use application/customer databases as targets."""

import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def remove_archives(tmp_path):
    yield
    for path in tmp_path.iterdir():
        if path.name.endswith((".dump", ".manifest.json")):
            path.unlink(missing_ok=True)


@pytest.fixture
async def empty_target():
    source = make_url(os.environ["TEST_DATABASE_URL"])
    name = "ph_m5c_" + uuid4().hex + "_test"
    admin = create_async_engine(source, isolation_level="AUTOCOMMIT", hide_parameters=True)
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield source.set(database=name).render_as_string(hide_password=False)
    finally:
        # Only a database created by this fixture, with an unguessable test-only name.
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


async def backup(container, tmp_path):
    from pricehunter.operations.recovery import Recovery
    from pricehunter.operations.synthetic import seed

    dataset = await seed(container)
    recovery = Recovery(os.environ["TEST_DATABASE_URL"])
    path = tmp_path / "snapshot.dump"
    manifest = await recovery.create(path)
    return recovery, dataset, path, manifest


async def test_restore_nonempty_target_rejected(container, tmp_path, empty_target):
    from pricehunter.operations.recovery import RecoveryError

    recovery, _, path, _ = await backup(container, tmp_path)
    engine = create_async_engine(empty_target)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE must_survive (id integer)"))
    await engine.dispose()
    with pytest.raises(RecoveryError, match="target_not_empty"):
        await recovery.restore(path, empty_target, confirm_disposable=True)


async def test_corrupt_backup_and_manifest_rejected(container, tmp_path):
    from pricehunter.operations.recovery import RecoveryError, manifest_path

    recovery, _, path, _ = await backup(container, tmp_path)
    original = path.read_bytes()
    path.write_bytes(original + b"corruption")
    with pytest.raises(RecoveryError, match="dump_checksum"):
        await recovery.verify(path)
    path.write_bytes(original)
    manifest_path(path).write_text('{"format_version":99}')
    with pytest.raises(RecoveryError, match="manifest_invalid"):
        await recovery.verify(path)


async def test_restore_identity_digests_and_guards(container, tmp_path, empty_target):
    recovery, dataset, path, manifest = await backup(container, tmp_path)
    report = await recovery.restore(path, empty_target, confirm_disposable=True)
    assert report.checksums_matched and report.schema_compatible and report.domain_smoke_passed
    assert report.tables == len(manifest.tables)
    assert manifest.tables["users"].rows >= 3
    assert manifest.tables["payment_events"].rows >= 3
    from pricehunter.operations.integrity import probe_immutability
    from pricehunter.operations.synthetic import restored_container

    async with restored_container(container, empty_target) as restored:
        await probe_immutability(restored.sessions)
        assert (await restored.products.offer(dataset.offer_id)).product_id == dataset.product_id
        from pricehunter.core.security import token_digest

        assert (
            await restored.users.authenticate(token_digest(dataset.api_keys[0]))
        ).id == dataset.user_ids[0]


async def test_restored_replays_are_idempotent(container, tmp_path, empty_target):
    from pricehunter.operations.synthetic import replay, restored_container

    recovery, dataset, path, _ = await backup(container, tmp_path)
    await recovery.restore(path, empty_target, confirm_disposable=True)
    async with restored_container(container, empty_target) as restored:
        await replay(restored, dataset)
        await replay(restored, dataset)


async def test_redis_loss_preserves_durable_state(container):
    from pricehunter.operations.synthetic import redis_loss, seed

    await seed(container)
    await redis_loss(container)


async def test_http_smoke_zero_unexpected_failures_and_pool_return(container):
    from pricehunter.api.app import create_app
    from pricehunter.operations.load import run_load
    from pricehunter.operations.synthetic import seed

    dataset = await seed(container)
    app = create_app(container.settings, container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        report = await run_load(container, dataset, client, "smoke")
    assert report.requests == 56
    assert report.unexpected_failures == 0
    assert report.integrity_passed
    assert container.sessions.kw["bind"].sync_engine.pool.checkedout() == 0


@pytest.mark.parametrize(
    "case",
    [
        "same",
        "confirmation",
        "production",
        "missing_client",
        "restore_failure",
        "wrong_head",
        "wrong_table",
        "wrong_manifest",
    ],
)
async def test_restore_safe_failures(container, tmp_path, empty_target, monkeypatch, case):
    import json

    from pricehunter.operations import recovery as module
    from pricehunter.operations.recovery import RecoveryError, manifest_path

    recovery, _, path, manifest = await backup(container, tmp_path)
    target, confirm, expected = empty_target, True, ""
    if case == "same":
        target, expected = os.environ["TEST_DATABASE_URL"], "source_equals_target"
    elif case == "confirmation":
        confirm, expected = False, "disposable_confirmation_required"
    elif case == "production":
        target = (
            make_url(empty_target).set(database="production").render_as_string(hide_password=False)
        )
        expected = "disposable_confirmation_required"
    elif case == "missing_client":
        recovery.restore_tool, expected = "missing-ph-pg-restore", "postgres_client_missing"
    elif case == "restore_failure":
        original = module.command

        async def failed(executable, arguments, *args, **kwargs):
            if "--single-transaction" in arguments:
                raise RecoveryError("postgres_command_failed")
            return await original(executable, arguments, *args, **kwargs)

        monkeypatch.setattr(module, "command", failed)
        expected = "postgres_command_failed"
    else:
        data = manifest.model_dump(mode="json")
        if case == "wrong_head":
            data["expected_alembic_head"], expected = "old", "schema_mismatch"
        elif case == "wrong_table":
            data["tables"]["payment_events"]["primary_keys_sha256"] = "0" * 64
            expected = "table_checksum_mismatch"
        else:
            data["unexpected_secret"] = "must not print"
            expected = "manifest_invalid"
        manifest_path(path).write_text(json.dumps(data))
    with pytest.raises(RecoveryError, match=expected):
        await recovery.restore(path, target, confirm_disposable=confirm)
    if case != "wrong_table":
        engine = create_async_engine(empty_target)
        async with engine.connect() as connection:
            assert not await connection.scalar(text("SELECT to_regclass('public.users')"))
        await engine.dispose()


async def test_backup_cleanup_and_compatibility(container, tmp_path):
    from pricehunter.operations.recovery import Recovery, RecoveryError, manifest_path

    await container.users.telegram(8877123)
    path = tmp_path / "failed.dump"
    recovery = Recovery(os.environ["TEST_DATABASE_URL"], pg_dump="not-installed-ph-dump")
    with pytest.raises(RecoveryError, match="postgres_client_missing"):
        await recovery.create(path)
    assert not path.exists() and not manifest_path(path).exists()
    with pytest.raises(RecoveryError, match="postgres_version_mismatch"):
        await Recovery(os.environ["TEST_DATABASE_URL"]).client("pg_dump", 12)


async def test_manifest_snapshot_is_consistent_under_application_writes(
    container, tmp_path, empty_target
):
    import asyncio

    from pricehunter.operations.recovery import Recovery
    from pricehunter.operations.synthetic import seed
    from pricehunter.schemas.api import UserSettingsPatch

    dataset = await seed(container)
    stop, entered = asyncio.Event(), asyncio.Event()
    writes = 0

    async def writer():
        nonlocal writes
        entered.set()
        while not stop.is_set():
            await container.users.settings(
                dataset.user_ids[0], UserSettingsPatch(language_code="en" if writes % 2 else "fr")
            )
            writes += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(writer())
    try:
        await entered.wait()
        path = tmp_path / "concurrent.dump"
        recovery = Recovery(os.environ["TEST_DATABASE_URL"])
        manifest = await recovery.create(path)
        assert manifest.tables["users"].rows == 32
    finally:
        stop.set()
        await task
    assert writes > 1
    report = await recovery.restore(path, empty_target, confirm_disposable=True)
    assert report.checksums_matched


async def test_worker_crashes_and_notification_uncertainty(container):
    from pricehunter.operations.crashes import crash_rehearsal
    from pricehunter.operations.synthetic import seed

    dataset = await seed(container)
    assert all((await crash_rehearsal(container, dataset)).values())


async def test_redis_outage_then_loss_recovers_readiness(container, monkeypatch):
    from unittest.mock import AsyncMock

    from redis.exceptions import ConnectionError

    from pricehunter.operations.integrity import state
    from pricehunter.operations.synthetic import redis_loss, seed

    await seed(container)
    before = await state(container.sessions)
    app = __import__("pricehunter.api.app", fromlist=["create_app"]).create_app(
        container.settings, container
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        with monkeypatch.context() as context:
            context.setattr(
                container.redis, "ping", AsyncMock(side_effect=ConnectionError("SECRET"))
            )
            assert (await client.get("/health/ready")).status_code == 503
            assert (await client.get("/health/live")).status_code == 200
        await redis_loss(container)
        assert (await client.get("/health/ready")).status_code == 200
    assert before == await state(container.sessions)


async def test_actual_restore_failure_rolls_back_schema(container, tmp_path, empty_target):
    import json

    from pricehunter.operations.recovery import RecoveryError, file_digest, manifest_path

    recovery, _, path, manifest = await backup(container, tmp_path)
    path.write_bytes(path.read_bytes()[:4096])
    size, digest = file_digest(path)
    data = manifest.model_dump(mode="json")
    data.update(dump_size_bytes=size, dump_sha256=digest)
    manifest_path(path).write_text(json.dumps(data))
    with pytest.raises(RecoveryError, match="postgres_command_failed"):
        await recovery.restore(path, empty_target, confirm_disposable=True)
    engine = create_async_engine(empty_target)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT to_regclass('public.users')")) is None
    await engine.dispose()


async def test_archive_schema_mismatch_is_not_upgraded(container, tmp_path, empty_target):
    import json

    from pricehunter.core.schema import EXPECTED_SCHEMA_HEAD
    from pricehunter.operations.recovery import (
        RecoveryError,
        command,
        file_digest,
        libpq_environment,
        manifest_path,
    )

    recovery, _, path, manifest = await backup(container, tmp_path)
    async with container.sessions.begin() as session:
        await session.execute(text("UPDATE alembic_version SET version_num='old_head'"))
    try:
        with libpq_environment(recovery.url) as environment:
            await command(
                "pg_dump",
                ["--format=custom", "--no-owner", "--no-privileges", f"--file={path}"],
                environment,
                30,
            )
    finally:
        async with container.sessions.begin() as session:
            await session.execute(
                text("UPDATE alembic_version SET version_num=:head"), {"head": EXPECTED_SCHEMA_HEAD}
            )
    size, digest = file_digest(path)
    data = manifest.model_dump(mode="json")
    data.update(dump_size_bytes=size, dump_sha256=digest)
    manifest_path(path).write_text(json.dumps(data))
    with pytest.raises(RecoveryError, match="schema_mismatch"):
        await recovery.restore(path, empty_target, confirm_disposable=True)
    engine = create_async_engine(empty_target)
    async with engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version")) == "old_head"
        )
    await engine.dispose()


async def test_manifest_contains_no_raw_identity_or_tokens(container, tmp_path):
    from pricehunter.operations.recovery import manifest_path

    _, dataset, path, manifest = await backup(container, tmp_path)
    serialized = manifest_path(path).read_text()
    for value in (
        *dataset.api_keys,
        *(str(user) for user in dataset.user_ids),
        str(dataset.product_id),
        dataset.payment.charge_id,
        dataset.payment.payload,
    ):
        assert value not in serialized
    assert "password" not in serialized.lower()
    assert path.stat().st_mode & 0o077 == 0
    assert manifest_path(path).stat().st_mode & 0o077 == 0
    assert len(manifest.tables) == 33


async def test_integrity_detects_disabled_guard_and_unclassified_table(container):
    from pricehunter.operations.errors import RecoveryError
    from pricehunter.operations.integrity import check_connection

    async with container.sessions() as session, session.begin():
        connection = await session.connection()
        await connection.execute(
            text("ALTER TABLE payment_events DISABLE TRIGGER payment_immutable")
        )
        with pytest.raises(RecoveryError, match="immutability_guard_missing"):
            await check_connection(connection)
        await session.rollback()
    async with container.sessions() as session, session.begin():
        connection = await session.connection()
        await connection.execute(text("CREATE TABLE unclassified_business_state (id integer)"))
        with pytest.raises(RecoveryError, match="table_inventory_mismatch"):
            await check_connection(connection)
        await session.rollback()


async def test_operator_full_recovery_report(sessions, empty_target, monkeypatch):
    from urllib.parse import urlsplit, urlunsplit

    from pricehunter.operations.rehearsal import recovery_rehearsal

    redis = urlsplit(os.environ["TEST_REDIS_URL"])
    monkeypatch.setenv("REHEARSAL_DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("RESTORE_DATABASE_URL", empty_target)
    monkeypatch.setenv("REHEARSAL_REDIS_URL", urlunsplit(redis._replace(path="/14")))
    report = await recovery_rehearsal()
    assert report["recovery"]["checksums_matched"] and report["recovery"]["tables"] == 33
    assert report["replay_idempotent"] and report["redis_loss_passed"]
    assert all(report["crashes"].values())

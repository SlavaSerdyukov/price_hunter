import asyncio
import os
import stat
import sys

import pytest
from sqlalchemy import Column, Integer, Table

from pricehunter.db.base import Base
from pricehunter.operations.durability import TABLES, validate_inventory
from pricehunter.operations.errors import RecoveryError
from pricehunter.operations.integrity import portable_constraint
from pricehunter.operations.load import PROFILES, latency
from pricehunter.operations.recovery import (
    Recovery,
    command,
    database_url,
    endpoint,
    libpq_environment,
    private_file,
)
from pricehunter.operations.rehearsal import synthetic_settings


def test_inventory_requires_every_future_table_to_be_reviewed():
    validate_inventory()
    table = Table("unreviewed_future_table", Base.metadata, Column("id", Integer, primary_key=True))
    try:
        with pytest.raises(RecoveryError, match="table_inventory_mismatch"):
            validate_inventory()
    finally:
        Base.metadata.remove(table)
    with pytest.raises(RecoveryError, match="table_inventory_mismatch"):
        validate_inventory(set(TABLES) | {"unknown_database_table"})


def test_password_is_only_in_private_transient_pgpassfile(monkeypatch):
    password = "synthetic:password\\with-special-chars"
    from sqlalchemy.engine import URL

    url = URL.create(
        "postgresql+asyncpg",
        host="localhost",
        port=5432,
        username="synthetic",
        password=password,
        database="synthetic_test",
    )
    monkeypatch.setenv("PGPASSWORD", "do-not-inherit")
    monkeypatch.setenv("PGSERVICE", "untrusted")
    with libpq_environment(url) as environment:
        from pathlib import Path

        path = Path(environment["PGPASSFILE"])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert "synthetic\\:password\\\\with-special-chars" in path.read_text()
        assert "PGPASSWORD" not in environment and "PGSERVICE" not in environment
        assert password not in repr(environment) and "do-not-inherit" not in repr(environment)
    assert not path.exists()
    assert password not in endpoint(url)


@pytest.mark.parametrize(
    "url",
    [
        "not a URL",
        "sqlite:///example",
        "postgresql://u@host/db?options=SECRET",
        "postgresql://u@host/host%3Devil%20password%3DSECRET_test",
        "postgresql://u@host/db?sslmode=wrong",
    ],
)
def test_invalid_connection_diagnostics_do_not_expose_credentials(url):
    with pytest.raises(RecoveryError, match="database_url_invalid") as error:
        database_url(url)
    assert "SECRET" not in str(error.value)


async def test_subprocess_safe_failure_timeout_and_bounded_output():
    env = {"PATH": os.environ.get("PATH", "")}
    with pytest.raises(RecoveryError, match="postgres_command_failed") as error:
        await command(
            sys.executable, ["-c", "import sys; sys.stderr.write('SECRET'); sys.exit(3)"], env, 5
        )
    assert "SECRET" not in str(error.value)
    with pytest.raises(RecoveryError, match="postgres_command_timeout"):
        await command(sys.executable, ["-c", "import time; time.sleep(5)"], env, 0.1)
    with pytest.raises(RecoveryError, match="postgres_command_unavailable"):
        await command("/does/not/exist", [], env, 5)
    with pytest.raises(RecoveryError, match="client_version_invalid"):
        await command(sys.executable, ["-c", "print('x'*2000)"], env, 5, version=True)
    assert await command(sys.executable, ["-c", "print('safe')"], env, 5, version=True) == "safe\n"


async def test_cancelled_subprocess_is_reaped():
    task = asyncio.create_task(
        command(sys.executable, ["-c", "import time; time.sleep(30)"], {}, 40)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_private_files_reject_symlinks_and_public_permissions(tmp_path):
    path = tmp_path / "file.dump"
    path.write_bytes(b"PGDMP")
    path.chmod(0o644)
    with pytest.raises(RecoveryError, match="backup_permissions"):
        private_file(path)
    path.chmod(0o600)
    private_file(path)
    link = tmp_path / "symlink.dump"
    link.symlink_to(path)
    with pytest.raises(RecoveryError, match="backup_permissions"):
        private_file(link)


def test_constraint_normalization_preserves_meaningful_differences():
    source = (
        "CHECK (((plan)::text = ANY ((ARRAY['pro'::character varying, "
        "'power'::character varying])::text[])))"
    )
    restored = (
        "CHECK (((plan)::text = ANY (ARRAY[('pro'::character varying)::text, "
        "('power'::character varying)::text])))"
    )
    assert portable_constraint(source) == portable_constraint(restored)
    assert portable_constraint("CHECK (price > 0)") != portable_constraint("CHECK (price >= 0)")


def test_load_profiles_and_percentiles_are_observations():
    assert PROFILES["smoke"].concurrency == 4
    assert PROFILES["beta-medium"].concurrency > PROFILES["beta-small"].concurrency
    result = latency([0.1, 0.2, 0.3, 0.4])
    assert result.p50_ms == 200 and result.p95_ms == 400


@pytest.mark.parametrize(
    "source,redis",
    [
        ("postgresql://u@localhost/production", "redis://localhost/14"),
        ("postgresql://u@remote/data_test", "redis://localhost/14"),
        ("postgresql://u@localhost/data_test", "redis://remote/14"),
        ("postgresql://u@localhost/data_test", "redis://localhost/0"),
    ],
)
def test_synthetic_harness_rejects_production_dependencies(source, redis):
    with pytest.raises(RecoveryError, match="synthetic_local_dependencies_required"):
        synthetic_settings(source, redis)


async def test_verify_missing_manifest_safe_code(tmp_path):
    path = tmp_path / "missing.dump"
    with pytest.raises(RecoveryError, match="manifest_invalid"):
        await Recovery("postgresql://u@localhost/source").verify(path)

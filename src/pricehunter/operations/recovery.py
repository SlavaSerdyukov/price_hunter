"""Real PostgreSQL archives with a manifest from the *same exported snapshot*."""

import asyncio
import hashlib
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pricehunter.core.schema import EXPECTED_SCHEMA_HEAD
from pricehunter.db.base import utcnow
from pricehunter.db.session import SessionFactory
from pricehunter.operations.durability import validate_inventory
from pricehunter.operations.errors import RecoveryError
from pricehunter.operations.integrity import (
    TableDigest,
    check_connection,
    compare,
    fingerprints,
    prepare,
)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1] = 1
    created_at: datetime
    application_git_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    expected_alembic_head: str
    postgres_server_major: int = Field(ge=12, le=99)
    pg_dump_version: str = Field(pattern=r"^\d+\.\d+(?:\.\d+)?$")
    source_endpoint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    dump_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    dump_size_bytes: int = Field(gt=0)
    protections_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    tables: dict[str, TableDigest]
    backup_seconds: float = Field(ge=0)


class RecoveryReport(BaseModel):
    backup_size_bytes: int
    backup_seconds: float
    restore_seconds: float
    tables: int
    checksums_matched: bool = True
    schema_compatible: bool = True
    domain_smoke_passed: bool = True


def manifest_path(path: Path) -> Path:
    return path.with_name(path.name + ".manifest.json")


def database_url(value: str) -> URL:
    try:
        url = make_url(value)
        if (
            url.drivername not in ("postgresql", "postgresql+asyncpg")
            or not url.host
            or not url.database
            or not url.username
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", url.database) is None
            or any(key not in {"sslmode"} for key in url.query)
            or url.query.get("sslmode", "prefer")
            not in {"disable", "prefer", "require", "verify-ca", "verify-full"}
            or any(
                "\n" in part or "\r" in part or "\x00" in part
                for part in (url.host, url.database, url.username, url.password or "")
            )
        ):
            raise ValueError()
        return url
    except Exception:
        raise RecoveryError("database_url_invalid") from None


def endpoint(url: URL) -> str:
    data = f"{(url.host or '').lower()}:{url.port or 5432}/{url.database}"
    return hashlib.sha256(data.encode()).hexdigest()


def sessions_for(url: URL) -> SessionFactory:
    # SQLAlchemy asyncpg uses 'ssl', libpq uses 'sslmode'. Never lose TLS intent.
    mode = url.query.get("sslmode")
    arguments: dict[str, object] = {"command_timeout": 30, "timeout": 5}
    if mode is not None:
        arguments["ssl"] = mode
    engine = create_async_engine(
        url.set(drivername="postgresql+asyncpg", query={}),
        hide_parameters=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=5,
        connect_args=arguments,
    )
    return async_sessionmaker(engine, expire_on_commit=False)


@contextmanager
def libpq_environment(url: URL) -> Iterator[dict[str, str]]:
    # Do not inherit PGPASSWORD, service files, options, or unrelated process secrets.
    with tempfile.TemporaryDirectory(prefix="ph-libpq-") as directory:
        password_file = Path(directory) / "password"

        def escape(value: str) -> str:
            return value.replace("\\", "\\\\").replace(":", "\\:")

        line = ":".join(
            escape(v)
            for v in (
                url.host or "",
                str(url.port or 5432),
                url.database or "",
                url.username or "",
                url.password or "",
            )
        )
        descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(line + "\n")
        yield {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C",
            "LC_ALL": "C",
            "PGHOST": url.host or "",
            "PGPORT": str(url.port or 5432),
            "PGDATABASE": url.database or "",
            "PGUSER": url.username or "",
            "PGPASSFILE": str(password_file),
            "PGCONNECT_TIMEOUT": "5",
            "PGSSLMODE": str(url.query.get("sslmode", "prefer")),
            "PGAPPNAME": "pricehunter-recovery",
        }


async def command(
    executable: str,
    arguments: list[str],
    environment: dict[str, str],
    seconds: float,
    *,
    version: bool = False,
) -> str:
    process: asyncio.subprocess.Process | None = None
    try:
        async with asyncio.timeout(seconds):
            process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                env=environment,
                stdout=asyncio.subprocess.PIPE if version else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output = b""
            if version:
                assert process.stdout is not None
                output = await process.stdout.read(1024)
                if len(output) >= 1024:
                    raise RecoveryError("client_version_invalid")
            status = await process.wait()
            if status != 0:
                raise RecoveryError("postgres_command_failed")
            return output.decode("ascii")
    except TimeoutError:
        raise RecoveryError("postgres_command_timeout") from None
    except (OSError, UnicodeError):
        raise RecoveryError("postgres_command_unavailable") from None
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


def file_digest(path: Path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise RecoveryError("backup_permissions")


class Recovery:
    def __init__(
        self,
        source_url: str,
        *,
        pg_dump: str = "pg_dump",
        pg_restore: str = "pg_restore",
        timeout: float = 120,
    ) -> None:
        self.url = database_url(source_url)
        self.dump = pg_dump
        self.restore_tool = pg_restore
        self.timeout = timeout

    async def client(self, name: str, major: int) -> str:
        binary = shutil.which(name)
        if binary is None:
            raise RecoveryError("postgres_client_missing")
        output = await command(
            binary, ["--version"], {"PATH": os.environ.get("PATH", "")}, 5, version=True
        )
        match = re.search(r"PostgreSQL\) (\d+\.\d+(?:\.\d+)?)", output)
        if not match:
            raise RecoveryError("client_version_invalid")
        version = match[1]
        # Conservative portability policy: source, archive client and target majors must match.
        if int(version.split(".")[0]) != major:
            raise RecoveryError("postgres_version_mismatch")
        return version

    async def create(self, path: Path, *, revision: str | None = None) -> Manifest:
        try:
            async with asyncio.timeout(self.timeout + 180):
                return await self._create(path, revision=revision)
        except TimeoutError:
            raise RecoveryError("backup_timeout") from None

    async def _create(self, path: Path, *, revision: str | None = None) -> Manifest:
        started = time.monotonic()
        sidecar = manifest_path(path)
        created: list[Path] = []
        sessions = sessions_for(self.url)
        try:
            if path.parent.stat().st_mode & 0o077:
                raise RecoveryError("private_directory_required")
            # O_EXCL plus no-follow; never overwrite a previous backup or a symlink.
            for destination in (path, sidecar):
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                created.append(destination)
            async with sessions() as session, session.begin():
                connection = await session.connection(
                    execution_options={"isolation_level": "REPEATABLE READ"}
                )
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                await prepare(connection)
                signature = await check_connection(connection)
                major = int(await connection.scalar(text("SHOW server_version_num"))) // 10000
                version = await self.client(self.dump, major)
                snapshot = str(await connection.scalar(text("SELECT pg_export_snapshot()")))
                tables = await fingerprints(connection)
                with libpq_environment(self.url) as environment:
                    await command(
                        self.dump,
                        [
                            "--format=custom",
                            "--no-owner",
                            "--no-privileges",
                            "--lock-wait-timeout=5s",
                            f"--snapshot={snapshot}",
                            f"--file={path}",
                        ],
                        environment,
                        self.timeout,
                    )
            private_file(path)
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
            size, digest = await asyncio.to_thread(file_digest, path)
            manifest = Manifest(
                created_at=utcnow(),
                application_git_revision=revision,
                expected_alembic_head=EXPECTED_SCHEMA_HEAD,
                postgres_server_major=major,
                pg_dump_version=version,
                source_endpoint_sha256=endpoint(self.url),
                dump_sha256=digest,
                dump_size_bytes=size,
                protections_sha256=signature,
                tables=tables,
                backup_seconds=time.monotonic() - started,
            )
            with sidecar.open("w") as handle:
                handle.write(manifest.model_dump_json(indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return manifest
        except BaseException as exc:
            for destination in created:
                destination.unlink(missing_ok=True)
            if isinstance(exc, (RecoveryError, asyncio.CancelledError)):
                raise
            raise RecoveryError("backup_failed") from None
        finally:
            await sessions.kw["bind"].dispose()

    async def verify(self, path: Path) -> Manifest:
        try:
            private_file(path)
            sidecar = manifest_path(path)
            private_file(sidecar)
            if sidecar.stat().st_size > 128 * 1024:
                raise RecoveryError("manifest_invalid")
            manifest = Manifest.model_validate_json(sidecar.read_bytes())
            validate_inventory(set(manifest.tables))
            if manifest.created_at.tzinfo is None:
                raise RecoveryError("manifest_invalid")
            if manifest.expected_alembic_head != EXPECTED_SCHEMA_HEAD:
                raise RecoveryError("schema_mismatch")
            if int(manifest.pg_dump_version.split(".")[0]) != manifest.postgres_server_major:
                raise RecoveryError("manifest_invalid")
            size, digest = await asyncio.to_thread(file_digest, path)
            if (size, digest) != (manifest.dump_size_bytes, manifest.dump_sha256):
                raise RecoveryError("dump_checksum")
            with path.open("rb") as handle:
                if handle.read(5) != b"PGDMP":
                    raise RecoveryError("dump_format")
            return manifest
        except (ValidationError, ValueError, OSError):
            raise RecoveryError("manifest_invalid") from None

    async def restore(
        self,
        path: Path,
        target_url: str,
        *,
        confirm_disposable: bool = False,
    ) -> RecoveryReport:
        try:
            async with asyncio.timeout(self.timeout + 180):
                return await self._restore(path, target_url, confirm_disposable=confirm_disposable)
        except TimeoutError:
            raise RecoveryError("restore_timeout") from None

    async def _restore(
        self,
        path: Path,
        target_url: str,
        *,
        confirm_disposable: bool = False,
    ) -> RecoveryReport:
        started = time.monotonic()
        target = database_url(target_url)
        if not confirm_disposable or not (target.database or "").endswith(("_test", "_rehearsal")):
            raise RecoveryError("disposable_confirmation_required")
        manifest = await self.verify(path)
        if endpoint(target) in (endpoint(self.url), manifest.source_endpoint_sha256):
            raise RecoveryError("source_equals_target")
        sessions = sessions_for(target)
        try:
            async with sessions() as session:
                objects = await session.scalar(
                    text("""
                    SELECT EXISTS(SELECT 1 FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema')
                    OR EXISTS(SELECT 1 FROM pg_proc p
                    JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema')
                """)
                )
                if objects:
                    raise RecoveryError("target_not_empty")
                major = int(await session.scalar(text("SHOW server_version_num"))) // 10000
            if major != manifest.postgres_server_major:
                raise RecoveryError("postgres_version_mismatch")
            await self.client(self.restore_tool, major)
            with libpq_environment(target) as environment:
                await command(
                    self.restore_tool,
                    [
                        "--single-transaction",
                        "--exit-on-error",
                        "--no-owner",
                        "--no-privileges",
                        "--dbname=" + (target.database or ""),
                        str(path),
                    ],
                    environment,
                    self.timeout,
                )
            # No migrations run here. The restored version must already be exactly current.
            from pricehunter.operations.smoke import domain_smoke

            await domain_smoke(sessions)
            async with sessions() as session, session.begin():
                connection = await session.connection(
                    execution_options={"isolation_level": "REPEATABLE READ"}
                )
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                signature = await check_connection(connection)
                if signature != manifest.protections_sha256:
                    raise RecoveryError("schema_protections_mismatch")
                compare(await fingerprints(connection), manifest.tables)
            return RecoveryReport(
                backup_size_bytes=manifest.dump_size_bytes,
                backup_seconds=manifest.backup_seconds,
                restore_seconds=time.monotonic() - started,
                tables=len(manifest.tables),
            )
        except (RecoveryError, asyncio.CancelledError):
            raise
        except Exception:
            raise RecoveryError("restore_failed") from None
        finally:
            await sessions.kw["bind"].dispose()

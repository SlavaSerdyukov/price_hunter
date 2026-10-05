"""Bounded, read-only state verification shared by restore and load rehearsal."""

import hashlib
import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import CheckConstraint, UniqueConstraint, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from pricehunter.core.schema import EXPECTED_SCHEMA_HEAD
from pricehunter.db.base import Base
from pricehunter.db.session import SessionFactory
from pricehunter.operations.durability import TABLES, Durability, validate_inventory
from pricehunter.operations.errors import RecoveryError


class TableDigest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    durability: Durability
    rows: int = Field(ge=0)
    primary_keys_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rows_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


REQUIRED_GUARDS = {
    ("payment_events", "payment_immutable"),
    ("billing_products", "contract_immutable"),
    ("checkout_intents", "contract_immutable"),
    ("merchant_audits", "merchant_audit_immutable"),
    ("merchant_program_audits", "merchant_program_audit_immutable"),
    ("merchant_program_validations", "merchant_program_validations_immutable"),
    ("feed_publication_audits", "feed_publication_audits_immutable"),
    ("stores", "store_default_merchant"),
}


def portable_constraint(definition: str) -> str:
    # pg_dump reparses varchar IN lists as text ANY arrays. Normalize only literal
    # varchar -> text widening, not general parentheses/operators/check expressions.
    definition = re.sub(
        r"\('((?:[^']|'')*)'::character varying\)::text", r"'\1'::character varying", definition
    )
    return re.sub(
        r"\(\(ARRAY\[((?:'[^']*'::character varying(?:, )?)*)\]\)::text\[\]\)",
        r"(ARRAY[\1])",
        definition,
    )


async def prepare(connection: AsyncConnection) -> None:
    await connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    await connection.execute(text("SET LOCAL statement_timeout = '30s'"))
    await connection.execute(text("SET LOCAL lock_timeout = '5s'"))


async def fingerprints(connection: AsyncConnection) -> dict[str, TableDigest]:
    validate_inventory()
    result: dict[str, TableDigest] = {}
    for name in sorted(TABLES):
        keys = (
            [column.name for column in Base.metadata.tables[name].primary_key]
            if name != "alembic_version"
            else ["version_num"]
        )
        columns = ",".join(f't."{key}"' for key in keys)
        query = text(
            f"SELECT jsonb_build_array({columns})::text, to_jsonb(t)::text "
            f'FROM public."{name}" t ORDER BY {columns}'
        ).execution_options(yield_per=128)
        identity, payload, count = hashlib.sha256(), hashlib.sha256(), 0
        stream = await connection.stream(query)
        try:
            async for row in stream:
                for digest, value in ((identity, row[0]), (payload, row[1])):
                    data = value.encode("utf-8")
                    digest.update(len(data).to_bytes(8, "big"))
                    digest.update(data)
                count += 1
        finally:
            await stream.close()
        result[name] = TableDigest(
            durability=TABLES[name],
            rows=count,
            primary_keys_sha256=identity.hexdigest(),
            rows_sha256=payload.hexdigest(),
        )
    return result


async def protections(connection: AsyncConnection) -> str:
    """Hash all user triggers/functions, constraints and indexes, not just known guards."""
    guards = await connection.execute(
        text("""
        SELECT c.relname, t.tgname, t.tgenabled::text, pg_get_triggerdef(t.oid),
               pg_get_functiondef(t.tgfoid)
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND NOT t.tgisinternal
        ORDER BY c.relname,t.tgname
    """)
    )
    rows = guards.all()
    if not REQUIRED_GUARDS <= {(r[0], r[1]) for r in rows} or any(r[2] != "O" for r in rows):
        raise RecoveryError("immutability_guard_missing")
    constraints = await connection.execute(
        text("""
        SELECT c.relname, x.conname, x.convalidated, pg_get_constraintdef(x.oid)
        FROM pg_constraint x JOIN pg_class c ON c.oid=x.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' ORDER BY c.relname,x.conname
    """)
    )
    constraints_rows = [(*r[:3], portable_constraint(r[3])) for r in constraints]
    if any(not row[2] for row in constraints_rows):
        raise RecoveryError("constraint_invalid")
    indexes = await connection.execute(
        text("""
        SELECT c.relname, i.indisvalid, i.indisready, pg_get_indexdef(i.indexrelid)
        FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' ORDER BY c.relname
    """)
    )
    indexes_rows = indexes.all()
    if any(not r[1] or not r[2] for r in indexes_rows):
        raise RecoveryError("index_invalid")
    digest = hashlib.sha256()
    for group in (rows, constraints_rows, indexes_rows):
        for row in group:
            digest.update(str(tuple(row)).encode())
            digest.update(b"\n")
    return digest.hexdigest()


async def check_connection(connection: AsyncConnection) -> str:
    await prepare(connection)
    versions = list(await connection.scalars(text("SELECT version_num FROM alembic_version")))
    if versions != [EXPECTED_SCHEMA_HEAD]:
        raise RecoveryError("schema_mismatch")
    deployed = list(
        await connection.execute(
            text("""
        SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
              AND c.relkind IN ('r','p','f')
    """)
        )
    )
    if any(schema != "public" for schema, _ in deployed):
        raise RecoveryError("table_inventory_mismatch")
    names = {name for _, name in deployed}
    validate_inventory(names)
    signature = await protections(connection)
    # Check actual data as well as the catalog's validated constraint flags.
    for table in Base.metadata.sorted_tables:
        for fk in table.foreign_key_constraints:
            joins = " AND ".join(f'a."{e.parent.name}" = b."{e.column.name}"' for e in fk.elements)
            present = " AND ".join(f'a."{e.parent.name}" IS NOT NULL' for e in fk.elements)
            parent = fk.elements[0].column.table.name
            pk = fk.elements[0].column.name
            bad = await connection.scalar(
                text(
                    f'SELECT EXISTS(SELECT 1 FROM "{table.name}" a LEFT JOIN "{parent}" b '
                    f'ON {joins} WHERE {present} AND b."{pk}" IS NULL)'
                )
            )
            if bad:
                raise RecoveryError("foreign_key_violation")
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint):
                if await connection.scalar(
                    text(
                        f'SELECT EXISTS(SELECT 1 FROM "{table.name}" '
                        f"WHERE NOT ({constraint.sqltext}))"
                    )
                ):
                    raise RecoveryError("check_constraint_violation")
            if isinstance(constraint, UniqueConstraint):
                columns = [f'"{c.name}"' for c in constraint.columns]
                keys = ",".join(columns)
                nonnull = (
                    "true"
                    if constraint.dialect_options["postgresql"].get("nulls_not_distinct")
                    else " AND ".join(f"{c} IS NOT NULL" for c in columns)
                )
                if await connection.scalar(
                    text(
                        f'SELECT EXISTS(SELECT 1 FROM "{table.name}" WHERE {nonnull} '
                        f"GROUP BY {keys} HAVING count(*)>1)"
                    )
                ):
                    raise RecoveryError("duplicate_identity")
    for table_name, fields in {
        "feed_sync_states": ("generation", "failure_count", "row_count"),
        "product_discoveries": ("failure_count", "result_count", "sequence"),
        "store_offers": ("failure_count", "refresh_sequence", "feed_generation"),
        "billing_updates": ("attempts",),
        "notification_events": ("attempts",),
        "merchant_program_validations": ("valid_rows", "invalid_rows", "duplicate_rows"),
    }.items():
        conditions = " OR ".join(f'"{field}"<0' for field in fields)
        if await connection.scalar(
            text(f'SELECT EXISTS(SELECT 1 FROM "{table_name}" WHERE {conditions})')
        ):
            raise RecoveryError("invalid_business_counter")
    return signature


async def integrity(sessions: SessionFactory) -> str:
    async with sessions() as session, session.begin():
        connection = await session.connection(
            execution_options={"isolation_level": "REPEATABLE READ"}
        )
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        return await check_connection(connection)


async def state(sessions: SessionFactory) -> dict[str, TableDigest]:
    async with sessions() as session, session.begin():
        connection = await session.connection(
            execution_options={"isolation_level": "REPEATABLE READ"}
        )
        await prepare(connection)
        return await fingerprints(connection)


def compare(actual: Mapping[str, TableDigest], expected: Mapping[str, TableDigest]) -> None:
    if actual != expected:
        raise RecoveryError("table_checksum_mismatch")


async def probe_immutability(sessions: SessionFactory) -> None:
    """Rollback-only probes on populated synthetic/restored evidence, never delete records."""
    mutations: dict[str, str] = {
        "payment_events": "amount=amount+1",
        "billing_products": "stars=stars+1",
        "checkout_intents": "expected_amount=expected_amount+1",
        "merchant_audits": "reason=reason||'probe'",
        "merchant_program_audits": "reason=reason||'probe'",
        "merchant_program_validations": "valid_rows=valid_rows+1",
        "feed_publication_audits": "reason=reason||'probe'",
    }
    async with sessions() as session, session.begin():
        for table, mutation in mutations.items():
            if not await session.scalar(text(f'SELECT EXISTS(SELECT 1 FROM "{table}")')):
                raise RecoveryError("probe_fixture_incomplete")
            rejected = False
            try:
                async with session.begin_nested():
                    key = "code" if table == "billing_products" else "id"
                    await session.execute(
                        text(
                            f'UPDATE "{table}" SET {mutation} '
                            f'WHERE "{key}"=(SELECT "{key}" FROM "{table}" LIMIT 1)'
                        )
                    )
            except DBAPIError as exc:
                rejected = getattr(exc.orig, "sqlstate", None) == "P0001"
            if not rejected:
                raise RecoveryError("immutability_probe_failed")
        await session.rollback()

"""Canonical retailer identity, preserving acquisition sources and snapshots.

Revision ID: 6bde2194a7c0
Revises: 9f62c7d40a11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "6bde2194a7c0"
down_revision: str | None = "9f62c7d40a11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("primary_domain", sa.String(200), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("version >= 1", name="positive_version"),
    )
    op.add_column("stores", sa.Column("merchant_id", sa.Uuid(), nullable=True))
    # Exact 1:1 backfill, never a domain/name/provider match. Existing IDs stay intact.
    op.execute("""INSERT INTO merchants(id,slug,display_name,primary_domain,created_at,updated_at)
        SELECT id,'source-'||id::text,name,domain,created_at,updated_at FROM stores""")
    op.execute("UPDATE stores SET merchant_id=id")
    op.alter_column("stores", "merchant_id", nullable=False)
    op.create_foreign_key(None, "stores", "merchants", ["merchant_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_stores_merchant_id", "stores", ["merchant_id"])
    op.create_table(
        "merchant_audits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "merchant_id",
            sa.Uuid(),
            sa.ForeignKey("merchants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "store_id", sa.Uuid(), sa.ForeignKey("stores.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column(
            "related_merchant_id",
            sa.Uuid(),
            sa.ForeignKey("merchants.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("previous_version", sa.Integer(), nullable=False),
        sa.Column("new_version", sa.Integer(), nullable=False),
        sa.Column("changed_fields", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("merchant_id", "new_version"),
        sa.CheckConstraint("new_version = previous_version + 1", name="audit_version_step"),
    )
    op.create_index("ix_merchant_audit_history", "merchant_audits", ["merchant_id", "created_at"])
    op.execute("""INSERT INTO merchant_audits
        (id,merchant_id,store_id,action,previous_version,new_version,changed_fields,reason)
        SELECT gen_random_uuid(),id,id,'created',0,1,'[]'::jsonb,'migration_backfill'
        FROM stores""")
    op.execute("""CREATE FUNCTION guard_merchant_audit() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'Merchant audit is append-only'; END;
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER merchant_audit_immutable BEFORE UPDATE OR DELETE
        ON merchant_audits FOR EACH ROW EXECUTE FUNCTION guard_merchant_audit()""")
    op.execute("""CREATE FUNCTION create_source_merchant() RETURNS trigger AS $$
        DECLARE existing_merchant uuid;
        BEGIN
            IF NEW.merchant_id IS NULL THEN
                -- Serialize only exact acquisition identity creation, including concurrent
                -- INSERT ON CONFLICT callers. No retailer inference; no orphan loser rows.
                PERFORM pg_advisory_xact_lock(hashtextextended('source-merchant:'||NEW.slug,0));
                SELECT merchant_id INTO existing_merchant FROM stores WHERE slug=NEW.slug;
                IF existing_merchant IS NOT NULL THEN
                    NEW.merchant_id := existing_merchant;
                ELSE
                    INSERT INTO merchants(id,slug,display_name,primary_domain)
                    VALUES(NEW.id,'source-'||NEW.id::text,NEW.name,NEW.domain);
                    INSERT INTO merchant_audits
                        (id,merchant_id,action,previous_version,new_version,changed_fields,reason)
                    VALUES(gen_random_uuid(),NEW.id,'created',0,1,'[]'::jsonb,'source_identity_created');
                    NEW.merchant_id := NEW.id;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER store_default_merchant BEFORE INSERT ON stores
        FOR EACH ROW EXECUTE FUNCTION create_source_merchant()""")
    for table, column in (
        ("product_watches", "best_merchant_id"),
        ("product_best_states", "merchant_id"),
        ("best_price_events", "merchant_id"),
        ("outbound_clicks", "merchant_id"),
    ):
        op.add_column(table, sa.Column(column, sa.Uuid(), nullable=True))
        op.create_foreign_key(None, table, "merchants", [column], ["id"], ondelete="RESTRICT")
        if table == "outbound_clicks":
            op.execute(
                f"UPDATE {table} t SET {column}=s.merchant_id FROM stores s WHERE t.store_id=s.id"
            )
        else:
            offer_column = "best_offer_id" if table == "product_watches" else "store_offer_id"
            op.execute(f"""UPDATE {table} t SET {column}=s.merchant_id
                FROM store_offers o JOIN stores s ON s.id=o.store_id WHERE t.{offer_column}=o.id""")


def downgrade() -> None:
    used = op.get_bind().scalar(
        sa.text("""SELECT
        EXISTS(SELECT 1 FROM merchants WHERE version<>1)
        OR EXISTS(SELECT 1 FROM stores WHERE merchant_id<>id)
        OR EXISTS(SELECT 1 FROM merchant_audits WHERE action<>'created'
            OR reason NOT IN ('migration_backfill','source_identity_created'))
        OR EXISTS(SELECT 1 FROM merchants m WHERE NOT EXISTS(
            SELECT 1 FROM stores s WHERE s.merchant_id=m.id))""")
    )
    if used:
        raise RuntimeError(
            "Export/reconcile canonical merchant identity and audit before downgrading M4D"
        )
    for table, column in (
        ("outbound_clicks", "merchant_id"),
        ("best_price_events", "merchant_id"),
        ("product_best_states", "merchant_id"),
        ("product_watches", "best_merchant_id"),
    ):
        op.drop_constraint(f"fk_{table}_{column}_merchants", table, type_="foreignkey")
        op.drop_column(table, column)
    op.execute("DROP TRIGGER store_default_merchant ON stores")
    op.execute("DROP FUNCTION create_source_merchant()")
    op.execute("DROP TRIGGER merchant_audit_immutable ON merchant_audits")
    op.execute("DROP FUNCTION guard_merchant_audit()")
    op.drop_table("merchant_audits")
    op.drop_index("ix_stores_merchant_id", table_name="stores")
    op.drop_constraint("fk_stores_merchant_id_merchants", "stores", type_="foreignkey")
    op.drop_column("stores", "merchant_id")
    op.drop_table("merchants")

"""merchant programs and bounded feed staging

Revision ID: ff5e1c895ca6
Revises: b7c21a48d903
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "ff5e1c895ca6"
down_revision: str | None = "b7c21a48d903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchant_programs",
        sa.Column("network", sa.String(length=50), nullable=False),
        sa.Column("external_merchant_id", sa.String(length=100), nullable=False),
        sa.Column("market_country", sa.String(length=2), nullable=False),
        sa.Column("store_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("domain", sa.String(length=200), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("feed_mode", sa.String(length=30), nullable=False),
        sa.Column("external_feed_id", sa.String(length=100), nullable=False),
        sa.Column("feed_language", sa.String(length=2), nullable=False),
        sa.Column("policy_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reference", sa.String(length=500), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "market_country ~ '^[A-Z]{2}$'", name=op.f("ck_merchant_programs_program_market")
        ),
        sa.ForeignKeyConstraint(
            ["store_id"],
            ["stores.id"],
            name=op.f("fk_merchant_programs_store_id_stores"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merchant_programs")),
        sa.UniqueConstraint(
            "network",
            "external_merchant_id",
            "market_country",
            name=op.f("uq_merchant_programs_network"),
        ),
    )
    op.create_index(
        "ix_program_network_market",
        "merchant_programs",
        ["network", "market_country", "active", "approved"],
        unique=False,
    )
    op.create_table(
        "feed_pending_items",
        sa.Column("merchant_program_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("gtin", sa.String(length=14), nullable=True),
        sa.Column("brand_model", sa.String(length=410), nullable=True),
        sa.Column("brand_mpn", sa.String(length=410), nullable=True),
        sa.Column("normalized_title", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["merchant_program_id"],
            ["merchant_programs.id"],
            name=op.f("fk_feed_pending_items_merchant_program_id_merchant_programs"),
        ),
        sa.PrimaryKeyConstraint(
            "merchant_program_id", "attempt", "external_id", name=op.f("pk_feed_pending_items")
        ),
    )
    op.create_index(
        op.f("ix_feed_pending_items_created_at"), "feed_pending_items", ["created_at"], unique=False
    )
    op.create_table(
        "feed_sync_states",
        sa.Column("merchant_program_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("source_version", sa.String(length=200), nullable=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["merchant_program_id"],
            ["merchant_programs.id"],
            name=op.f("fk_feed_sync_states_merchant_program_id_merchant_programs"),
        ),
        sa.PrimaryKeyConstraint("merchant_program_id", name=op.f("pk_feed_sync_states")),
    )
    op.create_index(
        op.f("ix_feed_sync_states_next_sync_at"), "feed_sync_states", ["next_sync_at"], unique=False
    )
    op.create_table(
        "merchant_feed_items",
        sa.Column("merchant_program_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("gtin", sa.String(length=14), nullable=True),
        sa.Column("brand_model", sa.String(length=410), nullable=True),
        sa.Column("brand_mpn", sa.String(length=410), nullable=True),
        sa.Column("normalized_title", sa.String(length=500), nullable=False),
        sa.Column(
            "search_document",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', normalized_title)", persisted=True),
            nullable=False,
        ),
        sa.Column("feed_generation", sa.BigInteger(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["merchant_program_id"],
            ["merchant_programs.id"],
            name=op.f("fk_merchant_feed_items_merchant_program_id_merchant_programs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merchant_feed_items")),
        sa.UniqueConstraint(
            "merchant_program_id",
            "external_id",
            name=op.f("uq_merchant_feed_items_merchant_program_id"),
        ),
    )
    op.create_index(
        "ix_feed_gtin", "merchant_feed_items", ["gtin", "merchant_program_id"], unique=False
    )
    op.create_index("ix_feed_inactive", "merchant_feed_items", ["active", "seen_at"], unique=False)
    op.create_index(
        "ix_feed_model", "merchant_feed_items", ["brand_model", "merchant_program_id"], unique=False
    )
    op.create_index(
        "ix_feed_mpn", "merchant_feed_items", ["brand_mpn", "merchant_program_id"], unique=False
    )
    op.create_index(
        "ix_feed_text",
        "merchant_feed_items",
        ["search_document"],
        unique=False,
        postgresql_using="gin",
    )
    op.add_column("store_offers", sa.Column("merchant_program_id", sa.Uuid(), nullable=True))
    op.add_column(
        "store_offers",
        sa.Column("feed_generation", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "store_offers",
        sa.Column("catalog_active", sa.Boolean(), server_default="true", nullable=False),
    )
    op.create_index(
        op.f("ix_store_offers_merchant_program_id"),
        "store_offers",
        ["merchant_program_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_store_offers_merchant_program_id_merchant_programs"),
        "store_offers",
        "merchant_programs",
        ["merchant_program_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM merchant_programs)")):
        raise RuntimeError("Remove/export merchant programs explicitly before M4B downgrade")
    op.drop_constraint(
        op.f("fk_store_offers_merchant_program_id_merchant_programs"),
        "store_offers",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_store_offers_merchant_program_id"), table_name="store_offers")
    op.drop_column("store_offers", "catalog_active")
    op.drop_column("store_offers", "feed_generation")
    op.drop_column("store_offers", "merchant_program_id")
    op.drop_index("ix_feed_text", table_name="merchant_feed_items", postgresql_using="gin")
    op.drop_index("ix_feed_mpn", table_name="merchant_feed_items")
    op.drop_index("ix_feed_model", table_name="merchant_feed_items")
    op.drop_index("ix_feed_inactive", table_name="merchant_feed_items")
    op.drop_index("ix_feed_gtin", table_name="merchant_feed_items")
    op.drop_table("merchant_feed_items")
    op.drop_index(op.f("ix_feed_sync_states_next_sync_at"), table_name="feed_sync_states")
    op.drop_table("feed_sync_states")
    op.drop_index(op.f("ix_feed_pending_items_created_at"), table_name="feed_pending_items")
    op.drop_table("feed_pending_items")
    op.drop_index("ix_program_network_market", table_name="merchant_programs")
    op.drop_table("merchant_programs")

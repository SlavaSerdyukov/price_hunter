"""international markets affiliate links clicks and reference FX

Revision ID: 2c125500eaf6
Revises: 2872920b653a
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "2c125500eaf6"
down_revision: str | None = "2872920b653a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fx_rates",
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("rate", sa.Numeric(precision=30, scale=12), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("rate > 0", name=op.f("ck_fx_rates_positive_rate")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fx_rates")),
        sa.UniqueConstraint(
            "base_currency",
            "quote_currency",
            "effective_date",
            "source",
            name=op.f("uq_fx_rates_base_currency"),
        ),
    )
    op.create_index("ix_fx_effective_date", "fx_rates", ["effective_date"], unique=False)
    op.create_table(
        "outbound_clicks",
        sa.Column("offer_id", sa.Uuid(), nullable=True),
        sa.Column("store_id", sa.Uuid(), nullable=True),
        sa.Column("affiliate_network", sa.String(length=80), nullable=True),
        sa.Column("surface", sa.String(length=20), nullable=False),
        sa.Column("market_country", sa.String(length=2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opaque_click_reference", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["store_offers.id"],
            name=op.f("fk_outbound_clicks_offer_id_store_offers"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["store_id"],
            ["stores.id"],
            name=op.f("fk_outbound_clicks_store_id_stores"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbound_clicks")),
    )
    op.create_index(
        op.f("ix_outbound_clicks_created_at"), "outbound_clicks", ["created_at"], unique=False
    )
    op.add_column(
        "product_watches", sa.Column("market_country", sa.String(length=2), nullable=True)
    )
    op.execute(
        "UPDATE product_watches AS w SET market_country = COALESCE(u.country_code, 'BE') "
        "FROM users AS u WHERE u.id = w.user_id"
    )
    op.alter_column("product_watches", "market_country", nullable=False)
    op.create_check_constraint(
        "market_country_code", "product_watches", "market_country ~ '^[A-Z]{2}$'"
    )
    op.drop_constraint(op.f("uq_product_watches_user_id"), "product_watches", type_="unique")
    op.create_unique_constraint(
        op.f("uq_product_watches_user_id"),
        "product_watches",
        ["user_id", "product_id", "market_country", "currency"],
    )
    op.add_column(
        "store_offers",
        sa.Column(
            "affiliate_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column("store_offers", sa.Column("delivery_country", sa.String(length=2), nullable=True))
    op.add_column("store_offers", sa.Column("postal_code", sa.String(length=20), nullable=True))
    op.add_column(
        "store_offers",
        sa.Column("shipping_price", sa.Numeric(precision=18, scale=4), nullable=True),
    )
    op.add_column(
        "store_offers", sa.Column("tax", sa.Numeric(precision=18, scale=4), nullable=True)
    )
    op.alter_column(
        "store_offers", "direct_url", existing_type=sa.VARCHAR(length=2048), nullable=True
    )
    op.add_column("stores", sa.Column("external_merchant_id", sa.String(length=100), nullable=True))


def downgrade() -> None:
    incompatible = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM outbound_clicks) OR EXISTS(SELECT 1 FROM fx_rates) "
                "OR EXISTS(SELECT 1 FROM store_offers WHERE direct_url IS NULL) "
                "OR EXISTS(SELECT 1 FROM product_watches GROUP BY user_id, product_id, currency "
                "HAVING count(*) > 1)"
            )
        )
        .scalar()
    )
    if incompatible:
        raise RuntimeError(
            "Export or reconcile M4A clicks, FX, affiliate-only offers "
            "and market watches before downgrade"
        )
    op.drop_column("stores", "external_merchant_id")
    op.alter_column(
        "store_offers", "direct_url", existing_type=sa.VARCHAR(length=2048), nullable=False
    )
    op.drop_column("store_offers", "tax")
    op.drop_column("store_offers", "shipping_price")
    op.drop_column("store_offers", "postal_code")
    op.drop_column("store_offers", "delivery_country")
    op.drop_column("store_offers", "affiliate_metadata")
    op.drop_constraint(op.f("uq_product_watches_user_id"), "product_watches", type_="unique")
    op.create_unique_constraint(
        op.f("uq_product_watches_user_id"),
        "product_watches",
        ["user_id", "product_id", "currency"],
        postgresql_nulls_not_distinct=False,
    )
    op.drop_constraint(
        op.f("ck_product_watches_market_country_code"), "product_watches", type_="check"
    )
    op.drop_column("product_watches", "market_country")
    op.drop_index(op.f("ix_outbound_clicks_created_at"), table_name="outbound_clicks")
    op.drop_table("outbound_clicks")
    op.drop_index("ix_fx_effective_date", table_name="fx_rates")
    op.drop_table("fx_rates")

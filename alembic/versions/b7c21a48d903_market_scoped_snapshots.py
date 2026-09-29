"""Market-scoped offers and best series; preserve ambiguous global history.

Revision ID: b7c21a48d903
Revises: 2c125500eaf6
"""

import sqlalchemy as sa
from babel import Locale

from alembic import op

revision = "b7c21a48d903"
down_revision = "2c125500eaf6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("store_offers", sa.Column("market_country", sa.String(2)))
    op.add_column("store_offers", sa.Column("source_updated_at", sa.DateTime(timezone=True)))
    countries = [
        code
        for code in Locale("en").territories
        if len(code) == 2
        and code.isascii()
        and code.isupper()
        and code not in {"ZZ", "EU", "UN", "XA", "XB"}
    ]
    op.get_bind().execute(
        sa.text(
            "UPDATE store_offers o SET market_country = CASE "
            "WHEN o.metadata->>'market_country' = ANY(:countries) "
            "THEN o.metadata->>'market_country' ELSE s.country END "
            "FROM stores s WHERE s.id = o.store_id"
        ),
        {"countries": countries},
    )
    op.alter_column("store_offers", "market_country", nullable=False)
    op.create_check_constraint(
        "offer_market_country", "store_offers", "market_country ~ '^[A-Z]{2}$'"
    )
    op.drop_constraint("uq_store_offers_store_id", "store_offers", type_="unique")
    op.create_unique_constraint(
        "uq_store_offers_store_id", "store_offers", ["store_id", "external_id", "market_country"]
    )
    op.create_index(
        "ix_offers_product_market_currency",
        "store_offers",
        ["product_id", "market_country", "currency"],
    )
    for table, check, columns in (
        (
            "product_best_states",
            "best_state_market_country",
            ["product_id", "market_country", "currency"],
        ),
        (
            "best_price_events",
            "best_event_market_country",
            ["product_id", "market_country", "currency", "sequence"],
        ),
    ):
        # Winning-offer country cannot establish the scope of a former global series.
        op.add_column(table, sa.Column("market_country", sa.String(2), nullable=True))
        op.create_check_constraint(check, table, "market_country ~ '^[A-Z]{2}$'")
        op.drop_constraint(f"uq_{table}_product_id", table, type_="unique")
        op.create_unique_constraint(
            f"uq_{table}_product_id", table, columns, postgresql_nulls_not_distinct=True
        )
    op.drop_index("ix_best_history", table_name="best_price_events")
    op.create_index(
        "ix_best_history",
        "best_price_events",
        ["product_id", "market_country", "currency", "observed_at"],
    )
    op.execute("UPDATE product_best_states SET next_evaluation_at = NULL")
    # Preserve old baseline values until bounded maintenance resets them without alerts.
    op.execute("UPDATE product_watches SET best_absence_reason = 'market_rebuild'")


def downgrade() -> None:
    incompatible = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM best_price_events WHERE market_country IS NOT NULL) "
                "OR EXISTS(SELECT 1 FROM store_offers WHERE source_updated_at IS NOT NULL) "
                "OR EXISTS(SELECT 1 FROM store_offers GROUP BY store_id, external_id "
                "HAVING count(*) > 1)"
            )
        )
        .scalar()
    )
    if incompatible:
        raise RuntimeError("Export market-scoped history and reconcile snapshots before downgrade")
    op.execute("DELETE FROM product_best_states WHERE market_country IS NOT NULL")
    op.drop_index("ix_best_history", table_name="best_price_events")
    op.create_index(
        "ix_best_history", "best_price_events", ["product_id", "currency", "observed_at"]
    )
    for table, check, columns in (
        ("product_best_states", "best_state_market_country", ["product_id", "currency"]),
        ("best_price_events", "best_event_market_country", ["product_id", "currency", "sequence"]),
    ):
        op.drop_constraint(f"uq_{table}_product_id", table, type_="unique")
        op.create_unique_constraint(f"uq_{table}_product_id", table, columns)
        op.drop_constraint(op.f(f"ck_{table}_{check}"), table, type_="check")
        op.drop_column(table, "market_country")
    op.drop_index("ix_offers_product_market_currency", table_name="store_offers")
    op.drop_constraint("uq_store_offers_store_id", "store_offers", type_="unique")
    op.create_unique_constraint(
        "uq_store_offers_store_id", "store_offers", ["store_id", "external_id"]
    )
    op.drop_constraint(op.f("ck_store_offers_offer_market_country"), "store_offers", type_="check")
    op.drop_column("store_offers", "source_updated_at")
    op.drop_column("store_offers", "market_country")

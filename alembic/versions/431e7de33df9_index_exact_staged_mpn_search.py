"""index exact staged MPN search

Revision ID: 431e7de33df9
Revises: ff5e1c895ca6
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "431e7de33df9"
down_revision: str | None = "ff5e1c895ca6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_feed_mpn_exact",
        "merchant_feed_items",
        [sa.literal_column("lower(data ->> 'mpn')")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_feed_mpn_exact", table_name="merchant_feed_items")

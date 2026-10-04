"""Recoverable technical validation lease.

Revision ID: b65a7012c904
Revises: a54ef912b603
"""

import sqlalchemy as sa

from alembic import op

revision = "b65a7012c904"
down_revision = "a54ef912b603"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("feed_sync_states", sa.Column("validation_token", sa.Uuid(), nullable=True))
    op.add_column(
        "feed_sync_states",
        sa.Column("validation_lease_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM feed_sync_states WHERE validation_token IS NOT NULL)")
    ):
        raise RuntimeError("Stop validation and let leases expire/release before downgrade")
    op.drop_column("feed_sync_states", "validation_lease_until")
    op.drop_column("feed_sync_states", "validation_token")

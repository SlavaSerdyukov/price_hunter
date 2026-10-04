"""Immutable technical validations and publication exceptions.

Revision ID: a54ef912b603
Revises: 6bde2194a7c0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a54ef912b603"
down_revision: str | None = "6bde2194a7c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "feed_sync_states",
        sa.Column(
            "rejected_report",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_table(
        "merchant_program_validations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "merchant_program_id",
            sa.Uuid(),
            sa.ForeignKey("merchant_programs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_rows", sa.BigInteger(), nullable=False),
        sa.Column("invalid_rows", sa.BigInteger(), nullable=False),
        sa.Column("duplicate_rows", sa.BigInteger(), nullable=False),
        sa.Column("sampled_rows", sa.BigInteger(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("error_code", sa.String(60), nullable=True),
        sa.Column("adapter_revision", sa.String(40), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('passed','failed')", name="validation_status"),
    )
    op.create_index(
        "ix_program_validation_history",
        "merchant_program_validations",
        ["merchant_program_id", "completed_at"],
    )
    op.create_table(
        "feed_publication_audits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "merchant_program_id",
            sa.Uuid(),
            sa.ForeignKey("merchant_programs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("previous_rows", sa.BigInteger(), nullable=False),
        sa.Column("candidate_rows", sa.BigInteger(), nullable=False),
        sa.Column("invalid_rows", sa.BigInteger(), nullable=False),
        sa.Column("guard", sa.String(60), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("merchant_program_id", "generation"),
        sa.CheckConstraint("guard = 'quality_shrink'", name="publication_override_guard"),
    )
    op.execute("""CREATE FUNCTION guard_merchant_pilot_evidence() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'Merchant pilot evidence is append-only'; END;
        $$ LANGUAGE plpgsql""")
    for table in ("merchant_program_validations", "feed_publication_audits"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION guard_merchant_pilot_evidence()"
        )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM merchant_program_validations) "
            "OR EXISTS(SELECT 1 FROM feed_publication_audits)"
        )
    ):
        raise RuntimeError("Export merchant pilot evidence before downgrading M4E")
    for table in ("feed_publication_audits", "merchant_program_validations"):
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
        op.drop_table(table)
    op.execute("DROP FUNCTION guard_merchant_pilot_evidence()")
    op.drop_column("feed_sync_states", "rejected_report")

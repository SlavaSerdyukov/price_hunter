"""Versioned merchant operations and append-only audit.

Revision ID: 9f62c7d40a11
Revises: 431e7de33df9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9f62c7d40a11"
down_revision: str | None = "431e7de33df9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "feed_sync_states", sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE feed_sync_states SET last_failure_at=started_at WHERE error_code IS NOT NULL"
    )
    op.add_column(
        "merchant_programs", sa.Column("version", sa.Integer(), nullable=False, server_default="1")
    )
    op.create_table(
        "merchant_program_audits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "merchant_program_id",
            sa.Uuid(),
            sa.ForeignKey("merchant_programs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("previous_version", sa.Integer(), nullable=False),
        sa.Column("new_version", sa.Integer(), nullable=False),
        sa.Column("changed_fields", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("merchant_program_id", "new_version"),
        sa.CheckConstraint("new_version = previous_version + 1", name="audit_version_step"),
    )
    op.create_index(
        "ix_program_audit_history", "merchant_program_audits", ["merchant_program_id", "created_at"]
    )
    op.execute("""INSERT INTO merchant_program_audits
        (id,merchant_program_id,action,previous_version,new_version,changed_fields,reason)
        SELECT gen_random_uuid(),id,'migrated',0,1,'[]'::jsonb,
        'Existing M4B program state retained' FROM merchant_programs""")
    op.execute("""CREATE FUNCTION guard_merchant_program_audit() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'Merchant program audit is append-only'; END;
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER merchant_program_audit_immutable BEFORE UPDATE OR DELETE
        ON merchant_program_audits FOR EACH ROW EXECUTE FUNCTION guard_merchant_program_audit()""")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM merchant_program_audits)")):
        raise RuntimeError("Export merchant program audit before downgrading M4C")
    op.execute("DROP TRIGGER merchant_program_audit_immutable ON merchant_program_audits")
    op.execute("DROP FUNCTION guard_merchant_program_audit()")
    op.drop_index("ix_program_audit_history", table_name="merchant_program_audits")
    op.drop_table("merchant_program_audits")
    op.drop_column("merchant_programs", "version")
    op.drop_column("feed_sync_states", "last_failure_at")

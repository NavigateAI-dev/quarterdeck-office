"""Bind approvals to an immutable proposal.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("scope_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column("approvals", sa.Column("scope_digest", sa.String(length=64), nullable=True))
    op.add_column("approvals", sa.Column("decision_id", sa.UUID(), nullable=True))
    op.add_column("approvals", sa.Column("revision_target", sa.String(length=32), nullable=True))
    op.create_index("ix_approvals_scope_digest", "approvals", ["scope_digest"])
    op.create_foreign_key(
        "fk_approvals_decision_id_decisions", "approvals", "decisions", ["decision_id"], ["id"]
    )
    # At most one pending approval per run: a second one would let two
    # decisions authorize the same work.
    op.create_index(
        "uq_approvals_run_id_pending",
        "approvals",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index("uq_approvals_run_id_pending", table_name="approvals")
    op.drop_constraint("fk_approvals_decision_id_decisions", "approvals", type_="foreignkey")
    op.drop_index("ix_approvals_scope_digest", table_name="approvals")
    op.drop_column("approvals", "revision_target")
    op.drop_column("approvals", "decision_id")
    op.drop_column("approvals", "scope_digest")
    op.drop_column("approvals", "scope_json")

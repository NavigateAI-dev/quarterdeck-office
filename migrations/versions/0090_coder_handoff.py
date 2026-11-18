"""Coder jobs carry the approval they carry out, a build's seed folder, and
whether a comment should interrupt the turn (Stage 36b).

Revision ID: 0090
Revises: 0089
"""

import sqlalchemy as sa
from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "coder_jobs", sa.Column("scope_digest", sa.String(64), nullable=False, server_default="")
    )
    op.add_column(
        "coder_jobs", sa.Column("seed_from", sa.String(500), nullable=False, server_default="")
    )
    op.add_column(
        "coder_jobs",
        sa.Column("notes_now", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_coder_jobs_scope_digest", "coder_jobs", ["scope_digest"])


def downgrade() -> None:
    op.drop_index("ix_coder_jobs_scope_digest", table_name="coder_jobs")
    op.drop_column("coder_jobs", "notes_now")
    op.drop_column("coder_jobs", "seed_from")
    op.drop_column("coder_jobs", "scope_digest")

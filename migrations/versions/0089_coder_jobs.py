"""The coder on the owner's Mac (Stage 36a): the helper, and the jobs handed to it.

Revision ID: 0089
Revises: 0088
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coder_helpers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("only", sa.Boolean(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.String(32), nullable=False, server_default=""),
        sa.Column("root", sa.String(500), nullable=False, server_default=""),
        sa.Column("clis_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("only", name="uq_coder_helpers_only"),
    )
    op.create_table(
        "coder_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="SET NULL", name="fk_coder_jobs_run_id_runs"),
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("folder", sa.String(500), nullable=False),
        sa.Column("base_branch", sa.String(255), nullable=False),
        sa.Column("base_commit", sa.String(64)),
        sa.Column("branch", sa.String(255), nullable=False),
        sa.Column("cli", sa.String(32), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("stop_asked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes_json", postgresql.JSONB(), nullable=False),
        sa.Column("session", sa.String(120), nullable=False, server_default=""),
        sa.Column("worktree", sa.String(500), nullable=False, server_default=""),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("heard_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("outcome_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_coder_jobs_run_id", "coder_jobs", ["run_id"])
    op.create_index("ix_coder_jobs_state", "coder_jobs", ["state"])


def downgrade() -> None:
    op.drop_index("ix_coder_jobs_state", table_name="coder_jobs")
    op.drop_index("ix_coder_jobs_run_id", table_name="coder_jobs")
    op.drop_table("coder_jobs")
    op.drop_table("coder_helpers")

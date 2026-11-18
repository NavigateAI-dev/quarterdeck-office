"""The task's room: posts every agent writes and reads (T), and team mode.

Revision ID: 0041
Revises: 0040
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The same post said again (a step run again after a pause) is kept once.
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("author_role", sa.String(32), nullable=False),
        sa.Column("step_id", sa.String(64), nullable=True),
        sa.Column("to_role", sa.String(32), nullable=True),
        sa.Column("to_step", sa.String(64), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("wave", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("run_id", "key", name="uq_team_posts_run_key"),
    )
    op.create_index("ix_team_posts_run_id", "team_posts", ["run_id", "created_at"])
    # Team mode: peers read the room while they work, and huddles after each
    # wave. Off until the owner turns it on; a project may say otherwise.
    op.add_column(
        "office_settings",
        sa.Column("team_on", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.add_column("projects", sa.Column("team_on", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "team_on")
    op.drop_column("office_settings", "team_on")
    op.drop_index("ix_team_posts_run_id", table_name="team_posts")
    op.drop_table("team_posts")

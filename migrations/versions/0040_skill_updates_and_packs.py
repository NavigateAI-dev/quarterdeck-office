"""Skill update checks, and the skills each business line keeps.

Revision ID: 0040
Revises: 0039
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_updates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("skill_id", sa.String(64), nullable=False, unique=True),
        sa.Column("repo", sa.String(255), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("installed_commit", sa.String(40), nullable=False),
        sa.Column("latest_commit", sa.String(40), nullable=True),
        sa.Column("latest_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        # current | available | unreachable
        sa.Column("state", sa.String(16), nullable=False, server_default="current"),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    # A skill in any line's pack is offered only to that line's tasks.
    op.add_column(
        "ventures",
        sa.Column(
            "skill_ids_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ventures", "skill_ids_json")
    op.drop_table("skill_updates")

"""Who put a project under its business line, and the office's suggestion.

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # "owner" or "office"; null for a project nobody has placed yet.
    op.add_column("projects", sa.Column("venture_set_by", sa.String(16), nullable=True))
    # The office's last judgment: line, venture id, confidence, when, and how
    # many tasks the project had then, so it is judged again only for a reason.
    op.add_column(
        "projects",
        sa.Column(
            "venture_suggestion_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    # A line already chosen before this revision was chosen by the owner, by
    # hand or by pressing Sort; either way the office should not move it.
    op.execute("UPDATE projects SET venture_set_by = 'owner' WHERE venture_id IS NOT NULL")


def downgrade() -> None:
    op.drop_column("projects", "venture_suggestion_json")
    op.drop_column("projects", "venture_set_by")

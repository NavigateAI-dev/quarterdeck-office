"""An idea's project, the time blocked for it, and Ideas as a board under the day.

Revision ID: 0063
Revises: 0062
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ideas",
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_ideas_project_id", "ideas", ["project_id"])
    op.add_column("ideas", sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ideas", sa.Column("calendar_change_id", sa.BigInteger(), nullable=True))
    # Ideas became a wide board beside the day; a wall made with the small
    # tile (earlier on 10 Oct) gets the board too.
    op.execute("UPDATE office_widgets SET size = 'wide' WHERE kind = 'ideas' AND size = 'small'")


def downgrade() -> None:
    op.drop_column("ideas", "calendar_change_id")
    op.drop_column("ideas", "blocked_at")
    op.drop_index("ix_ideas_project_id", table_name="ideas")
    op.drop_column("ideas", "project_id")

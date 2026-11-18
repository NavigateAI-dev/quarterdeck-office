"""A thread's own settings: model, access, goal and its cap.

Revision ID: 0075
Revises: 0074
"""

import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Null model: the office's choice for the role. Access read: work started
    # here never writes to a folder. Null goal cap: the hand-off's default.
    op.add_column(
        "topics",
        sa.Column(
            "model_connection_id",
            sa.Uuid(),
            sa.ForeignKey("model_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "topics", sa.Column("access", sa.String(8), server_default="read", nullable=False)
    )
    op.add_column("topics", sa.Column("goal", sa.Text(), nullable=True))
    op.add_column("topics", sa.Column("goal_cap_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("topics", "goal_cap_usd")
    op.drop_column("topics", "goal")
    op.drop_column("topics", "access")
    op.drop_column("topics", "model_connection_id")

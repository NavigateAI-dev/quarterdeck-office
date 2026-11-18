"""Coming back to a discussion: when the owner last looked at it, and
what the office's own small calls about it cost (its brief, a colleague's
check), so they count against the month like every other call.

Revision ID: 0047
Revises: 0046
"""

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("topics", sa.Column("owner_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("topic_items", sa.Column("cost_usd", sa.Float(), nullable=True))
    op.add_column("topic_items", sa.Column("tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("topic_items", "tokens")
    op.drop_column("topic_items", "cost_usd")
    op.drop_column("topics", "owner_seen_at")

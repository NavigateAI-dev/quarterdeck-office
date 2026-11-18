"""The task's room as a conversation: the stage a post was said in, and the
post it answers.

Revision ID: 0042
Revises: 0041
"""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # plan, work, huddle, review or answer; empty for the owner's posts and for
    # posts written before 0042, whose stage is read from the room's order.
    op.add_column("team_posts", sa.Column("phase", sa.String(16), nullable=True))
    # The key of the post this one answers, in the same room.
    op.add_column("team_posts", sa.Column("reply_to", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("team_posts", "reply_to")
    op.drop_column("team_posts", "phase")

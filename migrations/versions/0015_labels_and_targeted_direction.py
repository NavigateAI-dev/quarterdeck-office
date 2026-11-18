"""Labels on a task, and direction aimed at one role.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "labels_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    # An instruction for one step waits for that step; without it, the next
    # step to start picks it up, as before.
    op.add_column("run_controls", sa.Column("target_node", sa.String(32)))


def downgrade() -> None:
    op.drop_column("run_controls", "target_node")
    op.drop_column("runs", "labels_json")

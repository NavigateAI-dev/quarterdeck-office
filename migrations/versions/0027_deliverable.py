"""What the owner wants at the end, said before the work starts.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("projects", "runs"):
        op.add_column(
            table,
            sa.Column(
                "deliverable_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
            ),
        )


def downgrade() -> None:
    for table in ("runs", "projects"):
        op.drop_column(table, "deliverable_json")

"""The advisor on call, on or off for the office and per project.

Revision ID: 0037
Revises: 0036
"""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Off until the owner turns it on: it reads a lot, and that is spent.
    op.add_column(
        "office_settings",
        sa.Column("advisor_on", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    # Null follows the office.
    op.add_column("projects", sa.Column("advisor_on", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "advisor_on")
    op.drop_column("office_settings", "advisor_on")

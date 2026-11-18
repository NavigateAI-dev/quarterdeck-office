"""Whether builds may install packages.

Revision ID: 0058
Revises: 0057
"""

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "office_settings",
        sa.Column("build_packages", sa.Boolean(), nullable=True, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("office_settings", "build_packages")

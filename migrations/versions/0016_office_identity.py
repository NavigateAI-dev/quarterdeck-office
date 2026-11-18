"""The office's own name and its owner's name, set in the browser.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("office_settings", sa.Column("office_name", sa.String(128)))
    op.add_column("office_settings", sa.Column("owner_name", sa.String(255)))
    # When the owner finished the guided setup; null means never.
    op.add_column("office_settings", sa.Column("setup_completed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("office_settings", "setup_completed_at")
    op.drop_column("office_settings", "owner_name")
    op.drop_column("office_settings", "office_name")

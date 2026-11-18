"""The lead answers every new request: the 'who answers' setting goes.

The full team stays one press away on any task or discussion. The live office
never stored anything but the default (lead) here.

Revision ID: 0079
Revises: 0078
"""

import sqlalchemy as sa
from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("office_settings", "answering")


def downgrade() -> None:
    op.add_column("office_settings", sa.Column("answering", sa.String(16), nullable=True))

"""How a task's model is chosen: auto routing or the fixed role models.

Revision ID: 0067
Revises: 0066
"""

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("office_settings", sa.Column("model_routing", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("office_settings", "model_routing")

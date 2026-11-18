"""Where an idea came from outside the office, kept once.

Revision ID: 0062
Revises: 0061
"""

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ideas", sa.Column("source_ref", sa.String(160), nullable=True))
    op.create_unique_constraint("uq_ideas_source_ref", "ideas", ["source_ref"])


def downgrade() -> None:
    op.drop_constraint("uq_ideas_source_ref", "ideas", type_="unique")
    op.drop_column("ideas", "source_ref")

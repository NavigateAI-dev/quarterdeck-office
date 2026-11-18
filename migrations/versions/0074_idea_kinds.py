"""An idea's kind: friction, curiosity or opportunity.

Revision ID: 0074
Revises: 0073
"""

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Null is unsorted: every idea kept before kinds existed.
    op.add_column("ideas", sa.Column("kind", sa.String(16), nullable=True))
    op.create_index("ix_ideas_kind", "ideas", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_ideas_kind", table_name="ideas")
    op.drop_column("ideas", "kind")

"""A project may finish on a cheaper model instead of stopping.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column("projects", sa.Column("downgrade_at_usd", sa.Float()))
    op.add_column(
        "projects",
        sa.Column(
            "cheap_connection_id",
            UUID,
            sa.ForeignKey("model_connections.id", name="fk_projects_cheap_connection_id"),
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "cheap_connection_id")
    op.drop_column("projects", "downgrade_at_usd")

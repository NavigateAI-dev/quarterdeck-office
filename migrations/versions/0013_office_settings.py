"""Office settings the owner can change, and the monthly spending limit.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "office_settings",
        sa.Column("id", UUID, primary_key=True),
        # One row: the office's own settings. The key keeps it that way.
        sa.Column("only", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("run_max_tokens", sa.Integer()),
        sa.Column("run_max_cost_usd", sa.Float()),
        sa.Column("run_max_minutes", sa.Integer()),
        sa.Column("monthly_cost_limit_usd", sa.Float()),
        sa.Column("chat_daily_max_tokens", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("uq_office_settings_single_row", "office_settings", ["only"], unique=True)


def downgrade() -> None:
    op.drop_table("office_settings")

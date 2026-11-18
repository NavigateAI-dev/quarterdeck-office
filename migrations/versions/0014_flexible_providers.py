"""Model connections to any OpenAI-compatible provider, and per-role defaults.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Where the provider's API lives. Null means the SDK's own default.
    op.add_column("model_connections", sa.Column("base_url", sa.String(1000)))
    # Which connection each workflow role uses when no agent names one.
    op.add_column(
        "office_settings",
        sa.Column(
            "role_models_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("office_settings", "role_models_json")
    op.drop_column("model_connections", "base_url")

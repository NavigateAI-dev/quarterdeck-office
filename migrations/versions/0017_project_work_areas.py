"""A project may carry its own limits, models and notification channel.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column("projects", sa.Column("monthly_cost_limit_usd", sa.Float()))
    op.add_column("projects", sa.Column("run_max_tokens", sa.Integer()))
    op.add_column("projects", sa.Column("run_max_cost_usd", sa.Float()))
    op.add_column("projects", sa.Column("run_max_minutes", sa.Integer()))
    op.add_column(
        "projects",
        sa.Column(
            "role_models_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    # Where this project's notifications go; null uses the office's channels.
    op.add_column(
        "projects",
        sa.Column(
            "notify_destination_id",
            UUID,
            sa.ForeignKey(
                "channel_destinations.id", name="fk_projects_notify_destination_id_destinations"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "notify_destination_id")
    op.drop_column("projects", "role_models_json")
    op.drop_column("projects", "run_max_minutes")
    op.drop_column("projects", "run_max_cost_usd")
    op.drop_column("projects", "run_max_tokens")
    op.drop_column("projects", "monthly_cost_limit_usd")

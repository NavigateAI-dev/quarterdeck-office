"""What a business line is trying to achieve, and the work that serves it.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "objectives",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("venture_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("measure", sa.Text(), nullable=False, server_default=""),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["venture_id"], ["ventures.id"], name="fk_objectives_venture_id_ventures"
        ),
    )
    op.create_index("ix_objectives_venture_id", "objectives", ["venture_id"])
    op.create_index("ix_objectives_status", "objectives", ["status"])

    op.add_column("runs", sa.Column("objective_id", PG_UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_runs_objective_id_objectives", "runs", "objectives", ["objective_id"], ["id"]
    )
    op.create_index("ix_runs_objective_id", "runs", ["objective_id"])


def downgrade() -> None:
    op.drop_index("ix_runs_objective_id", table_name="runs")
    op.drop_constraint("fk_runs_objective_id_objectives", "runs", type_="foreignkey")
    op.drop_column("runs", "objective_id")
    op.drop_table("objectives")

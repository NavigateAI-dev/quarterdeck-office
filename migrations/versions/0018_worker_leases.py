"""Worker leases on runs, and reconciliation of unknown deliveries.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Who is executing a run, and until when. A run may be taken over only
    # once its lease has expired, so two workers cannot run it at once.
    op.add_column("runs", sa.Column("worker_id", sa.String(64)))
    op.add_column("runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.create_index("ix_runs_lease_expires_at", "runs", ["lease_expires_at"])
    # A delivery whose outcome the platform never confirmed waits for a
    # decision instead of being sent again.
    op.add_column("channel_deliveries", sa.Column("reconciled_at", sa.DateTime(timezone=True)))
    op.add_column("channel_deliveries", sa.Column("reconciled_by", sa.String(32)))


def downgrade() -> None:
    op.drop_column("channel_deliveries", "reconciled_by")
    op.drop_column("channel_deliveries", "reconciled_at")
    op.drop_index("ix_runs_lease_expires_at", table_name="runs")
    op.drop_column("runs", "lease_expires_at")
    op.drop_column("runs", "worker_id")

"""Every model call a run makes, kept so any step can be replayed.

Revision ID: 0072
Revises: 0071
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_payloads",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "model_calls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("model_alias", sa.String(128), nullable=False),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("request_json", postgresql.JSONB(), nullable=False),
        sa.Column("response_digest", sa.String(64), nullable=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_seconds", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_model_calls_run_id_id", "model_calls", ["run_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_model_calls_run_id_id", table_name="model_calls")
    op.drop_table("model_calls")
    op.drop_table("model_payloads")

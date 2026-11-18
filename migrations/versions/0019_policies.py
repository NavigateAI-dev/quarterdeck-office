"""Policies the owner turns on, what they decided, and what a run has done.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "policies",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        # "office" applies everywhere; "project" applies to one project's work.
        sa.Column("scope", sa.String(16), nullable=False, server_default="office"),
        sa.Column(
            "project_id",
            UUID,
            sa.ForeignKey("projects.id", name="fk_policies_project_id_projects"),
        ),
        sa.Column(
            "params_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_policies_scope_project", "policies", ["scope", "project_id"])
    op.create_table(
        "policy_records",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", UUID, index=True),
        sa.Column("project_id", UUID),
        sa.Column("hook", sa.String(32), nullable=False),
        sa.Column("target", sa.String(128), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("policy", sa.String(64)),
        sa.Column("reason", sa.Text()),
        sa.Column(
            "detail_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Set when an "ask" was settled, and what the owner said.
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision", sa.String(16)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "policy_state",
        sa.Column("run_id", UUID, primary_key=True),
        sa.Column(
            "counters_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("policy_state")
    op.drop_table("policy_records")
    op.drop_index("ix_policies_scope_project", table_name="policies")
    op.drop_table("policies")

"""The recurring subjects of the work, and what the office knows about them.

Revision ID: 0026
Revises: 0025
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("venture_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default="subject"),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("aliases_json", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["venture_id"], ["ventures.id"], name="fk_entities_venture_id_ventures"
        ),
    )
    op.create_index("ix_entities_venture_id", "entities", ["venture_id"])
    op.create_index("ix_entities_status", "entities", ["status"])
    op.create_index("uq_entities_venture_name", "entities", ["venture_id", "name"], unique=True)

    op.create_table(
        "entity_facts",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("fact", sa.Text(), nullable=False),
        sa.Column("fact_digest", sa.String(64), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="CANDIDATE"),
        sa.Column("durable_probability", sa.Float(), nullable=True),
        sa.Column("evaluator", sa.String(64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(255), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["entities.id"], name="fk_entity_facts_entity_id_entities"
        ),
    )
    op.create_index("ix_entity_facts_entity_id", "entity_facts", ["entity_id"])
    op.create_index("ix_entity_facts_run_id", "entity_facts", ["run_id"])
    op.create_index("ix_entity_facts_status", "entity_facts", ["status"])
    op.create_index(
        "uq_entity_facts_digest", "entity_facts", ["entity_id", "fact_digest"], unique=True
    )


def downgrade() -> None:
    op.drop_table("entity_facts")
    op.drop_table("entities")

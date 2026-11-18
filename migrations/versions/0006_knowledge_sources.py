"""Local knowledge sources, their extracted documents, and project grants.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def _timestamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "knowledge_sources",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("root", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("document_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "skipped_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("indexed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        *_timestamps(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("uq_knowledge_sources_name", "knowledge_sources", ["name"], unique=True)
    op.create_table(
        "source_documents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "source_id",
            UUID,
            sa.ForeignKey(
                "knowledge_sources.id", name="fk_source_documents_source_id_knowledge_sources"
            ),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True)),
        sa.Column("text", sa.Text(), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_source_documents_source_id", "source_documents", ["source_id"])
    op.create_index(
        "uq_source_documents_source_path", "source_documents", ["source_id", "path"], unique=True
    )
    op.create_table(
        "project_sources",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "project_id",
            UUID,
            sa.ForeignKey("projects.id", name="fk_project_sources_project_id_projects"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            UUID,
            sa.ForeignKey(
                "knowledge_sources.id", name="fk_project_sources_source_id_knowledge_sources"
            ),
            nullable=False,
        ),
        *_timestamps(),
    )
    op.create_index(
        "uq_project_sources_project_source",
        "project_sources",
        ["project_id", "source_id"],
        unique=True,
    )
    op.create_index("ix_project_sources_source_id", "project_sources", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_project_sources_source_id", table_name="project_sources")
    op.drop_index("uq_project_sources_project_source", table_name="project_sources")
    op.drop_table("project_sources")
    op.drop_index("uq_source_documents_source_path", table_name="source_documents")
    op.drop_index("ix_source_documents_source_id", table_name="source_documents")
    op.drop_table("source_documents")
    op.drop_index("uq_knowledge_sources_name", table_name="knowledge_sources")
    op.drop_table("knowledge_sources")

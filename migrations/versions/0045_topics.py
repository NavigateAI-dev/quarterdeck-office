"""Topics: the conversation is the unit of collaboration.

A project's room becomes its project's first topic ("home"), and every
message belongs to a topic. A topic needs no project: talk starts before
anyone knows where the work belongs. Tasks started from a topic remember it.

Revision ID: 0045
Revises: 0044
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "topics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        # Where it belongs, when anyone knows; who said so (office | owner).
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True
        ),
        sa.Column(
            "venture_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ventures.id"), nullable=True
        ),
        sa.Column("scope_set_by", sa.String(16), nullable=True),
        # The project's first topic: what its room was.
        sa.Column("home", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("lead_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "participants_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("parent_topic_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("branched_from_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "brief_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("revisit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_topics_project_id", "topics", ["project_id"])
    op.create_index("ix_topics_last_activity_at", "topics", ["last_activity_at"])
    op.create_index(
        "uq_topics_project_home",
        "topics",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("home"),
    )
    op.add_column(
        "room_messages", sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_index("ix_room_messages_topic_id", "room_messages", ["topic_id"])
    op.alter_column("room_messages", "project_id", nullable=True)
    op.add_column("runs", sa.Column("topic_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_runs_topic_id", "runs", ["topic_id"])
    op.add_column("runs", sa.Column("origin_message_id", sa.BigInteger(), nullable=True))
    # Each project with a room becomes a topic of its own, its messages kept.
    op.execute(
        """
        INSERT INTO topics (id, title, project_id, venture_id, scope_set_by, home, status,
                            last_activity_at, created_at, updated_at)
        SELECT gen_random_uuid(), left(p.name, 200), p.id, p.venture_id, 'owner', true, 'open',
               max(m.created_at), min(m.created_at), now()
        FROM projects p JOIN room_messages m ON m.project_id = p.id
        GROUP BY p.id, p.name, p.venture_id
        """
    )
    op.execute(
        """
        UPDATE room_messages m SET topic_id = t.id
        FROM topics t WHERE t.home AND t.project_id = m.project_id
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM room_messages WHERE project_id IS NULL")
    op.drop_column("runs", "origin_message_id")
    op.drop_index("ix_runs_topic_id", table_name="runs")
    op.drop_column("runs", "topic_id")
    op.alter_column("room_messages", "project_id", nullable=False)
    op.drop_index("ix_room_messages_topic_id", table_name="room_messages")
    op.drop_column("room_messages", "topic_id")
    op.drop_index("uq_topics_project_home", table_name="topics")
    op.drop_index("ix_topics_last_activity_at", table_name="topics")
    op.drop_index("ix_topics_project_id", table_name="topics")
    op.drop_table("topics")

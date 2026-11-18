"""A routine: a schedule that says its own words and is answered in a discussion.

The morning and evening recap become routines with the owner's words, times and
channels as they were; their templates go.

Revision ID: 0082
Revises: 0081
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None

HEAD = "What I want in it:\n"
TAIL = "\n\nUse the connected tools"


def upgrade() -> None:
    op.alter_column("schedules", "template_id", nullable=True)
    op.add_column("schedules", sa.Column("title", sa.String(200), nullable=True))
    op.add_column("schedules", sa.Column("prompt", sa.Text(), nullable=True))
    op.add_column(
        "schedules", sa.Column("quiet_when", sa.Text(), nullable=False, server_default="")
    )
    op.add_column("schedules", sa.Column("every_hours", sa.Integer(), nullable=True))
    op.add_column("schedules", sa.Column("cron", sa.String(64), nullable=True))
    op.add_column("schedules", sa.Column("once_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("schedules", sa.Column("preset", sa.String(32), nullable=True))
    op.add_column(
        "schedules", sa.Column("agent_profile_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("schedules", sa.Column("last_message_id", sa.BigInteger(), nullable=True))
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "select s.id, s.recap, s.template_id, t.name, t.objective from schedules s "
            "join task_templates t on t.id = s.template_id where s.recap is not null"
        )
    ).all()
    for row in rows:
        words = row.objective.split(HEAD, 1)[-1].split(TAIL, 1)[0].strip()
        connection.execute(
            sa.text(
                "update schedules set title = :title, prompt = :prompt, preset = :preset, "
                "template_id = null, last_run_id = null where id = :id"
            ),
            {
                "title": row.name,
                "prompt": words,
                "preset": f"{row.recap}-recap",
                "id": row.id,
            },
        )
        connection.execute(
            sa.text("delete from task_templates where id = :id"), {"id": row.template_id}
        )
    op.drop_index("uq_schedules_recap", table_name="schedules")
    op.drop_column("schedules", "recap")
    op.drop_column("schedules", "workflow")


def downgrade() -> None:
    op.add_column(
        "schedules",
        sa.Column("workflow", sa.String(16), nullable=False, server_default="v0_analysis"),
    )
    op.add_column("schedules", sa.Column("recap", sa.String(16), nullable=True))
    op.create_index("uq_schedules_recap", "schedules", ["recap"], unique=True)
    op.execute("delete from schedules where template_id is null")
    for column in (
        "last_message_id",
        "agent_profile_id",
        "preset",
        "once_at",
        "cron",
        "every_hours",
        "quiet_when",
        "prompt",
        "title",
    ):
        op.drop_column("schedules", column)
    op.alter_column("schedules", "template_id", nullable=False)

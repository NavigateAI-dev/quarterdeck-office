"""The office hears what is written as it is written.

A new run event, thread message or run control, or a run whose status changes,
sends one notice on the channel `office_<schema>`: which table, the row, the
run and the thread. Never the content. A trigger, so every writer is covered,
whichever process it runs in. Also the index the event feed reads by.

Revision ID: 0069
Revises: 0068
"""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None

TABLES = {
    "run_events": "AFTER INSERT",
    "room_messages": "AFTER INSERT",
    "run_controls": "AFTER INSERT OR UPDATE",
    "runs": "AFTER INSERT OR UPDATE OF status",
}

NOTIFY = """
CREATE OR REPLACE FUNCTION office_notify() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    run_ref uuid;
    topic_ref uuid;
BEGIN
    IF TG_TABLE_NAME = 'runs' THEN
        IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
            RETURN NULL;
        END IF;
        run_ref := NEW.id;
        topic_ref := NEW.topic_id;
    ELSIF TG_TABLE_NAME = 'room_messages' THEN
        run_ref := NEW.run_id;
        topic_ref := NEW.topic_id;
    ELSE
        run_ref := NEW.run_id;
    END IF;
    IF topic_ref IS NULL AND run_ref IS NOT NULL THEN
        EXECUTE format('SELECT topic_id FROM %I.runs WHERE id = $1', TG_TABLE_SCHEMA)
            INTO topic_ref USING run_ref;
    END IF;
    PERFORM pg_notify(
        'office_' || TG_TABLE_SCHEMA,
        json_build_object(
            'table', TG_TABLE_NAME, 'id', NEW.id::text, 'run', run_ref, 'topic', topic_ref
        )::text
    );
    RETURN NULL;
END
$$;
"""


def upgrade() -> None:
    op.execute(NOTIFY)
    for table, when in TABLES.items():
        op.execute(
            f"CREATE TRIGGER {table}_notify {when} ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION office_notify()"
        )
    op.create_index("ix_run_events_run_id_id", "run_events", ["run_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_run_events_run_id_id", table_name="run_events")
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_notify ON {table}")
    op.execute("DROP FUNCTION IF EXISTS office_notify()")

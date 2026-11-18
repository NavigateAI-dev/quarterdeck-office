from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.engine import make_url


def checkpoint_dsn(database_url: str) -> str:
    """Convert a SQLAlchemy URL into a psycopg connection string.

    The checkpointer uses psycopg directly and does not understand the
    SQLAlchemy driver suffix.
    """
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("Checkpoints require a PostgreSQL database URL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def open_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """Open a checkpointer and ensure its tables exist.

    Checkpoint tables are owned by LangGraph and created here, deliberately
    separate from the Alembic-managed domain tables. setup() is idempotent.
    """
    async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn(database_url)) as saver:
        await saver.setup()
        yield saver


def thread_config(run_id: str) -> dict[str, dict[str, str]]:
    """Address a run's checkpoint thread. The run ID is the thread ID."""
    return {"configurable": {"thread_id": run_id}}

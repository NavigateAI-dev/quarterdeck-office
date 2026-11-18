from alembic import context
from sqlalchemy import Connection

from app.db.models import Base
from app.db.session import make_engine


def database_url() -> str:
    from app.config import get_settings

    return get_settings().database_url.get_secret_value()


def run_on_connection(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=database_url(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif (connection := context.config.attributes.get("connection")) is not None:
    run_on_connection(connection)
else:
    engine = make_engine(database_url())
    try:
        with engine.connect() as connection:
            run_on_connection(connection)
    finally:
        engine.dispose()

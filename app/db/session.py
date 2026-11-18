from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

# The migration this code requires; /health is not_ready until it is applied.
SCHEMA_REVISION = "0090"


def make_engine(database_url: str) -> Engine:
    """Create a lazy connection pool; callers own engine disposal."""
    return create_engine(
        database_url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)

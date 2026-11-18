from unittest.mock import MagicMock, patch

import httpx
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.db.session import SCHEMA_REVISION
from app.main import create_app


async def test_health_checks_database_and_migration_without_leaking_errors() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://test:secret@localhost/test",
    )
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar.return_value = SCHEMA_REVISION
    app = create_app(settings)
    with patch("app.main.make_engine", return_value=engine):
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            assert (await client.get("/health")).json() == {"status": "ok", "database": "ok"}
            connection.execute.return_value.scalar.return_value = None
            assert (await client.get("/health")).status_code == 503
            engine.connect.side_effect = OperationalError(
                "private-password", {}, Exception("secret")
            )
            response = await client.get("/health")
            assert response.status_code == 503
            assert "secret" not in response.text
            assert "private-password" not in response.text
            assert (await client.get("/health/live")).status_code == 200
    engine.dispose.assert_called_once()


def test_the_required_revision_is_the_migration_head() -> None:
    """Adding a migration without updating SCHEMA_REVISION would fail health."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    assert heads == [SCHEMA_REVISION]

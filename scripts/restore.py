"""Restore a backup archive into an empty, migrated office.

    docker compose run --rm api python -m scripts.restore /backups/<archive>.tar.gz

Refuses unless the database holds no data and the workspace is empty, so it can
never overwrite an office in use.

The archive carries the LangGraph checkpoint tables, so a task that was waiting
for approval can carry on after a restore. Those tables are not managed by
Alembic: the checkpointer creates them, and on a machine where no task has ever
run they do not exist yet. So this creates them first — `setup()` is idempotent
— and only then restores. Without that step a restore onto a fresh machine
fails with `table_missing:checkpoint_blobs`.
"""

import asyncio
import sys
from pathlib import Path

from app.archive.protection import root_key
from app.backup import BackupError, restore_backup
from app.config import get_settings
from app.db.session import make_engine
from app.orchestrator.checkpoints import open_checkpointer


async def ensure_checkpoint_tables(database_url: str) -> None:
    async with open_checkpointer(database_url):
        pass


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m scripts.restore <archive>")
    settings = get_settings()
    database_url = settings.database_url.get_secret_value()
    asyncio.run(ensure_checkpoint_tables(database_url))
    engine = make_engine(database_url)
    try:
        skipped = restore_backup(
            Path(sys.argv[1]), engine, settings.workspace_root, root_key(settings)
        )
    except BackupError as error:
        raise SystemExit(f"Restore refused: {error.code}") from None
    finally:
        engine.dispose()
    if skipped:
        print(f"Not restored, sealed and no root key here: {', '.join(skipped)}")
    print("Restore complete.")


if __name__ == "__main__":
    main()

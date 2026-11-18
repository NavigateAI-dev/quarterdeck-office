"""Write a private backup archive of the database and workspace.

    docker compose exec -T api python -m scripts.backup

The archive excludes the credential root key; keep `.secrets/root.key` backed
up separately if stored provider keys, or AI history in backups, should survive
a machine loss.
"""

from app.archive.protection import backup_choice, root_key
from app.backup import create_backup
from app.config import get_settings
from app.db.session import make_engine, make_session_factory


def main() -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        with make_session_factory(engine)() as session:
            leave_out = backup_choice(session) == "left_out"
        key = None if leave_out else root_key(settings)
        if key is None and not leave_out:
            # Never plain: with no key to seal it, history stays out of this backup.
            print("No root key: AI history is left out of this backup.")
            leave_out = True
        archive = create_backup(
            engine,
            settings.workspace_root,
            settings.backup_dir,
            history_key=key,
            leave_out_history=leave_out,
        )
    finally:
        engine.dispose()
    print(f"Backup written: {archive.name}")


if __name__ == "__main__":
    main()

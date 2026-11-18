"""Compare a restored office against the archive it came from.

    docker compose run --rm api python -m scripts.verify_restore /backups/<archive>.tar.gz

Run inside the restored stack, after scripts.restore. Exits non-zero if any
check fails, so the drill cannot report a success it did not see.
"""

import sys
from pathlib import Path

from app.archive.protection import root_key
from app.backup import BackupError, verify_restore
from app.config import get_settings
from app.db.session import make_engine


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m scripts.verify_restore <archive>")
    settings = get_settings()
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        checks = verify_restore(
            Path(sys.argv[1]), engine, settings.workspace_root, root_key(settings)
        )
    except BackupError as error:
        print(f"FAIL unreadable archive: {error.code}")
        return 1
    finally:
        engine.dispose()
    failed = 0
    for name, passed, detail in checks:
        if not passed:
            failed += 1
        print(f"{'ok  ' if passed else 'FAIL'} {name}: {detail}")
    print(f"{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

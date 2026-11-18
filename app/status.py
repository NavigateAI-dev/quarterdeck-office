"""Owner-facing office status. Facts only; never a secret value."""

import shutil
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import ModelConnection
from app.office_settings import role_models
from app.runtimes.isolated import heartbeat_age
from app.sources.client import reader_heartbeat_age
from app.tools.fetch_client import fetcher_heartbeat_age
from app.version import VERSION

RUNNER_STALE_SECONDS = 30


# The roles every task needs a model for; the advisor is optional.
MODEL_ROLES = ("chief", "researcher", "strategist", "critic")


def roles_state(session: Session, settings: Settings) -> dict[str, Any]:
    """Which roles have a model: each needs a connection since the gateway left."""
    chosen = role_models(session, settings)
    missing = [role for role in MODEL_ROLES if role not in chosen]
    return {"state": "missing" if missing else "ok", "missing": missing}


def runner_state(settings: Settings) -> dict[str, Any]:
    if settings.engineering_test_runner != "isolated":
        return {"state": "local", "last_seen_seconds": None}
    age = heartbeat_age(settings.engineering_root)
    if age is None:
        return {"state": "not_seen", "last_seen_seconds": None}
    return {
        "state": "ok" if age < RUNNER_STALE_SECONDS else "stale",
        "last_seen_seconds": int(age),
    }


def reader_state(settings: Settings) -> dict[str, Any]:
    age = reader_heartbeat_age(settings.sources_exchange)
    if age is None:
        return {"state": "not_seen", "last_seen_seconds": None}
    return {"state": "ok" if age < RUNNER_STALE_SECONDS else "stale", "last_seen_seconds": int(age)}


def fetcher_state(settings: Settings) -> dict[str, Any]:
    """The page reader: a service with network access and no credentials."""
    if settings.fetch_exchange is None:
        return {"state": "in_process", "last_seen_seconds": None}
    age = fetcher_heartbeat_age(settings.fetch_exchange)
    if age is None:
        return {"state": "not_seen", "last_seen_seconds": None}
    return {"state": "ok" if age < RUNNER_STALE_SECONDS else "stale", "last_seen_seconds": int(age)}


def latest_backup_age(settings: Settings) -> float | None:
    import time

    try:
        archives = list(settings.backup_dir.glob("quarterdeck-backup-*.tar.gz"))
    except OSError:
        return None
    if not archives:
        return None
    return time.time() - max(path.stat().st_mtime for path in archives)


def office_status(session: Session, settings: Settings, queued: int) -> dict[str, Any]:
    revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    rows = session.execute(
        select(ModelConnection.status, func.count()).group_by(ModelConnection.status)
    )
    connections = {str(status): int(count) for status, count in rows}
    backup_age = latest_backup_age(settings)
    return {
        "version": VERSION,
        "database": {"state": "ok", "revision": revision},
        "worker": {"queued": queued},
        "models": roles_state(session, settings),
        "runner": runner_state(settings),
        "sources_reader": reader_state(settings),
        "page_reader": fetcher_state(settings),
        "storage": {"free_bytes": shutil.disk_usage(settings.workspace_root).free},
        "backups": {"latest_age_seconds": int(backup_age) if backup_age is not None else None},
        "credential_store": {
            "root_key": "present" if settings.secret_key_file.is_file() else "missing"
        },
        "connections": connections,
        "judgments": "typesafe" if settings.typesafe_api_key else "lexical_only",
        "code_execution": settings.enable_code_execution,
        "publication": settings.enable_github_pr,
    }

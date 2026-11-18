"""Backup and restore round trip against real PostgreSQL schemas."""

import json
import os
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, select, text

from app.artifacts import ArtifactStore
from app.backup import BackupError, create_backup, restore_backup, verify_restore
from app.db.models import Approval, ArchiveImport, Artifact, Project, Run
from app.db.session import make_engine, make_session_factory
from app.orchestrator.approval_store import ApprovalStore
from app.orchestrator.runner import RunRepository

pytestmark = pytest.mark.integration


def fresh_schema() -> tuple[Engine, str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to opt into PostgreSQL integration tests")
    schema = f"test_backup_{uuid4().hex}"
    admin = make_engine(url)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with admin.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET search_path TO "{schema}"'))
        connection.commit()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()
    admin.dispose()
    return make_engine(f"{url}?options=-csearch_path%3D{schema}"), schema


def drop(schema: str) -> None:
    admin = make_engine(os.environ["TEST_DATABASE_URL"])
    with admin.connect() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        connection.commit()
    admin.dispose()


@pytest.fixture
def databases():  # type: ignore[no-untyped-def]
    source, source_schema = fresh_schema()
    target, target_schema = fresh_schema()
    yield source, target
    source.dispose()
    target.dispose()
    drop(source_schema)
    drop(target_schema)


def populate(engine: Engine, workspace: Path) -> tuple[str, str]:
    factory = make_session_factory(engine)
    with factory() as session, session.begin():
        project = Project(name="Backed up", description="restore me")
        session.add(project)
        session.flush()
        project_id = project.id
    repository = RunRepository(factory, ArtifactStore(workspace))
    dispatch = repository.create(project_id, "Objective to survive")
    repository.record_stage(dispatch, "synthesis", {"recommendation": "Keep this"})
    from app.approvals import ApprovalScope

    scope = ApprovalScope(
        run_id=dispatch.run_id,
        project_id=project_id,
        objective="Objective to survive",
        recommendation="Keep this",
        proposed_engineering_scope="s",
    )
    approval_id = ApprovalStore(factory).open(dispatch.run_id, dispatch.task_id, scope)
    return str(approval_id), scope.digest()


def test_a_restore_brings_back_runs_artifacts_and_pending_approvals(
    databases,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    source, target = databases
    source_workspace = tmp_path / "source-workspace"
    approval_id, digest = populate(source, source_workspace)

    archive = create_backup(source, source_workspace, tmp_path / "backups")
    restored_workspace = tmp_path / "restored-workspace"
    restore_backup(archive, target, restored_workspace)

    factory = make_session_factory(target)
    with factory() as session:
        project = session.scalars(select(Project)).one()
        assert project.description == "restore me"
        assert session.scalars(select(Run)).one().project_id == project.id
        approval = session.get(Approval, approval_id)
        assert approval is not None and approval.status == "PENDING"
        assert approval.scope_digest == digest
        artifact = session.scalars(select(Artifact)).one()
    body = ArtifactStore(restored_workspace).read(project.id, artifact.id, artifact.version)
    assert json.loads(body)["recommendation"] == "Keep this"


def test_restore_refuses_a_database_that_already_has_data(
    databases,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    source, target = databases
    populate(source, tmp_path / "ws")
    archive = create_backup(source, tmp_path / "ws", tmp_path / "backups")
    populate(target, tmp_path / "other")
    with pytest.raises(BackupError, match="target_not_empty"):
        restore_backup(archive, target, tmp_path / "restored")


def test_a_tampered_archive_is_refused(databases, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    source, target = databases
    populate(source, tmp_path / "ws")
    archive = create_backup(source, tmp_path / "ws", tmp_path / "backups")
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive) as original, tarfile.open(tampered, "w:gz") as copy:
        for member in original.getmembers():
            data = original.extractfile(member)
            content = data.read() if data else b""
            if member.name.startswith("db/projects"):
                content = content.replace(b"restore me", b"rewritten!")
            member.size = len(content)
            import io

            copy.addfile(member, io.BytesIO(content))
    with pytest.raises(BackupError, match="digest_mismatch"):
        restore_backup(tampered, target, tmp_path / "restored")


def test_the_backup_never_contains_the_credential_root_key(
    databases,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    source, _ = databases
    workspace = tmp_path / "ws"
    populate(source, workspace)
    (workspace / "secrets").mkdir(parents=True, exist_ok=True)
    (workspace / "secrets" / "credentials.json").write_text('{"version": 1, "secrets": {}}')
    (workspace / "secrets" / "root.key").write_bytes(b"k" * 32)
    archive = create_backup(source, workspace, tmp_path / "backups")
    with tarfile.open(archive) as bundle:
        names = bundle.getnames()
    assert "workspace/secrets/credentials.json" in names
    assert not any(name.endswith(".key") for name in names)
    assert archive.stat().st_mode & 0o077 == 0


def test_owner_sessions_never_travel_in_a_backup(databases, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    from app.db.models import OwnerSession

    source, _ = databases
    populate(source, tmp_path / "ws")
    with make_session_factory(source)() as session, session.begin():
        now = datetime.now(UTC)
        session.add(
            OwnerSession(token_hash="a" * 64, last_seen_at=now, expires_at=now + timedelta(days=1))
        )
    archive = create_backup(source, tmp_path / "ws", tmp_path / "backups")
    with tarfile.open(archive) as bundle:
        assert "db/owner_sessions.jsonl" not in bundle.getnames()


def test_verification_confirms_a_good_restore(databases, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    source, target = databases
    source_workspace = tmp_path / "ws"
    populate(source, source_workspace)
    archive = create_backup(source, source_workspace, tmp_path / "backups")
    restored = tmp_path / "restored"
    restore_backup(archive, target, restored)

    checks = verify_restore(archive, target, restored)
    assert checks, "the drill compares something"
    assert all(passed for _, passed, _ in checks), [c for c in checks if not c[1]]
    names = {name for name, _, _ in checks}
    assert "table projects" in names and "schema revision" in names
    assert any(name.startswith("file workspace/") for name in names), "artifacts are compared"


def test_verification_catches_a_restore_that_lost_rows(databases, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A restore that did not raise is not proof; the comparison is the proof."""
    source, target = databases
    source_workspace = tmp_path / "ws"
    populate(source, source_workspace)
    archive = create_backup(source, source_workspace, tmp_path / "backups")
    restored = tmp_path / "restored"
    restore_backup(archive, target, restored)

    # Something ate a row after the restore reported success.
    with target.begin() as connection:
        connection.execute(text("DELETE FROM approvals"))
    checks = verify_restore(archive, target, restored)
    failures = {name: detail for name, passed, detail in checks if not passed}
    assert "table approvals" in failures
    assert "0 rows (archive: 1)" in failures["table approvals"]


def test_verification_catches_a_missing_workspace_file(databases, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    source, target = databases
    source_workspace = tmp_path / "ws"
    populate(source, source_workspace)
    archive = create_backup(source, source_workspace, tmp_path / "backups")
    restored = tmp_path / "restored"
    restore_backup(archive, target, restored)

    for path in sorted(restored.rglob("*")):
        if path.is_file():
            path.unlink()
            break
    failures = [name for name, passed, _ in verify_restore(archive, target, restored) if not passed]
    assert any(name.startswith("file workspace/") for name in failures)


def test_verification_catches_a_changed_value_with_the_same_row_count(  # type: ignore[no-untyped-def]
    databases, tmp_path: Path
) -> None:
    """R10: only counts were compared, so changed content passed every check."""
    source, target = databases
    source_workspace = tmp_path / "ws"
    populate(source, source_workspace)
    archive = create_backup(source, source_workspace, tmp_path / "backups")
    restored = tmp_path / "restored"
    restore_backup(archive, target, restored)

    with target.begin() as connection:
        connection.execute(text("UPDATE projects SET description = 'changed'"))
    checks = verify_restore(archive, target, restored)
    failures = {name: detail for name, passed, detail in checks if not passed}
    assert "table projects" in failures, "a changed value must fail the drill"
    assert "differ" in failures["table projects"]
    assert all(passed for name, passed, _ in checks if name != "table projects")


def test_a_restored_office_can_write_new_rows(databases, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Found on a restored copy: rows came back with their ids, the id
    counters did not, so the first new event or message collided with one."""
    source, target = databases
    source_workspace = tmp_path / "ws"
    populate(source, source_workspace)
    source_factory = make_session_factory(source)
    source_repository = RunRepository(source_factory, ArtifactStore(source_workspace))
    with source_factory() as session:
        source_run = session.scalar(text("SELECT id FROM runs LIMIT 1"))
    for kind in ("run_started", "step_started", "step_completed"):
        source_repository.add_event(source_run, kind, {})
    archive = create_backup(source, source_workspace, tmp_path / "backups")
    restored = tmp_path / "restored"
    restore_backup(archive, target, restored)

    factory = make_session_factory(target)
    repository = RunRepository(factory, ArtifactStore(restored))
    with factory() as session:
        run_id = session.scalar(text("SELECT id FROM runs LIMIT 1"))
    repository.add_event(run_id, "after_restore", {})
    with target.connect() as connection:
        newest = connection.execute(
            text("SELECT kind FROM run_events ORDER BY id DESC LIMIT 1")
        ).scalar()
    assert newest == "after_restore"


def _with_history(engine) -> None:  # type: ignore[no-untyped-def]
    factory = make_session_factory(engine)
    with factory() as session, session.begin():
        row = ArchiveImport(filename="private.zip", status="done")
        session.add(row)


def test_sealed_history_comes_back_with_the_key_and_verifies(
    databases,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    source, target = databases
    _with_history(source)
    key = b"k" * 32
    archive = create_backup(source, tmp_path / "ws", tmp_path / "backups", history_key=key)
    assert restore_backup(archive, target, tmp_path / "restored", key) == []
    with make_session_factory(target)() as session:
        assert session.scalars(select(ArchiveImport)).one().filename == "private.zip"
    assert all(
        passed for _, passed, _ in verify_restore(archive, target, tmp_path / "restored", key)
    )


def test_without_the_key_everything_else_comes_back_and_it_says_what_did_not(
    databases,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    source, target = databases
    source_workspace = tmp_path / "source-workspace"
    populate(source, source_workspace)
    _with_history(source)
    archive = create_backup(source, source_workspace, tmp_path / "backups", history_key=b"k" * 32)
    skipped = restore_backup(archive, target, tmp_path / "restored")
    assert "archive_imports" in skipped
    with make_session_factory(target)() as session:
        assert session.scalars(select(Project)).one().description == "restore me"
        assert session.scalars(select(ArchiveImport)).first() is None
    failed = [n for n, ok, _ in verify_restore(archive, target, tmp_path / "restored") if not ok]
    assert "table archive_imports" in failed


def test_history_left_out_restores_cleanly(
    databases,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    source, target = databases
    _with_history(source)
    archive = create_backup(source, tmp_path / "ws", tmp_path / "backups", leave_out_history=True)
    assert restore_backup(archive, target, tmp_path / "restored") == []
    with make_session_factory(target)() as session:
        assert session.scalars(select(ArchiveImport)).first() is None

"""The upgrade refuses without a fresh backup, and fails on a bad smoke test."""

import os
import time

import pytest

from scripts.upgrade import UpgradeRefused, fresh_archive, smoke, take_backup


def archive(directory, name: str, age: float = 0.0):
    path = directory / name
    path.write_bytes(b"archive")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def test_the_upgrade_refuses_to_proceed_without_a_fresh_backup(tmp_path):
    # An archive from last week is not a backup of what is about to migrate.
    archive(tmp_path, "office-last-week.tar.gz", age=7 * 24 * 3600)
    calls: list[list[str]] = []

    with pytest.raises(UpgradeRefused) as refused:
        take_backup(tmp_path, runner=lambda command: (calls.append(command), 0)[1])

    assert "wrote no new archive" in str(refused.value)
    assert calls, "the backup was attempted"


def test_a_failing_backup_command_stops_the_upgrade(tmp_path):
    with pytest.raises(UpgradeRefused) as refused:
        take_backup(tmp_path, runner=lambda command: 1)
    assert "nothing was changed" in str(refused.value)


def test_a_backup_written_during_the_run_is_accepted(tmp_path):
    def backup(command: list[str]) -> int:
        archive(tmp_path, "office-now.tar.gz")
        return 0

    assert take_backup(tmp_path, runner=backup).name == "office-now.tar.gz"


def test_the_smoke_test_fails_the_upgrade_rather_than_reporting_success():
    healthy = {
        "/status": {"database": {"state": "ok", "revision": "0021"}},
        "/office/overview": {"counts": {"in_motion": 0}},
        "/setup": {"steps": []},
    }
    assert all(passed for _, passed in smoke(healthy.__getitem__))

    # The office answers, but its database does not: still a failed upgrade.
    broken = {**healthy, "/status": {"database": {"state": "unreachable", "revision": None}}}
    checks = dict(smoke(broken.__getitem__))
    assert checks["database"] is False
    assert checks["schema revision"] is False

    # An endpoint that raises is a failure, not an exception out of the script.
    def explode(path: str) -> dict:
        raise RuntimeError("connection refused")

    assert smoke(explode) == [("the office could not be read: connection refused", False)]


def test_fresh_archive_ignores_files_that_are_not_archives(tmp_path):
    (tmp_path / "notes.txt").write_text("not an archive")
    assert fresh_archive(tmp_path, 0.0) is None
    assert fresh_archive(tmp_path / "missing", 0.0) is None


def test_a_retired_service_is_stopped_by_the_upgrade():
    from scripts.upgrade import BUILD_COMMAND

    assert "--remove-orphans" in BUILD_COMMAND

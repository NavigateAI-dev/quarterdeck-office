"""The package installer runs only its own two commands."""

import json
from pathlib import Path

from app.runtimes import installer


def job(root: Path, **fields: object) -> Path:
    jobs = root / installer.JOBS
    jobs.mkdir(parents=True, exist_ok=True)
    payload = {"id": "a1", "kind": "npm", "cwd": "build"} | fields
    path = jobs / "a1.job.json"
    path.write_text(json.dumps(payload))
    return jobs / "a1.result.json"


async def test_an_unknown_kind_of_install_is_refused(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    result = job(tmp_path, kind="sh")
    assert await installer.serve_once(tmp_path)
    assert json.loads(result.read_text()) == {"error": "install_kind_unknown"}


async def test_a_handed_command_is_never_run(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    result = job(tmp_path, command=["sh", "-c", "touch pwned"])
    assert await installer.serve_once(tmp_path)
    assert json.loads(result.read_text()) == {"error": "manifest_missing"}
    assert not (tmp_path / "build" / "pwned").exists()


async def test_a_folder_outside_the_build_area_is_refused(tmp_path: Path) -> None:
    result = job(tmp_path, cwd="../../etc")
    assert await installer.serve_once(tmp_path)
    assert json.loads(result.read_text())["error"] == "cwd_outside_root"


def test_the_commands_switch_package_code_off() -> None:
    assert "--ignore-scripts" in installer.COMMANDS["npm"]
    assert "--only-binary=:all:" in installer.COMMANDS["pip"]


def test_a_build_asks_for_what_its_manifests_name(tmp_path: Path) -> None:
    assert installer.wanted(tmp_path) == []
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "requirements.txt").write_text("rich")
    assert installer.wanted(tmp_path) == ["npm", "pip"]

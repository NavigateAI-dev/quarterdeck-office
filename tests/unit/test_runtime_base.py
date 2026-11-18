import sys
from pathlib import Path

import pytest

from app.runtimes.base import (
    INHERITED_ENVIRONMENT,
    ExecutionError,
    ExecutionLimits,
    child_environment,
    run_bounded,
)


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


async def test_a_successful_command_reports_its_output(tmp_path: Path) -> None:
    result = await run_bounded(python("print('done')"), tmp_path)
    assert result.succeeded
    assert result.stdout.strip() == "done"
    assert result.exit_code == 0


async def test_a_failing_command_is_never_reported_as_success(tmp_path: Path) -> None:
    result = await run_bounded(python("import sys; sys.exit(3)"), tmp_path)
    assert not result.succeeded
    assert result.exit_code == 3


async def test_a_timeout_kills_the_process_and_fails(tmp_path: Path) -> None:
    with pytest.raises(ExecutionError, match="execution_timeout"):
        await run_bounded(
            python("import time; time.sleep(30)"),
            tmp_path,
            ExecutionLimits(timeout_seconds=0.5),
        )


async def test_oversized_output_is_truncated_and_flagged(tmp_path: Path) -> None:
    result = await run_bounded(
        python("print('x' * 100000)"), tmp_path, ExecutionLimits(max_output_bytes=1000)
    )
    assert result.truncated
    assert len(result.stdout) <= 1000


async def test_arguments_are_never_shell_interpreted(tmp_path: Path) -> None:
    """A fixed argument vector means no value can become shell syntax."""
    marker = tmp_path / "pwned"
    result = await run_bounded(python(f"print({str(marker)!r})"), tmp_path)
    assert result.succeeded
    assert not marker.exists()

    injected = await run_bounded(["echo", f"; touch {marker}"], tmp_path)
    assert injected.succeeded
    assert not marker.exists()


async def test_a_missing_executable_is_reported_distinctly(tmp_path: Path) -> None:
    with pytest.raises(ExecutionError, match="executable_not_found"):
        await run_bounded(["definitely-not-a-real-binary"], tmp_path)


async def test_an_empty_command_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ExecutionError, match="empty_command"):
        await run_bounded([], tmp_path)


def test_the_child_environment_is_an_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repository command must not be able to read provider credentials."""
    monkeypatch.setenv("CHIEF_API_KEY", "provider-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "gateway-secret")
    monkeypatch.setenv("OWNER_API_TOKEN", "owner-secret")
    environment = child_environment()
    assert set(environment) <= set(INHERITED_ENVIRONMENT)
    assert "provider-secret" not in "".join(environment.values())
    assert "github-secret" not in "".join(environment.values())


async def test_the_child_process_cannot_read_withheld_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRITIC_API_KEY", "provider-secret")
    result = await run_bounded(
        python("import os; print(os.environ.get('CRITIC_API_KEY', 'absent'))"), tmp_path
    )
    assert result.stdout.strip() == "absent"


async def test_explicit_extra_values_are_passed(tmp_path: Path) -> None:
    result = await run_bounded(
        python("import os; print(os.environ['FIXTURE_VALUE'])"),
        tmp_path,
        ExecutionLimits(environment={"FIXTURE_VALUE": "supplied"}),
    )
    assert result.stdout.strip() == "supplied"


SPAWN_GRANDCHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open('child.pid', 'w').write(str(child.pid))\n"
    "{tail}\n"
)


def alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child of an exited parent may linger as a zombie until reaped.
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        return waited == 0
    except ChildProcessError:
        state = Path(f"/proc/{pid}/stat")
        if state.exists():
            return state.read_text().split()[2] != "Z"
        import subprocess

        ps = subprocess.run(  # noqa: S603
            ["ps", "-o", "stat=", "-p", str(pid)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        return bool(ps.stdout.strip()) and not ps.stdout.strip().startswith("Z")


async def wait_until_dead(pid: int) -> bool:
    import asyncio

    for _ in range(50):
        if not alive(pid):
            return True
        await asyncio.sleep(0.1)
    return False


async def test_a_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    code = SPAWN_GRANDCHILD.format(tail="time.sleep(60)")
    with pytest.raises(ExecutionError, match="execution_timeout"):
        await run_bounded(python(code), tmp_path, ExecutionLimits(timeout_seconds=2))
    assert await wait_until_dead(int((tmp_path / "child.pid").read_text()))


async def test_background_processes_do_not_outlive_the_command(tmp_path: Path) -> None:
    """A test command that leaves a server running must not leave it running."""
    code = SPAWN_GRANDCHILD.format(tail="child.stdout = None")
    result = await run_bounded(python(code), tmp_path, ExecutionLimits(timeout_seconds=10))
    assert result.succeeded
    assert await wait_until_dead(int((tmp_path / "child.pid").read_text()))


async def test_output_beyond_the_cap_is_discarded_while_streaming(tmp_path: Path) -> None:
    result = await run_bounded(
        python("import sys\nfor _ in range(200): sys.stdout.write('y' * 50000)"),
        tmp_path,
        ExecutionLimits(max_output_bytes=4096),
    )
    assert result.truncated
    assert len(result.stdout) == 4096
    assert result.succeeded


async def test_a_cancelled_command_takes_its_whole_group_with_it(tmp_path: Path) -> None:
    """R04: cancelling the task that waits on a command left it running.

    Worker shutdown cancels its tasks; a command (and anything it started)
    must not outlive the task that owned it, possibly in a discarded workspace.
    """
    import asyncio

    code = SPAWN_GRANDCHILD.format(tail="time.sleep(60)")
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(
        run_bounded(python(code), tmp_path, ExecutionLimits(timeout_seconds=60))
    )
    marker = tmp_path / "child.pid"
    for _ in range(100):
        if marker.exists() and marker.read_text():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await wait_until_dead(int(marker.read_text())), "the grandchild outlived the task"
    await asyncio.sleep(0)
    left = {t for t in asyncio.all_tasks() if not t.done()} - before
    assert not left, f"reader tasks were left running: {left}"

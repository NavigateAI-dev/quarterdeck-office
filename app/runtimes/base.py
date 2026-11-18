import asyncio
import logging
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_OUTPUT_BYTES = 256_000

# Only these are passed to a child process. Everything else, including every
# provider and GitHub credential, is withheld: a repository command has no
# business reading them, and an allowlist fails closed when new settings appear.
INHERITED_ENVIRONMENT = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


class ExecutionError(Exception):
    """A bounded execution failed. Carries a code, never environment values."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True)
class ExecutionResult:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class ExecutionLimits:
    timeout_seconds: float = 300.0
    max_output_bytes: int = MAX_OUTPUT_BYTES
    environment: dict[str, str] = field(default_factory=dict)
    # Each line of output as it arrives, for a live view. Never fails the run.
    on_line: Callable[[str], None] | None = None
    # Asked every few seconds: true stops the command and all it started.
    should_stop: Callable[[], bool] | None = None
    stop_poll_seconds: float = 2.0


def child_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a child environment from an explicit allowlist."""
    environment = {name: os.environ[name] for name in INHERITED_ENVIRONMENT if name in os.environ}
    environment.update(extra or {})
    return environment


async def run_bounded(
    command: list[str], cwd: Path, limits: ExecutionLimits | None = None
) -> ExecutionResult:
    """Run one command with fixed arguments, a timeout, and an output cap.

    The command is a fixed argument vector rather than a shell string, so no
    value can be interpreted as shell syntax. A timeout kills the process and
    is reported as a failure; it is never mistaken for success.
    """
    bounds = limits or ExecutionLimits()
    if not command:
        raise ExecutionError("empty_command")
    started = asyncio.get_running_loop().time()
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            # Nothing to read: a command that waits for input (Codex reads
            # stdin when it is open) would otherwise wait until its timeout.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_environment(bounds.environment),
            # Its own process group, so everything it starts can be stopped.
            start_new_session=True,
        )
    except FileNotFoundError:
        raise ExecutionError("executable_not_found") from None
    except (OSError, ValueError):
        raise ExecutionError("execution_failed") from None

    stdout = _CappedBuffer(bounds.max_output_bytes, bounds.on_line)
    stderr = _CappedBuffer(bounds.max_output_bytes)
    assert process.stdout is not None and process.stderr is not None  # noqa: S101
    readers = asyncio.gather(stdout.drain(process.stdout), stderr.drain(process.stderr))
    try:
        async with asyncio.timeout(bounds.timeout_seconds):
            await _exited(process, bounds.should_stop, bounds.stop_poll_seconds)
    except _Stopped:
        # The owner stopped it: the command and everything it started go.
        _kill_group(process.pid)
        await _exited(process)
        await _finish(readers)
        raise ExecutionError("stopped_by_owner") from None
    except TimeoutError:
        _kill_group(process.pid)
        await _exited(process)
        await _finish(readers)
        raise ExecutionError("execution_timeout") from None
    except asyncio.CancelledError:
        # The task that owned the command was cancelled (a worker shutting
        # down): the command and everything it started go with it, its pipes
        # are closed, and only then does the cancellation carry on.
        _kill_group(process.pid)
        await asyncio.shield(_reap(process, readers))
        raise
    # Anything the command left running (a test server, a watcher) is stopped
    # with it. It would otherwise outlive the attempt and hold the pipes open.
    _kill_group(process.pid)
    await _finish(readers)

    return ExecutionResult(
        command=tuple(command),
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout.text(),
        stderr=stderr.text(),
        duration_seconds=asyncio.get_running_loop().time() - started,
        truncated=stdout.truncated or stderr.truncated,
    )


class _CappedBuffer:
    """Keeps at most `limit` bytes while reading a stream to its end.

    Excess output is read and discarded, so a chatty process can neither fill
    memory nor block on a full pipe.
    """

    def __init__(self, limit: int, on_line: Callable[[str], None] | None = None) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False
        self.on_line = on_line
        self.partial = bytearray()

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65536):
            room = self.limit - len(self.data)
            if room > 0:
                self.data.extend(chunk[:room])
            if len(chunk) > max(room, 0):
                self.truncated = True
            if self.on_line is not None:
                self._lines(chunk)
        if self.on_line is not None and self.partial:
            self._say(bytes(self.partial))

    def _lines(self, chunk: bytes) -> None:
        self.partial.extend(chunk)
        while b"\n" in self.partial:
            line, _, rest = bytes(self.partial).partition(b"\n")
            self.partial = bytearray(rest)
            self._say(line)
        if len(self.partial) > 1_000_000:
            self.partial.clear()  # A line that never ends is not a progress line.

    def _say(self, line: bytes) -> None:
        if self.on_line is None:
            return
        try:
            self.on_line(line.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - a live view never fails the command
            logging.getLogger("quarterdeck.runtime").warning('{"event":"progress_line_dropped"}')

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


class _Stopped(Exception):
    """The owner asked for the command to stop."""


async def _exited(
    process: asyncio.subprocess.Process,
    should_stop: Callable[[], bool] | None = None,
    poll_seconds: float = 2.0,
) -> None:
    """Wait for the process itself to exit, or for the owner to stop it.

    ``Process.wait()`` also waits for its pipes to close, which never happens
    while a background grandchild holds them, so the exit status is polled.
    """
    waited = 0.0
    while process.returncode is None:
        await asyncio.sleep(0.05)
        waited += 0.05
        if should_stop is not None and waited >= poll_seconds:
            waited = 0.0
            try:
                stop = should_stop()
            except Exception:  # noqa: BLE001 - an unreadable answer is "go on"
                stop = False
            if stop:
                raise _Stopped


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _reap(process: asyncio.subprocess.Process, readers: "asyncio.Future[Any]") -> None:
    """Wait for a killed group's leader, then for its pipes, briefly."""
    try:
        async with asyncio.timeout(5):
            await _exited(process)
    except TimeoutError:
        pass
    await _finish(readers)


async def _finish(readers: "asyncio.Future[Any]") -> None:
    """Wait briefly for the pipes to close once the group is gone."""
    try:
        async with asyncio.timeout(5):
            await readers
    except TimeoutError:
        readers.cancel()

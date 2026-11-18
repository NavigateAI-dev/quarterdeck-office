"""A small browser driver for the office's end-to-end tests.

It speaks the Chrome DevTools Protocol over a WebSocket, using the
`websockets` dependency the office already has and whatever Chromium is
already installed. No test-only browser dependency is added, and the tests
skip themselves where no browser exists.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import websockets

DEVTOOLS = re.compile(r"ws://[^\s]+")
LAUNCH_TIMEOUT = 30.0
CALL_TIMEOUT = 30.0
CANDIDATES = (
    "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/"
    "chrome-headless-shell",
    "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Google Chrome for Testing.app/"
    "Contents/MacOS/Google Chrome for Testing",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def find_browser() -> str | None:
    """Any Chromium already on this machine, or None to skip the tests."""
    for pattern in CANDIDATES:
        expanded = Path(pattern).expanduser()
        matches = sorted(Path("/").glob(str(expanded).lstrip("/")))
        for match in matches:
            if match.is_file() and os.access(match, os.X_OK):
                return str(match)
    for name in ("chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


class Page:
    """One tab, with the few operations the office's tests need."""

    def __init__(self, socket: Any, session: str) -> None:
        self.socket = socket
        self.session = session
        self.counter = 0
        self.console: list[str] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.counter += 1
        message_id = self.counter
        await self.socket.send(
            json.dumps(
                {
                    "id": message_id,
                    "method": method,
                    "params": params or {},
                    "sessionId": self.session,
                }
            )
        )
        async with asyncio.timeout(CALL_TIMEOUT):
            while True:
                message = json.loads(await self.socket.recv())
                self._record(message)
                if message.get("id") == message_id:
                    if "error" in message:
                        raise RuntimeError(f"{method}: {message['error']}")
                    return dict(message.get("result") or {})

    def _record(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails", {})
            text = detail.get("exception", {}).get("description") or detail.get("text", "")
            self.console.append(f"exception: {text}")
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            parts = [
                str(arg.get("value", arg.get("description", ""))) for arg in params.get("args", [])
            ]
            self.console.append("console.error: " + " ".join(parts))

    async def goto(self, url: str) -> None:
        await self.call("Page.navigate", {"url": url})
        await self.settle()

    async def settle(self, seconds: float = 1.2) -> None:
        """Let the office's own fetches finish; it renders after them."""
        await asyncio.sleep(seconds)

    async def evaluate(self, expression: str) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if result.get("exceptionDetails"):
            raise AssertionError(f"page error: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    async def viewport(self, width: int, height: int, scale: float = 1.0) -> None:
        await self.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": scale, "mobile": False},
        )

    async def press(self, key: str, code: str, key_code: int) -> None:
        for kind in ("rawKeyDown", "keyUp"):
            await self.call(
                "Input.dispatchKeyEvent",
                {"type": kind, "key": key, "code": code, "windowsVirtualKeyCode": key_code},
            )
        await self.settle(0.4)


def _stop(process: subprocess.Popen[str]) -> None:
    """Close the browser and its pipes, so no child is left behind."""
    process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - a browser that will not die
        pass
    for stream in (process.stderr, process.stdout):
        if stream is not None:
            stream.close()


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _endpoint(profile: Path, process: subprocess.Popen[str], port: int) -> str:
    """The browser answers on its debugging port once it is ready."""
    import urllib.error
    import urllib.request

    deadline = asyncio.get_running_loop().time() + LAUNCH_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed loopback URL
                f"http://127.0.0.1:{port}/json/version", timeout=2
            ) as response:
                return str(json.load(response)["webSocketDebuggerUrl"])
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            await asyncio.sleep(0.2)
    output = ""
    if process.stderr is not None and process.poll() is not None:
        output = process.stderr.read()[-400:]
    _stop(process)
    raise RuntimeError(f"the browser did not start: {output.strip() or 'no output'}")


@asynccontextmanager
async def browser(url_base: str) -> AsyncIterator[Page]:
    binary = find_browser()
    if binary is None:  # pragma: no cover - environment without a browser
        raise RuntimeError("no browser found")
    profile = Path(tempfile.mkdtemp(prefix="office-e2e-"))
    port = _free_port()
    process = subprocess.Popen(  # noqa: S603 - fixed argument vector
        [
            binary,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            # Never touch the machine's keychain: the tests need no stored
            # password, and macOS would ask the owner for permission.
            "--use-mock-keychain",
            "--password-store=basic",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--window-size=1440,900",
            "about:blank",
        ],
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
    )
    endpoint = await _endpoint(profile, process, port)
    socket = await websockets.connect(endpoint, max_size=32 * 1024 * 1024)
    page = Page(socket, "")
    try:
        target = await page.call("Target.createTarget", {"url": "about:blank"})
        attached = await page.call(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )
        page.session = attached["sessionId"]
        await page.call("Page.enable")
        await page.call("Runtime.enable")
        await page.viewport(1440, 900)
        await page.goto(url_base)
        yield page
    finally:
        await socket.close()
        _stop(process)
        shutil.rmtree(profile, ignore_errors=True)

"""Start the local office and open it signed in.

    python3 -m scripts.launch [--no-open]

Runs preflight checks, creates private local configuration on first start,
starts the pinned services, waits for health, and opens the browser with a
one-time sign-in code in the URL fragment. The owner token is never printed.
Only the standard library is used, so this runs before any dependency install.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8000"
PORTS = (8000, 5433)
MINIMUM_FREE_BYTES = 5 * 1024**3
HEALTH_TIMEOUT_SECONDS = 240


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    fix: str = ""


def run(command: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argument vectors only
        command, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )


def check_docker() -> Check:
    if shutil.which("docker") is None:
        return Check("docker", False, "Install Docker Desktop, then run this again.")
    try:
        if run(["docker", "info"], timeout=20).returncode != 0:
            return Check("docker", False, "Start Docker Desktop and wait until it is running.")
        if run(["docker", "compose", "version"]).returncode != 0:
            return Check("docker", False, "Update Docker Desktop; Docker Compose v2 is required.")
    except subprocess.TimeoutExpired:
        return Check("docker", False, "Docker did not respond. Restart Docker Desktop.")
    return Check("docker", True)


def office_running() -> bool:
    try:
        result = run(["docker", "compose", "ps", "--status", "running", "--services"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "api" in result.stdout.split() or "postgres" in result.stdout.split()


def check_port(port: int, owned_by_office: bool) -> Check:
    if owned_by_office:
        return Check(f"port {port}", True)
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return Check(
                f"port {port}",
                False,
                f"Port {port} is in use by another program. Stop it, then run this again.",
            )
    return Check(f"port {port}", True)


def check_disk(path: Path, minimum_bytes: int = MINIMUM_FREE_BYTES) -> Check:
    free = shutil.disk_usage(path).free
    if free < minimum_bytes:
        needed = minimum_bytes // 1024**3
        return Check("disk", False, f"Free at least {needed} GB of disk space.")
    return Check("disk", True)


def env_value(env_file: Path, name: str) -> str | None:
    for line in env_file.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1]
    return None


def sign_in_url(base: str, code: str) -> str:
    return f"{base}/#code={code}"


def healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as response:  # noqa: S310
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


def mint_code(token: str) -> str:
    request = urllib.request.Request(  # noqa: S310 - fixed loopback URL
        f"{BASE_URL}/session/code",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return str(json.load(response)["code"])


def start_calendar_helper(restart: bool = False) -> None:
    """On a Mac, the helper that reads its Calendar app. It asks macOS
    nothing until the owner presses "Use the calendars on this Mac"."""
    python = ROOT / ".venv" / "bin" / "python"
    if sys.platform != "darwin" or not python.exists():
        return
    try:
        if restart:
            run([str(python), "-m", "scripts.mac_calendar", "stop"], timeout=30)
        run([str(python), "-m", "scripts.mac_calendar", "start"], timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        print("note the calendar helper did not start; Calendars says how to start it.")


def main() -> int:
    open_browser = "--no-open" not in sys.argv
    if "--pick-folder" in sys.argv:
        from scripts.pick_folder import main as pick_folder

        if pick_folder() != 0:
            return 1
    running = office_running()
    checks = [check_docker(), check_disk(ROOT), *(check_port(p, running) for p in PORTS)]
    failed = [check for check in checks if not check.ok]
    for check in checks:
        print(
            f"{'ok  ' if check.ok else 'FAIL'} {check.name}{'' if check.ok else ': ' + check.fix}"
        )
    if failed:
        return 1

    env_file = ROOT / ".env"
    if not env_file.exists() or not (ROOT / ".secrets" / "root.key").exists():
        print("First start: creating private local configuration.")
        from scripts.init_local import main as init_local

        init_local()

    # The folder the office may read, mounted read-only into the sources service.
    (ROOT / "sources").mkdir(exist_ok=True)
    services = ["api", "runner", "sources", "installer"]
    command = ["docker", "compose"]
    print("Starting the office (the first start builds images and takes a few minutes).")
    started = run([*command, "up", "-d", "--build", *services], timeout=1800)
    if started.returncode != 0:
        print("FAIL start: Docker could not start the services. Run `docker compose logs`.")
        return 1

    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while not healthy():
        if time.monotonic() > deadline:
            print("FAIL health: the office did not become ready. Run `docker compose logs api`.")
            return 1
        time.sleep(2)
    print(f"ok   office ready at {BASE_URL}/")
    start_calendar_helper()

    token = env_value(env_file, "OWNER_API_TOKEN")
    if not token:
        print("Sign in is unavailable: OWNER_API_TOKEN is not set in .env.")
        return 1
    url = sign_in_url(BASE_URL, mint_code(token))
    if open_browser and webbrowser.open(url):
        print("Opened your browser, signed in. The sign-in link works once, for 2 minutes.")
    else:
        print(f"Open this link within 2 minutes to sign in (works once):\n{url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The remote route's control loop (MA3). Standard library only.

Starts tailscaled in userspace mode, writes what it finds to a status file
every few seconds, and runs the office's requests from the exchange folder,
each with documented `tailscale` commands only:

- connect: `tailscale up --hostname=quarterdeck`, with a one-time auth key when
  given (read from a file, never on the command line), else the login link
  Tailscale prints, shown to the owner;
- route_on: `tailscale serve --bg --https=443 http://127.0.0.1:8000`;
- route_off: `tailscale serve reset`;
- forget: `tailscale serve reset`, then `tailscale logout`.

Funnel is never run. A key is deleted as soon as it is read and never logged.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

EXCHANGE = Path(os.environ.get("REMOTE_EXCHANGE", "/remote-exchange"))
STATE_DIR = os.environ.get("REMOTE_STATE_DIR", "/var/lib/tailscale")
HOSTNAME = os.environ.get("REMOTE_HOSTNAME", "quarterdeck")
TARGET = "http://127.0.0.1:8000"
# Where the official image puts them.
TAILSCALE = "/usr/local/bin/tailscale"
TAILSCALED = "/usr/local/bin/tailscaled"
# A one-time key, for the moment `tailscale up` reads it; inside this container.
KEY_FILE = Path("/tmp/remote-auth-key")  # noqa: S108
STATUS_FILE = "remote.status.json"
EVERY = 5.0
LOGIN_URL = re.compile(r"https://login\.tailscale\.com/\S+")

Runner = Callable[[list[str], float], tuple[int, str]]


def run(command: list[str], timeout: float = 30.0) -> tuple[int, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    except subprocess.TimeoutExpired as late:
        said = (late.stdout or b"") + (late.stderr or b"")
        return 124, said.decode(errors="replace") if isinstance(said, bytes) else str(said)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def routed(serve: dict[str, Any], dns_name: str) -> bool:
    """Whether Serve sends https://<name>/ to the office, and nothing else."""
    if not dns_name:
        return False
    handlers = ((serve.get("Web") or {}).get(f"{dns_name}:443") or {}).get("Handlers") or {}
    return (handlers.get("/") or {}).get("Proxy") == TARGET and not serve.get("AllowFunnel")


def summary(status: dict[str, Any], serve: dict[str, Any]) -> dict[str, Any]:
    me = status.get("Self") or {}
    dns_name = str(me.get("DNSName") or "").rstrip(".")
    return {
        "backend": str(status.get("BackendState") or "Unknown"),
        "auth_url": str(status.get("AuthURL") or ""),
        "dns_name": dns_name,
        "online": bool(me.get("Online")),
        "https": bool(status.get("CertDomains")),
        "routed": routed(serve, dns_name),
        "funnel": bool(serve.get("AllowFunnel")),
        "health": [str(item) for item in status.get("Health") or []][:5],
        "at": time.time(),
    }


def observe(runner: Runner = run) -> dict[str, Any]:
    code, said = runner([TAILSCALE, "status", "--json"], 10.0)
    if code != 0 and not said.strip().startswith("{"):
        return {"backend": "NotRunning", "error": said.strip()[:300], "at": time.time()}
    _, served = runner([TAILSCALE, "serve", "status", "--json"], 10.0)
    return summary(_json(said), _json(served))


def connect(job: dict[str, Any], runner: Runner = run) -> dict[str, Any]:
    command = [TAILSCALE, "up", f"--hostname={HOSTNAME}", "--accept-dns=false"]
    key = str(job.get("auth_key") or "")
    if key:
        held = KEY_FILE
        held.write_text(key)
        held.chmod(0o600)
        try:
            code, said = runner([*command, f"--auth-key=file:{held}", "--timeout=60s"], 70.0)
        finally:
            held.unlink(missing_ok=True)
        return {"ok": code == 0, "said": said.replace(key, "…")[-400:]}
    # No key: Tailscale prints a login link and waits for it; keep it waiting.
    subprocess.Popen(  # noqa: S603
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
    for _ in range(20):
        found = observe(runner)
        if found.get("auth_url") or found.get("backend") == "Running":
            return {"ok": True, **found}
        time.sleep(0.5)
    return {"ok": False, "said": "Tailscale gave no login link yet; try again."}


def route_on(runner: Runner = run) -> dict[str, Any]:
    code, said = runner([TAILSCALE, "serve", "--bg", "--https=443", TARGET], 25.0)
    found = observe(runner)
    if found.get("routed"):
        return {"ok": True, **found}
    link = LOGIN_URL.search(said)
    return {
        "ok": False,
        # Serve asks for HTTPS certificates to be enabled in the tailnet once.
        "needs_https": bool(link),
        "url": link.group(0) if link else "",
        "said": said[-400:],
        **found,
    }


def route_off(runner: Runner = run) -> dict[str, Any]:
    runner([TAILSCALE, "serve", "reset"], 15.0)
    found = observe(runner)
    return {"ok": not found.get("routed"), **found}


def forget(runner: Runner = run) -> dict[str, Any]:
    runner([TAILSCALE, "serve", "reset"], 15.0)
    code, said = runner([TAILSCALE, "logout"], 20.0)
    return {"ok": code == 0, "said": said[-300:], **observe(runner)}


ACTIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "status": lambda job: {"ok": True, **observe()},
    "connect": connect,
    "route_on": lambda job: route_on(),
    "route_off": lambda job: route_off(),
    "forget": lambda job: forget(),
}


def answer(job_file: Path) -> None:
    try:
        job = json.loads(job_file.read_text())
    except (OSError, ValueError):
        job = {}
    job_file.unlink(missing_ok=True)  # it may hold a key: gone before anything runs
    action = ACTIONS.get(str(job.get("action")))
    try:
        result = action(job) if action else {"ok": False, "said": "unknown request"}
    except Exception as failed:  # noqa: BLE001 - reported to the office, never raised
        result = {"ok": False, "said": type(failed).__name__}
    write(job_file.with_name(job_file.name.replace(".job.json", ".result.json")), result)


def write(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data))
    os.replace(temporary, path)


def main() -> None:
    EXCHANGE.mkdir(parents=True, exist_ok=True)
    daemon = subprocess.Popen(  # noqa: S603
        [
            TAILSCALED,
            "--tun=userspace-networking",
            f"--statedir={STATE_DIR}",
        ]
    )
    last = 0.0
    while daemon.poll() is None:
        for job_file in sorted(EXCHANGE.glob("*.job.json")):
            answer(job_file)
        if time.time() - last > EVERY:
            write(EXCHANGE / STATUS_FILE, observe())
            last = time.time()
        time.sleep(0.3)
    print(json.dumps({"event": "tailscaled_stopped", "code": daemon.returncode}), flush=True)
    sys.exit(1)


if __name__ == "__main__":
    main()

"""Publish the office on this Mac's home network, for a phone, or stop.

    python3 -m scripts.access home        # publish on the Mac's network address
    python3 -m scripts.access home --off  # back to this computer only

It finds the Mac's own network addresses (never Docker's), writes them with
OFFICE_BIND=0.0.0.0 into .env, and recreates the office container. Nothing
else on the Mac is changed: no firewall rule, no sleep setting. Publishing
alone lets nobody in: the office answers an address only once it is turned on
and checked under Settings → Mobile & remote access, and every page still
needs a sign-in.

Standard library only, like the launcher.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
PORT = 8000
INTERFACES = ("en0", "en1")
# The remote route shares the office container's network: restarted with it.
RECREATE = ["docker", "compose", "up", "-d", "--force-recreate", "--no-deps", "api", "remote"]

Runner = Callable[[list[str]], str]


def _run(command: list[str]) -> str:
    done = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603
    return done.stdout.strip() if done.returncode == 0 else ""


def lan_addresses(run: Runner = _run) -> list[str]:
    """The Mac's private IPv4 addresses on Wi-Fi and Ethernet, then its .local name."""
    found: list[str] = []
    for interface in INTERFACES:
        ip = run(["ipconfig", "getifaddr", interface])
        private = re.fullmatch(r"(10|192\.168|172\.(1[6-9]|2\d|3[01]))(\.\d+){2,3}", ip)
        if ip and private and f"http://{ip}:{PORT}" not in found:
            found.append(f"http://{ip}:{PORT}")
    name = run(["scutil", "--get", "LocalHostName"])
    if found and re.fullmatch(r"[A-Za-z0-9-]+", name):
        found.append(f"http://{name.lower()}.local:{PORT}")
    return found


def with_values(text: str, values: dict[str, str]) -> str:
    """The .env text with these settings replaced, or added at the end."""
    lines = text.splitlines()
    for name, value in values.items():
        at = next((i for i, line in enumerate(lines) if line.startswith(f"{name}=")), None)
        if at is None:
            lines.append(f"{name}={value}")
        else:
            lines[at] = f"{name}={value}"
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, run: Runner = _run, env_file: Path = ENV_FILE) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.access")
    parser.add_argument("mode", choices=["home"])
    parser.add_argument("--off", action="store_true", help="this computer only again")
    args = parser.parse_args(argv)
    if not env_file.exists():
        print("No .env here: run `python3 -m scripts.init_local` first.")
        return 2
    if args.off:
        values = {"OFFICE_BIND": "127.0.0.1", "OFFICE_LAN_ADDRESSES": ""}
    else:
        found = lan_addresses(run)
        if not found:
            print(
                "This Mac has no home-network address on Wi-Fi or Ethernet. Connect it, then retry."
            )
            return 1
        values = {"OFFICE_BIND": "0.0.0.0", "OFFICE_LAN_ADDRESSES": ",".join(found)}  # noqa: S104
    env_file.write_text(with_values(env_file.read_text(), values))
    print("Restarting the office…")
    done = subprocess.run(RECREATE, cwd=env_file.parent, check=False)  # noqa: S603
    if done.returncode != 0:
        print("The office did not restart; run `docker compose up -d api` and check its log.")
        return 1
    if args.off:
        print("The office answers on this computer only again.")
        return 0
    print("Published on this Mac's network at:")
    for address in found:
        print(f"  {address}")
    print(
        "Nothing is reachable yet. Turn it on under Settings → Mobile & remote access,\n"
        "then scan the code with the phone on the same Wi-Fi. The Mac must stay on,\n"
        "awake and running Docker for the phone to reach it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

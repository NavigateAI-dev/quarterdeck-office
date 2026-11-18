"""Re-check the tool servers catalog, for a person to review.

    python3 -m scripts.mcp_catalog [--registry]

For each entry in app/mcp/catalog.json it sends one unauthenticated MCP
`initialize` and reports whether the server still answers and still asks for
the sign-in the catalog says. With --registry it also looks the entry up in
the official MCP Registry and reports whether the registry still lists that
address. It changes nothing: the catalog is edited by a person, and the office
never installs anything from the registry by itself.

Standard library only, like the launcher.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "app" / "mcp" / "catalog.json"
REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers"
TIMEOUT = 20
# Named, as the office names itself when it reads a page: several of these
# hosts refuse Python's default agent outright (403) while answering any other.
USER_AGENT = "Quarterdeck/1.0 (catalog check; single owner; contact: local operator)"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "quarterdeck-catalog-check", "version": "1"},
    },
}


def sign_in_seen(status: int | None, challenge: str) -> str:
    """What the server's answer says about signing in."""
    if status == 200:
        return "none"
    if status == 401:
        return "sign-in"
    return "not answering"


def drift(entry: dict[str, Any], seen: str) -> str | None:
    """Why the entry no longer matches the server, or None if it still does."""
    if seen == "not answering":
        return "not answering: check the address or remove the entry"
    if entry["sign_in"] == "none" and seen == "sign-in":
        return "now asks for sign-in: record how, or remove the entry"
    if entry["sign_in"] in ("key", "account") and seen == "none":
        return "no longer asks for sign-in: check whether a key is still needed"
    return None


def probe(url: str) -> tuple[int | None, str]:
    request = urllib.request.Request(  # noqa: S310 - addresses come from the reviewed catalog
        url,
        data=json.dumps(INITIALIZE).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return response.status, response.headers.get("WWW-Authenticate", "")
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("WWW-Authenticate", "") or ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, ""


def registry_lists(name: str, url: str) -> bool | None:
    """Whether the registry's latest entry of this name still has this address."""
    query = urllib.parse.urlencode({"search": name, "limit": 100})
    request = urllib.request.Request(  # noqa: S310 - the fixed registry address
        f"{REGISTRY}?{query}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    for row in data.get("servers", []):
        meta = row.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {})
        server = row.get("server", {})
        if server.get("name") != name or not meta.get("isLatest"):
            continue
        return any(
            remote.get("url", "").rstrip("/") == url.rstrip("/")
            for remote in server.get("remotes") or []
        )
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", action="store_true", help="also check the MCP Registry")
    args = parser.parse_args(argv)
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    print(f"catalog last checked {catalog['checked_on']}")
    problems = 0
    for entry in catalog["entries"]:
        status, challenge = probe(entry["url"])
        seen = sign_in_seen(status, challenge)
        found = drift(entry, seen)
        line = f"{'ok  ' if found is None else 'DIFF'} {entry['id']}: {seen} ({status})"
        if found:
            line += f" — {found}"
            problems += 1
        if args.registry and entry.get("registry"):
            listed = registry_lists(entry["registry"], entry["url"])
            line += {
                True: "; registry agrees",
                False: "; REGISTRY DIFFERS",
                None: "; registry unreachable",
            }[listed]
            problems += listed is False
        print(line)
    print(f"{problems} entr{'y' if problems == 1 else 'ies'} to review")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

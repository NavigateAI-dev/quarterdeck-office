"""Putting a finished website online.

Two places, each with the owner's own account and token: Netlify (the site
uploaded as a zip, a public https address at once) and GitHub Pages (a public
repository in the owner's account, its files uploaded, Pages switched on).
Publishing is always the owner's press, after saying it will be public.

The token is sealed in the secret store and sent to that company only. A
GitHub repository is only ever written when this build created it: a name the
owner already uses for something else is refused, never overwritten.
"""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app import builds

TIMEOUT = httpx.Timeout(60.0, connect=15.0)
NETLIFY = "https://api.netlify.com/api/v1"
GITHUB = "https://api.github.com"
TOKENS = {"netlify": "publish-netlify-token", "github": "publish-github-token"}
# What a website needs; what only the office or the build's checks need.
NOT_PUBLISHED = {"check.sh", ".gitignore", "package-lock.json"}
SITE_FOLDERS = ("dist", "build", "public", "out", "_site")
MAX_FILES = 300


class HostingError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def site_root(folder: Path) -> Path:
    """Where the website is: a built folder (dist/, build/, …) or the build itself."""
    for name in SITE_FOLDERS:
        if (folder / name / "index.html").is_file():
            return folder / name
    if (folder / "index.html").is_file():
        return folder
    raise HostingError("no_website")


def site_files(root: Path) -> list[tuple[str, bytes]]:
    """The website's files, read only from inside it."""
    found = []
    for entry in builds.files(root):
        path = str(entry["path"])
        if Path(path).name in NOT_PUBLISHED:
            continue
        data, _ = builds.read_file(root, path)
        found.append((path, data))
        if len(found) > MAX_FILES:
            raise HostingError("website_too_large")
    if not any(path == "index.html" for path, _ in found):
        raise HostingError("no_website")
    return found


def record_path(settings: Any, key: str) -> Path:
    return builds.root_of(settings) / f"{key}.publish.json"


def published(settings: Any, key: str) -> list[dict[str, Any]]:
    try:
        return list(json.loads(record_path(settings, key).read_text()))
    except (OSError, ValueError):
        return []


def remember(settings: Any, key: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in published(settings, key) if row.get("target") != entry["target"]]
    rows.append(entry | {"at": datetime.now(UTC).isoformat()})
    path = record_path(settings, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))
    return rows


def slug(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return cleaned[:60] or "office-site"


def _zip(files: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in files:
            archive.writestr(path, data)
    return buffer.getvalue()


def _checked(response: httpx.Response, refused: str = "host_refused") -> dict[str, Any]:
    if response.status_code in (401, 403):
        raise HostingError("token_refused")
    if response.status_code >= 400:
        raise HostingError(refused)
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def to_netlify(
    token: str,
    files: list[tuple[str, bytes]],
    site_id: str | None,
    http: httpx.AsyncClient,
) -> dict[str, Any]:
    """Upload the site; a site published before gets the new version."""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if not site_id:
            site = _checked(await http.post(f"{NETLIFY}/sites", json={}, headers=headers))
            site_id = str(site.get("id") or "")
            if not site_id:
                raise HostingError("host_refused")
        deploy = _checked(
            await http.post(
                f"{NETLIFY}/sites/{site_id}/deploys",
                content=_zip(files),
                headers=headers | {"Content-Type": "application/zip"},
            )
        )
        site = _checked(await http.get(f"{NETLIFY}/sites/{site_id}", headers=headers))
    except httpx.HTTPError:
        raise HostingError("host_unreachable") from None
    url = str(site.get("ssl_url") or site.get("url") or deploy.get("ssl_url") or "")
    return {"target": "netlify", "site_id": site_id, "url": url}


async def to_github(
    token: str,
    name: str,
    files: list[tuple[str, bytes]],
    repository: str | None,
    http: httpx.AsyncClient,
) -> dict[str, Any]:
    """A public repository of the site's files, with Pages switched on."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        login = str(_checked(await http.get(f"{GITHUB}/user", headers=headers)).get("login") or "")
        if not login:
            raise HostingError("token_refused")
        repo = repository or f"{login}/{slug(name)}"
        if repository is None:
            made = await http.post(
                f"{GITHUB}/user/repos",
                json={
                    "name": repo.split("/", 1)[1],
                    "description": "A website built and published by the Quarterdeck",
                    "has_issues": False,
                    "has_wiki": False,
                    "auto_init": False,
                },
                headers=headers,
            )
            if made.status_code == 422:
                # The name is taken: never write into a repository this build did not make.
                raise HostingError("repository_name_taken")
            _checked(made)
        for path, data in files:
            current = await http.get(
                f"{GITHUB}/repos/{repo}/contents/{path}", params={"ref": "main"}, headers=headers
            )
            sha = current.json().get("sha") if current.status_code == 200 else None
            body: dict[str, Any] = {
                "message": f"Publish {path}",
                "content": base64.b64encode(data).decode(),
                "branch": "main",
            }
            if sha:
                body["sha"] = sha
            _checked(
                await http.put(f"{GITHUB}/repos/{repo}/contents/{path}", json=body, headers=headers)
            )
        pages = await http.post(
            f"{GITHUB}/repos/{repo}/pages",
            json={"source": {"branch": "main", "path": "/"}},
            headers=headers,
        )
        if pages.status_code not in (201, 409):
            _checked(pages, "pages_refused")
    except httpx.HTTPError:
        raise HostingError("host_unreachable") from None
    owner, _, project = repo.partition("/")
    return {
        "target": "github",
        "repository": repo,
        "url": f"https://{owner.lower()}.github.io/{project}/",
    }

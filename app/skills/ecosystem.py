"""Skills from the open Agent Skills ecosystem: skills.sh and GitHub.

A skill there is a folder with a SKILL.md (name, description, instructions),
sometimes scripts and reference notes, in a public GitHub repository. The
office reads it the way it reads everything from outside: over public HTTPS,
on the allow-list, and as data. What it keeps is instructions only:

- pinned to the commit it was read at, so what the owner reviewed is what runs;
- scripts and anything executable refused, and named, so the owner knows what
  the skill loses in the office;
- what the skill assumes the office cannot do (write files, run commands, use
  tools it lacks), said plainly beside it;
- skills.sh's security audits (Gen Agent Trust Hub, Socket, Snyk) shown as
  information, never as the decision: skills.sh itself says it cannot
  guarantee any skill.

skills.sh's API only accepts apps hosted on Vercel, so the office reads GitHub
directly and the audit panel from the skill's public page.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import yaml

from app.mcp.client import McpError, check_address

TIMEOUT_SECONDS = 20.0
USER_AGENT = "Quarterdeck/1.0 (skill intake; +https://github.com)"
# How much of a skill the office keeps: its SKILL.md and reference notes.
MAX_INSTRUCTIONS = 24_000
MAX_FILE_BYTES = 200_000
# A large repository's file listing is far bigger than any skill file:
# anthropics/skills' listing passed the file limit on its own.
MAX_LISTING_BYTES = 8_000_000
# Files a skill may carry that the office will not install: code of any kind.
EXECUTABLE = frozenset(
    {
        ".py",
        ".sh",
        ".bash",
        ".zsh",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".rb",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".php",
        ".pl",
        ".lua",
        ".ps1",
        ".bat",
        ".cmd",
        ".exe",
        ".bin",
        ".jar",
        ".wasm",
        ".so",
        ".dylib",
        ".dll",
        ".swift",
        ".c",
        ".cc",
        ".cpp",
        ".h",
    }
)
NOT_REFERENCE = re.compile(
    r"^(readme|license|licence|changelog|contributing|code_of_conduct)", re.I
)
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)

# What a skill written for a coding agent may assume that the office cannot do.
ASSUMES = (
    (
        "files",
        re.compile(
            r"current directory|working directory|\.agents/|\.claude/|"
            r"\b(write|save|create|read)\b[^.\n]{0,60}\b(file|folder|directory)\b|"
            r"\bwrite (it )?to [\w./<>-]+\.(md|json|txt)",
            re.I,
        ),
        "It reads or writes files in a working directory. In the office its result is the "
        "step's output instead, and there is no working directory to read from.",
    ),
    (
        "shell",
        re.compile(
            r"```(bash|sh|shell|zsh|console)|`(npx|npm|pnpm|pip|python3?|git|curl|bash|make)\b|"
            r"\brun (the |this )?(command|script)",
            re.I,
        ),
        "It runs commands. The office runs none, so those parts are skipped.",
    ),
    (
        "tools",
        re.compile(r"\b(playwright|puppeteer|mcp__|browser tool|claude code|cursor)\b", re.I),
        "It expects tools of a coding agent. The office's researcher reads public pages and "
        "uses the tools you granted a project, and nothing else.",
    ),
    (
        "interview",
        re.compile(r"\b(ask the user|interview the user|grill|ask (me|them) (one|each))\b", re.I),
        "It interviews the user as it goes. In the office the work does not stop to chat: the "
        "Chief asks you its questions before the work starts.",
    ),
)
NEEDS_WEB = re.compile(
    r"\b(search the web|web search|google|look (it )?up online|browse the web)\b", re.I
)
NEEDS_DOCUMENTS = re.compile(r"\b(local (documents|files)|knowledge base|your documents)\b", re.I)
# Research first: "audit, review or diagnose" describes looking into a site,
# not checking another agent's work (seo-audit was first suggested for the
# critic). A name decides before a description does.
ROLE_WORDS = (
    (
        "researcher",
        re.compile(r"\b(research|find|investigat|analy[sz]|audit|explore|search|diagnos)\w*", re.I),
    ),
    ("critic", re.compile(r"\b(review|verify|verification|critique|check)\w*", re.I)),
)
WRITING = re.compile(r"\b(write|writing|draft|copy|questionnaire|prd|brief|report|plan)\w*", re.I)
AUDIT = re.compile(
    r"Gen Agent Trust Hub\s*(Pass|Warn|Fail|Pending|Safe|Low Risk|Med Risk|High Risk)?.{0,80}?"
    r"Socket\s*(Pass|Warn|Fail|Pending|\d+ alerts?)?.{0,80}?"
    r"Snyk\s*(Pass|Warn|Fail|Pending|Low Risk|Med Risk|High Risk)?",
    re.S,
)


class EcosystemError(Exception):
    """The skill could not be read; `code` says why, `choices` which to pick."""

    def __init__(self, code: str, choices: list[str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.choices = choices or []


@dataclass
class Found:
    """One skill as read: what the owner reviews and what the office installs."""

    owner: str
    repo: str
    path: str
    commit: str
    name: str
    description: str
    instructions: str
    refused: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    assumptions: list[dict[str, str]] = field(default_factory=list)
    audits: dict[str, str] = field(default_factory=dict)
    permissions: list[str] = field(default_factory=list)
    suggested_role: str = "summarist"

    def view(self) -> dict[str, Any]:
        return {
            "source": f"{self.owner}/{self.repo}",
            "path": self.path,
            "commit": self.commit,
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "characters": len(self.instructions),
            "refused": self.refused,
            "omitted": self.omitted,
            "assumptions": self.assumptions,
            "audits": self.audits,
            "permissions": self.permissions,
            "suggested_role": self.suggested_role,
            "skills_sh": f"https://www.skills.sh/{self.owner}/{self.repo}/{self.folder_name()}",
        }

    def folder_name(self) -> str:
        parts = self.path.split("/")
        return parts[-2] if len(parts) > 1 else self.name


def parse_source(text: str) -> tuple[str, str, str | None]:
    """Owner, repository and skill from any way a person would name them."""
    raw = " ".join((text or "").split())
    if not raw:
        raise EcosystemError("source_missing")
    named = re.search(r"--skill[= ]+([A-Za-z0-9._-]+)", raw)
    skill = named.group(1) if named else None
    words = [
        word
        for word in raw.split(" ")
        if word not in ("npx", "skills", "add") and not word.startswith("--")
    ]
    if named:
        words = [word for word in words if word != skill]
    if not words:
        raise EcosystemError("source_unreadable")
    first = words[0]
    if first.startswith(("http://", "https://")):
        url = urlparse(first)
        host = (url.hostname or "").lower()
        parts = [part for part in url.path.split("/") if part]
        if host in ("skills.sh", "www.skills.sh") and len(parts) >= 2:
            owner, repo = parts[0], parts[1]
            skill = skill or (parts[2] if len(parts) >= 3 else None)
        elif host in ("github.com", "www.github.com") and len(parts) >= 2:
            owner, repo = parts[0], parts[1].removesuffix(".git")
            # …/tree/<ref>/<path to the skill folder>
            if len(parts) >= 4 and parts[2] in ("tree", "blob"):
                tail = [part for part in parts[4:] if part != "SKILL.md"]
                skill = skill or (tail[-1] if tail else None)
        else:
            raise EcosystemError("source_unreadable")
    else:
        pieces = first.split("/")
        if len(pieces) != 2:
            raise EcosystemError("source_unreadable")
        owner, repo = pieces
        if len(words) > 1:
            skill = skill or words[1]
    for part in (owner, repo, *([skill] if skill else [])):
        if not NAME.match(part):
            raise EcosystemError("source_unreadable")
    return owner, repo, skill


async def fetch(
    owner: str,
    repo: str,
    skill: str | None,
    allow_list: list[str],
    commit: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> Found:
    """Read one skill from GitHub, pinned to a commit, with what the office must know."""
    http = client or httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, follow_redirects=False, headers={"User-Agent": USER_AGENT}
    )
    try:
        api = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
        if commit is None:
            head = await _json(http, f"{api}/commits/HEAD", allow_list)
            commit = str(head.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
            raise EcosystemError("commit_unreadable")
        tree = await _json(http, f"{api}/git/trees/{commit}?recursive=1", allow_list)
        paths = [
            str(entry.get("path"))
            for entry in tree.get("tree") or []
            if isinstance(entry, dict) and entry.get("type") == "blob"
        ]
        path = _locate(paths, skill)
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        raw = f"https://raw.githubusercontent.com/{quote(owner)}/{quote(repo)}/{commit}/"
        text = await _text(http, raw + path, allow_list)
        front, body = _front_matter(text)
        name = str(front.get("name") or (folder.rsplit("/", 1)[-1] if folder else repo)).strip()
        description = " ".join(str(front.get("description") or "").split())
        inside = [p for p in paths if (p.startswith(folder + "/") if folder else "/" not in p)]
        refused = sorted(
            p[len(folder) + 1 :] if folder else p for p in inside if _extension(p) in EXECUTABLE
        )
        references = sorted(
            p
            for p in inside
            if p != path
            and p.lower().endswith(".md")
            and not NOT_REFERENCE.match(p.rsplit("/", 1)[-1])
        )
        instructions, omitted = body.strip(), []
        if len(instructions) > MAX_INSTRUCTIONS:
            instructions = instructions[:MAX_INSTRUCTIONS]
            omitted.append("SKILL.md (its end)")
        for reference in references:
            label = reference[len(folder) + 1 :] if folder else reference
            content = (await _text(http, raw + reference, allow_list)).strip()
            section = f"\n\n## Reference: {label}\n\n{content}"
            if len(instructions) + len(section) > MAX_INSTRUCTIONS:
                omitted.append(label)
                continue
            instructions += section
        whole = f"{description}\n{body}"
        found = Found(
            owner=owner,
            repo=repo,
            path=path,
            commit=commit,
            name=name,
            description=description,
            instructions=instructions,
            refused=refused,
            omitted=omitted,
            assumptions=[
                {"kind": kind, "text": said}
                for kind, pattern, said in ASSUMES
                if pattern.search(whole)
            ],
            permissions=[
                permission
                for permission, pattern in (
                    ("web_search", NEEDS_WEB),
                    ("local_documents", NEEDS_DOCUMENTS),
                )
                if pattern.search(whole)
            ],
            suggested_role=_role(name) or _role(description) or "summarist",
        )
        found.audits = await _audits(http, owner, repo, found.folder_name(), allow_list)
        return found
    finally:
        if client is None:
            await http.aclose()


async def latest_change(
    owner: str,
    repo: str,
    folder: str,
    installed: str,
    allow_list: list[str],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """The newest commit that touched a skill's folder, and whether it is newer
    than the one installed.

    The installed commit is the repository's head when the owner installed it,
    and the folder's newest commit is usually older than that: older is not an
    update, and neither is a change elsewhere in the repository.
    """
    http = client or httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, follow_redirects=False, headers={"User-Agent": USER_AGENT}
    )
    try:
        api = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
        response = await _get(
            http, f"{api}/commits?path={quote(folder)}&per_page=1", allow_list, MAX_LISTING_BYTES
        )
        try:
            listing = response.json()
        except ValueError:
            raise EcosystemError("source_unreadable") from None
        if not isinstance(listing, list) or not listing or not isinstance(listing[0], dict):
            raise EcosystemError("not_found")
        newest = listing[0]
        sha = str(newest.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise EcosystemError("commit_unreadable")
        raw_commit = newest.get("commit")
        said: dict[str, Any] = raw_commit if isinstance(raw_commit, dict) else {}
        message = str(said.get("message") or "").strip().split("\n", 1)[0][:200]
        raw_committer = said.get("committer")
        committer: dict[str, Any] = raw_committer if isinstance(raw_committer, dict) else {}
        newer = False
        if sha != installed:
            compared = await _json(http, f"{api}/compare/{installed}...{sha}", allow_list)
            newer = compared.get("status") == "ahead"
        return {
            "commit": sha,
            "date": str(committer.get("date") or "") or None,
            "message": message,
            "newer": newer,
        }
    finally:
        if client is None:
            await http.aclose()


def manifest(found: Found, role: str, version: str) -> dict[str, Any]:
    """The office's manifest for a skill read from outside, with where it came from."""
    return {
        "skill_id": _slug(found.name),
        "name": found.name[:120],
        "version": version,
        "publisher": found.owner[:120],
        "description": (found.description or f"{found.name} from {found.owner}/{found.repo}")[
            :1000
        ],
        "roles": [role],
        "permissions": found.permissions,
        "instructions": found.instructions,
        "changelog": f"Read from {found.owner}/{found.repo} at {found.commit[:12]}.",
        "review": {
            "status": "reviewed",
            "checks": ["read_by_owner", "instructions_only", f"pinned:{found.commit}"],
        },
        "source": {
            "kind": "github",
            "repo": f"{found.owner}/{found.repo}",
            "path": found.path,
            "commit": found.commit,
            "skills_sh": found.view()["skills_sh"],
        },
        "refused": found.refused,
        "omitted": found.omitted,
        "assumptions": found.assumptions,
        "audits": found.audits,
    }


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:63]
    return slug if len(slug) >= 2 else f"skill-{slug or 'x'}"


def _role(text: str) -> str:
    """A role from what a skill is called or says it does; empty if it says neither."""
    for role, pattern in ROLE_WORDS:
        if pattern.search(text):
            return role
    return "summarist" if WRITING.search(text) else ""


def _extension(path: str) -> str:
    leaf = path.rsplit("/", 1)[-1]
    return "." + leaf.rsplit(".", 1)[-1].lower() if "." in leaf else ""


def _locate(paths: list[str], skill: str | None) -> str:
    skills = sorted(p for p in paths if p == "SKILL.md" or p.endswith("/SKILL.md"))
    if not skills:
        raise EcosystemError("no_skill_here")
    folders = {(p.rsplit("/", 2)[-2] if "/" in p else ""): p for p in skills}
    if skill:
        for folder, path in folders.items():
            if folder.lower() == skill.lower():
                return path
        raise EcosystemError("skill_not_found", sorted(f for f in folders if f))
    if len(skills) == 1:
        return skills[0]
    raise EcosystemError("choose_skill", sorted(f for f in folders if f))


def _front_matter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONT.match(text.lstrip("﻿"))
    if not match:
        return {}, text
    try:
        front = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        front = {}
    return (front if isinstance(front, dict) else {}), match.group(2)


async def _get(
    http: httpx.AsyncClient, url: str, allow_list: list[str], limit: int = MAX_FILE_BYTES
) -> httpx.Response:
    try:
        checked = check_address(url, allow_list)
    except McpError as refused:
        raise EcosystemError(refused.code) from None
    try:
        response = await http.get(checked, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError:
        raise EcosystemError("source_unreachable") from None
    if response.status_code == 404:
        raise EcosystemError("not_found")
    if response.status_code in (403, 429):
        raise EcosystemError("github_rate_limited")
    if response.status_code != 200:
        raise EcosystemError("source_unreachable")
    if len(response.content) > limit:
        raise EcosystemError("file_too_large")
    return response


async def _json(http: httpx.AsyncClient, url: str, allow_list: list[str]) -> dict[str, Any]:
    response = await _get(http, url, allow_list, MAX_LISTING_BYTES)
    try:
        body = response.json()
    except ValueError:
        raise EcosystemError("source_unreadable") from None
    return body if isinstance(body, dict) else {}


async def _text(http: httpx.AsyncClient, url: str, allow_list: list[str]) -> str:
    return (await _get(http, url, allow_list)).text


async def _audits(
    http: httpx.AsyncClient, owner: str, repo: str, skill: str, allow_list: list[str]
) -> dict[str, str]:
    """skills.sh's published audit results for this skill, when it lists them.

    Read from the skill's public page: information for the owner, never a
    decision. Anything unreadable is simply not shown.
    """
    try:
        page = await _text(http, f"https://www.skills.sh/{owner}/{repo}/{skill}", allow_list)
    except EcosystemError:
        return {}
    plain = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())
    match = AUDIT.search(plain)
    if not match:
        return {}
    named = dict(zip(("gen", "socket", "snyk"), match.groups(), strict=True))
    return {key: value for key, value in named.items() if value}

"""Whether a skill from GitHub has been improved since it was installed.

A skill is pinned to the commit the owner reviewed, so it never changes under
a task. This only says when its author has changed its folder since: the owner
previews the change and installs it through the same reviewed path, or not.
"""

from __future__ import annotations

import difflib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import InstalledSkill, SkillUpdate
from app.skills import ecosystem

# How much of a diff an update preview shows, in changed lines.
MAX_CHANGES = 400


def _version(row: InstalledSkill) -> tuple[int, ...]:
    return tuple(int(part) for part in row.version.split(".") if part.isdigit())


def newest_from_github(session: Session) -> dict[str, InstalledSkill]:
    """The newest installed version of each skill that came from GitHub."""
    newest: dict[str, InstalledSkill] = {}
    for row in session.scalars(
        select(InstalledSkill).where(InstalledSkill.status.in_(("ENABLED", "NEEDS_REVIEW")))
    ):
        source = (row.manifest_json or {}).get("source") or {}
        if source.get("kind") != "github" or not source.get("commit"):
            continue
        held = newest.get(row.skill_id)
        if held is None or _version(row) > _version(held):
            newest[row.skill_id] = row
    return newest


def folder_of(path: str) -> str:
    """The skill's folder in its repository, or its file when it sits at the top."""
    return path.rsplit("/", 1)[0] if "/" in path else path


def source_text(repo: str, path: str) -> str:
    """How the install path names this skill: repository, then its folder's name."""
    folder = folder_of(path)
    name = folder.rsplit("/", 1)[-1] if "/" in path else ""
    return f"{repo} {name}".strip()


def view(row: SkillUpdate) -> dict[str, Any]:
    return {
        "skill_id": row.skill_id,
        "repo": row.repo,
        "path": row.path,
        "installed_commit": row.installed_commit,
        "latest_commit": row.latest_commit,
        "latest_date": row.latest_date.isoformat() if row.latest_date else None,
        "message": row.message,
        "state": row.state,
        "reason": row.reason,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
        "source_text": source_text(row.repo, row.path),
    }


def listing(session: Session) -> list[dict[str, Any]]:
    return [
        view(row) for row in session.scalars(select(SkillUpdate).order_by(SkillUpdate.skill_id))
    ]


def _when(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


async def check(
    session_factory: sessionmaker[Session], allow_list: list[str]
) -> list[dict[str, Any]]:
    """Ask GitHub about every installed skill from it; keep what it said."""
    with session_factory() as session:
        skills = {
            skill_id: dict((row.manifest_json or {}).get("source") or {})
            for skill_id, row in newest_from_github(session).items()
        }
    for skill_id, source in skills.items():
        repo = str(source.get("repo") or "")
        path = str(source.get("path") or "")
        installed = str(source.get("commit") or "")
        owner, _, name = repo.partition("/")
        answer: dict[str, Any] | None = None
        reason = None
        try:
            answer = await ecosystem.latest_change(
                owner, name, folder_of(path), installed, allow_list
            )
        except ecosystem.EcosystemError as failed:
            reason = failed.code
        with session_factory() as session, session.begin():
            row = session.scalar(select(SkillUpdate).where(SkillUpdate.skill_id == skill_id))
            if row is None:
                row = SkillUpdate(skill_id=skill_id, repo=repo, path=path, message="")
                session.add(row)
            row.repo, row.path, row.installed_commit = repo, path, installed
            row.checked_at = datetime.now(UTC)
            if answer is None:
                # The last answer stands; it says it could not check.
                row.state, row.reason = "unreachable", reason
                continue
            row.reason = None
            row.latest_commit = answer["commit"]
            row.latest_date = _when(answer.get("date"))
            row.message = str(answer.get("message") or "")
            row.state = "available" if answer.get("newer") else "current"
    with session_factory() as session:
        return listing(session)


def mark_installed(session: Session, skill_id: str, commit: str) -> None:
    """An update installed is no longer an update."""
    row = session.scalar(select(SkillUpdate).where(SkillUpdate.skill_id == skill_id))
    if row is None:
        return
    row.installed_commit = commit
    if row.latest_commit in (None, commit) or row.state == "available":
        row.state = "current"


def changes(before: str, after: str) -> list[dict[str, str]]:
    """What an update changes in the instructions, line by line."""
    found: list[dict[str, str]] = []
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        if line.startswith(("+ ", "- ")) and line[2:].strip():
            found.append({"op": line[0], "text": line[2:]})
        if len(found) >= MAX_CHANGES:
            break
    return found

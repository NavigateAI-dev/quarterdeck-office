"""Sort my ideas: one model call, on the owner's press, that
suggests; nothing changes until the owner accepts a suggestion.

The lead reads the open ideas (numbered), the active projects' names, and the
coming week of the owner's calendar only when agents may read it. It
answers with suggestions: merge ideas that say the same, reword one more
clearly, plan one for a day (or someday), or place one in a project. Each is
checked here before the owner sees it: ideas that exist and are open, a day not
in the past and within reach, a project that exists. The call's cost is kept
with the suggestions and counts against the month like every other call.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import ideas, widgets
from app.budget import estimate_prompt_tokens
from app.calendars.store import agent_view
from app.db.models import Idea, IdeaSort, Project, TopicItem

# At most this many ideas are read in one sort, newest unplanned first.
READ = 60
IDEA_CHARS = 300
SUGGESTIONS = 12
# A day the lead may suggest: from today, within this reach.
REACH = timedelta(days=30)
KINDS = ("merge", "reword", "plan", "project")
# What the estimate reads: the cost per token of recent single calls (briefs).
RECENT = 20

ASK = """You help the owner of a private office sort the ideas they jotted
down for later. Suggest only what clearly helps them plan; it is fine to
suggest little. The owner accepts or ignores each suggestion.
Kinds of suggestion:
- merge: two or more ideas that say the same thing; "text" is one idea for them.
- reword: an idea that is unclear or long; "text" is a short, clear wording in
  the owner's own language.
- plan: a day to take an idea up, "day" as YYYY-MM-DD from today on (within 30
  days), or "someday". Prefer days the calendar shows as lighter.
- project: an idea that belongs to one of the listed projects; "project" is its
  exact name.
Refer to ideas by their number. At most 12 suggestions, each with a short
"why" for the owner.
Return JSON only:
{"suggestions": [{"kind": "...", "ideas": [1], "text": "...", "day": "...",
"project": "...", "why": "..."}]}"""


class SortRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _open_ideas(session: Session) -> list[Idea]:
    """Unplanned first (newest first), then planned, then someday."""
    rows = list(session.scalars(select(Idea).where(Idea.status.in_(ideas.OPEN))))
    rows.sort(
        key=lambda i: (
            i.status == "someday",
            i.plan_for is not None,
            -(i.created_at.timestamp() if i.created_at else 0),
        )
    )
    return rows[:READ]


def _projects(session: Session) -> list[Project]:
    return list(
        session.scalars(select(Project).where(Project.status == "ACTIVE").order_by(Project.name))
    )


def prompt(session: Session, now: datetime) -> tuple[list[dict[str, str]], list[UUID]]:
    """The messages for the call, and which idea each number stands for."""
    today = ideas.owner_today(session, now)
    listed = _open_ideas(session)
    lines = []
    for n, idea in enumerate(listed, start=1):
        planned = (
            "someday"
            if idea.status == "someday"
            else f"planned {idea.plan_for:%a %Y-%m-%d}"
            if idea.plan_for
            else "not planned"
        )
        lines.append(f"{n}. {' '.join(idea.text.split())[:IDEA_CHARS]} ({planned})")
    projects = [p.name for p in _projects(session)]
    calendar = agent_view(session, now, days=7) if widgets.reads(session, "calendar") else ""
    told = [
        f"Today is {today:%A %Y-%m-%d}.",
        "Ideas:\n" + "\n".join(lines),
        "Projects: " + ("; ".join(projects) if projects else "none"),
    ]
    if calendar:
        told.append("The owner's coming week:\n" + calendar)
    messages = [
        {"role": "system", "content": ASK},
        {"role": "user", "content": "\n\n".join(told)},
    ]
    return messages, [idea.id for idea in listed]


def estimate(session: Session, now: datetime, output_tokens: int) -> dict[str, Any]:
    """What a sort may cost, from what recent single calls cost per token.
    None when the office has no such calls yet to go by."""
    count = int(session.scalar(select(func.count(Idea.id)).where(Idea.status.in_(ideas.OPEN))) or 0)
    messages, _ = prompt(session, now)
    tokens = estimate_prompt_tokens(messages) + output_tokens
    recent = session.execute(
        select(TopicItem.cost_usd, TopicItem.tokens)
        .where(TopicItem.cost_usd.is_not(None), TopicItem.tokens > 0)
        .order_by(TopicItem.created_at.desc())
        .limit(RECENT)
    ).all()
    spent = sum(float(cost or 0) for cost, _ in recent)
    used = sum(int(n or 0) for _, n in recent)
    per_token = spent / used if used else None
    return {
        "ideas": count,
        "tokens": tokens,
        "estimate_usd": round(per_token * tokens, 4) if per_token else None,
    }


def _parsed(text: str) -> list[dict[str, Any]]:
    found = re.search(r"\{.*\}", text, re.DOTALL)
    if not found:
        return []
    try:
        data = json.loads(found.group(0))
    except ValueError:
        return []
    items = data.get("suggestions") if isinstance(data, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def checked(
    session: Session, raw: list[dict[str, Any]], numbered: list[UUID], today: date
) -> list[dict[str, Any]]:
    """Only suggestions that can be applied as said; each idea suggested once per kind."""
    projects = {p.name.casefold(): p for p in _projects(session)}
    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, UUID]] = set()
    for item in raw:
        kind = str(item.get("kind") or "")
        if kind not in KINDS:
            continue
        given = item.get("ideas")
        numbers: list[Any] = given if isinstance(given, list) else []
        targets: list[UUID] = []
        for number in numbers:
            if isinstance(number, int) and 1 <= number <= len(numbered):
                if numbered[number - 1] not in targets:
                    targets.append(numbered[number - 1])
        if not targets or (kind == "merge") != (len(targets) > 1):
            continue
        if any((kind, t) in seen for t in targets):
            continue
        shown: dict[str, Any] = {
            "kind": kind,
            "ideas": [str(t) for t in targets],
            "why": " ".join(str(item.get("why") or "").split())[:240],
            "status": "open",
        }
        if kind in ("merge", "reword"):
            text = " ".join(str(item.get("text") or "").split())[: ideas.MAX_CHARS]
            if not text:
                continue
            shown["text"] = text
        elif kind == "plan":
            day = str(item.get("day") or "").strip().lower()
            if day == "someday":
                shown["someday"] = True
            else:
                try:
                    planned = date.fromisoformat(day)
                except ValueError:
                    continue
                if not today <= planned <= today + REACH:
                    continue
                shown["day"] = planned.isoformat()
        else:
            project = projects.get(str(item.get("project") or "").strip().casefold())
            if project is None:
                continue
            shown["project"] = {"id": str(project.id), "name": project.name}
        seen.update((kind, t) for t in targets)
        shown["n"] = len(kept)
        kept.append(shown)
        if len(kept) >= SUGGESTIONS:
            break
    return kept


async def run(
    session_factory: Any,
    now: datetime,
    agent: dict[str, Any],
    complete: Callable[[dict[str, Any], list[dict[str, str]]], Awaitable[Any]],
) -> IdeaSort:
    """The one call, its suggestions checked and kept with what it cost."""
    with session_factory() as session:
        messages, numbered = prompt(session, now)
        today = ideas.owner_today(session, now)
    if not numbered:
        raise SortRejected("no_ideas")
    completion = await complete(agent, messages)
    usage = completion.usage
    with session_factory() as session, session.begin():
        suggestions = checked(session, _parsed(str(completion.content or "")), numbered, today)
        row = IdeaSort(
            ideas_count=len(numbered),
            suggestions_json=suggestions,
            cost_usd=completion.estimated_cost,
            tokens=int((usage.prompt_tokens or 0) + (usage.completion_tokens or 0)),
        )
        session.add(row)
        session.flush()
        session.expunge(row)
    return row


def view(session: Session, row: IdeaSort | None) -> dict[str, Any] | None:
    """A sort's suggestions, each with the words of its ideas; a suggestion
    whose ideas are no longer all open is shown as passed."""
    if row is None:
        return None
    wanted = {UUID(i) for s in row.suggestions_json or [] for i in s.get("ideas", [])}
    found = (
        {idea.id: idea for idea in session.scalars(select(Idea).where(Idea.id.in_(wanted)))}
        if wanted
        else {}
    )
    shown = []
    for suggestion in row.suggestions_json or []:
        rows = [found.get(UUID(i)) for i in suggestion.get("ideas", [])]
        alive = all(r is not None and r.status in ideas.OPEN for r in rows)
        status = suggestion.get("status", "open")
        shown.append(
            {
                **suggestion,
                "status": status if status != "open" or alive else "passed",
                "texts": [r.text if r is not None else "" for r in rows],
            }
        )
    return {
        "id": str(row.id),
        "at": row.created_at.isoformat() if row.created_at else None,
        "ideas": row.ideas_count,
        "cost_usd": row.cost_usd,
        "suggestions": shown,
        "open": sum(1 for s in shown if s["status"] == "open"),
    }


def latest(session: Session) -> IdeaSort | None:
    return session.scalar(select(IdeaSort).order_by(IdeaSort.created_at.desc()).limit(1))


def answer(session: Session, row: IdeaSort, n: int, accept: bool, now: datetime) -> None:
    """Accept (apply it now) or ignore one suggestion; each is answered once."""
    suggestions = [dict(s) for s in row.suggestions_json or []]
    if not 0 <= n < len(suggestions):
        raise SortRejected("no_such_suggestion")
    suggestion = suggestions[n]
    if suggestion.get("status", "open") != "open":
        raise SortRejected("already_answered")
    if accept:
        targets = [session.get(Idea, UUID(i)) for i in suggestion["ideas"]]
        if any(t is None or t.status not in ideas.OPEN for t in targets):
            raise SortRejected("ideas_changed")
        kept = [t for t in targets if t is not None]
        kind = suggestion["kind"]
        if kind == "merge":
            first, rest = kept[0], kept[1:]
            first.text = suggestion["text"]
            for other in rest:
                # The first keeps its day and project unless it had none.
                first.plan_for = first.plan_for or other.plan_for
                first.project_id = first.project_id or other.project_id
                other.status = "merged"
        elif kind == "reword":
            kept[0].text = suggestion["text"]
        elif kind == "plan":
            day = date.fromisoformat(suggestion["day"]) if suggestion.get("day") else None
            try:
                ideas.plan(session, kept[0], day, bool(suggestion.get("someday")), now)
            except ideas.IdeaRejected as rejected:
                raise SortRejected(rejected.code) from None
        else:
            project = session.get(Project, UUID(suggestion["project"]["id"]))
            if project is None:
                raise SortRejected("project_gone")
            ideas.set_project(kept[0], project)
    suggestion["status"] = "accepted" if accept else "ignored"
    suggestions[n] = suggestion
    # A new list, so the JSON column is seen to have changed.
    row.suggestions_json = suggestions

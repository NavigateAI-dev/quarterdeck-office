"""Revising one passage of delivered work, beside the discussion.

The owner reads the delivered document next to the discussion, selects a
passage and says what is wrong with it. The discussion's lead revises that
passage, and only that, into a new version of the answer, with a sentence on
what changed; the earlier version stays. When doing it properly needs evidence
the work does not have, nothing is invented: the lead says what is missing and
the discussion offers to rework the task with research, the existing path.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Artifact, RoomMessage, Task, Topic, TopicItem
from app.documents import Document, Section, document_for, to_markdown
from app.documents.edited import KEY as EDITED_KEY
from app.documents.edited import as_dict
from app.documents.edited import from_dict as edited_document
from app.office_settings import MonthlyLimitReached, check_monthly_limit, paused
from app.topic_briefs import clip, lead
from app.topics import touch

logger = logging.getLogger(__name__)

# The answer a person reads first, and the stage that writes it.
ANSWER_STAGES = (
    ("synthesis", "synthesis"),
    ("strategy_proposal", "strategy"),
    ("final_report", "final_report"),
)
NOTE_CHARS = 300

ASK = """You revise one passage of a finished document, at its owner's request.
The document is JSON. Change only what the owner's comment asks about the
quoted passage, and keep everything else exactly as it is. Do not invent facts,
figures or sources: if revising it properly needs evidence the document does
not already contain, do not change it; say what is missing instead.
Return JSON only:
{"revised": <the whole document JSON, revised> or null,
 "note": "<one sentence: what changed, or what is missing>",
 "needs_research": <true only if new evidence is needed>}"""


def answer_of(session: Session, run_id: UUID) -> tuple[str, str, list[Artifact]] | None:
    """The run's answer: its type, the stage that writes it, and every version,
    oldest first."""
    for kind, stage in ANSWER_STAGES:
        versions = list(
            session.scalars(
                select(Artifact)
                .where(Artifact.run_id == run_id, Artifact.type == kind)
                .order_by(Artifact.version)
            )
        )
        if versions:
            return kind, stage, versions
    return None


def notes_of(session: Session, run_id: UUID) -> dict[int, str]:
    """What changed in each revised version, by version."""
    notes: dict[int, str] = {}
    for item in session.scalars(select(TopicItem).where(TopicItem.kind == "revision")):
        try:
            data = json.loads(item.text)
        except ValueError:
            continue
        if data.get("run_id") == str(run_id) and data.get("version"):
            notes[int(data["version"])] = str(data.get("note") or "")
    return notes


def answer_view(session: Session, run_id: UUID) -> dict[str, Any] | None:
    found = answer_of(session, run_id)
    if found is None:
        return None
    kind, stage, versions = found
    notes = notes_of(session, run_id)
    shown = [
        {
            "artifact_id": str(row.id),
            "version": row.version,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "note": notes.get(row.version, ""),
            "by": row.created_by,
        }
        for row in versions
    ]
    return {
        "run_id": str(run_id),
        "type": kind,
        "stage": stage,
        "current": shown[-1],
        "versions": shown,
    }


class StaleEdit(Exception):
    """The answer changed since the editor loaded it."""


def current_answer(
    session_factory: sessionmaker[Session], repository: Any, run_id: UUID
) -> tuple[str, Artifact, dict[str, Any], Document] | None:
    """The newest answer: its stage, row, stored payload and the document shown."""
    with session_factory() as session:
        found = answer_of(session, run_id)
        if found is None:
            return None
        kind, stage, versions = found
        current = versions[-1]
        task = session.get(Task, current.task_id) if current.task_id else None
        objective = task.objective if task is not None else None
    try:
        payload = json.loads(repository.store.read(current.project_id, current.id, current.version))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        payload = {"text": str(payload)}
    title = (objective or "").strip().splitlines()[0][:120] if objective else None
    return stage, current, payload, document_for(kind, payload, title)


def save_edit(
    session_factory: sessionmaker[Session],
    repository: Any,
    run_id: UUID,
    base_version: int,
    edited: dict[str, Any],
) -> dict[str, Any] | None:
    """The owner's edit of the answer, as its next version.

    The editor holds the answer's own sections; the working beneath them is
    kept as it was. The agent's payload stays beside the edit, so anything that
    reads the fields still finds them. Raises StaleEdit when a newer version
    was written since the editor loaded `base_version`, and ValueError for a
    document that is not one.
    """
    found = current_answer(session_factory, repository, run_id)
    if found is None:
        return None
    stage, current, payload, shown = found
    if current.version != base_version:
        raise StaleEdit(current.version)
    answer = edited_document(edited)
    document = Document(
        title=answer.title,
        subtitle=shown.subtitle,
        sections=[Section(row.heading, row.blocks, notes=row.notes) for row in answer.sections]
        + shown.working(),
    )
    dispatch = repository.describe([run_id])[0][0]
    repository.record_stage(
        dispatch, stage, {**payload, EDITED_KEY: as_dict(document)}, created_by="owner"
    )
    with session_factory() as session:
        return answer_view(session, run_id)


REWRITE = """You rewrite one passage of a finished document, at its owner's request.
Change the passage only as the owner asks. Keep its facts; do not add facts,
figures or sources the document does not already contain. Use **bold** and
*italic* only where the passage already does or the owner asks for it.
Return JSON only:
{"text": "<the passage, rewritten>" or null if it cannot be done honestly,
 "note": "<one sentence: what changed, or what is missing>"}"""


class RewriteRefused(Exception):
    """The office will not make the call. Carries a short reason."""


async def rewrite(
    session_factory: sessionmaker[Session],
    repository: Any,
    settings: Any,
    run_id: UUID,
    topic_id: UUID,
    passage: str,
    instruction: str,
    complete: Callable[[dict[str, Any], list[dict[str, str]]], Awaitable[Any]],
) -> dict[str, Any]:
    """One passage of the answer, rewritten by the lead as a suggestion.

    Nothing is saved to the answer: the owner accepts or rejects the
    suggestion in the editor. The call is paid, so it is made only on the
    owner's press, under the same switch and monthly limit as a reply, and it
    is recorded in the task's room with its cost.
    """
    with session_factory() as session:
        if paused(session, settings, "chat"):
            raise RewriteRefused("chat_paused")
        try:
            check_monthly_limit(session, settings)
        except MonthlyLimitReached:
            raise RewriteRefused("monthly_limit") from None
        agent = lead(session)
    if agent is None:
        raise RewriteRefused("no_agent")
    found = current_answer(session_factory, repository, run_id)
    if found is None:
        raise RewriteRefused("no_answer")
    document = found[3]
    context = to_markdown(Document(title=document.title, sections=document.answer()))
    messages = [
        {"role": "system", "content": REWRITE},
        {
            "role": "user",
            "content": (
                f"The document:\n{context[:12000]}\n\n"
                f"The passage:\n{passage}\n\nThe owner asks:\n{instruction}"
            ),
        },
    ]
    completion = await complete(agent, messages)
    answer = _parsed(str(completion.content or "")) or {}
    text = answer.get("text")
    text = text.strip() if isinstance(text, str) and text.strip() else None
    note = clip(str(answer.get("note") or ""), NOTE_CHARS)
    with session_factory() as session, session.begin():
        topic = session.get(Topic, topic_id)
        if topic is not None:
            said = (
                f"Suggested a rewrite of a passage of the answer: {note or 'see the editor.'}"
                if text
                else note or "That passage could not be rewritten as asked."
            )
            session.add(
                RoomMessage(
                    project_id=topic.project_id,
                    topic_id=topic.id,
                    run_id=run_id,
                    author_kind="agent",
                    author_role=agent.get("role"),
                    author_name=str(agent.get("name") or "The office"),
                    body=said,
                    cost_usd=completion.estimated_cost,
                    tokens=int(
                        (completion.usage.prompt_tokens or 0)
                        + (completion.usage.completion_tokens or 0)
                    ),
                )
            )
            touch(session, topic.id)
    return {"text": text, "note": note, "cost_usd": completion.estimated_cost}


def _parsed(text: str) -> dict[str, Any] | None:
    found = re.search(r"\{.*\}", text, re.DOTALL)
    if not found:
        return None
    try:
        data = json.loads(found.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _say(
    session: Session,
    topic: Topic,
    text: str,
    author: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
    cost: float | None = None,
) -> None:
    session.add(
        RoomMessage(
            project_id=topic.project_id,
            topic_id=topic.id,
            author_kind="agent" if author else "office",
            author_role=(author or {}).get("role"),
            author_name=str((author or {}).get("name") or "The office"),
            author_profile_id=UUID(str(author["profile_id"]))
            if author and author.get("profile_id")
            else None,
            body=text,
            actions_json=actions or [],
            cost_usd=cost,
        )
    )
    touch(session, topic.id)


async def revise(
    session_factory: sessionmaker[Session],
    repository: Any,
    topic_id: UUID,
    run_id: UUID,
    quote: str,
    comment: str,
    complete: Callable[[dict[str, Any], list[dict[str, str]]], Awaitable[Any]],
) -> int | None:
    """Revise the quoted passage; returns the new version, or None."""
    with session_factory() as session:
        topic = session.get(Topic, topic_id)
        found = answer_of(session, run_id)
        agent = lead(session)
        if topic is None or found is None or agent is None:
            return None
        kind, stage, versions = found
        current = versions[-1]
        try:
            payload = json.loads(
                repository.store.read(current.project_id, current.id, current.version)
            )
        except (OSError, ValueError):
            return None
    messages = [
        {"role": "system", "content": ASK},
        {
            "role": "user",
            "content": (
                f"The document (JSON):\n{json.dumps(payload, ensure_ascii=False)[:24000]}\n\n"
                f"The quoted passage:\n{quote}\n\nThe owner's comment:\n{comment}"
            ),
        },
    ]
    completion = await complete(agent, messages)
    answer = _parsed(str(completion.content or "")) or {}
    note = clip(str(answer.get("note") or ""), NOTE_CHARS)
    revised = answer.get("revised")
    usable = (
        isinstance(revised, dict)
        and not answer.get("needs_research")
        and set(payload).issubset(set(revised))
        and revised != payload
    )
    version = None
    if usable:
        dispatch = repository.describe([run_id])[0][0]
        repository.record_stage(dispatch, stage, revised)
        version = current.version + 1
    with session_factory() as session, session.begin():
        topic = session.get(Topic, topic_id)
        if topic is None:
            return version
        session.add(
            TopicItem(
                topic_id=topic_id,
                kind="revision",
                text=json.dumps(
                    {
                        "run_id": str(run_id),
                        "version": version,
                        "note": note,
                        "quote": clip(quote, 400),
                        "comment": clip(comment, 400),
                    },
                    ensure_ascii=False,
                ),
                status="kept",
                cost_usd=completion.estimated_cost,
                tokens=int(
                    (completion.usage.prompt_tokens or 0)
                    + (completion.usage.completion_tokens or 0)
                ),
            )
        )
        if version is not None:
            said = note or "the passage is revised."
            _say(session, topic, f"Version {version}: {said} The earlier version is kept.")
        elif answer.get("needs_research"):
            _say(
                session,
                topic,
                note or "That needs evidence the work does not have yet.",
                author=agent,
                actions=[
                    {
                        "kind": "rework",
                        "run_id": str(run_id),
                        "comment": clip(f"In the passage “{quote}”: {comment}", 600),
                    }
                ],
            )
        else:
            _say(session, topic, "That passage could not be revised as asked; nothing was changed.")
    return version

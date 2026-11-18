"""Keeping imported conversations, and finding what was said.

These are records of what was said, by the owner and by other AIs. They are kept
apart from anything the office has decided or learned, and nothing here calls a
model.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    ArchiveConversation,
    ArchiveImport,
    ArchiveMemory,
    ArchiveMessage,
    RoomMessage,
    Topic,
)
from app.privacy.detect import LABELS, MASKED, counts
from app.sources.reader import ARCHIVE

logger = logging.getLogger("quarterdeck.archive")

PROVIDERS = ("chatgpt", "claude")
OFFICE = "office"
ROLES = ("user", "assistant", "tool")
# A file this big is refused while it arrives; the reader walks it piece by piece.
MAX_UPLOAD_BYTES = 4 * 1024**3
SEARCH_LIMIT = 20
EXCERPT_CHARS = 320
WINDOW_MESSAGES = 10
MESSAGE_CHARS = 2000
# An import still "running" after this was cut off by a restart.
STALE_AFTER = timedelta(hours=1)
PROVIDER_NAMES = {"chatgpt": "ChatGPT", "claude": "Claude", OFFICE: "This office"}


class ArchiveRejected(ValueError):
    """Not found or not allowed; the message says why."""


def _clean(value: Any, limit: int | None = None) -> str:
    """Text Postgres will take: no NUL, no lone surrogate half."""
    text = str(value or "").replace("\x00", "").encode("utf-8", "replace").decode("utf-8")
    return text[:limit] if limit else text


def _moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _digest(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()


def keep(session: Session, line: dict[str, Any], import_id: UUID) -> tuple[int, int]:
    """One conversation from the reader. Returns (new conversations, new messages).

    What the reader sends is checked as any other outside input. Importing the
    same thing again adds nothing, and one the owner deleted stays deleted.
    """
    provider = str(line.get("provider"))
    if provider not in PROVIDERS or not isinstance(line.get("messages"), list):
        return 0, 0
    title = _clean(line.get("title"))
    started = _moment(line.get("started_at"))
    external = _clean(line.get("external_id"), 255) or _digest(title, line.get("started_at"))
    row = session.scalar(
        select(ArchiveConversation).where(
            ArchiveConversation.provider == provider, ArchiveConversation.external_id == external
        )
    )
    added = 0
    if row is None:
        row = ArchiveConversation(
            provider=provider,
            external_id=external,
            title=title,
            started_at=started,
            updated_at=_moment(line.get("updated_at")),
            import_id=import_id,
            state="excluded",
            flags_json={},
        )
        session.add(row)
        session.flush()
        added = 1
    elif row.state == "deleted":
        return 0, 0
    else:
        row.title = title or row.title
        row.updated_at = _moment(line.get("updated_at")) or row.updated_at
    fresh = 0
    found = counts(title) if added else Counter[str]()
    for position, message in enumerate(line["messages"]):
        role = str(message.get("role")) if isinstance(message, dict) else ""
        if role not in ROLES:
            continue
        content = _clean(message.get("content"))
        if not content:
            continue
        identity = _clean(message.get("external_id"), 255) or _digest(
            role, message.get("sent_at"), content
        )
        made = session.scalar(
            insert(ArchiveMessage)
            .values(
                conversation_id=row.id,
                external_id=identity,
                position=position,
                role=role,
                content=content,
                sent_at=_moment(message.get("sent_at")),
            )
            .on_conflict_do_nothing(constraint="uq_archive_messages_external")
            .returning(ArchiveMessage.id)
        )
        if made is not None:
            fresh += 1
            found.update(counts(content))
    _flag(row, found)
    return added, fresh


def _flag(row: ArchiveConversation, found: Counter[str]) -> None:
    """Only what just arrived counts. Something sensitive hides it, even if opened before."""
    if not found:
        return
    row.flags_json = dict(Counter(row.flags_json) + found)
    if MASKED & set(found):
        row.sensitive = True
        row.state = "excluded"


async def run_import(
    session_factory: sessionmaker[Session],
    indexer: Any,
    exchange: Path,
    import_id: UUID,
    name: str,
) -> None:
    """Have the reader parse a stored upload, then keep what it wrote. Always cleans up."""
    upload = exchange / ARCHIVE / f"{name}.upload"
    lines = exchange / ARCHIVE / f"{name}.jsonl"
    new, messages, seen = 0, 0, 0
    error: str | None = None
    try:
        result = await indexer.parse_archive(name)
        error = result.get("error")
        if not error:
            with lines.open(encoding="utf-8") as stream:
                for text in stream:
                    with session_factory() as session, session.begin():
                        added, fresh = keep(session, json.loads(text), import_id)
                    new, messages, seen = new + added, messages + fresh, seen + 1
    except Exception as wrong:
        logger.exception('{"event":"archive_import_failed","import":"%s"}', import_id)
        error = "reader_unavailable" if type(wrong).__name__ == "SourceError" else "import_failed"
    finally:
        upload.unlink(missing_ok=True)
        lines.unlink(missing_ok=True)
    with session_factory() as session, session.begin():
        row = session.get(ArchiveImport, import_id)
        if row is not None:
            row.status = "failed" if error else "done"
            row.error = error
            row.conversations_new, row.messages_new = new, messages
            row.conversations_seen = seen
            row.finished_at = datetime.now(UTC)


def new_upload(session: Session, filename: str) -> tuple[UUID, str]:
    """An import record, and the name its file travels under."""
    row = ArchiveImport(filename=_clean(filename, 255) or "export")
    session.add(row)
    session.flush()
    return row.id, uuid4().hex


def _waiting(session: Session, import_id: UUID) -> tuple[int, int]:
    """Hidden conversations of an import: (ready to open together, to review one by one)."""
    ready, review = session.execute(
        select(
            func.count(case((ArchiveConversation.sensitive.is_(False), 1))),
            func.count(case((ArchiveConversation.sensitive.is_(True), 1))),
        ).where(ArchiveConversation.import_id == import_id, ArchiveConversation.state == "excluded")
    ).one()
    return int(ready), int(review)


def import_view(
    row: ArchiveImport, now: datetime, waiting: tuple[int, int] = (0, 0)
) -> dict[str, Any]:
    stale = row.status == "running" and now - row.created_at > STALE_AFTER
    return {
        "id": str(row.id),
        "filename": row.filename,
        "status": "failed" if stale else row.status,
        "error": "interrupted" if stale else row.error,
        "conversations_new": row.conversations_new,
        "messages_new": row.messages_new,
        "conversations_seen": row.conversations_seen,
        "hidden_ready": waiting[0],
        "hidden_review": waiting[1],
        "at": row.created_at.isoformat(),
    }


def overview(session: Session, now: datetime) -> dict[str, Any]:
    """What is kept, by tool, and the latest imports."""
    counts = session.execute(
        select(
            ArchiveConversation.provider,
            func.count(func.distinct(ArchiveConversation.id)),
            func.count(ArchiveMessage.id),
        )
        .outerjoin(ArchiveMessage, ArchiveMessage.conversation_id == ArchiveConversation.id)
        .where(ArchiveConversation.state != "deleted")
        .group_by(ArchiveConversation.provider)
    ).all()
    imports = session.scalars(
        select(ArchiveImport).order_by(ArchiveImport.created_at.desc()).limit(10)
    )
    return {
        "sources": [
            {
                "provider": provider,
                "name": PROVIDER_NAMES.get(provider, provider),
                "conversations": conversations,
                "messages": messages,
            }
            for provider, conversations, messages in counts
        ],
        "imports": [import_view(row, now, _waiting(session, row.id)) for row in imports],
    }


def open_import(session: Session, import_id: UUID) -> int:
    """Agents may read this import, except what holds something sensitive."""
    done = session.execute(
        update(ArchiveConversation)
        .where(
            ArchiveConversation.import_id == import_id,
            ArchiveConversation.state == "excluded",
            ArchiveConversation.sensitive.is_(False),
        )
        .values(state="active")
        .returning(ArchiveConversation.id)
    )
    return len(done.all())


def to_review(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    """Conversations held back for what is in them: what kind, never the values."""
    rows = session.scalars(
        select(ArchiveConversation)
        .where(ArchiveConversation.sensitive.is_(True), ArchiveConversation.state == "excluded")
        .order_by(ArchiveConversation.started_at.desc().nulls_last())
        .limit(limit)
    )
    return [
        {
            "conversation": f"archive:{row.id}",
            "title": row.title,
            "source_name": PROVIDER_NAMES.get(row.provider, row.provider),
            "found": [
                {"kind": LABELS.get(kind, kind), "count": count}
                for kind, count in row.flags_json.items()
                if kind in MASKED
            ],
        }
        for row in rows
    ]


def _excerpt(text: str, terms: list[str]) -> str:
    lowered = text.lower()
    at = min((p for p in (lowered.find(term.lower()) for term in terms) if p >= 0), default=0)
    start = max(0, at - EXCERPT_CHARS // 3)
    piece = text[start : start + EXCERPT_CHARS].strip()
    return ("…" if start else "") + piece + ("…" if start + EXCERPT_CHARS < len(text) else "")


def search(
    session: Session,
    query: str,
    *,
    source: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    project_id: UUID | None = None,
    include_excluded: bool = False,
    limit: int = SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    """Messages holding every word of the query, newest first.

    Substring, so Chinese without spaces is found. A project narrows it to the
    office's own discussions: imported conversations belong to no project yet.
    """
    terms = query.split()
    if not terms:
        return []
    hits: list[dict[str, Any]] = []
    if source != OFFICE and project_id is None:
        statement = (
            select(ArchiveMessage, ArchiveConversation)
            .join(ArchiveConversation, ArchiveMessage.conversation_id == ArchiveConversation.id)
            .where(*(ArchiveMessage.content.icontains(t, autoescape=True) for t in terms))
            .where(ArchiveConversation.state != "deleted")
            .order_by(ArchiveMessage.sent_at.desc().nulls_last(), ArchiveMessage.id.desc())
            .limit(limit)
        )
        if not include_excluded:
            statement = statement.where(ArchiveConversation.state == "active")
        if source:
            statement = statement.where(ArchiveConversation.provider == source)
        if since:
            statement = statement.where(ArchiveMessage.sent_at >= since)
        if until:
            statement = statement.where(ArchiveMessage.sent_at < until)
        for message, conversation in session.execute(statement):
            hits.append(
                {
                    "source": conversation.provider,
                    "source_name": PROVIDER_NAMES.get(conversation.provider, conversation.provider),
                    "conversation": f"archive:{conversation.id}",
                    "title": conversation.title,
                    "message_id": message.id,
                    "role": message.role,
                    "at": message.sent_at.isoformat() if message.sent_at else None,
                    "excerpt": _excerpt(message.content, terms),
                    "excluded": conversation.state == "excluded",
                }
            )
    if source in (None, OFFICE):
        office = (
            select(RoomMessage, Topic.title)
            .outerjoin(Topic, RoomMessage.topic_id == Topic.id)
            .where(*(RoomMessage.body.icontains(t, autoescape=True) for t in terms))
            .where(
                RoomMessage.author_kind.in_(("owner", "agent")), RoomMessage.topic_id.is_not(None)
            )
            .order_by(RoomMessage.created_at.desc())
            .limit(limit)
        )
        if since:
            office = office.where(RoomMessage.created_at >= since)
        if until:
            office = office.where(RoomMessage.created_at < until)
        if project_id:
            office = office.where(RoomMessage.project_id == project_id)
        for message, title in session.execute(office):
            hits.append(
                {
                    "source": OFFICE,
                    "source_name": PROVIDER_NAMES[OFFICE],
                    "conversation": f"office:{message.topic_id}",
                    "title": title or "",
                    "message_id": message.id,
                    "role": "user" if message.author_kind == "owner" else "assistant",
                    "at": message.created_at.isoformat(),
                    "excerpt": _excerpt(message.body, terms),
                    "excluded": False,
                }
            )
    hits.sort(key=lambda hit: hit["at"] or "", reverse=True)
    return hits[:limit]


def _window(rows: list[Any], ident: Any, around: int | None) -> list[Any]:
    if around is None:
        return rows[-WINDOW_MESSAGES * 2 :]
    place = next((n for n, row in enumerate(rows) if ident(row) == around), 0)
    return rows[max(0, place - WINDOW_MESSAGES) : place + WINDOW_MESSAGES + 1]


def conversation(
    session: Session, ref: str, around: int | None = None, for_agent: bool = False
) -> dict[str, Any]:
    """Messages of one conversation, around one message. Refs come from search."""
    kind, _, raw = ref.partition(":")
    try:
        identity = UUID(raw)
    except ValueError:
        raise ArchiveRejected("no such conversation") from None
    if kind == OFFICE:
        topic = session.get(Topic, identity)
        if topic is None:
            raise ArchiveRejected("no such conversation")
        rooms = list(
            session.scalars(
                select(RoomMessage).where(RoomMessage.topic_id == identity).order_by(RoomMessage.id)
            )
        )
        return {
            "conversation": ref,
            "source": OFFICE,
            "title": topic.title,
            "state": "active",
            "messages": [
                {
                    "id": row.id,
                    "role": "user" if row.author_kind == "owner" else "assistant",
                    "content": row.body[:MESSAGE_CHARS],
                    "at": row.created_at.isoformat(),
                }
                for row in _window(rooms, lambda m: m.id, around)
            ],
        }
    row = session.get(ArchiveConversation, identity) if kind == "archive" else None
    if row is None or row.state == "deleted" or (for_agent and row.state != "active"):
        raise ArchiveRejected("no such conversation")
    kept = list(
        session.scalars(
            select(ArchiveMessage)
            .where(ArchiveMessage.conversation_id == row.id)
            .order_by(ArchiveMessage.position, ArchiveMessage.id)
        )
    )
    return {
        "conversation": ref,
        "source": row.provider,
        "title": row.title,
        "state": row.state,
        "sensitive": row.sensitive,
        "messages": [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content[:MESSAGE_CHARS],
                "at": message.sent_at.isoformat() if message.sent_at else None,
            }
            for message in _window(kept, lambda m: m.id, around)
        ],
    }


def _archived(session: Session, conversation_id: UUID) -> ArchiveConversation:
    row = session.get(ArchiveConversation, conversation_id)
    if row is None or row.state == "deleted":
        raise ArchiveRejected("no such conversation")
    return row


def set_excluded(session: Session, conversation_id: UUID, excluded: bool) -> None:
    """An excluded conversation stays on the owner's page and reaches no agent."""
    _archived(session, conversation_id).state = "excluded" if excluded else "active"


def forget(session: Session, conversation_id: UUID) -> None:
    """Its words go for good. The bare row stays, so an old export cannot bring it back."""
    row = _archived(session, conversation_id)
    session.query(ArchiveMemory).filter(ArchiveMemory.conversation_id == row.id).delete()
    session.query(ArchiveMessage).filter(ArchiveMessage.conversation_id == row.id).delete()
    row.title = ""
    row.state = "deleted"
    row.sensitive = False
    row.flags_json = {}

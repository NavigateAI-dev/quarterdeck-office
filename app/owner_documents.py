"""A long chat reply, opened in the editor as a document of its own.

An agent's reply in a discussion can be a draft worth keeping: privacy terms,
an email, a plan. Opened in the editor it becomes the owner's document, in
the discussion's project (or Free exploration), versioned like a task's
answer: every save is a new version, the earlier ones are kept, and it
downloads as Word, PowerPoint, Markdown or a web page like any answer.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.artifacts import ArtifactMetadata, ArtifactStore
from app.db.models import Artifact, RoomMessage, Topic
from app.documents import Bullets, Document, Paragraph, Section
from app.documents.edited import KEY as EDITED_KEY
from app.documents.edited import as_dict
from app.documents.edited import from_dict as edited_document
from app.handoff import free_exploration

KIND = "document"
TITLE_CHARS = 120
BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")


class StaleDocument(Exception):
    """A newer version was saved since the editor loaded this one."""


def from_text(text: str, title: str) -> Document:
    """A reply's text as a document: headed sections, paragraphs and lists.

    A short line on its own with no closing punctuation and no colon reads as
    a heading ("Privacy Policy"); a Markdown heading does too. Lines starting
    with "-", "*" or "1." are lists. Everything else is a paragraph.
    """
    sections: list[Section] = []
    heading = ""
    blocks: list[Any] = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue
        marked = MARKDOWN_HEADING.match(lines[0])
        if marked and len(lines) == 1:
            if heading or blocks:
                sections.append(Section(heading, blocks))
            heading, blocks = marked.group(1).strip()[:300], []
            continue
        if marked:
            if heading or blocks:
                sections.append(Section(heading, blocks))
            heading, blocks = marked.group(1).strip()[:300], []
            lines = lines[1:]
        rest = lines[1:]
        listed = rest and (
            all(BULLET.match(line) for line in rest) or all(NUMBERED.match(line) for line in rest)
        )
        if listed and _heading_like(lines[0]):
            # A heading with its list straight under it, no blank line between.
            if heading or blocks:
                sections.append(Section(heading, blocks))
            heading, blocks = lines[0].strip(), []
            lines = rest
        if all(BULLET.match(line) for line in lines):
            blocks.append(Bullets(items=[BULLET.match(line).group(1).strip() for line in lines]))  # type: ignore[union-attr]
        elif all(NUMBERED.match(line) for line in lines):
            blocks.append(
                Bullets(
                    items=[NUMBERED.match(line).group(1).strip() for line in lines],  # type: ignore[union-attr]
                    ordered=True,
                )
            )
        elif len(lines) == 1 and _heading_like(lines[0]):
            if heading or blocks:
                sections.append(Section(heading, blocks))
            heading, blocks = lines[0].strip(), []
        else:
            blocks.append(Paragraph(" ".join(line.strip() for line in lines)))
    if heading or blocks:
        sections.append(Section(heading, blocks))
    if not sections:
        sections = [Section("", [Paragraph("")])]
    return Document(title=title[:TITLE_CHARS] or "A document", sections=sections)


def _heading_like(line: str) -> bool:
    words = line.strip()
    if BULLET.match(line) or NUMBERED.match(line):
        return False
    return 0 < len(words) <= 60 and words[-1] not in ".!?:;,)" and ":" not in words


def open_from_message(
    session_factory: sessionmaker[Session], store: ArtifactStore, topic_id: UUID, message_id: int
) -> dict[str, Any] | None:
    """The reply's document: made the first time, the same one afterwards."""
    with session_factory() as session, session.begin():
        topic = session.get(Topic, topic_id)
        message = session.get(RoomMessage, message_id)
        if topic is None or message is None or message.topic_id != topic.id:
            return None
        existing = _lineage_of_message(session, message_id)
        if existing is not None:
            return view(session, existing)
        project_id = topic.project_id or free_exploration(session).id
        document = from_text(message.body or "", topic.title or "A document")
        lineage = uuid4()
        _write_version(
            session,
            store,
            project_id,
            lineage,
            1,
            {EDITED_KEY: as_dict(document), "source": {"message_id": message_id}},
            {"topic_id": str(topic.id), "message_id": message_id},
            artifact_id=lineage,
        )
    with session_factory() as session:
        return view(session, lineage)


def save(
    session_factory: sessionmaker[Session],
    store: ArtifactStore,
    lineage: UUID,
    base_version: int,
    edited: dict[str, Any],
) -> dict[str, Any] | None:
    """The owner's edit as the document's next version."""
    document = edited_document(edited)
    with session_factory() as session, session.begin():
        versions = _versions(session, lineage)
        if not versions:
            return None
        current = versions[-1]
        if current.version != base_version:
            raise StaleDocument(current.version)
        _write_version(
            session,
            store,
            current.project_id,
            lineage,
            current.version + 1,
            {EDITED_KEY: as_dict(document)},
            {
                k: v
                for k, v in (current.metadata_json or {}).items()
                if k in ("topic_id", "message_id")
            },
        )
    with session_factory() as session:
        return view(session, lineage)


def view(session: Session, lineage: UUID) -> dict[str, Any] | None:
    """The document's versions, newest last, as an answer's are shown."""
    versions = _versions(session, lineage)
    if not versions:
        return None
    shown = [
        {
            "artifact_id": str(row.id),
            "version": row.version,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "note": "",
            "by": row.created_by,
        }
        for row in versions
    ]
    return {
        "lineage": str(lineage),
        "topic_id": (versions[0].metadata_json or {}).get("topic_id"),
        "current": shown[-1],
        "versions": shown,
    }


def _versions(session: Session, lineage: UUID) -> list[Artifact]:
    return list(
        session.scalars(
            select(Artifact)
            .where(
                Artifact.type == KIND,
                Artifact.metadata_json["lineage"].astext == str(lineage),
            )
            .order_by(Artifact.version)
        )
    )


def _lineage_of_message(session: Session, message_id: int) -> UUID | None:
    row = session.scalar(
        select(Artifact)
        .where(
            Artifact.type == KIND,
            Artifact.metadata_json["message_id"].astext == str(message_id),
        )
        .limit(1)
    )
    return UUID(str((row.metadata_json or {})["lineage"])) if row is not None else None


def _write_version(
    session: Session,
    store: ArtifactStore,
    project_id: UUID,
    lineage: UUID,
    version: int,
    payload: dict[str, Any],
    extra: dict[str, Any],
    artifact_id: UUID | None = None,
) -> None:
    metadata = ArtifactMetadata(
        artifact_id=artifact_id or uuid4(),
        project_id=project_id,
        type=KIND,
        created_by="owner",
        version=version,
    )
    body = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    path = store.write(metadata, body)
    session.execute(
        insert(Artifact).values(
            id=metadata.artifact_id,
            project_id=project_id,
            task_id=None,
            run_id=None,
            type=KIND,
            path=str(path),
            created_by="owner",
            version=version,
            metadata_json=ArtifactStore.metadata_json(metadata) | {"lineage": str(lineage)} | extra,
        )
    )

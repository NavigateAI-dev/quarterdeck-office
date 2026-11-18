"""AI history taken out as files: the owner's data, readable without the office."""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.archive.memories import export_markdown
from app.db.models import ArchiveConversation, ArchiveMemory, ArchiveMessage
from app.privacy.detect import masked

README = """Your AI history, exported from your office.

conversations/  one JSON file per conversation, as it was imported
memories.json   every memory with its status and the messages it rests on
memories.md     what you kept, as a document to hand to another AI

{note}
"""


def write_zip(session: Session, source: str | None, mask: bool) -> Path:
    """To a private temp file, one conversation at a time. The caller removes it."""

    def clean(text: str) -> str:
        return masked(text)[0] if mask else text

    descriptor, name = tempfile.mkstemp(prefix="ai-history-", suffix=".zip")
    os.close(descriptor)
    path = Path(name)
    statement = (
        select(ArchiveConversation)
        .where(ArchiveConversation.state != "deleted")
        .order_by(ArchiveConversation.started_at.nulls_last(), ArchiveConversation.id)
    )
    if source:
        statement = statement.where(ArchiveConversation.provider == source)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
            note = (
                "Secrets and identity numbers are masked."
                if mask
                else ("This file holds secrets and identity numbers as written. Keep it safe.")
            )
            bundle.writestr("README.txt", README.format(note=note))
            wanted = list(session.scalars(statement))
            for row in wanted:
                bundle.writestr(
                    f"conversations/{row.provider}-{row.id}.json",
                    json.dumps(_conversation(session, row, clean), ensure_ascii=False, indent=1),
                )
            ids = [row.id for row in wanted]
            bundle.writestr(
                "memories.json",
                json.dumps(_memories(session, ids, clean), ensure_ascii=False, indent=1),
            )
            bundle.writestr("memories.md", clean(export_markdown(session, None, 10**7)))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _conversation(session: Session, row: ArchiveConversation, clean: Any) -> dict[str, Any]:
    said = session.scalars(
        select(ArchiveMessage)
        .where(ArchiveMessage.conversation_id == row.id)
        .order_by(ArchiveMessage.position, ArchiveMessage.id)
        .execution_options(yield_per=500)
    )
    return {
        "id": str(row.id),
        "source": row.provider,
        "original_id": row.external_id,
        "title": clean(row.title),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "hidden_from_agents": row.state == "excluded",
        "messages": [
            {
                "id": message.id,
                "role": message.role,
                "at": message.sent_at.isoformat() if message.sent_at else None,
                "content": clean(message.content),
            }
            for message in said
        ],
    }


def _memories(session: Session, ids: list[Any], clean: Any) -> list[dict[str, Any]]:
    rows = session.scalars(select(ArchiveMemory).where(ArchiveMemory.conversation_id.in_(ids)))
    return [
        {
            "conversation": str(row.conversation_id),
            "kind": row.kind,
            "said_by": row.said_by,
            "status": row.status,
            "content": clean(row.content),
            "corrected_from": clean(row.corrected_from) if row.corrected_from else None,
            "evidence": [
                {"message_id": item["message_id"], "quote": clean(item["quote"])}
                for item in row.evidence_json or []
            ],
        }
        for row in rows
    ]

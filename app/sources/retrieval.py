"""Retrieve passages from a project's granted local sources.

Grants are checked at retrieval time, so a revoked grant or deleted source is
never retrieved again. Candidates are found cheaply in code (term overlap),
then ranked by a relevance judgment when one is available; a passage judged
less than "partly answers" is dropped. Every passage carries its source,
path, content hash and chunk number for provenance.
"""

import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import KnowledgeSource, ProjectSource, SourceDocument
from app.judgments import JudgmentUnavailable

CHUNK_CHARS = 1200
CANDIDATES = 30
PASSAGES = 8
# Score levels run 0 (unrelated) to 3 (directly answers); 2 is "partly answers".
MIN_RELEVANCE = 1.5
WORD = re.compile(r"[a-z0-9]{3,}")
# Chinese and Japanese are written without spaces: a run of these characters
# is matched by each pair in it, so "护肤步骤" meets "护肤".
HAN = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]{2,}")


@dataclass(frozen=True)
class Passage:
    source: str
    path: str
    sha256: str
    chunk: int
    text: str

    @property
    def citation(self) -> str:
        # Fixed host and percent-encoded parts: names with spaces or capitals
        # round-trip through URL validation unchanged.
        source, path = quote(self.source, safe=""), quote(self.path, safe="/")
        return f"local://docs/{source}/{path}#chunk-{self.chunk}"

    def provenance(self) -> dict[str, Any]:
        return {
            "citation": self.citation,
            "source": self.source,
            "path": self.path,
            "sha256": self.sha256,
            "chunk": self.chunk,
        }


class RelevanceJudge(Protocol):
    async def relevance(self, question: str, passages: list[str]) -> list[float]: ...


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split on blank lines, packing paragraphs up to `size`; split longer ones."""
    chunks: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", text):
        pieces = [paragraph[i : i + size] for i in range(0, len(paragraph), size)] or [""]
        for piece in pieces:
            joined = f"{current}\n\n{piece}" if current else piece
            if len(joined) <= size:
                current = joined
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current.strip():
        chunks.append(current)
    return chunks


def granted_passages(session: Session, project_id: UUID) -> list[Passage]:
    rows = session.execute(
        select(
            KnowledgeSource.name, SourceDocument.path, SourceDocument.sha256, SourceDocument.text
        )
        .join(SourceDocument, SourceDocument.source_id == KnowledgeSource.id)
        .join(ProjectSource, ProjectSource.source_id == KnowledgeSource.id)
        .where(ProjectSource.project_id == project_id)
        .order_by(KnowledgeSource.name, SourceDocument.path)
    )
    return [
        Passage(source=name, path=path, sha256=sha, chunk=index, text=chunk)
        for name, path, sha, text in rows
        for index, chunk in enumerate(chunk_text(text))
    ]


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    terms = set(WORD.findall(lowered))
    for run in HAN.findall(lowered):
        terms.update(run[i : i + 2] for i in range(len(run) - 1))
    return terms


def lexical_candidates(
    query: str, passages: list[Passage], limit: int = CANDIDATES
) -> list[Passage]:
    wanted = _terms(query)
    scored = [(len(wanted & _terms(p.text)), index, p) for index, p in enumerate(passages)]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [p for overlap, _, p in scored[:limit] if overlap > 0 or len(passages) <= limit]


async def select_passages(
    query: str,
    passages: list[Passage],
    judge: RelevanceJudge | None,
    limit: int = PASSAGES,
) -> tuple[list[Passage], str]:
    candidates = lexical_candidates(query, passages)
    if judge is None:
        return candidates[:limit], "lexical"
    try:
        scores = await judge.relevance(query, [p.text for p in candidates])
    except JudgmentUnavailable:
        return candidates[:limit], "lexical_fallback"
    ranked = sorted(
        (
            item
            for item in zip(scores, range(len(candidates)), candidates, strict=False)
            if item[0] >= MIN_RELEVANCE
        ),
        key=lambda item: (-item[0], item[1]),
    )
    return [p for _, _, p in ranked[:limit]], "typesafe"


def source_summary(session: Session, project_id: UUID) -> list[str]:
    rows = session.execute(
        select(KnowledgeSource.name, KnowledgeSource.document_count)
        .join(ProjectSource, ProjectSource.source_id == KnowledgeSource.id)
        .where(ProjectSource.project_id == project_id)
        .order_by(KnowledgeSource.name)
    )
    return [f"{name} ({count} documents)" for name, count in rows]


def granted_passages_exist(session: Session, project_id: UUID) -> bool:
    return (
        session.scalar(
            select(ProjectSource.id).where(ProjectSource.project_id == project_id).limit(1)
        )
        is not None
    )

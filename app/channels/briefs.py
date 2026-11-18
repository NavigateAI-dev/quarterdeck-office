"""A task's research, as a brief in the Slack thread it was asked in.

When a task's research step finishes, its thread gets one card: the main
findings, the first sources, how sure the researcher is, and what is still
unknown. The answer comes later; the brief lets the owner steer before it
does, by replying in the thread. Only the thread the work was asked in gets
it, never a notification channel, which may be a shared room.

Save to Knowledge keeps the research as a document under "Saved from Slack",
replacing an earlier save of the same task.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.channels import slack_format
from app.channels.progress import ACTIVE, origin_destination
from app.channels.results import office_link, task_about
from app.db.models import (
    ChannelDelivery,
    KnowledgeSource,
    Project,
    Run,
    RunEvent,
    SourceDocument,
    Task,
)

EVENT = "brief"
SAVED_SOURCE = "Saved from Slack"
FINDINGS = 3
SOURCES = 3
FINDING_CHARS = 300


def key_of(run_id: object) -> str:
    return f"run:{run_id}/{EVENT}"


def _report(run: Run) -> dict[str, Any] | None:
    report = (run.state_json or {}).get("research")
    return report if isinstance(report, dict) and report.get("key_findings") else None


def _link(source: dict[str, Any]) -> str | None:
    url = str(source.get("url") or "")
    title = str(source.get("title") or url)[:120]
    if not re.match(r"^https?://[^\s<>|]+$", url):
        return None  # An owner's own document, or an address Slack could not link.
    return f"<{url}|{slack_format.escaped(title).replace('|', '¦')}>"


def brief_card(session: Session, run: Run, report: dict[str, Any]) -> slack_format.Card:
    task = session.get(Task, run.root_task_id)
    objective = ((task.objective if task else "") or "the task").strip().splitlines()[0][:120]
    findings = [str(item) for item in report.get("key_findings") or []][:FINDINGS]
    sources = [item for item in report.get("sources") or [] if isinstance(item, dict)]
    lines = [f"• {slack_format.escaped(item[:FINDING_CHARS])}" for item in findings]
    links = [link for link in (_link(item) for item in sources[:SOURCES]) if link]
    if links:
        lines.append("Sources: " + ", ".join(links))
    primary = sum(1 for item in sources if item.get("source_type") == "primary")
    facts = [
        ("Sources", f"{len(sources)} ({primary} primary)"),
        ("Confidence", str(report.get("confidence") or "not said")[:80]),
    ]
    unknowns = report.get("unknowns") or []
    if unknowns:
        facts.append(("Still unknown", str(len(unknowns))))
    link = office_link(session, run.id)
    buttons: list[dict[str, Any]] = []
    if link:
        buttons.append(slack_format.button("Read", slack_format.OPEN, url=link, style="primary"))
    buttons.append(slack_format.button("Save to Knowledge", slack_format.SAVE, str(run.id)))
    return slack_format.Card(
        kind="Research brief",
        title=objective,
        tone="done",
        status="Research done",
        about=task_about(session, run),
        body="\n".join(lines),
        facts=tuple(facts),
        footer="_The answer follows. Reply in this thread to steer it first._",
        buttons=tuple(buttons),
    )


def _researched(session: Session, run_id: UUID) -> bool:
    return (
        session.scalar(
            select(RunEvent.id)
            .where(
                RunEvent.run_id == run_id,
                RunEvent.kind == "step_completed",
                RunEvent.payload_json["node"].as_string() == "research",
            )
            .limit(1)
        )
        is not None
    )


def queue_briefs(session_factory: sessionmaker[Session]) -> int:
    """Queue a brief for each Slack task whose research has just finished. Returns how many."""
    created = 0
    with session_factory() as session, session.begin():
        origin = Run.config_snapshot_json["origin"]
        runs = session.scalars(
            select(Run).where(origin["kind"].as_string() == "slack", Run.status.in_(ACTIVE))
        )
        for run in runs:
            said = (run.config_snapshot_json or {}).get("origin") or {}
            report = _report(run)
            if said.get("standing") or report is None or not _researched(session, run.id):
                continue
            if session.scalar(
                select(ChannelDelivery.id).where(ChannelDelivery.idempotency_key == key_of(run.id))
            ):
                continue
            reply = origin_destination(session, said)
            if reply is None:
                continue
            destination_id, app_id, agent_profile_id = reply
            card = brief_card(session, run, report)
            result = session.execute(
                insert(ChannelDelivery)
                .values(
                    destination_id=destination_id,
                    run_id=run.id,
                    event_kind=EVENT,
                    idempotency_key=key_of(run.id),
                    text=slack_format.card_text(card),
                    status="QUEUED",
                    app_id=app_id,
                    agent_profile_id=agent_profile_id,
                    thread_ref=said.get("thread"),
                    blocks_json=slack_format.card_blocks(card),
                )
                .on_conflict_do_nothing(index_elements=[ChannelDelivery.idempotency_key])
                .returning(ChannelDelivery.id)
            )
            created += 1 if result.scalar_one_or_none() is not None else 0
    return created


def as_text(objective: str, report: dict[str, Any]) -> str:
    """The research as a document a later search can find."""
    parts = [f"# {objective}", "", f"Question: {report.get('research_question') or objective}"]
    for heading, key in (
        ("Findings", "key_findings"),
        ("Evidence", "evidence"),
        ("Against", "contradictory_evidence"),
        ("Still unknown", "unknowns"),
        ("What it means", "implications"),
    ):
        items = [str(item) for item in report.get(key) or []]
        if items:
            parts += ["", f"## {heading}", *(f"- {item}" for item in items)]
    sources = [item for item in report.get("sources") or [] if isinstance(item, dict)]
    if sources:
        parts += ["", "## Sources", *(f"- {s.get('title')}: {s.get('url')}" for s in sources)]
    parts += ["", f"Confidence: {report.get('confidence') or 'not said'}"]
    return "\n".join(parts) + "\n"


def _saved_source(session: Session) -> KnowledgeSource:
    source = session.scalar(
        select(KnowledgeSource).where(
            KnowledgeSource.name == SAVED_SOURCE, KnowledgeSource.kind == "upload"
        )
    )
    if source is None:
        taken = session.scalar(
            select(KnowledgeSource.id).where(KnowledgeSource.name == SAVED_SOURCE)
        )
        source = KnowledgeSource(
            name=SAVED_SOURCE if taken is None else f"{SAVED_SOURCE} (briefs)",
            kind="upload",
            status="EMPTY",
        )
        session.add(source)
        session.flush()
    return source


def save_brief(session_factory: sessionmaker[Session], run_id: UUID) -> str:
    """Keep a task's research in Knowledge. Returns what to say to the owner."""
    with session_factory() as session, session.begin():
        run = session.get(Run, run_id)
        report = _report(run) if run is not None else None
        if run is None or report is None:
            return "That task has no research to save yet."
        task = session.get(Task, run.root_task_id)
        objective = ((task.objective if task else "") or "Research").strip().splitlines()[0]
        project = session.get(Project, run.project_id)
        folder = re.sub(r"[/\\]", "-", project.name if project else "Office")[:80]
        name = re.sub(r"[^\w\s-]", "", objective, flags=re.UNICODE).strip()[:60] or "research"
        path = f"{folder}/{name} ({str(run.id)[:8]}).md"
        text = as_text(objective, report)
        data = text.encode()
        source = _saved_source(session)
        session.execute(
            delete(SourceDocument).where(
                SourceDocument.source_id == source.id,
                # An earlier save of this task, even under a project renamed since.
                SourceDocument.path.endswith(f" ({str(run.id)[:8]}).md", autoescape=True),
            )
        )
        session.add(
            SourceDocument(
                source_id=source.id,
                path=path,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                text=text,
            )
        )
        session.flush()
        source.document_count = (
            session.scalar(select(func.count()).where(SourceDocument.source_id == source.id)) or 0
        )
        source.status = "INDEXED"
        source.indexed_at = datetime.now(UTC)
        return f"✓ Saved to Knowledge, under {source.name}."

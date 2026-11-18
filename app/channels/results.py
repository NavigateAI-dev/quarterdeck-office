"""What a task asked for in Slack says back in its thread.

The thread used to get one line, "…has finished, open your office", and the
owner had to go to the office to read anything. Now the thread gets the work:
the answer itself, cut at a readable length, and the whole document as a file
in the format the task was asked for (Word when none was). A failure says why
in words, and every message links to the task when the office has an outside
address a phone can open.

Only the thread the work was asked in gets the content. Notification channels
are often shared rooms, so they keep the one line, plus the link.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import origin_of
from app.approvals import ApprovalScope, describe_consequence, low_risk, risk_reason
from app.artifacts import ArtifactStore
from app.channels import slack_format
from app.db.models import AccessPoint, AgentProfile, Approval, Artifact, Project, Run, Task
from app.documents.charts import charts_in, png
from app.documents.model import Document
from app.documents.office_files import to_docx, to_pptx, to_xlsx
from app.documents.render import document_for
from app.documents.serialise import to_html, to_markdown
from app.work_performance import run_cost

# The kinds that are a task's answer, in the order the task page prefers them.
ANSWER_TYPES = ("synthesis", "strategy_proposal", "final_report")
# Formats a file can be written in; "onscreen" and anything else become Word.
FILE_FORMATS = ("docx", "xlsx", "pptx", "md", "html")
DEFAULT_FORMAT = "docx"
# The office's own reasons for stopping, in words (as the task page says them).
STOP_TEXT = {
    "providers_unavailable": "The model providers were unavailable or refusing.",
    "unexpected_error": "The office hit a fault of its own, not a problem with the work.",
    "engineering_failed": "The build failed.",
    "engineering_outcome_uncertain": "The office stopped during the build and cannot tell "
    "whether it finished.",
    "revision_not_converging": "The critic kept raising the same concerns.",
    "revision_made_no_change": "A revision changed nothing.",
    "revision_limit_reached": "The office used all its revision rounds.",
    "critic_blocked": "The critic found a problem it would not pass.",
    "needs_owner_answers": "The Chief needs your answers before the research can start.",
    "evidence_not_reachable": "The evidence this needs is out of the office's reach.",
    "stopped_by_owner": "You stopped it.",
}
# The order an outside address is offered in: the private remote route works
# from anywhere, the home network only at home.
ADDRESS_ORDER = ("remote", "home")


@dataclass
class ThreadMessage:
    text: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    # Files to upload with it: {"artifact_id", "format"}.
    files: list[dict[str, str]] = field(default_factory=list)


def stop_text(code: str | None) -> str:
    text = str(code or "")
    known = STOP_TEXT.get(text) or STOP_TEXT.get(text.split(":", 1)[0])
    if known:
        return known
    return text.replace("_", " ") if text else "The office could not finish on its own."


def office_origin(session: Session) -> str | None:
    """The office at a verified address a phone can open, if there is one."""
    for kind in ADDRESS_ORDER:
        point = session.get(AccessPoint, kind)
        if point and point.enabled and point.address and point.verified_at is not None:
            return origin_of(point.address)
    return None


def office_link(session: Session, run_id: object) -> str | None:
    """The task in the office, at a verified address a phone can open, if any."""
    origin = office_origin(session)
    return f"{origin}/#task/{run_id}" if origin else None


# At most this many charts go to Slack as pictures; the page has them all.
SLACK_CHARTS = 3


def answer_artifact(session: Session, run_id: UUID) -> Artifact | None:
    rows = list(
        session.scalars(
            select(Artifact)
            .where(Artifact.run_id == run_id, Artifact.type.in_(ANSWER_TYPES))
            .order_by(Artifact.created_at, Artifact.version)
        )
    )
    for kind in ANSWER_TYPES:
        newest = [row for row in rows if row.type == kind]
        if newest:
            return newest[-1]
    return None


def document_of(session: Session, store: ArtifactStore, artifact: Artifact) -> Document | None:
    """The artifact as a document, as the office's download writes it."""
    try:
        body = store.read(artifact.project_id, artifact.id, artifact.version)
    except (OSError, ValueError):
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {"text": body}
    task = session.get(Task, artifact.task_id) if artifact.task_id else None
    objective = task.objective if task is not None else None
    title = objective.strip().splitlines()[0][:120] if objective and objective.strip() else None
    fields = payload if isinstance(payload, dict) else {"text": body}
    return document_for(artifact.type, fields, title)


def file_format(run: Run) -> str:
    asked = str((run.deliverable_json or {}).get("format") or "")
    return asked if asked in FILE_FORMATS else DEFAULT_FORMAT


def took(run: Run, now: datetime | None = None) -> str | None:
    """How long the task has been working, or took, in whole minutes."""
    if run.started_at is None:
        return None
    end = run.completed_at or now or datetime.now(UTC)
    minutes = max(0, int((end - run.started_at).total_seconds() // 60))
    if minutes < 1:
        return "under a minute"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if hours else f"{minutes} min"


def spent(session: Session, run: Run) -> str:
    """What the task has cost so far, against its own limit when it has one."""
    cost = run_cost(session, [run.id]).get(run.id, 0.0)
    limit = (run.budget_json or {}).get("max_cost_usd")
    if isinstance(limit, int | float) and limit > 0:
        return f"${cost:.2f} of ${float(limit):.2f}"
    return f"${cost:.2f}"


def task_facts(
    session: Session, run: Run, now: datetime | None = None
) -> tuple[tuple[str, str], ...]:
    time = took(run, now)
    return ((("Time", time),) if time else ()) + (("Cost", spent(session, run)),)


def task_about(session: Session, run: Run) -> tuple[str, ...]:
    """Who works on it and where: the agent the work was asked of, and its project."""
    origin = (run.config_snapshot_json or {}).get("origin") or {}
    about: list[str] = []
    profile_id = origin.get("agent_profile_id")
    if profile_id:
        try:
            profile = session.get(AgentProfile, UUID(str(profile_id)))
        except ValueError:
            profile = None
        if profile is not None:
            about.append(profile.name)
    project = session.get(Project, run.project_id)
    if project is not None:
        about.append(project.name)
    return tuple(about)


def _build_line(run: Run) -> str | None:
    state = run.state_json or {}
    if not state.get("build_commit") and not state.get("build_folder"):
        return None
    return (
        "The build is saved as a new version. Preview it, download it or put it online "
        "from the task in the office."
    )


def _title(session: Session, run: Run) -> str:
    """The task in the owner's words, its first line, unescaped (a card escapes it)."""
    task = session.get(Task, run.root_task_id)
    text = (task.objective if task else "") or "the task"
    return (text.strip().splitlines()[0] if text.strip() else "the task")[:120]


def _task_card(
    session: Session,
    run: Run,
    tone: str,
    status: str,
    body: str,
    buttons: tuple[dict[str, Any], ...],
    footer: str = "",
) -> slack_format.Card:
    return slack_format.Card(
        kind="Agent task",
        title=_title(session, run),
        tone=tone,
        status=status,
        about=task_about(session, run),
        body=body,
        facts=task_facts(session, run),
        footer=footer,
        buttons=buttons,
    )


def _message(card: slack_format.Card, files: list[dict[str, str]] | None = None) -> ThreadMessage:
    return ThreadMessage(slack_format.card_text(card), slack_format.card_blocks(card), files or [])


def thread_message(
    session: Session,
    store: ArtifactStore,
    run: Run,
    event: str,
    page: Callable[[object], str | None] | None = None,
) -> ThreadMessage | None:
    """The message for the thread the work was asked in, by what happened."""
    link = office_link(session, run.id)
    run_id = str(run.id)
    finished = slack_format.rework_buttons(run_id, link)
    if event == "completed":
        built = _build_line(run)
        artifact = answer_artifact(session, run.id)
        document = document_of(session, store, artifact) if artifact is not None else None
        if artifact is None or document is None:
            body = built or "The answer is in the office."
            return _message(_task_card(session, run, "done", "Finished", body, finished))
        answer, cut = slack_format.answer_text(document)
        if built:
            # The software is the deliverable, not a document about it.
            body = "\n\n".join(part for part in (answer, built) if part)
            return _message(
                _task_card(
                    session,
                    run,
                    "done",
                    "Finished",
                    body,
                    finished,
                    "_Reply in this thread with what to change for the next version._",
                )
            )
        fmt = file_format(run)
        parts = [answer]
        if cut:
            parts.append(f"_The rest is in the attached file ({fmt})._")
        # The answer as a page of its own, charts and all.
        origin = office_origin(session)
        address = page(artifact.id) if page is not None else None
        if origin and address:
            parts.append(f"<{origin}{address}|Read it as a page>")
        drawn = charts_in(document)[:SLACK_CHARTS]
        standing = ((run.config_snapshot_json or {}).get("origin") or {}).get("standing")
        footer = (
            f"_A standing report, {slack_format.escaped(str(standing))}._"
            if standing
            else "_Reply in this thread to ask for changes or a next step._"
        )
        card = _task_card(
            session, run, "done", "Finished", "\n\n".join(p for p in parts if p), finished, footer
        )
        return _message(
            card,
            [
                *(
                    {"artifact_id": str(artifact.id), "format": f"chart{n}"}
                    for n in range(len(drawn))
                ),
                {"artifact_id": str(artifact.id), "format": fmt},
            ],
        )
    if event == "failed":
        why = stop_text((run.state_json or {}).get("stopped_reason") or run.error)
        return _message(
            _task_card(
                session,
                run,
                "failed",
                "Failed",
                slack_format.escaped(why),
                finished,
                "_Rework tries again; replying in this thread starts a follow-up._",
            )
        )
    if event == "needs_approval":
        return _message(approval_card(session, run, link))
    if event == "needs_input":
        why = stop_text((run.state_json or {}).get("stopped_reason") or run.error)
        return _message(
            _task_card(
                session,
                run,
                "waiting",
                "Needs your decision",
                f"{slack_format.escaped(why)}\n"
                "Decide in the office: accept the work as it is, or say what to change.",
                tuple(slack_format.open_button(link)),
            )
        )
    return None


def approval_card(session: Session, run: Run, link: str | None) -> slack_format.Card:
    """The approval a task waits for: what it proposes, its risk, and what to press.

    Approve and Reject are offered only for a low-risk proposal (`low_risk`):
    anything that runs code or reaches outside the office is approved in the
    office, where the whole proposal can be read.
    """
    approval = session.scalar(
        select(Approval)
        .where(Approval.run_id == run.id, Approval.status == "PENDING")
        .order_by(Approval.requested_at.desc())
        .limit(1)
    )
    scope = ApprovalScope.model_validate(approval.scope_json) if approval is not None else None
    if approval is None or scope is None:
        return _task_card(
            session,
            run,
            "waiting",
            "Waiting for your approval",
            "Approve it in the office, where you can read exactly what is proposed.",
            tuple(slack_format.open_button(link)),
        )
    safe = low_risk(scope)
    recommendation = slack_format.escaped(scope.recommendation.strip()[:400])
    consequence = slack_format.escaped(describe_consequence(scope))
    body = f"{recommendation}\n_{consequence}_"
    if not safe:
        body += (
            "\nThis one runs code or reaches outside the office, so it is approved in the office."
        )
    facts: list[tuple[str, str]] = [
        ("Risk", "Low: records a decision only" if safe else f"Higher: {risk_reason(scope)}"),
        ("Asked", f"{approval.requested_at:%d %b %H:%M} UTC" if approval.requested_at else "now"),
    ]
    if scope.unverified_claims:
        facts.append(("Unverified claims", str(len(scope.unverified_claims))))
    facts += task_facts(session, run)
    return slack_format.Card(
        kind="Approval",
        title=_title(session, run),
        tone="waiting",
        status="Waiting for your approval",
        about=task_about(session, run),
        body=body,
        facts=tuple(facts),
        buttons=slack_format.approval_buttons(
            str(approval.id), approval.scope_digest or scope.digest(), link, safe
        ),
    )


def _slug(text: str) -> str:
    """A filename in the task's own words and script, as the office's download names it."""
    cleaned = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    return re.sub(r"[\s_-]+", "-", cleaned)[:60].strip("-").lower()


def render_file(
    session: Session, store: ArtifactStore, artifact_id: str, fmt: str
) -> tuple[str, bytes] | None:
    """A file to upload: its name and bytes, written as the office's download is."""

    artifact = session.get(Artifact, UUID(artifact_id))
    chart = fmt.startswith("chart") and fmt[5:].isdigit()
    if artifact is None or (fmt not in FILE_FORMATS and not chart):
        return None
    document = document_of(session, store, artifact)
    if document is None:
        return None
    if chart:
        drawn = charts_in(document)
        index = int(fmt[5:])
        if index >= len(drawn):
            return None
        return f"chart-{index + 1}.png", png(drawn[index])
    writers = {
        "md": lambda doc: to_markdown(doc).encode(),
        "html": lambda doc: to_html(doc).encode(),
        "docx": to_docx,
        "xlsx": to_xlsx,
        "pptx": to_pptx,
    }
    name = f"{_slug(document.title) or artifact.type}-v{artifact.version}.{fmt}"
    return name, writers[fmt](document)

"""Layered run context (SPEC section 13), scoped to one project.

Layers: the office, the project's own background, the owner's recorded
decisions for it, and the recommendations of its recent finished runs. Every
query filters by project, and every layer is bounded, so context never crosses
projects and cannot grow without limit.
"""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifacts import ArtifactStore
from app.config import Settings
from app.db.models import Artifact, Decision, Project, Run, Venture
from app.db.models import Project as ProjectModel
from app.memory.store import approved
from app.objectives import active, as_lines
from app.sources.retrieval import source_summary

MAX_DECISIONS = 10
MAX_OUTCOMES = 5
MAX_ITEM_CHARS = 600
OUTCOME_STATUSES = ("COMPLETED", "WAITING_FOR_APPROVAL", "REJECTED", "CLOSED")


def _clip(text: str) -> str:
    return text if len(text) <= MAX_ITEM_CHARS else text[: MAX_ITEM_CHARS - 1] + "…"


def objective_lines(session: Session, project: Project) -> list[str]:
    """What this project's business line is working towards now."""

    return as_lines(active(session, project.venture_id))


def venture_doctrine(session: Session, project_id: Any) -> tuple[str, dict[str, str]]:
    """The business line of this project, and what it says to each role."""

    project = session.get(ProjectModel, project_id)
    if project is None or project.venture_id is None:
        return "", {}
    venture = session.get(Venture, project.venture_id)
    if venture is None:
        return "", {}
    doctrine = {
        str(role): str(text)
        for role, text in (venture.role_doctrine_json or {}).items()
        if str(text).strip()
    }
    return venture.name, doctrine


def venture_of(session: Session, project: Project) -> dict[str, str]:
    """The business line this project belongs to, and its doctrine."""
    from app.db.models import Venture

    if project.venture_id is None:
        return {"venture": "", "doctrine": ""}
    row = session.get(Venture, project.venture_id)
    if row is None:
        return {"venture": "", "doctrine": ""}
    return {"venture": row.name, "doctrine": row.doctrine or ""}


def approved_lessons(session: Session, project_id: Any) -> list[str]:
    """What this project has learned and the owner approved."""

    return approved(session, project_id)


def context_summary(session: Session, project: Project, settings: Settings) -> dict[str, Any]:
    """What a task in this project will be told, and how much of each.

    Built from `build_run_context` itself rather than from a second reading of
    the database, so what the office shows is what the agents are given. An
    office that assembles a task's context invisibly is asking to be trusted
    on it.
    """
    context = build_run_context(session, project, settings)
    return {
        "project_id": str(project.id),
        "project_name": project.name,
        "as_of": context["as_of"],
        "parts": summarize(context),
    }


def run_context_summary(run: Run, project_name: str) -> dict[str, Any]:
    """What this task was told, read from the context stored with it.

    Not the project's context now: that holds answers finished after this task
    began and lacks what only this task was given.
    """
    context = dict((run.state_json or {}).get("context") or {})
    return {
        "run_id": str(run.id),
        "project_id": str(run.project_id),
        "project_name": project_name,
        "as_of": context.get("as_of"),
        "parts": summarize(context),
    }


def summarize(context: dict[str, Any]) -> list[dict[str, Any]]:
    """A context, as the parts the owner can open, in the order they matter."""

    def items(key: str) -> list[str]:
        value = context.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return [str(value)] if isinstance(value, str) and value.strip() else []

    parts = [
        (
            f"How work is judged in {context.get('venture') or 'this line'}",
            items("doctrine"),
            "the doctrine of the business line this project is for",
        ),
        ("Project background", items("background"), "the description you gave this project"),
        ("The task it continues", items("continues"), "what the earlier task found"),
        (
            "The discussion it came from",
            items("discussion"),
            "what was being said when you handed it off",
        ),
        (
            "Earlier in this line of work",
            items("lineage"),
            "earlier links of this line, judged relevant to this question",
        ),
        (
            "Related work in the office",
            items("related"),
            "earlier answers from any project, judged relevant to this question",
        ),
        ("What it should come to", items("deliverable"), "the shape you asked for"),
        ("Your calendar", items("schedule"), "your next days, as much as you let agents see"),
        ("From your wall", items("wall"), "the widgets you let agents read"),
        ("What you attached", items("attached"), "files and folders you attached for this task"),
        ("What the office knows", items("dossiers"), "approved facts about its subjects"),
        ("Approved lessons", items("lessons"), "what earlier tasks learned and you kept"),
        ("Knowledge sources", items("local_sources"), "granted to this project"),
        ("Recorded decisions", items("decisions"), "your decisions on earlier work"),
        ("Earlier outcomes", items("prior_outcomes"), "what finished tasks recommended"),
        ("Tools", items("tools"), "reviewed tools this project was granted"),
    ]
    return [
        {"name": name, "count": len(listed), "note": note, "items": listed}
        for name, listed, note in parts
        if listed
    ]


def _decision_line(row: Decision) -> str:
    """A decision as agents are told it: an owner's own words stand alone; one
    recorded elsewhere keeps its title before it."""
    if row.title.rstrip("…") and row.decision.startswith(row.title.rstrip("…")):
        return _clip(row.decision)
    return _clip(f"{row.title}: {row.decision}")


def build_run_context(session: Session, project: Project, settings: Settings) -> dict[str, Any]:
    decisions = session.scalars(
        select(Decision)
        # In force only: a replaced decision is history, not instruction.
        .where(Decision.project_id == project.id, Decision.status == "active")
        .order_by(Decision.created_at.desc())
        .limit(MAX_DECISIONS)
    )
    return {
        "organization": settings.office_name,
        "as_of": datetime.now(UTC).date().isoformat(),
        "project_id": str(project.id),
        "project_name": project.name,
        "background": project.description,
        "decisions": [_decision_line(row) for row in decisions],
        "prior_outcomes": _prior_outcomes(session, project, settings),
        "local_sources": source_summary(session, project.id),
        "lessons": approved_lessons(session, project.id),
        **venture_of(session, project),
        "objectives": objective_lines(session, project),
    }


def _prior_outcomes(session: Session, project: Project, settings: Settings) -> list[str]:
    rows = session.execute(
        select(Artifact)
        .join(Run, Artifact.run_id == Run.id)
        .where(
            Artifact.project_id == project.id,
            Artifact.type == "synthesis",
            Run.status.in_(OUTCOME_STATUSES),
        )
        .order_by(Artifact.created_at.desc())
        .limit(MAX_OUTCOMES)
    ).scalars()
    store = ArtifactStore(settings.workspace_root)
    outcomes = []
    for artifact in rows:
        try:
            body = json.loads(store.read(project.id, artifact.id, artifact.version))
        except (OSError, ValueError):
            continue  # A missing or unreadable artifact is omitted, not invented.
        recommendation = body.get("recommendation") if isinstance(body, dict) else None
        if recommendation:
            outcomes.append(_clip(str(recommendation)))
    return outcomes

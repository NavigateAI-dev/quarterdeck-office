"""What still stands between a new owner and their first task.

Every step reports what is actually true — a stored key, a model the provider
confirmed, a folder that was read — never a box someone ticked. Steps the
office can do without are marked optional, so setup can end honestly.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    ChannelApp,
    ChannelInstallation,
    KnowledgeSource,
    ModelConnection,
    OfficeSettings,
    Run,
)
from app.office_settings import effective
from app.sample_office import installed as sample_installed
from app.status import roles_state

READY = ("MODEL_AVAILABLE", "VERIFIED")


def setup_state(session: Session, settings: Settings) -> dict[str, Any]:
    current = effective(session, settings)
    row = session.scalar(select(OfficeSettings).limit(1))
    connections = list(session.scalars(select(ModelConnection)))
    ready = [c for c in connections if c.status in READY]
    roles = roles_state(session, settings)
    sources = list(session.scalars(select(KnowledgeSource)))
    indexed = [s for s in sources if s.document_count]
    installation = session.scalar(
        select(ChannelInstallation).where(ChannelInstallation.status == "ACTIVE")
    )
    app = session.scalar(select(ChannelApp).where(ChannelApp.agent_profile_id.is_(None)))
    runs = int(session.scalar(select(func.count(Run.id))) or 0)
    sample = sample_installed(session)
    named = bool(getattr(row, "office_name", None))
    budget_set = current["monthly_cost_limit_usd"] is not None or bool(
        getattr(row, "run_max_cost_usd", None) or getattr(row, "run_max_tokens", None)
    )
    steps = [
        {
            "key": "name",
            "title": "Name your office",
            "done": named,
            "optional": False,
            "state": f"Called “{current['office_name']}”"
            if named
            else "Using the name from configuration",
        },
        {
            # Offered before the model, deliberately: choosing a provider is
            # easier once you have seen what a finished task looks like.
            "key": "sample",
            "title": "See a finished task first",
            "done": sample is not None,
            "optional": True,
            "state": (
                "A sample project is installed, marked as not real work"
                if sample is not None
                else "Optional: install a sample task, calling nothing and spending nothing"
            ),
        },
        {
            "key": "model",
            "title": "Connect a model",
            "done": roles["state"] == "ok",
            "optional": False,
            "state": (
                "Every role has a model"
                if roles["state"] == "ok"
                else f"No model yet for: {', '.join(roles['missing'])}"
                if connections
                else "No model yet: agents cannot work without one"
            ),
            "detail": {
                "connections": len(connections),
                "ready": len(ready),
                "roles_assigned": len(current["role_models"]),
                "roles_missing": roles["missing"],
            },
        },
        {
            "key": "budget",
            "title": "Set your limits",
            "done": budget_set,
            "optional": False,
            "state": (
                f"Monthly limit ${current['monthly_cost_limit_usd']:.2f}"
                if current["monthly_cost_limit_usd"] is not None
                else "Using the limits from configuration"
            ),
        },
        {
            "key": "knowledge",
            "title": "Add what the office may read",
            "done": bool(indexed),
            "optional": True,
            "state": (
                f"{len(indexed)} source(s), {sum(s.document_count for s in indexed)} documents"
                if indexed
                else "None yet; tasks will use the web and what you type"
            ),
        },
        {
            "key": "chat",
            "title": "Connect Slack",
            "done": installation is not None,
            "optional": True,
            "state": (
                f"Connected to {installation.workspace_name}"
                + (
                    ""
                    if app and app.app_token_ref
                    else " · add the app-level token to receive messages"
                )
                if installation
                else "Not connected; the office works without it"
            ),
        },
        {
            "key": "task",
            "title": "Start your first task",
            "done": runs > 0,
            "optional": False,
            "state": f"{runs} task(s) started" if runs else "Nothing started yet",
        },
    ]
    required = [step for step in steps if not step["optional"]]
    return {
        "steps": steps,
        "done": sum(1 for step in steps if step["done"]),
        "total": len(steps),
        "required_done": sum(1 for step in required if step["done"]),
        "required_total": len(required),
        "ready": all(step["done"] for step in required),
        "completed_at": current["setup_completed_at"],
        "office_name": current["office_name"],
        "owner_name": current["owner_name"],
    }


def mark_complete(session: Session) -> None:
    row = session.scalar(select(OfficeSettings).limit(1))
    if row is None:
        row = OfficeSettings(only=True)
        session.add(row)
    row.setup_completed_at = datetime.now(UTC)
    session.flush()

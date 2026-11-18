"""Which coding CLI on the owner's Mac does the office's coding, and whether a
build may list packages (Stage 36: the office runs no coding agent itself).

The CLIs are run by Quarterdeck Coder on the Mac (`scripts/mac_coder.py`), with
the owner's own sign-in; which one is bound into each approval
(`ApprovalScope.coding_agent`).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.coding_names import AGENTS, DEFAULT
from app.db.models import OfficeSettings


def chosen_agent(session: Session, settings: Any) -> str:
    row = session.scalar(select(OfficeSettings).limit(1))
    agent = str(getattr(row, "coding_agent", None) or getattr(settings, "coding_agent", "") or "")
    return agent if agent in AGENTS else DEFAULT


def set_agent(session: Session, agent: str) -> str:
    if agent not in AGENTS:
        raise ValueError("coding_agent_unknown")
    row = session.scalar(select(OfficeSettings).limit(1))
    if row is None:
        row = OfficeSettings(only=True)
        session.add(row)
    row.coding_agent = agent
    session.flush()
    return agent


def packages_allowed(session: Session) -> bool:
    row = session.scalar(select(OfficeSettings).limit(1))
    return bool(getattr(row, "build_packages", False))


def allow_packages(session: Session, allowed: bool) -> bool:
    row = session.scalar(select(OfficeSettings).limit(1))
    if row is None:
        row = OfficeSettings(only=True)
        session.add(row)
    row.build_packages = allowed
    session.flush()
    return allowed

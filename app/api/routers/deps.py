"""What every group of owner routes shares."""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.access import Reachable
from app.api.sessions import LoginCodes, LoginThrottle
from app.calendars.store import Calendars
from app.config import Settings
from app.handoff import Handoff
from app.mcp import oauth as mcp_oauth
from app.orchestrator.approval_store import ApprovalStore
from app.orchestrator.runner import RunRepository
from app.room_replies import RoomReplies
from app.run_actions import RunActions
from app.runtime.worker import RunWorker


@dataclass
class RouterDeps:
    """What the owner's routes share. Built once by `build_router`."""

    session_factory: sessionmaker[Session]
    repository: RunRepository
    worker: RunWorker
    approvals: ApprovalStore
    settings: Settings
    codes: LoginCodes | None
    reachable: Reachable | None
    throttle: LoginThrottle | None
    calendars: Calendars | None
    require_owner: Any
    actions: RunActions
    calendar_service: Calendars
    replies: RoomReplies
    sign_ins: mcp_oauth.Flows
    login_codes: LoginCodes
    handoff: Handoff = field(init=False)

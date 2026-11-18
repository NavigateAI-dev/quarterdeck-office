"""Starting the full team run the lead proposed on a task (`app/team_proposals.py`)."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app import team_proposals
from app.api.routers.deps import RouterDeps
from app.api.routes import found
from app.api.schemas import RunView
from app.db.models import Run
from app.office_settings import MonthlyLimitReached, OfficePaused
from app.orchestrator.graph import ANALYSIS_WORKFLOW
from app.run_start import start_run
from app.titles import title_of


def add_proposal_routes(router: APIRouter, d: RouterDeps) -> None:
    @router.post("/runs/{run_id}/team-run", response_model=RunView, status_code=202)
    def start_team_run(run_id: UUID) -> Any:
        """The owner's press: the lead's proposal becomes a full team task, once,
        building on this one and in the same discussion."""
        with d.session_factory() as session, session.begin():
            # Held while it starts, so two presses at once start one task.
            run = found(
                session.scalars(select(Run).where(Run.id == run_id).with_for_update()).first(),
                "Run",
            )
            proposal = team_proposals.of_run(session, run_id)
            if proposal is None or not proposal["objective"]:
                raise HTTPException(status_code=404, detail="no_team_proposal")
            if proposal["started_run_id"]:
                raise HTTPException(status_code=409, detail="already_started")
            try:
                team = start_run(
                    d.session_factory,
                    d.repository,
                    d.worker,
                    d.settings,
                    run.project_id,
                    proposal["objective"],
                    workflow=ANALYSIS_WORKFLOW,
                    continues_run_id=run_id,
                    topic_id=run.topic_id,
                )
            except OfficePaused:
                raise HTTPException(status_code=409, detail="office_paused") from None
            except MonthlyLimitReached:
                raise HTTPException(status_code=409, detail="monthly_limit_reached") from None
            team_proposals.started(session, run_id, team.id)
        team.title = title_of(None, proposal["objective"])  # type: ignore[attr-defined]
        return team

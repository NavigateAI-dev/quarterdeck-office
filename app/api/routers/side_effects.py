"""Writes that left the office, and the owner settling one (`app/side_effects.py`).

A call the office stopped in the middle of is "unknown": it is not sent again,
and the owner, who can look at the third party, says whether it happened.
"""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import OwnerIdentity
from app.api.routers.deps import RouterDeps
from app.api.routes import found
from app.db.models import Run
from app.side_effects import SideEffectNotFound, SideEffects, SideEffectSettled


class Settled(BaseModel):
    status: Literal["done", "failed"]


def add_side_effect_routes(router: APIRouter, d: RouterDeps) -> None:
    ledger = SideEffects(d.session_factory)
    Owner = Annotated[OwnerIdentity, Depends(d.require_owner)]

    @router.get("/runs/{run_id}/side-effects")
    def side_effects(run_id: UUID) -> list[dict[str, Any]]:
        with d.session_factory() as session:
            found(session.get(Run, run_id), "Run")
        return ledger.of_run(run_id)

    @router.post("/side-effects/{effect_id}/settle")
    def settle(effect_id: int, body: Settled, who: Owner) -> dict[str, Any]:
        try:
            return ledger.settle_by_owner(effect_id, body.status, who.name)
        except SideEffectNotFound:
            raise HTTPException(status_code=404, detail="side_effect_not_found") from None
        except SideEffectSettled:
            raise HTTPException(status_code=409, detail="side_effect_not_unknown") from None

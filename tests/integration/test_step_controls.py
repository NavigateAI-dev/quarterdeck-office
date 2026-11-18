"""The owner's "Do next" on a step of a running plan (13d), beside Skip."""

from uuid import UUID

import httpx
from sqlalchemy import update

from app.db.models import Run


async def test_do_next_is_kept_latest_first_while_the_work_runs(
    client: httpx.AsyncClient,  # noqa: F811
) -> None:
    state = client.app_state  # type: ignore[attr-defined]
    project = (await client.post("/projects", json={"name": "Steps"})).json()["id"]
    dispatch = state.repository.create(UUID(project), "Compare three CRMs")
    with state.session_factory() as session, session.begin():
        session.execute(update(Run).where(Run.id == dispatch.run_id).values(status="RUNNING"))
    for step in ("s2", "s3", "s2"):
        sent = await client.post(
            f"/runs/{dispatch.run_id}/controls", json={"kind": "prioritize", "text": step}
        )
        assert sent.status_code in (200, 201), sent.text
    assert state.repository.prioritized_steps(dispatch.run_id) == ["s2", "s3"]
    with state.session_factory() as session, session.begin():
        session.execute(update(Run).where(Run.id == dispatch.run_id).values(status="COMPLETED"))
    late = await client.post(
        f"/runs/{dispatch.run_id}/controls", json={"kind": "prioritize", "text": "s1"}
    )
    assert late.status_code == 409, "a finished task takes no more direction"

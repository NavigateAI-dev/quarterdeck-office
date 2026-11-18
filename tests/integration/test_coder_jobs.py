"""The coder on the owner's Mac (Stage 36a): the job protocol against the database."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.coder import jobs
from app.db.models import CoderJob, RunEvent

pytestmark = pytest.mark.integration

CLAUDE_INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"})
CLAUDE_EDIT = json.dumps(
    {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": "/Users/me/dev/office-worktrees/site/office/x/a.py"},
                }
            ]
        },
    }
)


def factory(client: httpx.AsyncClient) -> Any:
    return client.app_state.session_factory  # type: ignore[attr-defined]


async def a_run(client: httpx.AsyncClient) -> str:
    project = (await client.post("/projects", json={"name": "P"})).json()["id"]
    return str(
        (await client.post("/runs", json={"project_id": project, "objective": "o"})).json()["id"]
    )


def queued(client: httpx.AsyncClient, run_id: str | None = None, **given: Any) -> str:
    fields = {"folder": "site", "branch": "office/x", "cli": "claude_code", "brief": "Add a page"}
    with factory(client)() as session, session.begin():
        job = jobs.queue_job(session, run_id=UUID(run_id) if run_id else None, **(fields | given))
        return str(job.id)


async def hello(client: httpx.AsyncClient) -> dict[str, Any]:
    reply = await client.post(
        "/coder/hello",
        json={
            "version": "36a.1",
            "root": "/Users/me/dev",
            "clis": [
                {"id": "claude_code", "path": "/x/claude", "version": "2.1", "signed_in": True},
                {"id": "nonsense", "path": "/x/y"},
            ],
        },
    )
    assert reply.status_code == 200
    return dict(reply.json())


async def test_the_card_shows_the_helper_once_it_says_hello(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    before = (await client.get("/coder")).json()
    assert before["connected"] is False and before["clis"] == []
    assert await hello(client) == {"jobs": False}
    after = (await client.get("/coder")).json()
    assert after["connected"] is True
    assert after["root"] == "/Users/me/dev"
    # A CLI the office does not know is not shown.
    assert [cli["label"] for cli in after["clis"]] == ["Claude Code"]


async def test_a_job_is_handed_out_once(client: httpx.AsyncClient, no_dispatch: list[Any]) -> None:
    job_id = queued(client)
    assert await hello(client) == {"jobs": True}
    first = (await client.post("/coder/jobs/next")).json()["job"]
    assert (
        first["id"] == job_id and first["branch"] == "office/x" and first["brief"] == "Add a page"
    )
    assert (await client.post("/coder/jobs/next")).json() == {"job": None}
    assert await hello(client) == {"jobs": False}


@pytest.mark.parametrize(
    ("given", "code"),
    [
        ({"folder": "/Users/me/dev/site"}, "absolute_path"),
        ({"folder": "../etc"}, "outside_root"),
        ({"folder": "work/.ssh"}, "credential_folder"),
        ({"branch": "main"}, "branch_not_office"),
        ({"branch": "office/../main"}, "branch_not_office"),
        ({"cli": "cursor"}, "cli_unknown"),
        ({"brief": "  "}, "brief_empty"),
    ],
)
async def test_a_job_the_helper_may_not_do_is_never_stored(
    client: httpx.AsyncClient, no_dispatch: list[Any], given: dict[str, str], code: str
) -> None:
    with pytest.raises(jobs.JobRefused) as refused:
        queued(client, **given)
    assert refused.value.code == code


async def test_its_lines_become_the_tasks_steps_and_answer_with_stop_and_notes(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    run_id = await a_run(client)
    job_id = queued(client, run_id)
    await hello(client)
    await client.post("/coder/jobs/next")
    note = await client.post(f"/coder/jobs/{job_id}/notes", json={"text": "Use blue"})
    assert note.status_code == 200
    reply = await client.post(
        f"/coder/jobs/{job_id}/lines",
        json={
            "lines": [CLAUDE_INIT, CLAUDE_EDIT, "not json"],
            "worktree": "office-worktrees/site/x",
        },
    )
    assert reply.json() == {"stop": False, "notes": ["Use blue"], "now": False}
    # A note is handed over once.
    again = await client.post(f"/coder/jobs/{job_id}/lines", json={"lines": []})
    assert again.json() == {"stop": False, "notes": [], "now": False}
    assert (await client.post(f"/coder/jobs/{job_id}/stop")).json()["stop_asked"] is True
    stopped = await client.post(f"/coder/jobs/{job_id}/lines", json={"lines": []})
    assert stopped.json()["stop"] is True
    with factory(client)() as session:
        job = session.get(CoderJob, UUID(job_id))
        assert job is not None and job.session == "s-1" and job.state == "running"
        steps = [
            event.payload_json
            for event in session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == UUID(run_id), RunEvent.kind == "build_step"
                )
            )
        ]
    texts = [step.get("text") for step in steps]
    assert "Claude Code on your Mac took it." in texts
    # The file is named by its place in the worktree, as the helper reported it.
    assert "Edited a.py" in texts


async def test_the_last_word_closes_it_once(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    job_id = queued(client)
    await hello(client)
    await client.post("/coder/jobs/next")
    done = await client.post(
        f"/coder/jobs/{job_id}/done",
        json={
            "branch": "office/x",
            "head": "abc",
            "base": "def",
            "exit_code": 0,
            "take_over": "cd x",
        },
    )
    assert done.status_code == 200
    assert done.json()["state"] == "done" and done.json()["outcome"]["head"] == "abc"
    twice = await client.post(f"/coder/jobs/{job_id}/done", json={"exit_code": 0})
    assert twice.status_code == 409 and twice.json()["detail"] == "job_done"
    late = await client.post(f"/coder/jobs/{job_id}/lines", json={"lines": []})
    assert late.status_code == 409


async def test_a_report_on_another_branch_is_refused(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    job_id = queued(client)
    await client.post("/coder/jobs/next")
    moved = await client.post(f"/coder/jobs/{job_id}/done", json={"branch": "main", "exit_code": 0})
    assert moved.status_code == 409 and moved.json()["detail"] == "branch_changed"


async def test_a_failure_and_a_stop_are_said_as_such(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    failed, stopped = queued(client), queued(client, branch="office/y")
    await client.post("/coder/jobs/next")
    await client.post("/coder/jobs/next")
    one = await client.post(f"/coder/jobs/{failed}/done", json={"error": "timed_out"})
    two = await client.post(f"/coder/jobs/{stopped}/done", json={"stopped": True, "exit_code": 0})
    assert one.json()["state"] == "failed" and two.json()["state"] == "cancelled"


async def test_a_waiting_job_stopped_never_starts(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    job_id = queued(client)
    assert (await client.post(f"/coder/jobs/{job_id}/stop")).json()["state"] == "cancelled"
    assert (await client.post("/coder/jobs/next")).json() == {"job": None}


async def test_a_job_not_heard_from_becomes_unknown_and_is_not_handed_again(
    client: httpx.AsyncClient, no_dispatch: list[Any]
) -> None:
    run_id = await a_run(client)
    job_id = queued(client, run_id)
    claimed = datetime.now(UTC) - jobs.SILENT - timedelta(minutes=1)
    with factory(client)() as session, session.begin():
        assert jobs.claim_next(session, claimed) is not None
    assert await hello(client) == {"jobs": False}
    assert (await client.get(f"/coder/jobs/{job_id}")).json()["state"] == "unknown"
    assert (await client.post("/coder/jobs/next")).json() == {"job": None}
    # Its report, should it come after all, is kept: it says what happened.
    late = await client.post(
        f"/coder/jobs/{job_id}/done", json={"branch": "office/x", "exit_code": 0}
    )
    assert late.json()["state"] == "done"

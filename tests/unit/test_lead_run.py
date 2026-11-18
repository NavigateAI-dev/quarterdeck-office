"""A task run by the lead, in the worker."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.gateway.client import Completion, GatewayError, ToolCall, Usage
from app.lead.checks import CitationCheck
from app.lead.run import execute_lead_run, synthesis_of
from app.lead.service import LeadReply
from app.lead.tools import StepResult
from app.orchestrator.graph import AgentSet, RunFailure
from app.orchestrator.runner import Dispatch
from app.orchestrator.state import RunStatus
from app.policies.engine import Decision, Verdict
from app.policies.store import PolicyAsk
from app.schemas import Synthesis


def said(text: str) -> Completion:
    return Completion(model_alias="m", model="m", content=text, usage=Usage(), latency_seconds=0.0)


def proposing() -> Completion:
    arguments = {"objective": "Full e-bike study", "why": "Many sources"}
    return Completion(
        model_alias="m",
        model="m",
        content="",
        usage=Usage(),
        latency_seconds=0.0,
        tool_calls=[
            ToolCall(
                id="c1",
                name="propose_team_run",
                arguments=arguments,
                raw_arguments=json.dumps(arguments),
            )
        ],
    )


class Model:
    def __init__(self, *turns: Completion | Exception) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict[str, Any]]] = []

    async def complete(
        self, model_alias: str, messages: list[dict[str, Any]], **options: Any
    ) -> Any:
        self.seen.append(list(messages))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class Repository:
    def __init__(self, claimed: bool = True) -> None:
        self.claimed = claimed
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.finished: dict[str, Any] | None = None
        self.stages: dict[str, dict[str, Any]] = {}
        self.stopped: str | None = None
        self.failed: tuple[str | None, str] | None = None

    def claim(self, run_id: Any, worker_id: str) -> bool:
        return self.claimed

    def add_event(self, run_id: Any, kind: str, payload: dict[str, Any] | None = None) -> int:
        self.events.append((kind, payload or {}))
        return len(self.events)

    def renew_lease(self, run_id: Any, worker_id: str) -> None:
        pass

    def record_stage(self, dispatch: Any, stage: str, payload: dict[str, Any]) -> None:
        self.stages[stage] = payload

    def finish(self, run_id: Any, state: dict[str, Any], current_node: str | None = None) -> None:
        self.finished = state

    def expire_stop_controls(self, run_id: Any) -> None:
        pass

    def stop(self, run_id: Any, status: str) -> None:
        self.stopped = status

    def fail(self, run_id: Any, node: str | None, code: str) -> bool:
        self.failed = (node, code)
        return True

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]

    notes: list[str] = []
    stop_kind: str | None = None
    kept: list[dict[str, Any]] = []
    state: dict[str, Any] = {}

    def recent_events(self, run_id: Any, kind: str, limit: int = 12) -> list[dict[str, Any]]:
        return self.kept

    def live_state(self, run_id: Any) -> dict[str, Any]:
        return self.state

    applied: bool = False

    def take_new_instructions(self, run_id: Any, node: str) -> list[str]:
        taken, self.notes = self.notes, []
        return taken

    def pending_stop(self, run_id: Any) -> dict[str, Any] | None:
        return {"kind": self.stop_kind} if self.stop_kind else None

    def apply_stop_controls(self, run_id: Any) -> None:
        self.applied = True


def team(chief: Model) -> AgentSet:
    roles = {
        name: SimpleNamespace(role=name, gateway=chief, model_alias=name)
        for name in ("planner", "researcher", "strategist", "critic", "chief")
    }
    return AgentSet(**roles)  # type: ignore[arg-type]


DISPATCH = Dispatch(run_id=uuid4(), task_id=uuid4(), project_id=uuid4())
CONTEXT = {"project_name": "Pilot", "background": "Dutch e-bike shop", "as_of": "2026-10-16"}


async def run(model: Model, repository: Repository) -> dict[str, Any]:
    return await execute_lead_run(
        DISPATCH, repository, team(model), "Rain in Lyon?", CONTEXT, work=[], worker_id="w1"
    )


async def test_a_lead_task_answers_and_finishes_like_any_task() -> None:
    repository = Repository()

    state = await run(Model(said("No rain today.")), repository)

    assert state["status"] == RunStatus.COMPLETED
    assert state["synthesis"]["lead"] == "No rain today."
    assert repository.finished == state
    assert "synthesis" in repository.stages
    assert repository.kinds() == ["run_started", "lead_step", "run_finished"]


async def test_its_answer_fits_the_shape_the_task_page_reads() -> None:
    state = await run(
        Model(said("Rain at 15:00.\n\nTake an umbrella.\n\n- KNMI says so")), Repository()
    )

    Synthesis.model_validate(state["synthesis"])
    body = state["synthesis"]["deliverable_body"]
    assert state["synthesis"]["lead"] == "Rain at 15:00."
    assert body[0]["points"] == ["Take an umbrella.", "- KNMI says so"]


async def test_a_task_another_worker_holds_is_not_run() -> None:
    from app.orchestrator.dispatcher import RunNotClaimed

    with pytest.raises(RunNotClaimed):
        await run(Model(said("x")), Repository(claimed=False))


async def test_an_owner_question_pauses_the_task_with_the_question() -> None:
    asked = PolicyAsk(Decision(Verdict.ASK, "ask first", "crm"), 7)

    async def ask(arguments: dict[str, Any]) -> Any:
        raise asked

    from app.tools.pipeline import ToolSpec

    calling = Completion(
        model_alias="m",
        model="m",
        content="",
        usage=Usage(),
        latency_seconds=0.0,
        tool_calls=[ToolCall(id="c1", name="crm", arguments={}, raw_arguments="{}")],
    )
    repository = Repository()

    state = await execute_lead_run(
        DISPATCH,
        repository,
        team(Model(calling)),
        "Q",
        CONTEXT,
        work=[ToolSpec("crm", "The CRM.", ask)],
        worker_id="w1",
    )

    assert state["status"] == RunStatus.PAUSED
    assert repository.stopped == RunStatus.PAUSED
    [(kind, payload)] = [e for e in repository.events if e[0] == "policy_ask"]
    assert payload["record_id"] == 7


async def test_a_failed_provider_fails_the_task_with_its_code() -> None:
    repository = Repository()

    with pytest.raises(RunFailure):
        await run(Model(GatewayError("provider_unavailable")), repository)

    assert repository.failed == ("lead", "provider_unavailable")
    assert "run_failed" in repository.kinds()


async def test_a_proposed_team_run_is_recorded_for_the_owner() -> None:
    repository = Repository()

    await run(Model(proposing(), said("A first view; a team run is proposed.")), repository)

    [(kind, payload)] = [e for e in repository.events if e[0] == "team_run_proposed"]
    assert payload == {"objective": "Full e-bike study", "why": "Many sources"}


async def test_a_limit_finishes_with_the_best_answer_and_says_so() -> None:
    state = await run(Model(said_then_limit := GatewayError("budget_exhausted:cost")), Repository())

    assert said_then_limit.code == "budget_exhausted:cost"
    assert state["status"] == RunStatus.COMPLETED
    assert "budget_exhausted:cost" in state["synthesis"]["not_checked"]


def test_unresolved_citations_and_open_steps_are_told_not_hidden() -> None:
    reply = LeadReply(
        text="Rain at 15:00 (https://made-up.example).",
        stopped="answered",
        reason="",
        calls=2,
        citations=CitationCheck(["https://made-up.example"], ["https://made-up.example"]),
        todo=[],
        results=[
            StepResult(summary="s", findings=["KNMI: rain 15:00"], sources=["https://knmi.nl"])
        ],
        proposals=[],
    )

    synthesis = synthesis_of(reply)

    assert "https://made-up.example" in synthesis["not_checked"]
    assert synthesis["facts"] == ["KNMI: rain 15:00"]


async def test_a_long_answer_is_split_into_blocks_the_page_accepts() -> None:
    text = "\n\n".join(f"Paragraph {n}." for n in range(15))

    state = await run(Model(said(text)), Repository())

    Synthesis.model_validate(state["synthesis"])
    assert [len(b["points"]) for b in state["synthesis"]["deliverable_body"]] == [6, 6, 2]


async def test_a_note_the_owner_sends_while_it_works_reaches_the_lead() -> None:
    repository = Repository()
    repository.notes = ["Skip retail."]
    seen: list[list[dict[str, Any]]] = []

    class Seeing(Model):
        async def complete(
            self, model_alias: str, messages: list[dict[str, Any]], **options: Any
        ) -> Any:
            seen.append(list(messages))
            return await super().complete(model_alias, messages, **options)

    await run(Seeing(said("Done, retail skipped.")), repository)

    assert seen[0][-1]["content"] == "The owner, while you work: Skip retail."


async def test_a_cancel_stops_the_lead_task_and_marks_it_cancelled() -> None:
    repository = Repository()
    repository.stop_kind = "cancel"

    state = await run(Model(said("never")), repository)

    assert state["status"] == RunStatus.CANCELLED
    assert repository.stopped == RunStatus.CANCELLED and repository.applied


async def test_a_resumed_task_is_told_what_it_already_found() -> None:
    model = Model(said("Rain at noon, https://knmi.nl/rain says so."))
    repository = Repository()
    repository.kept = [
        {
            "brief": "KNMI forecast",
            "status": "done",
            "summary": "Rain at noon",
            "findings": ["Rain at noon"],
            "sources": ["https://knmi.nl/rain"],
        },
        {
            "brief": "Never finished",
            "status": "failed",
            "summary": "",
            "findings": [],
            "sources": [],
        },
    ]
    repository.state = {"lead_todo": [{"text": "Check KNMI", "status": "done"}]}

    state = await execute_lead_run(
        DISPATCH, repository, team(model), "Rain?", CONTEXT, work=[], worker_id="w1", resume=True
    )

    told = str(model.seen[0])
    assert "Rain at noon" in told and "Check KNMI" in told
    assert "Never finished" not in told
    # What an earlier stretch of the task really returned may be cited.
    assert state["lead"]["unresolved"] == []


async def test_a_first_start_reads_nothing_back() -> None:
    model = Model(said("Rain."))
    repository = Repository()
    repository.kept = [{"brief": "Old", "status": "done", "summary": "Old find", "sources": []}]

    await run(model, repository)

    assert "Old find" not in str(model.seen[0])

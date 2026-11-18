import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.orchestrator.checkpoints import thread_config
from app.orchestrator.graph import ANALYSIS_WORKFLOW, AgentSet, RunFailure, RunStopped, run_analysis
from app.orchestrator.pipeline import build_graph
from app.orchestrator.runner import ARTIFACT_OF_STAGE, Dispatch, RunRepository
from app.orchestrator.state import AnalysisState, Node, RunStatus, initial_state
from app.policies.store import PolicyAsk

logger = logging.getLogger("quarterdeck.dispatcher")
# A ceiling reached, not work that failed. Carries the kind after the colon.
BUDGET_CODE = "budget_exhausted"
# What a running task shows of itself before it ends.
LIVE_STATE = (
    "plan",
    "step_status",
    "step_skills",
    "replans",
    "waves",
    "revision_count",
    "advice",
    # The room's huddles and what came of its challenges (T), shown while it runs.
    "huddles",
    "challenges",
)
# How often a running task renews its worker's lease (the lease is 180s).
HEARTBEAT_SECONDS = 60

RUN_ENDING = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.WAITING_FOR_APPROVAL,
        RunStatus.FAILED,
        RunStatus.REJECTED,
        RunStatus.BLOCKED,
        RunStatus.NEEDS_HUMAN_REVIEW,
    }
)


class RunNotClaimed(Exception):
    """Another worker owns this run, or it is not eligible to start."""


class RecordedBoard:
    """The task's room, kept on the run's record as it is written (T)."""

    def __init__(self, repository: Any, run_id: UUID) -> None:
        self.repository = repository
        self.run_id = run_id

    def post(self, entry: dict[str, Any]) -> None:
        self.repository.add_team_post(self.run_id, entry)

    def read(self) -> list[dict[str, Any]]:
        return list(self.repository.team_posts(self.run_id))

    def huddled(self, wave: int) -> None:
        """A huddle has ended: the project room says it in one line."""
        self.repository.add_event(self.run_id, "huddle_held", {"wave": wave})


class RecordedAdvice:
    """The advisor's consultations, kept on the run's record as they are made."""

    def __init__(self, repository: Any, run_id: UUID) -> None:
        self.repository = repository
        self.run_id = run_id

    def recall(self) -> list[dict[str, Any]]:
        kept = self.repository.live_state(self.run_id).get("advice")
        return [entry for entry in kept or [] if isinstance(entry, dict)]

    def keep(self, entries: list[dict[str, Any]]) -> None:
        self.repository.merge_state(self.run_id, {"advice": entries})


class StepStuck(Exception):
    """A step cannot get on and the owner is asked what to do."""

    def __init__(self, details: dict[str, Any]) -> None:
        super().__init__(str(details.get("what") or "stuck"))
        self.details = details


# Of the browser's latest moves, this many failing reads as going in circles.
LOOP_WINDOW = 10
LOOP_FAILURES = 5


def looping(repository: Any, run_id: UUID) -> dict[str, Any] | None:
    """What the browser kept failing at lately, if it was going in circles."""
    reader = getattr(repository, "recent_events", None)
    if reader is None:
        return None
    steps = reader(run_id, "browser_step", LOOP_WINDOW)
    failed = [s for s in steps if "(failed:" in str(s.get("said") or "")]
    if len(failed) < LOOP_FAILURES:
        return None
    labels = [str(s.get("label") or s.get("said") or "") for s in failed]
    what = max(set(labels), key=labels.count)
    said = str(failed[0].get("said") or "")
    return {
        "what": what,
        "tries": labels.count(what),
        "error": said.partition("(failed:")[2].strip(" )"),
        "url": str(failed[0].get("url") or ""),
    }


async def execute_run(
    dispatch: Dispatch,
    repository: RunRepository,
    agents: AgentSet,
    checkpointer: BaseCheckpointSaver[Any],
    max_revisions: int,
    objective: str,
    context: dict[str, Any],
    resume: bool = False,
    gate: Any = None,
    executor: Any = None,
    state_override: dict[str, Any] | None = None,
    resume_node: str | None = None,
    judge: Any = None,
    max_parallel: int = 4,
    workflow: str = ANALYSIS_WORKFLOW,
    worker_id: str = "",
    before_step: Callable[[str], None] | None = None,
    team: bool = False,
) -> AnalysisState:
    """Claim, execute, and durably record one analysis run.

    The claim precedes execution, so a duplicate dispatch of the same run does
    no work. On resume the graph is invoked with no input, continuing from its
    checkpoint rather than restarting the chain.
    """
    if not repository.claim(dispatch.run_id, worker_id):
        raise RunNotClaimed(str(dispatch.run_id))
    run_id = dispatch.run_id
    repository.add_event(run_id, "run_resumed" if resume else "run_started")
    graph = build_graph(
        agents,
        max_revisions,
        checkpointer=checkpointer,
        gate=gate,
        executor=executor,
        judge=judge,
        guidance=_guidance(repository, run_id, worker_id, before_step),
        max_parallel=max_parallel,
        workflow=workflow,
        advice_memory=RecordedAdvice(repository, run_id),
        board=RecordedBoard(repository, run_id) if hasattr(repository, "add_team_post") else None,
        team=team,
        late_notes=lambda node: repository.take_new_instructions(run_id, node),
        skipped=lambda: repository.skipped_steps(run_id),
        prioritized=lambda: repository.prioritized_steps(run_id),
    )
    config = thread_config(str(dispatch.run_id))
    start: AnalysisState | None = None if resume else initial_state(objective, context)
    if resume and await checkpointer.aget_tuple(config) is None:  # type: ignore[arg-type]
        # Claimed, then interrupted before the first step saved anything: there
        # is nothing to resume from, so it starts rather than failing.
        logger.info('{"event":"run_restarted_without_checkpoint","run":"%s"}', run_id)
        start = initial_state(objective, context)
        resume = False

    if state_override:
        # A human decision has to reach the checkpoint, which is authoritative
        # for continuation; writing it only to the domain row would be ignored.
        # Applying it as the critique node reuses the existing revision edges.
        await graph.aupdate_state(
            config,  # type: ignore[arg-type]
            state_override,
            as_node=resume_node or str(Node.CRITIQUE),
        )

    async def heartbeat() -> None:
        # A step can outlast the lease: a round of research ran past it on the
        # live office, and recovery took the task from a worker still running
        # it. So the hold is renewed while the task runs, not only when a step
        # starts.
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            repository.renew_lease(run_id, worker_id)

    beating = asyncio.create_task(heartbeat()) if worker_id else None
    try:
        state = await run_analysis(graph, start, config, on_node=_persister(repository, dispatch))  # type: ignore[arg-type]
    except RunFailure as failure:
        if failure.code.startswith(BUDGET_CODE):
            # Reaching a ceiling is not a failure of the work: everything the
            # run produced so far is intact and the step is replayable from the
            # checkpoint. It pauses and asks, so the owner can raise the ceiling
            # and continue instead of losing what has been paid for.
            repository.stop(run_id, RunStatus.PAUSED)
            repository.add_event(run_id, "budget_ask", _budget_ask(repository, run_id, failure))
            return await _where_it_stands(graph, config, RunStatus.PAUSED)
        repository.fail(run_id, failure.node, failure.code)
        repository.add_event(run_id, "run_failed", {"node": failure.node, "code": failure.code})
        raise
    except PolicyAsk as ask:
        # A rule wants the owner's word. The task pauses where it stands, with
        # the question recorded, and continues once the owner has answered.
        repository.stop(run_id, RunStatus.PAUSED)
        repository.add_event(
            run_id,
            "policy_ask",
            {
                "policy": ask.decision.policy,
                "reason": ask.decision.reason,
                "record_id": ask.record_id,
            },
        )
        return await _where_it_stands(graph, config, RunStatus.PAUSED)
    except StepStuck as stuck:
        repository.stop(run_id, RunStatus.PAUSED)
        repository.add_event(run_id, "stuck_ask", stuck.details)
        return await _where_it_stands(graph, config, RunStatus.PAUSED)
    except RunStopped as stopped:
        status = RunStatus.CANCELLED if stopped.kind == "cancel" else RunStatus.PAUSED
        repository.apply_stop_controls(run_id)
        repository.stop(run_id, status)
        return await _where_it_stands(graph, config, status)
    finally:
        if beating is not None:
            beating.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beating

    repository.finish(run_id, state)
    repository.expire_stop_controls(run_id)
    repository.add_event(run_id, "run_finished", {"status": str(state.get("status"))})
    return state


def _guidance(
    repository: RunRepository,
    run_id: UUID,
    worker_id: str,
    before_step: Callable[[str], None] | None,
) -> Callable[[str], list[str]]:
    """What runs before each step: the hold renewed, a last change of model,
    and the owner's instructions for it."""

    def guidance(node: str) -> list[str]:
        # Each step renews this worker's hold, so a worker that dies mid-run
        # frees the run for another within one lease.
        if worker_id:
            repository.renew_lease(run_id, worker_id)
        if before_step is not None:
            # A chance to change what the next step runs on, such as moving to
            # a cheaper model once the task has spent enough.
            before_step(node)
        repository.add_event(run_id, "step_started", {"node": node})
        return repository.receive_instructions(run_id, node)

    return guidance


def _persister(
    repository: RunRepository, dispatch: Dispatch
) -> Callable[[str, dict[str, Any]], None]:
    """What each finished step leaves on the record, and where a pause, a stop
    or a stuck step takes effect: at the step boundary."""
    run_id = dispatch.run_id

    def persist(node: str, values: dict[str, Any]) -> None:
        for stage in ARTIFACT_OF_STAGE:
            payload = values.get(stage)
            if isinstance(payload, dict):
                repository.record_stage(dispatch, stage, payload)
        for index, subtask in enumerate(values.get("research_subtasks") or [], start=1):
            repository.record_subtask(dispatch, index, subtask["question"], subtask["report"])
        repository.note_progress(run_id, node)
        # The plan and where its steps stand, on the record as they change, so
        # progress is visible while the task runs and not only when it ends.
        live = {key: values[key] for key in LIVE_STATE if key in values}
        if live:
            repository.merge_state(run_id, live)
        repository.apply_instructions(run_id, node)
        repository.add_event(run_id, "step_completed", {"node": node})
        # A step boundary: the completed step is checkpointed and no further
        # step has started, so this is where a pause or cancel takes effect.
        # A step that ends the run (approval pause, completion, stop) leaves
        # nothing to pause; the request expires in finish below.
        if values.get("status") in RUN_ENDING:
            return
        if values.get("stuck"):
            # A step cannot get on: the owner is asked, with the wave kept.
            raise StepStuck(dict(values["stuck"]))
        stop = repository.pending_stop(run_id)
        if stop is not None:
            raise RunStopped(str(stop["kind"]))

    return persist


def _budget_ask(repository: RunRepository, run_id: UUID, failure: RunFailure) -> dict[str, Any]:
    """What the owner is asked when a ceiling is reached: which, and what was spent."""
    ask: dict[str, Any] = {
        "limit": failure.code.partition(":")[2] or "tokens",
        "node": failure.node,
        "budget": repository.budget(run_id),
        "spent": repository.usage(run_id)["spent"],
    }
    circling = looping(repository, run_id)
    if circling is not None:
        # Spent going in circles: more budget alone would buy more of
        # the same, so the owner is told what kept failing.
        ask["looping"] = circling
    return ask


async def _where_it_stands(graph: Any, config: Any, status: Any) -> AnalysisState:
    """The run's state from its checkpoint, with the status it stopped at."""
    snapshot = await graph.aget_state(config)
    return cast(AnalysisState, {**dict(snapshot.values), "status": status})


def recover_runs(repository: RunRepository) -> list[UUID]:
    """Reclaim runs no live worker holds any more.

    Analysis nodes are replayable from the checkpoint, so an interrupted run is
    eligible to resume. A run whose worker is still renewing its lease is left
    alone, so recovery can run repeatedly and beside other workers. A run whose
    approval was decided resumes at the approval node and so can reach
    engineering again; the execution ledger keeps that to one execution per
    approval, and an interrupted one waits for the owner.
    """
    recovered = repository.mark_for_recovery()
    if recovered:
        logger.info('{"event":"runs_marked_for_recovery","count":%d}', len(recovered))
    return recovered

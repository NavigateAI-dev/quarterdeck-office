"""Work nodes: the step waves, and the legacy research/strategy path."""

import asyncio
import copy
import json
from typing import Any, cast

from app.agents.base import AgentOutputError
from app.orchestrator.graph import (
    DONE,
    EFFORT_PAGES,
    EMPTY,
    FAILED,
    GATHERS,
    MAX_HUDDLES,
    MAX_REPLANS,
    MAX_STALLS,
    PENDING,
    STOP_NODE,
    _invoke,
    asked_of,
    context_of,
    digest_of,
    drafted,
    evidence_gap,
    found_nothing,
    logger,
    merge_reports,
    plan_of,
    providers_down,
    retry_focus,
    short,
    skill_for,
    unanswered,
    upstream,
    with_skill,
)
from app.orchestrator.nodes.context import (
    NodeContext,
    evidenced,
    late_for,
    owner,
    ready,
    say,
    with_gaps,
)
from app.orchestrator.nodes.team import huddle, settle
from app.orchestrator.routing import NO_PROGRESS, PROVIDERS_DOWN, stalled
from app.orchestrator.state import AnalysisState, Node
from app.schemas import PlanStep, TaskPlan


async def work(ctx: NodeContext, state: AnalysisState) -> dict[str, Any]:
    """One wave: every ready step at once, as one graph step, so a checkpoint,
    the budget, a pause and the owner's direction all apply between waves."""
    contract = plan_of(state)
    status = _skip_asked(ctx, contract, dict(state.get("step_status") or {}))
    state = cast(AnalysisState, {**state, "step_status": status})
    writing_with_gaps = not ready(ctx, state)
    wave = _next_wave(ctx, state)
    number = int(state.get("waves") or 0) + 1
    steps = _WaveSteps(ctx, state, contract, number, writing_with_gaps)
    outcomes = _Outcomes(ctx, status, steps.results, number, state.get("challenges") or [])
    for step, produced, worker, error in await asyncio.gather(*(steps.run(s) for s in wave)):
        outcomes.take(step, produced, worker, error)
    _share_researcher_memory(ctx, outcomes.workers)
    held, settled = await _after_wave(ctx, state, contract, wave, outcomes, number)
    update: dict[str, Any] = {
        **settled,
        "challenges": outcomes.challenges,
        "huddles": held,
        "step_status": status,
        "step_results": steps.results,
        "step_skills": steps.applied,
        "waves": int(state.get("waves") or 0) + 1,
        "stalls": 0 if outcomes.done_now else int(state.get("stalls") or 0) + 1,
        "gaps_written": bool(state.get("gaps_written")) or (writing_with_gaps and bool(wave)),
        "stuck": outcomes.stuck_on,
    }
    update.update(await _what_the_wave_made(ctx, contract, wave, status, steps.results))
    outage = providers_down(contract, wave, status, outcomes.errors)
    if outage:
        # Nothing was researched and the providers are why: replanning or
        # reviewing would spend again on what cannot run now.
        update["stopped_reason"] = PROVIDERS_DOWN
        update["provider_failure"] = {"codes": outage, "steps": len(outcomes.errors)}
    return update


def _skip_asked(ctx: NodeContext, contract: TaskPlan, status: dict[str, str]) -> dict[str, str]:
    """Steps the owner chose to skip are left out, said so in the room."""
    by_id = {step.id: step for step in contract.steps}
    for step_id in ctx.skipped() if ctx.skipped is not None else set():
        if step_id in by_id and status.get(step_id, PENDING) in (PENDING, FAILED):
            status[step_id] = EMPTY
            say(
                ctx,
                "doubt",
                "researcher" if by_id[step_id].kind in GATHERS else "writer",
                f"Skipped, as the owner asked: {short(by_id[step_id].brief)}.",
                step_id=step_id,
                phase="work",
            )
    return status


def _next_wave(ctx: NodeContext, state: AnalysisState) -> list[PlanStep]:
    """The steps to run now: the ready ones, the owner's "Do next" first (13d),
    the rest in the plan's order, as many as may run at once."""
    candidates = ready(ctx, state) or with_gaps(ctx, state)
    asked = ctx.prioritized() if ctx.prioritized is not None else []
    rank = {step_id: i for i, step_id in enumerate(asked)}
    candidates = sorted(candidates, key=lambda step: rank.get(step.id, len(rank)))
    return candidates[: max(1, ctx.max_parallel)]


class _WaveSteps:
    """Runs one step of the wave: the browser, a researcher or a writer."""

    def __init__(
        self,
        ctx: NodeContext,
        state: AnalysisState,
        contract: TaskPlan,
        number: int,
        writing_with_gaps: bool,
    ) -> None:
        self.ctx = ctx
        self.state = state
        self.contract = contract
        self.number = number
        self.writing_with_gaps = writing_with_gaps
        self.by_id = {step.id: step for step in contract.steps}
        self.results: dict[str, Any] = dict(state.get("step_results") or {})
        self.applied: dict[str, Any] = dict(state.get("step_skills") or {})
        # A step run again answers the challenge that sent it back.
        self.answering = {
            str(c.get("to_step")): str(c.get("key"))
            for c in state.get("challenges") or []
            if c.get("outcome") == "rerun" and c.get("key")
        }
        self.base = upstream(state, "plan", "critique") + f"\n\nThe task: {asked_of(state)}"
        try:
            ctx.agents.researcher.official_urls = list(contract.official_sources)  # type: ignore[attr-defined]
        except AttributeError:
            pass

    def given(self, step: PlanStep) -> str:
        """The task, and what the steps it depends on found (or could not)."""
        found = [
            f"Step {needed} ({self.by_id[needed].brief}) found:\n"
            + json.dumps(self.results[needed], indent=2, ensure_ascii=False)
            for needed in step.depends_on
            if needed in self.results
        ]
        missing = [
            f"- {needed}: {self.by_id[needed].brief}"
            for needed in step.depends_on
            if needed not in self.results and needed in self.by_id
        ]
        if self.writing_with_gaps and missing:
            found.append(
                "These steps found nothing usable, and research is over:\n"
                + "\n".join(missing)
                + "\nAnswer the task with what is known, from the steps above and "
                "general knowledge marked as such, and say plainly what could not "
                "be confirmed and how the reader can check it."
            )
        return self.base + "".join(f"\n\n{part}" for part in found)

    async def run(self, step: PlanStep) -> tuple[PlanStep, dict[str, Any] | None, Any, str]:
        ctx = self.ctx
        say(
            ctx,
            "claim",
            "researcher" if step.kind in GATHERS else "writer",
            f"Taking: {short(step.brief)}",
            step_id=step.id,
            phase="work",
            wave=self.number,
            reply_to=self.answering.get(step.id),
        )
        outcome, skill = skill_for(step, ctx.agents.skills)
        if outcome:
            self.applied[step.id] = outcome
        try:
            browser = getattr(ctx.agents, "browser", None)
            if step.kind == "browse" and browser is not None:
                return await self._browse(step, browser)
            if step.kind in GATHERS:
                return await self._research(step, skill)
            return await self._write(step, skill)
        except AgentOutputError as failed:
            # A ceiling reached stops the task and asks, as it would anywhere.
            if str(failed.code).startswith("budget_exhausted"):
                raise
            # Any other failure costs the step; the progress check replans.
            return step, None, None, str(failed.code)

    async def _browse(
        self, step: PlanStep, browser: Any
    ) -> tuple[PlanStep, dict[str, Any] | None, Any, str]:
        # The office's own browser; with it off, the step is read as
        # research instead. The owner's notes go with it, and new ones reach
        # it between moves.
        report = await browser.browse(
            step.brief,
            context_of(self.state),
            step.effort,
            notes=owner(self.ctx, "research").strip(),
            step_id=step.id,
        )
        return step, report, None, "stuck" if report.get("stuck") else ""

    async def _research(
        self, step: PlanStep, skill: Any
    ) -> tuple[PlanStep, dict[str, Any] | None, Any, str]:
        worker = copy.copy(self.ctx.agents.researcher)
        if skill is not None:
            worker = with_skill(worker, skill)
        worker.late_inputs = late_for(self.ctx, step.id, "research")
        pages = getattr(worker, "max_pages", None)
        if isinstance(pages, int):
            worker.max_pages = EFFORT_PAGES[step.effort](pages)  # type: ignore[attr-defined]
        report = await _invoke(
            worker,
            step.brief,
            self.state,
            self.given(step) + owner(self.ctx, "research"),
            focus=evidence_gap(self.state),
        )
        return step, report, worker, ""

    async def _write(
        self, step: PlanStep, skill: Any
    ) -> tuple[PlanStep, dict[str, Any] | None, Any, str]:
        writer = copy.copy(self.ctx.agents.deliverable_for(self.contract.task_kind))
        if skill is not None:
            writer = with_skill(writer, skill)
        writer.late_inputs = late_for(self.ctx, step.id, "strategy")
        if hasattr(writer, "allowed_citations"):
            writer.allowed_citations = {
                str(source.get("url"))
                for needed in step.depends_on
                for source in (self.results.get(needed) or {}).get("sources") or []
            }
        written = await _invoke(
            writer, step.brief, self.state, self.given(step) + owner(self.ctx, "strategy")
        )
        return step, written, None, ""


class _Outcomes:
    """What each step of the wave came to: its status, said in the room."""

    def __init__(
        self,
        ctx: NodeContext,
        status: dict[str, str],
        results: dict[str, Any],
        number: int,
        challenges: list[Any],
    ) -> None:
        self.ctx = ctx
        self.status = status
        self.results = results
        self.number = number
        self.challenges = [dict(c) for c in challenges]
        self.skipped = ctx.skipped() if ctx.skipped is not None else set()
        self.done_now = 0
        self.workers: list[Any] = []
        self.errors: dict[str, str] = {}
        self.stuck_on: dict[str, Any] | None = None

    def _say(self, kind: str, role: str, text: str, step: PlanStep) -> None:
        say(self.ctx, kind, role, text, step_id=step.id, phase="work", wave=self.number)

    def take(
        self, step: PlanStep, produced: dict[str, Any] | None, worker: Any, error: str
    ) -> None:
        role = "researcher" if step.kind in GATHERS else "writer"
        # A step that ran with a challenge to it in hand has answered it,
        # if it finished; otherwise the challenge stands as a limit.
        for challenge in self.challenges:
            if challenge.get("to_step") == step.id and challenge.get("outcome") in (
                "rerun",
                "pending",
            ):
                challenge["outcome"] = "answered" if not error and produced is not None else "limit"
        if error == "stuck" and produced is not None and step.id not in self.skipped:
            # The browser cannot get on: the step waits, and the owner is
            # asked for a hint, a skip or a finish.
            self.status[step.id] = PENDING
            self.stuck_on = {
                **dict(produced.get("stuck") or {}),
                "step": step.id,
                "brief": step.brief,
            }
            what = self.stuck_on.get("what", "the page does not respond")
            self._say("doubt", role, f"Stuck: {what}. Asking the owner.", step)
            return
        if produced is not None:
            produced.pop("stuck", None)
        if step.id in self.skipped and (error or produced is None):
            self.status[step.id] = EMPTY
            return
        if error or produced is None:
            self.status[step.id] = FAILED
            self.errors[step.id] = error
            reason = error or "no output"
            self._say("doubt", role, f"Could not finish {short(step.brief)} ({reason}).", step)
            return
        self.results[step.id] = produced
        if step.kind in GATHERS and found_nothing(produced):
            self.status[step.id] = EMPTY
            self._say("doubt", role, f"Found nothing that answers: {short(step.brief)}.", step)
        else:
            self.status[step.id] = DONE
            self.done_now += 1
            self._say_done(step, role, produced)
        if worker is not None:
            self.workers.append(worker)

    def _say_done(self, step: PlanStep, role: str, produced: dict[str, Any]) -> None:
        if step.kind not in GATHERS:
            self._say("draft", role, drafted(produced), step)
            return
        findings = [str(f) for f in produced.get("key_findings") or []][:3]
        sources = len(produced.get("sources") or [])
        said = "; ".join(findings) + f" ({sources} source{'s' if sources != 1 else ''})"
        self._say("finding", role, said, step)


def _share_researcher_memory(ctx: NodeContext, workers: list[Any]) -> None:
    """What each step's researcher read and found, kept on the shared one."""
    for attribute in ("last_local", "last_evidence"):
        combined = dict(getattr(ctx.agents.researcher, attribute, {}) or {})
        for worker in workers:
            combined.update(getattr(worker, attribute, {}) or {})
        try:
            setattr(ctx.agents.researcher, attribute, combined)
        except AttributeError:
            pass


async def _after_wave(
    ctx: NodeContext,
    state: AnalysisState,
    contract: TaskPlan,
    wave: list[PlanStep],
    outcomes: _Outcomes,
    number: int,
) -> tuple[int, dict[str, Any]]:
    """The team's huddle and settling, when the office works as a team."""
    held = int(state.get("huddles") or 0)
    status = outcomes.status
    if (
        ctx.team
        and ctx.board is not None
        and held < MAX_HUDDLES
        and any(value == PENDING for value in status.values())
        # A wave that found nothing gives the team nothing to react to;
        # reacting anyway asked for more of what had just failed.
        and outcomes.done_now > 0
    ):
        await huddle(ctx, state, wave, status, outcomes.results, number)
        held += 1
    settled: dict[str, Any] = {}
    if ctx.team and ctx.board is not None:
        settled = settle(ctx, state, contract, status, outcomes.challenges, number)
        if held > int(state.get("huddles") or 0):
            # The project room says this huddle in one line.
            mark = getattr(ctx.board, "huddled", None)
            if callable(mark):
                try:
                    mark(number)
                except Exception:  # noqa: BLE001 - a missed line is not a failed task
                    logger.warning('{"event":"huddle_mark_failed"}')
    return held, settled


async def _what_the_wave_made(
    ctx: NodeContext,
    contract: TaskPlan,
    wave: list[PlanStep],
    status: dict[str, str],
    results: dict[str, Any],
) -> dict[str, Any]:
    """The research so far, merged and checked, and the latest draft."""
    update: dict[str, Any] = {}
    researched = [
        (step.brief, results[step.id])
        for step in contract.steps
        if step.kind in GATHERS and step.id in results
    ]
    if researched and any(step.kind in GATHERS for step in wave):
        report = (
            researched[0][1]
            if len(researched) == 1
            else merge_reports(contract.research_question, researched)
        )
        update.update(await evidenced(ctx, report))
    written = [
        results[step.id]
        for step in contract.steps
        if step.kind == "write" and status.get(step.id) == DONE
    ]
    if written and any(step.kind == "write" for step in wave):
        update["strategy"] = written[-1]
    return update


def after_work(ctx: NodeContext, state: AnalysisState) -> str:
    """The progress check: another wave, a replan, or review."""
    if stalled(state):
        return STOP_NODE
    # Written with its gaps, the research is over: no replan undoes that.
    stuck = int(state.get("stalls") or 0) >= MAX_STALLS or bool(state.get("gaps_written"))
    if ready(ctx, state) and not stuck:
        return Node.WORK
    troubled = [s for s in (state.get("step_status") or {}).values() if s in (EMPTY, FAILED)]
    # Once the answer is written, a step that found nothing earlier is a gap
    # the review sees, not a reason to research and write again.
    written = any(
        step.kind == "write" and (state.get("step_status") or {}).get(step.id) == DONE
        for step in plan_of(state).steps
    )
    if troubled and int(state.get("replans") or 0) < MAX_REPLANS and not stuck and not written:
        return Node.REPLAN
    if with_gaps(ctx, state):
        return Node.WORK
    return Node.CRITIQUE


async def research(ctx: NodeContext, state: AnalysisState) -> dict[str, Any]:
    plan_contract = TaskPlan.model_validate(state["plan"])
    inputs = upstream(state, "plan", "critique") + owner(ctx, "research")
    questions = [q for q in plan_contract.subquestions if q.strip()]
    # Copies made for subtasks inherit the plan's official pages.
    try:
        ctx.agents.researcher.official_urls = list(plan_contract.official_sources)  # type: ignore[attr-defined]
    except AttributeError:
        pass
    update: dict[str, Any] = {"research_subtasks": []}
    if len(questions) >= 2:
        limit = asyncio.Semaphore(max(1, ctx.max_parallel))

        async def one(question: str, focus: str = "") -> tuple[str, dict[str, Any], Any]:
            # Each subtask gets its own copy so per-call provenance cannot race.
            worker = copy.copy(ctx.agents.researcher)
            async with limit:
                report = await _invoke(
                    worker, question, state, inputs, focus=focus or evidence_gap(state)
                )
            return question, report, worker

        results = await asyncio.gather(*(one(q) for q in questions))
        # A question the evidence did not answer is a query, not a result.
        # One more attempt, told what was missing, before the critic sees
        # it: the alternative is a report that lists the owner's own
        # questions as unknowns.
        retried = await asyncio.gather(
            *(
                one(question, focus=retry_focus(report))
                for question, report, _ in results
                if unanswered(report)
            )
        )
        if retried:
            better = {row[0]: row for row in retried}
            results = [better.get(row[0], row) for row in results]
        update["research"] = merge_reports(
            plan_contract.research_question, [(q, r) for q, r, _ in results]
        )
        update["research_subtasks"] = [{"question": q, "report": r} for q, r, _ in results]
        for attribute in ("last_local", "last_evidence"):
            combined: dict[str, Any] = {}
            for _, _, worker in results:
                combined.update(getattr(worker, attribute, {}) or {})
            try:
                setattr(ctx.agents.researcher, attribute, combined)
            except AttributeError:
                pass
    else:
        update["research"] = await _invoke(
            ctx.agents.researcher,
            plan_contract.research_question,
            state,
            inputs,
            focus=evidence_gap(state),
        )
    update.update(await evidenced(ctx, update["research"]))
    if state.get("critique") is not None:
        update.update(
            strategy=None, critique=None, revision_count=state.get("revision_count", 0) + 1
        )
        # A rerun that produced the same report cannot improve anything
        # downstream, so spending the rest of the budget is waste.
        if digest_of(cast(dict[str, Any], dict(state).get("research"))) == digest_of(
            update["research"]
        ):
            update["stopped_reason"] = NO_PROGRESS
    return update


async def strategy(ctx: NodeContext, state: AnalysisState) -> dict[str, Any]:
    contract = TaskPlan.model_validate(state["plan"])
    deliverable = ctx.agents.deliverable_for(contract.task_kind)
    research_sources = cast(dict[str, Any], dict(state).get("research") or {}).get("sources")
    if hasattr(deliverable, "allowed_citations"):
        deliverable.allowed_citations = {
            str(source.get("url")) for source in research_sources or []
        }
    update: dict[str, Any] = {
        "strategy": await _invoke(
            deliverable,
            asked_of(state),
            state,
            upstream(state, "plan", "research", "critique") + owner(ctx, "strategy"),
        )
    }
    if state.get("critique") is not None:
        update.update(critique=None, revision_count=state.get("revision_count", 0) + 1)
        if digest_of(cast(dict[str, Any], dict(state).get("strategy"))) == digest_of(
            update["strategy"]
        ):
            update["stopped_reason"] = NO_PROGRESS
    return update

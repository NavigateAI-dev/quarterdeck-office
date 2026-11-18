"""Before and after: planning, review and writing on real finished tasks.

For each task it re-runs only the parts that changed, on the research the task
already gathered, with the models the office uses for each role:

- the plan: the new planner, with its review loop, against the first plan the
  task made;
- the critique: the new critic, on the same draft, against the task's own;
- the answer: the new Chief, with the same material, against the task's own.

A judge then compares each pair blind (the order is shuffled) for how useful
and readable it is to the task's reader. Plain measures are taken too: steps,
issues, length, office jargon.

Read only: it reads the tasks and writes nothing to the office. It stops if
the models' reported cost passes MAX_COST_USD.

    python -m scripts.quality_eval [run-id-prefix ...]
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import select

from app.agent_routing import apply_role_models
from app.agents.base import BaseAgent
from app.agents.chief import ChiefAgent
from app.agents.critic import CriticAgent
from app.agents.plan_reviewer import PlanReviewerAgent
from app.agents.planner import PlannerAgent
from app.artifacts import ArtifactStore
from app.config import get_settings
from app.connection_completer import completer_for_entry
from app.context import ProjectContext
from app.db.models import Artifact, Run, Task
from app.db.session import make_engine, make_session_factory
from app.events import InvocationRecorder
from app.office_models import office_models
from app.office_settings import role_models
from app.orchestrator.graph import AgentSet, render
from app.schemas import TaskPlan
from app.schemas.agent_output import PlanReview, StrictModel

TASKS = ["fd3220f1", "644ba607", "babe50a5", "4cce4e63", "e7e08ed4"]
MAX_COST_USD = 5.0
# Inside the office container, where the script runs; never the owner's files.
OUT = Path("/tmp/quality_eval.json")  # noqa: S108
# Words from how the office works, which a reader should never meet.
JARGON = [
    "核验",
    "口径",
    "证据强度",
    "条件性",
    "候选",
    "验收标准",
    "检索边界",
    "一源一行",
    "verification",
    "candidate",
    "acceptance criteria",
    "evidence strength",
    "scope",
    "conditional",
]


class Verdict(StrictModel):
    """A blind comparison of two versions of the same thing."""

    useful_a: int = Field(ge=1, le=5)
    useful_b: int = Field(ge=1, le=5)
    readable_a: int = Field(ge=1, le=5)
    readable_b: int = Field(ge=1, le=5)
    on_purpose_a: int = Field(ge=1, le=5)
    on_purpose_b: int = Field(ge=1, le=5)
    better: Literal["A", "B", "same"]
    why: str = Field(min_length=1, max_length=600)


class Judge(BaseAgent[Verdict]):
    role = "chief"
    output_schema = Verdict
    instructions = """You compare two versions, A and B, of the same piece of
work for the same request. Return JSON only matching the schema. Score each
from 1 to 5: useful (could the person who asked act on it?), readable (plain,
clear, no jargon, easy to take in), on_purpose (does it serve what they asked,
rather than procedure or detail they did not need). Then say which is better
overall, or same, and why in one or two sentences. Judge the content, not the
length: longer is not better."""

    def __init__(self, gateway: Any, settings: Any) -> None:
        super().__init__(gateway, InvocationRecorder(), settings.structured_output_attempts)
        self.model_alias = settings.chief_model_alias


def spent(agents: list[Any]) -> float:
    return sum(
        float(e.estimated_cost or 0) for a in agents for e in getattr(a.recorder, "events", [])
    )


def jargon(text: str) -> int:
    lowered = text.lower()
    return sum(lowered.count(word.lower()) for word in JARGON)


def sentence_length(text: str) -> float:
    parts = [p for p in re.split(r"[.!?。！？\n]+", text) if p.strip()]
    return round(sum(len(p) for p in parts) / len(parts), 1) if parts else 0.0


def answer_text(payload: dict[str, Any] | None) -> str:
    """What the reader reads first: the lead and body, else the old answer fields."""
    if not payload:
        return ""
    parts = [str(payload.get("lead") or "")]
    for block in payload.get("deliverable_body") or []:
        parts.append(str(block.get("heading") or ""))
        parts.extend(str(p) for p in block.get("points") or [])
    parts.extend(str(s) for s in payload.get("next_steps") or [])
    parts.append(str(payload.get("not_checked") or ""))
    if not payload.get("lead"):
        parts.append(str(payload.get("recommendation") or ""))
        parts.extend(str(f) for f in payload.get("facts") or [])
        parts.extend(str(f) for f in payload.get("unknowns") or [])
    return "\n".join(p for p in parts if p.strip())


def critique_text(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    lines = [str(payload.get("for_owner") or ""), f"Verdict: {payload.get('verdict')}"]
    for issue in payload.get("issues") or []:
        lines.append(
            f"- {issue.get('issue')} ({issue.get('why_it_matters')}) Fix: {issue.get('fix')}"
        )
    for key in ("critical_issues", "required_corrections", "missing_evidence", "weak_assumptions"):
        if payload.get("issues") and key == "critical_issues":
            continue
        lines.extend(f"- {item}" for item in payload.get(key) or [])
    return "\n".join(line for line in lines if line.strip())


def plan_text(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    lines = [f"Question: {payload.get('research_question')}"]
    if payload.get("reader_brief"):
        lines.append(f"Reader: {json.dumps(payload['reader_brief'], ensure_ascii=False)}")
    lines.extend(
        f"- {s.get('id')} ({s.get('kind')}): {s.get('brief')}" for s in payload.get("steps") or []
    )
    lines.extend(f"Done when: {c}" for c in payload.get("acceptance_criteria") or [])
    return "\n".join(lines)


async def compare(
    judge: Judge, ctx: ProjectContext, ask: str, what: str, old: str, new: str
) -> dict[str, Any]:
    flipped = random.random() < 0.5  # noqa: S311 - an order, not a secret
    a, b = (new, old) if flipped else (old, new)
    verdict = await judge.run(
        ask,
        ctx,
        f"The request:\n{ask}\n\nWhat is compared: {what}\n\nVersion A:\n{a}\n\nVersion B:\n{b}",
    )
    old_key, new_key = ("b", "a") if flipped else ("a", "b")
    got = verdict.model_dump()
    better = {"A": "new" if flipped else "old", "B": "old" if flipped else "new", "same": "same"}
    return {
        "old": {k: got[f"{k}_{old_key}"] for k in ("useful", "readable", "on_purpose")},
        "new": {k: got[f"{k}_{new_key}"] for k in ("useful", "readable", "on_purpose")},
        "better": better[verdict.better],
        "why": verdict.why,
    }


async def main(prefixes: list[str]) -> None:
    settings = get_settings()
    factory = make_session_factory(make_engine(settings.database_url.get_secret_value()))
    store = ArtifactStore(settings.workspace_root)

    def latest(run_id: UUID, kind: str, first: bool = False) -> dict[str, Any] | None:
        with factory() as session:
            order = Artifact.version.asc() if first else Artifact.version.desc()
            row = session.scalars(
                select(Artifact)
                .where(Artifact.run_id == run_id, Artifact.type == kind)
                .order_by(order)
                .limit(1)
            ).first()
            if row is None:
                return None
            try:
                return dict(json.loads(store.read(row.project_id, row.id, row.version)))
            except (OSError, ValueError):
                return None

    results: list[dict[str, Any]] = []
    async with office_models(settings, factory) as gateway:
        judge = Judge(gateway, settings)
        for prefix in prefixes:
            with factory() as session:
                found = session.execute(
                    select(Run, Task.objective).join(Task, Task.id == Run.root_task_id)
                ).all()
                run, objective = next((r, o) for r, o in found if str(r.id).startswith(prefix))
                entries = role_models(session, settings)
                context = dict((run.state_json or {}).get("context") or {})
            run_id = run.id
            ctx = (
                ProjectContext.model_validate(context)
                if context
                else ProjectContext(
                    organization="Office", project_id=run.project_id, project_name="Project"
                )
            )
            planner = PlannerAgent(gateway, settings)
            reviewer = PlanReviewerAgent(gateway, settings)
            critic = CriticAgent(gateway, settings)
            chief = ChiefAgent(gateway, settings)
            team = AgentSet(
                planner=planner,
                researcher=critic,
                strategist=chief,
                critic=critic,
                chief=chief,
                plan_reviewer=reviewer,
            )
            apply_role_models(
                team, entries, lambda entry: completer_for_entry(entry, settings, factory), set()
            )
            for agent in (planner, reviewer, critic, chief):
                agent.recorder = InvocationRecorder()

            old_plan = latest(run_id, "task_plan", first=True)
            research = latest(run_id, "research_report")
            strategy = latest(run_id, "strategy_proposal")
            old_critique = latest(run_id, "critique")
            old_answer = latest(run_id, "synthesis")

            # The plan, reviewed before work, as the office now does it.
            new_plan = (await planner.run(objective, ctx, "")).model_dump(mode="json")
            reviews = 0
            for _ in range(2):
                review = PlanReview.model_validate(
                    (
                        await reviewer.run(objective, ctx, render("The draft plan", new_plan))
                    ).model_dump()
                )
                reviews += 1
                if review.verdict == "ready":
                    break
                asked = "\n".join(f"- {c.change} (why: {c.why})" for c in review.changes)
                revised = (
                    await planner.run(
                        objective,
                        ctx,
                        render("Your draft plan", new_plan)
                        + "\n\nThe plan review asks for these changes:\n"
                        + asked
                        + "\n\nRevise the plan: apply the changes that serve the reader, keep the "
                        "rest as it is, and keep it small.",
                    )
                ).model_dump(mode="json")
                if TaskPlan.model_validate(revised).steps:
                    new_plan = revised
            material = render("Plan", new_plan) + "\n\n" + render("Research", research)
            if strategy:
                material += "\n\n" + render("Strategy", strategy)
            new_critique = (await critic.run(objective, ctx, material)).model_dump(mode="json")
            new_answer = (
                await chief.run(
                    objective, ctx, material + "\n\n" + render("Critique", new_critique)
                )
            ).model_dump(mode="json")
            if spent([planner, reviewer, critic, chief, judge]) > MAX_COST_USD:
                print("stopped: the cost cap was reached")
                break

            row: dict[str, Any] = {"task": prefix, "objective": objective[:120]}
            row["plan"] = {
                "steps_old": len((old_plan or {}).get("steps") or []),
                "steps_new": len(new_plan.get("steps") or []),
                "reviews": reviews,
                **await compare(
                    judge,
                    ctx,
                    objective,
                    "the plan for the work",
                    plan_text(old_plan),
                    plan_text(new_plan),
                ),
            }
            row["critique"] = {
                "issues_old": sum(
                    len((old_critique or {}).get(k) or [])
                    for k in (
                        "critical_issues",
                        "required_corrections",
                        "missing_evidence",
                        "weak_assumptions",
                        "alternative_explanations",
                        "what_would_change_conclusion",
                    )
                ),
                "issues_new": len(new_critique.get("issues") or []),
                "chars_old": len(critique_text(old_critique)),
                "chars_new": len(critique_text(new_critique)),
                **await compare(
                    judge,
                    ctx,
                    objective,
                    "the reviewer's comments on a draft, as the person who asked would read them",
                    critique_text(old_critique),
                    critique_text(new_critique),
                ),
            }
            old_text, new_text = answer_text(old_answer), answer_text(new_answer)
            row["answer"] = {
                "jargon_old": jargon(old_text),
                "jargon_new": jargon(new_text),
                "sentence_old": sentence_length(old_text),
                "sentence_new": sentence_length(new_text),
                "chars_old": len(old_text),
                "chars_new": len(new_text),
                **await compare(
                    judge,
                    ctx,
                    objective,
                    "the answer the person who asked reads",
                    old_text,
                    new_text,
                ),
            }
            row["new_plan"], row["new_critique"], row["new_answer"] = (
                new_plan,
                new_critique,
                new_answer,
            )
            row["cost_usd"] = round(spent([planner, reviewer, critic, chief]), 4)
            results.append(row)
            print(
                json.dumps(
                    {k: v for k, v in row.items() if not k.startswith("new_")}, ensure_ascii=False
                )
            )
        total = sum(r["cost_usd"] for r in results) + spent([judge])
    OUT.write_text(
        json.dumps({"results": results, "cost_usd": round(total, 4)}, ensure_ascii=False, indent=1)
    )
    print(json.dumps({"total_cost_usd": round(total, 4), "written": str(OUT)}))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or TASKS))

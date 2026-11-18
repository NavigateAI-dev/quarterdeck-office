"""The team room after a wave: huddles and settling challenges."""

import asyncio
from typing import Any

from app.agents.base import AgentOutputError
from app.orchestrator.graph import (
    DONE,
    MAX_RERUNS,
    PENDING,
    ROOM_KINDS,
    _invoke,
    asked_of,
    plan_of,
    room_line,
    room_text,
    short,
)
from app.orchestrator.nodes.context import NodeContext, make_teammate, say
from app.orchestrator.state import AnalysisState
from app.schemas import PlanStep, TaskPlan


async def huddle(
    ctx: NodeContext,
    state: AnalysisState,
    wave: list[PlanStep],
    status: dict[str, str],
    results: dict[str, Any],
    number: int,
) -> None:
    """After a wave, each role reacts from its angle, all at once."""
    contract = plan_of(state)
    finished = []
    for step in wave:
        line = f"- {step.id} ({step.kind}, {status.get(step.id)}): {short(step.brief)}"
        produced = results.get(step.id) or {}
        findings = [str(f) for f in produced.get("key_findings") or []][:3]
        if findings:
            line += "\n  found: " + "; ".join(findings)
        finished.append(line)
    # The room numbered, so a teammate can answer a post by its number.
    try:
        room = list(ctx.board.read()) if ctx.board is not None else []
    except Exception:  # noqa: BLE001 - an unreadable room is an empty one
        room = []
    numbered = [(n, p) for n, p in enumerate(room, 1) if p.get("kind") in ROOM_KINDS][-10:]
    coming = [
        f"- {step.id} ({step.kind}): {short(step.brief)}"
        for step in contract.steps
        if status.get(step.id, PENDING) == PENDING
    ]
    inputs = (
        "The wave that just finished:\n"
        + "\n".join(finished)
        + "\n\nStill to come:\n"
        + "\n".join(coming)
        + (
            "\n\nThe room so far:\n" + room_text([f"#{n} {room_line(p)[2:]}" for n, p in numbered])
            if numbered
            else ""
        )
    )
    speakers: list[tuple[str, Any]] = [
        ("critic", ctx.agents.critic),
        ("writer", ctx.agents.deliverable_for(contract.task_kind)),
    ]
    if ctx.agents.advisor is not None:
        speakers.append(("advisor", ctx.agents.advisor))

    async def react(role: str, agent: Any) -> tuple[str, dict[str, Any] | None]:
        try:
            return role, await _invoke(
                make_teammate(ctx, agent, role), asked_of(state), state, inputs
            )
        except AgentOutputError:
            # A huddle never stops the work, its budget included.
            return role, None

    for role, said in await asyncio.gather(*(react(r, a) for r, a in speakers)):
        for post in (said or {}).get("posts") or []:
            answers = post.get("reply_to")
            say(
                ctx,
                str(post.get("kind")),
                role,
                str(post.get("text")),
                to_step=post.get("to_step"),
                phase="huddle",
                wave=number,
                reply_to=(
                    str(room[answers - 1].get("key"))
                    if isinstance(answers, int) and 1 <= answers <= len(room)
                    else None
                ),
            )


def settle(
    ctx: NodeContext,
    state: AnalysisState,
    plan_now: TaskPlan,
    status: dict[str, str],
    challenges: list[dict[str, Any]],
    number: int,
) -> dict[str, Any]:
    """Act on what the room asked since the last wave.

    A challenge, or the owner's post, to a finished step runs that step
    again with it in hand, unless later work already rests on that step or
    the reruns are spent: then it is a limit the answer states. One to a
    step still to come waits for it. An offer becomes a step while the plan
    has room.
    """
    handled = list(state.get("handled") or [])
    reruns = int(state.get("reruns") or 0)
    offers = int(state.get("offers") or 0)
    steps = list(plan_now.steps)
    ids = {step.id for step in steps}
    try:
        posts = [p for p in ctx.board.read() if p.get("key") not in handled] if ctx.board else []
    except Exception:  # noqa: BLE001 - an unreadable room asks nothing
        posts = []
    added = False
    for post in posts:
        kind, target = post.get("kind"), post.get("to_step")
        if kind in ("challenge", "owner") and target in ids:
            handled.append(str(post.get("key")))
            entry = {
                "key": str(post.get("key")),
                "to_step": target,
                "text": str(post.get("text")),
                "by": str(post.get("author_role")),
                "outcome": "pending",
            }
            if status.get(target, PENDING) != PENDING:
                resting = any(
                    target in step.depends_on and status.get(step.id) == DONE for step in steps
                )
                if reruns < MAX_RERUNS and not resting:
                    status[target] = PENDING
                    reruns += 1
                    entry["outcome"] = "rerun"
                    say(
                        ctx,
                        "note",
                        "chief",
                        f"Rerunning {target} to answer: {short(entry['text'], 160)}",
                        phase="huddle",
                        wave=number,
                        reply_to=entry["key"],
                    )
                else:
                    entry["outcome"] = "limit"
                    say(
                        ctx,
                        "note",
                        "chief",
                        f"Kept as a limit for {target}: {short(entry['text'], 160)}",
                        phase="huddle",
                        wave=number,
                        reply_to=entry["key"],
                    )
            challenges.append(entry)
        elif kind == "offer":
            # Kept for the Chief, not added as a step: offers taken as
            # steps grew plans into audits nobody asked for. A replan reads
            # them beside the reader brief and takes only what serves it.
            handled.append(str(post.get("key")))
            offers += 1
            say(
                ctx,
                "note",
                "chief",
                f"Noted the {post.get('author_role')}'s offer for the next replan: "
                f"{short(str(post.get('text')), 160)}",
                phase="huddle",
                wave=number,
                reply_to=str(post.get("key")),
            )
    update: dict[str, Any] = {"handled": handled, "reruns": reruns, "offers": offers}
    if added:
        update["plan"] = plan_now.model_copy(update={"steps": steps}).model_dump(mode="json")
    return update

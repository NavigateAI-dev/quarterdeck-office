"""The workflow of one task, drawn from what it recorded.

The Chief plans and decides; quick judgments route the work; helpers run the
steps in parallel; the critic reviews; the Chief writes the answer. The office
has worked this way since D1, and this assembles it as one picture: which lane
is working, which steps run, what Jev judged and how sure it was, and what
happened last. Everything here traces to a run event or a state key. A
decision made by a rule in code says so, rather than posing as Jev's.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    ModelConnection,
    Project,
    Run,
    RunEvent,
    TeamPost,
    UsageEvent,
    Venture,
)
from app.orchestrator.graph import MAX_REPLANS
from app.orchestrator.runner import team_post_view
from app.rooms import talk_about
from app.team_room import threaded

ACTIVE = ("PENDING", "RUNNING", "RECOVERING")
# A judgment at or above this is acted on as it stands ("sharp"); below it the
# office holds back or asks ("split"). The same bar as the office's other checks.
SHARP = 0.75
LOG_ROWS = 14
# The room's posts the Workflow tab shows, newest last.
ROOM_SHOWN = 200
EARLIEST = datetime.min.replace(tzinfo=UTC)
BRIEF_CHARS = 160
# Which lane each graph node belongs to.
LANE = {
    "plan": "chief",
    "replan": "chief",
    "research": "workers",
    "strategy": "workers",
    "work": "workers",
    "engineering": "workers",
    "critique": "critic",
    "synthesis": "answer",
    "approval": "owner",
}
CHIEF_ROLES = ("chief", "planner")
WRITER_ROLES = ("writer", "summarist", "strategist")
# The progress check after a wave, read from where the task went next.
NEXT = {"research": "another wave", "plan": "replan", "critique": "to review"}
DONE_TEXT = {
    "plan": "plan ready",
    "replan": "replanned",
    "work": "wave done",
    "research": "research done",
    "strategy": "strategy done",
    "critique": "review done",
    "synthesis": "answer written",
    "approval": "decided",
    "engineering": "follow-up done",
}
ACTOR = {
    "plan": "chief",
    "replan": "chief",
    "research": "researcher",
    "work": "office",
    "strategy": "writer",
    "critique": "critic",
    "synthesis": "chief",
    "approval": "owner",
    "engineering": "engineer",
}
# The advisor's three moments, in the order a task reaches them.
MOMENTS = (
    ("before_plan", "before the plan"),
    ("repeated_failure", "the same failure again"),
    ("before_done", "before done"),
)
STARTED_TEXT = {
    "critique": "reviewing",
    "synthesis": "writing the answer",
    "approval": "waiting for you",
    "strategy": "writing",
    "engineering": "following up",
}


def _levels(steps: list[dict[str, Any]]) -> dict[str, int]:
    """How deep each step sits: 1 when it waits for nothing."""
    by_id = {step["id"]: step for step in steps}
    levels: dict[str, int] = {}

    def level(step_id: str, seen: frozenset[str] = frozenset()) -> int:
        if step_id in levels:
            return levels[step_id]
        needed = [d for d in by_id[step_id].get("depends_on") or [] if d in by_id and d not in seen]
        found = 1 + max((level(d, seen | {step_id}) for d in needed), default=0)
        levels[step_id] = found
        return found

    for step in steps:
        level(step["id"])
    return levels


def _step_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e["kind"] in ("step_started", "step_completed")]


def _active_node(events: list[dict[str, Any]], active: bool) -> str | None:
    """The node started last and not finished since, while the task runs."""
    if not active:
        return None
    steps = _step_events(events)
    if not steps or steps[-1]["kind"] != "step_started":
        return None
    return str(steps[-1]["payload"].get("node") or "") or None


def _model_of(usage: list[dict[str, Any]], roles: tuple[str, ...]) -> dict[str, Any]:
    rows = [row for row in usage if row["role"] in roles]
    if not rows:
        return {"model": None, "calls": 0, "tokens": 0}
    by_model: Counter[str] = Counter()
    for row in rows:
        by_model[str(row["model"] or "")] += int(row["calls"] or 0)
    return {
        "model": by_model.most_common(1)[0][0] or None,
        "calls": sum(int(row["calls"] or 0) for row in rows),
        "tokens": sum(int(row["tokens"] or 0) for row in rows),
    }


def _grade(confidence: float | None) -> str | None:
    if confidence is None:
        return None
    return "sharp" if confidence >= SHARP else "split"


def _judgment(
    label: str, answer: str, confidence: float | None = None, by: str = "jev"
) -> dict[str, Any]:
    return {
        "label": label,
        "answer": answer,
        "confidence": confidence,
        "grade": _grade(confidence),
        "by": by,
    }


def _jev(
    state: dict[str, Any], events: list[dict[str, Any]], line: dict[str, Any] | None
) -> list[dict[str, Any]]:
    judged: list[dict[str, Any]] = []
    related = [e for e in events if e["kind"] == "related_work"]
    if related:
        found = len(related[-1]["payload"].get("runs") or [])
        judged.append(
            _judgment("Related work", f"{found} earlier answer{'s' if found != 1 else ''}")
        )
    if line and line.get("set_by") == "office" and line.get("name"):
        judged.append(_judgment("Business line", str(line["name"]), line.get("confidence")))
    convergence = state.get("convergence") or {}
    if convergence.get("method") == "typesafe" and convergence.get("total"):
        share = round(int(convergence.get("still_raised") or 0) / int(convergence["total"]), 2)
        repeated = state.get("stopped_reason") == "revision_not_converging"
        judged.append(_judgment("Same concerns again", "yes" if repeated else "no", share))
    checks = [c for c in state.get("source_checks") or [] if isinstance(c, dict)]
    if checks:
        sure = max(float(c.get("confidence") or 0) for c in checks)
        judged.append(
            _judgment("Source labels", f"{len(checks)} corrected", round(sure, 2) if sure else None)
        )
    if state.get("claim_check") == "typesafe":
        flagged = len(state.get("unverified_claims") or [])
        judged.append(
            _judgment(
                "Claims checked",
                f"{flagged} not supported by the research" if flagged else "all supported",
            )
        )
    return judged


def _decisions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The progress check after each wave, read from where the task went next."""
    steps = _step_events(events)
    decided = []
    for index, row in enumerate(steps):
        if row["kind"] != "step_completed" or row["payload"].get("node") != "work":
            continue
        following = next(
            (later for later in steps[index + 1 :] if later["kind"] == "step_started"), None
        )
        if following is None:
            continue
        answer = NEXT.get(str(following["payload"].get("node")))
        if answer:
            decided.append(_judgment("Retry or stop", answer, by="rule"))
    return decided


# Why a task's model moved to the owner's backup, in words.
FAILOVER_REASON = {
    "no_credit": "ran out of credit",
    "key_refused": "refused its key",
    "failing": "kept failing",
}


def _log(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in events:
        kind, payload = row["kind"], row["payload"] or {}
        node = str(payload.get("node") or "")
        if kind == "step_started":
            if node == "plan":
                planned_before = any(
                    r["actor"] == "chief" and r["text"] in ("plan ready", "replanned") for r in rows
                )
                actor, text = "chief", "replanning" if planned_before else "planning"
            elif node == "research":
                actor, text = "researcher", "step started"
            else:
                actor, text = ACTOR.get(node, "office"), STARTED_TEXT.get(node, f"{node} started")
        elif kind == "step_completed":
            actor, text = ACTOR.get(node, "office"), DONE_TEXT.get(node, f"{node} done")
        elif kind == "run_started":
            actor, text = "office", "started"
        elif kind == "run_finished":
            actor, text = "office", str(payload.get("status") or "finished").lower()
        elif kind == "related_work":
            found = len(payload.get("runs") or [])
            actor, text = "jev", f"found {found} related answer{'s' if found != 1 else ''}"
        elif kind == "rejection":
            actor, text = str(payload.get("actor")), str(payload.get("text"))
        elif kind == "advice":
            actor, text = "advisor", str(payload.get("text"))
        elif kind == "venture_doctrine_applied":
            actor, text = "office", "told the business line's standard"
        elif kind == "model_failed_over":
            reason = FAILOVER_REASON.get(str(payload.get("reason")), "it kept failing")
            actor, text = (
                "office",
                f"moved {payload.get('role')} to {payload.get('to')}, your backup: "
                f"{payload.get('from')} {reason}",
            )
        elif kind == "model_downgraded":
            actor, text = "office", f"moved to {payload.get('model')}, the cheaper model"
        else:
            actor, text = "office", kind.replace("_", " ")
        previous = rows[-1] if rows else None
        if (
            previous is not None
            and previous["actor"] == actor
            and previous["base"] == text
            and abs((row["at"] - previous["at"]).total_seconds()) < 2
        ):
            previous["count"] += 1
            previous["text"] = (
                f"{previous['count']} steps started"
                if text == "step started"
                else f"{text} ×{previous['count']}"
            )
            continue
        rows.append({"at": row["at"], "actor": actor, "text": text, "base": text, "count": 1})
    return [
        {
            "at": r["at"].isoformat() if isinstance(r["at"], datetime) else r["at"],
            "actor": r["actor"],
            "text": r["text"],
        }
        for r in rows[-LOG_ROWS:]
    ]


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _advisor(state: dict[str, Any], usage: list[dict[str, Any]], on: bool) -> dict[str, Any]:
    consulted = [e for e in state.get("advice") or [] if isinstance(e, dict)]
    latest_by = {e.get("moment"): e for e in consulted}
    model = _model_of(usage, ("advisor",))
    return {
        # On for this task: it was consulted, or it is on call for work still running.
        "on": bool(consulted) or bool(model["calls"]) or on,
        **model,
        "moments": [
            {
                "moment": key,
                "label": label,
                "verdict": (latest_by.get(key) or {}).get("verdict"),
            }
            for key, label in MOMENTS
        ],
        "consulted": consulted,
        "latest": consulted[-1] if consulted else None,
    }


def _advice_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    labels = dict(MOMENTS)
    rows = []
    for entry in state.get("advice") or []:
        at = _when(entry.get("at")) if isinstance(entry, dict) else None
        if at is None:
            continue
        moment = labels.get(entry.get("moment"), str(entry.get("moment")))
        rows.append(
            {
                "kind": "advice",
                "payload": {"text": f"{moment} · {str(entry.get('verdict')).replace('_', ' ')}"},
                "at": at,
            }
        )
    return rows


# Which lane's actor a role's call belongs to, in the log.
ROLE_ACTOR = {
    "planner": "chief",
    "chief": "chief",
    "summarist": "writer",
    "strategist": "writer",
    "writer": "writer",
}


def _first_reason(detail: str) -> str:
    """The first attempt's reason, without its "attempt N:" label."""
    first = re.split(r";\s*attempt \d+:", detail or "", maxsplit=1)[0]
    first = re.sub(r"^attempt \d+:\s*", "", first.strip())
    return re.sub(r"^not valid JSON:\s*", "", first)[:140]


def _rejection_rows(rejections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for call in rejections:
        at = _when(call.get("at"))
        if at is None or not call.get("detail"):
            continue
        tries = int(call.get("attempts") or 1)
        if call.get("status") == "ok":
            text = f"output corrected on try {tries}"
        else:
            text = f"output rejected after {tries} tries · {_first_reason(str(call['detail']))}"
        role = str(call.get("role") or "office")
        rows.append(
            {
                "kind": "rejection",
                "payload": {"actor": ROLE_ACTOR.get(role, role), "text": text},
                "at": at,
            }
        )
    return rows


def _pipeline_steps(events: list[dict[str, Any]], active_node: str | None) -> list[dict[str, Any]]:
    """A task planned without steps: its research and strategy are the helpers."""
    cards: list[dict[str, Any]] = []
    for node, role in (("research", "researcher"), ("strategy", "writer")):
        started = any(
            e["kind"] == "step_started" and e["payload"].get("node") == node for e in events
        )
        if not started:
            continue
        finished = any(
            e["kind"] == "step_completed" and e["payload"].get("node") == node for e in events
        )
        status = "running" if active_node == node else "done" if finished else "pending"
        cards.append(
            {
                "id": node,
                "kind": "research" if node == "research" else "write",
                "role": role,
                "brief": "",
                "effort": None,
                "skill": None,
                "depends_on": [],
                "level": 1 if node == "research" else 2,
                "status": status,
                "model": None,
            }
        )
    return cards


def build_flow(
    *,
    state: dict[str, Any],
    status: str,
    workflow: str,
    events: list[dict[str, Any]],
    usage: list[dict[str, Any]],
    line: dict[str, Any] | None,
    max_parallel: int,
    advisor_on: bool = False,
    rejections: list[dict[str, Any]] | None = None,
    room: list[dict[str, Any]] | None = None,
    talk: list[dict[str, Any]] | None = None,
    team_on: bool = False,
) -> dict[str, Any]:
    active = status in ACTIVE
    node = _active_node(events, active)
    phase = LANE.get(node or "", None)
    chief = _model_of(usage, CHIEF_ROLES)
    researcher = _model_of(usage, ("researcher",))
    writer = _model_of(usage, WRITER_ROLES)
    critic = _model_of(usage, ("critic",))

    plan = state.get("plan") or {}
    planned = [s for s in plan.get("steps") or [] if isinstance(s, dict) and s.get("id")]
    step_status = state.get("step_status") or {}
    if planned:
        levels = _levels(planned)
        ready = [
            s["id"]
            for s in planned
            if step_status.get(s["id"], "pending") == "pending"
            and all(step_status.get(d) == "done" for d in s.get("depends_on") or [])
        ]
        running = set(ready[:max_parallel]) if phase == "workers" else set()
        steps = [
            {
                "id": s["id"],
                "kind": s.get("kind") or "research",
                "role": "writer" if s.get("kind") == "write" else "researcher",
                "brief": str(s.get("brief") or "")[:BRIEF_CHARS],
                "effort": s.get("effort"),
                "skill": s.get("skill"),
                "depends_on": list(s.get("depends_on") or []),
                "level": levels[s["id"]],
                "status": "running" if s["id"] in running else step_status.get(s["id"], "pending"),
                "model": (writer if s.get("kind") == "write" else researcher)["model"],
            }
            for s in planned
        ]
    else:
        steps = _pipeline_steps(events, node)
        for card in steps:
            card["model"] = (researcher if card["role"] == "researcher" else writer)["model"]

    completed_plans = sum(
        1
        for e in _step_events(events)
        if e["kind"] == "step_completed" and e["payload"].get("node") in ("plan", "replan")
    )
    doing = None
    if phase == "chief":
        doing = "replanning" if completed_plans else "planning"
    elif phase == "answer":
        doing = "writing the answer"
    critiques = sum(
        1
        for e in events
        if e["kind"] == "step_completed" and e["payload"].get("node") == "critique"
    )
    answered = any(
        e["kind"] == "step_completed" and e["payload"].get("node") == "synthesis" for e in events
    )
    judgments = _jev(state, events, line)
    efforts = Counter(str(s.get("effort")) for s in planned if s.get("effort"))
    replans = int(state.get("replans") or 0)
    return {
        "phase": phase or ("done" if not active else None),
        "active": active,
        "status": status,
        "workflow": workflow,
        "chief": {
            **chief,
            "doing": doing,
            "replans": replans,
            "max_replans": MAX_REPLANS,
            "plans": completed_plans,
        },
        "jev": {"judgments": judgments, "count": len(judgments)},
        "decisions": _decisions(events),
        "steps": steps,
        "critic": {
            **critic,
            "rounds": critiques,
            "verdict": (state.get("last_critique") or state.get("critique") or {}).get("verdict"),
            "active": phase == "critic",
        },
        "answer": {
            **chief,
            "state": "writing" if phase == "answer" else "done" if answered else "waiting",
        },
        "advisor": _advisor(state, usage, advisor_on and active),
        # The task's room (T): every post, oldest first; whether the team reads it.
        "room": {
            "posts": list(room or [])[-ROOM_SHOWN:],
            # The same posts by stage, each with the replies it drew.
            "sections": threaded(list(room or [])[-ROOM_SHOWN:]),
            # What the owner asked about this task, and the agents' answers.
            "talk": list(talk or []),
            "count": len(room or []),
            "team": bool(team_on) or int(state.get("huddles") or 0) > 0,
            "huddles": int(state.get("huddles") or 0),
        },
        "log": _log(
            sorted(
                events + _advice_rows(state) + _rejection_rows(rejections or []),
                key=lambda row: _when(row["at"]) or EARLIEST,
            )
        ),
        "status_line": {
            "efforts": dict(efforts),
            "running": sum(1 for s in steps if s["status"] == "running"),
            "max_parallel": max_parallel,
            "done": sum(1 for s in steps if s["status"] == "done"),
            "steps": len(steps),
            "replans": f"{replans}/{MAX_REPLANS}",
            "waves": int(state.get("waves") or 0),
            "jev": len(judgments),
            "tokens": sum(int(row["tokens"] or 0) for row in usage),
        },
    }


def run_flow(
    session: Session,
    run: Run,
    max_parallel: int,
    advisor_on: bool = False,
    team_on: bool = False,
) -> dict[str, Any]:
    """One task's flow, read from its record."""
    events = [
        {"kind": row.kind, "payload": dict(row.payload_json or {}), "at": row.created_at}
        for row in session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run.id)
            .order_by(RunEvent.created_at, RunEvent.id)
        )
    ]
    model = func.coalesce(UsageEvent.resolved_model, UsageEvent.model_alias)
    usage = [
        {"role": role, "model": name, "calls": int(calls), "tokens": int(tokens or 0)}
        for role, name, calls, tokens in session.execute(
            select(
                UsageEvent.role,
                model,
                func.count(),
                func.sum(
                    func.coalesce(UsageEvent.prompt_tokens, 0)
                    + func.coalesce(UsageEvent.completion_tokens, 0)
                ),
            )
            .where(UsageEvent.run_id == run.id)
            .group_by(UsageEvent.role, model)
        )
    ]
    # A call through a connection is recorded under the connection's alias;
    # name the model it serves instead.
    served = {
        f"connection:{row.id}": row.model_id or row.display_name
        for row in session.scalars(select(ModelConnection))
    }
    for row in usage:
        row["model"] = served.get(str(row["model"]), row["model"])
    # Calls whose attempts were rejected, and why: the log says so.
    rejections = [
        {
            "role": row.role,
            "status": row.status,
            "attempts": row.attempts,
            "detail": row.error_detail,
            "at": row.occurred_at,
        }
        for row in session.scalars(
            select(UsageEvent).where(
                UsageEvent.run_id == run.id, UsageEvent.error_detail.is_not(None)
            )
        )
    ]
    line = None
    project = session.get(Project, run.project_id) if run.project_id else None
    if project is not None and project.venture_id:
        venture = session.get(Venture, project.venture_id)
        judged = dict(project.venture_suggestion_json or {})
        line = {
            "name": venture.name if venture else None,
            "set_by": project.venture_set_by,
            "confidence": judged.get("confidence") if project.venture_set_by == "office" else None,
        }
    return build_flow(
        state=dict(run.state_json or {}),
        status=run.status,
        workflow=run.workflow_name,
        events=events,
        usage=usage,
        line=line,
        max_parallel=max_parallel,
        advisor_on=advisor_on,
        rejections=rejections,
        room=[
            team_post_view(row)
            for row in session.scalars(
                select(TeamPost)
                .where(TeamPost.run_id == run.id)
                .order_by(TeamPost.created_at, TeamPost.id)
            )
        ],
        team_on=team_on,
        talk=talk_about(session, run.id),
    )

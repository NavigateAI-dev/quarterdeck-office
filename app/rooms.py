"""A project's room: what was said, and what its tasks did, in one stream.

Two sources, merged when the room is read:

- **What was said** is stored (`room_messages`): the owner's messages, and in
  time the agents' replies.
- **What a task did** is not stored again. A task starting, a step finishing,
  a task stopping to ask — each is read from `run_events`, and a step's line is
  written here, in code, from the artifact it produced. A line generated per
  step would cost a model call each and could claim work that was not done;
  this can only say what the record says.

The room pages by two cursors, one per source, because the two have their own
ids. When one source has more than a page, both are cut at the same moment in
time, so a later page never holds something older than an earlier one.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import verdicts
from app.agent_asks import open_asks
from app.artifacts import ArtifactStore
from app.db.models import (
    Artifact,
    PolicyRecord,
    RoomMessage,
    Run,
    RunControl,
    RunEvent,
    Task,
    TeamPost,
    Topic,
)
from app.deliverable import clean as clean_deliverable
from app.deliverable import describe as describe_deliverable
from app.follow_up import CARRIED, parent_answer
from app.orchestrator.runner import ARTIFACT_OF_STAGE, team_post_view
from app.run_asks import BUDGET_ASK, STUCK_ASK, pending_ask
from app.team_room import huddle_summary
from app.titles import title_of
from app.topics import runs_of, touch

PAGE = 100
MAX_PAGE = 200
LINE = 240
# What a task did that belongs in its project's room. Everything else in the
# event log is progress detail the task room already shows.
TASK_EVENTS = (
    "run_started",
    "step_completed",
    "run_finished",
    "run_failed",
    "run_cancelled",
    "budget_ask",
    "stuck_ask",
    "policy_ask",
    # A huddle of a task's team, in one line.
    "huddle_held",
    # A call to a third party the office stopped in the middle of.
    "side_effect_unknown",
)
ROLE_NAME = {
    "planner": "Chief",
    "chief": "Chief",
    "researcher": "Researcher",
    "strategist": "Strategist",
    "critic": "Critic",
    "summarist": "Writer",
    "engineer": "Engineer",
}
STEP_NAME = {
    "plan": "the plan",
    "research": "the research",
    "strategy": "the proposal",
    "critique": "the review",
    "synthesis": "the answer",
    "engineering": "the engineering step",
}
FINISHED = {
    "COMPLETED": "is done.",
    "WAITING_FOR_APPROVAL": "is waiting for your approval.",
    "NEEDS_HUMAN_REVIEW": "needs your review.",
    "BLOCKED": "was stopped by its review and needs you.",
    "REJECTED": "was rejected.",
    "FAILED": "failed.",
}
OFFICE = {"kind": "office", "role": None, "name": "The office", "profile_id": None}
# Who speaks a huddle's line: the task's team, read from its posts.
TEAM = {"kind": "office", "role": None, "name": "The team", "profile_id": None}


def clip(text: str, limit: int = LINE) -> str:
    """Shorten at a word, and say so."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return f"{cut}…"


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def summarize(stage: str, payload: dict[str, Any]) -> str:
    """One line saying what a step did, from its artifact alone."""
    try:
        line = _summary(stage, payload)
    except (TypeError, AttributeError, KeyError):
        line = None
    return clip(line) if line else f"Finished {STEP_NAME.get(stage, stage)}."


def _summary(stage: str, payload: dict[str, Any]) -> str | None:
    if stage == "plan":
        questions = list(payload.get("subquestions") or [])
        sources = list(payload.get("official_sources") or [])
        if not questions:
            question = payload.get("research_question")
            return f"Planned it: {question}" if question else None
        line = f"Planned it as {plural(len(questions), 'question')}"
        if sources:
            line += f", with {plural(len(sources), 'official source')} to read"
        return line + "."
    if stage == "research":
        answers = payload.get("answers")
        if answers is not None and not isinstance(answers, list):
            return None
        refused = len(payload.get("blocked_sources") or [])
        if answers:
            statuses = [str(item.get("status")) for item in answers]
            line = f"Answered {statuses.count('answered')} of {len(answers)}"
            if statuses.count("partial"):
                line += f", {statuses.count('partial')} in part"
            line += "."
        else:
            found = payload.get("sources")
            if not isinstance(found, list):
                return None
            line = f"Found {plural(len(found), 'source')}."
        if refused:
            line += f" {plural(refused, 'source')} refused us."
        return line
    if stage == "critique":
        verdict = payload.get("verdict")
        issue = next(
            (
                str(item)
                for field in ("critical_issues", "required_corrections", "missing_evidence")
                for item in payload.get(field) or []
            ),
            "",
        )
        if verdict == "PASS":
            return "Passed the work."
        if verdict == "REVISE":
            target = payload.get("revision_target") or "work"
            return f"Sent the {target} back" + (f": {issue}" if issue else ".")
        if verdict == "BLOCK":
            return "Stopped the work" + (f": {issue}" if issue else ".")
        return None
    if stage == "strategy":
        problem = payload.get("problem")
        return f"Wrote the proposal: {problem}" if problem else None
    if stage == "synthesis":
        recommendation = payload.get("recommendation")
        return f"Wrote the answer: {recommendation}" if recommendation else None
    return None


def card_for(stage: str, payload: dict[str, Any]) -> dict[str, Any]:
    """What a step's card shows beyond its line. Small, and only from the record."""
    card: dict[str, Any] = {"stage": stage}
    try:
        if stage == "plan":
            card["questions"] = [str(q) for q in payload.get("subquestions") or []]
            card["sources"] = [str(s) for s in payload.get("official_sources") or []]
        elif stage == "research":
            card["answers"] = [
                {"question": clip(str(a.get("question")), 120), "status": str(a.get("status"))}
                for a in payload.get("answers") or []
                if isinstance(a, dict)
            ]
            card["refused"] = [
                {"url": str(b.get("url")), "status": str(b.get("status"))}
                for b in payload.get("blocked_sources") or []
                if isinstance(b, dict)
            ]
            card["sources"] = len(payload.get("sources") or [])
        elif stage == "critique":
            card["verdict"] = payload.get("verdict")
            card["target"] = payload.get("revision_target")
            card["issues"] = [
                clip(str(item), 200) for item in (payload.get("critical_issues") or [])[:3]
            ]
        elif stage == "synthesis":
            card["recommendation"] = clip(str(payload.get("recommendation") or ""), 600)
    except (TypeError, AttributeError):
        return {"stage": stage}
    return card


def author_for(stage: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """The agent that did a step: named as the run was started with it."""
    role = ARTIFACT_OF_STAGE.get(stage, (None, stage))[1]
    agent = next(
        (
            entry
            for entry in snapshot.get("agents") or []
            if isinstance(entry, dict) and entry.get("role") == role
        ),
        None,
    )
    return {
        "kind": "agent",
        "role": role,
        "name": str(agent.get("name")) if agent else ROLE_NAME.get(role, role.title()),
        "profile_id": agent.get("profile_id") if agent else None,
    }


def post(
    session: Session,
    project_id: UUID | None,
    text: str,
    owner: str,
    actions: list[dict[str, Any]] | None = None,
    run_id: UUID | None = None,
    reply_to: int | None = None,
    thread_id: int | None = None,
    topic_id: UUID | None = None,
    client_key: str | None = None,
) -> dict[str, Any]:
    """Store what the owner said, with any buttons it offers, and the task it
    is about when it was asked from that task's Team room."""
    message = RoomMessage(
        project_id=project_id,
        run_id=run_id,
        reply_to=reply_to,
        thread_id=thread_id,
        topic_id=topic_id,
        client_key=client_key,
        author_kind="owner",
        author_role=None,
        author_name=owner,
        body=text,
        actions_json=actions or [],
    )
    session.add(message)
    touch(session, topic_id)
    session.flush()
    session.refresh(message)
    return _said(message, _control_states(session, [message]))


def recent(
    session: Session, store: ArtifactStore, topic: Topic, count: int
) -> list[dict[str, Any]]:
    """The last `count` things in the topic, oldest first: what an agent reads
    before it replies, so it answers the conversation and not a lone line."""
    said = list(
        session.scalars(
            select(RoomMessage)
            .where(RoomMessage.topic_id == topic.id)
            .order_by(RoomMessage.id.desc())
            .limit(count)
        )
    )
    events = list(
        session.execute(
            select(RunEvent, Run)
            .join(Run, Run.id == RunEvent.run_id)
            .where(runs_of(topic), RunEvent.kind.in_(TASK_EVENTS))
            .order_by(RunEvent.id.desc())
            .limit(count)
        ).all()
    )
    titles: dict[UUID, str] = {}
    controls = _control_states(session, said)
    items: list[tuple[datetime, int, int, dict[str, Any]]] = [
        (row.created_at, 0, row.id, _said(row, controls)) for row in said
    ]
    for event, run in events:
        item = _did(session, store, event, run, titles)
        if item is not None:
            items.append((event.created_at, 1, event.id, item))
    items.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in items][-count:]


# A task that can still take direction; matches the task room's rule.
STEERABLE = ("PENDING", "RUNNING", "RECOVERING", "PAUSED")


def _control_states(session: Session, rows: list[RoomMessage]) -> dict[str, str]:
    """Where each direction sent from these messages has got to, and — keyed by
    run id — whether each task a direction is still offered to can take it."""
    open_runs = {
        str(action["run_id"])
        for row in rows
        for action in row.actions_json or []
        if isinstance(action, dict)
        and action.get("kind") == "send_as_direction"
        and not action.get("control_id")
    }
    states: dict[str, str] = {}
    if open_runs:
        for run_id, status in session.execute(
            select(Run.id, Run.status).where(Run.id.in_([UUID(r) for r in open_runs]))
        ):
            states[f"run:{run_id}"] = status
    ids = [
        UUID(str(action["control_id"]))
        for row in rows
        for action in row.actions_json or []
        if isinstance(action, dict) and action.get("control_id")
    ]
    if ids:
        for control in session.scalars(select(RunControl).where(RunControl.id.in_(ids))):
            states[str(control.id)] = control.status
    return states


def _said(row: RoomMessage, controls: dict[str, str] | None = None) -> dict[str, Any]:
    actions = []
    for action in row.actions_json or []:
        shown = dict(action)
        if shown.get("control_id"):
            # QUEUED, RECEIVED or APPLIED: read now, not when it was sent.
            shown["status"] = (controls or {}).get(str(shown["control_id"]), "QUEUED")
        elif shown.get("kind") == "send_as_direction":
            # Offered only while the task can take it; a finished task cannot.
            status = (controls or {}).get(f"run:{shown.get('run_id')}")
            shown["open"] = status in STEERABLE
        actions.append(shown)
    return {
        "id": f"m:{row.id}",
        "kind": "message",
        "at": row.created_at.isoformat(),
        "author": {
            "kind": row.author_kind,
            "role": row.author_role,
            "name": row.author_name,
            "profile_id": str(row.author_profile_id) if row.author_profile_id else None,
        },
        "text": row.body,
        "run_id": str(row.run_id) if row.run_id else None,
        "reply_to": f"m:{row.reply_to}" if row.reply_to else None,
        "thread_id": f"m:{row.thread_id}" if row.thread_id else None,
        "cost_usd": row.cost_usd,
        "actions": actions,
        # Asked by a routine (app/routines/run.py keys it so), not typed by the owner.
        "routine": (row.client_key or "").startswith("routine:"),
    }


def _about(
    session: Session, row: RoomMessage, controls: dict[str, str], titles: dict[UUID, str]
) -> dict[str, Any]:
    """A message, named with the task it is about when it is about one."""
    item = _said(row, controls)
    if row.run_id is not None:
        run = session.get(Run, row.run_id)
        if run is not None:
            item["task"] = _title(session, run, titles)
    return item


def said_before(session: Session, topic_id: UUID, client_key: str | None) -> dict[str, Any] | None:
    """The message already said with this request id, if any."""
    if not client_key:
        return None
    row = session.scalar(
        select(RoomMessage).where(
            RoomMessage.topic_id == topic_id, RoomMessage.client_key == client_key
        )
    )
    return _said(row, _control_states(session, [row])) if row is not None else None


def talk_about(session: Session, run_id: UUID) -> list[dict[str, Any]]:
    """What the owner and the agents said about one task, oldest first."""
    rows = list(
        session.scalars(
            select(RoomMessage).where(RoomMessage.run_id == run_id).order_by(RoomMessage.id)
        )
    )
    controls = _control_states(session, rows)
    return [_said(row, controls) for row in rows]


def feed(
    session: Session,
    store: ArtifactStore,
    topic: Topic,
    after_event: int = 0,
    after_message: int = 0,
    limit: int = PAGE,
) -> dict[str, Any]:
    """The topic after both cursors, oldest first."""
    limit = max(1, min(limit, MAX_PAGE))
    events = list(
        session.execute(
            select(RunEvent, Run)
            .join(Run, Run.id == RunEvent.run_id)
            .where(
                runs_of(topic),
                RunEvent.id > after_event,
                RunEvent.kind.in_(TASK_EVENTS),
            )
            .order_by(RunEvent.id)
            .limit(limit + 1)
        ).all()
    )
    said = list(
        session.scalars(
            select(RoomMessage)
            .where(RoomMessage.topic_id == topic.id, RoomMessage.id > after_message)
            .order_by(RoomMessage.id)
            .limit(limit + 1)
        )
    )
    more = len(events) > limit or len(said) > limit
    cutoffs: list[datetime] = []
    if len(events) > limit:
        events = events[:limit]
        cutoffs.append(events[-1][0].created_at)
    if len(said) > limit:
        said = said[:limit]
        cutoffs.append(said[-1].created_at)
    if cutoffs:
        # Both cut at one moment: nothing on a later page is older than this one.
        cutoff = min(cutoffs)
        events = [row for row in events if row[0].created_at <= cutoff]
        said = [row for row in said if row.created_at <= cutoff]
    titles: dict[UUID, str] = {}
    items: list[tuple[datetime, int, int, dict[str, Any]]] = []
    for event, run in events:
        item = _did(session, store, event, run, titles)
        if item is not None:
            items.append((event.created_at, 1, event.id, item))
    controls = _control_states(session, said)
    judged = verdicts.of_messages(session, [row.id for row in said if row.author_kind == "agent"])
    for row in said:
        item = _about(session, row, controls, titles)
        if row.id in judged:
            item["verdict"] = judged[row.id]
        items.append((row.created_at, 0, row.id, item))
    items.sort(key=lambda entry: entry[:3])
    return {
        "items": [entry[3] for entry in items],
        "cursor": {
            "after_event": events[-1][0].id if events else after_event,
            "after_message": said[-1].id if said else after_message,
        },
        "more": more,
    }


# A project's documents are what its tasks delivered: the report the owner
# asked for, else the Chief's final answer, else the task record. (The task
# page leads with the Chief's final answer and keeps the report beneath it.)
ANSWER_TYPES = ("strategy_proposal", "synthesis", "final_report")
DOCUMENTS = 50


def documents(session: Session, project_id: UUID) -> list[dict[str, Any]]:
    """The project's finished documents: each task's answer, newest first.

    One per task — its answer, in its latest version — rather than every
    artifact: the plan and the research are the working, and the task room
    keeps them.
    """
    rows = session.execute(
        select(Artifact, Run, Task.objective, Task.title)
        .join(Run, Run.id == Artifact.run_id)
        .join(Task, Task.id == Run.root_task_id)
        .where(Artifact.project_id == project_id, Artifact.type.in_(ANSWER_TYPES))
    ).all()
    best: dict[UUID, tuple[Artifact, Run, str, str | None]] = {}
    for artifact, run, objective, title in rows:
        held = best.get(run.id)
        rank = (ANSWER_TYPES.index(artifact.type), -artifact.version)
        if held is None or rank < (ANSWER_TYPES.index(held[0].type), -held[0].version):
            best[run.id] = (artifact, run, objective, title)
    chosen = sorted(best.values(), key=lambda entry: entry[0].created_at, reverse=True)
    return [
        {
            "artifact_id": str(artifact.id),
            "run_id": str(run.id),
            "task": title_of(title, objective),
            "kind": artifact.type,
            "version": artifact.version,
            "created_at": artifact.created_at.isoformat(),
            "status": run.status,
            # What the owner asked it to come to, and the file format to offer first.
            "asked_for": describe_deliverable(run.deliverable_json),
            "format": clean_deliverable(run.deliverable_json).get("format", ""),
        }
        for artifact, run, objective, title in chosen[:DOCUMENTS]
    ]


# A task in one of these may be waiting on the owner.
WAITING = ("WAITING_FOR_APPROVAL", "NEEDS_HUMAN_REVIEW", "BLOCKED", "PAUSED")
# What finished work needs from the owner, said in its delivery line.
NEED_LINE = {
    "NEEDS_HUMAN_REVIEW": "Needs you: accept it, or say what to change.",
    "WAITING_FOR_APPROVAL": "Needs you: it proposes something to be done; read it and decide.",
    "FAILED": "Needs you: say whether to try again.",
}


def owner_questions(state: dict[str, Any] | None) -> list[str]:
    """The Chief's questions, when a task stopped to ask them first."""
    state = state or {}
    if state.get("stopped_reason") != "needs_owner_answers":
        return []
    asked = (state.get("plan") or {}).get("owner_questions")
    return [str(q).strip() for q in asked or [] if str(q).strip()][:3]


def waiting(session: Session, topic: Topic) -> list[dict[str, Any]]:
    """What this topic's tasks are waiting for the owner to decide, now."""
    return waiting_where(session, runs_of(topic))


def waiting_where(session: Session, which: Any) -> list[dict[str, Any]]:
    """What the tasks matching `which` are waiting for the owner to decide, now
    (a topic's, or the whole office's for the desk).

    Read from the records on every call rather than kept, so a question
    answered anywhere — the task room, the board, another tab — is gone here
    at the next read.
    """
    questions: list[dict[str, Any]] = []
    asked = session.execute(
        select(PolicyRecord, Task.objective, Task.title)
        .join(Run, Run.id == PolicyRecord.run_id)
        .join(Task, Task.id == Run.root_task_id)
        .where(
            which,
            PolicyRecord.verdict == "ask",
            PolicyRecord.decided_at.is_(None),
        )
        .order_by(PolicyRecord.id)
    ).all()
    for record, objective, title in asked:
        questions.append(
            {
                "kind": "policy",
                "run_id": str(record.run_id),
                "task": title_of(title, objective),
                "record_id": record.id,
                "reason": clip(record.reason or "A rule wants your word before it continues.", 600),
            }
        )
    asking = {question["run_id"] for question in questions}
    rows = session.execute(
        select(Run, Task.objective, Task.title)
        .join(Task, Task.id == Run.root_task_id)
        .where(which, Run.status.in_(WAITING))
        .order_by(Task.created_at)
    ).all()
    for run, objective, title in rows:
        base = {"run_id": str(run.id), "task": title_of(title, objective)}
        if run.status == "WAITING_FOR_APPROVAL":
            questions.append({**base, "kind": "approval"})
        elif run.status in ("NEEDS_HUMAN_REVIEW", "BLOCKED"):
            reason = run.error or "It could not finish on its own."
            chief_asks = owner_questions(run.state_json)
            if chief_asks:
                # What the Chief needs to know, rather than the code for it.
                reason = "The Chief asks: " + " ".join(chief_asks)
            questions.append({**base, "kind": "review", "reason": clip(reason, 300)})
        elif str(run.id) not in asking and _stuck_ask(session, run.id) is not None:
            stuck = _stuck_ask(session, run.id) or {}
            questions.append(
                {
                    **base,
                    "kind": "stuck",
                    "reason": clip(f"Stuck: {stuck.get('what') or 'a step cannot get on'}.", 300),
                }
            )
        elif str(run.id) not in asking:
            ask = _budget_ask(session, run.id)
            if ask is not None:
                questions.append(
                    {
                        **base,
                        "kind": "budget",
                        "limit": ask.get("limit") or "tokens",
                        "node": ask.get("node"),
                        "budget": dict(ask.get("budget") or {}),
                        "spent": dict(ask.get("spent") or {}),
                    }
                )
    # A coding agent's permission question, while it waits.
    questions.extend(open_asks(session, which))
    return questions


def _stuck_ask(session: Session, run_id: UUID) -> dict[str, Any] | None:
    """What a stuck task asks the owner, until it goes on or ends."""
    return pending_ask(session, run_id, STUCK_ASK)


def _budget_ask(session: Session, run_id: UUID) -> dict[str, Any] | None:
    """The limit question a paused run is on, until a raise or a finish answers it."""
    return pending_ask(session, run_id, BUDGET_ASK)


def _title(session: Session, run: Run, titles: dict[UUID, str]) -> str:
    if run.id not in titles:
        task = session.get(Task, run.root_task_id)
        titles[run.id] = title_of(task.title, task.objective) if task else "A task"
    return titles[run.id]


def _did(
    session: Session,
    store: ArtifactStore,
    event: RunEvent,
    run: Run,
    titles: dict[UUID, str],
) -> dict[str, Any] | None:
    """One thing a task did, as the room says it. None when it says nothing."""
    payload = dict(event.payload_json or {})
    title = _title(session, run, titles)
    base: dict[str, Any] = {
        "id": f"e:{event.id}",
        "at": event.created_at.isoformat(),
        "run_id": str(run.id),
        "task": title,
        "card": None,
        "waiting": False,
    }
    if event.kind == "step_completed":
        stage = str(payload.get("node") or "")
        if stage not in STEP_NAME:
            return None
        body = _artifact(session, store, run, stage, event.created_at)
        return {
            **base,
            "kind": "step",
            "author": author_for(stage, dict(run.config_snapshot_json or {})),
            "text": summarize(stage, body) if body is not None else f"Finished {STEP_NAME[stage]}.",
            "card": card_for(stage, body) if body is not None else {"stage": stage},
        }
    if event.kind == "huddle_held":
        posts = [
            team_post_view(row)
            for row in session.scalars(
                select(TeamPost)
                .where(TeamPost.run_id == run.id)
                .order_by(TeamPost.created_at, TeamPost.id)
            )
        ]
        wave = int(payload.get("wave") or 0)
        return {**base, "kind": "huddle", "author": TEAM, "text": huddle_summary(posts, wave)}
    if event.kind == "run_started":
        text = f"Started a task: {title}"
    elif event.kind == "run_finished" and run.topic_id is not None:
        # Work handed off in a discussion comes back to it: what it
        # concluded, in its own first lines, with the way to the whole of it.
        status = str(payload.get("status") or "")
        concluded = _concluded(session, store, run)
        said = (
            f"Delivered: {title}."
            if status in ("COMPLETED", "CLOSED")
            else f"{title} {FINISHED.get(status, 'finished.')}"
        )
        # Whether anything is needed, said every time.
        needed = (
            NEED_LINE.get(status, "")
            if status not in ("COMPLETED", "CLOSED")
            else "Nothing needed from you now."
        )
        return {
            **base,
            "kind": "delivery",
            "author": OFFICE,
            "status": status,
            "text": " ".join(part for part in (said, concluded, needed) if part),
        }
    elif event.kind == "run_finished":
        status = str(payload.get("status") or "")
        text = f"{title} {FINISHED.get(status, 'finished.')}"
    elif event.kind == "run_failed":
        step = STEP_NAME.get(str(payload.get("node")), "a step")
        text = f"{title} failed during {step}: {payload.get('code') or 'unknown error'}."
    elif event.kind == "run_cancelled":
        text = f"{title} was cancelled."
    elif event.kind == "side_effect_unknown":
        what = f"{payload.get('tool') or 'a tool'} on {payload.get('server') or 'a server'}"
        text = (
            f"{title} may or may not have called {what}: the office stopped in the middle of it. "
            "It will not call it again. Check there, then say whether it went through."
        )
        base["waiting"] = True
        base["effect_id"] = payload.get("id")
    elif event.kind == "budget_ask":
        limit = payload.get("limit") or "spending"
        text = f"{title} stopped at its {limit} limit and is waiting for you."
        base["waiting"] = True
    elif event.kind == "stuck_ask":
        what = clip(str(payload.get("what") or "a step cannot get on"), 160)
        text = f"{title} is stuck ({what}) and asks for your help."
        base["waiting"] = True
    elif event.kind == "policy_ask":
        reason = clip(str(payload.get("reason") or "a rule wants your word"), 200)
        text = f"{title} is waiting for your answer: {reason}"
        base["waiting"] = True
    else:
        return None
    return {**base, "kind": "task", "author": OFFICE, "text": text}


def _concluded(session: Session, store: ArtifactStore, run: Run) -> str:
    """What finished work concluded, in its first lines."""

    payload = parent_answer(session, store, run)
    if not payload:
        return ""
    for key in CARRIED:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return clip(value, 360)
    return ""


def _artifact(
    session: Session, store: ArtifactStore, run: Run, stage: str, before: datetime
) -> dict[str, Any] | None:
    """The artifact a step wrote, as it stood when the step finished."""
    kind = ARTIFACT_OF_STAGE[stage][0]
    row = session.scalar(
        select(Artifact)
        .where(Artifact.run_id == run.id, Artifact.type == kind, Artifact.created_at <= before)
        .order_by(Artifact.version.desc())
        .limit(1)
    )
    if row is None:
        return None
    try:
        body = json.loads(store.read(run.project_id, row.id, row.version))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None

"""From talk to work, and back.

The owner does not switch between chatting, planning and executing. Each
thing they say in a discussion is read for what it commits to (Jev, a typed
choice), and the office acts on it, saying so in a line:

- talk: the agents answer, as in any room;
- note it: filed as an idea on the discussion, nothing started, no model paid;
- look into it / do it: the agents answer what they can at once, and the
  office offers to start it as work, one button; pressed, the work is tied to
  the discussion and the message, in its project (or Free exploration), within
  a small ceiling, and its result comes back to the discussion;
- redo the delivered work: offered as one button, the same work reworked;
- stop: the discussion's work in progress pauses;
- later: the discussion comes back on a date.

Nothing that spends money starts from what was typed (owner, 4 Oct: "a task
was started immediately without consent"): work and rework start only from a
button the owner presses. What costs nothing (note, stop, later) is done.
Without the judge every message is talk, as it was before.

Two kinds of message are never new work, whatever the judge reads (3 Oct,
after "是的", a reply to an agent's question, started a task of its own):

- a reply is an answer. Answering a question about work in progress passes the
  answer to that work, with the question, and pays no one; any other reply
  carries the conversation on, with a button if it reads like new work;
- a short message ("yes", "ok", "是的") never starts work by itself either;
  "Stop." or "Next week." still act.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import ideas, topic_settings
from app.agent_profiles import capture_agent_snapshot
from app.config import Settings
from app.db.models import Project, RoomMessage, Run, Task, Topic
from app.judgments import JudgmentUnavailable, build_judge
from app.rooms import clip
from app.titles import title_of
from app.topics import runs_of, touch

logger = logging.getLogger(__name__)

# What work started from a sentence may cost before it asks.
MENTION_AT = re.compile(r"(?<![\w.@])@\w")
# Acted on alone at or above SURE; offered as a button from MAYBE. 0.7 let a
# greeting with a question ("any news from yesterday?", look_into 0.76) start
# paid work.
SURE = 0.8
MAYBE = 0.5
# Up to this many words and characters, a message is an answer or an aside.
BRIEF_WORDS = 3
BRIEF_CHARS = 12
FREE_EXPLORATION = "Free exploration"
DISCUSSION_LINES = 8
WORKING = ("PENDING", "RUNNING", "RECOVERING")
DELIVERED = ("COMPLETED", "NEEDS_HUMAN_REVIEW", "CLOSED")
INSTRUCTABLE = ("PENDING", "RUNNING", "RECOVERING", "PAUSED")


def brief(text: str) -> bool:
    """A short reply or aside ("yes", "ok", "是的", "thanks"): never new work."""
    return len(text.split()) <= BRIEF_WORDS and len(re.sub(r"\s", "", text)) <= BRIEF_CHARS


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def judge_for(settings: Settings) -> Any:
    """The judge that reads what a message commits to; None without TypeSafe."""
    return build_judge(settings)


def returns_on(text: str, now: datetime | None = None) -> datetime:
    """When "later" means, read from the owner's own words; a week by default."""
    now = now or datetime.now(UTC)
    said = text.lower()
    found = re.search(r"\bin (\d{1,2}) (day|days|week|weeks|month|months)\b", said)
    if found:
        count, unit = int(found[1]), found[2]
        days = count * (1 if unit.startswith("day") else 7 if unit.startswith("week") else 30)
        return now + timedelta(days=days)
    if "tomorrow" in said:
        return now + timedelta(days=1)
    if "next month" in said:
        return now + timedelta(days=30)
    if "next week" in said:
        return now + timedelta(days=7)
    for index, day in enumerate(WEEKDAYS):
        if re.search(rf"\b{day}\b", said):
            ahead = (index - now.weekday()) % 7 or 7
            return now + timedelta(days=ahead)
    return now + timedelta(days=7)


def participants_of(session: Session, topic: Topic) -> list[dict[str, Any]]:
    """The discussion's colleagues as the office's active agents, in join order."""

    ids = [str(value) for value in topic.participants_json or []]
    agents = {
        str(entry.get("profile_id")): dict(entry)
        for entry in capture_agent_snapshot(session)["agents"]
    }
    return [agents[pid] for pid in ids if pid in agents]


def free_exploration(session: Session) -> Project:
    """Where work from a discussion in no project runs, until it is placed."""
    project = session.scalar(
        select(Project).where(Project.name == FREE_EXPLORATION, Project.status == "ACTIVE")
    )
    if project is None:
        project = Project(
            name=FREE_EXPLORATION,
            description=(
                "Work started from discussions that belong to no project yet. Place a "
                "discussion in a project and its new work runs there."
            ),
        )
        session.add(project)
        session.flush()
    return project


class Handoff:
    """Acts on what each message in a discussion commits to. One per office."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        replies: Any,
        start: Callable[..., Run],
        pause: Callable[[UUID], None],
        direct: Callable[[UUID, str], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.replies = replies
        self.start = start
        self.pause = pause
        # Gives running work a colleague's word as direction.
        self.direct = direct

    async def handle(
        self,
        topic_id: UUID,
        message_id: int,
        text: str,
        agents: list[dict[str, Any]],
        unknown: list[str],
        archived: list[str],
    ) -> None:
        answering = self.answering(topic_id, message_id)
        if answering is not None:
            self.pass_on(topic_id, message_id, *answering, text)
            if "?" in text or "？" in text:
                await self.replies.respond(topic_id, message_id, agents, unknown, archived)
            return
        kind, sure = await self.read(topic_id, message_id, text)
        # A name nobody answers to is said back first, as in any room.
        acting = not unknown and not archived and sure >= SURE
        # A reply or a short answer is never offered as new work.
        replying = self.is_reply(message_id) or brief(text)
        try:
            if acting and kind == "note":
                self.note(topic_id, message_id, text)
                return
            if acting and kind == "stop":
                self.stop(topic_id, message_id)
                return
            if acting and kind == "later":
                self.later(topic_id, message_id, text)
                return
        except Exception:
            logger.exception('{"event":"handoff_failed","kind":"%s"}', kind)
            self.say(topic_id, message_id, "That could not be done just now.")
            return
        # The agents answer first, with what they know and what is on record.
        await self.replies.respond(topic_id, message_id, agents, unknown, archived)
        if acting and kind in ("look_into", "do") and not replying:
            if not self.proposed(message_id):
                # One colleague named: theirs to do, once started.
                named = len(agents) == 1 and MENTION_AT.search(text)
                self.offer(topic_id, message_id, text, agents[0] if named else None)
        elif acting and kind == "revise":
            self.offer_redo(topic_id, message_id, text)
        if kind == "decide" and sure >= MAYBE:
            # Offered, never recorded by itself: a decision is the owner's.
            self.say(
                topic_id,
                message_id,
                "That sounds decided. Record it as a decision, so every agent here follows it?",
                [{"kind": "record_decision", "text": clip(text, 600)}],
            )

    async def read(self, topic_id: UUID, message_id: int, text: str) -> tuple[str, float]:
        """What the message commits to, and how sure the judge is."""
        judge = judge_for(self.settings)
        if judge is None or not hasattr(judge, "commitment"):
            return "talk", 0.0
        try:
            kind, sure = await judge.commitment(text, self.recent(topic_id, message_id))
        except JudgmentUnavailable:
            return "talk", 0.0
        return str(kind), float(sure)

    def recent(self, topic_id: UUID, before: int) -> list[str]:
        """The discussion's last lines before this message, oldest first."""
        with self.session_factory() as session:
            rows = list(
                session.scalars(
                    select(RoomMessage)
                    .where(RoomMessage.topic_id == topic_id, RoomMessage.id < before)
                    .order_by(RoomMessage.id.desc())
                    .limit(DISCUSSION_LINES)
                )
            )
        return [f"{row.author_name}: {clip(row.body, 400)}" for row in reversed(rows)]

    def say(
        self,
        topic_id: UUID,
        message_id: int,
        text: str,
        actions: list[dict[str, Any]] | None = None,
    ) -> None:
        """The office speaking for itself, free: never a model's words."""
        with self.session_factory() as session, session.begin():
            asked = session.get(RoomMessage, message_id)
            session.add(
                RoomMessage(
                    project_id=asked.project_id if asked is not None else None,
                    topic_id=topic_id,
                    author_kind="office",
                    author_role=None,
                    author_name="The office",
                    body=text,
                    reply_to=message_id,
                    thread_id=asked.thread_id if asked is not None else None,
                    actions_json=actions or [],
                )
            )
            touch(session, topic_id)

    def note(self, topic_id: UUID, message_id: int, text: str) -> None:
        with self.session_factory() as session, session.begin():
            ideas.keep(session, text, "discussion", topic_id, message_id)
        self.say(
            topic_id,
            message_id,
            "Noted as an idea — nothing started. It waits under Ideas on the floor until you "
            "plan it for a day or take it up.",
        )

    def start_work(
        self,
        topic_id: UUID,
        message_id: int,
        objective: str,
        handed_to: dict[str, Any] | None = None,
        full_team: bool = False,
    ) -> Run | None:
        """Work from what the owner handed off, tied to the topic and the message.

        The lead takes it up when the office answers with the lead;
        `full_team` asks for the team pipeline, as "Redo with full team" does."""
        with self.session_factory() as session, session.begin():
            topic = session.get(Topic, topic_id)
            if topic is None:
                return None
            project_id = topic.project_id or free_exploration(session).id
            cap = topic_settings.cap_of(topic)
        lines = self.recent(topic_id, message_id)
        try:
            run = self.start(
                project_id,
                objective,
                topic_id=topic_id,
                origin_message_id=message_id,
                discussion=lines,
                cost_cap=cap,
                handed_to=str(handed_to.get("name")) if handed_to else None,
                full_team=full_team,
            )
        except Exception as refused:  # noqa: BLE001 - said in the discussion, not raised
            self.say(topic_id, message_id, f"Nothing started: {explain(refused)}")
            return None
        who = f" for {handed_to.get('name')}" if handed_to else ""
        self.say(
            topic_id,
            message_id,
            f"Started{who}: “{clip(objective, 140)}”. Up to ${cap:.2f}; if it needs "
            "more it will ask you. The result comes back here.",
        )
        return run

    def is_reply(self, message_id: int) -> bool:
        with self.session_factory() as session:
            said = session.get(RoomMessage, message_id)
            return said is not None and (said.reply_to is not None or said.thread_id is not None)

    def answering(self, topic_id: UUID, message_id: int) -> tuple[UUID, str, str] | None:
        """The work in progress whose question this message answers: its run, its
        title and the question. A reply to an agent's or the office's message
        about that work; any other message answers nothing."""
        with self.session_factory() as session:
            said = session.get(RoomMessage, message_id)
            if said is None or said.reply_to is None:
                return None
            asked = session.get(RoomMessage, said.reply_to)
            if asked is None or asked.author_kind == "owner" or asked.topic_id != topic_id:
                return None
            run = session.get(Run, asked.run_id) if asked.run_id else None
            if run is None and asked.reply_to is not None:
                run = session.scalar(
                    select(Run).where(
                        Run.topic_id == topic_id, Run.origin_message_id == asked.reply_to
                    )
                )
            if run is None or run.status not in INSTRUCTABLE:
                return None
            task = session.get(Task, run.root_task_id)
            title = title_of(task.title, task.objective) if task is not None else ""
            return run.id, title, f"{asked.author_name}: {asked.body}"

    def pass_on(
        self, topic_id: UUID, message_id: int, run_id: UUID, title: str, question: str, text: str
    ) -> None:
        """The owner's answer, with the question it answers, to the work it is about."""
        if self.direct is not None:
            self.direct(run_id, clip(f"Asked: {question}\nThe owner answered: {text}", 1200))
        named = f" “{clip(title, 80)}”" if title else ""
        self.say(topic_id, message_id, f"Passed to the work in progress{named}.")

    def proposed(self, message_id: int) -> bool:
        """Whether an agent's answer to this message already proposes the work."""
        with self.session_factory() as session:
            replies = session.scalars(
                select(RoomMessage).where(
                    RoomMessage.reply_to == message_id, RoomMessage.author_kind == "agent"
                )
            )
            return any(
                action.get("kind") == "start_task"
                for reply in replies
                for action in reply.actions_json or []
            )

    def offer(
        self,
        topic_id: UUID,
        message_id: int,
        objective: str,
        handed_to: dict[str, Any] | None = None,
    ) -> None:
        """The work, offered: it starts only when the owner presses Start."""
        action: dict[str, Any] = {"kind": "start_work", "objective": clip(objective, 600)}
        if handed_to:
            action["handed_to"] = str(handed_to.get("name"))
        with self.session_factory() as session:
            cap = topic_settings.cap_of(session.get(Topic, topic_id))
        self.say(
            topic_id,
            message_id,
            f"Start this as a piece of work? Up to ${cap:.2f}; the result comes "
            "back here. Nothing starts until you press Start.",
            [action],
        )

    def offer_redo(self, topic_id: UUID, message_id: int, note: str) -> None:
        """Rework of the delivered work, offered with the owner's words."""
        with self.session_factory() as session:
            delivered = session.scalar(
                select(Run.id)
                .where(Run.topic_id == topic_id, Run.status.in_(DELIVERED))
                .order_by(Run.completed_at.desc().nulls_last())
                .limit(1)
            )
        if delivered is None:
            return
        self.say(
            topic_id,
            message_id,
            "Redo the delivered work with what you said? The earlier version is kept.",
            [
                {
                    "kind": "rework",
                    "run_id": str(delivered),
                    "comment": clip(note, 600),
                    "label": "Redo it",
                }
            ],
        )

    def stop(self, topic_id: UUID, message_id: int) -> None:
        with self.session_factory() as session:
            topic = session.get(Topic, topic_id)
            if topic is None:
                return
            working = list(
                session.scalars(
                    select(Run.id)
                    .where(runs_of(topic), Run.status.in_(WORKING))
                    .order_by(Run.started_at)
                )
            )
        if not working:
            self.say(
                topic_id,
                message_id,
                "Nothing is working in this discussion, so nothing was paused.",
            )
            return
        for run_id in working:
            self.pause(run_id)
        count = len(working)
        self.say(
            topic_id,
            message_id,
            f"Paused {count} piece{'s' if count != 1 else ''} of work. Everything done so far "
            "is kept; resume it from its card when you are ready.",
        )

    def later(self, topic_id: UUID, message_id: int, text: str) -> None:
        returning = returns_on(text)
        with self.session_factory() as session, session.begin():
            topic = session.get(Topic, topic_id)
            if topic is None:
                return
            topic.revisit_at = returning
        self.say(
            topic_id,
            message_id,
            f"I'll bring this back on {returning.strftime('%A %-d %B')}, with where it stands.",
        )


def explain(error: Exception) -> str:
    """Why work could not start, in words."""
    name = type(error).__name__
    if name == "OfficePaused":
        return "new work is paused in Settings."
    if name == "MonthlyLimitReached":
        return "this month's spending limit is reached."
    if name == "UnknownProject":
        return "its project no longer exists."
    return "the office could not start it."


def work_view(session: Session, run: Run, spent: dict[str, Any]) -> dict[str, Any]:
    """A piece of work as its card in the discussion shows it."""
    from app.db.models import Task

    task = session.get(Task, run.root_task_id)
    plan = ((run.state_json or {}).get("plan") or {}).get("steps") or []
    status = dict((run.state_json or {}).get("step_status") or {})
    done = sum(1 for value in status.values() if value in ("done", "empty", "failed"))
    # A lead task's own step list, when it keeps one.
    todo = [i for i in (run.state_json or {}).get("lead_todo") or [] if isinstance(i, dict)]
    if todo:
        plan = todo
        done = sum(1 for item in todo if item.get("status") == "done")
    doing = next((str(i.get("text")) for i in todo if i.get("status") == "in_progress"), None)
    return {
        "run_id": str(run.id),
        "title": title_of(task.title if task else None, task.objective if task else ""),
        "status": run.status,
        "step": run.current_node,
        "doing": doing,
        # The lead's own checklist, for the dock's Board; a team plan
        # is read from the run's flow instead.
        "todo": [
            {"text": str(item.get("text") or ""), "status": str(item.get("status") or "pending")}
            for item in todo
        ],
        "steps_done": done,
        "steps": len(plan),
        "spent_usd": round(float(spent.get("cost_usd") or 0), 4),
        "max_cost_usd": (run.budget_json or {}).get("max_cost_usd"),
        "origin_message_id": f"m:{run.origin_message_id}" if run.origin_message_id else None,
        "handed_to": (run.state_json or {}).get("handed_to"),
        "started_at": run.started_at.isoformat() if run.started_at else None,
    }

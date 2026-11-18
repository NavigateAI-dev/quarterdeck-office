"""Agents replying in a project's room.

The owner speaks; the agent they name answers in its own voice — its owner's
instructions, its instruction-only skills, its own model connection — from this
project's record and the conversation so far. A message naming no one reaches
the project's lead. Several names are answered in turn, each agent reading the
replies before its own, so agents can build on and disagree with each other.

Four rules, each held by a test:

- At most six agents answer one message; a seventh name is refused, not queued.
  A message to everyone (@everyone, @all, @team, or "everyone" when no one is
  named) is answered by every active agent, the lead first, up to that cap.
- A reply never sets another agent off. Only the owner's words are read for
  names, which is the rule that stops Slack bots looping.
- A reply decides nothing. It cannot start, approve or change anything, and
  neither can the owner's words. When answering would need new work, the agent
  ends with a `PROPOSE:` line and the room offers that work as a button.
- Replies cost, visibly: each is one model call, shown on the reply, counted
  against the agent's daily room allowance and the office's monthly spending.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import room_tools, topic_settings, widgets
from app.agent_profiles import capture_agent_snapshot
from app.archive import protection
from app.archive.tools import archive_specs
from app.artifacts import ArtifactStore
from app.attachments import attached_in_topic, excerpts, read_spec, told
from app.calendars.store import agent_view, calendars_by_id, preferences, zone_of
from app.channels.conversation import agent_persona, estimate_tokens
from app.channels.lookup import lookup_specs
from app.config import Settings
from app.connection_completer import completer_for_entry
from app.db.models import (
    AgentProfile,
    Approval,
    ModelConnection,
    OfficeSettings,
    Project,
    RoomMessage,
    Run,
    Task,
    TeamPost,
    Topic,
)
from app.failover import GATEWAY, FailoverCompleter
from app.follow_up import parent_answer
from app.followups import service as follow_ups
from app.followups.tools import follow_up_specs
from app.gateway.client import Completion, asked
from app.lead.chat import CHAT_LIMIT, AllowanceCompleter, office_lookup
from app.lead.loop import CUT_SHORT
from app.lead.prompt import Section
from app.lead.service import LeadRequest, answer
from app.mcp import store as mcp_store
from app.model_calls import KeptCompleter, ModelCalls
from app.office_models import office_models
from app.office_settings import (
    MonthlyLimitReached,
    chat_allowance,
    check_monthly_limit,
    paused,
    provider_backups,
)
from app.office_settings import role_models as office_role_models
from app.orchestrator.graph import room_line
from app.orchestrator.runner import team_post_view
from app.room_steps import StepLog, sources_action
from app.rooms import ROLE_NAME, STEERABLE, _concluded, clip, recent
from app.routines import service as routine_service
from app.routines.tools import routine_specs
from app.titles import title_of
from app.topic_briefs import lead_of
from app.topics import runs_of

logger = logging.getLogger(__name__)

MAX_REPLIES = 6
# Speaking to the whole room: with an @, always; in plain words, only when
# the message names no one, so "@Iris, what does everyone think?" stays Iris's.
EVERYONE_AT = re.compile(r"(?<![\w.@])@(?:everyone|everybody|all|team)\b", re.IGNORECASE)
EVERYONE_WORDS = re.compile(
    r"\b(?:everyone|everybody|all of you|each of you|you all|y'all|every agent|whole team)\b",
    re.IGNORECASE,
)
EVERYONE_NAMES = {"everyone", "everybody", "all", "team"}
HISTORY = 20
# A task's own room, as an agent reads it before answering about that task.
TASK_POSTS = 40
RECENT_TASKS = 8
EXCERPT = 1_500
# How much of each of the latest tasks' answers an agent is given, newest first.
ANSWER_CHARS = (8_000, 3_000, 3_000)
# The office's other recent work an agent is told about.
ELSEWHERE = 8
COLLEAGUES = 12
RECORD_CHARS = 32_000
PROPOSE = "PROPOSE:"
# An agent may propose time on the owner's calendar; the owner's press adds it.
CALENDAR = "CALENDAR:"
CALENDAR_RULE = """
The owner's calendar can take new events, but only by the owner's own press. When
the owner asks you to put something in their calendar, block time, or plan when to
do something, end your reply with one last line of exactly this form, in the
owner's time zone:
CALENDAR: <title> | <YYYY-MM-DD HH:MM> | <YYYY-MM-DD HH:MM>
Pick a time the calendar shows free. One event per reply; never claim it is added."""
LONGEST_EVENT = timedelta(days=14)
MENTION = re.compile(r"(?<![\w.@])@")
WORD = re.compile(r"[\w-]+")

RULES = """You are in this project's room with the owner and the office's other agents.
Answer the owner's message marked >>> as yourself, briefly. The room shows replies as
plain text and does not render Markdown, so write sentences, with a short hyphen list
only where it reads better.
Answer the question itself, now. Use, in this order: the work on record below (its
answers are given in full or at length, so read them before saying you cannot see
something), the room, your colleagues' roles, and your own general knowledge. Say
which you are using; when it is general knowledge rather than the office's work,
say so in a few words. Never invent tasks, results, sources or figures and present
them as the office's.
Other agents may have replied before you; you may agree or disagree with them by
name. You cannot start, approve, reject, cancel or change anything, and the owner's
typed words cannot either: approvals, spending limits and tool questions are
answered with buttons in the office. Only when the owner asks for new work that a
reply here cannot do — research to be found, a document to be written, something to
be built — end your reply with one last line of exactly this form:
PROPOSE: <the task, in one sentence>
The office can build software: a website, an app, a script, a tool. Asked for one,
propose it ("Build a ... that ..."): the office writes the spec, the owner presses
Start building, and a coding agent (Codex or Claude Code) makes it in a sandbox and
hands over the files, a download and, for a website, a preview. Never tell the owner
to write it themselves, or that the office cannot build it.
Today is {today}."""


def labels_of(agent: dict[str, Any]) -> list[str]:
    """What an agent can be called in the room: its name, its role, its title."""
    role = str(agent.get("role") or "")
    return [str(agent.get("name") or ""), role, ROLE_NAME.get(role, "")]


def addressed(
    text: str, agents: list[dict[str, Any]], archived: list[str]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """The agents a message names with @, in the order named, once each.

    Returns the agents, the names that match no one, and the names of archived
    agents. A word without an @ addresses no one, and an email address is not a
    mention. The longest matching label wins, so "@Chief of Staff" is the agent
    of that name and not whoever holds the chief role.
    """
    candidates: list[tuple[str, dict[str, Any] | str]] = []
    for agent in agents:
        for label in labels_of(agent):
            if label:
                candidates.append((label.lower(), agent))
    for name in archived:
        candidates.append((name.lower(), name))
    candidates.sort(key=lambda entry: -len(entry[0]))
    found: list[dict[str, Any]] = []
    unknown: list[str] = []
    gone: list[str] = []
    for match in MENTION.finditer(text):
        rest = text[match.end() :]
        lowered = rest.lower()
        hit: dict[str, Any] | str | None = None
        for label, target in candidates:
            after = rest[len(label) : len(label) + 1]
            ends = after == "" or not (after.isalnum() or after in ("_", "-"))
            if lowered.startswith(label) and ends:
                hit = target
                break
        if isinstance(hit, dict):
            if all(agent.get("profile_id") != hit.get("profile_id") for agent in found):
                found.append(hit)
        elif isinstance(hit, str):
            if hit not in gone:
                gone.append(hit)
        else:
            word = WORD.match(rest)
            if word and word.group(0) not in unknown:
                unknown.append(word.group(0))
    return found, unknown, gone


def everyone_called(text: str) -> bool:
    """Whether a message speaks to every agent in the room."""
    if EVERYONE_AT.search(text):
        return True
    named = [m for m in MENTION.finditer(text)]
    return not named and bool(EVERYONE_WORDS.search(text))


def everyone_in(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every active agent, the lead first and then in the office's order."""
    lead = lead_of(agents)
    rest = [a for a in agents if lead is None or a.get("profile_id") != lead.get("profile_id")]
    return ([lead] if lead else []) + rest


def split_proposal(text: str) -> tuple[str, str | None]:
    """A reply's own words, and the task it offers when its last line proposes one."""
    lines = text.rstrip().split("\n")
    last = lines[-1].strip()
    if not last.startswith(PROPOSE):
        return text, None
    body = "\n".join(lines[:-1]).rstrip()
    objective = last[len(PROPOSE) :].strip()
    return body, (objective or None)


def split_calendar(text: str, zone: Any, now: datetime) -> tuple[str, dict[str, Any] | None]:
    """A reply's own words, and the event it proposes for the owner's calendar.

    Read in the owner's time zone. A line that does not read as a sensible
    event (a bad time, an end before its start, a day long past) offers
    nothing, and says so, rather than a button that adds something wrong.
    """
    lines = text.rstrip().split("\n")
    last = lines[-1].strip()
    if not last.startswith(CALENDAR):
        return text, None
    body = "\n".join(lines[:-1]).rstrip()
    parts = [part.strip() for part in last[len(CALENDAR) :].split("|")]
    try:
        title, begins, ends = parts
        starts_at = datetime.strptime(begins, "%Y-%m-%d %H:%M").replace(tzinfo=zone)
        if re.fullmatch(r"\d{1,2}:\d{2}", ends):
            ends = f"{begins[:10]} {ends}"
        ends_at = datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=zone)
    except ValueError:
        return body + "\n(The proposed calendar time could not be read.)", None
    sensible = (
        title
        and starts_at < ends_at <= starts_at + LONGEST_EVENT
        and starts_at > now - timedelta(days=1)
    )
    if not sensible:
        return body + "\n(The proposed calendar time could not be used.)", None
    return body, {
        "kind": "add_event",
        "title": clip(title, 200),
        "starts_at": starts_at.astimezone(UTC).isoformat(),
        "ends_at": ends_at.astimezone(UTC).isoformat(),
    }


def view(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": agent.get("name"),
        "role": agent.get("role"),
        "profile_id": agent.get("profile_id"),
    }


class RoomReplies:
    """Makes the replies. One per office; tests replace `complete`."""

    def __init__(
        self, session_factory: sessionmaker[Session], settings: Settings, store: ArtifactStore
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.store = store
        # Who is replying where, for the room's "replying…" line.
        self.replying: dict[UUID, list[dict[str, Any]]] = {}

    def team(self) -> tuple[list[dict[str, Any]], list[str]]:
        """The active agents as a run would use them, and the archived ones' names."""
        with self.session_factory() as session:
            agents = [dict(entry) for entry in capture_agent_snapshot(session)["agents"]]
            active = {entry["profile_id"] for entry in agents}
            archived = [
                row.name
                for row in session.scalars(select(AgentProfile))
                if str(row.id) not in active
            ]
        return agents, archived

    @staticmethod
    def default_answerer(
        session: Session, quoted: RoomMessage | None, thread_id: int | None
    ) -> str | None:
        """Who answers when no one is named: in a thread, the agent who
        last spoke in it, else its root's author; replying to an agent, that
        agent. None leaves it to the lead."""
        if thread_id is not None:
            spoke = session.scalar(
                select(RoomMessage.author_profile_id)
                .where(
                    RoomMessage.thread_id == thread_id,
                    RoomMessage.author_kind == "agent",
                    RoomMessage.author_profile_id.is_not(None),
                )
                .order_by(RoomMessage.id.desc())
                .limit(1)
            )
            if spoke is not None:
                return str(spoke)
            root = session.get(RoomMessage, thread_id)
            if root is not None and root.author_kind == "agent" and root.author_profile_id:
                return str(root.author_profile_id)
        if quoted is not None and quoted.author_kind == "agent" and quoted.author_profile_id:
            return str(quoted.author_profile_id)
        return None

    def who(
        self, text: str, default: str | None = None
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        """Who should answer this message, and any names no one answers to.
        `default` is the agent (profile id) who answers when no one is named."""
        agents, archived = self.team()
        named, unknown, gone = addressed(text, agents, archived)
        # @everyone is not an agent's name: it is the whole room.
        unknown = [name for name in unknown if name.lower() not in EVERYONE_NAMES]
        if everyone_called(text):
            return everyone_in(agents), unknown, gone
        if not named and not unknown and not gone:
            chosen = next((a for a in agents if default and a.get("profile_id") == default), None)
            lead = chosen or lead_of(agents)
            named = [lead] if lead else []
        return named, unknown, gone

    @staticmethod
    def steerable(session: Session, topic: Topic) -> list[dict[str, Any]]:
        """Buttons for sending the owner's words to a task that is still running."""
        rows = session.execute(
            select(Run.id, Task.objective, Task.title)
            .join(Task, Task.id == Run.root_task_id)
            .where(runs_of(topic), Run.status.in_(STEERABLE))
            .order_by(Task.created_at.desc())
            .limit(MAX_REPLIES)
        ).all()
        return [
            {"kind": "send_as_direction", "run_id": str(run_id), "task": title_of(title, objective)}
            for run_id, objective, title in rows
        ]

    async def respond(
        self,
        topic_id: UUID,
        message_id: int,
        agents: list[dict[str, Any]],
        unknown: list[str],
        archived: list[str],
    ) -> None:
        """Answer one owner message: the office for names no one answers to,
        then each agent in turn."""
        notes = [f"There is no agent called @{name} in this office." for name in unknown]
        notes += [f"{name} is archived, so it cannot answer." for name in archived]
        if not agents and not notes:
            notes.append(
                "No agent is active in this office, so no one can answer. Add one under Agents."
            )
        if notes:
            self._store_office(topic_id, message_id, " ".join(notes))
        for agent in agents:
            waiting = self.replying.setdefault(topic_id, [])
            entry: dict[str, Any] = {**view(agent), "steps": []}
            waiting.append(entry)
            try:
                if not await self._reply(topic_id, message_id, agent, StepLog(entry["steps"])):
                    break
            except Exception:
                logger.exception('{"event":"room_reply_failed","agent":"%s"}', agent.get("name"))
                self._store_office(topic_id, message_id, f"{agent.get('name')} could not answer.")
            finally:
                waiting.remove(entry)
                if not waiting:
                    self.replying.pop(topic_id, None)

    async def _reply(
        self, topic_id: UUID, message_id: int, agent: dict[str, Any], steps: StepLog | None = None
    ) -> bool:
        """One agent's reply. False when no later agent should try either."""
        name = str(agent.get("name"))
        with self.session_factory() as session:
            if paused(session, self.settings, "chat"):
                self._store_office(
                    topic_id,
                    message_id,
                    "Chat is switched off in Settings, so no agent is replying.",
                )
                return False
            try:
                check_monthly_limit(session, self.settings)
            except MonthlyLimitReached:
                self._store_office(
                    topic_id,
                    message_id,
                    "This month's spending limit is reached, so agents are not replying. "
                    "Raise it under Settings.",
                )
                return False
            topic = session.get(Topic, topic_id)
            # The thread's own model, goal and plan-first ask.
            agent = {**agent, **(topic_settings.model_entry(session, topic) or {})}
            stable, context, room = self._parts(session, topic_id, message_id, agent)
            context += topic_settings.goal_line(topic)
            asked = session.get(RoomMessage, message_id)
            if asked is not None and topic_settings.plan_first(asked.actions_json):
                room += topic_settings.PLAN_ASK
            messages = [
                {"role": "system", "content": stable + context},
                {"role": "user", "content": room},
            ]
            spent = self._tokens_today(session, agent)
            about = asked.run_id if asked is not None else None
            thread = asked.thread_id if asked is not None else None
            project_id = asked.project_id if asked is not None else None
        reserve = estimate_tokens(messages) + self.settings.chat_max_output_tokens
        if spent + reserve > self._allowance():
            self._store_office(topic_id, message_id, allowance_used_up(name))
            return True
        # Every call behind this reply is kept against the message it answers.
        model = KeptCompleter(
            _RoomModel(self, agent),
            ModelCalls(self.session_factory),
            None,
            str(agent.get("role") or "chief"),
            topic_id=topic_id,
            message_id=message_id,
        )
        # The lead sizes its own reply: it may look things up and use the
        # granted tools, within today's allowance.
        text, tool_actions, tokens, cost, started = await self._led(
            agent, project_id, stable, context, room, spent, model, topic_id, steps or StepLog()
        )
        if not text:
            self._store_office(topic_id, message_id, f"{name} gave no answer.")
            return True
        body, objective = split_proposal(text)
        objective = objective or started
        actions = [{"kind": "start_task", "objective": clip(objective, 600)}] if objective else []
        if not objective:
            with self.session_factory() as session:
                zone = zone_of(preferences(session).get("timezone"))
            body, event = split_calendar(body, zone, datetime.now(UTC))
            if event is not None:
                actions = [event]
        actions = tool_actions + actions
        with self.session_factory() as session, session.begin():
            session.add(
                RoomMessage(
                    project_id=project_id,
                    topic_id=topic_id,
                    author_kind="agent",
                    author_role=agent.get("role"),
                    author_name=name,
                    author_profile_id=UUID(agent["profile_id"])
                    if agent.get("profile_id")
                    else None,
                    body=body or "That needs new work.",
                    reply_to=message_id,
                    run_id=about,
                    thread_id=thread,
                    cost_usd=cost,
                    tokens=tokens,
                    actions_json=actions,
                )
            )
        return True

    def _allowance(self) -> int:
        with self.session_factory() as session:
            return chat_allowance(session, self.settings)

    def _out_of_reach(self, project_id: UUID | None) -> list[Section]:
        """Connected tools this discussion cannot use, so the lead can say why."""
        with self.session_factory() as session:
            lines = mcp_store.out_of_reach(session, project_id)
        if not lines:
            return []
        return [
            Section(
                "Connected to the office, but not usable in this discussion",
                OUT_OF_REACH + "\n".join(lines),
            )
        ]

    async def _led(
        self,
        agent: dict[str, Any],
        project_id: UUID | None,
        stable: str,
        context: str,
        room: str,
        spent: int,
        kept: Any,
        topic_id: UUID | None = None,
        steps: StepLog | None = None,
    ) -> tuple[str, list[dict[str, Any]], int, float | None, str | None]:
        """The lead's reply: its text, the reply's actions, tokens, cost, and a
        task it proposes. Every call counts against the agent's allowance."""
        used: list[dict[str, str]] = []
        proposed: list[dict[str, Any]] = []
        log = steps or StepLog()
        work = list(lookup_specs(*office_lookup(self.settings, self.session_factory), cut=None))
        try:
            tools = await room_tools.tools_for(self.session_factory, self.settings, project_id)
        except Exception:
            logger.exception('{"event":"room_tools_failed","agent":"%s"}', agent.get("name"))
            tools = None
        if tools is not None:
            work += room_tools.room_specs(tools, room_tools.offered(tools), used, proposed)
        routines_told: list[Section] = []
        attached_told: list[Section] = []
        if topic_id is not None:
            # What the owner attached: named first, so the context budget never cuts it.
            with self.session_factory() as session:
                attached = attached_in_topic(session, topic_id)
                if attached:
                    attached_told = [Section("Attached by the owner", told(session, attached))]
            if attached:
                work.append(read_spec(self.session_factory, attached))
            work += routine_specs(self.session_factory, topic_id, agent.get("profile_id"), proposed)
            work += follow_up_specs(self.session_factory, "discussion")
            with self.session_factory() as session:
                history = protection.may_reach(session, self.settings, agent, "discussion")
            if history:
                work += archive_specs(
                    self.session_factory, str(agent.get("name") or "An agent"), "discussion"
                )
            with self.session_factory() as session:
                routines_told = [
                    Section("Recurring work", routine_service.told(session)),
                    Section("To-dos", follow_ups.told(session, datetime.now(UTC))),
                ]
        totals: dict[str, Any] = {"tokens": 0, "cost": None}

        def record(tokens: int, cost: float | None) -> None:
            totals["tokens"] += tokens
            if cost is not None:
                totals["cost"] = (totals["cost"] or 0.0) + cost

        model = AllowanceCompleter(
            kept,
            cap=self.settings.chat_max_output_tokens,
            allowance=self._allowance(),
            used_today=lambda: spent + int(totals["tokens"]),
            record=record,
        )
        reply = await answer(
            LeadRequest(
                request=room,
                completer_for=lambda role: (model, str(agent.get("role") or "chief")),
                work=work,
                now=datetime.now(UTC),
                doctrine=stable,
                sections=[
                    *attached_told,
                    *self._out_of_reach(project_id),
                    *routines_told,
                    Section("This discussion", context, ROOM_CONTEXT_CHARS),
                ],
                on_step=log.step,
                on_start=log.started,
            )
        )
        if reply.stopped == "error":
            raise RuntimeError(f"room_lead_failed:{reply.reason}")
        text = reply.text.strip()
        if reply.reason == CHAT_LIMIT and text.startswith(CUT_SHORT):
            # What it had gathered is raw search results, not an answer.
            text = allowance_used_up(str(agent.get("name") or "This agent"))
            cited: list[str] = []
        else:
            cited = reply.citations.cited
        if text and reply.citations.unresolved:
            text += (
                "\n\n_Not confirmed by a tool this time: "
                + ", ".join(reply.citations.unresolved)
                + "_"
            )
        kept = log.kept()
        actions: list[dict[str, Any]] = [{"kind": "steps", "steps": kept}] if kept else []
        if used:
            actions.append({"kind": "tools_used", "calls": used})
        sources = sources_action(cited, reply.citations.unresolved, log)
        if sources is not None:
            actions.append(sources)
        started = reply.proposals[0]["objective"] if reply.proposals else None
        return text, actions + proposed, int(totals["tokens"]), totals["cost"], started

    async def complete(
        self,
        agent: dict[str, Any],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion:
        """One call on the agent's own connection, else the office's model for its role.

        When that provider fails, the backup the owner chose for it answers,
        exactly as it would in a task.
        """
        cap = self.settings.chat_max_output_tokens
        options = asked(max_tokens=cap, tools=tools)
        entry = agent if agent.get("connection_id") else self._role_entry(str(agent.get("role")))
        if entry is None:
            # No model for the role: the office's models say so, with a code.
            role_alias = self.settings.model_aliases.get(
                str(agent.get("role")), self.settings.chief_model_alias
            )
            models = office_models(self.settings, self.session_factory)
            return await models.complete(role_alias, messages, **options)
        key = str(entry["connection_id"])
        primary = completer_for_entry(entry, self.settings, self.session_factory)
        backed = self._backed(primary, key)
        return cast(Completion, await backed.complete(f"connection:{key}", messages, **options))

    def _role_entry(self, role: str) -> dict[str, Any] | None:
        """The connection the owner chose for this role, if any."""
        with self.session_factory() as session:
            return office_role_models(session, self.settings).get(role)

    def _backed(self, primary: Any, key: str) -> Any:
        """The provider's model, with the owner's chosen backup behind it, if any."""
        with self.session_factory() as session:
            plan = provider_backups(session).get(key) or {}
            backup_key = str(plan.get("backup") or "")
            row = (
                session.get(ModelConnection, UUID(backup_key))
                if backup_key and backup_key != GATEWAY
                else None
            )  # a backup still naming the retired gateway is none
            entry = (
                {
                    "connection_id": backup_key,
                    "provider": row.provider,
                    "model_id": row.model_id,
                    "base_url": row.base_url,
                }
                if row is not None
                else None
            )
        if not backup_key or backup_key == key or entry is None:
            return primary

        def make() -> Any:
            return completer_for_entry(entry, self.settings, self.session_factory)

        backup_alias = f"connection:{backup_key}"
        return FailoverCompleter(
            primary, key, backup_key, make, backup_alias, bool(plan.get("come_back", True))
        )

    def _messages(
        self, session: Session, topic_id: UUID, message_id: int, agent: dict[str, Any]
    ) -> list[dict[str, str]]:
        stable, context, room = self._parts(session, topic_id, message_id, agent)
        attached = excerpts(session, attached_in_topic(session, topic_id))
        if attached:
            context += "\n\n" + attached
        return [
            {"role": "system", "content": stable + context},
            {"role": "user", "content": room},
        ]

    def _parts(
        self, session: Session, topic_id: UUID, message_id: int, agent: dict[str, Any]
    ) -> tuple[str, str, str]:
        """Who the agent is and the room's rules (stable); what it may know
        (context); and the room so far, with the message to answer (request)."""
        topic = session.get(Topic, topic_id)
        if topic is None:
            raise ValueError(f"no topic {topic_id}")
        project = session.get(Project, topic.project_id) if topic.project_id else None
        place = (
            f"in the office's discussion '{topic.title}' for the project '{project.name}'"
            if project is not None
            else f"in the office's discussion '{topic.title}', which belongs to no project yet"
        )
        persona = agent_persona(agent, place=place)
        stable = persona + RULES.format(today=datetime.now(UTC).date().isoformat())
        system = (
            "\n\nProject record (JSON):\n"
            + json.dumps(self._record(session, topic, agent), default=str, ensure_ascii=False)[
                :RECORD_CHARS
            ]
            + "\n\n"
            + self._colleagues(session, agent)
            + "\n\n"
            + self._elsewhere(session, topic)
        )
        # The owner's week, as far as they let agents see it.
        # Only while the calendar is on the wall and agents may read it.
        calendar = (
            agent_view(session, datetime.now(UTC), days=7)
            if widgets.reads(session, "calendar")
            else ""
        )
        if calendar:
            system += "\n\n" + calendar
            if any(facts.get("writable") for facts in calendars_by_id(session).values()):
                system += "\n" + CALENDAR_RULE
        # The other widgets on the owner's wall that agents may read.
        wall = widgets.agent_context(session, datetime.now(UTC), "discussion", skip=("calendar",))
        if wall:
            system += (
                "\n\nFrom the owner's wall on the office floor, which they let you read:\n"
                + "\n\n".join(wall)
            )
        lines = []
        for item in recent(session, self.store, topic, HISTORY):
            author = item["author"]
            if author["kind"] == "owner":
                who = f"{author['name']} (owner)"
            elif author["kind"] == "agent":
                who = f"{author['name']} ({ROLE_NAME.get(author['role'] or '', author['role'])})"
            else:
                who = "The office"
            mark = ">>> " if item["id"] == f"m:{message_id}" else ""
            lines.append(f"{mark}[{item['at'][11:16]}] {who}: {item['text']}")
        room = "The room so far, oldest first:\n" + "\n".join(lines)
        brief = dict(topic.brief_json or {})
        if brief.get("where"):
            # What a colleague brought in is given, not the whole history.
            room = (
                "The discussion's brief: where it got to: "
                + str(brief.get("where"))
                + (f" Decided: {brief.get('decided')}" if brief.get("decided") else "")
                + (f" Open: {brief.get('open')}" if brief.get("open") else "")
                + "\n\n"
                + room
            )
        asked = session.get(RoomMessage, message_id)
        if asked is not None and asked.run_id is not None:
            room = self._task_room(session, asked.run_id) + "\n\n" + room
        if asked is not None and asked.thread_id is not None:
            room += "\n\n" + self._thread(session, asked.thread_id, message_id)
        return stable, system, room + "\n\nReply to the message marked >>>."

    def _record(self, session: Session, topic: Topic, agent: dict[str, Any]) -> dict[str, Any]:
        """What an agent answering here is told (4 Oct: they could not see the
        discussion's own work, their colleagues, or work asked elsewhere):

        - this discussion's work, newest first, the latest ones with their
          answer at length, finished or not (a cancelled task's draft, else its
          research);
        - the colleagues in the office, with their roles;
        - the office's other recent work, each with what it concluded.
        """

        project = session.get(Project, topic.project_id) if topic.project_id else None
        rows = session.execute(
            select(Run, Task.objective, Task.title)
            .join(Task, Task.id == Run.root_task_id)
            .where(runs_of(topic))
            .order_by(Task.created_at.desc())
            .limit(RECENT_TASKS)
        ).all()
        tasks = []
        for index, (run, objective, title) in enumerate(rows):
            entry: dict[str, Any] = {
                "task": title_of(title, objective),
                "asked": clip(objective, 400),
                "status": run.status,
                "started": run.started_at,
                "finished": run.completed_at,
            }
            if run.error:
                entry["error"] = clip(run.error, 300)
            if index < len(ANSWER_CHARS):
                answer = parent_answer(session, self.store, run)
                if answer:
                    entry["answer"] = json.dumps(answer, ensure_ascii=False, default=str)[
                        : ANSWER_CHARS[index]
                    ]
                    if run.status not in ("COMPLETED", "CLOSED"):
                        entry["answer_is"] = (
                            "a draft or the research so far: the task did not finish"
                        )
            tasks.append(entry)
        waiting = session.scalar(
            select(func.count(Approval.id))
            .join(Run, Run.id == Approval.run_id)
            .where(runs_of(topic), Approval.status == "PENDING")
        )
        return {
            "topic": topic.title,
            "project": project.name if project else None,
            "description": clip(project.description, 1_000) if project else "",
            "tasks": tasks,
            "approvals_waiting": int(waiting or 0),
        }

    @staticmethod
    def _colleagues(session: Session, agent: dict[str, Any]) -> str:
        """Everyone in the office and what they do, so a question about the
        team is answered from who is on it rather than "I only see myself"."""
        lines = []
        for other in capture_agent_snapshot(session)["agents"][:COLLEAGUES]:
            role = ROLE_NAME.get(str(other.get("role")), str(other.get("role")))
            you = " (you)" if other.get("profile_id") == agent.get("profile_id") else ""
            does = clip(str(other.get("instructions") or ""), 200)
            model = other.get("model_id") or "the office's default model"
            lines.append(f"- {other['name']}{you}: {role}, on {model}. {does}".rstrip())
        return "Your colleagues in the office:\n" + "\n".join(lines)

    def _elsewhere(self, session: Session, topic: Topic) -> str:
        """The office's most recent work asked anywhere else, with what each
        concluded: "what did that comparison find?" is answerable from here."""

        query = (
            select(Run, Task.objective, Task.title)
            .join(Task, Task.id == Run.root_task_id)
            .where(~runs_of(topic))
        )
        across = session.scalar(select(OfficeSettings.related_across_lines).limit(1))
        own = session.get(Project, topic.project_id) if topic.project_id else None
        if own is not None and not across:
            # A project's discussion sees its own business line's work, and a
            # project in no line only its own. A discussion in no project
            # is the owner talking to the whole office: it sees all of it.
            if own.venture_id is None:
                return ""
            query = query.join(Project, Project.id == Run.project_id).where(
                Project.venture_id == own.venture_id
            )
        rows = session.execute(query.order_by(Task.created_at.desc()).limit(ELSEWHERE)).all()
        lines = []
        for run, objective, title in rows:
            where = session.get(Topic, run.topic_id) if run.topic_id else None
            project = session.get(Project, run.project_id)
            asked_in = where.title if where else (project.name if project else "")
            concluded = _concluded(session, self.store, run)
            when = run.completed_at.date().isoformat() if run.completed_at else "not finished"
            lines.append(
                f"- {title_of(title, objective)} ({run.status}, {when}; asked in “{asked_in}”)"
                + (f": {concluded}" if concluded else "")
            )
        if not lines:
            return ""
        return "The office's other recent work, newest first:\n" + "\n".join(lines)

    @staticmethod
    def _thread(session: Session, thread_id: int, message_id: int) -> str:
        """The thread the owner is speaking in: its first message and replies.
        The answer belongs to this thread, not to the room at large."""
        rows = [session.get(RoomMessage, thread_id)] + list(
            session.scalars(
                select(RoomMessage)
                .where(RoomMessage.thread_id == thread_id)
                .order_by(RoomMessage.id)
                .limit(HISTORY)
            )
        )
        lines = []
        for row in rows:
            if row is None:
                continue
            mark = ">>> " if row.id == message_id else ""
            lines.append(f"{mark}{row.author_name}: {clip(row.body, 600)}")
        return (
            "The message marked >>> is said in a thread. Answer within the thread; "
            "the thread so far, oldest first:\n" + "\n".join(lines)
        )

    def _task_room(self, session: Session, run_id: UUID) -> str:
        """The task the owner asks about: what was asked, where it stands, its
        answer, and what its agents said to each other while they worked."""
        row = session.execute(
            select(Run, Task.objective, Task.title)
            .join(Task, Task.id == Run.root_task_id)
            .where(Run.id == run_id)
        ).first()
        if row is None:
            return ""
        from app.follow_up import parent_answer

        run, objective, title = row
        # Read from what the task wrote down: its state does not keep its answer.
        answer = parent_answer(session, self.store, run)
        posts = [
            team_post_view(post)
            for post in session.scalars(
                select(TeamPost)
                .where(TeamPost.run_id == run_id)
                .order_by(TeamPost.created_at.desc(), TeamPost.id.desc())
                .limit(TASK_POSTS)
            )
        ][::-1]
        said = "\n".join(room_line(post) for post in posts) or "(nothing posted)"
        return (
            f"The owner is asking about one task: {title_of(title, objective)}\n"
            f"Asked: {clip(objective, 600)}\nStatus: {run.status}\n"
            + (
                "Its answer (JSON, cut): "
                f"{json.dumps(answer, default=str, ensure_ascii=False)[: ANSWER_CHARS[0]]}\n"
                if answer
                else ""
            )
            + f"What its agents said to each other while they worked, oldest first:\n{said}"
        )

    @staticmethod
    def _tokens_today(session: Session, agent: dict[str, Any]) -> int:
        if not agent.get("profile_id"):
            return 0
        start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
        used = session.scalar(
            select(func.sum(RoomMessage.tokens)).where(
                RoomMessage.author_profile_id == UUID(agent["profile_id"]),
                RoomMessage.created_at >= start,
            )
        )
        return int(used or 0)

    def _store_office(self, topic_id: UUID, message_id: int, text: str) -> None:
        """The office speaking for itself: free, and never a model's words."""
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
                    run_id=asked.run_id if asked is not None else None,
                    thread_id=asked.thread_id if asked is not None else None,
                )
            )


# A discussion's context: the project record, colleagues, attachments, wall.
def allowance_used_up(name: str) -> str:
    """Says what happened and what to do, instead of only that a reply will not come."""
    return (
        f"{name}'s room allowance for today is used up; it resets at midnight UTC. To go on now, "
        "raise Chat tokens per bot per day in the office's Settings."
    )


ROOM_CONTEXT_CHARS = 15_000
OUT_OF_REACH = (
    "Their sign-in is not the problem: connected tools work only in a project they are granted "
    "to. If the owner asks for one, say so, and that they can place this discussion in a project "
    "that has it, or grant it to this one under Tools → Manage access.\n"
)


class _RoomModel:
    """The agent's own model, with its backup, as the lead's completer in a room."""

    def __init__(self, replies: RoomReplies, agent: dict[str, Any]) -> None:
        self.replies = replies
        self.agent = agent

    async def complete(
        self, model_alias: str, messages: list[dict[str, Any]], **options: Any
    ) -> Completion:
        return await self.replies.complete(self.agent, messages, tools=options.get("tools"))

"""The office side of inbound chat requests: owner mapping, routing, runs."""

import asyncio
import json
import logging
import re
from collections import deque
from collections.abc import Callable, Coroutine, Hashable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app import ideas, watches
from app.agent_profiles import capture_agent_snapshot
from app.archive import protection
from app.archive.tools import archive_specs
from app.calendars.store import agent_view
from app.channels import slack_format
from app.channels.approving import decide
from app.channels.base import ChannelError, OutboundMessage
from app.channels.briefs import save_brief
from app.channels.conversation import (
    ROLE_OUTPUT,
    agent_persona,
    build_messages,
    estimate_tokens,
    used_tokens,
)
from app.channels.inbound import InboundHandler, InboundRequest, Pairing
from app.channels.lookup import (
    ANSWER_RULES,
    MAX_PICTURES,
    PLAN,
    ChatAnswer,
    material,
    parse_plan,
    picture_parts,
    run_calls,
)
from app.channels.members import owner_of, role_of, set_owner
from app.channels.progress import origin_destination, stuck_on
from app.channels.registry import adapter_for
from app.channels.socket_mode import STARTUP_SECONDS, listen
from app.channels.threads import DatabaseThreads
from app.config import Settings
from app.connection_completer import completer_for_entry
from app.credentials import resolve_credential
from app.db.models import (
    AgentProfile,
    ChannelApp,
    ChannelInstallation,
    ChannelUsage,
    Project,
    Run,
    Task,
)
from app.followups import service as follow_ups
from app.followups.tools import follow_up_specs
from app.gateway.client import GatewayError
from app.judgments import JudgmentUnavailable, build_judge
from app.lead.chat import USED_UP, ChatAsk, chat_reply, office_lookup
from app.live_build import live_view
from app.model_calls import KeptCompleter, ModelCalls
from app.office_models import can_see, office_models
from app.office_settings import chat_allowance, paused
from app.orchestrator.approval_store import ApprovalStore
from app.orchestrator.graph import ANALYSIS_WORKFLOW, RESEARCH_WORKFLOW
from app.orchestrator.runner import RunRepository
from app.project_routing import Catalog, catalog
from app.run_actions import INSTRUCTABLE, ActionRefused, RunActions
from app.run_start import start_run
from app.runtime.worker import RunWorker
from app.status import office_status

logger = logging.getLogger(__name__)
SEEN_EVENTS = 1000
RECONCILE_SECONDS = 30
RECENT_RUNS = 8
# How far ahead a conversation sees the owner's calendar: the week.
CHAT_CALENDAR_DAYS = 7
REPORT_EXCERPT = 3000
KIND = "slack"
# What the owner may share and the chat reads.
IMAGE_BYTES = 5 * 1024 * 1024
TEXT_BYTES = 200_000
TEXT_FILES = frozenset(
    {"text/plain", "text/csv", "text/markdown", "application/json", "text/x-markdown"}
)
# Why a button or a note could not act, said to the owner in Slack.
REFUSED = {
    "run_not_found": "That task is no longer in the office.",
    "run_not_cancellable": "It has already finished, so there is nothing to stop.",
    "run_not_accepting_instructions": "It is not working now, so it cannot take a note.",
    "run_not_paused": "It is not waiting on a stuck step any more.",
    "nothing_to_skip": "There is no stuck step to skip.",
    "raise_the_limit_first": "It is held at its spending limit. Raise the limit in the office "
    "first.",
    "not_finished": "It is still working; rework is for finished tasks.",
    "office_paused": "New work is switched off in the office at the moment.",
    "monthly_limit_reached": "The office has reached its monthly spending limit.",
    "revision_target_ambiguous": "I could not tell what to rework. Reply here with what to change.",
    "revision_target_unavailable": "The office could not decide what to rework just now. "
    "Try again in a minute.",
}
# Lets one app post under an agent's name; without it the name is written in.
CUSTOMIZE_SCOPE = "chat:write.customize"


class AgentUnavailable(Exception):
    """The bot's agent is archived or has no enabled version."""


def _excerpt(value: Any) -> str:
    return json.dumps(value, default=str)[:REPORT_EXCERPT]


class SlackOffice:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        repository: RunRepository,
        worker: RunWorker,
        approvals: ApprovalStore,
        settings: Settings,
        app_id: UUID,
    ) -> None:
        # Each chat app (the office's, or an agent's) gets its own SlackOffice.
        self.app_id = app_id
        # The same actions the office's own buttons take, by the same rules.
        self.actions = RunActions(session_factory, repository, worker, approvals, settings)
        self.session_factory = session_factory
        self.repository = repository
        self.worker = worker
        self.approvals = approvals
        self.settings = settings
        self._seen: deque[str] = deque(maxlen=SEEN_EVENTS)

    def role_of(self, workspace: str, user: str) -> str | None:
        """What this person may do here: owner, member, or nothing at all."""
        with self.session_factory() as session:
            return role_of(session, KIND, workspace, user)

    def owner_of(self, workspace: str) -> str | None:
        with self.session_factory() as session:
            return owner_of(session, KIND, workspace)

    def set_owner(self, workspace: str, user: str) -> None:
        with self.session_factory() as session, session.begin():
            set_owner(session, KIND, workspace, user)

    def first_time(self, event_id: str) -> bool:
        if not event_id or event_id in self._seen:
            return False
        self._seen.append(event_id)
        return True

    def _catalog(self) -> Catalog:
        with self.session_factory() as session:
            return catalog(session)

    async def route(
        self, text: str, recent: list[dict[str, str]] | None = None
    ) -> dict[str, tuple[str, float]]:
        judge = build_judge(self.settings)
        if judge is None:
            return {}
        try:
            # What each project is about, not only its name.
            return await judge.route(text, self._catalog().described, recent)
        except JudgmentUnavailable:
            return {}

    def project_id(self, name: str) -> str | None:
        found = self._catalog().ids.get(name)
        return str(found) if found else None

    def paused_for_new_tasks(self) -> bool:
        with self.session_factory() as session:
            return paused(session, self.settings, "new_tasks")

    def open_project(self, name: str) -> str:
        """The project by that name, creating it if the office has none.

        Only ever reached after the owner has said yes to a named proposal, so
        nothing here is created on a model's say-so alone.
        """
        with self.session_factory() as session, session.begin():
            existing = session.scalars(select(Project).where(Project.name == name)).first()
            if existing is not None:
                if existing.status != "ACTIVE":
                    existing.status = "ACTIVE"
                return str(existing.id)
            project = Project(name=name, description="Created from Slack.")
            session.add(project)
            session.flush()
            return str(project.id)

    def _workflow(self, agent: dict[str, Any] | None) -> str:
        """A researcher's bot does the research itself; any other bot asks the office."""
        if agent is not None and agent["role"] == "researcher":
            return RESEARCH_WORKFLOW
        return ANALYSIS_WORKFLOW

    def work_offer(self, project: str) -> str:
        try:
            agent = self.agent_entry()
        except AgentUnavailable:
            agent = None
        if self._workflow(agent) == RESEARCH_WORKFLOW:
            assert agent is not None  # noqa: S101 - only agents research alone
            return (
                f"I can research this myself in {project}; the critic will check my sources "
                "before I report back here:"
            )
        return f"I can have the office run the full analysis in {project}:"

    def start(self, project_id: str, objective: str, channel: str, thread: str | None) -> str:
        try:
            agent = self.agent_entry()
        except AgentUnavailable:
            agent = None
        earlier = self._earlier_in_thread(UUID(project_id), channel, thread)
        run = start_run(
            self.session_factory,
            self.repository,
            self.worker,
            self.settings,
            UUID(project_id),
            objective,
            workflow=self._workflow(agent),
            # A change asked in a build's thread is its next version.
            deliverable={"shape": "software", "format": "onscreen"}
            if self._was_build(earlier)
            else None,
            # A thread is one line of work: "the six industries above" means
            # the ones the task before it in this thread found.
            continues_run_id=earlier,
            # Where this bot reports progress: the thread the owner asked in.
            origin={
                "kind": KIND,
                "app_id": str(self.app_id),
                "agent_profile_id": agent["profile_id"] if agent else None,
                "channel": channel,
                "thread": thread,
            },
        )
        return str(run.id)

    def _was_build(self, run_id: UUID | None) -> bool:
        if run_id is None:
            return False
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            state = dict(run.state_json or {}) if run else {}
        return (
            bool(state.get("build_folder")) or (state.get("plan") or {}).get("task_kind") == "build"
        )

    def thread_project(self, channel: str, thread: str | None) -> tuple[str, str] | None:
        """The project of the latest work this office started in this thread."""
        if not thread:
            return None
        origin = Run.config_snapshot_json["origin"]
        with self.session_factory() as session:
            row = session.execute(
                select(Project.id, Project.name)
                .join(Run, Run.project_id == Project.id)
                .join(Task, Task.id == Run.root_task_id)
                .where(
                    Project.status == "ACTIVE",
                    origin["kind"].as_string() == KIND,
                    origin["channel"].as_string() == channel,
                    origin["thread"].as_string() == thread,
                )
                .order_by(Task.created_at.desc())
                .limit(1)
            ).first()
        return (str(row[0]), str(row[1])) if row else None

    def _earlier_in_thread(self, project_id: UUID, channel: str, thread: str | None) -> UUID | None:
        """The latest task this project started in this Slack thread, if any."""
        if not thread:
            return None
        origin = Run.config_snapshot_json["origin"]
        with self.session_factory() as session:
            return session.scalars(
                select(Run.id)
                .join(Task, Task.id == Run.root_task_id)
                .where(
                    Run.project_id == project_id,
                    origin["kind"].as_string() == KIND,
                    origin["channel"].as_string() == channel,
                    origin["thread"].as_string() == thread,
                )
                .order_by(Task.created_at.desc())
                .limit(1)
            ).first()

    def active_task(self, channel: str, thread: str | None) -> dict[str, Any] | None:
        """The latest task asked for in this thread, while it still takes notes."""
        if not thread:
            return None
        origin = Run.config_snapshot_json["origin"]
        with self.session_factory() as session:
            run = session.scalars(
                select(Run)
                .join(Task, Task.id == Run.root_task_id)
                .where(
                    origin["kind"].as_string() == KIND,
                    origin["channel"].as_string() == channel,
                    origin["thread"].as_string() == thread,
                )
                .order_by(Task.created_at.desc())
                .limit(1)
            ).first()
            if run is None or run.status not in INSTRUCTABLE:
                return None
            stuck = stuck_on(session, run.id) if run.status == "PAUSED" else None
            return {"run_id": str(run.id), "status": str(run.status), "stuck": stuck is not None}

    def note_task(self, task: dict[str, Any], text: str) -> str:
        """The owner's reply, given to the task: a hint when it is stuck, else a note."""
        run_id = UUID(task["run_id"])
        if self._building(run_id):
            # A build takes it at its agent's next step, in the same session.
            self.repository.add_control(run_id, "build_note", text)
            return "Passed to the coding agent. It takes it at its next step and carries on."
        try:
            if task["stuck"]:
                self.actions.help(run_id, "hint", text)
                return "Passed to the team as a hint. It goes on from where it was stuck."
            self.actions.control(run_id, "instruction", text)
        except ActionRefused as refused:
            return REFUSED.get(refused.code, "I could not pass that on: " + refused.code)
        if task["status"] == "PAUSED":
            return (
                "Noted for the team. The task is paused, so it takes the note when it is "
                "resumed in the office."
            )
        return "Passed to the team as a note. The next step takes it into account."

    def _building(self, run_id: UUID) -> bool:

        with self.session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                return False
            return bool(live_view(session, run, self.settings.engineering_root, None)["building"])

    async def act(self, action: str, run_id: str) -> str:
        """A task's button, pressed by the owner."""
        try:
            task = UUID(run_id)
        except ValueError:
            return "That button no longer points at a task."
        try:
            if action == slack_format.STOP:
                self.actions.control(task, "cancel")
                return "Stopping it. The work done so far is kept."
            if action == slack_format.SKIP:
                self.actions.help(task, "skip")
                return "Skipping that step; the task goes on without it."
            if action == slack_format.RETRY:
                self.actions.help(task, "retry")
                return "Trying that step again."
            if action == slack_format.SAVE:
                return save_brief(self.session_factory, task)
            if action == slack_format.REWORK:
                await self.actions.rework(task)
                return (
                    "Reworking it where it was weakest. The progress above follows it, and "
                    "the new answer comes here when it is done."
                )
        except ActionRefused as refused:
            return REFUSED.get(refused.code, "I could not do that: " + refused.code)
        return "I do not know that button."

    async def decide(self, action: str, value: str) -> tuple[bool, str]:
        """Approve or reject a low-risk proposal from a card's button."""
        return decide(self.approvals, self.worker, action, value)

    async def edit(self, channel: str, ts: str, text: str) -> None:
        with self.session_factory() as session:
            row = self._active_installation(session)
            ref = row.token_ref if row else None
        if ref is None:
            return
        try:
            await adapter_for(KIND).update(
                resolve_credential(ref, self.settings), channel, ts, OutboundMessage(text=text)
            )
        except ChannelError as error:
            # The answer itself still goes through; only the old buttons stay.
            logger.warning('{"event":"slack_edit_failed","code":"%s"}', error.code)

    async def status_line(self) -> str:
        with self.session_factory() as session:
            status = office_status(session, self.settings, self.worker.waiting_count)
        problems = [
            name
            for name, part in status.items()
            if isinstance(part, dict) and part.get("state") not in (None, "ok")
        ]
        queued = status["worker"]["queued"]
        if problems:
            return f"Needs attention: {', '.join(problems)}. {queued} run(s) queued."
        return f"All services ok. {queued} run(s) queued."

    def pending_count(self) -> int:
        return len(self.approvals.pending())

    def agent_entry(self) -> dict[str, Any] | None:
        """This bot's agent as a run would use it now, or None for the office bot."""
        with self.session_factory() as session:
            app = session.get(ChannelApp, self.app_id)
            if app is None or app.agent_profile_id is None:
                return None
            wanted = str(app.agent_profile_id)
            for entry in capture_agent_snapshot(session)["agents"]:
                if entry["profile_id"] == wanted:
                    return dict(entry)
        raise AgentUnavailable

    def facts(self, agent: dict[str, Any] | None = None) -> dict[str, Any]:
        """What the chat model may know: read-only, no credentials, no paths.

        An agent's bot sees only the runs that agent took part in, with its
        own role's output alongside the final report.
        """
        with self.session_factory() as session:
            names = {row.id: row.name for row in session.scalars(select(Project))}
            statement = (
                select(Run, Task.objective)
                .join(Task, Task.id == Run.root_task_id)
                .order_by(Run.started_at.desc().nulls_last())
                .limit(RECENT_RUNS)
            )
            if agent is not None:
                statement = statement.where(
                    Run.config_snapshot_json["agents"].contains(
                        [{"profile_id": agent["profile_id"]}]
                    )
                )
            runs = []
            for index, (run, objective) in enumerate(session.execute(statement).all()):
                entry: dict[str, Any] = {
                    "run": str(run.id)[:8],
                    "project": names.get(run.project_id, "unknown"),
                    "status": run.status,
                    "objective": objective[:400],
                    "started": run.started_at,
                    "finished": run.completed_at,
                }
                if run.error:
                    entry["error"] = run.error[:300]
                state = run.state_json or {}
                if index < 3:
                    if state.get("final_report"):
                        entry["result_excerpt"] = _excerpt(state["final_report"])
                    own = ROLE_OUTPUT.get(agent["role"]) if agent else None
                    if own and state.get(own):
                        entry["your_output_excerpt"] = _excerpt(state[own])
                runs.append(entry)
            # The owner's week, as far as they let agents see it: asked
            # "what is on my calendar?", the chat has to know.
            calendar = agent_view(session, datetime.now(UTC), days=CHAT_CALENDAR_DAYS)
        facts: dict[str, Any] = {
            "projects": sorted(name for name in names.values()),
            "approvals_waiting": self.pending_count(),
            "recent_runs": runs,
        }
        facts["owner_calendar"] = calendar or (
            "No calendar is connected. The owner connects one under Office settings, Calendars."
        )
        return facts

    def _completer(self, agent: dict[str, Any] | None) -> tuple[Any, str]:
        """The agent's own model connection, else the office's model for its role."""
        if agent is not None and agent.get("connection_id"):
            completer = completer_for_entry(agent, self.settings, self.session_factory)
            return completer, f"connection:{agent['connection_id']}"
        role = agent["role"] if agent else "chief"
        alias = self.settings.model_aliases.get(role, self.settings.chief_model_alias)
        return office_models(self.settings, self.session_factory), alias

    def _tokens_today(self) -> int:
        with self.session_factory() as session:
            used = session.scalar(
                select(ChannelUsage.tokens).where(
                    ChannelUsage.app_id == self.app_id,
                    ChannelUsage.day == datetime.now(UTC).date(),
                )
            )
        return int(used or 0)

    def _record(self, agent: dict[str, Any] | None, tokens: int, cost: float | None) -> None:
        with self.session_factory() as session, session.begin():
            if session.get(ChannelApp, self.app_id) is None:
                # A conversation held in the browser has no channel app behind
                # it. Its model use is already counted where every run's is;
                # the per-app ledger is for channels.
                return
            statement = insert(ChannelUsage).values(
                id=uuid4(),
                app_id=self.app_id,
                agent_profile_id=UUID(agent["profile_id"]) if agent else None,
                day=datetime.now(UTC).date(),
                calls=1,
                tokens=tokens,
                estimated_cost=cost,
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ChannelUsage.app_id, ChannelUsage.day],
                    set_={
                        "calls": ChannelUsage.calls + 1,
                        "tokens": ChannelUsage.tokens + tokens,
                        "estimated_cost": func.coalesce(ChannelUsage.estimated_cost, 0)
                        + (cost or 0),
                        "updated_at": func.now(),
                    },
                )
            )

    def addressed_agent(self, text: str) -> dict[str, Any] | None:
        """The agent this message names, for a bot that speaks for the office.

        An agent's own bot is already that agent, so it never redirects. The
        match is on the agent's name or its role, at a word boundary, so a
        passing mention of a word inside another does not count.
        """
        if self.agent_profile_id() is not None:
            return None
        with self.session_factory() as session:
            snapshot = capture_agent_snapshot(session)["agents"]
        lowered = text.lower()
        for entry in snapshot:
            for label in (entry["name"], entry["role"]):
                if re.search(rf"(?<![\w-]){re.escape(str(label).lower())}(?![\w-])", lowered):
                    return dict(entry)
        return None

    def agent_profile_id(self) -> UUID | None:
        with self.session_factory() as session:
            app = session.get(ChannelApp, self.app_id)
            return app.agent_profile_id if app else None

    def paused_for_chat(self) -> bool:
        with self.session_factory() as session:
            return paused(session, self.settings, "chat")

    async def converse(
        self,
        history: list[dict[str, str]],
        text: str,
        agent: dict[str, Any] | None = None,
        files: tuple[dict[str, Any], ...] = (),
        chat_ref: str | None = None,
        by_owner: bool = False,
    ) -> ChatAnswer | str | None:
        """Answer, looking things up first when the question needs it.

        The model first says what the message needs: an answer, up to three
        read-only lookups, or a task to offer. Every model call counts against
        this bot's daily chat allowance; nothing here starts paid work.
        """
        if self.paused_for_chat():
            return (
                "Chat is switched off in the office at the moment. Open the office on your "
                "computer to turn it back on."
            )
        try:
            agent = agent or self.agent_entry()
        except AgentUnavailable:
            return "This agent is archived or has no enabled version, so it cannot answer."
        try:
            facts = self.facts(agent)
            facts["status"] = await self.status_line()
            if by_owner:
                with self.session_factory() as session:
                    facts["owner_to_dos"] = follow_ups.told(session, datetime.now(UTC))
        except Exception:
            logger.exception('{"event":"chat_facts_failed"}')
            return None
        completer, alias = self._completer(agent)
        # Pictures go only to a model published as reading them.
        sees = await can_see(completer, alias)
        if chat_ref:
            # Each call behind this answer is kept against its thread, for a while.
            role = str((agent or {}).get("role") or "chief")
            completer = KeptCompleter(
                completer, ModelCalls(self.session_factory), None, role, chat_ref=chat_ref
            )
        images, notes = await self._shared(files)
        if not images:
            # One lead sizes the answer itself; a shared picture takes the
            # path below, which can show it to the model.
            return await self._converse_led(
                history, text, agent, notes, completer, alias, facts, by_owner
            )
        messages = build_messages(facts, history, text, agent_persona(agent) if agent else None)
        if notes or (images and sees):
            messages[-1] = {
                "role": "user",
                "content": [
                    {"type": "text", "text": "\n".join([text, *notes]).strip() or "(no words)"},
                    *(picture_parts(images) if sees else []),
                ],
            }
        reserve = estimate_tokens(messages) * 3 + self.settings.chat_max_output_tokens * 2
        if self._tokens_today() + reserve > self._allowance():
            return USED_UP
        try:
            planned = await self._complete(
                completer, alias, [*messages, {"role": "system", "content": PLAN}], agent, 300
            )
            plan = parse_plan(planned)
            if plan["do"] == "task":
                return ChatAnswer(text="", task=str(plan["objective"]))
            if plan["do"] == "watch":
                return ChatAnswer(text="", watch=plan["watch"])
            if plan["do"] == "lookup":
                found = await run_calls(plan["calls"], *self._lookup_tools())
                answer = await self._complete(
                    completer,
                    alias,
                    [
                        *messages,
                        {"role": "system", "content": ANSWER_RULES},
                        {"role": "user", "content": material(found, sees)},
                    ],
                    agent,
                    self.settings.chat_max_output_tokens,
                )
                shots = [
                    (f"looked-at-{n + 1}.jpg", item.picture)
                    for n, item in enumerate(i for i in found if i.picture)
                    if item.picture
                ][:MAX_PICTURES]
                return ChatAnswer(text=answer, files=shots) if answer else None
            answer = await self._complete(
                completer, alias, messages, agent, self.settings.chat_max_output_tokens
            )
        except GatewayError as error:
            logger.warning('{"event":"chat_model_failed","code":"%s"}', error.code)
            return None
        return answer or None

    def _allowance(self) -> int:
        with self.session_factory() as session:
            return chat_allowance(session, self.settings)

    async def _converse_led(
        self,
        history: list[dict[str, str]],
        text: str,
        agent: dict[str, Any] | None,
        notes: list[str],
        completer: Any,
        alias: str,
        facts: dict[str, Any],
        by_owner: bool = False,
    ) -> ChatAnswer | str | None:
        """The lead answers: looks up, offers a task or a watch, within the allowance."""
        return await chat_reply(
            ChatAsk(
                allowance=self._allowance(),
                text="\n".join([text, *notes]).strip() or "(no words)",
                history=list(history),
                persona=agent_persona(agent) if agent else "",
                facts=facts,
                completer=completer,
                alias=alias,
                settings=self.settings,
                lookup=self._lookup_tools(),
                used_today=self._tokens_today,
                record=lambda tokens, cost: self._record(agent, tokens, cost),
                own_work=(
                    [
                        *follow_up_specs(self.session_factory, "slack"),
                        *self._history_specs(agent),
                    ]
                    if by_owner
                    else []
                ),
            )
        )

    async def _complete(
        self,
        completer: Any,
        alias: str,
        messages: list[dict[str, Any]],
        agent: dict[str, Any] | None,
        cap: int,
    ) -> str:
        """One model call, its tokens recorded against the bot's allowance."""
        reserve = estimate_tokens(messages) + cap
        completion = await completer.complete(alias, messages, max_tokens=cap)
        usage = completion.usage
        self._record(
            agent,
            used_tokens(usage.prompt_tokens, usage.completion_tokens, reserve),
            completion.estimated_cost,
        )
        return str(completion.content or "").strip()

    def _history_specs(self, agent: dict[str, Any] | None) -> list[Any]:
        """The owner's AI history, only if they chose to let it reach Slack."""
        with self.session_factory() as session:
            allowed = agent is not None and protection.may_reach(
                session, self.settings, agent, "slack"
            )
        if not allowed or agent is None:
            return []
        return archive_specs(self.session_factory, str(agent.get("name") or "An agent"), "slack")

    def _lookup_tools(self) -> tuple[Any, Any, Any]:
        """Search, read and look, each only where the office has it running."""
        return office_lookup(self.settings, self.session_factory)

    async def _shared(
        self, files: tuple[dict[str, Any], ...]
    ) -> tuple[list[tuple[str, bytes]], list[str]]:
        """What the owner shared with the message: pictures, and text files read.

        Shared files are the owner's own; they are still read only as material.
        A file that cannot be opened is said, never silently dropped.
        """
        if not files:
            return [], []
        with self.session_factory() as session:
            row = self._active_installation(session)
            ref = row.token_ref if row else None
        if ref is None:
            return [], []
        token = resolve_credential(ref, self.settings)
        images: list[tuple[str, bytes]] = []
        notes: list[str] = []
        for shared in files[:4]:
            name = str(shared.get("name") or "file")[:120]
            kind = str(shared.get("mimetype") or "")
            size = int(shared.get("size") or 0)
            readable = kind.startswith("image/") or kind in TEXT_FILES
            limit = IMAGE_BYTES if kind.startswith("image/") else TEXT_BYTES
            if not readable:
                notes.append(f"(The owner shared {name}, a {kind or 'file'} I cannot read here.)")
                continue
            if size > limit:
                notes.append(f"(The owner shared {name}, too large to read here.)")
                continue
            try:
                data = await adapter_for(KIND).download(token, str(shared.get("url") or ""), limit)
            except ChannelError as error:
                why = (
                    "the Slack app needs the files:read permission (Connections, Chat channels)"
                    if error.code == "missing_scope"
                    else error.code
                )
                notes.append(f"(The owner shared {name}, but I could not open it: {why}.)")
                continue
            if kind.startswith("image/"):
                images.append((name, data))
                notes.append(f"(The owner shared a picture: {name}.)")
            else:
                body = data.decode("utf-8", errors="replace")[:TEXT_BYTES]
                notes.append(f"The owner shared {name}:\n<<<\n{body}\n>>>")
        return images, notes

    def start_watch(self, watch: dict[str, Any], channel: str, thread: str | None) -> str:
        """A watch the owner pressed Start on in Slack: its press is the consent.

        It tells this bot's chat, in the thread it was asked in, as a hidden
        destination like a task's replies. Raises `WatchRejected` for an
        address or search the office will not watch.
        """

        with self.session_factory() as session, session.begin():
            found = origin_destination(session, {"app_id": str(self.app_id), "channel": channel})
            made = watches.create(
                session,
                title=str(watch.get("title") or ""),
                kind=str(watch.get("kind") or ""),
                target=str(watch.get("target") or ""),
                condition=str(watch.get("condition") or ""),
                every_minutes=int(watch.get("every_minutes") or 240),
                post_to_id=found[0] if found else None,
                post_thread=thread,
                now=datetime.now(UTC),
            )
            title = made.title
        return (
            f"👀 Watching *{title}*. The first look is now; I will post here when there is "
            "something to tell. Pause or stop it under Watching on the floor."
        )

    def keep_idea(self, text: str, ref: str, kind: str | None = None) -> bool:
        """Keep a thought from Slack under Ideas: no model, no allowance.
        False when that message was kept already."""
        with self.session_factory() as session, session.begin():
            try:
                ideas.keep(session, text, "slack", ref=ref, kind=kind)
            except ideas.IdeaRejected as rejected:
                if rejected.code == "already_kept":
                    return False
                raise
        return True

    def _token(self) -> str | None:
        with self.session_factory() as session:
            row = self._active_installation(session)
            ref = row.token_ref if row else None
        return resolve_credential(ref, self.settings) if ref else None

    async def message_text(self, channel: str, ts: str) -> str | None:
        """The message a 💡 was put on, or None when Slack will not show it."""
        token = self._token()
        if token is None:
            return None
        try:
            return str(await adapter_for(KIND).message_text(token, channel, ts))
        except ChannelError as error:
            logger.info('{"event":"idea_message_unread","code":"%s"}', error.code)
            return None

    async def whisper(self, channel: str, user: str, text: str) -> None:
        token = self._token()
        if token is None:
            return
        try:
            await adapter_for(KIND).whisper(token, channel, user, text)
        except ChannelError as error:
            logger.warning('{"event":"idea_whisper_failed","code":"%s"}', error.code)

    async def share(self, channel: str, thread: str | None, files: list[tuple[str, bytes]]) -> None:
        """Pictures it looked at, under its answer."""
        with self.session_factory() as session:
            row = self._active_installation(session)
            ref = row.token_ref if row else None
        if ref is None:
            return
        token = resolve_credential(ref, self.settings)
        for name, data in files:
            try:
                await adapter_for(KIND).upload(token, channel, thread, name, data)
            except ChannelError as error:
                logger.warning('{"event":"chat_picture_not_shared","code":"%s"}', error.code)
                return

    # Socket Mode configuration, read fresh so changes in the office apply.
    def _active_installation(self, session: Session) -> ChannelInstallation | None:
        return session.scalar(
            select(ChannelInstallation).where(
                ChannelInstallation.app_id == self.app_id, ChannelInstallation.status == "ACTIVE"
            )
        )

    def app_token(self) -> str | None:
        with self.session_factory() as session:
            app = session.get(ChannelApp, self.app_id)
            ref = app.app_token_ref if app else None
        return resolve_credential(ref, self.settings) if ref else None

    def bot_user_id(self) -> str | None:
        with self.session_factory() as session:
            row = self._active_installation(session)
            return row.bot_user_id if row else None

    async def reply(
        self,
        channel: str,
        thread: str | None,
        text: str,
        persona: dict[str, Any] | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        with self.session_factory() as session:
            row = self._active_installation(session)
            ref = row.token_ref if row else None
            scopes = list(row.scopes_json or []) if row else []
        if ref is None:
            return
        token = resolve_credential(ref, self.settings)
        name = persona["name"] if persona else None
        body = text
        if name and CUSTOMIZE_SCOPE not in scopes:
            # Without that scope Slack keeps the app's own name, so the agent
            # says who it is instead of appearing to be the office.
            body = f"*{name}*\n{text}"
            name = None
        await adapter_for(KIND).send(
            token,
            channel,
            OutboundMessage(
                text=body,
                thread=thread,
                username=name,
                icon_emoji=":bust_in_silhouette:",
                blocks=blocks,
            ),
        )


def listening_apps(session_factory: sessionmaker[Session]) -> set[UUID]:
    """Apps that should hold a connection: token set, installed, agent (if any) active."""
    with session_factory() as session:
        rows = session.execute(
            select(ChannelApp.id)
            .join(ChannelInstallation, ChannelInstallation.app_id == ChannelApp.id)
            .outerjoin(AgentProfile, AgentProfile.id == ChannelApp.agent_profile_id)
            .where(
                ChannelApp.kind == KIND,
                ChannelApp.app_token_ref.is_not(None),
                ChannelInstallation.status == "ACTIVE",
                (ChannelApp.agent_profile_id.is_(None)) | (AgentProfile.status == "ACTIVE"),
            )
        )
        return {row[0] for row in rows}


async def supervise(
    wanted: Callable[[], set[Hashable]],
    start: Callable[[Hashable, asyncio.Event], Coroutine[Any, Any, None]],
    stop: asyncio.Event,
    interval: float = RECONCILE_SECONDS,
) -> None:
    """Keep exactly one running task per wanted key; stop tasks no longer wanted."""
    running: dict[Hashable, tuple[asyncio.Task[None], asyncio.Event]] = {}
    try:
        while not stop.is_set():
            try:
                keys = wanted()
            except Exception:
                logger.exception('{"event":"inbound_reconcile_failed"}')
                keys = set(running)
            for key in list(running):
                task, own_stop = running[key]
                if key not in keys or task.done():
                    own_stop.set()
                    task.cancel()
                    del running[key]
            for key in keys - set(running):
                own_stop = asyncio.Event()
                running[key] = (asyncio.create_task(start(key, own_stop)), own_stop)
            try:
                await asyncio.wait_for(stop.wait(), interval)
            except TimeoutError:
                pass
    finally:
        for task, own_stop in running.values():
            own_stop.set()
            task.cancel()
        await asyncio.gather(*(task for task, _ in running.values()), return_exceptions=True)


async def run_inbound(
    make_office: Callable[[UUID], SlackOffice],
    session_factory: sessionmaker[Session],
    pairing: Pairing,
    stop: asyncio.Event,
) -> None:
    """One Socket Mode connection per app: the office bot and each agent bot."""

    async def start(app_id: Hashable, own_stop: asyncio.Event) -> None:
        assert isinstance(app_id, UUID)  # noqa: S101 - keys come from listening_apps
        office = make_office(app_id)
        # Each bot remembers its own threads, in the database, across restarts.
        handler = InboundHandler(
            office, office.reply, pairing, DatabaseThreads(session_factory, app_id)
        )

        async def handle(request: InboundRequest) -> None:
            try:
                await handler.handle(request)
            except Exception:
                logger.exception('{"event":"inbound_request_failed"}')

        await listen(office.app_token, office.bot_user_id, handle, own_stop)

    try:  # Let the office finish starting first; a shutdown ends the wait.
        await asyncio.wait_for(stop.wait(), STARTUP_SECONDS)
        return
    except TimeoutError:
        pass
    await supervise(lambda: set(listening_apps(session_factory)), start, stop)

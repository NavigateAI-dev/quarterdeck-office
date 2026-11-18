import asyncio
import logging
import os
import socket
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.agent_routing import (
    apply_role_models,
    apply_snapshot,
    apply_venture_doctrine,
    budget,
    correlate,
    guard,
    keep,
)
from app.agents import (
    AdvisorAgent,
    ChiefAgent,
    CriticAgent,
    PlannerAgent,
    ResearcherAgent,
    StrategistAgent,
    SummaristAgent,
)
from app.agents.code_reviewer import CodeReviewerAgent
from app.agents.plan_reviewer import PlanReviewerAgent
from app.agents.researcher import Retriever
from app.artifacts import ArtifactStore
from app.browser.agent import BrowserAgent
from app.budget import Budget, RunLedger, budget_from
from app.coder.handoff import CoderHandoff
from app.coder.observer import BuildObserver
from app.config import Settings
from app.db.models import InstalledSkill, Project, Run, Venture
from app.events import InvocationRecorder
from app.gateway.direct import Completer
from app.judgments import TypeSafeJudge, build_judge
from app.kernel import EventBus
from app.lead.run import execute_lead_run
from app.lead.work import lead_work
from app.lineage import judged_line, line_candidates
from app.mcp import store as mcp_store
from app.mcp.calling import McpTools
from app.mcp.client import McpError
from app.mcp.metering import ToolMeter, spending_refusal
from app.mcp.oauth import OAuthError
from app.model_calls import ModelCalls
from app.office_models import office_models
from app.office_settings import (
    MonthlyLimitReached,
    check_monthly_limit,
    effective,
)
from app.orchestrator.approval_store import ApprovalStore
from app.orchestrator.checkpoints import open_checkpointer
from app.orchestrator.dispatcher import RunNotClaimed, execute_run
from app.orchestrator.engineering import DisabledEngineering, EngineeringExecutor
from app.orchestrator.gate import DatabaseApprovalGate
from app.orchestrator.graph import LEAD_WORKFLOW, AgentSet, RunFailure
from app.orchestrator.runner import Dispatch, RunRepository
from app.policies.guard import GuardedFetcher, GuardedSearch
from app.policies.store import PolicyGate
from app.project_context import venture_doctrine
from app.project_settings import advisor_on, role_models, team_on
from app.related_work import recent_answers, related_to
from app.runtime.events import RunSettled
from app.runtime.job import Job
from app.runtime.model_stack import ModelStack
from app.runtimes.engineer import LocalExecutor
from app.runtimes.installer import PackageInstaller
from app.runtimes.isolated import IsolatedExecutor
from app.runtimes.publish import Publisher
from app.secret_store import SecretStoreError
from app.side_effects import SideEffects
from app.sources.client import ExchangeIndexer
from app.sources.retrieval import (
    Passage,
    granted_passages,
    granted_passages_exist,
    select_passages,
)
from app.tools.deepseek import with_deepseek_fallback
from app.tools.fetch import PageFetcher
from app.tools.fetch_client import ExchangeFetcher
from app.tools.forge import GitLabForge
from app.tools.pipeline import ToolSpec
from app.tools.web import build_search_tool
from app.workspace_tools import workspace_specs
from app.workspaces import Workspaces, space_of

logger = logging.getLogger("quarterdeck.worker")

RESOLVED_APPROVALS = ("APPROVED", "REJECTED", "REVISION_REQUESTED")


def build_publisher(settings: Settings) -> Publisher | None:
    """Publication is off unless explicitly enabled and fully configured."""
    if not settings.enable_github_pr or settings.gitlab_token is None:
        return None
    token = settings.gitlab_token.get_secret_value()
    return Publisher(
        forge=GitLabForge(
            base_url=str(settings.gitlab_base_url),
            project_path=settings.gitlab_project_path,
            token=token,
            timeout_seconds=settings.forge_timeout_seconds,
        ),
        base_url=str(settings.gitlab_base_url),
        project_path=settings.gitlab_project_path,
        token=token,
    )


def build_executor(settings: Settings, session_factory: Any = None) -> EngineeringExecutor:
    """Select the engineering executor.

    Code execution stays off unless explicitly enabled; the disabled executor
    still runs the permission checks so the boundary is identical either way.
    Coding is handed to the coder on the owner's Mac (Stage 36); the office
    runs only the checks, in the isolated runner. Without the database there
    is no coder to hand it to, and nothing runs.
    """
    if not settings.enable_code_execution:
        return DisabledEngineering()
    test_executor = (
        IsolatedExecutor(settings.engineering_root)
        if settings.engineering_test_runner == "isolated"
        else LocalExecutor()
    )
    if session_factory is not None:
        return CoderHandoff(
            session_factory=session_factory,
            engineering_root=settings.engineering_root,
            test_executor=test_executor,
            test_timeout_seconds=settings.codex_timeout_seconds,
            installer=PackageInstaller(settings.engineering_root),
        )
    # Without the database there is no coder to hand coding to.
    return DisabledEngineering()


def build_agents(
    gateway: Completer,
    settings: Settings,
    fetcher: PageFetcher | None = None,
    retriever: Retriever | None = None,
    link_judge: Any = None,
    advisor: bool = False,
    browser: bool = False,
) -> AgentSet:
    return AgentSet(
        planner=PlannerAgent(gateway, settings),
        researcher=ResearcherAgent(
            gateway,
            settings,
            build_search_tool(settings),
            fetcher=fetcher,
            retriever=retriever,
            link_judge=link_judge,
        ),
        strategist=StrategistAgent(gateway, settings),
        summarist=SummaristAgent(gateway, settings),
        critic=CriticAgent(gateway, settings),
        chief=ChiefAgent(gateway, settings),
        plan_reviewer=PlanReviewerAgent(gateway, settings),
        code_reviewer=CodeReviewerAgent(gateway, settings),
        advisor=AdvisorAgent(gateway, settings) if advisor else None,
        # The office's own browser, when the owner turned it on and it runs.
        browser=BrowserAgent(gateway, settings)
        if browser and settings.browser_exchange is not None
        else None,
    )


# How often a worker waiting for a free place looks again at how many it may
# use, so a place the owner adds is taken without waiting for a task to end.
PLACES_RECHECK_SECONDS = 2.0


def revisions_for(context: dict[str, Any] | None, configured: int) -> int:
    """How often the critic may send the work back by itself. Work started in a
    discussion revises none: a REVISE stops it for the owner, with the answer
    and the critic's points, and the owner chooses whether to spend more."""
    return 0 if (context or {}).get("from_discussion") else configured


# A run in one of these is still the worker's to finish; a crash fails it.
CRASHABLE = ("PENDING", "RUNNING", "RECOVERING")


class RunWorker:
    """Run executor: up to `places()` runs at once, the rest in order (MW).

    The queue holds only pointers to durable rows, so a lost queue entry costs
    a restart-time recovery scan, never the run itself. Running a run is still
    claimed in the database first, so the same run submitted twice runs once.
    """

    def __init__(
        self,
        settings: Settings,
        repository: RunRepository,
        approvals: ApprovalStore | None = None,
        session_factory: Any = None,
        events: EventBus | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.approvals = approvals
        self.session_factory = session_factory
        # Where the engine announces what happened; listeners are plugins.
        self.events = events
        self.models = ModelStack(settings, repository, session_factory)
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        # The runs executing now, and a signal when one of them ends.
        self.running: dict[asyncio.Task[None], Job] = {}
        self.freed = asyncio.Event()
        # A job taken from the queue that is waiting for a free place.
        self.holding = 0
        # Identifies this worker in a run's lease, so a run interrupted here
        # can be told apart from one another worker still holds.
        self.worker_id = f"{socket.gethostname()[:32]}:{os.getpid()}"

    # The role model stack lives in app.runtime.model_stack; these keep the
    # worker's own names for callers and tests.
    def _completer_for(self, entry: dict[str, Any]) -> Completer:
        return self.models.completer_for(entry)

    def _with_backups(self, job: Job, agents: AgentSet) -> None:
        self.models.with_backups(job, agents)

    def _downgrade_watch(self, job: Job, agents: AgentSet) -> Callable[[str], None] | None:
        return self.models.downgrade_watch(job, agents)

    def start(self) -> None:
        self.task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        tasks = ([self.task] if self.task is not None else []) + list(self.running)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.running.clear()

    @property
    def running_count(self) -> int:
        return len(self.running)

    @property
    def waiting_count(self) -> int:
        """Tasks waiting for a place, the one at the front included."""
        return self.queue.qsize() + self.holding

    def places(self) -> int:
        """How many runs may execute at once: the owner's choice, else configuration."""
        if self.session_factory is not None:
            try:
                with self.session_factory() as session:
                    return int(effective(session, self.settings)["runs_at_once"])
            except Exception:  # noqa: BLE001 - an unreadable setting falls back
                logger.warning('{"event":"runs_at_once_unreadable"}')
        return self.settings.max_concurrent_runs

    def month_spent(self) -> bool:
        """Whether the office's monthly spending limit is already reached."""
        if self.session_factory is None:
            return False
        with self.session_factory() as session:
            try:
                check_monthly_limit(session, self.settings)
            except MonthlyLimitReached:
                return True
        return False

    def _hand_browser(self, agents: AgentSet, dispatch: Dispatch, gate: Any) -> None:
        """The browsing agent's rules and record, for this run.

        Each step it takes is an event on the task, and its screenshot is kept
        in the task's own folder, so the owner can see what the browser saw.
        """
        browser: Any = getattr(agents, "browser", None)
        if browser is None:
            return
        browser.gate = gate
        browser.run_id = dispatch.run_id
        browser.project_id = dispatch.project_id
        folder = self.settings.workspace_root / "browser" / str(dispatch.run_id)
        browser.replay_dir = folder

        def kept(step: dict[str, Any], shot: bytes | None) -> None:
            name = ""
            if shot:
                folder.mkdir(parents=True, exist_ok=True)
                count = len(list(folder.glob("*.jpg"))) + 1
                name = f"{count}.jpg"
                (folder / name).write_bytes(shot)
            self.repository.add_event(dispatch.run_id, "browser_step", step | {"shot": name})

        browser.on_step = kept
        # Notes the owner sends while it works, and steps they skip.
        run_id = dispatch.run_id
        browser.new_notes = lambda: self.repository.take_new_instructions(run_id, "research")
        browser.skipped = lambda: self.repository.skipped_steps(run_id)

    def _watched_executor(self, run_id: UUID) -> EngineeringExecutor:
        """The executor for this task, its build reported live and stoppable."""
        executor = build_executor(self.settings, self.session_factory)
        if isinstance(executor, CoderHandoff):
            repository = self.repository

            def said(item: dict[str, Any]) -> None:
                repository.add_event(run_id, "build_step", dict(item))

            executor.observer = BuildObserver(
                step=said,
                stop=lambda: (repository.pending_stop(run_id) or {}).get("kind") == "cancel",
                notes=lambda: repository.pending_build_notes(run_id),
                take=lambda ids: repository.take_build_notes(run_id, ids),
            )
        return executor

    def submit(self, job: Job) -> None:
        self.queue.put_nowait(job)

    def resume_for_review(self, run_id: UUID, override: dict[str, Any]) -> None:
        """Continue a run after a person has reviewed it."""
        self.repository.requeue(run_id)
        described = self.repository.describe([run_id])
        if not described:
            return
        dispatch, objective, context = described[0]
        self.submit(
            Job(
                dispatch=dispatch,
                objective=objective,
                context=context,
                resume=True,
                state_override=override,
                resume_node="critique",
            )
        )

    def restart(self, run_id: UUID) -> None:
        """Run a task again from its plan, with the context it was given."""
        described = self.repository.describe([run_id])
        if not described:
            return
        dispatch, objective, context = described[0]
        self.submit(Job(dispatch=dispatch, objective=objective, context=context))

    def resume_paused(self, run_id: UUID) -> bool:
        """Continue a run the owner paused, from its checkpoint when it has one."""
        if not self.repository.requeue(run_id):
            return False
        described = self.repository.describe([run_id])
        if not described:
            return False
        dispatch, objective, context = described[0]
        self.submit(
            Job(
                dispatch=dispatch,
                objective=objective,
                context=context,
                resume=self.repository.has_started(run_id),
            )
        )
        return True

    def resume(self, run_id: UUID) -> None:
        """Continue a run whose approval has been decided.

        The decision is already durable at this point, so a lost resume costs a
        restart-time recovery scan rather than the decision itself.
        """
        self.repository.requeue(run_id)
        described = self.repository.describe([run_id])
        if not described:
            return
        dispatch, objective, context = described[0]
        # Re-enter at the approval node so its outgoing edge runs again with the
        # decision now recorded. Without this the graph is already at its end.
        self.submit(
            Job(
                dispatch=dispatch,
                objective=objective,
                context=context,
                resume=True,
                state_override={"status": "RUNNING"},
                resume_node="approval",
            )
        )

    async def _consume(self) -> None:
        """Take the next job when a place is free, and run it beside the others."""
        while True:
            job = await self.queue.get()
            self.holding = 1
            while len(self.running) >= max(1, self.places()):
                self.freed.clear()
                try:
                    await asyncio.wait_for(self.freed.wait(), PLACES_RECHECK_SECONDS)
                except TimeoutError:
                    pass
            self.holding = 0
            task = asyncio.create_task(self._run(job))
            self.running[task] = job

    async def _run(self, job: Job) -> None:
        try:
            if job.resume_node is None and self.month_spent():
                # It waited its turn and the month's money ran out meanwhile:
                # it pauses and says why, and continues when the owner resumes.
                run_id = job.dispatch.run_id
                self.repository.stop(
                    run_id,
                    "PAUSED",
                    only_from=("PENDING", "RECOVERING"),
                    reason="monthly_limit_reached",
                )
                return
            await self._execute(job)
        except (RunFailure, RunNotClaimed) as exc:
            logger.warning('{"event":"run_not_completed","reason":"%s"}', type(exc).__name__)
        except Exception as error:
            logger.exception('{"event":"run_worker_error"}')
            await self._fail_crashed(job, error)
        finally:
            current = asyncio.current_task()
            if current is not None:
                self.running.pop(current, None)
            self.freed.set()
            self.queue.task_done()

    async def _fail_crashed(self, job: Job, error: Exception) -> None:
        """A task that crashed ends as failed and says why.

        Only logging it left the task "Running" while recovery replayed the
        same crash every few minutes. A shutdown's cancellation is not an
        Exception and stays recoverable; a task that already ended is left as
        it is.
        """
        run_id = job.dispatch.run_id
        code = f"unexpected_error:{type(error).__name__}"
        try:
            if not self.repository.fail(run_id, None, code, only_from=CRASHABLE):
                return
            self.repository.add_event(
                run_id, "run_failed", {"code": code, "detail": str(error)[:300]}
            )
            self.repository.release_lease(run_id)
            await self._notify(run_id, "FAILED")
        except Exception:
            logger.exception('{"event":"crashed_run_not_failed"}')

    async def _hand_tools(
        self,
        agents: Any,
        dispatch: Dispatch,
        gate: PolicyGate,
        ledger: RunLedger | None = None,
    ) -> None:
        """The researcher gathers with the granted tools; the critic checks with
        them. One instance, so every call either makes lands in one record, and
        every priced call is counted against this run's ledger."""
        tools = await self._mcp_tools(dispatch, gate, ledger)
        for role in ("researcher", "critic"):
            agent = getattr(agents, role, None)
            if agent is not None:
                agent.mcp = tools

    async def _mcp_tools(
        self, dispatch: Dispatch, gate: PolicyGate, ledger: RunLedger | None = None
    ) -> McpTools | None:
        """The tools this run's project was granted, behind every check.

        Credentials are resolved here, in the office, and reach a server in a
        header and nowhere else; a sign-in about to lapse is refreshed first. A
        server whose credential is gone, or whose sign-in could not be renewed,
        is left out and the run says so, rather than failing every call.
        """
        if self.session_factory is None:
            return None
        allow_list = mcp_store.allow_hosts(self.settings)
        with self.session_factory() as session:
            granted = mcp_store.reachable_for(session, dispatch.project_id)
        servers = []
        for server, reference in granted:
            try:
                token = await mcp_store.bearer_for(reference, self.settings, allow_list)
            except SecretStoreError:
                reason = "credential_missing"
            except OAuthError:
                reason = "sign_in_required"
                with self.session_factory() as session, session.begin():
                    mcp_store.record_failure(session, server.server_id, McpError(reason))
            else:
                servers.append(replace(server, token=token))
                continue
            self.repository.add_event(
                dispatch.run_id, "mcp_unavailable", {"server": server.name, "reason": reason}
            )
        if not servers:
            return None
        factory, settings = self.session_factory, self.settings

        def refusal() -> str | None:
            with factory() as session:
                return spending_refusal(session, settings, dispatch.project_id)

        return McpTools(
            servers,
            gate,
            dispatch.run_id,
            dispatch.project_id,
            mcp_store.allow_hosts(self.settings),
            record=lambda kind, data: self.repository.add_event(dispatch.run_id, kind, data),
            # Each call written down before it is sent.
            writes=SideEffects(factory).for_run(
                dispatch.run_id,
                lambda kind, data: self.repository.add_event(dispatch.run_id, kind, data),
            ),
            meter=ToolMeter(
                ledger,
                self.repository.record_usage,
                refusal,
                run_id=dispatch.run_id,
                task_id=dispatch.task_id,
                project_id=dispatch.project_id,
            ),
        )

    async def _share_related(
        self, dispatch: Dispatch, objective: str, context: dict[str, Any], judge: Any
    ) -> list[str]:
        """Tell this task what the rest of the office found that bears on it.

        Its own project's answers are candidates too: the project context gives
        them as a line each, and this gives the relevant ones in full. The task
        it continues is left out, being told already. `context` is updated in
        place and stored, so the task page shows it.
        """
        if self.session_factory is None:
            return []
        exclude = {dispatch.run_id}
        parent = self.repository.continued_run(dispatch.run_id)
        if parent is not None:
            exclude.add(parent)
        with self.session_factory() as session:
            found = recent_answers(session, self.repository.store, exclude, dispatch.project_id)
        chosen = await related_to(judge, objective, found)
        told = [item.text for item in chosen]
        context["related"] = told
        # Earlier links of its line of work, beyond the task it continues
        # never what it is already told as related work.
        with self.session_factory() as session:
            links = line_candidates(
                session,
                self.repository.store,
                dispatch.run_id,
                {item.run_id for item in chosen},
            )
        line = await judged_line(judge, objective, links)
        if line:
            context["lineage"] = [link.text for link in line]
        self.repository.set_context(dispatch.run_id, context)
        if chosen:
            self.repository.add_event(
                dispatch.run_id, "related_work", {"runs": [str(item.run_id) for item in chosen]}
            )
        if line:
            self.repository.add_event(
                dispatch.run_id, "lineage_told", {"runs": [str(link.run_id) for link in line]}
            )
        return told

    def _skill_library(self, project_id: UUID) -> dict[str, dict[str, Any]]:
        """The enabled skills a plan step may use: the latest version of
        each, and only those whose permissions this project's runs have."""
        if self.session_factory is None:
            return {}
        available = self._permissions(project_id)
        latest: dict[str, tuple[tuple[int, ...], dict[str, Any]]] = {}
        with self.session_factory() as session:
            # Packs: a skill in any business line's pack is offered only
            # to that line's tasks; a skill in no pack, to every task.
            project = session.get(Project, project_id)
            line = project.venture_id if project is not None else None
            packed: dict[str, set[Any]] = {}
            for venture in session.scalars(select(Venture).where(Venture.status == "ACTIVE")):
                for held in venture.skill_ids_json or []:
                    packed.setdefault(str(held), set()).add(venture.id)
            rows = session.scalars(select(InstalledSkill).where(InstalledSkill.status == "ENABLED"))
            for row in rows:
                manifest = dict(row.manifest_json or {})
                if not set(manifest.get("permissions") or []) <= available:
                    continue
                if row.skill_id in packed and line not in packed[row.skill_id]:
                    continue
                version = tuple(int(part) for part in row.version.split(".") if part.isdigit())
                if row.skill_id in latest and latest[row.skill_id][0] >= version:
                    continue
                source = manifest.get("source") or {}
                latest[row.skill_id] = (
                    version,
                    {
                        "name": manifest.get("name") or row.skill_id,
                        "description": manifest.get("description") or "",
                        "instructions": manifest.get("instructions") or "",
                        "roles": list(manifest.get("roles") or []),
                        "source": source.get("repo") or manifest.get("publisher") or "",
                    },
                )
        return {skill_id: entry for skill_id, (_, entry) in sorted(latest.items())}

    def _permissions(self, project_id: UUID) -> set[str]:
        """What a skill may rely on in this run: web search if a provider is
        configured, local documents if the project has granted sources."""
        available = set()
        if self.settings.search_provider != "static":
            available.add("web_search")
        if self.session_factory is not None:
            with self.session_factory() as session:
                if granted_passages_exist(session, project_id):
                    available.add("local_documents")
        return available

    def _retriever(self, judge: TypeSafeJudge | None) -> Retriever | None:
        """Passages from the project's granted sources, grants checked per call."""
        if self.session_factory is None:
            return None
        factory = self.session_factory

        async def retrieve(project_id: UUID, query: str) -> list[Passage]:
            with factory() as session:
                passages = granted_passages(session, project_id)
            if not passages:
                return []
            chosen, _ = await select_passages(query, passages, judge)
            return chosen

        return retrieve

    def _workspace_tools(self, job: Job) -> list[ToolSpec]:
        """The folders of the run's space, read live."""
        if self.session_factory is None:
            return []
        run_id = job.dispatch.run_id
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            space = space_of(session, job.dispatch.project_id, run.topic_id if run else None)
        if space is None:
            return []
        store = Workspaces(self.session_factory)
        return workspace_specs(
            ExchangeIndexer(self.settings.sources_exchange),
            lambda: store.granted(space),
            lambda kind, data: self.repository.add_event(run_id, kind, data),
        )

    async def _execute(self, job: Job) -> None:
        database_url = self.settings.database_url.get_secret_value()
        async with (
            office_models(self.settings, self.session_factory, job.dispatch.project_id) as gateway,
            PageFetcher(self.settings.fetch_timeout_seconds) as fetcher,
            open_checkpointer(database_url) as checkpointer,
        ):
            gate = None
            if self.approvals is not None and self.session_factory is not None:
                gate = DatabaseApprovalGate(
                    job.dispatch,
                    self.approvals,
                    self.session_factory,
                    ArtifactStore(self.settings.workspace_root),
                    self.settings,
                )
            recorder = InvocationRecorder(
                run_id=job.dispatch.run_id,
                task_id=job.dispatch.task_id,
                sink=self.repository.record_usage,
            )
            ledger = self._ledger(job)
            judge = build_judge(self.settings)
            if not job.resume and "related" not in job.context:
                # Once, when the task first starts: a resumed task keeps what
                # it was told rather than being told something new mid-work.
                await self._share_related(job.dispatch, job.objective, job.context, judge)
            policy_gate = PolicyGate(self.session_factory) if self.session_factory else None
            reader = self._reader(job, fetcher, policy_gate)
            on_call = False
            as_team = False
            browsing = False
            if self.session_factory is not None:
                with self.session_factory() as session:
                    on_call = advisor_on(session, self.settings, job.dispatch.project_id)
                    as_team = team_on(session, self.settings, job.dispatch.project_id)
                    browsing = bool(effective(session, self.settings)["browser_on"])
            agents = build_agents(
                gateway,
                self.settings,
                reader,
                retriever=self._retriever(judge),
                link_judge=judge,
                advisor=on_call,
                browser=browsing,
            )
            agents, lead_doctrine = await self._prepare(job, agents, policy_gate, ledger, recorder)
            workflow = self.repository.workflow(job.dispatch.run_id)
            try:
                if workflow == LEAD_WORKFLOW:
                    # One lead sizing the work, on the same
                    # routed, backed, guarded and budgeted roles.
                    led = await execute_lead_run(
                        job.dispatch,
                        self.repository,
                        agents,
                        job.objective,
                        job.context,
                        work=lead_work(agents) + self._workspace_tools(job),
                        worker_id=self.worker_id,
                        doctrine=lead_doctrine,
                        resume=job.resume,
                    )
                    self.repository.release_lease(job.dispatch.run_id)
                    await self._notify(job.dispatch.run_id, str(led.get("status")))
                    return
                state = await execute_run(
                    job.dispatch,
                    self.repository,
                    agents,
                    checkpointer,
                    revisions_for(job.context, self.settings.max_revision_loops),
                    job.objective,
                    job.context,
                    resume=job.resume,
                    state_override=job.state_override,
                    resume_node=job.resume_node,
                    gate=gate,
                    executor=self._watched_executor(job.dispatch.run_id),
                    judge=judge,
                    max_parallel=self.settings.max_parallel_subtasks,
                    workflow=workflow,
                    worker_id=self.worker_id,
                    before_step=self._downgrade_watch(job, agents),
                    team=as_team,
                )
            except RunFailure:
                self.repository.release_lease(job.dispatch.run_id)
                await self._notify(job.dispatch.run_id, "FAILED")
                raise
            # The run is no longer executing here, whatever it decided.
            self.repository.release_lease(job.dispatch.run_id)
            await self._notify(job.dispatch.run_id, str(state.get("status")))

    def _ledger(self, job: Job) -> RunLedger:
        """The run's budget, as spent so far, raised at once when the owner raises it."""
        spent_so_far = self.repository.usage(job.dispatch.run_id)["spent"]
        return RunLedger(
            budget_from(
                self.repository.budget(job.dispatch.run_id),
                Budget.model_validate(self.settings.default_budget),
            ),
            self.settings.llm_max_output_tokens,
            # A resumed run continues from what it has actually spent, in
            # money as well as tokens. Without the cost carried over, a
            # resume would hand the run a fresh cost ceiling and the owner's
            # raise would mean nothing.
            spent_tokens=self.repository.spent_tokens(job.dispatch.run_id),
            spent_cost=spent_so_far.get("cost_usd") or 0.0,
            cost_reported=bool(spent_so_far.get("cost_reported", True)),
            # A limit raised while the task runs applies at once.
            refresh=lambda: budget_from(
                self.repository.budget(job.dispatch.run_id),
                Budget.model_validate(self.settings.default_budget),
            ),
        )

    def _reader(self, job: Job, fetcher: Any, policy_gate: Any) -> Any:
        """How the task reads pages, when page reading is on."""
        reader: Any = fetcher if self.settings.enable_page_fetch else None
        if reader is not None and self.settings.fetch_exchange is not None:
            # Pages are read by a service with no credentials, never here.
            # One reader per task: what it read is kept for the task,
            # a page only a browser can show is opened in the office's own
            # browser when browsing is on, and every read is recorded
            # for the Performance view.
            run_id = job.dispatch.run_id
            repository = self.repository
            with_browser = False
            if self.session_factory is not None:
                with self.session_factory() as session:
                    with_browser = bool(effective(session, self.settings)["browser_on"])
            reader = ExchangeFetcher(
                self.settings.fetch_exchange,
                browser_exchange=self.settings.browser_exchange if with_browser else None,
                on_read=lambda read: repository.add_event(run_id, "page_read", read),
            )
        if reader is not None:
            # A page the office cannot read itself, read through DeepSeek
            # when the owner turned that on; inside the policy check below.
            reader = with_deepseek_fallback(reader, self.settings)
        if policy_gate is not None and reader is not None:
            reader = GuardedFetcher(
                reader, policy_gate, job.dispatch.run_id, job.dispatch.project_id
            )
        return reader

    async def _prepare(
        self,
        job: Job,
        agents: AgentSet,
        policy_gate: Any,
        ledger: RunLedger,
        recorder: InvocationRecorder,
    ) -> tuple[AgentSet, str]:
        """Each role as this run gives it: browser and tools, the owner's agents
        and models, the business line's doctrine, backups, replay, policies and
        the budget, in that order (innermost first). Returns the lead's doctrine."""
        self._hand_browser(agents, job.dispatch, policy_gate)
        researcher: Any = getattr(agents, "researcher", None)
        if policy_gate is not None and getattr(researcher, "search", None) is not None:
            researcher.search = GuardedSearch(
                researcher.search, policy_gate, job.dispatch.run_id, job.dispatch.project_id
            )
        if policy_gate is not None:
            await self._hand_tools(agents, job.dispatch, policy_gate, ledger)
        snapshot = self.repository.snapshot(job.dispatch.run_id)
        skills = apply_snapshot(
            agents,
            snapshot,
            self._completer_for,
            available_permissions=self._permissions(job.dispatch.project_id),
        )
        # Roles no agent covers use the model the owner chose for them.
        if self.session_factory is not None:
            with self.session_factory() as session:
                entries = role_models(session, self.settings, job.dispatch.project_id)
            covered = {
                entry["role"] for entry in snapshot.get("agents", []) if entry.get("connection_id")
            }
            entries = await self.models.route(job, agents, entries, covered)
            apply_role_models(agents, entries, self._completer_for, covered)
        for outcome in skills:
            self.repository.add_event(job.dispatch.run_id, "skill_" + outcome["status"], outcome)
        # Last layer: how this business line judges each role's work. The
        # run records which roles were told, so a result can be read
        # against the standard it was actually held to.
        lead_doctrine = ""
        if self.session_factory is not None:
            with self.session_factory() as session:
                venture, doctrine = venture_doctrine(session, job.dispatch.project_id)
            lead_doctrine = str(doctrine.get("chief") or "")
            if doctrine:
                told = apply_venture_doctrine(agents, venture, doctrine)
                if told:
                    self.repository.add_event(
                        job.dispatch.run_id,
                        "venture_doctrine_applied",
                        {"venture": venture, "roles": told},
                    )
        # The backup the owner chose for each provider, innermost, so a call
        # that moves is budgeted and checked as the first one was.
        self._with_backups(job, agents)
        # Each call a model answers is kept for replay, inside
        # the policies and the budget.
        keep(agents, ModelCalls(self.session_factory), job.dispatch.run_id)
        correlate(agents, recorder)
        if policy_gate is not None:
            # Policies sit outside the budget, so a refusal is reported as
            # the office's rule rather than as a spent budget.
            guard(agents, policy_gate, job.dispatch.run_id, job.dispatch.project_id)
        budget(agents, ledger)
        # Skills a plan step may name; only that step's worker is given one.
        agents = replace(agents, skills=self._skill_library(job.dispatch.project_id))
        return agents, lead_doctrine

    async def _notify(self, run_id: UUID, status: str) -> None:
        """Announce that a run settled; a failing listener never affects the run."""
        if self.events is not None:
            await self.events.publish(RunSettled(run_id, status))


RECOVERY_SWEEP_SECONDS = 60


async def run_recovery_loop(
    repository: RunRepository,
    approvals: ApprovalStore,
    worker: "RunWorker",
    stop: asyncio.Event,
    interval: float = RECOVERY_SWEEP_SECONDS,
) -> None:
    """Pick up work whose worker stopped holding it, again and again."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), interval)
            return
        except TimeoutError:
            pass
        try:
            from app.orchestrator.dispatcher import recover_runs

            for job in recovery_jobs(repository, approvals, recover_runs(repository)):
                worker.submit(job)
        except Exception:
            logger.exception('{"event":"recovery_sweep_failed"}')


def recovery_jobs(
    repository: RunRepository, approvals: ApprovalStore, run_ids: list[UUID]
) -> list[Job]:
    """Rebuild jobs for runs reclaimed at startup.

    A run that never began has no checkpoint to resume from and must start
    fresh; resuming it would invoke the graph with no input and no state.

    A run whose approval was decided while the process was down has a graph
    that already reached its end, so it re-enters at the approval node for its
    outgoing edge to run again. This is the crash between the approval
    transaction and the resume that the specification requires be recoverable.
    Re-entering there never repeats approved work: the engineering node reads
    the execution ledger first, so work already done is reported, and work
    interrupted mid-way waits for the owner.
    """
    jobs = []
    for dispatch, objective, context in repository.describe(run_ids):
        decided = approvals.latest_status(dispatch.run_id) in RESOLVED_APPROVALS
        jobs.append(
            Job(
                dispatch=dispatch,
                objective=objective,
                context=context,
                resume=repository.has_started(dispatch.run_id),
                state_override={"status": "RUNNING"} if decided else None,
                resume_node="approval" if decided else None,
            )
        )
    return jobs

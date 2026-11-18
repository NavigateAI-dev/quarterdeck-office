import json
import subprocess  # noqa: S404 - fixed executable, no shell
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app import builds
from app.approvals import ApprovalScope, ArtifactRef, build_scope, content_digest
from app.artifacts import ArtifactStore
from app.coding_agents import chosen_agent, packages_allowed
from app.config import Settings
from app.db.models import Artifact, EngineeringExecution, Run, Topic, Workspace
from app.orchestrator.approval_store import ApprovalStatus, ApprovalStore
from app.orchestrator.runner import Dispatch
from app.workspace_git import branch_for
from app.workspaces import WORK, space_of

EVIDENCE_TYPES = ("research_report", "strategy_proposal", "critique", "synthesis")


def current_branch(repository: Path) -> str | None:
    """The branch the owner's checkout is on: their default when none is set."""
    try:
        done = subprocess.run(  # noqa: S603
            ["git", "-C", str(repository), "symbolic-ref", "--short", "-q", "HEAD"],  # noqa: S607
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = done.stdout.decode().strip()
    return name if done.returncode == 0 and name else None


def resolve_base_commit(repository: Path, branch: str = "HEAD") -> str | None:
    """Resolve the configured branch to a commit, so the approval names the
    exact code reviewed rather than whatever the source happens to have
    checked out."""
    try:
        head = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "git",
                "-C",
                str(repository),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{branch}^{{commit}}",
            ],
            capture_output=True,
            timeout=15,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return head.stdout.strip() if head.returncode == 0 else None


class DatabaseApprovalGate:
    """Binds the graph's approval node to durable approval records.

    Evidence is bound by artifact identity, version, and content digest, so an
    approval cannot silently come to cover different work than was reviewed.
    """

    def __init__(
        self,
        dispatch: Dispatch,
        approvals: ApprovalStore,
        session_factory: sessionmaker[Session],
        store: ArtifactStore,
        settings: Settings | None = None,
    ) -> None:
        self.dispatch = dispatch
        self.approvals = approvals
        self.session_factory = session_factory
        self.store = store
        self.settings = settings
        # The build's project folder, once a build proposal is opened.
        self.build_folder: str | None = None

    def evidence(self) -> list[ArtifactRef]:
        refs: list[ArtifactRef] = []
        with self.session_factory() as session:
            rows = session.scalars(
                select(Artifact)
                .where(
                    Artifact.run_id == self.dispatch.run_id,
                    Artifact.type.in_(EVIDENCE_TYPES),
                )
                .order_by(Artifact.type, Artifact.version.desc())
            )
            latest: dict[str, Artifact] = {}
            for row in rows:
                latest.setdefault(row.type, row)
        for artifact_type, row in sorted(latest.items()):
            body = self.store.read(row.project_id, row.id, row.version)
            refs.append(
                ArtifactRef(
                    artifact_id=row.id,
                    type=artifact_type,
                    version=row.version,
                    content_digest=content_digest(body),
                )
            )
        return refs

    def open(self, objective: str, synthesis: dict[str, Any]) -> tuple[str, str]:
        scope = build_scope(
            self.dispatch.run_id,
            self.dispatch.project_id,
            objective,
            synthesis,
            self.evidence(),
        )
        folder = None if synthesis.get("building") else self._work_folder()
        if synthesis.get("building"):
            scope = self._bind_build(scope)
        elif folder is not None:
            scope = self._bind_workspace(scope, *folder)
        else:
            scope = self._bind_engineering_target(scope)
        approval_id = self.approvals.open(self.dispatch.run_id, self.dispatch.task_id, scope)
        return str(approval_id), scope.digest()

    def _bind_build(self, scope: ApprovalScope) -> ApprovalScope:
        """A build works in the office's own project folder for it: made
        now if new, the earlier build's when this follows one. Nothing here is
        proposed by a model; the owner approves exactly this binding."""

        settings = self.settings
        if settings is None:
            return scope
        with self.session_factory() as session:
            key = builds.project_key(session, self.dispatch.run_id)
            agent = chosen_agent(session, settings)
            packages = packages_allowed(session)
        path = builds.folder_for(settings, key)
        builds.ensure_repository(path)
        self.build_folder = key
        return scope.model_copy(
            update={
                "repository": str(path),
                "base_branch": "main",
                "base_commit": resolve_base_commit(path, "main"),
                "executes_code": settings.enable_code_execution,
                "permitted_paths": list(builds.PERMITTED),
                "test_command": builds.CHECK_COMMAND,
                "allows_pull_request": False,
                "lands_in_source": True,
                "coding_agent": agent,
                "installs_packages": packages,
            }
        )

    def _work_folder(self) -> tuple[Path, str | None] | None:
        """The run's space folder with work access: by its place under ~/dev, for
        the coder on the owner's Mac (Stage 36b), who checks it again where it is;
        or, where the owner mounted ~/dev writable (6d), as that mount holds it.
        The first by name when there are several."""
        settings = self.settings
        if settings is None:
            return None
        mounted = settings.workspaces_write_root if settings.workspaces_writable else None
        with self.session_factory() as session:
            run = session.get(Run, self.dispatch.run_id)
            topic = session.get(Topic, run.topic_id) if run and run.topic_id else None
            if topic is not None and topic.access != WORK:
                # Started from a thread set to read only.
                return None
            space = space_of(session, self.dispatch.project_id, run.topic_id if run else None)
            if space is None:
                return None
            folder = session.scalars(
                select(Workspace)
                .where(Workspace.venture_id == space, Workspace.access == WORK)
                .order_by(Workspace.name)
                .limit(1)
            ).first()
            if folder is None:
                return None
            relative, branch = Path(folder.path), folder.default_branch
        if mounted is None:
            return relative, branch
        path = mounted / relative
        return (path, branch) if (path / ".git").exists() else None

    def _bind_workspace(
        self, scope: ApprovalScope, repository: Path, default_branch: str | None
    ) -> ApprovalScope:
        """Bind the space's folder: the coder on the Mac works on an office/ branch of
        the owner's repository, and merging stays the owner's. The office cannot see
        the folder, so the base is the branch, whose commit the helper reports."""
        settings = self.settings
        assert settings is not None  # noqa: S101 - checked by _work_folder
        seen = repository.is_absolute()
        base = default_branch or (current_branch(repository) if seen else None) or "main"
        with self.session_factory() as session:
            agent = chosen_agent(session, settings)
            packages = packages_allowed(session)
        return scope.model_copy(
            update={
                "repository": str(repository),
                "base_branch": base,
                "base_commit": resolve_base_commit(repository, base) if seen else None,
                "executes_code": settings.enable_code_execution,
                "permitted_paths": ["*"],
                "test_command": settings.engineering_test_command,
                "allows_pull_request": False,
                "lands_in_source": True,
                "lands_on_branch": branch_for(scope.objective, str(self.dispatch.run_id)),
                "coding_agent": agent,
                "installs_packages": packages,
            }
        )

    def _bind_engineering_target(self, scope: ApprovalScope) -> ApprovalScope:
        """Bind the operator's engineering configuration into the proposal.

        The owner approves a specific repository, base commit, permitted paths,
        and test command. None of it is proposed by a model, and a change to any
        of it changes the digest and invalidates the approval.
        """
        settings = self.settings
        if settings is None or settings.engineering_repository is None:
            return scope
        return scope.model_copy(
            update={
                "repository": str(settings.engineering_repository),
                "base_branch": settings.engineering_base_branch,
                "base_commit": resolve_base_commit(
                    settings.engineering_repository, settings.engineering_base_branch
                ),
                "executes_code": settings.enable_code_execution,
                "permitted_paths": settings.permitted_paths,
                "test_command": settings.engineering_test_command,
                "allows_pull_request": settings.enable_github_pr,
            }
        )

    def begin_execution(self, approval_id: str, digest: str) -> tuple[str, dict[str, Any] | None]:
        """Claim the one execution this approval allows.

        "new" when this call claimed it and the work may start; "finished" with
        the recorded report when it was already carried out; "in_flight" when
        it started and never recorded an outcome, which only the owner can
        settle.
        """
        with self.session_factory() as session, session.begin():
            claimed = session.execute(
                insert(EngineeringExecution)
                .values(
                    approval_id=UUID(approval_id), run_id=self.dispatch.run_id, scope_digest=digest
                )
                .on_conflict_do_nothing(index_elements=[EngineeringExecution.approval_id])
                .returning(EngineeringExecution.approval_id)
            ).first()
            if claimed is not None:
                return "new", None
            existing = session.get(EngineeringExecution, UUID(approval_id))
            if existing is not None and existing.status == "FINISHED":
                return "finished", dict(existing.report_json or {})
            return "in_flight", None

    def finish_execution(self, approval_id: str, report: dict[str, Any]) -> None:
        with self.session_factory() as session, session.begin():
            session.execute(
                update(EngineeringExecution)
                .where(EngineeringExecution.approval_id == UUID(approval_id))
                .values(
                    status="FINISHED",
                    report_json=json.loads(json.dumps(report, default=str)),
                    finished_at=func.now(),
                )
            )

    def decision_for(self, approval_id: str) -> tuple[str, ApprovalScope] | None:
        approval = self.approvals.get(UUID(approval_id))
        if approval is None or approval.status == ApprovalStatus.PENDING:
            return None
        return approval.status, ApprovalScope.model_validate(approval.scope_json)

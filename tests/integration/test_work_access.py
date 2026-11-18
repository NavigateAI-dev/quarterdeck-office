"""Work access in a space's folder, built and off by default."""

import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.approvals import ApprovalScope
from app.artifacts import ArtifactStore
from app.config import Settings
from app.db.models import Project, Run, Topic, Venture
from app.orchestrator.approval_store import ApprovalStore
from app.orchestrator.gate import DatabaseApprovalGate
from app.workspaces import Workspaces
from tests.integration.test_dispatch import environment  # noqa: F401 - fixture
from tests.unit.test_graph import SYNTHESIS

pytestmark = pytest.mark.integration
GIT = shutil.which("git") or "git"


def repository(path: Path) -> str:
    path.mkdir(parents=True)
    for args in (["init", "-q", "-b", "trunk"], ["commit", "-q", "--allow-empty", "-m", "Start"]):
        subprocess.run(  # noqa: S603 - fixed binary, in a test
            [GIT, "-c", "user.name=T", "-c", "user.email=t@x", *args], cwd=path, check=True
        )
    done = subprocess.run(  # noqa: S603
        [GIT, "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def settings(url: str, root: Path | None, writable: bool) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=url,
        workspaces_writable=writable,
        workspaces_write_root=root,
    )


def scoped(
    environment: Any,  # noqa: F811
    tmp_path: Path,
    writable: bool,
    thread_access: str | None = None,
) -> tuple[ApprovalScope, str]:
    repository_, factory, project_id, url = environment
    root = tmp_path / "dev-rw"
    base = repository(root / "clients" / "acme")
    with factory() as session, session.begin():
        space = Venture(name="Acme")
        session.add(space)
        session.flush()
        session.get(Project, project_id).venture_id = space.id  # type: ignore[union-attr]
        space_id = space.id
    Workspaces(factory, writable=True).attach(space_id, "clients/acme", "work", "Alex")
    dispatch = repository_.create(project_id, "Add a pricing page")
    if thread_access is not None:
        # Started from a thread whose composer set its access.
        with factory() as session, session.begin():
            topic = Topic(title="Pricing", project_id=project_id, access=thread_access)
            session.add(topic)
            session.flush()
            session.get(Run, dispatch.run_id).topic_id = topic.id  # type: ignore[union-attr]
    gate = DatabaseApprovalGate(
        dispatch,
        ApprovalStore(factory),
        factory,
        ArtifactStore(tmp_path / "store"),
        settings(url, root, writable) if writable else settings(url, None, False),
    )
    approval_id, _ = gate.open("Add a pricing page", dict(SYNTHESIS))
    approval = ApprovalStore(factory).get(UUID(approval_id))
    assert approval is not None
    return ApprovalScope.model_validate(approval.scope_json), base


async def test_with_work_on_the_folder_is_bound_and_lands_on_an_office_branch(
    environment: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    scope, base = scoped(environment, tmp_path, writable=True)

    assert scope.repository == str(tmp_path / "dev-rw" / "clients" / "acme")
    assert (scope.base_branch, scope.base_commit) == ("trunk", base)
    assert scope.lands_on_branch.startswith("office/add-a-pricing-page-")
    assert scope.lands_in_source is True and scope.allows_pull_request is False


async def test_without_a_writable_mount_the_folder_is_bound_for_the_coder_on_the_mac(
    environment: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Stage 36b: the office needs no writable mount; the folder is named by its
    place under ~/dev, and the coder on the Mac works in it. The office cannot
    see it, so the base is its branch, not a commit."""
    scope, _ = scoped(environment, tmp_path, writable=False)

    assert scope.repository == "clients/acme"
    assert scope.lands_on_branch.startswith("office/add-a-pricing-page-")
    assert scope.base_branch == "main" and scope.base_commit is None


def test_an_approval_from_before_work_access_keeps_its_digest() -> None:
    from uuid import uuid4

    plain = ApprovalScope(
        run_id=uuid4(),
        project_id=uuid4(),
        objective="o",
        recommendation="r",
        proposed_engineering_scope="s",
    )
    dumped = plain.model_dump(mode="json")
    dumped.pop("lands_on_branch")
    import hashlib
    import json

    for key, default in (
        ("lands_in_source", False),
        ("coding_agent", ""),
        ("installs_packages", False),
    ):
        if dumped.get(key) == default:
            dumped.pop(key)
    before = hashlib.sha256(
        json.dumps(dumped, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert plain.digest() == before, "a pending approval is not made stale by the new field"
    assert plain.model_copy(update={"lands_on_branch": "office/x"}).digest() != before


async def test_merging_is_refused_while_work_access_is_off(client: Any) -> None:  # noqa: F811
    made = await client.post("/ventures", json={"name": "Merge check"})
    space = made.json()["id"]
    folder = (await client.post(f"/ventures/{space}/workspaces", json={"path": "site"})).json()

    merged = await client.post(f"/workspaces/{folder['id']}/merge", json={"branch": "office/x"})
    listed = await client.get(f"/workspaces/{folder['id']}/branches")

    changes = await client.get(f"/workspaces/{folder['id']}/branches/diff?branch=office/x")

    assert (merged.status_code, merged.json()["detail"]) == (409, "work_access_off")
    assert listed.status_code == 409
    assert (changes.status_code, changes.json()["detail"]) == (409, "work_access_off")


async def test_work_from_a_read_only_thread_binds_nothing_of_the_owners(
    environment: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    scope, _ = scoped(environment, tmp_path, writable=True, thread_access="read")

    assert scope.lands_on_branch == ""
    assert scope.repository is None or "dev-rw" not in scope.repository


async def test_work_from_a_thread_with_work_access_binds_the_folder(
    environment: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    scope, _ = scoped(environment, tmp_path, writable=True, thread_access="work")

    assert scope.repository == str(tmp_path / "dev-rw" / "clients" / "acme")

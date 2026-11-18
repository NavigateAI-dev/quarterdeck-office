"""A space's folders and who allowed what."""

from typing import Any
from uuid import UUID

import pytest

from app.workspaces import WORK, WorkspaceRefused, Workspaces

pytestmark = pytest.mark.integration


async def space(client: Any, name: str = "Acme") -> str:  # noqa: F811
    made = await client.post("/ventures", json={"name": name})
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


async def test_a_folder_is_given_to_a_space_and_taken_back_with_its_history(client: Any) -> None:  # noqa: F811
    space_id = await space(client)

    made = await client.post(f"/ventures/{space_id}/workspaces", json={"path": "clients/acme/site"})
    assert made.status_code == 201
    folder = made.json()
    assert (folder["name"], folder["access"]) == ("site", "read")
    again = await client.post(
        f"/ventures/{space_id}/workspaces", json={"path": "clients/acme/site"}
    )
    assert (again.status_code, again.json()["detail"]) == (409, "already_attached")

    gone = await client.delete(f"/workspaces/{folder['id']}")
    assert gone.status_code == 204
    listed = (await client.get(f"/ventures/{space_id}/workspaces")).json()
    assert listed["workspaces"] == []
    assert [(h["action"], h["path"]) for h in listed["history"]] == [
        ("revoked", "clients/acme/site"),
        ("granted", "clients/acme/site"),
    ]


@pytest.mark.parametrize(
    ("path", "code"),
    [("../etc", "outside_root"), ("app/.ssh", "credential_folder"), (".", "whole_root")],
)
async def test_a_folder_outside_or_unsafe_is_refused_by_the_server(
    client: Any,  # noqa: F811
    path: str,
    code: str,  # noqa: F811
) -> None:
    space_id = await space(client)
    refused = await client.post(f"/ventures/{space_id}/workspaces", json={"path": path})
    assert (refused.status_code, refused.json()["detail"]) == (409, code)


async def test_work_access_is_refused_while_the_office_cannot_write(client: Any) -> None:  # noqa: F811
    space_id = await space(client)
    asked = await client.post(
        f"/ventures/{space_id}/workspaces", json={"path": "app", "access": "work"}
    )
    assert (asked.status_code, asked.json()["detail"]) == (409, "work_access_off")
    folder = (await client.post(f"/ventures/{space_id}/workspaces", json={"path": "app"})).json()
    raised = await client.patch(f"/workspaces/{folder['id']}", json={"access": "work"})
    assert (raised.status_code, raised.json()["detail"]) == (409, "work_access_off")
    assert (await client.get("/workspaces")).json()["work_access"] is False


async def test_with_a_writable_mount_a_change_of_access_is_kept(client: Any) -> None:  # noqa: F811
    space_id = await space(client)
    store = Workspaces(client.app_state.session_factory, writable=True)
    folder = store.attach(UUID(space_id), "app", "read", "Alex")

    store.change(UUID(folder["id"]), WORK, "Alex")

    history = store.history(UUID(space_id))
    assert [(h["action"], h["access"]) for h in history] == [
        ("access_changed", "work"),
        ("granted", "read"),
    ]


async def test_an_unknown_space_or_folder_is_not_found(client: Any) -> None:  # noqa: F811
    missing = "00000000-0000-0000-0000-000000000000"
    assert (
        await client.post(f"/ventures/{missing}/workspaces", json={"path": "a"})
    ).status_code == 404
    assert (await client.delete(f"/workspaces/{missing}")).status_code == 404
    with pytest.raises(WorkspaceRefused):
        Workspaces(client.app_state.session_factory).detach(UUID(missing), "Alex")


async def test_the_folders_are_the_owners_only(client: Any) -> None:  # noqa: F811
    space_id = await space(client)
    stranger = {"Authorization": ""}
    assert (await client.get("/workspaces", headers=stranger)).status_code == 401
    asked = await client.post(
        f"/ventures/{space_id}/workspaces", json={"path": "a"}, headers=stranger
    )
    assert asked.status_code == 401


async def test_a_thread_sees_its_spaces_folders_and_what_its_work_read(client: Any) -> None:  # noqa: F811
    from app.db.models import Run, Topic

    space_id = await space(client, "Pilot")
    await client.post(f"/ventures/{space_id}/workspaces", json={"path": "clients/acme/site"})
    started = (await client.post("/topics", json={"text": "@Nobody-here pricing"})).json()
    topic = started["topic"]["id"]
    project = (await client.post("/projects", json={"name": "P"})).json()["id"]
    run = (await client.post("/runs", json={"project_id": project, "objective": "o"})).json()
    factory = client.app_state.session_factory
    with factory() as session, session.begin():
        session.get(Topic, UUID(topic)).venture_id = UUID(space_id)  # type: ignore[union-attr]
        session.get(Run, UUID(run["id"])).topic_id = UUID(topic)  # type: ignore[union-attr]
    repository = client.app_state.repository
    for path in ("docs/pricing.md", "docs/pricing.md", "notes.txt"):
        repository.add_event(UUID(run["id"]), "workspace_read", {"folder": "site", "path": path})

    seen = (await client.get(f"/topics/{topic}/workspaces")).json()

    assert seen["space"]["name"] == "Pilot"
    assert [f["path"] for f in seen["folders"]] == ["clients/acme/site"]
    assert [(r["folder"], r["path"]) for r in seen["read"]] == [
        ("site", "notes.txt"),
        ("site", "docs/pricing.md"),
    ]


class Reader:
    """The folder reader, as the routes see it; the real one runs in its container."""

    async def files(self, root: str) -> dict[str, Any]:
        return {"error": None, "files": [{"path": "notes.md", "size_bytes": 4, "modified_at": "x"}]}

    async def read(self, root: str, path: str, offset: int = 0) -> dict[str, Any]:
        if path == ".env":
            return {"error": "hidden_file"}
        return {
            "error": None,
            "path": path,
            "text": f"{root}:{path}",
            "offset": offset,
            "more": False,
        }


async def test_a_folder_is_browsed_through_the_reader_with_its_refusals(
    client: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.routers.workspaces as routes

    monkeypatch.setattr(routes, "build_indexer", lambda settings: Reader())
    space_id = await space(client, "Browse")
    folder = (
        await client.post(f"/ventures/{space_id}/workspaces", json={"path": "clients/acme"})
    ).json()

    files = (await client.get(f"/workspaces/{folder['id']}/files")).json()
    read = (
        await client.get(f"/workspaces/{folder['id']}/read", params={"path": "notes.md"})
    ).json()
    hidden = await client.get(f"/workspaces/{folder['id']}/read", params={"path": ".env"})
    missing = await client.get("/workspaces/00000000-0000-0000-0000-000000000000/files")

    assert [f["path"] for f in files["files"]] == ["notes.md"]
    assert read["text"] == "clients/acme:notes.md"
    assert (hidden.status_code, hidden.json()["detail"]) == (409, "hidden_file")
    assert missing.status_code == 404

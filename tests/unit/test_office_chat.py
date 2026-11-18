"""Asking the office about what is on screen.

From agent-native's context-awareness guide: without it "the agent is blind.
It asks 'which email?' when the user is staring at one."
"""

from uuid import UUID, uuid4

from app.office_chat import looking_at


class Row:
    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


class Fake:
    """A session that returns whatever was put in it, by type and id."""

    def __init__(self, rows: dict[tuple[str, UUID], object]) -> None:
        self.rows = rows

    def get(self, model: type, key: UUID) -> object | None:
        return self.rows.get((model.__name__, key))


def test_a_task_on_screen_is_named_not_merely_described() -> None:
    """It answered about a different run in the same project until it was named."""
    from app.db.models import Project, Run, Task

    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    session = Fake(
        {
            ("Run", run_id): Row(
                id=run_id,
                root_task_id=task_id,
                project_id=project_id,
                status="COMPLETED",
                current_node="synthesis",
                error=None,
            ),
            ("Task", task_id): Row(objective="Compare KYC vendors"),
            ("Project", project_id): Row(name="KYC"),
        }
    )
    said = looking_at(session, "task", run_id, None)  # type: ignore[arg-type]
    assert str(run_id)[:8] in said and str(run_id) in said
    assert "and no other" in said
    assert "Compare KYC vendors" in said
    assert "COMPLETED" in said and "synthesis" in said
    assert Project and Run and Task  # the names the fake stands in for


def test_a_failed_task_carries_what_it_recorded() -> None:
    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    session = Fake(
        {
            ("Run", run_id): Row(
                id=run_id,
                root_task_id=task_id,
                project_id=project_id,
                status="FAILED",
                current_node="critic",
                error="summarist:incomplete_response",
            ),
            ("Task", task_id): Row(objective="x"),
            ("Project", project_id): Row(name="P"),
        }
    )
    assert "summarist:incomplete_response" in looking_at(session, "task", run_id, None)  # type: ignore[arg-type]


def test_a_project_on_screen_is_named() -> None:
    project_id = uuid4()
    session = Fake({("Project", project_id): Row(name="Market intel")})
    assert "Market intel" in looking_at(session, "project", None, project_id)  # type: ignore[arg-type]


def test_any_other_screen_still_says_where_the_owner_is() -> None:
    assert "board" in looking_at(Fake({}), "board", None, None)  # type: ignore[arg-type]
    assert "office" in looking_at(Fake({}), "", None, None)  # type: ignore[arg-type]


def test_a_task_the_office_cannot_find_falls_back_to_the_screen() -> None:
    """The browser says which task is open; the office says what is true."""
    said = looking_at(Fake({}), "task", uuid4(), None)  # type: ignore[arg-type]
    assert "task screen" in said


def test_a_finished_task_on_screen_carries_what_it_concluded() -> None:
    """Its objective and status could never answer "why did it conclude that?"."""
    import json

    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    session = Fake(
        {
            ("Run", run_id): Row(
                id=run_id,
                root_task_id=task_id,
                project_id=project_id,
                status="COMPLETED",
                current_node="synthesis",
                error=None,
            ),
            ("Task", task_id): Row(objective="Compare KYC vendors"),
            ("Project", project_id): Row(name="KYC"),
        }
    )
    session.scalars = lambda statement: [  # type: ignore[attr-defined]
        Row(id=uuid4(), project_id=project_id, version=1, type="synthesis")
    ]

    class Store:
        def read(self, project_id: object, artifact_id: object, version: int) -> str:
            return json.dumps({"recommendation": "Provider B, on settlement time."})

    said = looking_at(session, "task", run_id, None, Store())  # type: ignore[arg-type]
    assert "Provider B, on settlement time." in said


def test_a_task_still_working_is_not_asked_for_an_answer_it_has_not_reached() -> None:
    run_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    session = Fake(
        {
            ("Run", run_id): Row(
                id=run_id,
                root_task_id=task_id,
                project_id=project_id,
                status="RUNNING",
                current_node="research",
                error=None,
            ),
            ("Task", task_id): Row(objective="Compare KYC vendors"),
            ("Project", project_id): Row(name="KYC"),
        }
    )

    class Loud:
        def read(self, *args: object) -> str:
            raise AssertionError("a running task has no conclusion to read")

    assert "RUNNING" in looking_at(session, "task", run_id, None, Loud())  # type: ignore[arg-type]

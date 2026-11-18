"""What a follow-up inherits from the task it continues."""

from types import SimpleNamespace
from uuid import uuid4

from app.follow_up import carried_context

ANSWER = {
    "recommendation": "Apply this quarter; the gap is governance, not capital.",
    "findings": ["Article 3 Wwft requires customer due diligence before onboarding."],
    "unknowns": ["Onboarding times are not published by any of the three."],
    "sources": [{"title": "Wwft", "url": "https://wetten.overheid.nl/BWBR0024282"}],
}


class Store:
    def __init__(self, body: str | None) -> None:
        self.body = body

    def read(self, project_id: object, artifact_id: object, version: int) -> str:
        if self.body is None:
            raise OSError("gone")
        return self.body


class Session:
    """Just enough of a session: the parent's task and its artifacts."""

    def __init__(self, objective: str, artifacts: list[object]) -> None:
        self.objective = objective
        self.artifacts = artifacts

    def get(self, model: object, key: object) -> object:
        return SimpleNamespace(objective=self.objective)

    def scalars(self, statement: object) -> list[object]:
        return self.artifacts


def parent() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), root_task_id=uuid4(), project_id=uuid4())


def artifact(kind: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), project_id=uuid4(), version=1, type=kind)


def test_a_follow_up_is_told_what_the_earlier_task_concluded() -> None:
    import json

    session = Session("Assess the EMI licence gap", [artifact("synthesis")])
    lines = carried_context(session, Store(json.dumps(ANSWER)), parent())
    joined = "\n".join(lines)
    assert "Assess the EMI licence gap" in joined
    assert "Apply this quarter" in joined
    assert "Article 3 Wwft" in joined
    assert "Onboarding times are not published" in joined
    assert "wetten.overheid.nl" in joined


def test_what_was_found_is_not_given_as_settled_fact() -> None:
    """The difference between carrying knowledge and carrying a conclusion."""
    import json

    session = Session("Assess the gap", [artifact("synthesis")])
    lines = carried_context(session, Store(json.dumps(ANSWER)), parent())
    assert any("contradicts it" in line for line in lines)


def test_a_task_that_concluded_nothing_says_so() -> None:
    session = Session("Assess the gap", [])
    lines = carried_context(session, Store(None), parent())
    assert any("no recorded answer" in line for line in lines)
    assert not any("concluded:" in line for line in lines)


def test_an_unreadable_artifact_does_not_break_the_follow_up() -> None:
    session = Session("Assess the gap", [artifact("synthesis")])
    lines = carried_context(session, Store(None), parent())
    assert any("no recorded answer" in line for line in lines)


def test_the_most_conclusive_artifact_is_the_one_carried() -> None:
    """A task has several; the synthesis is the answer, the report is the record."""
    import json

    session = Session("Assess", [artifact("final_report"), artifact("synthesis")])
    body = json.dumps(ANSWER)
    lines = carried_context(session, Store(body), parent())
    assert any("Apply this quarter" in line for line in lines)

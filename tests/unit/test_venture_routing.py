"""Sorting projects into business lines without the owner doing it by hand.

Jev suggests. What happens to the suggestion is the office's decision: a
confident answer is applied, an unsure one is put to the owner, and "none of
these" is respected. The wrong doctrine in front of an agent is worse than none.
"""

from app.judgments import JudgmentUnavailable, business_line_question
from app.venture_routing import CONFIDENT, describe, sort_projects


class Row:
    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


class FakeSession:
    """Returns the projects and ventures it was given, and records flushes."""

    def __init__(self, projects: list[Row], ventures: list[Row], tasks: list[str]) -> None:
        self.projects = projects
        self.ventures = ventures
        self.tasks = tasks
        self.flushed = False

    def scalars(self, statement: object) -> list[object]:
        text = str(statement)
        if "ventures" in text:
            return list(self.ventures)
        if "tasks" in text or "objective" in text:
            return list(self.tasks)
        return list(self.projects)

    def execute(self, statement: object) -> list[object]:
        # Task counts per project: none in these projects.
        return []

    def flush(self) -> None:
        self.flushed = True


class Judge:
    def __init__(self, answers: list[tuple[str, float]]) -> None:
        self.answers = answers
        self.asked: list[object] = []

    async def business_lines(self, projects: list[dict], lines: list[tuple[str, str]]):
        self.asked.append((projects, lines))
        return self.answers


def office(answers: list[tuple[str, float]]) -> tuple[FakeSession, Judge, list[Row]]:
    ventures = [
        Row(
            id="v-fin",
            name="Fintech compliance",
            description="EMI licences",
            status="ACTIVE",
            doctrine="",
        ),
        Row(
            id="v-mkt",
            name="Agentic commerce marketing",
            description="Brands",
            status="ACTIVE",
            doctrine="",
        ),
    ]
    projects = [
        Row(id="p-1", name="EMI governance", description="", status="ACTIVE", venture_id=None)
    ]
    session = FakeSession(projects, ventures, ["Compare KYC vendors"])
    return session, Judge(answers), projects


async def test_a_confident_answer_is_applied() -> None:
    session, judge, projects = office([("Fintech compliance", 0.93)])
    result = await sort_projects(session, judge)  # type: ignore[arg-type]
    assert projects[0].venture_id == "v-fin"
    assert projects[0].venture_set_by == "office", "the owner can see the office placed it"
    assert result["assigned"][0]["confidence"] == 0.93
    assert result["unsure"] == []
    assert session.flushed


async def test_an_unsure_answer_is_put_to_the_owner() -> None:
    session, judge, projects = office([("Fintech compliance", CONFIDENT - 0.01)])
    result = await sort_projects(session, judge)  # type: ignore[arg-type]
    assert projects[0].venture_id is None, "an unsure guess must not change what agents are told"
    assert result["assigned"] == [] and result["unsure"][0]["project"] == "EMI governance"
    # Kept as a suggestion the owner can accept in one click.
    assert projects[0].venture_suggestion_json["venture_id"] == "v-fin"


async def test_belonging_to_no_line_is_respected_not_forced() -> None:
    session, judge, projects = office([("none", 0.97)])
    result = await sort_projects(session, judge)  # type: ignore[arg-type]
    assert projects[0].venture_id is None
    assert result["unsure"][0]["line"] == "none"


async def test_an_unavailable_judge_changes_nothing() -> None:
    class Down:
        async def business_lines(self, projects: list[dict], lines: list[tuple[str, str]]):
            raise JudgmentUnavailable("offline")

    session, _, projects = office([])
    result = await sort_projects(session, Down())  # type: ignore[arg-type]
    assert result["reason"] == "judge_unavailable"
    assert projects[0].venture_id is None


async def test_without_a_judge_nothing_is_sorted() -> None:
    session, _, projects = office([])
    result = await sort_projects(session, None)
    assert result["assigned"] == [] and projects[0].venture_id is None


def test_the_choice_always_offers_belonging_to_none() -> None:
    """Forcing a choice would put the wrong doctrine in front of the agents."""
    question = business_line_question([("Fintech compliance", "EMI licences")])
    assert "none" in question.criteria
    assert "Fintech compliance" in question.criteria


def test_a_project_is_judged_by_its_work_not_only_its_label() -> None:
    """Several of this office's projects say only "Created from Slack"."""
    session, _, projects = office([])
    described = describe(session, projects[0])  # type: ignore[arg-type]
    assert described["name"] == "EMI governance"
    assert described["recent_tasks"] == ["Compare KYC vendors"]


def test_a_project_is_judged_again_only_for_a_reason() -> None:
    from datetime import UTC, datetime, timedelta

    from app.venture_routing import needs_judging

    judged = datetime(2026, 9, 29, 12, tzinfo=UTC)
    seen = {"judged_at": judged.isoformat(), "tasks": 2}
    assert needs_judging({}, 0, None), "never judged"
    assert not needs_judging(seen, 2, judged - timedelta(days=1)), "nothing new"
    assert needs_judging(seen, 3, None), "a new task says more about the project"
    assert needs_judging(seen, 2, judged + timedelta(minutes=1)), "the lines changed"

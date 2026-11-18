"""Whether work moved an objective, judged on the work's own account of itself.

A quarter should look advanced, not merely worked.
"""

from datetime import date, timedelta

from app.objectives import DELIVERED, PARTIAL, UNFINISHED, _verdict, as_lines


class Row:
    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


def run(status: str) -> Row:
    return Row(status=status)


def test_a_task_that_answered_nothing_is_no_progress() -> None:
    """The KYC report answered 0 of 4 and completed. Counting it would lie."""
    coverage = {"questions_asked": 4, "questions_answered": 0, "unmet_by_review": []}
    assert _verdict(run("COMPLETED"), coverage) == UNFINISHED


def test_a_task_that_answered_some_is_partial() -> None:
    coverage = {"questions_asked": 4, "questions_answered": 2, "unmet_by_review": []}
    assert _verdict(run("COMPLETED"), coverage) == PARTIAL


def test_unresolved_review_points_keep_it_partial() -> None:
    coverage = {"questions_asked": 2, "questions_answered": 2, "unmet_by_review": ["a gap"]}
    assert _verdict(run("COMPLETED"), coverage) == PARTIAL


def test_a_task_that_answered_everything_delivered() -> None:
    coverage = {"questions_asked": 3, "questions_answered": 3, "unmet_by_review": []}
    assert _verdict(run("COMPLETED"), coverage) == DELIVERED


def test_work_still_running_is_not_counted_either_way() -> None:
    for status in ("RUNNING", "PENDING", "PAUSED", "FAILED", "CANCELLED"):
        assert _verdict(run(status), None) == UNFINISHED


def test_without_a_scorecard_a_completed_task_is_taken_at_its_word() -> None:
    assert _verdict(run("COMPLETED"), None) == DELIVERED
    assert _verdict(run("NEEDS_HUMAN_REVIEW"), None) == PARTIAL


def test_an_objective_reads_as_one_line_with_its_measure_and_date() -> None:
    soon = date.today() + timedelta(days=30)
    lines = as_lines(
        [
            Row(title="Land two EMI retainers", measure="two signed", ends_on=soon),
            Row(title="A direction with no measure", measure="", ends_on=None),
        ]
    )
    assert lines[0] == f"Land two EMI retainers — measured by: two signed (by {soon.isoformat()})"
    assert lines[1] == "A direction with no measure"

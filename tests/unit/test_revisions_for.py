from app.runtime.worker import revisions_for


def test_work_from_a_discussion_asks_before_revising() -> None:
    assert revisions_for({"from_discussion": True}, 3) == 0


def test_other_work_revises_as_configured() -> None:
    assert revisions_for({}, 3) == 3
    assert revisions_for(None, 2) == 2

"""Every context the office builds for a task must be one its agents accept."""

from uuid import uuid4

from app.context import ProjectContext


def test_work_started_in_a_discussion_is_accepted_by_its_agents() -> None:
    """Tasks started with Start in a discussion crashed at their first step:
    the context said `from_discussion` and the agents' schema refused it."""
    context = ProjectContext.model_validate(
        {
            "organization": "Northwind",
            "project_id": str(uuid4()),
            "project_name": "Advisory",
            "from_discussion": True,
            "discussion": ["Owner: check the weather in Lyon"],
        }
    )
    assert context.from_discussion is True
    assert "from_discussion" not in context.render(), "a flag for the office, not the agents"

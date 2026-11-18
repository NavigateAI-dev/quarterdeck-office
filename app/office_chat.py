"""Answering the owner in the browser, about what they are looking at.

The office could already hold a grounded conversation, but only through Slack,
and never knew what the owner had on screen. Asking "why did this stop?" while
looking at the task that stopped was impossible.

This reuses the same conversation — `SlackOffice.converse` is named for where
it was first used, not for what it needs — and adds the one thing it lacked:
the screen. Answers only. It cannot approve anything and it starts no work.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.channels.inbound_office import SlackOffice
from app.channels.lookup import ChatAnswer
from app.config import Settings
from app.db.models import Project, Run, Task
from app.follow_up import carried_context
from app.orchestrator.approval_store import ApprovalStore
from app.orchestrator.runner import RunRepository

HISTORY_TURNS = 8
OBJECTIVE_CLIP = 300
FINISHED = ("COMPLETED", "FAILED", "CANCELLED")
# Said to the office, not to the owner: chat answers from the record and starts
# no work, so a question needing new work is handed back rather than guessed at.
NEEDS_WORK = (
    "You answer from what the office already recorded. If answering would need new research, "
    "a page fetched, or any work not already done, say so plainly and tell the owner they can "
    'press "Ask a follow-up" on this task, which starts a new task that carries this one\'s '
    "findings forward. Never guess in place of work."
)


def looking_at(
    session: Session,
    view: str,
    run_id: UUID | None,
    project_id: UUID | None,
    store: Any = None,
) -> str:
    """What the owner has on screen, as a line the office can answer about.

    Read from the database rather than taken from the browser: the client says
    which task is open, the office says what is true about it.
    """
    if run_id is not None:
        run = session.get(Run, run_id)
        if run is not None:
            task = session.get(Task, run.root_task_id)
            project = session.get(Project, run.project_id)
            objective = (task.objective if task else "")[:OBJECTIVE_CLIP]
            parts = [
                # Named, not merely described: without the reference the office
                # answered about a different run in the same project, which is
                # the "which one?" failure this context exists to prevent.
                f"The owner is looking at run {str(run.id)[:8]} "
                f'(full id {run.id}) in project "{project.name if project else ""}". '
                "Answer about this run and no other.",
                f"Its objective: {objective}",
                f"Its state: {run.status}"
                + (f", at step {run.current_node}" if run.current_node else ""),
            ]
            if run.error:
                parts.append(f"It recorded: {run.error}")
            if store is not None and run.status in FINISHED:
                # Its objective and status were never enough to answer "why did
                # it conclude that?" — the one question a finished task invites.
                concluded = carried_context(session, store, run)
                if concluded:
                    parts.append("What it produced, so questions about it can be answered:")
                    parts += concluded
            return " ".join(parts)
    if project_id is not None:
        project = session.get(Project, project_id)
        if project is not None:
            return f'The owner is looking at the project "{project.name}".'
    return f"The owner is looking at the {view or 'office'} screen."


class OfficeChat:
    """A browser conversation with the office, grounded in the screen."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        repository: RunRepository,
        worker: Any,
        approvals: ApprovalStore,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        # No channel app backs a browser conversation, so this answers as the
        # office itself rather than as any agent's bot.
        self.office = SlackOffice(session_factory, repository, worker, approvals, settings, uuid4())
        self.store = repository.store

    async def answer(
        self,
        text: str,
        history: list[dict[str, str]],
        view: str = "",
        run_id: UUID | None = None,
        project_id: UUID | None = None,
    ) -> tuple[str, str]:
        """The office's answer, and the screen context it was given."""
        with self.session_factory() as session:
            context = looking_at(session, view, run_id, project_id, self.store)
        turns = [
            *history[-HISTORY_TURNS:],
            {"role": "user", "content": f"[What the owner is looking at] {context}"},
            {"role": "user", "content": f"[How to answer] {NEEDS_WORK}"},
        ]
        said = await self.office.converse(turns, text)
        reply = said if isinstance(said, str) else ""
        if isinstance(said, ChatAnswer):
            # It looked something up, or the request needs the team.
            reply = said.text or (
                f"That needs the team's work: {said.task} Start it as a new task when you want it."
                if said.task
                else f"I can watch that for you: {said.watch['title']}. Set it up under "
                "Watching → ＋ Watch on the floor; looking is free, and only a change costs."
                if said.watch
                else ""
            )
        return (
            reply
            or "I could not answer that from what the office records. Try asking another way.",
            context,
        )

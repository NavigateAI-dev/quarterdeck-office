"""Helpers more than one group of routes uses."""

from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import attachments, rooms, topic_settings, topics
from app.api.auth import OwnerIdentity
from app.api.routers.deps import RouterDeps
from app.api.routes import found
from app.api.schemas import RoomAction, RoomPost
from app.db.models import KnowledgeSource, Project, RoomMessage, Run, TaskTemplate, Topic
from app.handoff import free_exploration, work_view
from app.handoff import free_exploration as handoff_free_exploration
from app.orchestrator.graph import ANALYSIS_WORKFLOW, LEAD_WORKFLOW
from app.orchestrator.state import RunStatus
from app.project_settings import budget_for
from app.room_replies import MAX_REPLIES
from app.room_replies import MENTION as MENTION_MARK
from app.room_replies import view as replier_view
from app.run_actions import INSTRUCTABLE
from app.run_start import start_run


def start_prep(d: RouterDeps, objective: str, cost: float) -> Run:
    # Free exploration: a brief for the owner's day belongs to no project.
    with d.session_factory() as session, session.begin():
        project_id = free_exploration(session).id
    with d.session_factory() as session:
        budget = budget_for(session, d.settings, project_id) | {"max_cost_usd": cost}
    return start_run(
        d.session_factory,
        d.repository,
        d.worker,
        d.settings,
        project_id,
        objective,
        budget,
        title="Prep brief: " + objective.split("“", 1)[-1].split("”", 1)[0][:80],
    )


def added_event(d: RouterDeps, action: dict[str, Any], body: RoomAction, owner_name: str) -> None:
    """A proposed event the owner added from the calendar form: the
    button records which change it became, once."""
    if action.get("change_id"):
        raise HTTPException(status_code=409, detail="already_done")
    change = d.calendar_service.change(body.change_id) if body.change_id else None
    if change is None or change["action"] != "create":
        raise HTTPException(status_code=422, detail="change_missing")
    action.update(change_id=change["id"], done_by=owner_name)


def source_view(d: RouterDeps, source: KnowledgeSource) -> dict[str, Any]:
    return {
        "id": source.id,
        "name": source.name,
        "kind": source.kind,
        "root": source.root,
        "status": source.status,
        "document_count": source.document_count,
        "skipped": source.skipped_json or [],
        "indexed_at": source.indexed_at,
        "last_error": source.last_error,
    }


def template_view(d: RouterDeps, row: TaskTemplate) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "project_id": str(row.project_id) if row.project_id else None,
        "objective": row.objective,
        "budget": row.budget_json or {},
        "source_run_id": str(row.source_run_id) if row.source_run_id else None,
    }


def save_template(d: RouterDeps, body: dict[str, Any]) -> dict[str, Any]:
    try:
        with d.session_factory() as session, session.begin():
            found(session.get(Project, body["project_id"]), "Project")
            row = TaskTemplate(**body)
            session.add(row)
            session.flush()
            return template_view(d, row)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="template_name_already_exists") from None


def _home(d: RouterDeps, project_id: UUID) -> UUID:
    """The project's first topic, which is what its room was."""
    with d.session_factory() as session, session.begin():
        project = found(session.get(Project, project_id), "Project")
        home = topics.home_topic(session, project)
        if home is None:
            raise HTTPException(status_code=404, detail="Topic not found")
        return home.id


def _topic_feed(
    d: RouterDeps, topic_id: UUID, after_event: int, after_message: int, limit: int
) -> dict[str, Any]:
    with d.session_factory() as session:
        topic = found(session.get(Topic, topic_id), "Topic")
        room = rooms.feed(
            session,
            d.repository.store,
            topic,
            max(after_event, 0),
            max(after_message, 0),
            limit,
        )
        # What its tasks wait on, read fresh: answered anywhere, gone here.
        room["waiting"] = rooms.waiting(session, topic)
        # The work handed off here, as it stands now.
        room["work"] = [
            work_view(session, run, d.repository.usage(run.id)["spent"])
            for run in session.scalars(
                select(Run)
                .where(Run.topic_id == topic.id)
                .order_by(Run.started_at.asc().nulls_last(), Run.id)
            )
        ]
    # Who is writing a reply right now, so the room can say so.
    room["replying"] = list(d.replies.replying.get(topic_id, []))
    return room


def _room_message(
    d: RouterDeps, session: Any, topic_id: UUID, ref: str | None
) -> RoomMessage | None:
    """A message of this topic named "m:<id>"; 404 for one of another."""
    if ref is None:
        return None
    row: RoomMessage | None = session.get(RoomMessage, int(ref.removeprefix("m:")))
    if row is None or row.topic_id != topic_id:
        raise HTTPException(status_code=404, detail="Message not found")
    return row


# From talk to work, and back: what each message commits to is read
# and acted on. Its three ways of acting are the office's own routes'.
def _start_from_topic(
    d: RouterDeps,
    project_id: UUID,
    objective: str,
    *,
    topic_id: UUID,
    origin_message_id: int,
    discussion: list[str],
    cost_cap: float,
    handed_to: str | None = None,
    full_team: bool = False,
) -> Run:
    """Start from a discussion: the lead, whatever the project's default
    deliverable; the full team when asked for it. The answer comes back to the
    discussion either way."""
    with d.session_factory() as session:
        budget = dict(budget_for(session, d.settings, project_id))
    lead = not full_team
    held = budget.get("max_cost_usd")
    budget["max_cost_usd"] = min(float(held), cost_cap) if held else cost_cap
    return start_run(
        d.session_factory,
        d.repository,
        d.worker,
        d.settings,
        project_id,
        objective,
        budget=budget,
        topic_id=topic_id,
        origin_message_id=origin_message_id,
        discussion=discussion,
        handed_to=handed_to,
        workflow=LEAD_WORKFLOW if lead else ANALYSIS_WORKFLOW,
    )


def _direct_from_topic(d: RouterDeps, run_id: UUID, text: str) -> None:
    with d.session_factory() as session:
        run = session.get(Run, run_id)
        status = run.status if run is not None else None
    if status in INSTRUCTABLE:
        d.repository.add_control(run_id, "instruction", text, None)


def _pause_from_topic(d: RouterDeps, run_id: UUID) -> None:
    with d.session_factory() as session:
        run = session.get(Run, run_id)
        status = run.status if run is not None else None
    if status not in ("PENDING", "RUNNING", "RECOVERING"):
        return
    d.repository.add_control(run_id, "pause", None, None)
    if status != "RUNNING" and d.repository.stop(
        run_id, RunStatus.PAUSED, only_from=("PENDING", "RECOVERING")
    ):
        d.repository.apply_stop_controls(run_id)


def _say(
    d: RouterDeps, topic_id: UUID, body: RoomPost, owner: Any, background: BackgroundTasks
) -> dict[str, Any]:
    """Store what the owner said in a topic; the agents it names reply after
    it returns. It decides nothing and starts nothing by itself."""
    with d.session_factory() as session:
        topic = found(session.get(Topic, topic_id), "Topic")
        # Sent again after a dropped connection: it was already said, and
        # its work already started; nothing happens twice.
        again = rooms.said_before(session, topic.id, body.client_key)
        if again is not None:
            return {**again, "replying": []}
        if body.run_id is not None:
            # A task's question is asked where that task's work lives.
            run = session.get(Run, body.run_id)
            if run is None or (
                run.topic_id != topic.id and not (topic.home and run.project_id == topic.project_id)
            ):
                raise HTTPException(status_code=404, detail="Run not found")
        # What it replies to, and the thread it is in: this topic's only.
        quoted = _room_message(d, session, topic_id, body.reply_to)
        root = _room_message(d, session, topic_id, body.thread_id)
        thread_id = (root.thread_id or root.id) if root is not None else None
        default = d.replies.default_answerer(session, quoted, thread_id)
    agents, unknown, archived = d.replies.who(body.text, default)
    if len(agents) > MAX_REPLIES:
        # Refused rather than queued: a long line of replies would cost
        # more than the owner meant to spend on one message.
        raise HTTPException(status_code=422, detail="room_too_many_agents")
    with d.session_factory() as session, session.begin():
        topic = found(session.get(Topic, topic_id), "Topic")
        attached: list[dict[str, Any]] = []
        if body.attachments:
            # Granted where this discussion's work runs; noted on the message.
            home = topic.project_id or handoff_free_exploration(session).id
            try:
                attached = attachments.attach(session, body.attachments, home)
            except attachments.AttachmentRejected as rejected:
                raise HTTPException(status_code=404, detail=rejected.code) from None
        if MENTION_MARK.search(body.text):
            # Named, so in the discussion now: they hear what follows.
            topics.join(session, topic, [str(a.get("profile_id") or "") for a in agents])
        said = rooms.post(
            session,
            topic.project_id,
            body.text,
            owner.name,
            [
                *d.replies.steerable(session, topic),
                *attached,
                *([{"kind": topic_settings.PLAN_FIRST}] if body.plan else []),
            ],
            run_id=body.run_id,
            reply_to=quoted.id if quoted is not None else None,
            thread_id=thread_id,
            topic_id=topic.id,
            client_key=body.client_key,
        )
    message_id = int(said["id"].removeprefix("m:"))
    background.add_task(
        d.handoff.handle, topic_id, message_id, body.text, agents, unknown, archived
    )
    return {**said, "replying": [replier_view(agent) for agent in agents]}


# A task that ended (not one waiting on the owner) hands its queue to its thread.
DELIVER_QUEUE_ON = frozenset({"COMPLETED", "CLOSED", "FAILED", "CANCELLED", "REJECTED"})


async def deliver_queue(d: RouterDeps, event: Any) -> None:
    """The owner's messages queued for after the work, said in the
    work's thread now that it ended, each answered as if just typed."""
    if str(event.status) not in DELIVER_QUEUE_ON:
        return
    with d.session_factory() as session:
        run = session.get(Run, event.run_id)
        if run is None:
            return
        topic_id = run.topic_id
        if topic_id is None:
            project = session.get(Project, run.project_id)
            home = topics.home_topic(session, project, create=False) if project else None
            topic_id = home.id if home is not None else None
    if topic_id is None:
        return  # no thread to say it in: it waits, and is not lost
    owner = OwnerIdentity(d.settings.owner_name or "Owner")
    for text in d.repository.take_queued(event.run_id):
        background = BackgroundTasks()
        _say(d, topic_id, RoomPost(text=text), owner, background)
        await background()

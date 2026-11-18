"""Routes for discussions: topics, chat and titles."""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy import select

from app import (
    decisions,
    owner_documents,
    revisions,
    room_tools,
    rooms,
    topic_settings,
    topic_usage,
    topics,
)
from app.api.auth import OwnerIdentity
from app.api.routers.deps import RouterDeps
from app.api.routers.shared import _room_message, _say, _topic_feed, added_event
from app.api.routes import found
from app.api.schemas import (
    DecisionRecord,
    OfficeChatRequest,
    RoomAction,
    RoomPost,
    TitleRequest,
    TopicBranch,
    TopicParticipant,
    TopicRevise,
    TopicSettings,
    TopicStart,
    TopicUpdate,
)
from app.coder import jobs as coder_jobs
from app.db.models import AgentProfile, Project, RoomMessage, Run, Topic
from app.mcp.calling import ToolRefused
from app.office_chat import OfficeChat
from app.office_settings import role_models
from app.routines import tools as routine_tools
from app.routines.service import RoutineRejected
from app.run_actions import INSTRUCTABLE
from app.titles import title_for


def add_topics_routes(router: APIRouter, d: RouterDeps) -> None:
    Owner = Annotated[OwnerIdentity, Depends(d.require_owner)]

    @router.post("/titles")
    def suggest_title(body: TitleRequest) -> dict[str, str]:
        """The title a task would get from this ask, for the form to show as the
        owner types. Derived in code, as every untitled task's is; free."""
        return {"title": title_for(body.objective)}

    @router.post("/chat")
    async def office_chat(body: OfficeChatRequest) -> dict[str, Any]:
        """Ask the office about what is on screen. It answers; it decides nothing."""
        chat = OfficeChat(d.session_factory, d.repository, d.worker, d.approvals, d.settings)
        reply, context = await chat.answer(
            body.text, body.history, body.view, body.run_id, body.project_id
        )
        return {"reply": reply, "context": context}

    @router.get("/topics")
    def list_topics(status: str = "open") -> list[dict[str, Any]]:
        """Topics with something said in them, most recent first."""
        if status not in (*topics.STATUSES, "all"):
            raise HTTPException(status_code=422, detail="unknown_status")
        with d.session_factory() as session:
            return topics.listed(session, None if status == "all" else status)

    @router.post("/topics", status_code=201)
    def start_topic(body: TopicStart, owner: Owner, background: BackgroundTasks) -> dict[str, Any]:
        """Start talking: a topic from a sentence, no project or form needed.
        The lead (or whoever is named) answers in it."""
        if body.client_key:
            with d.session_factory() as session:
                first = session.scalar(
                    select(RoomMessage).where(RoomMessage.client_key == body.client_key)
                )
                if first is not None and first.topic_id is not None:
                    # Started already, before the connection dropped.
                    said = rooms.said_before(session, first.topic_id, body.client_key)
                    view = topics.view(session, found(session.get(Topic, first.topic_id), "Topic"))
                    return {"topic": view, "message": said, "replying": []}
        with d.session_factory() as session, session.begin():
            project = (
                found(session.get(Project, body.project_id), "Project")
                if body.project_id is not None
                else None
            )
            topic = topics.start(session, body.text, project)
            topic_id = topic.id
        said = _say(
            d,
            topic_id,
            RoomPost(text=body.text, client_key=body.client_key, attachments=body.attachments),
            owner,
            background,
        )
        with d.session_factory() as session:
            view = topics.view(session, found(session.get(Topic, topic_id), "Topic"))
        return {"topic": view, "message": said, "replying": said["replying"]}

    @router.get("/topics/{topic_id}")
    def read_topic(topic_id: UUID) -> dict[str, Any]:
        with d.session_factory() as session:
            return topics.view(session, found(session.get(Topic, topic_id), "Topic"))

    @router.get("/topics/{topic_id}/settings")
    def read_topic_settings(topic_id: UUID) -> dict[str, Any]:
        """The thread's model, access and goal, as its composer shows them."""
        with d.session_factory() as session:
            view = topic_settings.view(session, found(session.get(Topic, topic_id), "Topic"))
            chief = role_models(session, d.settings).get("chief")
        return {
            **view,
            "work_available": work_possible(d),
            "default_model": chief["model_id"] if chief else None,
        }

    @router.put("/topics/{topic_id}/settings")
    def save_topic_settings(topic_id: UUID, body: TopicSettings) -> dict[str, Any]:
        """Change what was sent; a null clears it back to the office's default."""
        with d.session_factory() as session, session.begin():
            topic = found(session.get(Topic, topic_id), "Topic")
            try:
                topic_settings.save(
                    session,
                    topic,
                    body.model_dump(exclude_unset=True),
                    work_possible(d),
                )
            except topic_settings.SettingsRefused as refused:
                raise HTTPException(status_code=409, detail=refused.code) from None
            session.flush()
            view = topic_settings.view(session, topic)
            chief = role_models(session, d.settings).get("chief")
        return {
            **view,
            "work_available": work_possible(d),
            "default_model": chief["model_id"] if chief else None,
        }

    @router.get("/topics/{topic_id}/usage")
    def topic_usage_view(topic_id: UUID) -> dict[str, Any]:
        """The thread's status line: turns, steps, tokens, cost, context used."""
        with d.session_factory() as session:
            return topic_usage.usage(
                session, d.settings, found(session.get(Topic, topic_id), "Topic")
            )

    @router.get("/topics/{topic_id}/export.zip")
    def topic_export(topic_id: UUID) -> Response:
        """The thread as a download: its words, the work it started, its files' names."""
        with d.session_factory() as session:
            name, data = topic_usage.export(session, found(session.get(Topic, topic_id), "Topic"))
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @router.put("/topics/{topic_id}")
    def update_topic(topic_id: UUID, body: TopicUpdate) -> dict[str, Any]:
        """Rename it, place it in a project (or free it), or rest it."""
        changes = body.model_dump(exclude_unset=True)
        with d.session_factory() as session, session.begin():
            topic = found(session.get(Topic, topic_id), "Topic")
            if "title" in changes and changes["title"]:
                topic.title = changes["title"]
            if "project_id" in changes:
                if changes["project_id"] is not None:
                    found(session.get(Project, changes["project_id"]), "Project")
                topics.set_scope(session, topic, changes["project_id"])
            if "status" in changes and changes["status"]:
                topic.status = changes["status"]
            session.flush()
            return topics.view(session, topic)

    @router.get("/topics/{topic_id}/feed")
    def topic_feed(
        topic_id: UUID, after_event: int = 0, after_message: int = 0, limit: int = rooms.PAGE
    ) -> dict[str, Any]:
        """The topic after two cursors: what was said and what its work did."""
        return _topic_feed(d, topic_id, after_event, after_message, limit)

    @router.post("/topics/{topic_id}/messages", status_code=201)
    def say_in_topic(
        topic_id: UUID, body: RoomPost, owner: Owner, background: BackgroundTasks
    ) -> dict[str, Any]:
        return _say(d, topic_id, body, owner, background)

    @router.post("/topics/{topic_id}/decisions", status_code=201)
    def record_decision(topic_id: UUID, body: DecisionRecord, owner: Owner) -> dict[str, Any]:
        """Record what the owner decided, from their own message, in the
        discussion's project; it may replace an earlier decision."""
        with d.session_factory() as session, session.begin():
            topic = found(session.get(Topic, topic_id), "Topic")
            message = _room_message(d, session, topic_id, body.message_id)
            if message is None or message.author_kind != "owner":
                raise HTTPException(status_code=404, detail="Message not found")
            try:
                made = decisions.record(
                    session, topic, message, body.text, owner.name, body.replaces
                )
            except decisions.PlaceFirst:
                raise HTTPException(status_code=409, detail="place_first") from None
            except decisions.NotReplaceable:
                raise HTTPException(status_code=409, detail="not_replaceable") from None
            return decisions.view(session, made)

    @router.get("/topics/{topic_id}/decisions")
    def topic_decisions(topic_id: UUID) -> list[dict[str, Any]]:
        """The decisions in force where this discussion belongs."""
        with d.session_factory() as session:
            topic = found(session.get(Topic, topic_id), "Topic")
            return decisions.for_topic(session, topic)

    @router.post("/topics/{topic_id}/messages/{message_id}/document", status_code=201)
    def open_reply_as_document(topic_id: UUID, message_id: int) -> dict[str, Any]:
        """A reply, opened in the editor as a document of its own; the same one
        each time it is opened."""
        shown = owner_documents.open_from_message(
            d.session_factory, d.repository.store, topic_id, message_id
        )
        if shown is None:
            raise HTTPException(status_code=404, detail="Message not found")
        return shown

    @router.post("/topics/{topic_id}/revise", status_code=202)
    def revise_passage(
        topic_id: UUID, body: TopicRevise, background: BackgroundTasks
    ) -> dict[str, Any]:
        """Revise one passage of work delivered in this discussion into a new
        version, with a note of what changed; the lead writes it."""
        with d.session_factory() as session:
            topic = found(session.get(Topic, topic_id), "Topic")
            run = session.get(Run, body.run_id)
            if run is None or (
                run.topic_id != topic.id and not (topic.home and run.project_id == topic.project_id)
            ):
                raise HTTPException(status_code=404, detail="Run not found")
            if revisions.answer_of(session, run.id) is None:
                raise HTTPException(status_code=409, detail="no_answer")
        background.add_task(
            revisions.revise,
            d.session_factory,
            d.repository,
            topic_id,
            body.run_id,
            body.quote,
            body.comment,
            d.replies.complete,
        )
        return {"accepted": True}

    @router.post("/topics/{topic_id}/seen")
    def topic_seen(topic_id: UUID) -> dict[str, Any]:
        """The owner has the discussion open: coming back is measured from now."""
        with d.session_factory() as session, session.begin():
            topic = found(session.get(Topic, topic_id), "Topic")
            topic.owner_seen_at = datetime.now(UTC)
            session.flush()
            return topics.view(session, topic)

    @router.post("/topics/{topic_id}/participants", status_code=201)
    def bring_in(topic_id: UUID, body: TopicParticipant) -> dict[str, Any]:
        """Bring a colleague into the discussion. They are given its brief, not
        its whole history, and answer when named."""
        with d.session_factory() as session, session.begin():
            topic = found(session.get(Topic, topic_id), "Topic")
            agent = found(session.get(AgentProfile, body.profile_id), "Agent")
            if agent.status != "ACTIVE":
                raise HTTPException(status_code=409, detail="agent_not_active")
            if topics.join(session, topic, [str(agent.id)]):
                given = (
                    "given this discussion's brief"
                    if (topic.brief_json or {}).get("where")
                    else "given what was said so far"
                )
                session.add(
                    RoomMessage(
                        project_id=topic.project_id,
                        topic_id=topic.id,
                        author_kind="office",
                        author_name="The office",
                        body=f"{agent.name} joined · {given}.",
                    )
                )
                topics.touch(session, topic.id)
            session.flush()
            return topics.view(session, topic)

    @router.delete("/topics/{topic_id}/participants/{profile_id}")
    def let_go(topic_id: UUID, profile_id: UUID) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            topic = found(session.get(Topic, topic_id), "Topic")
            topics.leave(session, topic, str(profile_id))
            return topics.view(session, topic)

    @router.post("/topics/{topic_id}/branch", status_code=201)
    def branch_topic(topic_id: UUID, body: TopicBranch) -> dict[str, Any]:
        """A message becomes a discussion of its own, with a carried brief and a
        link both ways; the discussion it came from carries on."""
        with d.session_factory() as session, session.begin():
            parent = found(session.get(Topic, topic_id), "Topic")
            message = _room_message(d, session, topic_id, body.message_id)
            if message is None:
                raise HTTPException(status_code=404, detail="Message not found")
            title = body.title or title_for(message.body) or "A new topic"
            child = topics.branch(session, parent, message, title, body.brief or "")
            return topics.view(session, child)

    @router.post("/topics/{topic_id}/messages/{message_id}/actions/{index}", status_code=201)
    def press_topic_action(
        topic_id: UUID, message_id: int, index: int, body: RoomAction, owner: Owner
    ) -> dict[str, Any]:
        """A button the discussion offered: start the work in place (no form),
        or send the owner's words to running work. Each only once."""
        with d.session_factory() as session, session.begin():
            # Locked, so two presses of one button start one piece of work.
            row = session.scalar(
                select(RoomMessage)
                .where(RoomMessage.id == message_id, RoomMessage.topic_id == topic_id)
                .with_for_update()
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Message not found")
            actions = [dict(action) for action in row.actions_json or []]
            if not 0 <= index < len(actions):
                raise HTTPException(status_code=404, detail="Action not found")
            action = actions[index]
            if action.get("kind") in ("start_work", "start_task"):
                if action.get("run_id"):
                    raise HTTPException(status_code=409, detail="already_done")
                objective = body.objective or str(action.get("objective") or "")
                origin = row.reply_to or row.id
                named = action.get("handed_to")
                started = d.handoff.start_work(
                    topic_id, origin, objective, {"name": named} if named else None
                )
                if started is None:
                    raise HTTPException(status_code=409, detail="not_started")
                action.update(run_id=str(started.id), objective=objective, done_by=owner.name)
            elif action.get("kind") == "send_as_direction":
                if action.get("control_id"):
                    raise HTTPException(status_code=409, detail="already_done")
                run = session.get(Run, UUID(str(action.get("run_id"))))
                if run is None or run.status not in INSTRUCTABLE:
                    raise HTTPException(status_code=409, detail="run_not_accepting_instructions")
                control = d.repository.add_control(
                    run.id, "instruction", row.body, body.target_node
                )
                action.update(control_id=control["id"], done_by=owner.name)
            elif action.get("kind") == "add_event":
                added_event(d, action, body, owner.name)
            elif action.get("kind") in ("routine_create", "routine_update"):
                if action.get("routine_id"):
                    raise HTTPException(status_code=409, detail="already_done")
                try:
                    action.update(routine_tools.press(session, action, topic_id))
                except RoutineRejected as wrong:
                    raise HTTPException(status_code=409, detail=str(wrong)) from None
                action.update(done_by=owner.name)
            else:
                raise HTTPException(status_code=404, detail="Action not found")
            actions[index] = action
            row.actions_json = actions
        return {"action": action}

    @router.post("/topics/{topic_id}/messages/{message_id}/tool-calls/{index}", status_code=201)
    async def press_tool_call(
        topic_id: UUID, message_id: int, index: int, owner: Owner
    ) -> dict[str, Any]:
        """Make a call an agent proposed, because the owner pressed for it.

        Once only: the button is held while the call runs, and records where its
        result was put in the discussion.
        """
        with d.session_factory() as session, session.begin():
            row = session.scalar(
                select(RoomMessage)
                .where(RoomMessage.id == message_id, RoomMessage.topic_id == topic_id)
                .with_for_update()
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Message not found")
            actions = [dict(action) for action in row.actions_json or []]
            if not 0 <= index < len(actions) or actions[index].get("kind") != "tool_call":
                raise HTTPException(status_code=404, detail="Action not found")
            if actions[index].get("result_id") or actions[index].get("running"):
                raise HTTPException(status_code=409, detail="already_done")
            actions[index]["running"] = True
            row.actions_json = actions
            action = dict(actions[index])
            project_id, thread_id = row.project_id, row.thread_id

        def settle(**changes: Any) -> dict[str, Any]:
            with d.session_factory() as session, session.begin():
                row = session.get(RoomMessage, message_id)
                if row is None:
                    return action
                updated = [dict(item) for item in row.actions_json or []]
                updated[index].pop("running", None)
                updated[index].update(changes)
                row.actions_json = updated
                return updated[index]

        try:
            result = await room_tools.pressed(
                d.session_factory, d.settings, project_id, action, owner.name
            )
        except ToolRefused as refused:
            settle()
            raise HTTPException(status_code=409, detail=refused.reason) from None
        except Exception:
            settle()
            raise
        with d.session_factory() as session, session.begin():
            said = RoomMessage(
                project_id=project_id,
                topic_id=topic_id,
                author_kind="office",
                author_role=None,
                author_name="The office",
                body=room_tools.shown(str(action["server"]), str(action["tool"]), result),
                reply_to=message_id,
                thread_id=thread_id,
            )
            session.add(said)
            session.flush()
            result_id = said.id
        return {"action": settle(result_id=result_id, done_by=owner.name)}


def work_possible(d: RouterDeps) -> bool:
    """Whether a thread may be set to work: the coder on the Mac is there (36b)."""
    with d.session_factory() as session:
        return coder_jobs.work_possible(
            session, bool(d.settings.workspaces_writable), datetime.now(UTC)
        )

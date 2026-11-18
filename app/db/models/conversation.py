"""Tables for discussions: topics, room messages, ideas and watches."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, CreatedAt, Identity, UpdatedAt


class ChannelThread(Identity, CreatedAt, UpdatedAt, Base):
    """What one chat thread remembers for one app: the proposal waiting for a
    yes, and its last turns. Kept here so a restart does not forget them."""

    __tablename__ = "channel_threads"
    __table_args__ = (
        Index("uq_channel_threads_key", "app_id", "channel_id", "thread_key", unique=True),
    )

    # The chat app (office bot or an agent's bot). Apps are never deleted.
    app_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    channel_id: Mapped[str] = mapped_column(String(64))
    # The thread's timestamp, or "" for a message outside a thread.
    thread_key: Mapped[str] = mapped_column(String(64))
    # [project id or null, project name, objective] while a proposal waits.
    proposal_json: Mapped[list[Any] | None] = mapped_column(JSONB)
    proposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    turns_json: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class Topic(Identity, CreatedAt, UpdatedAt, Base):
    """A conversation, the unit of collaboration.

    It needs no project: talk starts before anyone knows where the work
    belongs. A project's first topic (`home`) is what its room was.
    """

    __tablename__ = "topics"
    __table_args__ = (
        Index(
            "uq_topics_project_home",
            "project_id",
            unique=True,
            postgresql_where=text("home"),
        ),
    )

    title: Mapped[str] = mapped_column(String(200))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    venture_id: Mapped[UUID | None] = mapped_column(ForeignKey("ventures.id"))
    # Who placed it: the office (from what was said) or the owner.
    scope_set_by: Mapped[str | None] = mapped_column(String(16))
    home: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    lead_profile_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    participants_json: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    parent_topic_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    branched_from_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # Where we were, what is decided, what is open.
    brief_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(16), server_default="open")
    revisit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the owner last opened it: coming back after a while shows its brief.
    owner_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # The thread's own settings, from its composer: the model its
    # replies use (null is the office's choice for the role); whether work
    # started here may write to a folder (read | work); what it is for, and
    # what work started from it may spend (null is the hand-off's default).
    model_connection_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("model_connections.id", ondelete="SET NULL")
    )
    access: Mapped[str] = mapped_column(String(8), server_default="read")
    goal: Mapped[str | None] = mapped_column(Text)
    goal_cap_usd: Mapped[float | None] = mapped_column(Float)


class TopicItem(Identity, CreatedAt, UpdatedAt, Base):
    """What a discussion leaves behind: an idea noted in it, and later
    its open questions and decisions."""

    __tablename__ = "topic_items"

    topic_id: Mapped[UUID] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), server_default="open")
    # What the office's own call for it cost (a brief, a colleague's check).
    cost_usd: Mapped[float | None] = mapped_column(Float)
    tokens: Mapped[int | None] = mapped_column(Integer)


class Idea(Identity, CreatedAt, UpdatedAt, Base):
    """A thought the owner caught, to plan for a day and take up later.

    Caught on the floor or noted in a discussion; keeping one never calls a
    model. `plan_for` is a day in the owner's own time zone.
    """

    __tablename__ = "ideas"

    text: Mapped[str] = mapped_column(Text)
    # floor | phone | discussion | slack
    source: Mapped[str] = mapped_column(String(16), server_default="floor")
    topic_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("topics.id", ondelete="SET NULL"), index=True
    )
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # Where outside the office it came from, once: "slack:<channel>:<ts>".
    source_ref: Mapped[str | None] = mapped_column(String(160), unique=True)
    # friction | curiosity | opportunity; None is unsorted.
    kind: Mapped[str | None] = mapped_column(String(16), index=True)
    # open | someday | done | taken | dropped | merged (into another)
    status: Mapped[str] = mapped_column(String(16), server_default="open", index=True)
    plan_for: Mapped[date | None] = mapped_column(Date)
    # The work it became, once started.
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # The project it belongs to, if the owner gave it one.
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    # Time the owner blocked for it on their calendar: the event's start, and
    # the calendar change that made it.
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    calendar_change_id: Mapped[int | None] = mapped_column(BigInteger)


class FollowUp(Identity, CreatedAt, UpdatedAt, Base):
    """What was due on a day, and where the owner says it stands (app/followups)."""

    __tablename__ = "follow_ups"

    # calendar | idea | owner
    source: Mapped[str] = mapped_column(String(16))
    # "calendar:<source id>:<event uid>", "idea:<id>" or "owner:<moment>:<title>".
    source_ref: Mapped[str] = mapped_column(String(600), unique=True)
    title: Mapped[str] = mapped_column(Text)
    due_on: Mapped[date] = mapped_column(Date)
    # open | doing | done | dropped | parked | not_a_task | gone
    status: Mapped[str] = mapped_column(String(16), server_default="open", index=True)
    progress: Mapped[str] = mapped_column(Text, server_default="")
    # Each word on it: [{"at", "status", "note", "via"}].
    notes_json: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # Times asked since the owner last said anything about it.
    asked_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    last_asked_on: Mapped[date | None] = mapped_column(Date)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The recap that told the owner.
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnswerVerdict(Identity, CreatedAt, UpdatedAt, Base):
    """The owner's 👍 or 👎 on one answer of the lead:
    a discussion reply or a task's answer, never both; the latest press wins.
    `redo_run_id` is the full-team work it led to, when the owner asked for it."""

    __tablename__ = "answer_verdicts"
    __table_args__ = (
        CheckConstraint(
            "(room_message_id IS NULL) <> (run_id IS NULL)", name="ck_answer_verdicts_one_target"
        ),
        Index(
            "uq_answer_verdicts_run",
            "run_id",
            unique=True,
            postgresql_where=text("room_message_id IS NULL"),
        ),
    )

    room_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("room_messages.id", ondelete="CASCADE"), unique=True
    )
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    verdict: Mapped[str] = mapped_column(String(8))
    note: Mapped[str | None] = mapped_column(Text)
    redo_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class IdeaSort(Identity, CreatedAt, Base):
    """One "Sort my ideas" call: its suggestions, each answered by the
    owner, and what the call cost (counted in the month's spending)."""

    __tablename__ = "idea_sorts"

    ideas_count: Mapped[int] = mapped_column(Integer)
    suggestions_json: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    cost_usd: Mapped[float | None] = mapped_column(Float)
    tokens: Mapped[int | None] = mapped_column(Integer)


class Watch(Identity, CreatedAt, UpdatedAt, Base):
    """A page or a topic the office checks for the owner.

    Read and compared for free; one small model call only when it changed.
    `last_lines_json` is what the last check read, to compare the next with.
    """

    __tablename__ = "watches"

    title: Mapped[str] = mapped_column(String(200))
    # page (an address) | search (words)
    kind: Mapped[str] = mapped_column(String(16))
    target: Mapped[str] = mapped_column(Text)
    condition: Mapped[str] = mapped_column(Text, server_default="")
    every_minutes: Mapped[int] = mapped_column(Integer)
    post_to_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("channel_destinations.id", ondelete="SET NULL")
    )
    # The Slack thread a watch started from answers in (0066).
    post_thread: Mapped[str | None] = mapped_column(String(64))
    ends_on: Mapped[date | None] = mapped_column(Date)
    daily_cap_usd: Mapped[float] = mapped_column(Float)
    # active | paused | ended | stopped
    status: Mapped[str] = mapped_column(String(16), server_default="active", index=True)
    stopped_reason: Mapped[str | None] = mapped_column(String(200))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_digest: Mapped[str | None] = mapped_column(String(64))
    last_lines_json: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    last_said: Mapped[str | None] = mapped_column(Text)
    last_said_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))
    failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class WatchCheck(Base):
    """One check of a watch: what happened, and what its model call cost."""

    __tablename__ = "watch_checks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    watch_id: Mapped[UUID] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # baseline | same | changed | told | capped | failed
    outcome: Mapped[str] = mapped_column(String(16))
    said: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(String(64))
    cost_usd: Mapped[float | None] = mapped_column(Float)
    tokens: Mapped[int | None] = mapped_column(Integer)


class RoomMessage(CreatedAt, Base):
    """Something said in a project's room: by the owner, an agent, or the office.

    Only what was said is stored. What a task did — a step finishing, a task
    starting or stopping — is read from `run_events` and its artifacts when the
    room is shown, so it is never recorded twice. The id is the room's cursor.
    """

    __tablename__ = "room_messages"
    __table_args__ = (
        Index(
            "uq_room_messages_client_key",
            "topic_id",
            "client_key",
            unique=True,
            postgresql_where=text("client_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # A topic's project, when it has one.
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    # The topic it was said in.
    topic_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    # The request id the owner's browser sent with it: a resend after a
    # dropped connection finds this message instead of saying it twice.
    client_key: Mapped[str | None] = mapped_column(String(64))
    # owner | agent | office
    author_kind: Mapped[str] = mapped_column(String(16))
    author_role: Mapped[str | None] = mapped_column(String(64))
    author_name: Mapped[str] = mapped_column(String(128))
    # The agent profile that spoke, when an agent did.
    author_profile_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # Named `body` here because `text` would shadow SQLAlchemy's text() below.
    body: Mapped[str] = mapped_column("text", Text)
    # The message this answers, when it is a reply.
    reply_to: Mapped[int | None] = mapped_column(BigInteger)
    # The message its thread hangs from, when it is said in a thread.
    thread_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # The task it is about, when it is about one.
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    cost_usd: Mapped[float | None] = mapped_column(Float)
    # Model tokens a reply used, for the agent's daily room allowance.
    tokens: Mapped[int | None] = mapped_column(Integer)
    # Buttons offered with it (start as a task, send as direction). A button,
    # never the text, is what acts.
    actions_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )

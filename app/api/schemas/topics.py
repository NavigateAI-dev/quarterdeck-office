"""API request and response shapes: topics."""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.api.schemas.base import StrictRequest


class TeamRoomPost(StrictRequest):
    """The owner's word in a task's room, to everyone or to one step."""

    text: str = Field(min_length=1, max_length=1000)
    to_step: str | None = Field(default=None, max_length=64)

    @field_validator("text")
    @classmethod
    def said_something(cls, value: str) -> str:
        """Blank is not a post."""
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("Say something")
        return cleaned


class RoomPost(StrictRequest):
    """Something the owner says in a project's room.

    Words only. Nothing typed here approves, raises a limit or starts work: a
    button on a card does that.
    """

    text: str = Field(min_length=1, max_length=4000)
    # Asked from a task's Team room: the task it is about.
    run_id: UUID | None = None
    # A request id from the browser: a resend after a dropped connection finds
    # the message already said instead of saying it twice.
    client_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
    # Files or folders the owner attached to this message.
    attachments: list[UUID] = Field(default_factory=list, max_length=10)
    # The message it replies to, and the thread it is said in ("m:<id>").
    reply_to: str | None = Field(default=None, pattern=r"^m:\d{1,18}$")
    thread_id: str | None = Field(default=None, pattern=r"^m:\d{1,18}$")
    # Ask the lead for a plan first, not an answer.
    plan: bool = False


class TopicStart(StrictRequest):
    """Start talking: a sentence, and a project only if the owner knows."""

    text: str = Field(min_length=1, max_length=4000)
    project_id: UUID | None = None
    client_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
    # Files or folders the owner attached to the first message.
    attachments: list[UUID] = Field(default_factory=list, max_length=10)


class TopicParticipant(StrictRequest):
    """A colleague brought into a discussion."""

    profile_id: UUID


class TopicBranch(StrictRequest):
    """A message branched into a discussion of its own, with what it carries."""

    message_id: str = Field(pattern=r"^m:\d{1,18}$")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    brief: str | None = Field(default=None, max_length=1200)


class TopicRevise(StrictRequest):
    """A passage of delivered work, and what the owner says about it."""

    run_id: UUID
    quote: str = Field(min_length=1, max_length=2000)
    comment: str = Field(min_length=1, max_length=2000)


class AnswerEdit(StrictRequest):
    """The owner's edit of a task's answer, against the version it was made on.

    The document itself is checked by `app.documents.edited`, which knows the
    shapes a document may take; this only bounds the request.
    """

    base_version: int = Field(ge=1)
    document: dict[str, Any]


class AnswerRewrite(StrictRequest):
    """A passage of the answer and how the owner wants it rewritten."""

    passage: str = Field(min_length=1, max_length=4000)
    instruction: str = Field(min_length=1, max_length=600)


class DecisionRecord(StrictRequest):
    """What the owner decided, from their own message; what it replaces."""

    message_id: str = Field(pattern=r"^m:\d{1,18}$")
    text: str = Field(min_length=1, max_length=2000)
    replaces: UUID | None = None


class AnswerJudged(StrictRequest):
    """The owner's 👍 or 👎 on an answer, with what was wrong if they say."""

    verdict: Literal["up", "down"]
    note: str | None = Field(default=None, max_length=1000)


class TopicSettings(StrictRequest):
    """A thread's own settings from its composer; only what is sent changes."""

    model_connection_id: UUID | None = None
    access: Literal["read", "work"] | None = None
    goal: str | None = Field(default=None, max_length=1000)
    goal_cap_usd: float | None = Field(default=None, ge=0.05, le=50)


class TopicUpdate(StrictRequest):
    """Rename a topic, place it in a project (null frees it), or rest it."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    project_id: UUID | None = None
    status: Literal["open", "resting", "archived"] | None = None


class RoomAction(StrictRequest):
    """Pressing a button the room offered on a message.

    `objective` rewords a proposed task before it starts; `target_node` aims a
    direction at one step, as in the task room.
    """

    objective: str | None = Field(default=None, min_length=1, max_length=4000)
    target_node: str | None = Field(default=None, pattern="^[a-z_]{1,32}$")
    # A proposed event, added by the owner from the calendar form.
    change_id: int | None = Field(default=None, ge=1)


class OfficeChatRequest(StrictRequest):
    """A question asked from the browser, with the screen it was asked from."""

    text: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=16)
    view: str = Field(default="", max_length=64)
    run_id: UUID | None = None
    project_id: UUID | None = None


class TitleRequest(StrictRequest):
    """What a task would be called, for the form to suggest while the owner types."""

    objective: str = Field(min_length=1, max_length=8192)

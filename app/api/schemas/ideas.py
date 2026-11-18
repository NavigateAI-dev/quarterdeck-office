"""API request and response shapes: ideas."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.api.schemas.base import StrictRequest


class IdeaKeep(StrictRequest):
    """A thought to keep, as written. Keeping it calls no model."""

    text: str = Field(min_length=1, max_length=2000)
    # Kept on a touch screen is "phone"; the office never guesses it.
    source: Literal["floor", "phone"] = "floor"
    # Friction, curiosity or opportunity; none is unsorted.
    kind: Literal["friction", "curiosity", "opportunity"] | None = None


class IdeaPlan(StrictRequest):
    """The day to take an idea up (in the owner's zone), none to unsort it,
    or someday."""

    day: date | None = None
    someday: bool = False


class IdeaKind(StrictRequest):
    kind: Literal["friction", "curiosity", "opportunity"] | None = None


class IdeaFinish(StrictRequest):
    status: Literal["done", "dropped"]


class IdeaProject(StrictRequest):
    project_id: UUID | None = None


class IdeaBlock(StrictRequest):
    """The calendar change the owner's new event made, saved from the idea."""

    change_id: int = Field(ge=1)


class IdeaText(StrictRequest):
    text: str = Field(min_length=1, max_length=2000)


class IdeaSortStart(StrictRequest):
    """Paid: the owner's press, after seeing what it may cost."""

    confirm: bool = False

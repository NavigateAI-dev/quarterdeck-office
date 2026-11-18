"""API request and response shapes: base."""

from pydantic import BaseModel, ConfigDict, Field

ROLE_DOCTRINE = Field(
    default=None,
    description="How this business line judges one role's work, keyed by role.",
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

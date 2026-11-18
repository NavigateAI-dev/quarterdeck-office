from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Shared settings for data exchanged across agent boundaries."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

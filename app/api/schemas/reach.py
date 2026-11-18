"""API request and response shapes: reach."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.schemas.base import StrictRequest


class SourceCreate(StrictRequest):
    name: str = Field(min_length=1, max_length=255)
    # A subfolder of the read-only mount, e.g. "research/2026".
    root: str = Field(min_length=1, max_length=1024)


class UploadSourceCreate(StrictRequest):
    name: str = Field(min_length=1, max_length=255)


class DocumentUpload(StrictRequest):
    path: str = Field(min_length=1, max_length=512, pattern=r"^[^/\\.][^\\]*$")
    text: str = Field(min_length=1, max_length=200_000)


class SourceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    kind: str
    root: str | None
    status: str
    document_count: int
    skipped: list[dict[str, Any]]
    indexed_at: datetime | None
    last_error: str | None


class ModelConnectionCreate(StrictRequest):
    # Any provider in the office's catalogue (GET /providers).
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    # Overrides the catalogue's address; required for a custom service.
    base_url: str | None = Field(default=None, max_length=1000, pattern=r"^https?://.+")
    display_name: str = Field(min_length=1, max_length=255)
    model_id: str = Field(min_length=1, max_length=255)
    # Optional: omit it and store the key through the credential endpoint.
    credential_ref: str | None = Field(
        default=None, pattern=r"^env:[A-Z][A-Z0-9_]{1,127}$", max_length=255
    )
    capabilities: list[str] = Field(default_factory=list, max_length=32)


class CredentialStore(StrictRequest):
    """A provider key on its way into the encrypted store. Never echoed back."""

    secret: SecretStr = Field(min_length=16, max_length=4096)


class McpServerCreate(StrictRequest):
    """A server offering tools the office did not hard-code.

    HTTPS only, and the office checks the address resolves publicly and sits on
    its host allow-list before it ever connects.
    """

    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=8, max_length=2048, pattern=r"^https://")
    # Optional: many servers are open. Stored encrypted, never echoed back.
    secret: SecretStr | None = Field(default=None, min_length=1, max_length=4096)


class DocumentEdit(StrictRequest):
    """The owner's edit of a document opened from a reply, against its version."""

    base_version: int = Field(ge=1)
    document: dict[str, Any]


class ProviderBackup(StrictRequest):
    """The backup the owner chose for one provider, or none.

    A key is a model connection's id or "gateway"; whether it exists is
    checked by the route.
    """

    backup: str | None = Field(default=None, max_length=64)
    come_back: bool = True


class McpBulkDecision(StrictRequest):
    """One decision for every tool not yet decided: one for those the
    server says only read (its claim, shown as such), one for the rest."""

    read_only: str = Field(pattern="^(allowed|needs_approval|refused)$")
    others: str = Field(pattern="^(allowed|needs_approval|refused)$")


class McpToolDecision(StrictRequest):
    """The owner's stance on one tool a server offers.

    Not a boolean: a tool that changes the world is allowed to be *proposed*
    and stops the run for the owner, which is different from being refused.
    """

    stance: str = Field(pattern="^(allowed|needs_approval|refused)$")
    # The SPEC section 18 actions this tool performs, as the owner judges it.
    # A denied action refuses the tool whatever the stance says.
    acts_as: list[str] = Field(default_factory=list, max_length=16)
    # The most one call costs, when the vendor charges per call. The office
    # counts it against the task's and the month's limits.
    price_usd: float | None = Field(default=None, gt=0, le=1000)


class ProviderModelsQuery(StrictRequest):
    """Ask a provider what a key can use, before a connection exists.

    The key is used for this one request and never stored.
    """

    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=1024)
    base_url: str | None = Field(default=None, max_length=1000, pattern=r"^https?://.+")


class ModelConnectionUpdate(StrictRequest):
    """Change which model a connection uses, its name or its address."""

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    model_id: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, max_length=1000, pattern=r"^https?://.+")


class ConnectionTest(StrictRequest):
    """A billable request is made only when the owner sets this explicitly."""

    billable: bool = False


class ModelConnectionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    provider: str
    display_name: str
    model_id: str
    status: str
    capabilities: list[str]
    credential_configured: bool
    provider_name: str = ""
    base_url: str | None = None
    last_checked_at: datetime | None
    last_error: str | None
    last_test: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class StagedFile(StrictRequest):
    """One file from the owner's computer, base64 encoded by the browser."""

    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=56_000_000)


class FolderUpload(StrictRequest):
    files: list[StagedFile] = Field(min_length=1, max_length=200)
    # The first batch of a folder replaces what was there; later ones add.
    replace: bool = False

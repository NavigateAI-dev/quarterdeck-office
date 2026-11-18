"""Tables for what the office reads: tool servers, sources, calendars."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, CreatedAt, Identity, UpdatedAt


class McpServer(Identity, CreatedAt, UpdatedAt, Base):
    """A server offering tools the office did not hard-code.

    Streamable HTTP only: the other standard transport launches the server as a
    subprocess with the owner's permissions. What a server says about its own
    tools is recorded as a claim and never read as permission.
    """

    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(String(128), unique=True)
    url: Mapped[str] = mapped_column(Text)
    # A reference into the secret store, never a value.
    credential_ref: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), server_default="UNREACHED")
    detail: Mapped[str] = mapped_column(Text, server_default="")
    server_name: Mapped[str] = mapped_column(String(128), server_default="")
    server_version: Mapped[str] = mapped_column(String(64), server_default="")
    protocol_version: Mapped[str] = mapped_column(String(32), server_default="")
    offering_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # A server that changes what it offers must be reviewed again.
    offering_digest: Mapped[str] = mapped_column(String(64), server_default="")
    # The owner's stance on each tool, keyed by name, each bound to a digest of
    # the tool as it stood when they decided.
    review_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The owner's tick: discussions that belong to no project may use this server.
    no_project_discussions: Mapped[bool] = mapped_column(Boolean, server_default="false")


class KnowledgeSource(Identity, CreatedAt, UpdatedAt, Base):
    """A selected local folder (read through the sources service) or an upload set."""

    __tablename__ = "knowledge_sources"
    __table_args__ = (Index("uq_knowledge_sources_name", "name", unique=True),)

    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32))  # folder | upload
    # Path relative to the read-only mount; None for uploads.
    root: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), server_default="PENDING")
    document_count: Mapped[int] = mapped_column(Integer, server_default="0")
    skipped_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class SourceDocument(Identity, CreatedAt, Base):
    """Extracted text of one document, with its content hash for provenance."""

    __tablename__ = "source_documents"
    __table_args__ = (Index("uq_source_documents_source_path", "source_id", "path", unique=True),)

    source_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_sources.id"), index=True)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    text: Mapped[str] = mapped_column(Text)


class ProjectMcpServer(Identity, CreatedAt, Base):
    """A grant: this project's runs may use this server's reviewed tools.

    Granted per project like a knowledge source, and revocable the same way. A
    server the owner added but granted nowhere reaches no task at all.
    """

    __tablename__ = "project_mcp_servers"
    __table_args__ = (Index("uq_project_mcp_servers", "project_id", "server_id", unique=True),)

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    server_id: Mapped[UUID] = mapped_column(ForeignKey("mcp_servers.id"))


class Workspace(Identity, CreatedAt, Base):
    """A folder of the owner's, inside the approved root, given to a business space
    (`app/workspaces.py`)."""

    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("venture_id", "path", name="uq_workspaces_space_path"),)

    venture_id: Mapped[UUID] = mapped_column(ForeignKey("ventures.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    # Relative to the approved root, POSIX: "clients/acme/site".
    path: Mapped[str] = mapped_column(String(500))
    # read | work
    access: Mapped[str] = mapped_column(String(8))
    # The branch coding work starts from, for work access (6d).
    default_branch: Mapped[str | None] = mapped_column(String(255))
    granted_by: Mapped[str] = mapped_column(String(128))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkspaceGrant(CreatedAt, Base):
    """Who gave, changed or took back a space's folder, and when. Kept after the
    folder is detached, so no foreign key to it."""

    __tablename__ = "workspace_grants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    venture_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), index=True)
    path: Mapped[str] = mapped_column(String(500))
    # granted | access_changed | revoked | merged
    action: Mapped[str] = mapped_column(String(16))
    access: Mapped[str] = mapped_column(String(8))
    by: Mapped[str] = mapped_column(String(128))
    # The branch merged, for a merge (6d).
    detail: Mapped[str | None] = mapped_column(String(255))


class ProjectSource(Identity, CreatedAt, Base):
    """A grant: this project's runs may retrieve from this source."""

    __tablename__ = "project_sources"
    __table_args__ = (
        Index("uq_project_sources_project_source", "project_id", "source_id", unique=True),
    )

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    source_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_sources.id"), index=True)


class CalendarSource(Identity, CreatedAt, UpdatedAt, Base):
    """One of the owner's calendars, connected read-only.

    The link or password is in the secret store under secret_name; this row
    holds only what the page shows and where the last sync got to.
    """

    __tablename__ = "calendar_sources"

    # "ics": a calendar link; "caldav": an account (iCloud, Fastmail, ...).
    kind: Mapped[str] = mapped_column(String(16))
    # Which service, for the card: google, icloud, outlook, fastmail, other.
    provider: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    # The CalDAV server and account; a link's address is a secret, never here.
    server_url: Mapped[str | None] = mapped_column(Text)
    username: Mapped[str | None] = mapped_column(String(255))
    secret_name: Mapped[str] = mapped_column(String(128))
    # The account's calendars the owner chose: [{"url", "name"}].
    calendars_json: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))
    event_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class CalendarEvent(Base):
    """An event read from a connected calendar, a repeat's each occurrence apart."""

    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint("source_id", "uid", name="uq_calendar_events_source_uid"),
        Index("ix_calendar_events_time", "starts_at", "ends_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("calendar_sources.id", ondelete="CASCADE"), index=True
    )
    uid: Mapped[str] = mapped_column(String(512))
    calendar: Mapped[str] = mapped_column(String(255), server_default="")
    # The calendar's own identifier where its source gives one (the Mac's), so
    # an edit reaches the right calendar.
    calendar_id: Mapped[str] = mapped_column(String(255), server_default="")
    title: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    busy: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    location: Mapped[str] = mapped_column(Text, server_default="")
    description: Mapped[str] = mapped_column(Text, server_default="")
    attendees: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class CalendarChange(Base):
    """An edit the owner made to their calendar, waiting to be applied.

    For the Mac's calendars the helper applies it through macOS and says how
    it went; the event then comes back with the next read.
    """

    __tablename__ = "calendar_changes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("calendar_sources.id", ondelete="CASCADE"), index=True
    )
    # create, update or delete.
    action: Mapped[str] = mapped_column(String(16))
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # pending; sent (handed to the Mac helper); done, failed or unknown.
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    error: Mapped[str | None] = mapped_column(String(64))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArchiveImport(Identity, CreatedAt, Base):
    """One export the owner gave the office, and what came of it."""

    __tablename__ = "archive_imports"

    filename: Mapped[str] = mapped_column(String(255))
    # running | done | failed
    status: Mapped[str] = mapped_column(String(16), server_default="running")
    error: Mapped[str | None] = mapped_column(String(64))
    conversations_new: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    messages_new: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    conversations_seen: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArchiveConversation(Identity, CreatedAt, Base):
    """A conversation from another AI tool, as it was (app/archive)."""

    __tablename__ = "archive_conversations"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_archive_conversations_external"),
    )

    # chatgpt | claude
    provider: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text, server_default="")
    # excluded (no agent reaches it, how each arrives) | active | deleted (its words are gone)
    state: Mapped[str] = mapped_column(String(16), server_default="excluded")
    # Holds a secret or an identity number: the owner opens it one by one.
    sensitive: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), default=False)
    # How many of each kind were found: {"iban": 2}. Never the values.
    flags_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    import_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("archive_imports.id", ondelete="SET NULL")
    )


class ArchiveReading(Identity, CreatedAt, Base):
    """One model call over a conversation, on the owner's press, and what it cost."""

    __tablename__ = "archive_readings"
    __table_args__ = (Index("ix_archive_readings_created_at", "created_at"),)

    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("archive_conversations.id", ondelete="SET NULL")
    )
    messages_read: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    messages_total: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    proposed: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    # Answers that did not hold up: no such message, or a quote it does not hold.
    dropped: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)


class ArchiveMemory(Identity, CreatedAt, Base):
    """Something worth keeping from a conversation (app/archive/memories.py)."""

    __tablename__ = "archive_memories"
    __table_args__ = (
        UniqueConstraint("conversation_id", "digest", name="uq_archive_memories_digest"),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("archive_conversations.id", ondelete="CASCADE")
    )
    # decision | preference | fact | idea | question
    kind: Mapped[str] = mapped_column(String(16))
    # owner | ai: from the role of the message it rests on, never from a model.
    said_by: Mapped[str] = mapped_column(String(8))
    content: Mapped[str] = mapped_column(Text)
    corrected_from: Mapped[str | None] = mapped_column(Text)
    # proposed | kept | rejected. Only kept reaches an agent or an export.
    status: Mapped[str] = mapped_column(String(16), server_default="proposed", index=True)
    # [{"message_id": 123, "quote": "..."}]
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    digest: Mapped[str] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArchiveAccess(Identity, CreatedAt, Base):
    """Who reached AI history, for the owner to read. Words of a query are masked."""

    __tablename__ = "archive_access"
    __table_args__ = (Index("ix_archive_access_created_at", "created_at"),)

    # An agent's name, or "You" for an export.
    who: Mapped[str] = mapped_column(String(255))
    # discussion | slack | owner
    place: Mapped[str] = mapped_column(String(16))
    # search | read | reading | export
    action: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str] = mapped_column(Text, server_default="")


class ArchiveMessage(Base):
    """One message of an imported conversation: the evidence anything later rests on."""

    __tablename__ = "archive_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "external_id", name="uq_archive_messages_external"),
        Index("ix_archive_messages_sent_at", "sent_at"),
        Index(
            "ix_archive_messages_content_trgm",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "public.gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("archive_conversations.id", ondelete="CASCADE")
    )
    external_id: Mapped[str] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer)
    # user | assistant | tool
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

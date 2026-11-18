"""Tables for the coder on the owner's Mac (Stage 36): the helper, and the jobs
the office hands it."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, CreatedAt, Identity, UpdatedAt


class CoderHelper(Identity, CreatedAt, UpdatedAt, Base):
    """The Quarterdeck Coder helper on this Mac: when it last asked, and the
    coding CLIs it found there. One row."""

    __tablename__ = "coder_helpers"

    only: Mapped[bool] = mapped_column(Boolean, unique=True, default=True)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[str] = mapped_column(String(32), server_default="")
    # The folder it works in on the Mac, as it says: "/Users/me/dev".
    root: Mapped[str] = mapped_column(String(500), server_default="")
    # [{"id", "label", "path", "version", "signed_in", "detail"}]
    clis_json: Mapped[list[Any]] = mapped_column(JSONB, default=list)


class CoderJob(Identity, CreatedAt, Base):
    """One piece of coding handed to the helper, at most once.

    queued -> claimed -> running -> done | failed; a claimed job not heard from
    for ten minutes becomes unknown and is never handed out again. The owner's
    Stop is `stop_asked`; their comments wait in `notes_json` until the helper
    takes them as the next turn.
    """

    __tablename__ = "coder_jobs"

    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    # code (36a); merge and discard follow with the review (36c).
    kind: Mapped[str] = mapped_column(String(16))
    # Relative to the helper's root (~/dev), POSIX: "clients/acme/site".
    folder: Mapped[str] = mapped_column(String(500))
    base_branch: Mapped[str] = mapped_column(String(255))
    base_commit: Mapped[str | None] = mapped_column(String(64))
    # Always office/<slug>; the helper refuses anything else.
    branch: Mapped[str] = mapped_column(String(255))
    cli: Mapped[str] = mapped_column(String(32))
    brief: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), index=True)
    stop_asked: Mapped[bool] = mapped_column(Boolean, server_default="false")
    notes_json: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # The approval it carries out, by its scope digest: one job per approval.
    scope_digest: Mapped[str] = mapped_column(String(64), server_default="", index=True)
    # A build's office folder (on the engineering volume): the helper starts
    # its worktree from that folder's version at `base_commit` (36b).
    seed_from: Mapped[str] = mapped_column(String(500), server_default="")
    # A comment marked "now": the helper stops the turn and resumes with it.
    notes_now: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # The CLI's own session, which a comment or the owner's take-over resumes.
    session: Mapped[str] = mapped_column(String(120), server_default="")
    # Relative to the helper's root, once made.
    worktree: Mapped[str] = mapped_column(String(500), server_default="")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heard_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What the helper reported: branch, head, base, exit, message, error.
    outcome_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

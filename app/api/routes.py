import re
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.credentials import (
    SECRET_PREFIX,
    open_secret_store,
)
from app.db.models import (
    EntityFact,
    InstalledSkill,
    McpServer,
    Project,
)
from app.mcp import oauth as mcp_oauth
from app.mcp import store as mcp_store
from app.mcp.client import McpClient, McpError
from app.mcp.oauth import OAuthError
from app.orchestrator.state import RunStatus
from app.run_actions import (  # noqa: F401 - rework_note is re-exported
    CANCELLABLE,
    INSTRUCTABLE,
    ActionRefused,
    RunActions,
    record_review,
    rework_note,
)
from app.secret_store import SecretStoreError
from app.sources.client import ExchangeIndexer, Indexer


def found[T](instance: T | None, label: str) -> T:
    if instance is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return instance


DEFAULT_NOTE = {
    "accept": "Accepted as sufficient without further comment.",
    "revise": "",
    "close": "Closed without further comment.",
}


# What one browser folder import may carry. Larger libraries belong in the
# shared folder, which is read in place rather than copied.
UPLOAD_MAX_BYTES = 40 * 1024 * 1024  # per request; a browser sends a folder in batches


def safe_relative_path(name: str) -> str | None:
    """A relative path safe to write under the staging directory, or None."""
    cleaned = (name or "").strip().replace("\\", "/")
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or part.startswith(".") for part in parts):
        return None
    if any(len(part) > 120 for part in parts) or len(parts) > 12:
        return None
    return "/".join(parts)


def build_indexer(settings: Settings) -> Indexer:
    return ExchangeIndexer(settings.sources_exchange)


# Below these, a judgment is treated as not knowing and the owner is asked.
INTAKE_CONFIDENCE = 0.6


# What a finished task can be downloaded as. Markdown and HTML need nothing;
# Word and Excel use the pure-Python writers the owner approved.
DOWNLOAD_TYPES = {
    "md": ("text/markdown; charset=utf-8", "md"),
    "html": ("text/html; charset=utf-8", "html"),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pptx",
    ),
}


def slug(text: str) -> str:
    """A filename a person recognises, from the task's own words.

    Keeps the owner's own script: a task named in Chinese should download with
    a Chinese name. The header carries an ASCII fallback beside it, because a
    header value itself cannot hold anything but latin-1.
    """
    cleaned = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    return re.sub(r"[\s_-]+", "-", cleaned)[:60].strip("-").lower()


def attachment(name: str, extension: str) -> str:
    """A Content-Disposition that survives a non-latin-1 filename (RFC 5987)."""
    full = f"{name}.{extension}"
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "-", full).strip("-") or f"document.{extension}"
    quoted = quote(full, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"


# The roles a business line can speak to. Anything else is ignored rather than
# stored, so a typo cannot become doctrine nobody reads.
DOCTRINE_ROLES = (
    "planner",
    "chief",
    "researcher",
    "strategist",
    "critic",
    "summarist",
    "advisor",
)
DOCTRINE_LIMIT = 2000


def clean_role_doctrine(given: dict[str, str] | None) -> dict[str, str]:
    if not given:
        return {}
    return {
        role: str(text).strip()[:DOCTRINE_LIMIT]
        for role, text in given.items()
        if role in DOCTRINE_ROLES and str(text).strip()
    }


def entity_view(row: Any, session: Any) -> dict[str, Any]:
    waiting = session.scalar(
        select(func.count())
        .select_from(EntityFact)
        .where(EntityFact.entity_id == row.id, EntityFact.status == "CANDIDATE")
    )
    kept = session.scalar(
        select(func.count())
        .select_from(EntityFact)
        .where(EntityFact.entity_id == row.id, EntityFact.status == "APPROVED")
    )
    return {
        "id": str(row.id),
        "name": row.name,
        "kind": row.kind,
        "aliases": list(row.aliases_json or []),
        "venture_id": str(row.venture_id) if row.venture_id else None,
        "status": row.status,
        "waiting": int(waiting or 0),
        "kept": int(kept or 0),
    }


def venture_view(row: Any, session: Any) -> dict[str, Any]:
    projects = session.scalar(
        select(func.count()).select_from(Project).where(Project.venture_id == row.id)
    )
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "doctrine": row.doctrine,
        "role_doctrine": dict(row.role_doctrine_json or {}),
        "status": row.status,
        "projects": int(projects or 0),
        # This line's pack: skills offered only to its tasks.
        "skills": list(row.skill_ids_json or []),
    }


def known_skills(session: Any, skill_ids: list[str]) -> list[str]:
    """A pack names installed skills only; anything else is refused."""
    wanted = list(dict.fromkeys(str(skill) for skill in skill_ids))
    installed = set(
        session.scalars(
            select(InstalledSkill.skill_id).where(InstalledSkill.status != "UNINSTALLED")
        )
    )
    if any(skill not in installed for skill in wanted):
        raise HTTPException(status_code=422, detail="unknown_skill")
    return wanted


# A task whose limits the owner can still change: it has work left to do.
# PENDING and RECOVERING are stored statuses without a RunStatus member.
LIMIT_EDITABLE = ("PENDING", "RUNNING", "RECOVERING", str(RunStatus.PAUSED))


async def list_mcp_tools(
    session_factory: sessionmaker[Session], settings: Settings, server_id: UUID
) -> dict[str, Any]:
    """Ask a tool server what it offers now, with its key or signed-in token."""
    with session_factory() as session:
        server = found(session.get(McpServer, server_id), "Server")
        url, reference = server.url, server.credential_ref
    # The same allow-list that bounds page fetching bounds this.
    allow_list = mcp_store.allow_hosts(settings)
    try:
        token = await mcp_store.bearer_for(reference, settings, allow_list)
        offering = await McpClient().offering(url, token, allow_list)
    except SecretStoreError as missing:
        raise HTTPException(status_code=409, detail=str(missing.args[0])) from None
    except (McpError, OAuthError) as refused:
        with session_factory() as session, session.begin():
            mcp_store.record_failure(session, server_id, McpError(refused.code))
            view = mcp_store.view(found(session.get(McpServer, server_id), "Server"))
        if refused.code == "sign_in_required":
            # Not a failure: it answered, and waits for the owner to sign in.
            return {**view, "changed": False}
        raise HTTPException(status_code=502, detail=f"mcp_{refused.code}") from None
    with session_factory() as session, session.begin():
        recorded = mcp_store.record_offering(session, server_id, offering)
        view = mcp_store.view(found(session.get(McpServer, server_id), "Server"))
    return {**view, "changed": recorded["changed"]}


def build_mcp_callback_router(
    session_factory: sessionmaker[Session], settings: Settings, sign_ins: mcp_oauth.Flows
) -> APIRouter:
    """The one public tool-server route: where a vendor returns the owner's browser.

    Public because that redirect does not carry the owner's SameSite=Strict
    cookie. It is bound to the owner by a single-use, short-lived `state` that
    only an owner-authenticated sign-in request could have created.
    """
    router = APIRouter()

    @router.get("/mcp/oauth/callback")
    async def mcp_sign_in_callback(
        state: str = "", code: str = "", error: str = ""
    ) -> RedirectResponse:
        """Where the vendor returns the owner's browser. Redeems the sign-in once,
        keeps the tokens encrypted, lists the tools, and goes back to Tools."""
        held = sign_ins.take(state) if state else None
        failed = "/#tools/sign-in-failed"
        if held is not None and held.get("back") == "accounts":
            failed = "/#accounts/sign-in-failed"
        if held is None or error or not code:
            return RedirectResponse(failed, status_code=303)
        server_id = UUID(held["server_id"])
        try:
            stored = await mcp_oauth.exchange(
                mcp_oauth.held_server(held),
                mcp_oauth.held_registration(held),
                code,
                held["verifier"],
                held["redirect_uri"],
                mcp_store.allow_hosts(settings),
            )
        except OAuthError:
            return RedirectResponse(failed, status_code=303)
        name = mcp_store.OAUTH_REFERENCE.format(id=server_id)
        with session_factory() as session, session.begin():
            server = session.get(McpServer, server_id)
            if server is None:
                return RedirectResponse(failed, status_code=303)
            previous = server.credential_ref or ""
            try:
                open_secret_store(settings).put(name, stored)
            except SecretStoreError:
                return RedirectResponse(failed, status_code=303)
            server.credential_ref = f"{SECRET_PREFIX}{name}"
        if previous.startswith(SECRET_PREFIX) and previous != f"{SECRET_PREFIX}{name}":
            # A key it had before is replaced by the sign-in, and forgotten.
            try:
                open_secret_store(settings).delete(previous.removeprefix(SECRET_PREFIX))
            except SecretStoreError:
                pass  # Already gone; the sign-in is what it uses now.
        try:
            await list_mcp_tools(session_factory, settings, server_id)
        except HTTPException:
            pass  # Signed in; the card says what listing the tools met.
        if held.get("back") == "accounts":
            return RedirectResponse(f"/#accounts/signed-in/{server_id}", status_code=303)
        return RedirectResponse(f"/#tools/signed-in/{server_id}", status_code=303)

    return router

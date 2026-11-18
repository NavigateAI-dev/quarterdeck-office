"""Recording an MCP server, and what it last offered.

A server is a durable thing the owner added, not a connection: the office keeps
its address, a reference to its credential, and the tool list as it stood when
last asked — with a digest of that list, so a server that changes what it offers
can be told apart from one that has not.

Nothing here is callable. A recorded offering is a proposal for the owner to
review, in the same way a recorded lesson is.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import McpServer, Project, ProjectMcpServer
from app.mcp import review as mcp_review
from app.mcp.calling import Reachable
from app.mcp.client import McpError, Offering

UNREACHED = "UNREACHED"
REACHABLE = "REACHABLE"
UNREACHABLE = "UNREACHABLE"
# It answered, and asks the owner to sign in with their account first.
NEEDS_SIGN_IN = "NEEDS_SIGN_IN"
# Where a signed-in server's tokens are kept, beside a pasted key's name.
OAUTH_REFERENCE = "mcp-oauth-{id}"


def digest_of(tools: list[dict[str, Any]]) -> str:
    """A digest of what a server offers, stable against ordering."""
    material = sorted(
        json.dumps({k: tool.get(k) for k in ("name", "description", "schema")}, sort_keys=True)
        for tool in tools
    )
    return hashlib.sha256("\n".join(material).encode()).hexdigest()


def as_rows(offering: Offering) -> list[dict[str, Any]]:
    """The offered tools as stored: the server's claims kept as claims."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "schema": tool.schema,
            # What the server asserts about itself. Recorded so the owner can
            # see it; never read as permission by anything in the office.
            "claims": tool.claims,
        }
        for tool in offering.tools
    ]


def record_offering(session: Session, server_id: UUID, offering: Offering) -> dict[str, Any]:
    """Store what a server offered, and whether that differs from before."""
    server = session.get(McpServer, server_id)
    if server is None:
        raise LookupError("mcp_server_not_found")
    tools = as_rows(offering)
    digest = digest_of(tools)
    changed = bool(server.offering_digest) and server.offering_digest != digest
    server.status = REACHABLE
    server.detail = ""
    server.server_name = offering.server_name
    server.server_version = offering.server_version
    server.protocol_version = offering.protocol_version
    server.offering_json = tools
    server.offering_digest = digest
    server.listed_at = datetime.now(UTC)
    session.flush()
    return {"tools": len(tools), "changed": changed, "digest": digest}


def record_failure(session: Session, server_id: UUID, error: McpError) -> None:
    """A server that could not be reached says so, rather than going quiet."""
    server = session.get(McpServer, server_id)
    if server is None:
        raise LookupError("mcp_server_not_found")
    # A server asking for sign-in is not unreachable: it answered.
    server.status = NEEDS_SIGN_IN if error.code == "sign_in_required" else UNREACHABLE
    # The code, never the server's own text: it is third-party content and
    # would be shown to the owner.
    server.detail = error.code
    server.listed_at = datetime.now(UTC)
    session.flush()


def view(server: McpServer) -> dict[str, Any]:
    """One server, as the office shows it. Never carries the credential."""
    tools = server.offering_json or []
    review = server.review_json or {}
    return {
        "id": str(server.id),
        "name": server.name,
        "url": server.url,
        "has_credential": bool(server.credential_ref),
        # Signed in with an account rather than given a key.
        "signed_in": "mcp-oauth-" in (server.credential_ref or ""),
        "status": server.status,
        "detail": server.detail,
        "server_name": server.server_name,
        "server_version": server.server_version,
        "protocol_version": server.protocol_version,
        # Each tool with the owner's stance beside the server's own claims.
        "tools": mcp_review.reviewed(tools, review),
        "awaiting_review": mcp_review.outstanding(tools, review),
        "callable": len(mcp_review.callable_tools(tools, review)),
        "no_project_discussions": server.no_project_discussions,
        "listed_at": server.listed_at.isoformat() if server.listed_at else None,
    }


def listed(session: Session) -> list[dict[str, Any]]:
    return [view(row) for row in session.scalars(select(McpServer).order_by(McpServer.name))]


def record_decision(
    session: Session,
    server_id: UUID,
    tool_name: str,
    stance: str,
    owner: str,
    acts_as: list[str] | None = None,
    price_usd: float | None = None,
) -> dict[str, Any]:
    """The owner's stance on one tool this server offers, and its price."""
    server = session.get(McpServer, server_id)
    if server is None:
        raise LookupError("mcp_server_not_found")
    tool = next((row for row in (server.offering_json or []) if row.get("name") == tool_name), None)
    if tool is None:
        raise LookupError("mcp_tool_not_offered")
    server.review_json = mcp_review.decide(
        server.review_json or {}, tool, stance, owner, acts_as=acts_as or [], price_usd=price_usd
    )
    session.flush()
    return {"tool": tool_name, "stance": stance}


def grant(session: Session, project_id: UUID, server_id: UUID) -> bool:
    """Let this project's runs use this server. Granting twice is not an error."""
    from app.db.models import ProjectMcpServer

    if session.get(McpServer, server_id) is None:
        raise LookupError("mcp_server_not_found")
    existing = session.scalar(
        select(ProjectMcpServer).where(
            ProjectMcpServer.project_id == project_id,
            ProjectMcpServer.server_id == server_id,
        )
    )
    if existing is not None:
        return False
    session.add(ProjectMcpServer(project_id=project_id, server_id=server_id))
    session.flush()
    return True


def revoke(session: Session, project_id: UUID, server_id: UUID) -> bool:
    """Take the grant away. Its tools stop being offered at once."""
    from app.db.models import ProjectMcpServer

    existing = session.scalar(
        select(ProjectMcpServer).where(
            ProjectMcpServer.project_id == project_id,
            ProjectMcpServer.server_id == server_id,
        )
    )
    if existing is None:
        return False
    session.delete(existing)
    session.flush()
    return True


def granted_to(session: Session, project_id: UUID | None) -> list[McpServer]:
    """The servers this project may use. Another project's are not among them.

    With no project, the servers the owner ticked for discussions with no project.
    """
    from app.db.models import ProjectMcpServer

    if project_id is None:
        return list(
            session.scalars(
                select(McpServer)
                .where(McpServer.no_project_discussions.is_(True))
                .order_by(McpServer.name)
            )
        )
    return list(
        session.scalars(
            select(McpServer)
            .join(ProjectMcpServer, ProjectMcpServer.server_id == McpServer.id)
            .where(ProjectMcpServer.project_id == project_id)
            .order_by(McpServer.name)
        )
    )


def told_about(session: Session, project_id: UUID) -> list[str]:
    """What a task in this project is told it may use, one line per tool.

    Only reviewed tools: an agent is never told about a tool it could not call,
    because naming one it cannot use invites it to ask for something the office
    will refuse.
    """
    lines: list[str] = []
    for server in granted_to(session, project_id):
        for tool in mcp_review.callable_tools(server.offering_json or [], server.review_json or {}):
            asks = mcp_review.needs_approval(tool, server.review_json or {})
            lines.append(
                f"{server.name} · {tool.get('name')}: {tool.get('description') or 'no description'}"
                + (" (stops for the owner's approval)" if asks else "")
            )
    return lines


def out_of_reach(session: Session, project_id: UUID | None) -> list[str]:
    """Connected servers this project may not use, one line each, naming the active
    projects that may, so an agent can say why rather than guess."""
    reachable = {server.id for server in granted_to(session, project_id)}
    lines: list[str] = []
    for server in session.scalars(select(McpServer).order_by(McpServer.name)):
        tools = list(server.offering_json or [])
        if (
            server.id in reachable
            or server.status != "REACHABLE"
            or not mcp_review.callable_tools(tools, dict(server.review_json or {}))
        ):
            continue
        names = session.scalars(
            select(Project.name)
            .join(ProjectMcpServer, ProjectMcpServer.project_id == Project.id)
            .where(ProjectMcpServer.server_id == server.id, Project.status == "ACTIVE")
            .order_by(Project.name)
        ).all()
        places = [
            *names,
            *(["discussions with no project"] if server.no_project_discussions else []),
        ]
        where = f"granted to: {', '.join(places)}" if places else "not granted to any project yet"
        lines.append(f"- {server.name} ({where})")
    return lines


def allow_hosts(settings: Any) -> list[str]:
    """The host allow-list, as page fetching reads it. It bounds MCP too."""
    return [
        part.strip().lower().lstrip("*.")
        for part in settings.fetch_allow_hosts.split(",")
        if part.strip()
    ]


def reachable_for(session: Session, project_id: UUID | None) -> list[tuple[Reachable, str | None]]:
    """The servers a run in this project may call, with their credential's
    reference, not yet resolved: resolving a sign-in may mean refreshing it.

    A server granted with no reviewed tool is not handed over at all: there
    would be nothing it could be asked to do.
    """
    rows: list[tuple[Reachable, str | None]] = []
    for server in granted_to(session, project_id):
        tools = list(server.offering_json or [])
        review = dict(server.review_json or {})
        if not mcp_review.callable_tools(tools, review):
            continue
        rows.append(
            (
                Reachable(
                    server_id=server.id,
                    name=server.name,
                    url=server.url,
                    token=None,
                    tools=tools,
                    review=review,
                ),
                server.credential_ref,
            )
        )
    return rows


async def bearer_for(reference: str | None, settings: Any, allow_list: list[str]) -> str | None:
    """The token to send: a pasted key as it is, or a current access token.

    Raises SecretStoreError when the credential is gone, and OAuthError when a
    sign-in has lapsed and could not be refreshed.
    """
    from app.credentials import SECRET_PREFIX, open_secret_store, resolve_credential
    from app.mcp import oauth

    if not reference:
        return None
    raw = resolve_credential(reference, settings)
    from app import accounts

    if accounts.is_account_credential(raw):
        return await _account_token(raw, reference, settings)

    def save(value: str) -> None:
        if reference.startswith(SECRET_PREFIX):
            open_secret_store(settings).put(reference.removeprefix(SECRET_PREFIX), value)

    return await oauth.token_for(raw, allow_list, save)


async def _account_token(raw: str, reference: str, settings: Any) -> str:
    """A current token for the owner's Google or Microsoft account, with
    the sign-in app it shares with the calendar."""
    from app import accounts
    from app.calendars.oauth import SignInError
    from app.credentials import SECRET_PREFIX, open_secret_store
    from app.mcp.oauth import OAuthError

    store = open_secret_store(settings)

    def app_for(key: str) -> dict[str, str]:
        return dict(json.loads(store.get(f"calendar-app-{key}")))

    def save(value: str) -> None:
        if reference.startswith(SECRET_PREFIX):
            store.put(reference.removeprefix(SECRET_PREFIX), value)

    try:
        return await accounts.token_for(raw, app_for, save)
    except SignInError:
        raise OAuthError("sign_in_required") from None


def decide_undecided(
    session: Session, server_id: UUID, read_only: str, others: str, owner: str
) -> int:
    """Decide every tool still unreviewed or changed, in one press.

    Which stance a tool gets follows the server's own read-only claim, which
    the owner chose to go by; tools already decided keep their decision.
    """
    server = session.get(McpServer, server_id)
    if server is None:
        raise LookupError("mcp_server_not_found")
    review = dict(server.review_json or {})
    decided = 0
    for tool in server.offering_json or []:
        if mcp_review.stance_of(review, tool) not in (mcp_review.UNREVIEWED, mcp_review.CHANGED):
            continue
        claims = tool.get("claims") or {}
        stance = read_only if claims.get("readOnlyHint") else others
        review = mcp_review.decide(review, tool, stance, owner)
        decided += 1
    server.review_json = review
    session.flush()
    return decided

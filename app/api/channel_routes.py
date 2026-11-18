"""Chat channel routes: configure, install, choose destinations, deliveries.

The owner routes sit behind the owner dependency. The OAuth callback is public
because Slack's browser redirect does not carry the owner's SameSite=Strict
cookie; it is bound to the owner by a single-use, short-lived `state` that
only an owner-authenticated request could have created.
"""

import secrets
import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.api.schemas import MemberAdd
from app.api.sessions import token_hash
from app.channels.base import ChannelError, Installation, OutboundMessage
from app.channels.inbound import PAIRING_SECONDS, Pairing
from app.channels.manifest import create_app_url, slack_manifest
from app.channels.members import add_member, members_of, owner_of, remove_member, set_owner
from app.channels.notifications import EVENTS, reconcile_unknown, resolve_by_owner
from app.channels.registry import adapter_for, available_kinds
from app.config import Settings
from app.credentials import SECRET_PREFIX, open_secret_store, resolve_credential
from app.db.models import (
    AgentProfile,
    ChannelApp,
    ChannelDelivery,
    ChannelDestination,
    ChannelInstallation,
)
from app.secret_store import SecretStoreError

STATE_TTL = 600.0


class OAuthStates:
    """Single-use install states, held in memory (a restart invalidates them)."""

    def __init__(self) -> None:
        self.states: dict[str, float] = {}

    def issue(self) -> str:
        now = time.monotonic()
        self.states = {k: v for k, v in self.states.items() if v > now}
        state = secrets.token_urlsafe(32)
        self.states[token_hash(state)] = now + STATE_TTL
        return state

    def redeem(self, state: str) -> bool:
        expiry = self.states.pop(token_hash(state), None)
        return expiry is not None and expiry > time.monotonic()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AppConfig(Strict):
    client_id: str | None = Field(default=None, max_length=255)
    client_secret: SecretStr | None = Field(default=None, min_length=8, max_length=512)
    signing_secret: SecretStr | None = Field(default=None, min_length=8, max_length=512)
    app_token: SecretStr | None = Field(default=None, min_length=8, max_length=512)
    redirect_uri: str | None = Field(default=None, max_length=1000)


class DeliveryResolution(Strict):
    """Whether a message whose outcome was unknown actually arrived."""

    arrived: bool


class AgentBotConnect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_token: SecretStr | None = Field(default=None, min_length=8, max_length=512)
    app_token: SecretStr | None = Field(default=None, min_length=8, max_length=512)


class TokenInstall(Strict):
    bot_token: SecretStr = Field(min_length=10, max_length=512)


class DestinationCreate(Strict):
    external_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    events: list[str] = Field(min_length=1, max_length=8)


class TestMessage(Strict):
    confirm: bool = False


def _store_secret(settings: Settings, name: str, value: SecretStr) -> str:
    try:
        open_secret_store(settings).put(name, value.get_secret_value())
    except SecretStoreError as error:
        raise HTTPException(status_code=503, detail=error.code) from None
    return f"{SECRET_PREFIX}{name}"


def _found(value: Any, what: str) -> Any:
    if value is None:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return value


def office_app(session: Session, kind: str, create: bool = False) -> ChannelApp | None:
    """The office's own app for a platform (the one not tied to an agent)."""
    row = session.scalar(
        select(ChannelApp).where(ChannelApp.kind == kind, ChannelApp.agent_profile_id.is_(None))
    )
    if row is None and create:
        row = ChannelApp(kind=kind)
        session.add(row)
        session.flush()
    return row


def installation_view(session: Session, row: ChannelInstallation) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "workspace_id": row.workspace_id,
        "workspace_name": row.workspace_name,
        "bot_user_id": row.bot_user_id,
        "app_id": str(row.app_id),
        "owner_external_id": owner_of(session, row.kind, row.workspace_id),
        "scopes": row.scopes_json or [],
        "status": row.status,
    }


def _save_installation(
    session_factory: sessionmaker[Session],
    settings: Settings,
    kind: str,
    found: Installation,
    app_id: UUID | None = None,
) -> dict[str, Any]:
    """Store a verified installation for an app (the office's app by default)."""
    with session_factory() as session, session.begin():
        if app_id is None:
            app = office_app(session, kind, create=True)
            assert app is not None  # noqa: S101 - created above
            app_id = app.id
        row = session.scalar(
            select(ChannelInstallation).where(
                ChannelInstallation.app_id == app_id,
                ChannelInstallation.workspace_id == found.workspace_id,
            )
        )
        if row is None:
            row = ChannelInstallation(
                app_id=app_id,
                kind=kind,
                workspace_id=found.workspace_id,
                workspace_name=found.workspace_name,
                bot_user_id=found.bot_user_id,
                token_ref="",
                status="ACTIVE",
            )
            session.add(row)
            session.flush()
        row.token_ref = _store_secret(settings, f"channel-{row.id}", SecretStr(found.bot_token))
        row.workspace_name = found.workspace_name
        row.bot_user_id = found.bot_user_id
        row.scopes_json = [s for s in found.scopes if s]
        if found.owner_external_id and owner_of(session, kind, found.workspace_id) is None:
            # The OAuth installer is verified by Slack; it never replaces a paired owner.
            set_owner(session, kind, found.workspace_id, found.owner_external_id)
        row.status = "ACTIVE"
        session.flush()
        return installation_view(session, row)


def build_channel_router(
    session_factory: sessionmaker[Session],
    settings: Settings,
    owner: Any,
    states: OAuthStates,
    pairing: Pairing | None = None,
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(owner)])
    pairing = pairing or Pairing()

    @router.get("/channels/installations/{installation_id}/members")
    def list_members(installation_id: UUID) -> list[dict[str, str]]:
        """Who may talk to this workspace's bots."""
        with session_factory() as session:
            row = _found(session.get(ChannelInstallation, installation_id), "Installation")
            return members_of(session, row.kind, row.workspace_id)

    @router.post("/channels/installations/{installation_id}/members", status_code=201)
    def add_workspace_member(installation_id: UUID, body: MemberAdd) -> dict[str, str]:
        """Let one more person use the office's bots.

        A member may ask and may give work; approvals stay with the owner,
        because a chat account should not authorise spending or an external
        write. Membership changes here and nowhere else.
        """
        with session_factory() as session, session.begin():
            row = _found(session.get(ChannelInstallation, installation_id), "Installation")
            if not add_member(session, row.kind, row.workspace_id, body.user_id):
                raise HTTPException(status_code=409, detail="already_a_member")
        return {"user_id": body.user_id, "role": "member"}

    @router.delete("/channels/installations/{installation_id}/members/{user_id}", status_code=204)
    def remove_workspace_member(installation_id: UUID, user_id: str) -> None:
        """Take someone's access away. The owner cannot be removed this way."""
        with session_factory() as session, session.begin():
            row = _found(session.get(ChannelInstallation, installation_id), "Installation")
            if not remove_member(session, row.kind, row.workspace_id, user_id):
                raise HTTPException(status_code=404, detail="not_a_member")

    @router.post("/channels/installations/{installation_id}/check")
    async def check_installation(installation_id: UUID) -> dict[str, Any]:
        """Read the bot's permissions again, after the owner updated the Slack app."""
        with session_factory() as session:
            row = _found(session.get(ChannelInstallation, installation_id), "Installation")
            kind, ref = row.kind, row.token_ref
        try:
            found = await adapter_for(kind).verify_token(resolve_credential(ref, settings))
        except SecretStoreError:
            raise HTTPException(status_code=409, detail="credential_unavailable") from None
        except ChannelError as error:
            raise HTTPException(status_code=409, detail=error.code) from None
        with session_factory() as session, session.begin():
            row = _found(session.get(ChannelInstallation, installation_id), "Installation")
            row.scopes_json = [scope for scope in found.scopes if scope]
            return installation_view(session, row)

    @router.post("/channels/installations/{installation_id}/pairing")
    def start_pairing(installation_id: UUID) -> dict[str, Any]:
        """Show a one-time code; the owner DMs it to the bot to prove who they are."""
        with session_factory() as session:
            row = _found(session.get(ChannelInstallation, installation_id), "Installation")
            app = session.get(ChannelApp, row.app_id)
            if app is None or not app.app_token_ref:
                # Without it the office cannot receive the DM, so the code would go unanswered.
                raise HTTPException(status_code=409, detail="app_token_missing")
            # Re-pairing replaces the previous owner for every bot in the workspace.
            set_owner(session, row.kind, row.workspace_id, None)
            session.commit()
            code = pairing.issue(row.workspace_id)
        return {"code": code, "expires_in_seconds": PAIRING_SECONDS}

    def _agent_app(session: Session, agent_id: UUID, kind: str) -> ChannelApp | None:
        return session.scalar(
            select(ChannelApp).where(
                ChannelApp.kind == kind, ChannelApp.agent_profile_id == agent_id
            )
        )

    def _agent_bot_view(session: Session, agent: AgentProfile, kind: str) -> dict[str, Any]:
        app = _agent_app(session, agent.id, kind)
        installation = (
            session.scalar(
                select(ChannelInstallation).where(
                    ChannelInstallation.app_id == app.id, ChannelInstallation.status == "ACTIVE"
                )
            )
            if app
            else None
        )
        manifest = slack_manifest(agent.name, agent.role)
        return {
            "agent_id": str(agent.id),
            "kind": kind,
            "manifest": manifest,
            "create_app_url": create_app_url(manifest),
            "app_token_configured": bool(app and app.app_token_ref),
            "installation": installation_view(session, installation) if installation else None,
        }

    @router.get("/agents/{agent_id}/chat-bot/{kind}")
    def agent_bot(agent_id: UUID, kind: str) -> dict[str, Any]:
        """The agent's own bot: its manifest, and whether it is connected."""
        if kind != "slack":
            raise HTTPException(status_code=404, detail="unsupported_kind")
        with session_factory() as session:
            agent = _found(session.get(AgentProfile, agent_id), "Agent")
            return _agent_bot_view(session, agent, kind)

    @router.put("/agents/{agent_id}/chat-bot/{kind}")
    async def connect_agent_bot(agent_id: UUID, kind: str, body: AgentBotConnect) -> dict[str, Any]:
        """Verify the agent app's bot token, then store both tokens encrypted."""
        if kind != "slack":
            raise HTTPException(status_code=404, detail="unsupported_kind")
        with session_factory() as session:
            agent = _found(session.get(AgentProfile, agent_id), "Agent")
            if agent.status != "ACTIVE":
                raise HTTPException(status_code=409, detail="agent_not_active")
        found = None
        if body.bot_token is not None:
            try:
                found = await adapter_for(kind).verify_token(body.bot_token.get_secret_value())
            except ChannelError as error:
                raise HTTPException(status_code=422, detail=error.code) from None
        with session_factory() as session, session.begin():
            app = _agent_app(session, agent_id, kind)
            if app is None:
                app = ChannelApp(kind=kind, agent_profile_id=agent_id)
                session.add(app)
                session.flush()
            if found is not None:
                taken = session.scalar(
                    select(ChannelInstallation.id).where(
                        ChannelInstallation.bot_user_id == found.bot_user_id,
                        ChannelInstallation.app_id != app.id,
                        ChannelInstallation.status == "ACTIVE",
                    )
                )
                if taken is not None:
                    # One Slack app speaks for one agent: never share a bot.
                    raise HTTPException(status_code=409, detail="bot_already_in_use")
            if body.app_token is not None:
                app.app_token_ref = _store_secret(
                    settings, f"channel-{kind}-agent-{agent_id}-app_token", body.app_token
                )
            app_id = app.id
        if found is not None:
            _save_installation(session_factory, settings, kind, found, app_id)
        with session_factory() as session:
            agent = _found(session.get(AgentProfile, agent_id), "Agent")
            return _agent_bot_view(session, agent, kind)

    @router.delete("/agents/{agent_id}/chat-bot/{kind}", status_code=204)
    def disconnect_agent_bot(agent_id: UUID, kind: str) -> None:
        """Delete the agent bot's stored tokens; its connection stops within 30 seconds."""
        store = open_secret_store(settings)
        with session_factory() as session, session.begin():
            app = _found(_agent_app(session, agent_id, kind), "Agent bot")
            references = [app.app_token_ref or ""]
            for row in session.scalars(
                select(ChannelInstallation).where(ChannelInstallation.app_id == app.id)
            ):
                references.append(row.token_ref)
                row.token_ref = ""
                row.status = "REVOKED"
                for destination in session.scalars(
                    select(ChannelDestination).where(ChannelDestination.installation_id == row.id)
                ):
                    destination.enabled = False
            app.app_token_ref = None
            for reference in references:
                if reference.startswith(SECRET_PREFIX):
                    try:
                        store.delete(reference.removeprefix(SECRET_PREFIX))
                    except SecretStoreError as error:
                        raise HTTPException(status_code=503, detail=error.code) from None

    @router.get("/channels")
    def channels() -> dict[str, Any]:
        with session_factory() as session:
            apps = {
                row.kind: {
                    "client_id": row.client_id,
                    "client_secret_configured": bool(row.client_secret_ref),
                    "signing_secret_configured": bool(row.signing_secret_ref),
                    "app_token_configured": bool(row.app_token_ref),
                    "redirect_uri": row.redirect_uri,
                }
                for row in session.scalars(
                    select(ChannelApp).where(ChannelApp.agent_profile_id.is_(None))
                )
            }
            installations = [
                installation_view(session, row)
                for row in session.scalars(
                    select(ChannelInstallation)
                    .join(ChannelApp, ChannelApp.id == ChannelInstallation.app_id)
                    .where(
                        ChannelInstallation.status == "ACTIVE",
                        ChannelApp.agent_profile_id.is_(None),
                    )
                )
            ]
            destinations = [
                {
                    "id": str(row.id),
                    "installation_id": str(row.installation_id),
                    "external_id": row.external_id,
                    "name": row.name,
                    "events": row.events_json or [],
                    "enabled": row.enabled,
                }
                for row in session.scalars(select(ChannelDestination))
            ]
        return {
            "kinds": available_kinds(),
            "apps": apps,
            "installations": installations,
            "destinations": destinations,
            "events": sorted(EVENTS),
        }

    @router.put("/channels/{kind}/app")
    def configure_app(kind: str, body: AppConfig) -> dict[str, Any]:
        """App-level configuration. Secrets go to the encrypted store only."""
        try:
            adapter_for(kind)
        except ChannelError as error:
            raise HTTPException(status_code=404, detail=error.code) from None
        with session_factory() as session, session.begin():
            row = office_app(session, kind, create=True)
            assert row is not None  # noqa: S101 - created above
            if body.client_id is not None:
                row.client_id = body.client_id
            if body.redirect_uri is not None:
                row.redirect_uri = body.redirect_uri
            for field in ("client_secret", "signing_secret", "app_token"):
                value = getattr(body, field)
                if value is not None:
                    setattr(
                        row,
                        f"{field}_ref",
                        _store_secret(settings, f"channel-{kind}-{field}", value),
                    )
        return {"kind": kind, "configured": True}

    @router.get("/channels/{kind}/install")
    def install_url(kind: str) -> dict[str, str]:
        """The OAuth v2 authorize URL, with a fresh single-use state."""
        with session_factory() as session:
            row = office_app(session, kind)
        if row is None or not row.client_id or not row.client_secret_ref:
            raise HTTPException(status_code=409, detail="app_not_configured")
        try:
            url = adapter_for(kind).authorize_url(  # type: ignore[attr-defined]
                row.client_id, row.redirect_uri or "", states.issue()
            )
        except ChannelError as error:
            raise HTTPException(status_code=409, detail=error.code) from None
        return {"url": url}

    @router.post("/channels/{kind}/installations", status_code=201)
    async def install_with_token(kind: str, body: TokenInstall) -> dict[str, Any]:
        """Direct install for the owner's own workspace: verify, then store encrypted."""
        try:
            found = await adapter_for(kind).verify_token(body.bot_token.get_secret_value())
        except ChannelError as error:
            raise HTTPException(status_code=422, detail=error.code) from None
        return _save_installation(session_factory, settings, kind, found)

    @router.get("/channels/installations/{installation_id}/available")
    async def available(installation_id: UUID) -> list[dict[str, Any]]:
        with session_factory() as session:
            row = _found(session.get(ChannelInstallation, installation_id), "Installation")
            kind, reference = row.kind, row.token_ref
        try:
            token = resolve_credential(reference, settings)
            found = await adapter_for(kind).list_destinations(token)
        except (ChannelError, SecretStoreError) as error:
            raise HTTPException(status_code=502, detail=error.code) from None
        return [
            {"external_id": d.external_id, "name": d.name, "can_post": d.can_post} for d in found
        ]

    @router.post("/channels/installations/{installation_id}/destinations", status_code=201)
    def add_destination(installation_id: UUID, body: DestinationCreate) -> dict[str, Any]:
        unknown = set(body.events) - EVENTS
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown_event:{sorted(unknown)[0]}")
        try:
            with session_factory() as session, session.begin():
                _found(session.get(ChannelInstallation, installation_id), "Installation")
                existing = session.scalar(
                    select(ChannelDestination).where(
                        ChannelDestination.installation_id == installation_id,
                        ChannelDestination.external_id == body.external_id,
                    )
                )
                if existing is not None and not existing.enabled:
                    # A removed destination, or a chat thread's channel: subscribe it now.
                    existing.name = body.name
                    existing.events_json = body.events
                    existing.enabled = True
                    return {
                        "id": str(existing.id),
                        "name": existing.name,
                        "events": existing.events_json,
                    }
                row = ChannelDestination(
                    installation_id=installation_id,
                    external_id=body.external_id,
                    name=body.name,
                    events_json=body.events,
                    enabled=True,
                )
                session.add(row)
                session.flush()
                return {"id": str(row.id), "name": row.name, "events": row.events_json}
        except IntegrityError:
            raise HTTPException(status_code=409, detail="destination_exists") from None

    @router.delete("/channels/destinations/{destination_id}", status_code=204)
    def remove_destination(destination_id: UUID) -> None:
        with session_factory() as session, session.begin():
            row = _found(session.get(ChannelDestination, destination_id), "Destination")
            row.enabled = False  # Kept: deliveries reference it.

    @router.post("/channels/destinations/{destination_id}/test")
    async def test_destination(destination_id: UUID, body: TestMessage) -> dict[str, str]:
        """Post one test message. External and visible, so it needs confirmation."""
        if not body.confirm:
            raise HTTPException(status_code=409, detail="confirmation_required")
        with session_factory() as session:
            destination = _found(session.get(ChannelDestination, destination_id), "Destination")
            installation = _found(
                session.get(ChannelInstallation, destination.installation_id), "Installation"
            )
            channel, kind, reference = (
                destination.external_id,
                installation.kind,
                installation.token_ref,
            )
        try:
            result = await adapter_for(kind).send(
                resolve_credential(reference, settings),
                channel,
                OutboundMessage(text="Quarterdeck is connected. Notifications will appear here."),
            )
        except (ChannelError, SecretStoreError) as error:
            raise HTTPException(status_code=502, detail=error.code) from None
        return {"external_ref": result.external_ref}

    @router.delete("/channels/installations/{installation_id}", status_code=204)
    def uninstall(installation_id: UUID) -> None:
        """Revoke locally: delete the stored token and stop all delivery."""
        with session_factory() as session, session.begin():
            row = _found(session.get(ChannelInstallation, installation_id), "Installation")
            if row.token_ref.startswith(SECRET_PREFIX):
                try:
                    open_secret_store(settings).delete(row.token_ref.removeprefix(SECRET_PREFIX))
                except SecretStoreError as error:
                    raise HTTPException(status_code=503, detail=error.code) from None
            row.token_ref = ""
            row.status = "REVOKED"
            for destination in session.scalars(
                select(ChannelDestination).where(ChannelDestination.installation_id == row.id)
            ):
                destination.enabled = False

    @router.post("/channels/deliveries/reconcile")
    async def reconcile_deliveries() -> dict[str, int]:
        """Ask each platform what became of the deliveries it never confirmed."""
        return await reconcile_unknown(session_factory, settings)

    @router.post("/channels/deliveries/{delivery_id}/resolve")
    def resolve_delivery(delivery_id: UUID, body: DeliveryResolution) -> dict[str, Any]:
        """The owner settles an unknown delivery: it arrived, or it did not."""
        try:
            return resolve_by_owner(session_factory, delivery_id, body.arrived)
        except ChannelError as error:
            raise HTTPException(status_code=409, detail=error.code) from None

    @router.get("/channels/deliveries")
    def deliveries(run_id: UUID | None = None) -> list[dict[str, Any]]:
        with session_factory() as session:
            statement = select(ChannelDelivery).order_by(ChannelDelivery.created_at.desc())
            if run_id is not None:
                statement = statement.where(ChannelDelivery.run_id == run_id)
            return [
                {
                    "id": str(row.id),
                    "run_id": str(row.run_id) if row.run_id else None,
                    "event": row.event_kind,
                    "status": row.status,
                    "attempts": row.attempts,
                    "error": row.error,
                    "external_ref": row.external_ref,
                    "text": row.text,
                    "reconciled_at": row.reconciled_at,
                    "reconciled_by": row.reconciled_by,
                    "app_id": str(row.app_id) if row.app_id else None,
                    "agent_profile_id": str(row.agent_profile_id) if row.agent_profile_id else None,
                }
                for row in session.scalars(statement.limit(200))
            ]

    return router


def build_channel_callback_router(
    session_factory: sessionmaker[Session], settings: Settings, states: OAuthStates
) -> APIRouter:
    router = APIRouter()

    @router.get("/channels/{kind}/oauth/callback", response_class=HTMLResponse)
    async def oauth_callback(kind: str, code: str = "", state: str = "") -> HTMLResponse:
        if not code or not states.redeem(state):
            raise HTTPException(status_code=400, detail="invalid_state")
        with session_factory() as session:
            app = office_app(session, kind)
        if app is None or not app.client_id or not app.client_secret_ref:
            raise HTTPException(status_code=409, detail="app_not_configured")
        try:
            secret = resolve_credential(app.client_secret_ref, settings)
            found = await adapter_for(kind).exchange_code(  # type: ignore[attr-defined]
                code, app.client_id, secret, app.redirect_uri or ""
            )
        except (ChannelError, SecretStoreError) as error:
            raise HTTPException(status_code=502, detail=error.code) from None
        view = _save_installation(session_factory, settings, kind, found)
        name = view["workspace_name"].replace("<", "").replace(">", "")
        return HTMLResponse(f"<p>Quarterdeck is installed in {name}. You can close this tab.</p>")

    return router

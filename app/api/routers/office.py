"""Routes for the office itself: settings, policies, widgets, setup and access."""

from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import access, coding_agents, desk, owner_login, sidebar, widgets
from app.api.auth import OwnerIdentity
from app.api.routers.deps import RouterDeps
from app.api.routes import found
from app.api.schemas import (
    AccessAddress,
    AccessPair,
    CodingAgentChoice,
    OfficeSettingsUpdate,
    OwnerLoginClear,
    OwnerLoginSet,
    PolicyCreate,
    PolicyDecision,
    PolicyUpdate,
    RemoteConnect,
    WidgetChange,
    WidgetOrder,
    WidgetRemove,
)
from app.api.sessions import CODE_TTL
from app.browser.client import ExchangeBrowser
from app.coder import jobs as coder_jobs
from app.coding_names import AGENTS
from app.db.models import AccessPoint, Policy, PolicyRecord, PolicyState, Project
from app.office_board import office_board
from app.office_overview import office_overview
from app.office_pulse import pulse as office_pulse
from app.office_reach import reach as office_reach
from app.office_settings import effective, save, spent_this_month
from app.office_setup import mark_complete, setup_state
from app.policies.engine import REGISTRY
from app.policies.store import registry_view
from app.progress_cards import office_progress
from app.remote_access import remote_view
from app.runtimes.installer import heartbeat_age
from app.sample_office import install as sample_install
from app.sample_office import remove as sample_remove
from app.status import office_status


# ---- The coding CLI on the owner's Mac that codes ----
def _coding_view(d: RouterDeps, session: Session) -> dict[str, Any]:
    """Which CLI codes, from those Quarterdeck Coder found on the Mac (Stage 36)."""
    chosen = coding_agents.chosen_agent(session, d.settings)
    helper = coder_jobs.helper_view(session, datetime.now(UTC))
    found = {cli["id"]: cli for cli in helper["clis"]}
    age = heartbeat_age(d.settings.engineering_root)
    return {
        "agent": chosen,
        "code_execution": bool(d.settings.enable_code_execution),
        "packages": coding_agents.packages_allowed(session),
        "installer_running": age is not None and age < 30,
        "helper_connected": helper["connected"],
        "agents": [
            {
                "id": key,
                "label": label,
                "installed": key in found,
                "ready": bool(found.get(key, {}).get("signed_in")),
                "runs": key in coder_jobs.RUNS,
            }
            for key, label in AGENTS.items()
        ],
    }


# ---- The owner's wall of widgets on the floor ----
def _widget_rejected(d: RouterDeps, rejected: widgets.WidgetRejected) -> HTTPException:
    status = 404 if rejected.code == "widget_unknown" else 422
    return HTTPException(status_code=status, detail=rejected.code)


# ---- Signing in with a name and password ----
def _login_view(d: RouterDeps, session: Session) -> dict[str, Any]:
    stored = owner_login.held(session)
    return {
        "set": bool(stored.get("hash")),
        "name": stored.get("name") or "",
        "set_at": stored.get("set_at"),
    }


def _remote(d: RouterDeps, request: Request) -> Any:
    return getattr(request.app.state, "remote", None)


def _remote_seen(d: RouterDeps, request: Request) -> dict[str, Any] | None:
    remote = _remote(d, request)
    return remote.seen() if remote is not None else None


def _remote_answer(
    d: RouterDeps, request: Request, changes: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Record what the container said on the remote point; its view."""
    with d.session_factory() as session, session.begin():
        found = access.point(session, "remote")
        for name, value in (changes or {}).items():
            setattr(found, name, value)
        view = access.view(found)
        view["remote"] = remote_view(found, _remote_seen(d, request))
    _refresh_reach(d)
    return view


def _remote_needed(d: RouterDeps, request: Request) -> Any:
    remote = _remote(d, request)
    if remote is None or remote.seen() is None:
        raise HTTPException(status_code=409, detail="remote_not_running")
    return remote


def _refresh_reach(d: RouterDeps) -> None:
    if d.reachable is not None:
        d.reachable.refresh()


def add_office_routes(router: APIRouter, d: RouterDeps) -> None:
    Owner = Annotated[OwnerIdentity, Depends(d.require_owner)]

    @router.get("/office/overview")
    def overview() -> dict[str, Any]:
        """Home and Tasks: counts, recent tasks, activity, agents, usage this month."""
        with d.session_factory() as session:
            summary = office_overview(session)
        with d.session_factory() as session:
            current = effective(session, d.settings)
        summary["office_name"] = current["office_name"]
        summary["owner_name"] = current["owner_name"]
        return summary

    @router.get("/office/board")
    def board(weeks: int = 8) -> dict[str, Any]:
        """The work as columns, with effort per task and how the weeks compare."""
        with d.session_factory() as session:
            return office_board(session, weeks=max(1, min(weeks, 26)))

    @router.get("/setup")
    async def setup_view() -> dict[str, Any]:
        """What is true about this office, step by step, for a new owner."""
        with d.session_factory() as session:
            return setup_state(session, d.settings)

    @router.post("/setup/complete")
    def finish_setup() -> dict[str, Any]:
        """Record that the owner has finished setting up. Nothing is enforced by it."""
        with d.session_factory() as session, session.begin():
            mark_complete(session)
        return {"completed": True}

    @router.get("/policies")
    def policies_view(project_id: UUID | None = None) -> dict[str, Any]:
        """Every rule the owner may turn on, and the ones already in force."""
        with d.session_factory() as session:
            rows = session.scalars(select(Policy).order_by(Policy.created_at)).all()
            names = {row.id: row.name for row in session.scalars(select(Project))}
        return {
            "available": registry_view(),
            "policies": [
                {
                    "id": str(row.id),
                    "kind": row.kind,
                    "scope": row.scope,
                    "project_id": str(row.project_id) if row.project_id else None,
                    "project": names.get(row.project_id) if row.project_id else None,
                    "params": row.params_json or {},
                    "enabled": row.enabled,
                }
                for row in rows
                if project_id is None or row.project_id == project_id or row.scope == "office"
            ],
        }

    @router.post("/policies", status_code=201)
    def add_policy(body: PolicyCreate) -> dict[str, Any]:
        """Turn a rule on, for the office or for one project."""
        if body.kind not in REGISTRY:
            raise HTTPException(status_code=422, detail="unknown_policy")
        if body.scope == "project" and body.project_id is None:
            raise HTTPException(status_code=422, detail="project_required")
        with d.session_factory() as session, session.begin():
            if body.project_id is not None:
                found(session.get(Project, body.project_id), "Project")
            row = Policy(
                kind=body.kind,
                scope=body.scope,
                project_id=body.project_id if body.scope == "project" else None,
                params_json=body.params,
                enabled=True,
            )
            session.add(row)
            session.flush()
            policy_id = row.id
        return {"id": str(policy_id), "kind": body.kind, "scope": body.scope}

    @router.put("/policies/{policy_id}")
    def change_policy(policy_id: UUID, body: PolicyUpdate) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            row = found(session.get(Policy, policy_id), "Policy")
            changes = body.model_dump(exclude_unset=True)
            if "params" in changes and changes["params"] is not None:
                row.params_json = changes["params"]
            if "enabled" in changes and changes["enabled"] is not None:
                row.enabled = bool(changes["enabled"])
            session.flush()
            return {"id": str(row.id), "enabled": row.enabled, "params": row.params_json}

    @router.delete("/policies/{policy_id}", status_code=204)
    def remove_policy(policy_id: UUID) -> None:
        with d.session_factory() as session, session.begin():
            row = found(session.get(Policy, policy_id), "Policy")
            session.delete(row)

    @router.post("/policies/records/{record_id}/decide")
    def decide_policy_ask(record_id: int, body: PolicyDecision) -> dict[str, Any]:
        """Answer a rule that paused a task: allow it, or refuse it.

        Allowing records permission for that task only, and lets it continue.
        Refusing leaves the task paused with the reason, for the owner to close
        it or change the rule.
        """
        with d.session_factory() as session, session.begin():
            row = found(session.get(PolicyRecord, record_id), "Policy record")
            if row.verdict != "ask":
                raise HTTPException(status_code=409, detail="not_a_question")
            if row.decided_at is not None:
                raise HTTPException(status_code=409, detail="already_decided")
            row.decided_at = datetime.now(UTC)
            row.decision = "allow" if body.allow else "refuse"
            run_id = row.run_id
            policy = row.policy
        if body.allow and run_id is not None and policy:
            with d.session_factory() as session, session.begin():
                state = session.get(PolicyState, run_id)
                counters = dict(state.counters_json or {}) if state else {}
                counters[f"allow:{policy}"] = 1
                if state is None:
                    session.add(PolicyState(run_id=run_id, counters_json=counters))
                else:
                    state.counters_json = counters
            resumed = d.worker.resume_paused(run_id)
            return {"decision": "allow", "resumed": bool(resumed)}
        return {"decision": "refuse", "resumed": False}

    @router.get("/policies/records")
    def policy_records(run_id: UUID | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """What the policies decided, most recent first."""
        with d.session_factory() as session:
            statement = select(PolicyRecord).order_by(PolicyRecord.id.desc())
            if run_id is not None:
                statement = statement.where(PolicyRecord.run_id == run_id)
            return [
                {
                    "id": row.id,
                    "run_id": str(row.run_id) if row.run_id else None,
                    "hook": row.hook,
                    "target": row.target,
                    "verdict": row.verdict,
                    "policy": row.policy,
                    "reason": row.reason,
                    "detail": row.detail_json or {},
                    "decided_at": row.decided_at,
                    "decision": row.decision,
                    "created_at": row.created_at,
                }
                for row in session.scalars(statement.limit(max(1, min(limit, 200))))
            ]

    @router.get("/settings")
    def office_settings_view() -> dict[str, Any]:
        """What the owner can change, and what configuration would give."""
        with d.session_factory() as session:
            current = effective(session, d.settings)
            spent = spent_this_month(session)
        return {
            "settings": current,
            "configured": {
                "run_max_tokens": d.settings.run_max_tokens,
                "run_max_cost_usd": d.settings.run_max_cost_usd,
                "run_max_minutes": d.settings.run_max_minutes,
                "chat_daily_max_tokens": d.settings.chat_daily_max_tokens,
                "runs_at_once": d.settings.max_concurrent_runs,
            },
            # Tasks executing now and waiting for a place (MW).
            "worker": {"running": d.worker.running_count, "waiting": d.worker.waiting_count},
            "spent_this_month_usd": round(spent, 4),
            "office_name": d.settings.office_name,
            "owner_name": d.settings.owner_name,
            # Whether the office's own browser service is there to be used.
            "browser_running": d.settings.browser_exchange is not None
            and ExchangeBrowser(d.settings.browser_exchange).running(),
        }

    @router.put("/settings")
    def update_office_settings(body: OfficeSettingsUpdate) -> dict[str, Any]:
        """Change the office's settings. They apply to new work only."""
        with d.session_factory() as session, session.begin():
            save(session, body.model_dump(exclude_unset=True))
        return office_settings_view()

    @router.get("/status")
    async def status_view() -> dict[str, Any]:
        """Office health for the owner: services, storage, backups, connections."""
        with d.session_factory() as session:
            return office_status(session, d.settings, d.worker.waiting_count)

    @router.get("/settings/coding-agent")
    def coding_agent_view() -> dict[str, Any]:
        with d.session_factory() as session:
            return _coding_view(d, session)

    @router.put("/settings/coding-agent")
    def choose_coding_agent(body: CodingAgentChoice) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            if body.agent is not None:
                coding_agents.set_agent(session, body.agent)
            if body.packages is not None:
                coding_agents.allow_packages(session, body.packages)
            return _coding_view(d, session)

    @router.get("/widgets")
    def widget_wall() -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            return {"widgets": widgets.wall(session)}

    @router.post("/widgets/{kind}/place")
    def place_widget(kind: str) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            try:
                return widgets.place(session, kind)
            except widgets.WidgetRejected as rejected:
                raise _widget_rejected(d, rejected) from rejected

    @router.post("/widgets/{kind}/remove")
    def remove_widget(kind: str, body: WidgetRemove) -> dict[str, Any]:
        """Off the wall. What it knew is kept unless the owner asked to clean it."""
        try:
            if body.clean:
                widgets.clean(widgets.Services(d.session_factory, d.calendar_service), kind)
            with d.session_factory() as session, session.begin():
                return widgets.remove(session, kind)
        except widgets.WidgetRejected as rejected:
            raise _widget_rejected(d, rejected) from rejected

    @router.patch("/widgets/{kind}")
    def change_widget(kind: str, body: WidgetChange) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            try:
                return widgets.change(session, kind, body.size, body.agents_read)
            except widgets.WidgetRejected as rejected:
                raise _widget_rejected(d, rejected) from rejected

    @router.put("/widgets/order")
    def order_widgets(body: WidgetOrder) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            return {"widgets": widgets.arrange(session, body.kinds)}

    @router.get("/settings/login")
    def login_settings() -> dict[str, Any]:
        with d.session_factory() as session:
            return _login_view(d, session)

    @router.put("/settings/login")
    def set_login(body: OwnerLoginSet) -> dict[str, Any]:
        current = body.current_password.get_secret_value() if body.current_password else ""
        with d.session_factory() as session, session.begin():
            try:
                owner_login.set_login(session, body.name, body.password.get_secret_value(), current)
            except owner_login.LoginRejected as rejected:
                raise HTTPException(status_code=422, detail=rejected.code) from rejected
            return _login_view(d, session)

    @router.post("/settings/login/remove")
    def remove_login(body: OwnerLoginClear) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            try:
                owner_login.clear_login(session, body.current_password.get_secret_value())
            except owner_login.LoginRejected as rejected:
                raise HTTPException(status_code=422, detail=rejected.code) from rejected
            return _login_view(d, session)

    @router.get("/access")
    def access_points(request: Request) -> dict[str, Any]:
        """The outside addresses, what was verified about each, and whether a
        phone has been confirmed through it."""
        with d.session_factory() as session, session.begin():
            points = [access.view(access.point(session, kind)) for kind in access.KINDS]
        points[0]["setup"] = access.home_setup(d.settings)
        with d.session_factory() as session:
            points[1]["remote"] = remote_view(
                access.point(session, "remote"), _remote_seen(d, request)
            )
        return {"points": points}

    @router.post("/access/remote/connect")
    async def connect_remote(body: RemoteConnect, request: Request) -> dict[str, Any]:
        """Join the owner's tailnet as quarterdeck: with a one-time key, or by a
        login link for the owner to approve. The key is not kept."""
        remote = _remote_needed(d, request)
        key = body.auth_key.get_secret_value() if body.auth_key else ""
        said = await remote.ask("connect", **({"auth_key": key} if key else {}))
        check = {k: v for k, v in said.items() if k in ("ok", "backend")}
        return _remote_answer(d, request, {"last_check_at": datetime.now(UTC), "check_json": check})

    @router.post("/access/remote/enable")
    async def enable_remote(request: Request) -> dict[str, Any]:
        """Route https://quarterdeck.<tailnet>.ts.net to the office with Serve, and
        turn it on only once Serve reports that route."""
        remote = _remote_needed(d, request)
        said = await remote.ask("route_on")
        seen = remote.seen() or {}
        name = str(said.get("dns_name") or seen.get("dns_name") or "")
        routed = bool(said.get("routed")) and bool(name)
        check = {
            "routed": routed,
            "needs_https": bool(said.get("needs_https")),
            "url": str(said.get("url") or ""),
            "at": datetime.now(UTC).isoformat(),
        }
        changes: dict[str, Any] = {
            "enabled": routed,
            "last_check_at": datetime.now(UTC),
            "check_json": check,
        }
        if routed:
            changes.update(address=f"https://{name}", verified_at=datetime.now(UTC))
        else:
            changes.update(verified_at=None, confirmed_at=None)
        return _remote_answer(d, request, changes)

    @router.post("/access/remote/disable")
    async def disable_remote(request: Request) -> dict[str, Any]:
        """Stop the route and refuse its address; this computer keeps working.
        A route that did not stop is reported, never hidden."""
        changes: dict[str, Any] = {"enabled": False, "last_check_at": datetime.now(UTC)}
        remote = _remote(d, request)
        still = False
        if remote is not None and remote.seen() is not None:
            said = await remote.ask("route_off")
            still = bool(said.get("routed"))
        changes["check_json"] = {"still_routed": still, "at": datetime.now(UTC).isoformat()}
        return _remote_answer(d, request, changes)

    @router.post("/access/remote/forget")
    async def forget_remote(request: Request) -> dict[str, Any]:
        """Stop the route and sign this device out of the tailnet."""
        remote = _remote_needed(d, request)
        said = await remote.ask("forget")
        return _remote_answer(
            d,
            request,
            {
                "enabled": False,
                "address": None,
                "verified_at": None,
                "confirmed_at": None,
                "confirmed_device": None,
                "last_check_at": datetime.now(UTC),
                "check_json": {"forgotten": bool(said.get("ok"))},
            },
        )

    @router.post("/access/home/enable")
    async def enable_home(body: AccessAddress) -> dict[str, Any]:
        """Turn the home network on at an address, only once the office has
        reached itself there."""
        setup = access.home_setup(d.settings)
        if setup["required"]:
            raise HTTPException(status_code=409, detail="setup_required")
        wrong = access.valid_address(body.address)
        if wrong:
            raise HTTPException(status_code=422, detail=wrong)
        address = access.origin_of(body.address)
        named = urlsplit(address)
        # A .local name does not resolve inside Docker: reach the Mac by one of
        # its IP addresses, under the name the phone will use.
        connect, host = address, None
        if (named.hostname or "").endswith(".local"):
            ips = [a for a in setup["suggested"] if not urlsplit(a).hostname.endswith(".local")]
            if ips:
                connect, host = access.origin_of(ips[0]), named.netloc
        checked = await access.probe(connect, host)
        with d.session_factory() as session, session.begin():
            found = access.point(session, "home")
            found.address = address
            found.last_check_at = datetime.now(UTC)
            found.check_json = checked
            found.verified_at = datetime.now(UTC) if checked.get("answered") else None
            found.enabled = bool(checked.get("answered"))
            if not found.enabled:
                found.confirmed_at = None
            view = access.view(found)
        _refresh_reach(d)
        return view

    @router.post("/access/home/disable")
    def disable_home() -> dict[str, Any]:
        """Refuse the home network address again, at once."""
        with d.session_factory() as session, session.begin():
            found = access.point(session, "home")
            found.enabled = False
            view = access.view(found)
        _refresh_reach(d)
        return view

    @router.post("/access/pair")
    def pair_phone(body: AccessPair) -> dict[str, Any]:
        """A pairing link for a phone: the verified address and a one-time code
        (single use, two minutes), as a QR code the office draws itself."""
        with d.session_factory() as session:
            found = session.get(AccessPoint, body.kind)
            if found is None or not found.enabled or found.verified_at is None or not found.address:
                raise HTTPException(status_code=409, detail="not_ready")
            address = access.origin_of(found.address)
        url = f"{address}/#code={d.login_codes.mint()}"
        return {"url": url, "expires_in": int(CODE_TTL), "qr": access.qr_svg(url)}

    @router.get("/office/desk")
    def office_desk() -> dict[str, Any]:
        """My desk: what needs the owner, discussions to continue, what was
        delivered, and what is parked: ideas and discussions coming back."""
        with d.session_factory() as session:
            return desk.desk(session, d.repository.store)

    @router.get("/office/sidebar")
    def office_sidebar() -> dict[str, Any]:
        """The sidebar: business spaces, their threads, each thread's state."""
        with d.session_factory() as session:
            return sidebar.sidebar(session)

    @router.get("/office/reach")
    async def office_reach_view(fresh: bool = False) -> dict[str, Any]:
        """What the office can reach, checked from where it reads."""
        with d.session_factory() as session:
            return await office_reach(session, d.settings, fresh)

    @router.get("/office/progress")
    def office_progress_view() -> dict[str, Any]:
        """The figures behind the project and task progress cards."""
        with d.session_factory() as session:
            return office_progress(session, d.settings)

    @router.get("/office/pulse")
    def office_pulse_view() -> dict[str, Any]:
        """The office at a glance: progress, spend, what needs the owner."""
        with d.session_factory() as session, session.begin():
            return office_pulse(session, d.settings)

    @router.post("/sample-office")
    def install_sample_office(owner: Owner) -> dict[str, Any]:
        """A sample project and one finished task, so the office can be seen first.

        Visibly simulated: nothing is called, nothing is spent, and every piece
        says so. Pressing it twice does not make two.
        """
        with d.session_factory() as session, session.begin():
            return sample_install(session, d.repository.store)

    @router.delete("/sample-office")
    def remove_sample_office(owner: Owner) -> dict[str, Any]:
        """Take the sample away once the owner has their own work."""
        with d.session_factory() as session, session.begin():
            return {"removed": sample_remove(session, d.repository.store)}

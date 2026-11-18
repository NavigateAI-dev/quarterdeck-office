"""Routes for the team: agents and skills."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import yaml
from fastapi import APIRouter, HTTPException, Response
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent_portable import export_agent, plan_import, read_yaml, to_yaml
from app.agent_profiles import ConnectionNotAssignable, check_assignable
from app.api.routers.deps import RouterDeps
from app.api.routes import found
from app.api.schemas import (
    AgentCreate,
    AgentIdentityUpdate,
    AgentImport,
    AgentVersionCreate,
    AgentView,
    ConnectionTest,
    SkillInstall,
    SkillPin,
    SkillSource,
    SkillSourceInstall,
)
from app.credentials import resolve_credential
from app.db.models import (
    AgentProfile,
    AgentProfileVersion,
    InstalledSkill,
    ModelConnection,
    SkillUpdate,
    Venture,
)
from app.gateway.client import GatewayError
from app.gateway.direct import DirectCompleter
from app.mcp import store as mcp_store
from app.secret_store import SecretStoreError
from app.skills import ecosystem as skill_ecosystem
from app.skills import updates as skill_updates
from app.skills.catalog_store import (
    CATALOG_DIR,
    SkillError,
    SkillManifest,
    expanded_permissions,
    load_catalog,
    load_manifest,
    manifest_digest,
)


def agent_view(d: RouterDeps, session: Session, profile: AgentProfile) -> dict[str, Any]:
    versions = session.scalars(
        select(AgentProfileVersion)
        .where(AgentProfileVersion.profile_id == profile.id)
        .order_by(AgentProfileVersion.version)
    )
    return {
        "id": profile.id,
        "name": profile.name,
        "role": profile.role,
        "status": profile.status,
        "enabled_version": profile.enabled_version,
        "versions": [
            {
                "version": row.version,
                "instructions": row.instructions,
                "connection_id": row.connection_id,
                "limits": row.limits_json or {},
                "skills": row.skills_json or [],
                "created_at": row.created_at,
            }
            for row in versions
        ],
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


def add_version(
    d: RouterDeps, session: Session, profile: AgentProfile, body: AgentVersionCreate
) -> int:
    """Append an immutable version. Assigning a connection is its grant."""
    if body.connection_id is not None:
        connection = found(session.get(ModelConnection, body.connection_id), "Connection")
        try:
            check_assignable(connection)
        except ConnectionNotAssignable:
            raise HTTPException(status_code=409, detail="connection_not_tested") from None
    for pin in body.skills:
        installed = session.scalar(
            select(InstalledSkill).where(
                InstalledSkill.skill_id == pin.skill_id,
                InstalledSkill.version == pin.version,
            )
        )
        if installed is None or installed.status != "ENABLED":
            raise HTTPException(
                status_code=409, detail=f"skill_not_enabled:{pin.skill_id}@{pin.version}"
            )
    latest = session.scalar(
        select(func.max(AgentProfileVersion.version)).where(
            AgentProfileVersion.profile_id == profile.id
        )
    )
    version = (latest or 0) + 1
    session.add(
        AgentProfileVersion(
            profile_id=profile.id,
            version=version,
            instructions=body.instructions,
            connection_id=body.connection_id,
            limits_json=body.limits,
            skills_json=[pin.model_dump() for pin in body.skills],
        )
    )
    session.flush()
    return version


def skill_view(d: RouterDeps, row: InstalledSkill) -> dict[str, Any]:
    manifest = row.manifest_json
    return {
        "skill_id": row.skill_id,
        "version": row.version,
        "name": manifest.get("name"),
        "description": manifest.get("description"),
        "status": row.status,
        "permissions": manifest.get("permissions", []),
        "roles": manifest.get("roles", []),
        "reviewed_at": row.reviewed_at,
        # Where a skill from the open ecosystem came from, and what the
        # office knows about it. None for a curated skill.
        "source": manifest.get("source"),
        "audits": manifest.get("audits"),
        "assumptions": manifest.get("assumptions"),
        "refused": manifest.get("refused"),
    }


async def read_skill_source(
    d: RouterDeps, text: str, commit: str | None = None
) -> skill_ecosystem.Found:
    try:
        owner, repo, skill = skill_ecosystem.parse_source(text)
        return await skill_ecosystem.fetch(
            owner, repo, skill, mcp_store.allow_hosts(d.settings), commit=commit
        )
    except skill_ecosystem.EcosystemError as failed:
        if failed.code in ("choose_skill", "skill_not_found"):
            raise HTTPException(
                status_code=409, detail={"code": failed.code, "choices": failed.choices}
            ) from None
        status = 422 if failed.code in ("source_missing", "source_unreadable") else 502
        raise HTTPException(status_code=status, detail=f"skill_{failed.code}") from None


def installed_row(d: RouterDeps, session: Session, skill_id: str, version: str) -> InstalledSkill:
    return found(
        session.scalar(
            select(InstalledSkill).where(
                InstalledSkill.skill_id == skill_id, InstalledSkill.version == version
            )
        ),
        "Skill",
    )


def add_team_routes(router: APIRouter, d: RouterDeps) -> None:
    @router.post("/skills/ecosystem/preview")
    async def preview_ecosystem_skill(body: SkillSource) -> dict[str, Any]:
        """What a skill from skills.sh or GitHub is, before anything is installed."""
        found = await read_skill_source(d, body.source)
        return found.view()

    @router.post("/skills/ecosystem/install", status_code=201)
    async def install_ecosystem_skill(body: SkillSourceInstall) -> dict[str, Any]:
        """Install the commit the owner previewed, for the role they chose.

        Read again at that commit, so what is installed is what was reviewed
        even if the repository moved on in between. The same commit twice is
        the same skill; a new commit is a new version.
        """
        found = await read_skill_source(d, body.source, commit=body.commit)
        with d.session_factory() as session, session.begin():
            skill_id = skill_ecosystem.manifest(found, body.role, "1.0.0")["skill_id"]
            rows = list(
                session.scalars(select(InstalledSkill).where(InstalledSkill.skill_id == skill_id))
            )
            for row in rows:
                source = (row.manifest_json or {}).get("source") or {}
                if (
                    source.get("commit") == found.commit
                    and source.get("repo") == f"{found.owner}/{found.repo}"
                ):
                    skill_updates.mark_installed(session, skill_id, found.commit)
                    return skill_view(d, row)
            if any(not (row.manifest_json or {}).get("source") for row in rows):
                # A curated skill already has this name: never overwritten.
                raise HTTPException(status_code=409, detail="skill_id_taken")
            data = skill_ecosystem.manifest(found, body.role, f"1.0.{len(rows)}")
            try:
                manifest = SkillManifest.model_validate(data)
            except ValidationError:
                raise HTTPException(status_code=422, detail="skill_unusable") from None
            # An update that asks for more than the version before it waits for
            # the owner's review, as a curated update does.
            before = (
                set().union(*[set((r.manifest_json or {}).get("permissions") or []) for r in rows])
                if rows
                else set()
            )
            widened = bool(rows) and not set(manifest.permissions) <= before
            row = InstalledSkill(
                skill_id=manifest.skill_id,
                version=manifest.version,
                status="NEEDS_REVIEW" if widened else "ENABLED",
                manifest_json=manifest.model_dump(),
                manifest_sha256=manifest_digest(manifest),
                reviewed_at=None if widened else datetime.now(UTC),
            )
            session.add(row)
            session.flush()
            skill_updates.mark_installed(session, manifest.skill_id, found.commit)
            return skill_view(d, row)

    @router.get("/skills/updates")
    def list_skill_updates() -> list[dict[str, Any]]:
        """What the last check said about each skill from GitHub."""
        with d.session_factory() as session:
            return skill_updates.listing(session)

    @router.post("/skills/updates/check")
    async def check_skill_updates() -> list[dict[str, Any]]:
        """Ask GitHub now whether any installed skill has been improved."""
        return await skill_updates.check(d.session_factory, mcp_store.allow_hosts(d.settings))

    @router.post("/skills/{skill_id}/update/preview")
    async def preview_skill_update(skill_id: str) -> dict[str, Any]:
        """The update as the owner decides on it: what changes, what it adds."""
        with d.session_factory() as session:
            known = session.scalar(select(SkillUpdate).where(SkillUpdate.skill_id == skill_id))
            current = skill_updates.newest_from_github(session).get(skill_id)
            if known is None or current is None or not known.latest_commit:
                raise HTTPException(status_code=404, detail="no_update_known")
            before = dict(current.manifest_json or {})
            text, commit = skill_updates.source_text(known.repo, known.path), known.latest_commit
        found = await read_skill_source(d, text, commit=commit)
        return {
            "skill_id": skill_id,
            "commit": found.commit,
            "source_text": text,
            "role": (before.get("roles") or ["summarist"])[0],
            "changes": skill_updates.changes(
                str(before.get("instructions") or ""), found.instructions
            ),
            "permissions_added": sorted(
                set(found.permissions) - set(before.get("permissions") or [])
            ),
            "found": found.view(),
        }

    @router.get("/skills/catalog")
    def skill_catalog() -> list[dict[str, Any]]:
        """Curated skills and every version's declared permissions and review."""
        try:
            catalog = load_catalog()
        except SkillError as error:
            raise HTTPException(status_code=503, detail=error.code) from None
        return [
            {
                "skill_id": skill_id,
                "name": next(iter(versions.values())).name,
                "versions": [
                    {
                        "version": manifest.version,
                        "description": manifest.description,
                        "publisher": manifest.publisher,
                        "roles": manifest.roles,
                        "permissions": manifest.permissions,
                        "changelog": manifest.changelog,
                        "review": manifest.review.model_dump(),
                        "instructions": manifest.instructions,
                    }
                    for manifest in sorted(
                        versions.values(), key=lambda m: tuple(map(int, m.version.split(".")))
                    )
                ],
            }
            for skill_id, versions in sorted(catalog.items())
        ]

    @router.post("/skills/install", status_code=201)
    def install_skill(body: SkillInstall) -> dict[str, Any]:
        """Install a catalog version after verifying its integrity.

        A version that declares permissions no earlier installed version had
        waits for owner review; others are enabled at once.
        """
        try:
            manifest = load_manifest(CATALOG_DIR, body.skill_id, body.version)
        except SkillError as error:
            code = 404 if error.code == "unknown_skill" else 422
            raise HTTPException(status_code=code, detail=error.code) from None
        with d.session_factory() as session, session.begin():
            existing = list(
                session.scalars(
                    select(InstalledSkill).where(
                        InstalledSkill.skill_id == body.skill_id,
                        InstalledSkill.status != "UNINSTALLED",
                    )
                )
            )
            previous = sorted(
                {p for row in existing for p in row.manifest_json.get("permissions", [])}
            )
            new = expanded_permissions(previous, manifest.permissions) if existing else []
            unreviewed = manifest.review.status != "reviewed"
            status = "NEEDS_REVIEW" if new or unreviewed else "ENABLED"
            row = session.scalar(
                select(InstalledSkill).where(
                    InstalledSkill.skill_id == body.skill_id,
                    InstalledSkill.version == body.version,
                )
            )
            if row is None:
                row = InstalledSkill(skill_id=body.skill_id, version=body.version)
                session.add(row)
            row.status = status
            row.manifest_json = manifest.model_dump()
            row.manifest_sha256 = manifest_digest(manifest)
            row.reviewed_at = None if status == "NEEDS_REVIEW" else datetime.now(UTC)
            session.flush()
            return skill_view(d, row) | {"new_permissions": new}

    @router.get("/skills")
    def list_skills(include_uninstalled: bool = False) -> list[dict[str, Any]]:
        with d.session_factory() as session:
            statement = select(InstalledSkill).order_by(
                InstalledSkill.skill_id, InstalledSkill.version
            )
            if not include_uninstalled:
                statement = statement.where(InstalledSkill.status != "UNINSTALLED")
            # Which business lines keep each skill in their pack.
            packs: dict[str, list[str]] = {}
            for venture in session.scalars(
                select(Venture).where(Venture.status == "ACTIVE").order_by(Venture.name)
            ):
                for held in venture.skill_ids_json or []:
                    packs.setdefault(str(held), []).append(venture.name)
            return [
                skill_view(d, row) | {"lines": packs.get(row.skill_id, [])}
                for row in session.scalars(statement)
            ]

    @router.post("/skills/{skill_id}/versions/{version}/approve")
    def approve_skill(skill_id: str, version: str) -> dict[str, Any]:
        """The owner reviewed the declared permissions and accepts them."""
        with d.session_factory() as session, session.begin():
            row = installed_row(d, session, skill_id, version)
            if row.status != "NEEDS_REVIEW":
                raise HTTPException(status_code=409, detail="skill_not_awaiting_review")
            row.status = "ENABLED"
            row.reviewed_at = datetime.now(UTC)
            return skill_view(d, row)

    @router.post("/skills/{skill_id}/versions/{version}/{action}")
    def toggle_skill(skill_id: str, version: str, action: str) -> dict[str, Any]:
        if action not in ("enable", "disable"):
            raise HTTPException(status_code=404, detail="Not Found")
        with d.session_factory() as session, session.begin():
            row = installed_row(d, session, skill_id, version)
            if row.status in ("NEEDS_REVIEW", "UNINSTALLED"):
                raise HTTPException(status_code=409, detail=f"skill_{row.status.lower()}")
            row.status = "ENABLED" if action == "enable" else "DISABLED"
            return skill_view(d, row)

    @router.delete("/skills/{skill_id}/versions/{version}")
    def uninstall_skill(skill_id: str, version: str) -> dict[str, Any]:
        """Stop offering a skill. Earlier runs keep the copy they captured."""
        with d.session_factory() as session, session.begin():
            row = installed_row(d, session, skill_id, version)
            row.status = "UNINSTALLED"
            affected = sorted(
                {
                    name
                    for name, pins in session.execute(
                        select(AgentProfile.name, AgentProfileVersion.skills_json).join(
                            AgentProfileVersion,
                            (AgentProfileVersion.profile_id == AgentProfile.id)
                            & (AgentProfileVersion.version == AgentProfile.enabled_version),
                        )
                    )
                    if any(
                        pin.get("skill_id") == skill_id and pin.get("version") == version
                        for pin in pins or []
                    )
                }
            )
            return skill_view(d, row) | {"affected_agents": affected}

    @router.get("/agents", response_model=list[AgentView])
    def list_agents() -> list[dict[str, Any]]:
        with d.session_factory() as session:
            profiles = session.scalars(select(AgentProfile).order_by(AgentProfile.name))
            return [agent_view(d, session, profile) for profile in profiles]

    @router.post("/agents", response_model=AgentView, status_code=201)
    def create_agent(body: AgentCreate) -> dict[str, Any]:
        try:
            with d.session_factory() as session, session.begin():
                profile = AgentProfile(name=body.name, role=body.role, status="ACTIVE")
                session.add(profile)
                session.flush()
                profile.enabled_version = add_version(d, session, profile, body)
                session.flush()
                session.refresh(profile)
                return agent_view(d, session, profile)
        except IntegrityError:
            raise HTTPException(status_code=409, detail="agent_name_already_exists") from None

    @router.get("/agents/{agent_id}/export")
    def export_agent_file(agent_id: UUID) -> Response:
        """The agent as a file: what you decided, not what this office holds."""
        with d.session_factory() as session:
            profile = found(session.get(AgentProfile, agent_id), "Agent")
            document = export_agent(session, profile)
            name = profile.name
        return Response(
            content=to_yaml(document),
            media_type="application/yaml; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="agent-{name[:40]}.yaml"'.replace(
                    " ", "-"
                )
            },
        )

    @router.post("/agents/import")
    def import_agent_file(body: AgentImport) -> dict[str, Any]:
        """Read an agent file. Without `confirm` it only says what it would do."""
        try:
            document = read_yaml(body.document)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except yaml.YAMLError:
            raise HTTPException(status_code=422, detail="not_yaml") from None
        with d.session_factory() as session:
            plan = plan_import(session, document)
        if not body.confirm:
            return {"plan": plan, "imported": False}
        if not plan["can_import"]:
            raise HTTPException(status_code=409, detail="agent_name_already_exists")
        created = create_agent(
            AgentCreate(
                name=plan["name"],
                role=plan["role"],
                instructions=plan["instructions"],
                connection_id=UUID(plan["connection_id"]) if plan["connection_id"] else None,
                skills=[SkillPin(**pin) for pin in plan["skills"]],
                limits=plan["limits"],
            )
        )
        return {"plan": plan, "imported": True, "agent": created}

    @router.patch("/agents/{agent_id}", response_model=AgentView)
    def update_agent_identity(agent_id: UUID, body: AgentIdentityUpdate) -> dict[str, Any]:
        """Rename an agent or move it to another role.

        Versions are untouched: the role decides which workflow step the agent
        contributes to, and runs already started keep their snapshot.
        """
        try:
            with d.session_factory() as session, session.begin():
                profile = found(session.get(AgentProfile, agent_id), "Agent")
                if body.name is not None:
                    profile.name = body.name
                if body.role is not None:
                    profile.role = body.role
                session.flush()
                session.refresh(profile)
                return agent_view(d, session, profile)
        except IntegrityError:
            raise HTTPException(status_code=409, detail="agent_name_already_exists") from None

    @router.post("/agents/{agent_id}/test")
    async def test_agent(agent_id: UUID, body: ConnectionTest) -> dict[str, Any]:
        """Send one short sample to the enabled version's model. Billable."""
        if not body.billable:
            raise HTTPException(status_code=409, detail="billable_confirmation_required")
        with d.session_factory() as session:
            profile = found(session.get(AgentProfile, agent_id), "Agent")
            version = session.scalar(
                select(AgentProfileVersion).where(
                    AgentProfileVersion.profile_id == profile.id,
                    AgentProfileVersion.version == profile.enabled_version,
                )
            )
            connection = (
                session.get(ModelConnection, version.connection_id)
                if version is not None and version.connection_id is not None
                else None
            )
            if version is None or connection is None:
                raise HTTPException(status_code=409, detail="agent_has_no_model")
            provider, model_id, reference = (
                connection.provider,
                connection.model_id,
                connection.credential_ref,
            )
            instructions, number = version.instructions, version.version
        try:
            credential = resolve_credential(reference, d.settings)
        except SecretStoreError:
            raise HTTPException(status_code=409, detail="credential_missing") from None
        completer = DirectCompleter(
            provider, model_id, credential, d.settings, label=f"agent-test:{agent_id}"
        )
        messages = [
            {"role": "system", "content": instructions or "You are an agent in a private office."},
            {
                "role": "user",
                "content": "In one sentence, describe what you will do in this office.",
            },
        ]
        try:
            completion = await completer.complete("agent-test", messages)
        except GatewayError as error:
            raise HTTPException(status_code=502, detail=error.code) from None
        return {
            "version": number,
            "provider": provider,
            "model": completion.model,
            "latency_seconds": round(completion.latency_seconds, 2),
            "usage": {
                "prompt_tokens": completion.usage.prompt_tokens,
                "completion_tokens": completion.usage.completion_tokens,
            },
            "reply": completion.content[:500],
        }

    @router.get("/agents/{agent_id}", response_model=AgentView)
    def get_agent(agent_id: UUID) -> dict[str, Any]:
        with d.session_factory() as session:
            return agent_view(d, session, found(session.get(AgentProfile, agent_id), "Agent"))

    @router.post("/agents/{agent_id}/versions", response_model=AgentView, status_code=201)
    def create_agent_version(agent_id: UUID, body: AgentVersionCreate) -> dict[str, Any]:
        """Save a new version. It is not used by new tasks until enabled."""
        with d.session_factory() as session, session.begin():
            profile = found(session.get(AgentProfile, agent_id), "Agent")
            add_version(d, session, profile, body)
            return agent_view(d, session, profile)

    @router.post("/agents/{agent_id}/versions/{version}/enable", response_model=AgentView)
    def enable_agent_version(agent_id: UUID, version: int) -> dict[str, Any]:
        with d.session_factory() as session, session.begin():
            profile = found(session.get(AgentProfile, agent_id), "Agent")
            found(
                session.scalar(
                    select(AgentProfileVersion).where(
                        AgentProfileVersion.profile_id == profile.id,
                        AgentProfileVersion.version == version,
                    )
                ),
                "Agent version",
            )
            profile.enabled_version = version
            session.flush()
            session.refresh(profile)
            return agent_view(d, session, profile)

    @router.post("/agents/{agent_id}/archive", response_model=AgentView)
    def archive_agent(agent_id: UUID) -> dict[str, Any]:
        """Stop offering the agent to new runs. History and versions are kept."""
        with d.session_factory() as session, session.begin():
            profile = found(session.get(AgentProfile, agent_id), "Agent")
            profile.status = "ARCHIVED"
            session.flush()
            session.refresh(profile)
            return agent_view(d, session, profile)

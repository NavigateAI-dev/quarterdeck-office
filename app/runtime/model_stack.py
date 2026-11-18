"""Each role's model stack: backups, provider moves, mid-run downgrades.

Moved out of the run worker; the one role-stack builder.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from sqlalchemy import select

from app.agent_routing import innermost, members, swap_model
from app.config import Settings
from app.connection_completer import completer_for_entry
from app.db.models import ModelConnection
from app.failover import GATEWAY, FailoverCompleter
from app.gateway.direct import Completer
from app.judgments import build_judge
from app.model_catalog import price_map
from app.model_router import USABLE, decide, moves, price_of, targets
from app.office_settings import effective, provider_backups
from app.orchestrator.graph import AgentSet
from app.orchestrator.runner import RunRepository
from app.project_settings import downgrade_rule, pinned_roles
from app.runtime.job import Job

# A price catalogue that cannot be read quickly leaves the owner's models in place.
CATALOGUE_SECONDS = 5.0
ROUTED = "model_routed"


@dataclass(frozen=True)
class Backups:
    """The owner's backup plan per provider key, the connections, and their names."""

    chosen: dict[str, dict[str, Any]]
    rows: dict[str, Any]
    names: dict[str, str]


class ModelStack:
    def __init__(self, settings: Settings, repository: RunRepository, session_factory: Any) -> None:
        self.settings = settings
        self.repository = repository
        self.session_factory = session_factory

    def completer_for(self, entry: dict[str, Any]) -> Completer:
        return completer_for_entry(entry, self.settings, self.session_factory)

    async def route(
        self,
        job: Job,
        agents: AgentSet,
        entries: dict[str, dict[str, Any]],
        covered: set[str],
    ) -> dict[str, dict[str, Any]]:
        """The role models for this task, after the model router.

        Decided once per task and recorded with the roles it moved; a resumed
        task applies the same moves instead of asking again. Roles an agent or
        the project pinned are never moved, and with routing set to fixed
        nothing changes.
        """
        if self.session_factory is None:
            return entries
        run_id = job.dispatch.run_id
        recorded = self._recorded_moves(run_id)
        if recorded is not None:
            return entries | recorded
        with self.session_factory() as session:
            if effective(session, self.settings)["model_routing"] != "auto":
                return entries
            pinned = covered | pinned_roles(session, job.dispatch.project_id)
            connections = [
                {
                    "connection_id": str(row.id),
                    "provider": row.provider,
                    "model_id": row.model_id,
                    "base_url": row.base_url,
                    "status": row.status,
                }
                for row in session.scalars(select(ModelConnection))
            ]
        # Nothing is fetched for a router that cannot run: no judge, or no model to move to.
        judge = build_judge(self.settings)
        usable = [row for row in connections if row["status"] in USABLE]
        catalogue = await self._catalogue() if judge is not None and usable else {}
        pool = targets(usable, catalogue)
        decision = await decide(job.objective, pool, judge)
        moved: dict[str, dict[str, Any]] = {}
        if decision.target is not None:
            current: dict[str, float | None] = {}
            # A role without its own entry answers through its alias's role
            # (the planner through the Chief's), so it costs what that one does.
            role_of = {alias: role for role, alias in self.settings.model_aliases.items()}
            for agent in members(agents):
                role = str(agent.role)
                alias = str(getattr(agent, "model_alias", "") or "")
                entry = entries.get(role) or entries.get(role_of.get(alias, ""))
                current[role] = (
                    price_of(catalogue, str(entry["provider"]), str(entry["model_id"]))
                    if entry is not None
                    else None
                )
            moved = moves(decision, current, pinned)
        self.repository.add_event(
            run_id,
            ROUTED,
            {
                "outcome": decision.outcome,
                "tier": decision.tier,
                "confidence": decision.confidence,
                "model_id": decision.target.entry["model_id"] if decision.target else None,
                "roles": sorted(moved),
                "moved": moved,
                "pool": {tier: target.entry["model_id"] for tier, target in pool.items()},
            },
        )
        return entries | moved

    def _recorded_moves(self, run_id: Any) -> dict[str, dict[str, Any]] | None:
        recorded = next(
            (event for event in self.repository.events(run_id) if event.get("kind") == ROUTED), None
        )
        if recorded is None:
            return None
        return dict((recorded.get("payload") or {}).get("moved") or {})

    async def _catalogue(self) -> dict[str, Any]:
        try:
            async with asyncio.timeout(CATALOGUE_SECONDS):
                return await price_map(self.settings.workspace_root)
        except TimeoutError:
            return {}

    def with_backups(self, job: Job, agents: AgentSet) -> None:
        """Put the backup the owner chose behind each role's provider."""
        backups = self._backups()
        if backups is None:
            return
        for agent in members(agents):
            alias = str(getattr(agent, "model_alias", "") or "")
            key = alias.removeprefix("connection:") if alias.startswith("connection:") else GATEWAY
            agent.gateway = self.backed(job, str(agent.role), agent.gateway, key, backups)

    def backed(
        self,
        job: Job,
        role: str,
        completer: Any,
        key: str,
        backups: "Backups | None",
        previous: FailoverCompleter | None = None,
    ) -> Any:
        """A role's model with a backup behind it: the one stack builder.

        The backup is the one the owner chose for this model's provider; when
        they chose none, a role that had a backup keeps it (owner, 16 Oct 2026:
        a mid-run downgrade must not leave a role without one). Otherwise the
        model stands alone.
        """
        plan = (backups.chosen.get(key) or {}) if backups is not None else {}
        backup_key = str(plan.get("backup") or "")
        made = self._backup_for(backup_key, backups) if backup_key != key else None
        names = backups.names if backups is not None else {}
        if made is not None:
            make, alias = made
            return FailoverCompleter(
                completer,
                key,
                backup_key,
                make,
                alias,
                bool(plan.get("come_back", True)),
                on_move=self.mover(job, role, key, backup_key, names),
            )
        if previous is not None and previous.backup_key != key:
            return FailoverCompleter(
                completer,
                key,
                previous.backup_key,
                previous.make_backup,
                previous.backup_alias,
                previous.come_back,
                on_move=self.mover(job, role, key, previous.backup_key, names),
            )
        return completer

    def _backup_for(
        self, backup_key: str, backups: "Backups | None"
    ) -> tuple[Callable[[], Any], str] | None:
        """How to make the named backup, and the alias it answers to.

        Only a connection: a backup still naming the retired gateway is none.
        """
        if not backup_key or backups is None:
            return None
        row = backups.rows.get(backup_key)
        if row is None:
            return None
        entry = {
            "connection_id": backup_key,
            "provider": row.provider,
            "model_id": row.model_id,
            "base_url": row.base_url,
        }
        return partial(self.completer_for, entry), f"connection:{backup_key}"

    def _backups(self) -> "Backups | None":
        """The owner's backup per provider, with the connections they name."""
        if self.session_factory is None:
            return None
        with self.session_factory() as session:
            chosen = provider_backups(session)
            if not chosen:
                return None
            rows = {str(row.id): row for row in session.scalars(select(ModelConnection))}
        names = {key: row.display_name for key, row in rows.items()}
        return Backups(chosen, rows, names)

    def mover(
        self, job: Job, role: str, primary: str, backup: str, names: dict[str, str]
    ) -> Callable[[str], None]:
        def moved(reason: str) -> None:
            self.repository.add_event(
                job.dispatch.run_id,
                "model_failed_over",
                {
                    "role": role,
                    "from": names.get(primary, primary),
                    "to": names.get(backup, backup),
                    "from_key": primary,
                    "to_key": backup,
                    "reason": reason,
                },
            )

        return moved

    def downgrade_watch(self, job: Job, agents: AgentSet) -> Callable[[str], None] | None:
        """Move a task to its project's cheaper model once it has spent enough.

        The owner asked for the work to finish rather than stop, so this trades
        the model down at a step boundary and says so in the task room.
        """
        if self.session_factory is None:
            return None
        factory = self.session_factory
        moved = False

        def before_step(node: str) -> None:
            nonlocal moved
            if moved:
                return
            with factory() as session:
                rule = downgrade_rule(session, job.dispatch.project_id)
            if rule is None:
                return
            threshold, entry = rule
            spent = self.repository.usage(job.dispatch.run_id)["spent"]
            if not spent.get("cost_reported") or float(spent.get("cost_usd") or 0) < threshold:
                return
            moved = True
            completer = completer_for_entry(entry, self.settings, factory)
            backups = self._backups()
            for field_name in (
                "planner",
                "researcher",
                "strategist",
                "critic",
                "chief",
                "plan_reviewer",
            ):
                role = getattr(agents, field_name, None)
                if role is None:
                    continue
                earlier = innermost(role)
                swap_model(
                    role,
                    self.backed(
                        job,
                        str(role.role),
                        completer,
                        str(entry["connection_id"]),
                        backups,
                        earlier if isinstance(earlier, FailoverCompleter) else None,
                    ),
                )
                role.provider = entry["provider"]
                role.model_alias = f"connection:{entry['connection_id']}"
            self.repository.add_event(
                job.dispatch.run_id,
                "model_downgraded",
                {
                    "model": entry["model_id"],
                    "after_usd": threshold,
                    "spent_usd": round(float(spent.get("cost_usd") or 0), 4),
                },
            )

        return before_step

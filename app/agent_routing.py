"""Apply a run's agent snapshot to the analysis roles.

A snapshotted agent whose role matches a workflow role contributes its
instructions to that role, and, when it names a model connection, the role
calls that connection instead of the default gateway alias. Output contracts
and validation are unchanged: the profile adds guidance, it cannot replace
the schema a role must satisfy.
"""

from collections.abc import Callable
from dataclasses import fields
from typing import Any

from app.budget import BudgetedCompleter, RunLedger
from app.events import InvocationRecorder
from app.model_calls import KeptCompleter
from app.orchestrator.graph import AgentSet
from app.policies.guard import GuardedCompleter


def members(agents: AgentSet) -> list[Any]:
    """The agents of a set: every role that is filled, and not its skills library."""
    found = []
    for item in fields(agents):
        value = getattr(agents, item.name)
        if item.name != "skills" and value is not None:
            found.append(value)
    return found


def apply_snapshot(
    agents: AgentSet,
    snapshot: dict[str, Any],
    completer_for: Callable[[dict[str, Any]], Any],
    available_permissions: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Apply profiles and their pinned skills; return what happened to each skill.

    A skill runs only when every permission it declares is available to this
    run; otherwise it is skipped and the reason recorded.
    """
    available = available_permissions or set()
    outcomes: list[dict[str, Any]] = []
    by_role = {entry["role"]: entry for entry in snapshot.get("agents", [])}
    for agent in members(agents):
        entry = by_role.get(agent.role)
        if entry is None:
            continue
        if entry.get("instructions"):
            agent.instructions = (
                f"{type(agent).instructions}\n\n"
                f"Owner instructions for agent '{entry['name']}' (version {entry['version']}):\n"
                f"{entry['instructions']}"
            )
        skill_text = []
        for skill in entry.get("skills") or []:
            missing = [p for p in skill.get("permissions", []) if p not in available]
            outcome = {"skill_id": skill["skill_id"], "version": skill["version"]}
            if missing:
                reason = f"missing_permission:{missing[0]}"
                outcomes.append(outcome | {"status": "skipped", "reason": reason})
                continue
            skill_text.append(
                f"Skill '{skill['name']}' (version {skill['version']}):\n{skill['instructions']}"
            )
            outcomes.append(outcome | {"status": "applied"})
        if skill_text:
            agent.instructions = agent.instructions + "\n\n" + "\n\n".join(skill_text)
        if entry.get("connection_id"):
            agent.gateway = completer_for(entry)
            agent.provider = entry["provider"]
            agent.model_alias = f"connection:{entry['connection_id']}"
    return outcomes


def apply_role_models(
    agents: AgentSet,
    entries: dict[str, dict[str, Any]],
    completer_for: Callable[[dict[str, Any]], Any],
    already: set[str],
) -> None:
    """Point roles without their own agent at the office's chosen models.

    An agent's own connection wins; this fills the rest, so the owner can pick
    a model per role without creating an agent for it.
    """
    for agent in members(agents):
        if agent is None:
            continue  # An optional role this workflow does not use.
        entry = entries.get(agent.role)
        if entry is None or agent.role in already:
            continue
        agent.gateway = completer_for(entry)
        agent.provider = entry["provider"]
        agent.model_alias = f"connection:{entry['connection_id']}"


def correlate(agents: AgentSet, recorder: InvocationRecorder) -> AgentSet:
    """Give every role one recorder that stamps the run and task on its events."""
    for agent in members(agents):
        agent.recorder = recorder
    return agents


def guard(agents: AgentSet, gate: Any, run_id: Any, project_id: Any) -> AgentSet:
    """Put the office's policies in front of every role's model calls."""

    for agent in members(agents):
        if agent is None:
            continue
        agent.gateway = GuardedCompleter(agent.gateway, gate, run_id, project_id, agent.role)
    return agents


def keep(agents: AgentSet, calls: Any, run_id: Any) -> AgentSet:
    """Keep every call each role's model answers, for replay.

    Inside the policy and budget layers, around the model and its backups: only
    calls that reach a model are kept, as the model got them."""
    for agent in members(agents):
        if agent is None:
            continue
        agent.gateway = KeptCompleter(agent.gateway, calls, run_id, agent.role)
    return agents


def budget(agents: AgentSet, ledger: RunLedger) -> AgentSet:
    """Route every role's calls through the run's budget ledger."""
    for agent in members(agents):
        agent.gateway = BudgetedCompleter(agent.gateway, ledger)
    return agents


def _layers() -> tuple[type[Any], ...]:
    """The budget, policy and keeping layers that wrap a role's model."""
    from app.policies.guard import GuardedCompleter

    return (BudgetedCompleter, GuardedCompleter, KeptCompleter)


def innermost(agent: Any) -> Any:
    """The role's model under the budget and policy layers: the one a swap replaces."""
    current: Any = agent.gateway
    while isinstance(current, _layers()):
        current = current.inner
    return current


def swap_model(agent: Any, completer: Any) -> None:
    """Give a role another model behind the policies and budget it already has.

    `guard` and `budget` wrap a role's gateway; replacing the gateway outright
    would drop them, and with them the run's ledger and the office's rules.
    Only the innermost model is changed; `ModelStack.backed` puts a backup
    behind the new model first, so a swap keeps one.
    """
    holder: Any = agent
    attribute = "gateway"
    current: Any = agent.gateway
    while isinstance(current, _layers()):
        holder, attribute, current = current, "inner", current.inner
    setattr(holder, attribute, completer)


def apply_venture_doctrine(agents: AgentSet, venture: str, doctrine: dict[str, str]) -> list[str]:
    """Tell each role how work is judged in this business line.

    The same critic serves compliance work and marketing; what it should refuse
    is not the same in both. This is the last layer, after the owner's own
    instructions and any skills, because it is the standard the work is
    measured against — but it cannot touch the output contract, which is the
    schema every role still has to satisfy.
    """
    applied: list[str] = []
    for agent in members(agents):
        text = str(doctrine.get(agent.role) or "").strip()
        if not text:
            continue
        agent.instructions = (
            f"{agent.instructions}\n\nHow work is judged in {venture}, for your role:\n{text}"
        )
        applied.append(agent.role)
    return applied

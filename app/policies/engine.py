"""The office's rules, as named policies.

A policy reads one event — a model call, a tool call, a result — and answers
ALLOW, DENY or ASK. Policies compose: the office's own and the project's are
evaluated together and the most restrictive answer wins, so adding a policy
can only narrow what the office does.

Nothing here can widen an existing rule. Approval before an external effect,
path confinement, skill permissions and budgets are enforced where they always
were; policies refuse earlier, never permit later.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID


class Hook(StrEnum):
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    RESULT = "result"


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class PolicyEvent:
    """What is about to happen, in the office's own words."""

    hook: Hook
    # What is being used: a role for a model call, a tool name for a tool call.
    target: str
    run_id: UUID | None = None
    project_id: UUID | None = None
    # Tool arguments, model alias, URL, and anything else the policy reads.
    data: dict[str, Any] = field(default_factory=dict)
    # Counters this run has accumulated, kept durably between steps.
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str = ""
    policy: str = ""
    # Counter changes to keep for the rest of the run, e.g. {"fetches": 1}.
    add: dict[str, int] = field(default_factory=dict)


Evaluator = Callable[[PolicyEvent, dict[str, Any]], Decision | None]


def has_allowance(event: PolicyEvent, policy_id: str) -> bool:
    """Whether the owner already allowed this policy's subject for this task."""
    return bool(event.state.get(f"allow:{policy_id}"))


@dataclass(frozen=True)
class PolicyKind:
    """One rule the owner can turn on, and what it needs to know."""

    id: str
    title: str
    about: str
    hooks: tuple[Hook, ...]
    parameters: dict[str, Any]
    evaluate: Evaluator


REGISTRY: dict[str, PolicyKind] = {}


def register(kind: PolicyKind) -> PolicyKind:
    REGISTRY[kind.id] = kind
    return kind


@dataclass(frozen=True)
class ConfiguredPolicy:
    """A policy the owner turned on, with its settings and where it applies."""

    id: UUID
    kind: str
    scope: str  # "office" or "project"
    params: dict[str, Any]
    project_id: UUID | None = None


# A stricter answer replaces a weaker one; DENY ends the evaluation.
RANK = {Verdict.ALLOW: 0, Verdict.ASK: 1, Verdict.DENY: 2}


def evaluate(event: PolicyEvent, policies: list[ConfiguredPolicy]) -> Decision:
    """The office's answer for this event: the most restrictive one given."""
    answer = Decision(Verdict.ALLOW)
    added: dict[str, int] = {}
    for policy in policies:
        kind = REGISTRY.get(policy.kind)
        if kind is None or event.hook not in kind.hooks:
            continue
        decision = kind.evaluate(event, policy.params)
        if decision is None:
            continue
        for key, value in decision.add.items():
            added[key] = added.get(key, 0) + value
        if RANK[decision.verdict] > RANK[answer.verdict]:
            answer = Decision(
                decision.verdict, decision.reason, decision.policy or policy.kind, added
            )
        if decision.verdict is Verdict.DENY:
            # Nothing later can permit what this policy refused.
            return Decision(Verdict.DENY, decision.reason, decision.policy or policy.kind, added)
    return Decision(answer.verdict, answer.reason, answer.policy, added)

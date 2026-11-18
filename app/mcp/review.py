"""What the owner decided about each tool a server offers.

Nothing an MCP server offers is callable because the server says so. The
specification is explicit that a client must treat a server's own annotations
as untrusted, so `readOnlyHint` is shown to the owner as a claim and decides
nothing. The owner decides, per tool, and the office records what they decided.

Three stances, and they are deliberately not a boolean:

- **allowed** — an agent may call it during a run, still through the policy
  engine, which can refuse it for its own reasons.
- **needs_approval** — an agent may propose it and the run stops for the owner,
  exactly as an engineering scope does. This is the stance for anything that
  changes the world.
- **refused** — never called, and not offered to an agent at all.

A decision is bound to the tool as it stood when it was made. A tool whose
description or schema changes afterwards is unreviewed again, because "I read
this and it was fine" is a statement about a particular thing, not about a name.
That is the same reasoning as a skill's manifest digest.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from app.policy import POLICY, Permission

ALLOWED = "allowed"
NEEDS_APPROVAL = "needs_approval"
REFUSED = "refused"
STANCES = (ALLOWED, NEEDS_APPROVAL, REFUSED)

# What an unreviewed tool is: not refused by the owner, but not callable.
UNREVIEWED = "unreviewed"
# What a tool is when the owner reviewed it and it has changed since.
CHANGED = "changed_since_review"
# What a tool is when the owner tagged it with an action SPEC section 18 denies.
# No stance promotes it: that section is the one thing a review cannot overrule.
DENIED_BY_POLICY = "denied_by_policy"
# The most the owner may say one call costs. A price is the owner's estimate of
# a vendor's charge; anything above this is a typing slip, not a tariff.
MAX_PRICE_USD = 1000.0


def tool_digest(tool: dict[str, Any]) -> str:
    """A digest of the tool the owner actually read.

    Name, description and schema: the three things that tell an owner what a
    tool does. The server's own claims are deliberately excluded — a server
    that changes only its `readOnlyHint` has not changed what the tool is, and
    the office never believed the hint anyway.
    """
    material = json.dumps(
        {
            "name": tool.get("name"),
            "description": tool.get("description"),
            "schema": tool.get("schema"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def stance_of(review: dict[str, Any], tool: dict[str, Any]) -> str:
    """The owner's stance on this tool as it stands now."""
    decided = review.get(str(tool.get("name") or ""))
    if not isinstance(decided, dict):
        return UNREVIEWED
    stance = str(decided.get("stance") or "")
    if stance not in STANCES:
        return UNREVIEWED
    if decided.get("digest") != tool_digest(tool):
        # Reviewed, then changed. The decision was about something else.
        return CHANGED
    acts_as = decided.get("acts_as", [])
    if not isinstance(acts_as, list) or any(action not in POLICY for action in acts_as):
        # A tag the office cannot read cannot be honoured, so nothing is allowed.
        return UNREVIEWED
    return ceiling(stance, acts_as)


def ceiling(stance: str, acts_as: list[str]) -> str:
    """The owner's stance, capped by what section 18 says of the tagged actions.

    The office cannot tell what a third party's tool does; the owner says so by
    tagging it. A denied action refuses the tool whatever it was set to, and an
    action that needs approval makes "allowed" mean "needs approval".
    """
    permissions = {POLICY[action] for action in acts_as}
    if Permission.DENY in permissions:
        return DENIED_BY_POLICY
    gated = {Permission.APPROVAL, Permission.APPROVAL_OR_PREAPPROVED}
    if stance == ALLOWED and permissions & gated:
        return NEEDS_APPROVAL
    return stance


def decide(
    review: dict[str, Any],
    tool: dict[str, Any],
    stance: str,
    owner: str,
    acts_as: list[str] | tuple[str, ...] = (),
    price_usd: float | None = None,
) -> dict[str, Any]:
    """Record the owner's stance on one tool, the section 18 actions it
    performs, and the most one call costs if the vendor charges for it.
    Returns the new review."""
    if price_usd is not None and not (0 < price_usd <= MAX_PRICE_USD):
        # NaN fails the comparison too, so it is refused with the rest.
        raise ValueError("a price must be more than 0 and at most 1000 dollars")
    if stance not in STANCES:
        raise ValueError(f"unknown stance: {stance}")
    unknown = sorted(set(acts_as) - set(POLICY))
    if unknown:
        raise ValueError(f"unknown action: {', '.join(unknown)}")
    name = str(tool.get("name") or "")
    if not name:
        raise ValueError("a tool with no name cannot be decided")
    return {
        **review,
        name: {
            "stance": stance,
            "acts_as": sorted(set(acts_as)),
            "price_usd": price_usd,
            "digest": tool_digest(tool),
            "decided_at": datetime.now(UTC).isoformat(),
            "decided_by": owner,
        },
    }


def price_of(review: dict[str, Any], tool: dict[str, Any]) -> float | None:
    """The most one call to this tool costs, as the owner said when reviewing
    the tool as it stands now. A tool changed since has no price: the owner
    priced something else."""
    decided = review.get(str(tool.get("name") or ""))
    if not isinstance(decided, dict) or decided.get("digest") != tool_digest(tool):
        return None
    price = decided.get("price_usd")
    if isinstance(price, bool) or not isinstance(price, int | float):
        return None
    return float(price) if 0 < price <= MAX_PRICE_USD else None


def callable_tools(tools: list[dict[str, Any]], review: dict[str, Any]) -> list[dict[str, Any]]:
    """The tools an agent may be offered at all.

    Unreviewed, changed and refused tools are not among them. A tool needing
    approval is offered, because proposing it is how the owner is asked.
    """
    return [tool for tool in tools if stance_of(review, tool) in (ALLOWED, NEEDS_APPROVAL)]


def needs_approval(tool: dict[str, Any], review: dict[str, Any]) -> bool:
    """Whether calling this tool must stop for the owner."""
    return stance_of(review, tool) == NEEDS_APPROVAL


def reviewed(tools: list[dict[str, Any]], review: dict[str, Any]) -> list[dict[str, Any]]:
    """Each tool with the owner's stance beside the server's own claims.

    `stance` is what applies; `chosen` is what the owner set, so a tool that
    section 18 holds back says why rather than looking ignored.
    """
    rows = []
    for tool in tools:
        decided = review.get(str(tool.get("name") or ""))
        decided = decided if isinstance(decided, dict) else {}
        acts_as = decided.get("acts_as")
        rows.append(
            {
                **tool,
                "stance": stance_of(review, tool),
                "chosen": decided.get("stance"),
                "acts_as": acts_as if isinstance(acts_as, list) else [],
                "price_usd": price_of(review, tool),
            }
        )
    return rows


def outstanding(tools: list[dict[str, Any]], review: dict[str, Any]) -> int:
    """How many tools are waiting for the owner to look at them."""
    return sum(1 for tool in tools if stance_of(review, tool) in (UNREVIEWED, CHANGED))

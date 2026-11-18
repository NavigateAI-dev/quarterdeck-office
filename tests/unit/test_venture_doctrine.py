"""The same agents, holding a different standard in each business line.

Fintech compliance refuses a vendor page as evidence for an obligation.
Marketing refuses an uplift with no baseline. It is one critic.
"""

from dataclasses import dataclass

from app.agent_routing import apply_venture_doctrine


class Role:
    def __init__(self, role: str, instructions: str) -> None:
        self.role = role
        self.instructions = instructions


@dataclass
class Set:
    planner: Role
    researcher: Role
    strategist: Role
    critic: Role
    chief: Role


def agents() -> Set:
    return Set(
        planner=Role("planner", "PLAN"),
        researcher=Role("researcher", "RESEARCH"),
        strategist=Role("strategist", "STRATEGY"),
        critic=Role("critic", "CRITIQUE"),
        chief=Role("chief", "CHIEF"),
    )


def test_a_role_is_told_the_standard_it_is_held_to() -> None:
    team = agents()
    told = apply_venture_doctrine(
        team,
        "Fintech compliance",
        {"critic": "Refuse a vendor page as evidence for an obligation."},
    )
    assert told == ["critic"]
    assert "Refuse a vendor page" in team.critic.instructions
    assert "How work is judged in Fintech compliance, for your role:" in team.critic.instructions
    # Its own instructions are kept; doctrine is the last layer, not a replacement.
    assert team.critic.instructions.startswith("CRITIQUE")


def test_roles_with_no_standard_are_untouched() -> None:
    team = agents()
    apply_venture_doctrine(team, "Fintech compliance", {"critic": "Refuse vendor pages."})
    assert team.researcher.instructions == "RESEARCH"
    assert team.chief.instructions == "CHIEF"


def test_a_business_line_with_no_doctrine_changes_nothing() -> None:
    team = agents()
    assert apply_venture_doctrine(team, "Anything", {}) == []
    assert team.critic.instructions == "CRITIQUE"


def test_blank_doctrine_is_not_applied() -> None:
    team = agents()
    assert apply_venture_doctrine(team, "Anything", {"critic": "   "}) == []
    assert team.critic.instructions == "CRITIQUE"


def test_several_roles_can_each_have_their_own() -> None:
    team = agents()
    told = apply_venture_doctrine(
        team,
        "Agentic commerce marketing",
        {
            "researcher": "Name the surfaces measured.",
            "critic": "An uplift without a baseline is not a finding.",
        },
    )
    assert set(told) == {"researcher", "critic"}
    assert "surfaces measured" in team.researcher.instructions
    assert "without a baseline" in team.critic.instructions
    assert "surfaces measured" not in team.critic.instructions

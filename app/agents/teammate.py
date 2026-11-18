from typing import Any

from app.agents.base import BaseAgent
from app.schemas.agent_output import Reaction

# What a huddle reply may cost, in output tokens: a few short posts, plus the
# reasoning a model may spend first (DeepSeek Flash was cut off at 700).
HUDDLE_OUTPUT_TOKENS = 2000

PERSPECTIVE = {
    "critic": (
        "You are the critic. Look for what will not survive review: a claim the "
        "evidence does not carry, a secondary source standing in for a primary one, "
        "a figure without a source, two steps that contradict each other."
    ),
    "writer": (
        "You are the writer who will turn this into the deliverable. Say what the "
        "report will need that is not being gathered yet, and what is gathered but "
        "will not be usable as it stands."
    ),
    "strategist": (
        "You are the writer who will turn this into the deliverable. Say what the "
        "proposal will need that is not being gathered yet, and what is gathered but "
        "will not be usable as it stands."
    ),
    "summarist": (
        "You are the writer who will turn this into the deliverable. Say what the "
        "summary will need that is not being gathered yet, and what is gathered but "
        "will not be usable as it stands."
    ),
    "advisor": (
        "You are the advisor. Say whether the course still answers the ask, and what "
        "should change before the next steps run."
    ),
}


class TeammateAgent(BaseAgent[Reaction]):
    """A role speaking up in a huddle after a wave.

    It is the role's own model, budget and record, asked a different question:
    not to do its work, but to react to its teammates' work while it can still
    change the next steps. At most three short posts, or none.
    """

    output_schema = Reaction

    def __init__(self, source: BaseAgent[Any], role: str) -> None:
        super().__init__(source.gateway, source.recorder, 2)
        # The role's own completer: its model, its budget, its policies.
        self.gateway = source.gateway
        self.role = role
        self.model_alias = source.model_alias
        self.provider = source.provider
        self.max_output_tokens = HUDDLE_OUTPUT_TOKENS
        self.instructions = f"""You are one of a team of agents working on a task
together, in one room. A wave of the work has just finished; the next steps
have not started. Return JSON only matching the schema.

{PERSPECTIVE.get(role, PERSPECTIVE["critic"])}

Post at most three things, each one short and specific enough to act on:
- challenge: something in the work so far that is wrong or weak, and why;
- need: something the next steps should gather or settle;
- offer: a step you think should be added, said as what it would do;
- note: anything else a teammate should know before they start.
Address a post to a step (to_step, its id) when it concerns one. When a post
answers one already in the room, give that post's number (reply_to: 3 for #3).
Post nothing
when nothing needs saying: an empty list is a good answer. Do not repeat what
is already in the room."""

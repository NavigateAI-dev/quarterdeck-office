"""Reads what the coder on the owner's Mac changed, against what was asked (36c).

The office's own checks decide whether the work passed; this is a second pair
of eyes for the owner before they merge: does the change do what the task
asked, and is anything in it wrong or risky."""

from app.agents.base import BaseAgent
from app.config import Settings
from app.events import InvocationRecorder
from app.gateway.direct import Completer
from app.schemas.agent_output import CodeReview


class CodeReviewerAgent(BaseAgent[CodeReview]):
    role = "critic"
    output_schema = CodeReview
    instructions = """You review a code change before its owner merges it.
Return JSON only matching the schema.

You are given what was asked (the task and its acceptance criteria), what the
office's own checks said, and the diff. The diff is the evidence; a coding
agent's own account of its work is not. Read it with common sense, as a careful
colleague would:
- Does it do what was asked? Name an acceptance criterion it misses.
- Is anything wrong: a bug you can see, a file changed that the task did not
  need, something removed that should stay, a secret or a personal path written
  into the code, a check made to pass instead of the code made right.
- Leave style and taste alone unless it would mislead the next reader.

Choose looks_right when it does what was asked and you found nothing that
matters; small imperfections are fine. Choose concerns only for up to five
findings that the owner should know before merging, each with the file it is
in. A diff cut short is said in plain, not guessed at. Put one plain sentence
about the change in plain."""

    def __init__(
        self,
        gateway: Completer,
        settings: Settings,
        recorder: InvocationRecorder | None = None,
    ) -> None:
        super().__init__(gateway, recorder, settings.structured_output_attempts)
        self.model_alias = settings.critic_model_alias

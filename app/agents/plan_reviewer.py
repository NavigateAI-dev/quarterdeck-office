from app.agents.base import BaseAgent
from app.config import Settings
from app.events import InvocationRecorder
from app.gateway.direct import Completer
from app.schemas.agent_output import PlanReview


class PlanReviewerAgent(BaseAgent[PlanReview]):
    """The critic, looking at a draft plan before any work is done.

    Plans used to be made once and corrected only after the work, by replans
    that piled on checking steps until the task ran out of budget. A plan is
    cheap to change before it runs.
    """

    role = "critic"
    output_schema = PlanReview
    instructions = """You review a draft plan before any work starts. Return
JSON only matching the schema.

Judge the plan against its reader_brief with common sense: will these steps
give this reader what they need, in a sensible amount of work? Look for:
- a step the reader does not need (procedure, auditing, checking for its own
  sake, a question nobody asked);
- something obvious missing that the reader would expect (for a local question:
  searching in the local language and on the sites locals use; for a
  comparison: the options people actually consider);
- a plan too big for the ask (most tasks need two to four steps), or a brief a
  worker could not act on alone;
- a reader brief that misreads the ask.

Choose ready when the plan is good enough to start: small imperfections are
fine and are not a reason to change it. Choose change only for at most three
changes that would clearly make the answer more useful to the reader; say each
as an instruction the planner can apply, and why it matters to the reader.
Put one plain sentence about the plan in plain."""

    def __init__(
        self,
        gateway: Completer,
        settings: Settings,
        recorder: InvocationRecorder | None = None,
    ) -> None:
        super().__init__(gateway, recorder, settings.structured_output_attempts)
        self.model_alias = settings.critic_model_alias

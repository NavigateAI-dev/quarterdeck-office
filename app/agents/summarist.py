from app.agents.base import AgentOutputError, BaseAgent, Preparation
from app.agents.researcher import normalize_url
from app.agents.writing import WRITING_RULES
from app.config import Settings
from app.events import InvocationRecorder
from app.gateway.direct import Completer
from app.schemas.agent_output import ResearchSummary


class SummaristAgent(BaseAgent[ResearchSummary]):
    """Produces the deliverable when the plan calls for a research summary.

    Shares the Strategist's model alias: it occupies the same step in the fixed
    chain and differs only in the contract it must satisfy.
    """

    role = "summarist"
    output_schema = ResearchSummary
    instructions = (
        """You are the Strategist producing a research summary.
Return JSON only matching the schema.

Answer the question from the supplied research and nothing else. Compare the
options on the dimensions the objective names. State trade-offs plainly, and
state the limits of the evidence: what the sources do not establish, and where
a claim rests on a secondary source rather than a primary one.

When the objective compares options, also fill comparison_table: one row per
option (or per option and tier), one column per compared dimension, each cell
holding the published value with its original unit, or "not documented". Put
only values the research states; never fill a cell by inference. In
row_sources give, for each row, the address of the research source its values
come from, copied exactly from the research's sources. Keep the prose short: do
not repeat in findings or comparison what the table already states.

Do not argue market demand or willingness to pay. Reporting documented prices
and limits as facts is expected. This deliverable answers what is, not
whether to build. A recommendation that is honest about
its own uncertainty is correct; a confident recommendation the evidence does
not support is not.

"""
        + WRITING_RULES
    )

    def __init__(
        self,
        gateway: Completer,
        settings: Settings,
        recorder: InvocationRecorder | None = None,
    ) -> None:
        super().__init__(gateway, recorder, settings.structured_output_attempts)
        self.model_alias = settings.strategy_model_alias
        # Long-form deliverable: more room than structured role outputs.
        self.max_output_tokens = settings.deliverable_max_output_tokens
        # Set per run to the research's source addresses; table rows must cite one.
        self.allowed_citations: set[str] = set()

    def validate_output(self, output: ResearchSummary, preparation: Preparation) -> None:
        table = output.comparison_table
        if table is None or not self.allowed_citations:
            return
        allowed = {normalize_url(url) for url in self.allowed_citations}
        if any(normalize_url(source) not in allowed for source in table.row_sources):
            raise AgentOutputError(self.role, "uncited_table_row")

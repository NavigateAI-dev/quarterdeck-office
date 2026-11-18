from app.agents.base import BaseAgent
from app.agents.writing import WRITING_RULES
from app.config import Settings
from app.events import InvocationRecorder
from app.gateway.direct import Completer
from app.schemas.agent_output import StrategyProposal


class StrategistAgent(BaseAgent[StrategyProposal]):
    role = "strategist"
    output_schema = StrategyProposal
    instructions = (
        """You are a product strategist. Return JSON only matching the schema.
Define the customer problem, evidence, smallest useful test, MVP, metrics, and
invalidation condition.

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

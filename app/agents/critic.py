from app.agents.base import BaseAgent, Preparation
from app.agents.tool_round import gather
from app.config import Settings
from app.context import ProjectContext
from app.events import InvocationRecorder
from app.gateway.direct import Completer
from app.mcp.calling import McpTools
from app.schemas.agent_output import Critique

# Checks the critic may make with the owner's tools, per critique.
CHECK_CALLS = 2


class CriticAgent(BaseAgent[Critique]):
    role = "critic"
    output_schema = Critique
    instructions = """You are the editor who reads the work before it goes to
its reader. Return JSON only matching the schema.

Read it as the reader in the plan's reader_brief would, with common sense.
Ask first: does this give them what they need for their purpose? Can they act
on it? Is it right? That is the whole job; everything else is secondary.

Judge against the reader brief and the plan's task_kind, not against an ideal
or a procedure. A research_summary must not be required to argue demand,
pricing or willingness to pay: those belong to a product_proposal only. Do not
ask for audits, measurement methods, access dates, administrative details,
source formatting or completeness proofs the reader does not need. A few good,
correct options beat an exhaustive search. Missing details the reader can ask
for themselves (a price not published, a timetable) are fine when the answer
says so.

Check that it is right first: a factual error, two sources or places mixed
up, a contradiction, or a conclusion the evidence does not carry is always an
issue, and comes before anything missing. Then check that it serves the
purpose. Ask only for what the evidence can give: never demand a number of
options or a certainty the search cannot reach.

issues: at most three, most important first, only those that would change
what the reader can do or whether the answer is right. For each: the issue in
plain words, why it matters to this reader, and the fix. Small things that
would not change the reader's decision are not issues: leave them out.

writing_fixes: at most five changes to how the answer is written, so the reader
understands it at once: plainer words, the answer first, less hedging, a table
instead of prose. These need no new research and are applied when the answer is
written.

for_owner: one or two plain sentences for the person who asked: what is good,
what is missing, and whether it is fine to use. No jargon.

Choose exactly one verdict:

PASS when the work serves the reader's purpose and its conclusion follows from
the evidence. Honest limits are fine. Writing problems alone are a PASS with
writing_fixes, never a revision.

REVISE only when the main purpose is not met, or something important is wrong,
and a specific piece of work would fix it. Set revision_target to research when
evidence is missing that a search can find, or strategy when the evidence
supports a different answer than the one drafted. Never revise for evidence
the available tools cannot obtain: say so in missing_evidence.

BLOCK when proceeding would be harmful or the premise is invalid.

Leave weak_assumptions, alternative_explanations, required_corrections and
what_would_change_conclusion empty unless one item there truly matters to the
reader; issues carry what matters."""

    def __init__(
        self,
        gateway: Completer,
        settings: Settings,
        recorder: InvocationRecorder | None = None,
    ) -> None:
        super().__init__(gateway, recorder, settings.structured_output_attempts)
        self.model_alias = settings.critic_model_alias
        # Tools from servers the owner granted this project, set per run.
        self.mcp: McpTools | None = None

    async def prepare(
        self, objective: str, context: ProjectContext, focus: str = ""
    ) -> Preparation:
        """Look a claim up before judging it, when the owner granted a way to.

        A checker, not an actor: only tools the owner allowed outright are
        offered. One that needs approval is how the owner marks a tool that
        changes the world, and a critique is no reason to pause the task.
        """
        checks = await gather(
            self,
            self.mcp,
            objective,
            context,
            purpose="to check the research's claims before you critique it",
            limit=CHECK_CALLS,
            allowed_only=True,
        )
        if not checks:
            return Preparation()
        return Preparation(
            prompt_section=(
                "\n\nChecks you made with the owner's tools. Weigh them as evidence you "
                "gathered yourself. When a check contradicts the research, name it in "
                "critical_issues; when it confirms a claim, do not ask for that claim to be "
                "sourced again:\n"
                + "\n\n".join(f"[{check.label}]\n{check.text}" for check in checks)
            )
        )

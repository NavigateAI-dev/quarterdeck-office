"""Semantic judgments that stand in for fragile heuristics (TypeSafe Jev).

Code owns the workflow and the policy; these return calibrated probabilities
for narrow questions ordinary code answers badly:

- whether an earlier review concern is still raised, however it is reworded
  (replacing bag-of-words overlap for loop convergence);
- whether an acceptance criterion can be met from public web pages at all.

Each item is its own request with one question, run concurrently, because the
items are independent and their state differs. Any service failure raises
JudgmentUnavailable with a code; callers decide the fallback.
"""

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score, TypeSafeError

if TYPE_CHECKING:
    from app.config import Settings

MODEL = "jev-latest"

STILL_RAISED = Noul(
    instructions=(
        "`earlier_concern` was raised by a reviewer about a previous draft. Decide "
        "whether `new_review` still raises the same underlying problem as unresolved, "
        "even if it is worded differently."
    ),
    criteria={
        "true": "The new review raises the same underlying problem again, so it was not resolved.",
        "false": "The new review does not raise this problem, or treats it as resolved.",
    },
)

REACHABLE = Noul(
    instructions=(
        "Decide whether `criterion` can be satisfied using only the evidence listed in "
        "`available_evidence`: public web pages found by an ordinary web search, and any "
        "of the owner's local document collections named there."
    ),
    criteria={
        "true": (
            "Public pages usually state this directly, such as published prices, "
            "documented limits or feature lists."
        ),
        "false": (
            "It needs private or internal data, account access, or information that "
            "pages rarely publish."
        ),
    },
)


REVISION_TARGET = Choice(
    # Judged from the note alone: with the critique in view the model inferred a
    # direction the owner never gave ("make it better" became research).
    instructions=(
        "The owner wrote `note` to send an analysis back for revision. Decide what the "
        "note itself asks to change. Judge only the note's own words; do not infer a "
        "direction the note does not state."
    ),
    criteria={
        "research": (
            "The note explicitly asks for different, more or better evidence, sources, "
            "data or facts."
        ),
        "strategy": (
            "The note explicitly asks to change the interpretation, recommendation, "
            "framing, audience, tone or structure."
        ),
        "unclear": (
            "The note gives no specific direction, e.g. general dissatisfaction such as "
            "'make it better', 'not good enough', 'try again' or 'improve this'."
        ),
    },
)

SOURCE_TYPE = Choice(
    instructions=(
        "Classify `source` using its title, address and excerpt. The distinction is the "
        "same one the researcher is instructed to apply."
    ),
    criteria={
        "primary": (
            "The subject's own record: its official documentation, repository, release "
            "notes, licence, specification or published price list."
        ),
        "secondary": (
            "Commentary, comparison or summary, including news, reviews, forums, "
            "aggregators, and vendor blog posts about their own product."
        ),
    },
)

SUPPORTED = Noul(
    instructions=(
        "Decide whether `claim` is supported by `evidence`, meaning the evidence states it "
        "or directly implies it. Do not use outside knowledge."
    ),
    criteria={
        "true": "The evidence states or directly implies the claim.",
        "false": "The evidence does not support the claim, contradicts it, or does not address it.",
    },
)

INTAKE_ACTION = Choice(
    instructions=(
        "`request` was sent to a private AI office by its owner. `recent`, when given, is "
        "the conversation just before it: read the request in its light. Decide what they "
        "want now."
    ),
    criteria={
        "start_task": (
            "Have the office's team take on new work: research across sources, analysis, "
            "a document, a dashboard, or building something."
        ),
        # A look at one page is not a task (the Buienradar case).
        "lookup": (
            "A quick fact or a look at something live, answered now in the conversation: "
            "the weather or rain radar, a price, opening hours, what one page says."
        ),
        # "tell me if it rains before 15:00" is a watch, offered with Start.
        "watch": (
            "Be told later when something changes or happens: keep an eye on a page, a "
            "price, the weather or a topic, and tell them."
        ),
        "office_status": "Know whether the office and its services are working.",
        "pending_approvals": "See what is waiting for their decision or approval.",
        "none": (
            "Anything else: talk, a remark about the conversation itself (such as having "
            "shared a picture), approving, rejecting or changing settings."
        ),
    },
)


# What a message in a discussion commits to. The office acts on the
# answer: talk is answered, a note is filed, a hand-off starts work.
COMMITMENT = Choice(
    instructions=(
        "`message` was said by the owner of a private AI office in a discussion with "
        "their agents; `recent` is what was said just before it. Decide what the "
        "message asks the office to do now."
    ),
    criteria={
        "talk": (
            "Thinking aloud, an opinion, a greeting, an answer or a thank-you, a question "
            "the agents can answer from what they already know (any news? what do you "
            "think? how is it going?), or a tentative suggestion that something might be "
            "worth doing (maybe we could check..., this might be worth researching) "
            "without asking for it now. Nothing should be started yet."
        ),
        "note": "Asks to note or remember something for later, without acting on it now.",
        "look_into": (
            "Clearly asks the office to go and find out, check, research or compare "
            "something it does not already know, now, as an instruction rather than a "
            "possibility or a question to the agents."
        ),
        "do": (
            "Asks the office to produce or carry out a piece of work now: write, draft, "
            "prepare, plan, or go ahead with what was discussed."
        ),
        "revise": "Asks to change, fix or redo work already delivered in this discussion.",
        "stop": "Asks to stop, pause or hold off work that is under way.",
        "later": "Asks to come back to this at a later time.",
        "decide": (
            "States a decision the office should follow from now on, said as settled "
            "(we will..., from now on..., we go with...), not as a possibility."
        ),
    },
)


RELEVANCE = Score(
    instructions="Rate how useful `passage` is for answering `question`.",
    criteria=[
        "Unrelated to the question.",
        "Mentions the topic but does not help answer it.",
        "Partly answers the question.",
        "Directly answers the question.",
    ],
)


LINK_RELEVANT = Noul(
    instructions=(
        "`link` is a link on an official page. Decide whether the page it leads to is "
        "likely to contain information that answers `question`, judging from its text "
        "and address."
    ),
    criteria={
        "true": "The linked page is likely to answer part of the question directly.",
        "false": "The linked page is unlikely to help, such as navigation, blog or legal pages.",
    },
)


class JudgmentUnavailable(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def business_line_question(lines: list[tuple[str, str]]) -> Choice:
    """Which business line a stream of work belongs to.

    Built at the moment of asking, because the options are the owner's own
    business lines rather than anything the office knows in advance. "none" is
    always offered: a project that belongs to no line is a real answer, and
    forcing a choice would put the wrong doctrine in front of the agents.
    """
    criteria = {name: text for name, text in lines}
    criteria["none"] = (
        "The work does not belong to any of the business lines above, or there is too "
        "little to tell which."
    )
    return Choice(
        instructions=(
            "`project` is one stream of work in a private office: its name, what the "
            "owner wrote about it, and what its recent tasks asked for. Decide which "
            "business line it belongs to, judging from the work itself."
        ),
        criteria=criteria,
    )


class Judge(Protocol):
    async def unresolved(self, earlier: list[str], review: dict[str, Any]) -> list[float]: ...

    async def reachability(
        self, criteria: list[str], local_sources: list[str] | None = None
    ) -> list[float]: ...

    async def supported(self, claims: list[str], evidence: dict[str, Any]) -> list[float]: ...

    async def source_types(self, sources: list[dict[str, str]]) -> list[tuple[str, float]]: ...

    async def reusable(self, lessons: list[str], project: str) -> list[float]: ...

    async def business_lines(
        self, projects: list[dict[str, Any]], lines: list[tuple[str, str]]
    ) -> list[tuple[str, float]]: ...

    async def durable(self, statements: list[tuple[str, str]]) -> list[float]: ...


class TypeSafeJudge:
    def __init__(
        self, api_key: str, timeout_seconds: float = 15.0, http_client: Any = None
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client

    def _client(self) -> AsyncTypeSafeClient:
        return AsyncTypeSafeClient(
            api_key=self.api_key,
            model=MODEL,
            timeout=self.timeout_seconds,
            http_client=self.http_client,
        )

    async def _choices(
        self, states: list[Any], key: str, question: Choice
    ) -> list[tuple[str, float]]:
        if not states:
            return []
        try:
            async with self._client() as client:
                responses = await asyncio.gather(
                    *(client.system_one(state=state, questions={key: question}) for state in states)
                )
        except TypeSafeError:
            raise JudgmentUnavailable("typesafe_unavailable") from None
        answers = [response.answers[key] for response in responses]
        return [(str(a.choice), float(a.confidence)) for a in answers]  # type: ignore[union-attr]

    async def _nouls(self, states: list[dict[str, Any]], key: str, question: Noul) -> list[float]:
        if not states:
            return []
        try:
            async with AsyncTypeSafeClient(
                api_key=self.api_key,
                model=MODEL,
                timeout=self.timeout_seconds,
                http_client=self.http_client,
            ) as client:
                responses = await asyncio.gather(
                    *(client.system_one(state=state, questions={key: question}) for state in states)
                )
        except TypeSafeError:
            raise JudgmentUnavailable("typesafe_unavailable") from None
        return [float(response.answers[key].noul) for response in responses]  # type: ignore[union-attr]

    async def model_route(
        self, task: str, instructions: str, criteria: dict[str, str]
    ) -> tuple[str, float]:
        """Which model tier a task needs (`app.model_router`), and how sure Jev is."""
        [answer] = await self._choices(
            [{"request": task}],
            "model_route",
            Choice(instructions=instructions, criteria=criteria),
        )
        return answer

    async def unresolved(self, earlier: list[str], review: dict[str, Any]) -> list[float]:
        return await self._nouls(
            [{"earlier_concern": item, "new_review": review} for item in earlier],
            "still_raised",
            STILL_RAISED,
        )

    async def reachability(
        self, criteria: list[str], local_sources: list[str] | None = None
    ) -> list[float]:
        available = ["public web search", *[f"owner's documents: {s}" for s in local_sources or []]]
        return await self._nouls(
            [{"criterion": c, "available_evidence": available} for c in criteria],
            "reachable",
            REACHABLE,
        )

    async def supported(self, claims: list[str], evidence: dict[str, Any]) -> list[float]:
        return await self._nouls(
            [{"claim": claim, "evidence": evidence} for claim in claims], "supported", SUPPORTED
        )

    async def durable(self, statements: list[tuple[str, str]]) -> list[float]:
        """How durable each statement about a subject looks. Ranking only."""
        states = [{"subject": subject, "statement": text} for subject, text in statements]
        return await self._nouls(states, "durable", DURABLE)

    async def business_lines(
        self, projects: list[dict[str, Any]], lines: list[tuple[str, str]]
    ) -> list[tuple[str, float]]:
        """Which line each project belongs to, with how sure the judgement is.

        A suggestion. What it does with the answer is decided above: a
        confident one is applied, an unsure one is put to the owner.
        """
        if not lines:
            return []
        question = business_line_question(lines)
        states = [{"project": project} for project in projects]
        return await self._choices(states, "business_line", question)

    async def reusable(self, lessons: list[str], project: str) -> list[float]:
        """How likely each lesson is to help a later task in the same project.

        Ranking only: a probability never approves a lesson or changes what an
        agent is told. The owner approves.
        """
        states = [{"project": project, "lesson": lesson} for lesson in lessons]
        return await self._nouls(states, "reusable", REUSABLE)

    async def source_types(self, sources: list[dict[str, str]]) -> list[tuple[str, float]]:
        return await self._choices(
            [{"source": source} for source in sources], "source_type", SOURCE_TYPE
        )

    async def commitment(self, text: str, recent: list[str]) -> tuple[str, float]:
        [answer] = await self._choices(
            [{"message": text, "recent": recent[-8:]}], "commitment", COMMITMENT
        )
        return answer

    async def revision_target(self, note: str) -> tuple[str, float]:
        [answer] = await self._choices([{"note": note}], "target", REVISION_TARGET)
        return answer

    async def route(
        self,
        text: str,
        projects: list[str] | dict[str, str],
        recent: list[dict[str, str]] | None = None,
    ) -> dict[str, tuple[str, float]]:
        """Pick a handler and, speculatively, the project a new task belongs to.

        Both questions read the same request, so they run in one call; the
        project answer is used only when the action is start_task. Each project
        may come with what it is about: a name alone says little.
        """
        described = projects if isinstance(projects, dict) else dict.fromkeys(projects, "")
        project = Choice(
            instructions=(
                "If `request` asks for new work, decide which of the owner's projects it "
                "belongs to: the one whose work it continues or relates to most closely. "
                "Choose none only when no project is about anything related."
            ),
            criteria={
                **{name: (about or None) for name, about in described.items()},
                "none": "No listed project is about anything related.",
            },
        )
        try:
            async with self._client() as client:
                state: dict[str, Any] = {"request": text}
                if recent:
                    state["recent"] = [
                        f"{turn.get('role')}: {str(turn.get('content') or '')[:400]}"
                        for turn in recent[-6:]
                    ]
                response = await client.system_one(
                    state=state, questions={"action": INTAKE_ACTION, "project": project}
                )
        except TypeSafeError:
            raise JudgmentUnavailable("typesafe_unavailable") from None
        return {
            key: (str(answer.choice), float(answer.confidence))  # type: ignore[union-attr]
            for key, answer in response.answers.items()
        }

    async def relevance(self, question: str, passages: list[str]) -> list[float]:
        """Score (0 unrelated .. 3 directly answers) per passage, concurrently."""
        if not passages:
            return []
        try:
            async with self._client() as client:
                responses = await asyncio.gather(
                    *(
                        client.system_one(
                            state={"question": question, "passage": passage},
                            questions={"relevance": RELEVANCE},
                        )
                        for passage in passages
                    )
                )
        except TypeSafeError:
            raise JudgmentUnavailable("typesafe_unavailable") from None
        return [float(r.answers["relevance"].score) for r in responses]  # type: ignore[union-attr]

    async def link_relevance(self, question: str, links: list[dict[str, str]]) -> list[float]:
        return await self._nouls(
            [{"question": question, "link": link} for link in links], "relevant", LINK_RELEVANT
        )


REUSABLE = Noul(
    instructions=(
        "`lesson` was learned by an agent while working on a task in `project`. Decide "
        "whether a different task in the same project would be better off knowing it."
    ),
    criteria={
        "true": (
            "A later task would act differently, or save work, because of it: a source that "
            "cannot be read, an address that is wrong, a limit that was too low, a question "
            "public sources cannot answer."
        ),
        "false": (
            "It is specific to that one task, already obvious, or says nothing a later task "
            "could act on."
        ),
    },
)


DURABLE = Noul(
    instructions=(
        "`statement` was said by a finished piece of work about `subject`. Decide whether "
        "it is a durable fact about that subject, worth telling a later task."
    ),
    criteria={
        "true": (
            "It states something about the subject that stays true beyond this one task: "
            "what it is, where it is found, what it requires, how it behaves."
        ),
        "false": (
            "It is about this task rather than the subject, is a judgement or a "
            "recommendation, is tied to a moment that has passed, or says nothing a "
            "later task could use."
        ),
    },
)


def build_judge(settings: "Settings") -> "TypeSafeJudge | None":
    """Semantic judgments when a TypeSafe key is configured; otherwise none."""
    if settings.typesafe_api_key is None:
        return None
    return TypeSafeJudge(settings.typesafe_api_key.get_secret_value())

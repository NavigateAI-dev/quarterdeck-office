"""Semantic judgments (TypeSafe) in the analysis loop, with a fake and mocked HTTP."""

import json
from typing import Any

import httpx2
import pytest

from app.judgments import JudgmentUnavailable, TypeSafeJudge
from app.orchestrator.graph import run_analysis
from app.orchestrator.pipeline import build_graph
from app.orchestrator.routing import NOT_CONVERGING, UNREACHABLE
from app.orchestrator.state import RunStatus
from tests.unit.test_graph import PLAN, agents, critique, start

CONCERNS = ["Pricing page not cited", "No rate-limit documentation", "Tiers incomplete"]


def revise(*issues: str) -> dict[str, Any]:
    return critique("REVISE", "research") | {"critical_issues": list(issues)}


class FakeJudge:
    def __init__(
        self,
        still: list[float] | None = None,
        reachable: list[float] | None = None,
        fail: bool = False,
    ) -> None:
        self.still = still or []
        self.reachable = reachable
        self.fail = fail
        self.calls: list[str] = []

    async def unresolved(self, earlier: list[str], review: dict[str, Any]) -> list[float]:
        self.calls.append("unresolved")
        if self.fail:
            raise JudgmentUnavailable("typesafe_unavailable")
        return self.still[: len(earlier)]

    async def reachability(
        self, criteria: list[str], local_sources: list[str] | None = None
    ) -> list[float]:
        self.calls.append("reachability")
        if self.fail:
            raise JudgmentUnavailable("typesafe_unavailable")
        return self.reachable if self.reachable is not None else [0.9] * len(criteria)

    supported_p: list[float] | None = None
    sources: list[tuple[str, float]] | None = None

    async def supported(self, claims: list[str], evidence: dict[str, Any]) -> list[float]:
        self.calls.append("supported")
        if self.fail:
            raise JudgmentUnavailable("typesafe_unavailable")
        return self.supported_p if self.supported_p is not None else [0.9] * len(claims)

    async def source_types(self, sources: list[dict[str, str]]) -> list[tuple[str, float]]:
        self.calls.append("source_types")
        if self.fail:
            raise JudgmentUnavailable("typesafe_unavailable")
        return self.sources if self.sources is not None else [("primary", 0.9)] * len(sources)


async def run(judge: FakeJudge, critiques: list[dict[str, Any]], plan: Any = None) -> Any:
    set_, roles = agents(critiques)
    if plan is not None:
        roles["planner"].outputs = [plan]
    graph = build_graph(set_, 3, judge=judge)
    return await run_analysis(graph, start()), roles


async def test_reworded_but_unresolved_concerns_stop_the_loop() -> None:
    """The lexical overlap here is zero; the judgment sees the same problems."""
    first = revise(*CONCERNS)
    second = revise("Brave's price list is never referenced", "Throughput caps undocumented")
    state, _ = await run(FakeJudge(still=[0.9, 0.8, 0.2]), [first, second])
    assert state["stopped_reason"] == NOT_CONVERGING
    assert state["status"] == RunStatus.NEEDS_HUMAN_REVIEW
    assert state["convergence"]["method"] == "typesafe"
    assert state["convergence"]["still_raised"] == 2


async def test_resolved_concerns_let_the_loop_continue() -> None:
    first = revise(*CONCERNS)
    state, roles = await run(FakeJudge(still=[0.1, 0.2, 0.1]), [first, critique("PASS")])
    assert state["status"] != RunStatus.NEEDS_HUMAN_REVIEW or state.get("stopped_reason") is None
    assert state.get("stopped_reason") != NOT_CONVERGING


async def test_an_unavailable_judge_falls_back_to_the_lexical_check() -> None:
    first = revise(*CONCERNS)
    state, _ = await run(FakeJudge(fail=True), [first, revise(*CONCERNS)])
    assert state["stopped_reason"] == NOT_CONVERGING
    assert state["convergence"]["method"] == "lexical_fallback"


async def test_unreachable_criteria_are_told_to_the_critic() -> None:
    plan = PLAN | {"acceptance_criteria": ["Lists published prices", "Quotes our internal ledger"]}
    state, roles = await run(FakeJudge(reachable=[0.9, 0.05]), [critique("PASS")], plan)
    assert state["unreachable_criteria"] == ["Quotes our internal ledger"]
    critic_input = roles["critic"].inputs[0]
    assert "Quotes our internal ledger" in critic_input
    assert "do not request revision" in critic_input.lower()


async def test_a_plan_whose_criteria_all_seem_unreachable_still_researches() -> None:
    """E1: the judgment tells the critic what not to demand; it stops nothing."""
    plan = PLAN | {"acceptance_criteria": ["Quotes our internal ledger"]}
    state, roles = await run(FakeJudge(reachable=[0.02]), [critique("PASS")], plan)
    assert state["stopped_reason"] != UNREACHABLE
    assert len(roles["researcher"].calls) == 1
    assert state["unreachable_criteria"] == ["Quotes our internal ledger"]


async def test_without_a_judge_the_graph_behaves_as_before() -> None:
    set_, _ = agents([critique("PASS")])
    state = await run_analysis(build_graph(set_, 3), start())
    assert state["unreachable_criteria"] == []


def typesafe(handler: Any) -> TypeSafeJudge:
    return TypeSafeJudge(
        "ts-test-key", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )


async def test_the_adapter_sends_one_noul_per_item_and_reads_probabilities() -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: Any) -> Any:
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers["authorization"] == "Bearer ts-test-key"
        noul = 0.9 if "prices" in json.dumps(body["state"]) else 0.1
        return httpx2.Response(
            200,
            json={
                "model": "jev-latest",
                "answers": {"reachable": {"type": "noul", "noul": noul}},
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        )

    result = await typesafe(handle).reachability(["Lists published prices", "Quotes a ledger"])
    assert result == [0.9, 0.1]
    assert {body["model"] for body in seen} == {"jev-latest"}
    assert all(body["questions"]["reachable"]["type"] == "noul" for body in seen)


async def test_service_errors_become_unavailable_without_leaking_the_key() -> None:
    def handle(request: Any) -> Any:
        return httpx2.Response(401, json={"error": {"message": "bad key ts-test-key"}})

    with pytest.raises(JudgmentUnavailable) as caught:
        await typesafe(handle).reachability(["x"])
    assert "ts-test-key" not in str(caught.value)


def test_the_worker_uses_typesafe_only_when_a_key_is_configured() -> None:
    from app.config import Settings
    from app.judgments import build_judge

    base = {
        "database_url": "postgresql+psycopg://office:secret@localhost/office",
    }
    assert build_judge(Settings(_env_file=None, **base)) is None  # type: ignore[call-arg,arg-type]
    configured = Settings(_env_file=None, typesafe_api_key="ts-key", **base)  # type: ignore[call-arg,arg-type]
    assert isinstance(build_judge(configured), TypeSafeJudge)


async def test_unsupported_synthesis_claims_are_bound_into_the_approval() -> None:
    from tests.unit.test_approval_graph import Gate

    judge = FakeJudge()
    judge.supported_p = [0.1]
    set_, _ = agents([critique("PASS")])

    class Recording(Gate):
        synthesis: dict[str, Any] = {}

        def open(self, objective: str, synthesis: dict[str, Any]) -> tuple[str, str]:
            Recording.synthesis = synthesis
            return super().open(objective, synthesis)

    gate = Recording(None)
    await run_analysis(build_graph(set_, 3, gate=gate, judge=judge), start())
    assert Recording.synthesis["unverified_claims"] == ["f"]


def test_unverified_claims_are_part_of_what_is_approved() -> None:
    from uuid import uuid4

    from app.approvals import build_brief, build_scope

    scope = build_scope(
        uuid4(),
        uuid4(),
        "o",
        {"recommendation": "r", "proposed_engineering_scope": "s", "unverified_claims": ["c"]},
        [],
    )
    assert scope.unverified_claims == ["c"]
    assert build_brief(scope, None, None).unverified_claims == ["c"]
    assert scope.digest() != scope.model_copy(update={"unverified_claims": []}).digest()


async def test_a_failed_claim_check_never_blocks_approval() -> None:
    from tests.unit.test_approval_graph import Gate

    set_, _ = agents([critique("PASS")])
    from app.orchestrator.engineering import DisabledEngineering

    graph = build_graph(
        set_, 3, gate=Gate(None), executor=DisabledEngineering(), judge=FakeJudge(fail=True)
    )
    state = await run_analysis(graph, start())
    assert state["status"] == RunStatus.WAITING_FOR_APPROVAL
    assert state["claim_check"] == "unavailable"


PRICING = "https://brave.com/search/api/"
BLOG = "https://blog.example/brave"


async def test_a_confident_source_misclassification_is_corrected_and_recorded() -> None:
    from app.agent_checks import check_sources

    research = {
        "sources": [
            {
                "title": "Brave pricing",
                "url": "https://brave.com/search/api/",
                "source_type": "secondary",
            },
            {
                "title": "Blog review",
                "url": "https://blog.example/brave",
                "source_type": "secondary",
            },
        ]
    }
    snippets = {
        "https://brave.com/search/api/": "Plans and pricing",
        "https://blog.example/brave": "A review",
    }
    judge = FakeJudge()
    judge.sources = [("primary", 0.92), ("primary", 0.55)]
    corrected, checks = await check_sources(judge, research, snippets)
    assert corrected["sources"][0]["source_type"] == "primary"
    assert corrected["sources"][1]["source_type"] == "secondary"  # not confident enough
    assert checks == [
        {
            "url": "https://brave.com/search/api/",
            "declared": "secondary",
            "judged": "primary",
            "confidence": 0.92,
            "corrected": True,
        }
    ]


async def test_a_failed_source_check_leaves_the_research_unchanged() -> None:
    from app.agent_checks import check_sources

    research = {"sources": [{"title": "t", "url": "https://x.example", "source_type": "primary"}]}
    corrected, checks = await check_sources(FakeJudge(fail=True), research, {})
    assert corrected == research
    assert checks == []


def choice_response(key: str, choice: str, confidence: float) -> Any:
    return httpx2.Response(
        200,
        json={
            "model": "jev-latest",
            "answers": {
                key: {
                    "type": "choice",
                    "choice": choice,
                    "confidence": confidence,
                    "probabilities": {choice: confidence},
                }
            },
            "usage": {"input_tokens": 10, "output_tokens": 1},
        },
    )


async def test_the_adapter_reads_choice_answers() -> None:
    def handle(request: Any) -> Any:
        body = json.loads(request.content)
        assert set(body["questions"]["target"]["criteria"]) == {"research", "strategy", "unclear"}
        return choice_response("target", "strategy", 0.83)

    choice, confidence = await typesafe(handle).revision_target("Reframe the ICP")
    assert (choice, confidence) == ("strategy", 0.83)


async def test_the_researcher_excerpt_reaches_the_source_judge() -> None:
    from app.agent_checks import check_sources
    from app.agents.researcher import normalize_url

    seen: list[list[dict[str, str]]] = []

    class Capturing(FakeJudge):
        async def source_types(self, sources: list[dict[str, str]]) -> list[tuple[str, float]]:
            seen.append(sources)
            return [("primary", 0.9)]

    url = "https://Docs.Example.com/Pricing/"
    research = {"sources": [{"title": "t", "url": url, "source_type": "primary"}]}
    await check_sources(Capturing(), research, {normalize_url(url): "Official price list"})
    assert seen[0][0]["excerpt"] == "Official price list"


async def test_cited_local_passages_are_recorded_with_provenance() -> None:
    from tests.unit.test_graph import RESEARCH

    local = "local://docs/Research/q3.md#chunk-0"
    set_, roles = agents([critique("PASS")])
    researcher = roles["researcher"]
    researcher.outputs = [
        RESEARCH | {"sources": [{"title": "Q3", "url": local, "source_type": "primary"}]}
    ]
    researcher.last_local = {  # type: ignore[attr-defined]
        local: {"citation": local, "path": "q3.md", "sha256": "b" * 64, "chunk": 0},
        "local://docs/Research/unused.md#chunk-0": {"path": "unused.md"},
    }
    judge = FakeJudge()
    state = await run_analysis(build_graph(set_, 3, judge=judge), start())
    assert "source_types" not in judge.calls
    assert state["local_evidence"] == [
        {"citation": local, "path": "q3.md", "sha256": "b" * 64, "chunk": 0}
    ]


async def test_the_adapter_asks_the_model_route_with_the_offered_tiers() -> None:
    sent: list[dict[str, Any]] = []

    def handle(request: Any) -> Any:
        sent.append(json.loads(request.content))
        return choice_response("model_route", "fast", 0.88)

    answer = await typesafe(handle).model_route(
        "What is the VAT rate?", "Pick the cheapest.", {"fast": "lookups", "performance": "hard"}
    )

    assert answer == ("fast", 0.88)
    question = sent[0]["questions"]["model_route"]
    assert set(question["criteria"]) == {"fast", "performance"}
    assert sent[0]["state"] == {"request": "What is the VAT rate?"}

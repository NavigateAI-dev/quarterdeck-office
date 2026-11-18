import json
from typing import Any
from uuid import uuid4

import pytest

from app.agents import AgentOutputError, ChiefAgent, CriticAgent, ResearcherAgent, StrategistAgent
from app.config import Settings
from app.context import ProjectContext
from app.events import InvocationRecorder
from app.gateway.client import Completion, GatewayError, Usage
from app.tools.web import SearchError, SearchResult, StaticSearchTool


class Gateway:
    def __init__(self, content: str = "", error: GatewayError | None = None) -> None:
        self.content = content
        self.error = error
        self.aliases: list[str] = []
        self.prompts: list[str] = []

    async def complete(
        self,
        alias: str,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        schema: Any = None,
    ) -> Completion:
        self.aliases.append(alias)
        self.prompts.append(messages[-1]["content"])
        if self.error is not None:
            raise self.error
        return Completion(
            model_alias=alias,
            model="fixture",
            content=self.content,
            usage=Usage(prompt_tokens=11, completion_tokens=7),
            latency_seconds=0.5,
        )


class FailingSearch:
    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        raise SearchError("search_timeout")


def config() -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://x:y@localhost/x",
    )


def context(name: str = "Harbor AI") -> ProjectContext:
    return ProjectContext(
        organization="Northwind",
        project_id=uuid4(),
        project_name=name,
        background=f"{name} background",
        constraints=["No customer data leaves the environment"],
    )


def chief_payload() -> dict[str, object]:
    return {"facts": ["f"], "recommendation": "r", "proposed_engineering_scope": "s"}


def research_payload(url: str = "https://example.com", source_type: str = "primary") -> dict:
    return {
        "research_question": "q",
        "key_findings": ["f"],
        "evidence": ["e"],
        "implications": ["i"],
        "sources": [{"title": "t", "url": url, "source_type": source_type}],
        "confidence": "high",
    }


def search_tool(source_type: str = "primary") -> StaticSearchTool:
    return StaticSearchTool(
        [SearchResult("t", "https://example.com", "snippet", source_type=source_type)]
    )


def researcher(gateway: Gateway, recorder: InvocationRecorder | None = None) -> ResearcherAgent:
    return ResearcherAgent(gateway, config(), search_tool(), recorder)


@pytest.mark.asyncio
async def test_role_alias_and_output_contract() -> None:
    gateway = Gateway(json.dumps(chief_payload()))
    result = await ChiefAgent(gateway, config()).run("objective", context())
    assert result.recommendation == "r"
    assert gateway.aliases == ["0001"]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent,alias", [(StrategistAgent, "0003"), (CriticAgent, "0004")])
async def test_agents_use_configured_aliases(agent: type, alias: str) -> None:
    if agent is StrategistAgent:
        fields = [
            "problem",
            "who_has_it",
            "frequency_severity",
            "current_behavior",
            "willingness_to_change",
            "smallest_useful_test",
            "invalidation_condition",
            "icp",
            "jtbd",
        ]
        body: dict[str, object] = {k: "x" for k in fields} | {
            "supporting_evidence": ["e"],
            "mvp_scope": ["m"],
            "success_metrics": ["s"],
        }
    else:
        body = {"verdict": "PASS", "confidence": "high"}
    gateway = Gateway(json.dumps(body))
    await agent(gateway, config()).run("objective", context())
    assert gateway.aliases == [alias]


@pytest.mark.asyncio
async def test_researcher_uses_alias_and_supplied_sources() -> None:
    gateway = Gateway(json.dumps(research_payload()))
    report = await researcher(gateway).run("objective", context())
    assert gateway.aliases == ["0002"]
    assert "https://example.com" in gateway.prompts[0]
    assert str(report.sources[0].url).startswith("https://example.com")


@pytest.mark.asyncio
async def test_malformed_output_does_not_advance() -> None:
    with pytest.raises(AgentOutputError, match="malformed_structured_output"):
        await ChiefAgent(Gateway("not json"), config()).run("objective", context())


@pytest.mark.asyncio
async def test_unverified_source_is_rejected() -> None:
    gateway = Gateway(json.dumps(research_payload(url="https://fabricated.example")))
    with pytest.raises(AgentOutputError, match="unverified_source"):
        await researcher(gateway).run("objective", context())


@pytest.mark.asyncio
async def test_misreported_source_type_is_rejected() -> None:
    gateway = Gateway(json.dumps(research_payload(source_type="primary")))
    agent = ResearcherAgent(gateway, config(), search_tool(source_type="secondary"))
    with pytest.raises(AgentOutputError, match="misreported_source_type"):
        await agent.run("objective", context())


@pytest.mark.asyncio
async def test_tool_failure_prevents_model_invocation() -> None:
    gateway = Gateway(json.dumps(research_payload()))
    agent = ResearcherAgent(gateway, config(), FailingSearch())
    with pytest.raises(AgentOutputError, match="search_timeout"):
        await agent.run("objective", context())
    assert gateway.aliases == []


@pytest.mark.asyncio
async def test_empty_search_results_prevent_model_invocation() -> None:
    gateway = Gateway(json.dumps(research_payload()))
    agent = ResearcherAgent(gateway, config(), StaticSearchTool([]))
    with pytest.raises(AgentOutputError, match="no_search_results"):
        await agent.run("objective", context())
    assert gateway.aliases == []


@pytest.mark.asyncio
async def test_gateway_failure_is_reported_as_agent_error() -> None:
    gateway = Gateway(error=GatewayError("provider_timeout"))
    with pytest.raises(AgentOutputError, match="provider_timeout"):
        await ChiefAgent(gateway, config()).run("objective", context())


@pytest.mark.asyncio
async def test_successful_invocation_records_event_without_content() -> None:
    recorder = InvocationRecorder()
    project = context()
    await ChiefAgent(Gateway(json.dumps(chief_payload())), config(), recorder).run(
        "secret-objective-text", project
    )
    event = recorder.events[0]
    assert (event.role, event.model_alias, event.status) == ("chief", "0001", "ok")
    assert event.project_id == project.project_id
    assert (event.prompt_tokens, event.completion_tokens) == (11, 7)
    assert "secret-objective-text" not in event.model_dump_json()


@pytest.mark.asyncio
async def test_rejected_output_records_error_code() -> None:
    recorder = InvocationRecorder()
    gateway = Gateway(json.dumps(research_payload(url="https://fabricated.example")))
    with pytest.raises(AgentOutputError):
        await researcher(gateway, recorder).run("objective", context())
    assert recorder.events[0].status == "rejected"
    assert recorder.events[0].error_code == "unverified_source"


@pytest.mark.asyncio
async def test_each_invocation_receives_only_its_project_context() -> None:
    gateway = Gateway(json.dumps(chief_payload()))
    agent = ChiefAgent(gateway, config())
    first, second = context("Harbor AI"), context("Northwind Web")
    await agent.run("objective", first)
    await agent.run("objective", second)
    assert "Harbor AI" in gateway.prompts[0] and "Northwind Web" not in gateway.prompts[0]
    assert "Northwind Web" in gateway.prompts[1] and "Harbor AI" not in gateway.prompts[1]
    assert str(second.project_id) not in gateway.prompts[0]


@pytest.mark.asyncio
async def test_system_message_carries_the_output_schema() -> None:
    gateway = Gateway(json.dumps(chief_payload()))
    agent = ChiefAgent(gateway, config())
    system = agent.system_message()
    assert "proposed_engineering_scope" in system
    assert "additionalProperties" in system
    await agent.run("objective", context())


@pytest.mark.asyncio
async def test_code_fenced_json_is_accepted() -> None:
    fenced = f"```json\n{json.dumps(chief_payload())}\n```"
    result = await ChiefAgent(Gateway(fenced), config()).run("objective", context())
    assert result.recommendation == "r"


@pytest.mark.asyncio
async def test_event_records_the_resolved_model_not_only_the_alias() -> None:
    recorder = InvocationRecorder()
    await ChiefAgent(Gateway(json.dumps(chief_payload())), config(), recorder).run("o", context())
    event = recorder.events[0]
    assert event.model_alias == "0001"
    assert event.resolved_model == "fixture"


class Sequence:
    """Returns queued contents in order, recording the messages it received."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.messages: list[list[dict[str, str]]] = []

    async def complete(
        self, alias: str, messages: list[dict[str, str]], schema: Any = None
    ) -> Completion:
        self.messages.append(list(messages))
        return Completion(
            model_alias=alias,
            model="fixture",
            content=self.contents.pop(0),
            usage=Usage(),
            latency_seconds=0.1,
        )


@pytest.mark.asyncio
async def test_malformed_output_is_corrected_within_the_attempt_budget() -> None:
    gateway = Sequence(['{"facts": ["f"]', json.dumps(chief_payload())])
    recorder = InvocationRecorder()
    result = await ChiefAgent(gateway, config(), recorder).run("objective", context())
    assert result.recommendation == "r"
    assert len(gateway.messages) == 2
    # The retry says what was wrong. It goes back to the original request
    # rather than resending the failed answer on top of it.
    assert gateway.messages[1][-1]["content"].startswith("Your previous response did not satisfy")
    assert recorder.events[0].attempts == 2


@pytest.mark.asyncio
async def test_attempts_are_bounded() -> None:
    gateway = Sequence(["not json", "still not json", "nor this"])
    settings = config()
    settings.structured_output_attempts = 2
    with pytest.raises(AgentOutputError, match="malformed_structured_output"):
        await ChiefAgent(gateway, settings).run("objective", context())
    assert len(gateway.messages) == 2


@pytest.mark.asyncio
async def test_provenance_failure_is_corrected_within_the_budget() -> None:
    """Citing an unsupplied source is correctable; the check still has to pass."""
    gateway = Sequence(
        [
            json.dumps(research_payload(url="https://fabricated.example")),
            json.dumps(research_payload()),
        ]
    )
    agent = ResearcherAgent(gateway, config(), search_tool(), None)
    report = await agent.run("objective", context())
    assert str(report.sources[0].url).startswith("https://example.com")
    assert "unverified_source" in gateway.messages[1][-1]["content"]


@pytest.mark.asyncio
async def test_an_unverified_source_is_never_accepted() -> None:
    """Retrying must not weaken the check: persistent fabrication still fails."""
    fabricated = json.dumps(research_payload(url="https://fabricated.example"))
    gateway = Sequence([fabricated, fabricated, fabricated])
    with pytest.raises(AgentOutputError, match="unverified_source"):
        await researcher(gateway).run("objective", context())


class RecordingSearch:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        self.queries.append(query)
        suffix = "gap" if len(self.queries) > 1 else "base"
        return [SearchResult("t", f"https://example.com/{suffix}", "s", source_type="primary")]


@pytest.mark.asyncio
async def test_a_rerun_widens_the_search_to_the_named_evidence_gap() -> None:
    """Repeating the original query would return the same sources every time."""
    search = RecordingSearch()
    payload = research_payload(url="https://example.com/base")
    agent = ResearcherAgent(Gateway(json.dumps(payload)), config(), search)
    await agent.run("original question", context(), focus="official repository licences")
    assert search.queries == [
        "original question",
        "original question official repository licences",
    ]


@pytest.mark.asyncio
async def test_without_a_gap_only_one_query_is_issued() -> None:
    search = RecordingSearch()
    agent = ResearcherAgent(
        Gateway(json.dumps(research_payload(url="https://example.com/base"))), config(), search
    )
    await agent.run("original question", context())
    assert search.queries == ["original question"]


@pytest.mark.asyncio
async def test_widened_results_are_citable_and_deduplicated() -> None:
    search = RecordingSearch()
    payload = research_payload(url="https://example.com/gap")
    agent = ResearcherAgent(Gateway(json.dumps(payload)), config(), search)
    report = await agent.run("q", context(), focus="primary documentation")
    assert str(report.sources[0].url).startswith("https://example.com/gap")


class StubFetcher:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    async def fetch_many(self, urls: list[str]) -> list[Any]:
        from app.tools.fetch import Page

        self.requested = list(urls)
        return [Page(url=u, text=self.pages[u]) for u in urls if u in self.pages]


@pytest.mark.asyncio
async def test_page_content_is_supplied_to_the_researcher() -> None:
    gateway = Gateway(json.dumps(research_payload()))
    fetcher = StubFetcher({"https://example.com": "Official documentation body"})
    agent = ResearcherAgent(
        gateway,
        config(),
        search_tool(source_type="unknown"),
        None,
        fetcher,  # type: ignore[arg-type]
    )
    await agent.run("objective", context())
    prompt = gateway.prompts[0]
    assert "Official documentation body" in prompt
    assert fetcher.requested == ["https://example.com"]


@pytest.mark.asyncio
async def test_the_researcher_classifies_a_source_the_provider_could_not() -> None:
    """Brave cannot tell primary from secondary; the researcher may say so."""
    gateway = Gateway(json.dumps(research_payload(source_type="primary")))
    agent = ResearcherAgent(gateway, config(), search_tool(source_type="unknown"))
    report = await agent.run("objective", context())
    assert report.sources[0].source_type == "primary"


@pytest.mark.asyncio
async def test_an_asserted_source_type_still_cannot_be_contradicted() -> None:
    gateway = Gateway(json.dumps(research_payload(source_type="primary")))
    agent = ResearcherAgent(gateway, config(), search_tool(source_type="secondary"))
    with pytest.raises(AgentOutputError, match="misreported_source_type"):
        await agent.run("objective", context())


@pytest.mark.asyncio
async def test_an_unclassified_source_is_rejected() -> None:
    """A source the researcher declined to classify cannot be weighed."""
    gateway = Gateway(json.dumps(research_payload(source_type="unknown")))
    with pytest.raises(AgentOutputError, match="malformed_structured_output"):
        await ResearcherAgent(gateway, config(), search_tool(source_type="unknown")).run(
            "objective", context()
        )


def test_the_schema_offers_only_primary_or_secondary() -> None:
    from app.schemas import SourceType

    assert {member.value for member in SourceType} == {"primary", "secondary"}


@pytest.mark.asyncio
async def test_instructions_tell_the_researcher_to_classify_itself() -> None:
    agent = ResearcherAgent(Gateway(""), config(), search_tool())
    assert "Do not copy a classification" in agent.instructions
    assert "primary" in agent.system_message()


class SequencedGateway(Gateway):
    """Returns each content in turn, with a cost per attempt."""

    def __init__(self, contents: list[str]) -> None:
        super().__init__()
        self.contents = contents

    async def complete(
        self,
        alias: str,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        schema: Any = None,
    ) -> Completion:
        content = self.contents.pop(0)
        return Completion(
            model_alias=alias,
            model="fixture",
            content=content,
            usage=Usage(prompt_tokens=100, completion_tokens=10),
            latency_seconds=0.5,
            estimated_cost=0.01,
        )


async def test_usage_counts_every_attempt_including_corrective_retries() -> None:
    """A retry is billed; accounting that reports only the last attempt undercounts."""
    recorder = InvocationRecorder()
    gateway = SequencedGateway(["not json", json.dumps(chief_payload())])
    await ChiefAgent(gateway, config(), recorder).run("objective", context())
    [event] = recorder.events
    assert event.attempts == 2
    assert event.prompt_tokens == 200
    assert event.completion_tokens == 20
    assert event.estimated_cost == pytest.approx(0.02)


async def test_events_carry_the_run_and_task_they_belong_to() -> None:
    run_id, task_id = uuid4(), uuid4()
    recorder = InvocationRecorder(run_id=run_id, task_id=task_id)
    await ChiefAgent(Gateway(json.dumps(chief_payload())), config(), recorder).run(
        "objective", context()
    )
    [event] = recorder.events
    assert (event.run_id, event.task_id) == (run_id, task_id)
    assert event.started_at <= event.occurred_at


async def test_a_direct_connection_is_recorded_as_its_provider() -> None:
    recorder = InvocationRecorder()
    agent = ChiefAgent(Gateway(json.dumps(chief_payload())), config(), recorder)
    agent.model_alias = "connection:0000"
    agent.provider = "anthropic"
    await agent.run("objective", context())
    assert recorder.events[0].provider == "anthropic"
    assert recorder.events[0].model_alias == "connection:0000"


def test_context_layers_are_rendered_for_the_model() -> None:
    layered = context().model_copy(
        update={"decisions": ["Storage: Use Postgres"], "prior_outcomes": ["Ship v1"]}
    )
    text = layered.render()
    assert "Recorded decisions:\n- Storage: Use Postgres" in text
    assert "Earlier outcomes in this project:\n- Ship v1" in text


LOCAL = "local://docs/Research/q3.md#chunk-0"


def local_passages() -> Any:
    from app.sources.retrieval import Passage

    async def retrieve(project_id: Any, query: str) -> list[Passage]:
        return [
            Passage(
                source="Research",
                path="q3.md",
                sha256="b" * 64,
                chunk=0,
                text="Q3 revenue grew 12% in Europe.",
            )
        ]

    return retrieve


def local_researcher(gateway: Gateway, web: list[SearchResult] | None = None) -> ResearcherAgent:
    return ResearcherAgent(
        gateway, config(), StaticSearchTool(web or []), retriever=local_passages()
    )


async def test_the_researcher_can_cite_the_owners_local_documents() -> None:
    gateway = Gateway(json.dumps(research_payload(LOCAL, "primary")))
    agent = local_researcher(gateway)
    report = await agent.run("How did revenue change?", context())
    assert str(report.sources[0].url) == LOCAL
    assert "Q3 revenue grew 12% in Europe." in gateway.prompts[0]
    assert agent.last_local[LOCAL]["sha256"] == "b" * 64


async def test_a_local_citation_that_was_not_supplied_is_rejected() -> None:
    other = "local://docs/Research/secret.md#chunk-0"
    gateway = Gateway(json.dumps(research_payload(other, "primary")))
    with pytest.raises(AgentOutputError, match="unverified_source"):
        await local_researcher(gateway).run("q", context())


async def test_local_documents_are_primary_sources() -> None:
    gateway = Gateway(json.dumps(research_payload(LOCAL, "secondary")))
    with pytest.raises(AgentOutputError, match="misreported_source_type"):
        await local_researcher(gateway).run("q", context())


async def test_no_web_results_and_no_local_passages_still_fails() -> None:
    async def nothing(project_id: Any, query: str) -> list[Any]:
        return []

    agent = ResearcherAgent(Gateway("{}"), config(), StaticSearchTool([]), retriever=nothing)
    with pytest.raises(AgentOutputError, match="no_search_results"):
        await agent.run("q", context())


def test_the_run_date_is_rendered_for_every_agent() -> None:
    from datetime import date

    dated = context().model_copy(update={"as_of": date(2026, 9, 19)})
    assert "Date of this work: 2026-09-19" in dated.render()
    assert "Date of this work" not in context().render()


class FakeFetcher:
    """Serves named pages; anything else is unreachable."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    async def fetch(self, url: str, follow_redirects: int = 0, max_chars: int = 4000) -> Any:
        from datetime import UTC, datetime

        from app.tools.fetch import FetchError, Page

        self.requested.append(url)
        if url not in self.pages:
            raise FetchError("fetch_unreachable")
        return Page(url=url, text=self.pages[url], retrieved_at=datetime(2026, 9, 19, tzinfo=UTC))

    async def fetch_many(self, urls: list[str]) -> list[Any]:
        return []


OFFICIAL = "https://tavily.com/pricing"


def official_researcher(gateway: Gateway, web: list[SearchResult] | None = None) -> ResearcherAgent:
    agent = ResearcherAgent(
        gateway,
        config(),
        StaticSearchTool(web or []),
        fetcher=FakeFetcher({OFFICIAL: "Researcher plan $30 per month, 4,000 credits"}),  # type: ignore[arg-type]
    )
    agent.official_urls = [OFFICIAL, "https://tavily.com/made-up-page"]
    return agent


async def test_named_official_pages_are_fetched_and_citable() -> None:
    gateway = Gateway(json.dumps(research_payload(OFFICIAL, "primary")))
    agent = official_researcher(gateway)
    report = await agent.run("Tavily pricing", context())
    assert str(report.sources[0].url) == OFFICIAL
    assert "Researcher plan $30 per month" in gateway.prompts[0]


async def test_a_named_page_that_could_not_be_fetched_is_not_citable() -> None:
    gateway = Gateway(json.dumps(research_payload("https://tavily.com/made-up-page", "primary")))
    with pytest.raises(AgentOutputError, match="unverified_source"):
        await official_researcher(gateway).run("Tavily pricing", context())


async def test_access_dates_are_set_by_the_application_not_the_model() -> None:
    payload = research_payload(OFFICIAL, "primary")
    payload["sources"][0]["accessed_on"] = "1999-01-01"  # the model's claim is ignored
    gateway = Gateway(json.dumps(payload))
    report = await official_researcher(gateway).run("Tavily pricing", context())
    assert str(report.sources[0].accessed_on) == "2026-09-19"
    assert "retrieved 2026-09-19" in gateway.prompts[0]


async def test_search_results_carry_the_search_date() -> None:
    from datetime import UTC, datetime

    gateway = Gateway(json.dumps(research_payload()))
    report = await researcher(gateway).run("q", context())
    assert report.sources[0].accessed_on == datetime.now(UTC).date().isoformat()


def test_a_research_summary_may_carry_a_comparison_table() -> None:
    from app.schemas.agent_output import ResearchSummary

    summary = ResearchSummary.model_validate(
        {
            "question": "q",
            "findings": ["f"],
            "limits": ["l"],
            "recommendation": "r",
            "comparison_table": {
                "columns": ["Provider", "Tier", "Price"],
                "rows": [["Brave", "Search", "$5 / 1,000"], ["Tavily", "Researcher", "$30 / mo"]],
                "row_sources": ["https://brave.com/search/api/", "https://www.tavily.com/pricing"],
            },
        }
    )
    assert summary.comparison_table is not None
    assert summary.comparison_table.rows[1][2] == "$30 / mo"


def test_a_table_row_must_have_one_cell_per_column() -> None:
    from pydantic import ValidationError

    from app.schemas.agent_output import ComparisonTable

    with pytest.raises(ValidationError, match="one cell per column"):
        ComparisonTable.model_validate(
            {"columns": ["A", "B"], "rows": [["only one"]], "row_sources": ["https://a.example"]}
        )


def test_a_table_is_optional() -> None:
    from app.schemas.agent_output import ResearchSummary

    summary = ResearchSummary.model_validate(
        {"question": "q", "findings": ["f"], "limits": ["l"], "recommendation": "r"}
    )
    assert summary.comparison_table is None


class LinkingFetcher(FakeFetcher):
    """Official docs home links to a rate-limits page and elsewhere."""

    async def fetch(self, url: str, follow_redirects: int = 0, max_chars: int = 4000) -> Any:
        page = await super().fetch(url, follow_redirects, max_chars)
        if url == "https://docs.tavily.com/":
            from dataclasses import replace

            return replace(
                page,
                links=(
                    ("Rate Limits", "https://docs.tavily.com/rate-limits"),
                    ("Blog", "https://docs.tavily.com/blog"),
                    ("Partner", "https://partner.example/tavily"),
                ),
            )
        return page


def linking_researcher(
    gateway: Gateway, judge: Any = None
) -> tuple[ResearcherAgent, LinkingFetcher]:
    fetcher = LinkingFetcher(
        {
            "https://docs.tavily.com/": "Tavily docs home",
            "https://docs.tavily.com/rate-limits": "Development: 100 RPM; Production: 1,000 RPM",
            "https://docs.tavily.com/blog": "News",
            "https://partner.example/tavily": "Off-site",
        }
    )
    agent = ResearcherAgent(
        gateway,
        config(),
        StaticSearchTool([]),
        fetcher=fetcher,
        link_judge=judge,  # type: ignore[arg-type]
    )
    agent.official_urls = ["https://docs.tavily.com/"]
    return agent, fetcher


async def test_a_relevant_same_site_link_is_followed_and_citable() -> None:
    class Judge:
        async def link_relevance(self, question: str, links: list[dict[str, str]]) -> list[float]:
            return [0.95 if "Rate" in link["text"] else 0.1 for link in links]

    page = "https://docs.tavily.com/rate-limits"
    gateway = Gateway(json.dumps(research_payload(page, "primary")))
    agent, fetcher = linking_researcher(gateway, Judge())
    report = await agent.run("What rate limits does Tavily document?", context())
    assert str(report.sources[0].url) == page
    assert "1,000 RPM" in gateway.prompts[0]
    assert "https://docs.tavily.com/blog" not in fetcher.requested
    assert "https://partner.example/tavily" not in fetcher.requested  # off-site never fetched


async def test_without_a_judge_links_are_chosen_by_matching_words() -> None:
    gateway = Gateway(
        json.dumps(research_payload("https://docs.tavily.com/rate-limits", "primary"))
    )
    agent, fetcher = linking_researcher(gateway)
    await agent.run("What rate limits does Tavily document?", context())
    assert "https://docs.tavily.com/rate-limits" in fetcher.requested
    assert "https://docs.tavily.com/blog" not in fetcher.requested


def summary_with_table(row_sources: list[str]) -> dict[str, Any]:
    return {
        "question": "q",
        "findings": ["f"],
        "limits": ["l"],
        "recommendation": "r",
        "comparison_table": {
            "columns": ["Provider", "Price"],
            "rows": [["Brave", "$5"], ["Tavily", "$30"]],
            "row_sources": row_sources,
        },
    }


def test_every_table_row_names_its_source() -> None:
    from pydantic import ValidationError

    from app.schemas.agent_output import ResearchSummary

    with pytest.raises(ValidationError, match="one source per row"):
        ResearchSummary.model_validate(summary_with_table(["https://brave.com/search/api/"]))


async def test_a_table_row_citing_a_source_outside_the_research_is_rejected() -> None:
    from app.agents.summarist import SummaristAgent

    payload = summary_with_table(["https://brave.com/search/api/", "https://invented.example/"])
    agent = SummaristAgent(Gateway(json.dumps(payload)), config())
    agent.allowed_citations = {"https://brave.com/search/api/", "https://www.tavily.com/pricing"}
    with pytest.raises(AgentOutputError, match="uncited_table_row"):
        await agent.run("q", context())


async def test_table_rows_citing_research_sources_are_accepted() -> None:
    from app.agents.summarist import SummaristAgent

    payload = summary_with_table(
        ["https://brave.com/search/api/", "https://www.tavily.com/pricing"]
    )
    agent = SummaristAgent(Gateway(json.dumps(payload)), config())
    agent.allowed_citations = {"https://brave.com/search/api", "https://www.tavily.com/pricing"}
    summary = await agent.run("q", context())
    assert summary.comparison_table is not None


class RedirectingFetcher(FakeFetcher):
    """The named page redirects: the page retrieved has a different address."""

    async def fetch(self, url: str, follow_redirects: int = 0, max_chars: int = 4000) -> Any:
        from dataclasses import replace

        page = await super().fetch(url, follow_redirects, max_chars)
        return replace(page, url="https://www.tavily.com/")


async def test_citing_a_named_page_by_its_requested_address_is_accepted_and_resolved() -> None:
    """Regression: run 5 failed when the plan's address redirected elsewhere."""
    requested = "https://tavily.com/#pricing"
    gateway = Gateway(json.dumps(research_payload(requested, "primary")))
    agent = ResearcherAgent(
        gateway,
        config(),
        StaticSearchTool([]),
        fetcher=RedirectingFetcher({requested: "Plans and credits"}),  # type: ignore[arg-type]
    )
    agent.official_urls = [requested]
    report = await agent.run("Tavily pricing", context())
    assert str(report.sources[0].url) == "https://www.tavily.com/"
    assert report.sources[0].accessed_on == "2026-09-19"


def test_fragments_do_not_make_addresses_differ() -> None:
    from app.agents.researcher import normalize_url

    assert normalize_url("https://tavily.com/#pricing") == normalize_url("https://tavily.com")


def test_local_chunk_fragments_are_preserved() -> None:
    from app.agents.researcher import normalize_url

    first = normalize_url("local://docs/R/a.md#chunk-0")
    assert first != normalize_url("local://docs/R/a.md#chunk-1")


class IndexedDocsFetcher(FakeFetcher):
    """Docs home links only to an index; the index links to the rate-limits page."""

    LINKS = {
        "https://docs.tavily.com/": (
            ("Docs index (llms.txt)", "https://docs.tavily.com/llms.txt"),
        ),
        "https://docs.tavily.com/llms.txt": (
            ("Rate Limits", "https://docs.tavily.com/rate-limits"),
            ("Changelog", "https://docs.tavily.com/changelog"),
        ),
    }

    async def fetch(self, url: str, follow_redirects: int = 0, max_chars: int = 4000) -> Any:
        from dataclasses import replace

        page = await super().fetch(url, follow_redirects, max_chars)
        return replace(page, links=self.LINKS.get(url, ()))


async def test_a_second_hop_reaches_a_page_listed_only_in_a_docs_index() -> None:
    class Judge:
        async def link_relevance(self, question: str, links: list[dict[str, str]]) -> list[float]:
            return [
                0.9 if ("Rate" in link["text"] or "index" in link["text"]) else 0.1
                for link in links
            ]

    page = "https://docs.tavily.com/rate-limits"
    fetcher = IndexedDocsFetcher(
        {
            "https://docs.tavily.com/": "Docs home",
            "https://docs.tavily.com/llms.txt": "Index",
            page: "Development keys: 100 RPM. Production keys: 1,000 RPM.",
            "https://docs.tavily.com/changelog": "Changes",
        }
    )
    gateway = Gateway(json.dumps(research_payload(page, "primary")))
    agent = ResearcherAgent(
        gateway,
        config(),
        StaticSearchTool([]),
        fetcher=fetcher,
        link_judge=Judge(),  # type: ignore[arg-type]
    )
    agent.official_urls = ["https://docs.tavily.com/"]
    report = await agent.run("What rate limits does Tavily document?", context())
    assert str(report.sources[0].url) == page
    assert "1,000 RPM" in gateway.prompts[0]
    assert "https://docs.tavily.com/changelog" not in fetcher.requested


async def test_a_relevant_link_deep_in_a_long_index_is_still_considered() -> None:
    """Candidates are ranked before the cap, not taken in page order."""
    seen_by_judge: list[str] = []

    class Judge:
        async def link_relevance(self, question: str, links: list[dict[str, str]]) -> list[float]:
            seen_by_judge.extend(link["text"] for link in links)
            return [0.9 if link["text"] == "Rate Limits" else 0.1 for link in links]

    index = tuple((f"Guide {n}", f"https://docs.tavily.com/guide-{n}") for n in range(120))
    index += (("Rate Limits", "https://docs.tavily.com/rate-limits"),)

    class LongIndexFetcher(FakeFetcher):
        async def fetch(self, url: str, follow_redirects: int = 0, max_chars: int = 4000) -> Any:
            from dataclasses import replace

            page = await super().fetch(url, follow_redirects, max_chars)
            return replace(page, links=index if url.endswith("/") else ())

    fetcher = LongIndexFetcher(
        {"https://docs.tavily.com/": "Index", "https://docs.tavily.com/rate-limits": "100 RPM"}
    )
    agent = ResearcherAgent(
        Gateway(json.dumps(research_payload("https://docs.tavily.com/rate-limits", "primary"))),
        config(),
        StaticSearchTool([]),
        fetcher=fetcher,  # type: ignore[arg-type]
        link_judge=Judge(),
    )
    agent.official_urls = ["https://docs.tavily.com/"]
    await agent.run("What rate limits does Tavily document?", context())
    assert "Rate Limits" in seen_by_judge
    assert len(seen_by_judge) <= 40
    assert "https://docs.tavily.com/rate-limits" in fetcher.requested


async def test_deliverable_writers_get_the_larger_output_cap() -> None:
    from app.agents.summarist import SummaristAgent

    caps: list[Any] = []

    class Capturing(Gateway):
        async def complete(
            self,
            alias: str,
            messages: list[dict[str, str]],
            max_tokens: int | None = None,
            schema: Any = None,
        ) -> Completion:
            caps.append(max_tokens)
            return await super().complete(alias, messages)

    payload = {"question": "q", "findings": ["f"], "limits": ["l"], "recommendation": "r"}
    settings = config().model_copy(update={"deliverable_max_output_tokens": 16_000})
    await SummaristAgent(Capturing(json.dumps(payload)), settings).run("q", context())
    await ChiefAgent(Capturing(json.dumps(chief_payload())), settings).run("q", context())
    assert caps == [16_000, None]


async def test_a_direct_call_sends_its_per_call_output_cap() -> None:
    import httpx

    from app.gateway.direct import DirectCompleter

    sent: list[Any] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["max_completion_tokens"])
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    client = DirectCompleter(
        "openai",
        "m",
        "sk-test-key-0123456789",
        config(),
        label="connection:o",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        prices={},
    )
    await client.complete("0003", [{"role": "user", "content": "x"}], max_tokens=16_000)
    await client.complete("0003", [{"role": "user", "content": "x"}])
    assert sent == [16_000, config().llm_max_output_tokens]


class CutOffThenComplete:
    """Answers with a truncated response first, as a model at its output cap does."""

    def __init__(self, content: str, cut_offs: int = 1) -> None:
        self.content = content
        self.cut_offs = cut_offs
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(
        self,
        alias: str,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        schema: Any = None,
    ) -> Completion:
        self.calls += 1
        self.prompts.append(messages[-1]["content"])
        if self.calls <= self.cut_offs:
            raise GatewayError("incomplete_response")
        return Completion(
            model_alias=alias,
            model="fixture",
            content=self.content,
            usage=Usage(prompt_tokens=11, completion_tokens=7),
            latency_seconds=0.5,
        )


@pytest.mark.asyncio
async def test_a_cut_off_deliverable_is_asked_for_again_shorter() -> None:
    """The owner's run died at summarist:incomplete_response after every step
    before it had succeeded and been paid for."""
    gateway = CutOffThenComplete(json.dumps(chief_payload()))
    output = await ChiefAgent(gateway, config()).run("objective", context())
    assert output is not None
    assert gateway.calls == 2, "the cut-off answer must be asked for again"
    assert "cut off" in gateway.prompts[-1], gateway.prompts[-1]
    assert "shorter" in gateway.prompts[-1]


@pytest.mark.asyncio
async def test_a_deliverable_cut_off_every_time_still_fails() -> None:
    """Retries are bounded: a model that cannot finish is not retried forever."""
    gateway = CutOffThenComplete(json.dumps(chief_payload()), cut_offs=99)
    with pytest.raises(AgentOutputError, match="incomplete_response"):
        await ChiefAgent(gateway, config()).run("objective", context())
    assert gateway.calls <= 3


@pytest.mark.asyncio
async def test_a_correction_does_not_resend_the_failed_answer() -> None:
    """The owner's task paid 52k, 65k, then 79k tokens of prompt for one step:
    each retry resent the whole conversation plus every failed answer."""
    gateway = Sequence(["not json at all", '{"facts": ["f"]}', json.dumps(chief_payload())])
    await ChiefAgent(gateway, config()).run("objective", context())
    first, second, third = gateway.messages
    assert len(second) == len(first) + 1 and len(third) == len(first) + 1
    assert all(message["role"] != "assistant" for message in third)
    # The correction names what was wrong, so the model can fix it.
    assert "recommendation" in third[-1]["content"]


@pytest.mark.asyncio
async def test_the_reasons_an_output_was_rejected_are_recorded() -> None:
    """Why a step failed could not be told afterwards: nothing kept the reason."""
    gateway = Sequence(['{"facts": "SECRET-TEXT"', '{"facts": ["f"]}', "still not json"])
    recorder = InvocationRecorder()
    with pytest.raises(AgentOutputError, match="malformed_structured_output"):
        await ChiefAgent(gateway, config(), recorder).run("objective", context())
    detail = recorder.events[0].error_detail or ""
    assert "attempt 1: not valid JSON" in detail
    assert "attempt 2:" in detail and "recommendation" in detail
    assert "attempt 3: not valid JSON" in detail
    assert "SECRET-TEXT" not in detail, "the model's output is never recorded"


@pytest.mark.asyncio
async def test_a_call_that_recovered_still_says_what_went_wrong_first() -> None:
    gateway = Sequence(["not json", json.dumps(chief_payload())])
    recorder = InvocationRecorder()
    await ChiefAgent(gateway, config(), recorder).run("objective", context())
    assert recorder.events[0].status == "ok"
    assert "attempt 1: not valid JSON" in (recorder.events[0].error_detail or "")

"""Validate live model output against each role's structured output contract.

Phase 2 exit gate. Makes one real, billable request per role through the
configured gateway aliases. Uses fixed search results so the Researcher check
is deterministic and needs no search credential.
"""

import asyncio
import json
import sys
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.agents import AgentOutputError, ChiefAgent, CriticAgent, ResearcherAgent, StrategistAgent
from app.agents.base import BaseAgent
from app.config import Settings, get_settings
from app.context import ProjectContext
from app.db.session import make_engine, make_session_factory
from app.events import InvocationRecorder
from app.gateway.direct import Completer
from app.office_models import office_models
from app.tools.web import SearchResult, StaticSearchTool

OBJECTIVE = "Assess whether small European AI agencies need an automated research assistant."

FIXTURE_SOURCES = [
    SearchResult(
        title="EU AI services market overview",
        url="https://example.org/eu-ai-services",
        snippet="Agency headcount and service mix across the EU.",
        source_type="secondary",
    ),
    SearchResult(
        title="Agency operations survey",
        url="https://example.org/agency-survey",
        snippet="Reported hours spent on manual desk research.",
        source_type="primary",
    ),
]


def project_context() -> ProjectContext:
    return ProjectContext(
        organization="Northwind",
        project_id=uuid4(),
        project_name="Contract verification",
        background="One-off check that each role returns its structured contract.",
        constraints=["Return JSON only"],
    )


def build_agents(
    gateway: Completer, settings: Settings, recorder: InvocationRecorder
) -> list[BaseAgent[Any]]:
    search = StaticSearchTool(FIXTURE_SOURCES)
    return [
        ChiefAgent(gateway, settings, recorder),
        ResearcherAgent(gateway, settings, search, recorder),
        StrategistAgent(gateway, settings, recorder),
        CriticAgent(gateway, settings, recorder),
    ]


async def verify(gateway: Completer, settings: Settings) -> bool:
    recorder = InvocationRecorder()
    context = project_context()
    passed = True
    for agent in build_agents(gateway, settings, recorder):
        record: dict[str, object] = {"agent": agent.role, "model_alias": agent.model_alias}
        try:
            output = await agent.run(OBJECTIVE, context)
        except AgentOutputError as exc:
            passed = False
            record.update(status="failed", error=exc.code)
        else:
            record.update(status="ok", fields=sorted(output.model_dump().keys()))
        event = recorder.events[-1]
        record.update(
            latency_seconds=round(event.latency_seconds, 3),
            input_tokens=event.prompt_tokens,
            output_tokens=event.completion_tokens,
            estimated_cost=event.estimated_cost,
        )
        print(json.dumps(record), flush=True)
    return passed


async def main() -> int:
    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        print(json.dumps({"status": "configuration_error", "fields": fields}), file=sys.stderr)
        return 2
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        models = office_models(settings, make_session_factory(engine))
        return 0 if await verify(models, settings) else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

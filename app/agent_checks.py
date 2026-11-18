"""Judgment-backed checks applied to agent outputs. Code holds the thresholds."""

import copy
from typing import Any

from app.agents.researcher import normalize_url
from app.judgments import Judge, JudgmentUnavailable

# Only a confident disagreement changes a label the researcher chose.
SOURCE_CORRECTION_CONFIDENCE = 0.75
# A claim judged less likely than this to be supported is flagged for the owner.
SUPPORTED_PROBABILITY = 0.5


async def check_sources(
    judge: Judge, research: dict[str, Any], snippets: dict[str, str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Re-judge each cited source's primary/secondary label; correct confident errors.

    Returns the (possibly corrected) research and a record of every correction.
    A failed check leaves the research exactly as the researcher produced it.
    """
    # The owner's own documents are primary by definition; only web sources are judged.
    web = [
        index
        for index, source in enumerate(research.get("sources") or [])
        if not str(source.get("url", "")).startswith("local://")
    ]
    sources = [research["sources"][index] for index in web]
    if not sources:
        return research, []
    items = [
        {
            "title": str(source.get("title", "")),
            "url": str(source.get("url", "")),
            "excerpt": snippets.get(normalize_url(str(source.get("url", ""))), ""),
        }
        for source in sources
    ]
    try:
        judged = await judge.source_types(items)
    except JudgmentUnavailable:
        return research, []
    corrected = copy.deepcopy(research)
    checks = []
    targets = [corrected["sources"][index] for index in web]
    for source, (label, confidence) in zip(targets, judged, strict=False):
        declared = source.get("source_type")
        if label != declared and confidence >= SOURCE_CORRECTION_CONFIDENCE:
            source["source_type"] = label
            checks.append(
                {
                    "url": str(source.get("url")),
                    "declared": declared,
                    "judged": label,
                    "confidence": round(confidence, 2),
                    "corrected": True,
                }
            )
    return corrected, checks


async def unverified_claims(
    judge: Judge, synthesis: dict[str, Any], research: dict[str, Any] | None
) -> list[str]:
    """Synthesis facts the research evidence does not support. Raises when unavailable."""
    facts = [str(fact) for fact in synthesis.get("facts") or []]
    if not facts:
        return []
    evidence = {
        key: (research or {}).get(key) or [] for key in ("key_findings", "evidence", "sources")
    }
    probabilities = await judge.supported(facts, evidence)
    return [
        fact for fact, p in zip(facts, probabilities, strict=False) if p < SUPPORTED_PROBABILITY
    ]

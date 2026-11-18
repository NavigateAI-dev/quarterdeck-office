"""Run several real workflows and report what happens to each.

SPEC phase 8 asks for repeated real-world workflows so that failures can be
found and fixed. This submits objectives through the HTTP API exactly as a
person would, waits for each to settle, and prints one machine-readable record
per run plus a summary by outcome.

Makes real, billable model and search calls.
"""

import asyncio
import json
import os
import time
from typing import Any

import httpx

BASE_URL = os.environ.get("QUARTERDECK_URL", "http://127.0.0.1:8000")
POLL_SECONDS = 10.0
RUN_TIMEOUT_SECONDS = 1800.0

# Deliberately varied: two the evidence base can answer, one it cannot, and one
# too vague to act on. A workflow that only handles good input is not hardened.
OBJECTIVES = [
    "Compare Temporal, Restate and Inngest on durable execution guarantees, "
    "citing their official documentation.",
    "What rate limits and pricing tiers does the Brave Search API document?",
    "Assess whether small European AI agencies need an automated research assistant.",
    "Improve our onboarding.",
]

# The approval pause is the expected end of an analysis run, not a timeout.
TERMINAL = {
    "COMPLETED",
    "FAILED",
    "BLOCKED",
    "REJECTED",
    "NEEDS_HUMAN_REVIEW",
    "WAITING_FOR_APPROVAL",
}


def category(record: dict[str, Any]) -> str:
    """Classify an outcome into the specification's phase 8 vocabulary."""
    status, reason, error = record["status"], record.get("reason"), record.get("error") or ""
    if status == "COMPLETED":
        return "completed"
    if status == "WAITING_FOR_APPROVAL":
        return "awaiting_approval"
    if reason == "evidence_not_reachable":
        return "stopped_at_planning"
    if reason in ("revision_made_no_change", "revision_not_converging"):
        return "workflow_loop_stopped_early"
    if reason == "revision_limit_reached":
        return "workflow_loop_exhausted"
    if reason == "critic_blocked":
        return "blocked_by_critic"
    if "malformed" in error:
        return "malformed_output"
    if any(token in error for token in ("timeout", "unavailable", "unreachable", "rejected")):
        return "provider_failure"
    if "search" in error:
        return "tool_failure"
    return "other_failure"


async def submit(client: httpx.AsyncClient, project_id: str, objective: str) -> str:
    response = await client.post("/runs", json={"project_id": project_id, "objective": objective})
    response.raise_for_status()
    return str(response.json()["id"])


async def settle(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    started = time.monotonic()
    while True:
        run: dict[str, Any] = (await client.get(f"/runs/{run_id}")).json()
        if run["status"] in TERMINAL:
            return run
        if time.monotonic() - started > RUN_TIMEOUT_SECONDS:
            run["status"] = "TIMED_OUT_WAITING"
            return run
        await asyncio.sleep(POLL_SECONDS)


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60, trust_env=False) as client:
        project = (await client.post("/projects", json={"name": "Phase 8 hardening"})).json()
        records: list[dict[str, Any]] = []

        for objective in OBJECTIVES:
            started = time.monotonic()
            run_id = await submit(client, project["id"], objective)
            run = await settle(client, run_id)
            artifacts = (await client.get(f"/runs/{run_id}/artifacts")).json()
            state = run.get("state") or {}
            record = {
                "objective": objective[:70],
                "run_id": run_id,
                "status": run["status"],
                "reason": state.get("stopped_reason"),
                "error": run.get("error"),
                "revisions": state.get("revision_count"),
                "artifacts": len(artifacts),
                "artifact_types": sorted({a["type"] for a in artifacts}),
                "seconds": round(time.monotonic() - started, 1),
            }
            record["category"] = category(record)
            records.append(record)
            print(json.dumps(record), flush=True)

        summary: dict[str, int] = {}
        for record in records:
            summary[record["category"]] = summary.get(record["category"], 0) + 1
        print(
            json.dumps(
                {
                    "summary": summary,
                    "runs": len(records),
                    "total_seconds": round(sum(r["seconds"] for r in records), 1),
                    "project_id": project["id"],
                }
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

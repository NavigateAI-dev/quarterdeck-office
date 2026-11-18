"""The task room reads a task's events by cursor.

The events endpoint answers at most a page at a time, so a room that fetched
"all events" on every refresh showed a long task's first page and nothing
after it. These run the shipped script in Node, and are skipped without it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.orchestrator.runner import RunRepository

SCRIPT = Path("app/api/web/office/events.js")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is not installed")

HARNESS = """
const vm = require("vm");
const fs = require("fs");
const context = { console };
vm.createContext(context);
const source = fs.readFileSync(process.argv[1], "utf8");
vm.runInContext(source + ";this.EventReader = EventReader;", context);
const { EventReader } = context;
// A server holding `total` events for run "a" and 2 for run "b", paged like the API.
function server(total, page) {
  const asked = [];
  const made = Array.from({ length: total }, (_, i) => ({ cursor: i + 1, kind: "e" + (i + 1) }));
  const rows = { a: made, b: [{ cursor: 901, kind: "b1" }, { cursor: 902, kind: "b2" }] };
  const fetchPage = async (runId, after) => {
    asked.push([runId, after]);
    return rows[runId].filter((row) => row.cursor > after).slice(0, page);
  };
  return { asked, rows, fetchPage };
}
(async () => {
  const scenario = JSON.parse(process.argv[2]);
  const s = server(scenario.total, scenario.page);
  const reader = new EventReader(s.fetchPage, scenario.page);
  const out = {};
  out.first = (await reader.read("a")).map((e) => e.cursor);
  s.rows.a.push({ cursor: scenario.total + 1, kind: "late" });
  out.second = (await reader.read("a")).map((e) => e.cursor);
  out.other = (await reader.read("b")).map((e) => e.cursor);
  out.asked = s.asked;
  console.log(JSON.stringify(out));
})();
"""


def run(total: int, page: int) -> dict:
    done = subprocess.run(  # noqa: S603 - a fixed harness and the shipped script
        [NODE or "node", "-e", HARNESS, str(SCRIPT), json.dumps({"total": total, "page": page})],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(done.stdout)


def test_a_long_task_is_read_past_its_first_page() -> None:
    got = run(total=7, page=3)
    assert got["first"] == [1, 2, 3, 4, 5, 6, 7]


def test_a_refresh_asks_only_for_what_is_new_and_appends_it() -> None:
    got = run(total=4, page=3)
    assert got["second"] == [1, 2, 3, 4, 5]
    # After the first load, the refresh starts from the last cursor it holds.
    refreshes = [after for run_id, after in got["asked"] if run_id == "a"][2:]
    assert refreshes[0] == 4


def test_another_task_starts_again_from_zero() -> None:
    got = run(total=4, page=3)
    assert got["other"] == [901, 902]
    assert ["b", 0] in got["asked"]


def test_the_page_size_matches_what_the_api_returns() -> None:
    """A reader that assumed a larger page would stop after the first one."""
    import inspect

    limit = inspect.signature(RunRepository.events).parameters["limit"].default
    assert f"EVENT_PAGE = {limit};" in SCRIPT.read_text()

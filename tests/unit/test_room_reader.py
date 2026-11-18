"""The room screen reads a project's room by its two cursors."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path("app/api/web/office/events.js")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is not installed")

HARNESS = """
const vm = require("vm");
const fs = require("fs");
const context = { console };
vm.createContext(context);
const source = fs.readFileSync(process.argv[1], "utf8");
vm.runInContext(source + ";this.RoomReader = RoomReader;", context);
const { RoomReader } = context;
// Rooms "a" (five items, paged two at a time) and "b" (one item).
const rooms = {
  a: [1, 2, 3, 4, 5].map((n) => ({ id: "m:" + n, n })),
  b: [{ id: "m:90", n: 90 }],
};
const asked = [];
const fetchPage = async (projectId, cursor) => {
  asked.push([projectId, cursor.after_message]);
  const rest = rooms[projectId].filter((item) => item.n > cursor.after_message);
  const page = rest.slice(0, 2);
  const last = page.length ? page[page.length - 1].n : cursor.after_message;
  return {
    items: page,
    cursor: { after_event: 0, after_message: last },
    more: rest.length > 2,
    replying: projectId === "a" && rest.length <= 2 ? [{ name: "Iris" }] : [],
    waiting: projectId === "a" ? [{ kind: "approval", run_id: "r" + rooms.a.length }] : [],
  };
};
(async () => {
  const reader = new RoomReader(fetchPage);
  const out = {};
  out.first = (await reader.read("a")).items.map((i) => i.n);
  out.replying = reader.replying.map((r) => r.name);
  out.waiting = reader.waiting.map((w) => w.run_id);
  rooms.a.push({ id: "m:6", n: 6 });
  out.second = (await reader.read("a")).items.map((i) => i.n);
  out.waitingLater = reader.waiting.map((w) => w.run_id);
  out.other = (await reader.read("b")).items.map((i) => i.n);
  out.waitingOther = reader.waiting;
  out.asked = asked;
  console.log(JSON.stringify(out));
})();
"""


def run() -> dict:
    done = subprocess.run(  # noqa: S603 - a fixed harness and the shipped script
        [NODE or "node", "-e", HARNESS, str(SCRIPT)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(done.stdout)


def test_a_room_is_read_past_its_first_page() -> None:
    assert run()["first"] == [1, 2, 3, 4, 5]


def test_a_refresh_asks_only_for_what_is_new_and_appends_it() -> None:
    got = run()
    assert got["second"] == [1, 2, 3, 4, 5, 6]
    refreshes = [after for room, after in got["asked"] if room == "a"][3:]
    assert refreshes[0] == 5


def test_who_is_replying_comes_from_the_latest_page() -> None:
    assert run()["replying"] == ["Iris"]


def test_what_the_room_waits_on_is_replaced_by_each_read_not_added_to() -> None:
    got = run()
    assert got["waiting"] == ["r5"]
    assert got["waitingLater"] == ["r6"]
    assert got["waitingOther"] == []


def test_another_room_starts_again_from_nothing() -> None:
    got = run()
    assert got["other"] == [90]
    assert ["b", 0] in got["asked"]

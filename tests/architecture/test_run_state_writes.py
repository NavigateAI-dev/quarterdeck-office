"""A run's state is written in one place.

`app/run_state.py` appends each change to the run's log and projects it onto
`runs.state_json` in the same transaction. A write anywhere else would change
the state without the log, and the state could no longer be rebuilt from it.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
WRITER = APP / "run_state.py"
# `state_json=` in an update's values, or `.state_json =` on a loaded run.
WRITES = re.compile(r"\bstate_json\s*=(?!=)|\.state_json\s*=(?!=)")
# The column itself, declared on the model.
DECLARED = APP / "db" / "models" / "work.py"


def test_only_run_state_writes_a_runs_state() -> None:
    found = []
    for path in sorted(APP.rglob("*.py")):
        if path in (WRITER, DECLARED):
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if WRITES.search(line.split("#", 1)[0]):
                found.append(f"{path.relative_to(APP.parent)}:{number}: {line.strip()}")
    assert found == [], "write a run's state through app/run_state.py:\n" + "\n".join(found)

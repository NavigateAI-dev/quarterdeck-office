"""Architecture gates for the reshape.

These are ratchets: they record the structure as it was on 13 Oct 2026 and fail
when it gets worse. Each later stage tightens a baseline below when it improves
the number; never loosen one to make a change pass.
"""

import os
import subprocess
import sys

from scripts.arch_metrics import (
    ROOT,
    cycles,
    edges,
    file_lines,
    local_import_count,
    module_name,
    python_files,
)

# The import cycles that exist today, counting imports made inside functions.
# A cycle may shrink or disappear; it may not grow or appear.
KNOWN_CYCLES: list[frozenset[str]] = [
    frozenset(
        {"app.accounts", "app.mcp.calling", "app.mcp.client", "app.mcp.oauth", "app.mcp.store"}
    ),
    frozenset({"app.tools.fetch", "app.tools.fetch_service", "app.tools.platforms"}),
]

# `app` imports made inside a function body, which hide cycles from mypy.
LOCAL_IMPORTS_BASELINE = 38

# Files over the size budget today, frozen at their current length.
MAX_LINES = 1000
FROZEN_FILES = {}


def test_module_level_imports_have_no_cycles() -> None:
    assert cycles(edges(), include_local=False) == []


def test_no_new_or_growing_import_cycles() -> None:
    found = [frozenset(component) for component in cycles(edges(), include_local=True)]
    new = [sorted(c) for c in found if not any(c <= known for known in KNOWN_CYCLES)]
    assert not new, f"new or grown import cycle(s): {new}"


def test_function_local_imports_do_not_grow() -> None:
    count = local_import_count()
    assert count <= LOCAL_IMPORTS_BASELINE, (
        f"{count} function-local app imports (baseline {LOCAL_IMPORTS_BASELINE}); "
        "import at module level, or move the code so the dependency points one way"
    )


def test_files_stay_within_size_budget() -> None:
    sizes = file_lines()
    too_big = {
        path: lines
        for path, lines in sizes.items()
        if lines > MAX_LINES and lines > FROZEN_FILES.get(path, MAX_LINES)
    }
    assert not too_big, f"over {MAX_LINES} lines or grown past their frozen size: {too_big}"


def test_frozen_list_has_no_stale_entries() -> None:
    sizes = file_lines()
    stale = [path for path in FROZEN_FILES if sizes.get(path, 0) <= MAX_LINES]
    assert not stale, f"now within budget, remove from FROZEN_FILES: {stale}"


def test_kernel_imports_nothing_else_from_app() -> None:
    """The kernel holds no feature logic, so it depends on nothing."""
    leaks = [
        f"{edge.source} -> {edge.target}"
        for edge in edges()
        if edge.source.startswith("app.kernel") and not edge.target.startswith("app.kernel")
    ]
    assert not leaks, leaks


def test_kernel_stays_small() -> None:
    kernel = sum(lines for path, lines in file_lines().items() if path.startswith("app/kernel/"))
    assert kernel <= 500, f"kernel is {kernel} lines; budget 500"


def test_only_delivery_code_imports_the_http_layer() -> None:
    """The run engine and the office's domain never import app.api."""
    allowed = ("app.api", "app.main", "app.plugins")
    leaks = sorted(
        {
            f"{edge.source} -> {edge.target}"
            for edge in edges()
            if edge.target.startswith("app.api") and not edge.source.startswith(allowed)
        }
    )
    assert not leaks, leaks


IMPORT_ALONE = """
import importlib, sys
failed = []
for name in sys.argv[1:]:
    for loaded in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[loaded]
    try:
        importlib.import_module(name)
    except ImportError as error:
        failed.append(f"{name}: {error}")
print("\\n".join(failed))
"""


def test_every_module_imports_on_its_own() -> None:
    """A package's `__init__` runs before its submodule, which the static graph
    above does not see; importing each module first catches that cycle.

    The modules are shared out over a few interpreters, which run at once.
    """
    names = [module_name(path) for path in python_files()]
    workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    running = [
        subprocess.Popen(  # noqa: S603 - this interpreter, module names from the tree
            [sys.executable, "-c", IMPORT_ALONE, *names[start::workers]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=ROOT,
        )
        for start in range(workers)
    ]
    failed = []
    for process in running:
        out, err = process.communicate()
        assert process.returncode == 0, err
        failed.append(out.strip())
    assert "\n".join(line for line in failed if line) == ""

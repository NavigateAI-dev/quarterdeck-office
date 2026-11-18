"""Measure the office's structure: the reshape's metrics M1–M4.

    python -m scripts.arch_metrics          # readable summary
    python -m scripts.arch_metrics --json   # for pasting into a stage's status

M1 the largest file, M2 the largest function, M3 import cycles (counting imports
made inside functions, which hide cycles from mypy), M4 how many of those
function-local imports there are. The architecture tests use the same functions,
so the numbers and the gates cannot disagree. Standard library only.
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "app"


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    local: bool  # made inside a function body


def python_files(root: Path = ROOT) -> list[Path]:
    return sorted((root / PACKAGE).rglob("*.py"))


def module_name(path: Path, root: Path = ROOT) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve(name: str, known: set[str]) -> str | None:
    """The longest known module that `name` refers to (a module or a name in one)."""
    while name:
        if name in known:
            return name
        name = name.rpartition(".")[0]
    return None


def _is_type_checking(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(tree: ast.Module) -> list[tuple[list[str], bool]]:
    """Per `app` import statement that runs: (dotted names, made inside a function)."""
    found: list[tuple[list[str], bool]] = []

    def visit(node: ast.AST, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child):
                for orelse in child.orelse:
                    visit(orelse, in_function)
                continue
            local = in_function or isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
            )
            if isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                if child.module == PACKAGE or child.module.startswith(PACKAGE + "."):
                    names = [f"{child.module}.{alias.name}" for alias in child.names]
                    found.append((names, in_function))
            elif isinstance(child, ast.Import):
                names = [
                    alias.name
                    for alias in child.names
                    if alias.name == PACKAGE or alias.name.startswith(PACKAGE + ".")
                ]
                if names:
                    found.append((names, in_function))
            visit(child, local)

    visit(tree, False)
    return found


def edges(root: Path = ROOT) -> list[Edge]:
    files = python_files(root)
    known = {module_name(path, root) for path in files}
    result: list[Edge] = []
    for path in files:
        source = module_name(path, root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for names, local in _imports(tree):
            for name in names:
                target = _resolve(name, known)
                if target and target != source:
                    result.append(Edge(source, target, local))
    return result


def cycles(all_edges: list[Edge], include_local: bool = True) -> list[list[str]]:
    """Strongly connected components with more than one module, largest first."""
    graph: dict[str, set[str]] = {}
    for edge in all_edges:
        if edge.local and not include_local:
            continue
        graph.setdefault(edge.source, set()).add(edge.target)
        graph.setdefault(edge.target, set())

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for start in sorted(graph):
        if start in index:
            continue
        work: list[tuple[str, list[str]]] = [(start, sorted(graph[start]))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, pending = work[-1]
            if pending:
                nxt = pending.pop(0)
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, sorted(graph[nxt])))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(sorted(component))
    return sorted(components, key=lambda c: (-len(c), c))


def local_import_count(root: Path = ROOT) -> int:
    """Import statements of `app` modules made inside a function body."""
    count = 0
    for path in python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        count += sum(1 for _, local in _imports(tree) if local)
    return count


def file_lines(root: Path = ROOT) -> dict[str, int]:
    return {
        path.relative_to(root).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for path in python_files(root)
    }


def largest_functions(root: Path = ROOT, top: int = 5) -> list[tuple[str, int]]:
    sizes: list[tuple[str, int]] = []
    for path in python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.end_lineno:
                sizes.append((f"{relative}:{node.name}", node.end_lineno - node.lineno + 1))
    return sorted(sizes, key=lambda item: -item[1])[:top]


def own_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """A function's lines, not counting the functions nested inside it."""
    total = (node.end_lineno or node.lineno) - node.lineno + 1
    for child in ast.walk(node):
        if child is not node and isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            if child.end_lineno and not any(
                outer is not node
                and outer is not child
                and isinstance(outer, ast.FunctionDef | ast.AsyncFunctionDef)
                and outer.lineno < child.lineno <= (outer.end_lineno or 0)
                for outer in ast.walk(node)
            ):
                total -= child.end_lineno - child.lineno + 1
    return total


def largest_own_functions(root: Path = ROOT, top: int = 5) -> list[tuple[str, int]]:
    """M2b: the biggest functions by their own lines, nested functions excluded."""
    sizes: list[tuple[str, int]] = []
    for path in python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                sizes.append((f"{relative}:{node.name}", own_lines(node)))
    return sorted(sizes, key=lambda item: -item[1])[:top]


def measure(root: Path = ROOT) -> dict[str, object]:
    sizes = file_lines(root)
    biggest = sorted(sizes.items(), key=lambda item: -item[1])[:5]
    all_edges = edges(root)
    with_local = cycles(all_edges, include_local=True)
    return {
        "M1_largest_files": biggest,
        "M2_largest_functions": largest_functions(root),
        "M2b_largest_own_functions": largest_own_functions(root),
        "M3_cycles_with_local_imports": [len(c) for c in with_local],
        "M3_cycles_module_level": [len(c) for c in cycles(all_edges, include_local=False)],
        "M4_function_local_imports": local_import_count(root),
        "files_over_1000_lines": sum(1 for lines in sizes.values() if lines > 1000),
    }


def main(argv: list[str]) -> int:
    result = measure()
    if "--json" in argv:
        print(json.dumps(result, indent=2))
        return 0
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""An agent that uses the office's browser for one plan step.

The page is told to it as text: the address, the title, what the page says,
and a numbered list of what can be pressed or filled. It answers with one
action at a time, or with what it found. Pressing what signs in, submits,
sends, pays, books or deletes, and typing a password, stops the task and asks
the owner; their allow lasts for that task. What it found is returned in the
research report's shape, so the rest of the work reads it as research.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field

from app.agents.base import AgentOutputError, BaseAgent
from app.browser.client import BrowserUnavailable, ExchangeBrowser
from app.browser.safety import clean_url, needs_owner, url_allowed
from app.config import Settings
from app.context import ProjectContext
from app.events import InvocationRecorder
from app.gateway.direct import Completer
from app.schemas.agent_output import StrictModel

logger = logging.getLogger("quarterdeck.browser")

# Steps a browse step may take, by the plan step's effort.
STEPS = {"light": 8, "standard": 15, "deep": 25}
ELEMENTS_SHOWN = 120
PAGE_TEXT = 5000
# A move that failed this often is not made again; this many failures in a
# row and the step stops as stuck.
REPEATS = 2
IN_A_ROW = 3


class BrowserMove(StrictModel):
    """One move in the office's browser, or the end of the step."""

    thought: str = Field(default="", max_length=600)
    action: Literal["goto", "click", "type", "select", "scroll", "back", "done"]
    # The number of the element on the page, for click, type and select.
    element: int | None = Field(default=None, ge=1, le=500)
    # The address for goto, the words for type, the option for select.
    text: str = Field(default="", max_length=1000)
    # Press Enter after typing, as a search box wants.
    submit: bool = False
    direction: Literal["down", "up"] = "down"
    # When done: the answer, and what was found that supports it.
    answer: str = Field(default="", max_length=4000)
    findings: list[str] = Field(default_factory=list, max_length=12)


INSTRUCTIONS = """You are the office's researcher, using the office's own browser to do one step
of a task: what the step's brief asks, on the web, as a person would in a browser.
You are shown the page as text and a numbered list of what can be pressed or filled.
Answer with one action at a time:
- goto: open an address (put it in text). Start here when the page is blank.
- click: press element number N.
- type: put text into element N; submit true to press Enter (a search box).
- select: choose the option named in text from element N.
- scroll: down or up, to see more of the page.
- back: the page before.
- done: when the brief is answered or cannot be: answer in a few sentences, with
  findings, each a fact you saw, saying on which page.
Only report what the pages showed. Never invent results. Do not sign in, create
accounts, buy, book, send, post or submit forms unless the brief asks for exactly
that; the office asks the owner first, and you may be refused. Never type a
password. If a page blocks you (a login wall, a captcha), say so and try another
source, or finish with what you have."""


def page_for_agent(page: dict[str, Any]) -> str:
    """The page as the agent reads it: where it is, what it says, what it holds."""
    if not page or not page.get("url") or page.get("url") == "about:blank":
        return "The browser is open on a blank page. Start with goto."
    lines = [
        f"Address: {clean_url(str(page.get('url') or ''))}",
        f"Title: {page.get('title') or ''}",
    ]
    shown = []
    for element in (page.get("elements") or [])[:ELEMENTS_SHOWN]:
        kind = element.get("type") or element.get("role") or element.get("tag")
        label = element.get("label") or element.get("href") or ""
        extra = f" (options: {element['value']})" if element.get("tag") == "select" else ""
        if element.get("tag") in ("input", "textarea") and element.get("value"):
            extra = f" (now: {element['value']})"
        where = "" if element.get("in_view") else " [below]"
        shown.append(f"[{element.get('n')}] {kind}: {label}{extra}{where}")
    lines.append("What can be pressed or filled:\n" + ("\n".join(shown) or "(nothing)"))
    lines.append("What the page says:\n" + str(page.get("text") or "")[:PAGE_TEXT])
    return "\n\n".join(lines)


def move_key(
    page: dict[str, Any], action: dict[str, Any], element: dict[str, Any] | None
) -> tuple[str, str]:
    """A move as the same move, whatever number its element has this time,
    and how to say it: "click 'Case studies' on loot-drop.io"."""
    host = urlsplit(str(page.get("url") or "")).hostname or "the page"
    kind = str(action.get("kind") or "")
    if kind == "goto":
        target = str(action.get("text") or "")[:200]
        return f"goto|{target}", f"open {target}"
    label = str((element or {}).get("label") or (element or {}).get("href") or "")[:80]
    if not label:
        label = f"element {action.get('element')}" if action.get("element") else ""
    if kind == "scroll":
        return f"scroll|{host}|{page.get('url')}|{action.get('direction')}", f"scroll on {host}"
    if kind == "back":
        return f"back|{page.get('url')}", f"go back from {host}"
    words = f" with '{str(action.get('text') or '')[:40]}'" if kind in ("type", "select") else ""
    return f"{kind}|{host}|{label}|{action.get('text') or ''}", f"{kind} '{label}'{words} on {host}"


def failed_moves(failures: dict[str, dict[str, Any]]) -> str:
    """What failed earlier in this task, so it is not tried again."""
    if not failures:
        return ""
    listed = "\n".join(
        f"- {f.get('move')}: {f.get('error') or 'failed'} ({f.get('times')} times)"
        for f in list(failures.values())[-12:]
    )
    head = "Moves that failed earlier in this task. Do not repeat them; find another way:"
    return f"{head}\n{listed}\n\n"


def stuck_at(described: str, failure: dict[str, Any], page: dict[str, Any]) -> dict[str, Any]:
    """What the owner is told when the browser cannot get on."""
    return {
        "what": described,
        "error": str(failure.get("error") or ""),
        "tries": int(failure.get("times") or 0),
        "url": clean_url(str(page.get("url") or "")),
        "title": str(page.get("title") or "")[:200],
    }


def element_of(page: dict[str, Any], number: int | None) -> dict[str, Any] | None:
    if number is None:
        return None
    return next((e for e in page.get("elements") or [] if e.get("n") == number), None)


def as_research(
    brief: str, move: BrowserMove | None, visited: list[dict[str, str]]
) -> dict[str, Any]:
    """What the step found, in the research report's shape the work reads."""
    sources = [
        {"title": v["title"][:200] or v["url"], "url": v["url"], "source_type": "secondary"}
        for v in visited
        if url_allowed(v["url"])
    ][:12]
    answer = (move.answer if move else "").strip()
    findings = [f for f in (move.findings if move else []) if f.strip()]
    if not answer:
        answer = "The browser step ended before it found an answer."
    return {
        "research_question": brief[:1000],
        "answers": [],
        "blocked_sources": [],
        "key_findings": findings or [answer],
        "evidence": [f"{v['title'] or v['url']} ({v['url']})" for v in visited][:12]
        or ["The office browser visited no page."],
        "contradictory_evidence": [],
        "unknowns": [] if move else ["The browser step did not finish."],
        "implications": [answer],
        "sources": sources
        or [
            {
                "title": "The office browser",
                "url": "https://example.com/",
                "source_type": "secondary",
            }
        ],
        "confidence": "medium" if move and findings else "low",
    }


class BrowserAgent(BaseAgent[BrowserMove]):
    """The researcher, at the office's browser."""

    role = "researcher"
    output_schema = BrowserMove
    instructions = INSTRUCTIONS

    def __init__(
        self,
        gateway: Completer,
        settings: Settings,
        recorder: InvocationRecorder | None = None,
    ) -> None:
        super().__init__(gateway, recorder, settings.structured_output_attempts)
        self.model_alias = settings.research_model_alias
        self.exchange = settings.browser_exchange
        # Set per run by the worker: the owner's rules, and where steps are kept.
        self.gate: Any = None
        self.run_id: Any = None
        self.project_id: Any = None
        self.on_step: Callable[[dict[str, Any], bytes | None], None] | None = None
        # Where a step that stopped to ask keeps its moves for the replay.
        self.replay_dir: Path | None = None
        # Notes the owner sent since the step began, and the steps they skipped.
        self.new_notes: Callable[[], list[str]] | None = None
        self.skipped: Callable[[], set[str]] | None = None

    def _replay_path(self, brief: str) -> Path | None:
        if self.replay_dir is None:
            return None
        key = hashlib.sha256(brief.encode()).hexdigest()[:16]
        return Path(self.replay_dir) / f"replay-{key}.json"

    def _take_replay(self, brief: str) -> dict[str, Any] | None:
        """The moves made before this step stopped to ask the owner, once."""
        path = self._replay_path(brief)
        if path is None or not path.is_file():
            return None
        try:
            saved = dict(json.loads(path.read_text()))
        except (OSError, ValueError):
            saved = None
        path.unlink(missing_ok=True)
        return saved

    def _keep_replay(self, brief: str, held: dict[str, Any]) -> Path | None:
        path = self._replay_path(brief)
        if path is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(held))
        return path

    def _failures_path(self) -> Path | None:
        return None if self.replay_dir is None else Path(self.replay_dir) / "failures.json"

    def _failures(self) -> dict[str, dict[str, Any]]:
        """Moves that failed earlier in this task, kept across pauses."""
        path = self._failures_path()
        if path is None or not path.is_file():
            return {}
        try:
            return dict(json.loads(path.read_text()))
        except (OSError, ValueError):
            return {}

    def _keep_failures(self, failures: dict[str, dict[str, Any]]) -> None:
        path = self._failures_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(failures))

    async def browse(
        self,
        brief: str,
        context: ProjectContext,
        effort: str = "standard",
        notes: str = "",
        step_id: str = "",
    ) -> dict[str, Any]:
        """Do one step in the browser and return what it found.

        Stopped to ask the owner, it keeps the moves it made; once they allow
        it, the step starts again and replays those moves without asking the
        model, then makes the move the owner allowed and carries on.

        It does not go round in circles: a move that failed twice is not
        made again, in this step or a later one, and after three failures in a
        row the step stops as stuck, with what it found, so the owner can be
        asked. Notes the owner sends while it works are read before each move.
        """
        if self.exchange is None:
            raise AgentOutputError(self.role, "browser_not_running")
        browser = ExchangeBrowser(self.exchange)
        visited: list[dict[str, str]] = []
        history: list[str] = []
        made: list[dict[str, Any]] = []
        final: BrowserMove | None = None
        queued: list[dict[str, Any]] = []
        saved = self._take_replay(brief)
        if saved:
            queued = [m | {"replayed": True} for m in saved.get("moves") or []]
            if saved.get("pending"):
                queued.append(dict(saved["pending"]))
            history = list(saved.get("history") or [])
            visited = list(saved.get("visited") or [])
        limit = STEPS.get(effort, STEPS["standard"])
        asked = 0
        failures = self._failures()
        in_a_row = 0
        stuck: dict[str, Any] | None = None
        heard: list[str] = [notes.strip()] if notes.strip() else []
        try:
            page = (await browser.open()).get("page") or {}
            while asked < limit:
                if self.skipped is not None and step_id and step_id in self.skipped():
                    # The owner skipped this step while it ran.
                    break
                fresh = self.new_notes() if self.new_notes is not None else []
                if fresh:
                    heard.extend(fresh)
                    history.append(f"(The owner sent a note: {fresh[-1][:200]})")
                thought = ""
                if queued:
                    action = queued.pop(0)
                else:
                    inputs = _move_inputs(brief, heard, failures, history, limit - asked, page)
                    move = await self.run(brief, context, inputs)
                    asked += 1
                    if move.action == "done":
                        final = move
                        break
                    thought = move.thought
                    action = {
                        "kind": move.action,
                        "element": move.element,
                        "text": move.text,
                        "submit": move.submit,
                        "direction": move.direction,
                    }
                replayed = bool(action.pop("replayed", False))
                element = element_of(page, action.get("element"))
                key, described = move_key(page, action, element)
                if not replayed and failures.get(key, {}).get("times", 0) >= REPEATS:
                    # Failed twice already: not sent again, told to choose otherwise.
                    in_a_row += 1
                    history.append(
                        f"{len(history) + 1}. {described}: not tried again, it failed "
                        f"{failures[key]['times']} times. Choose another way."
                    )
                    if in_a_row >= IN_A_ROW:
                        stuck = stuck_at(described, failures[key], page)
                        break
                    continue
                why = needs_owner(str(action["kind"]), element, bool(action.get("submit")))
                if why and self.gate is not None and not replayed:
                    replay = {"moves": made, "pending": action, "history": history}
                    self._ask_owner(
                        brief, why, action, element, page, replay | {"visited": visited}
                    )
                answer = await browser.act(action)
                page = answer.get("page") or page
                if answer.get("error"):
                    if replayed:
                        # The page has changed since; the model takes over from here.
                        queued.clear()
                    else:
                        in_a_row += 1
                        _note_failure(failures, key, described, str(answer["error"]))
                        self._keep_failures(failures)
                else:
                    made.append(action)
                    in_a_row = 0
                words = str(action.get("text") or "")[:80]
                said = f"{action['kind']} {action.get('element') or ''} {words}"
                said = said.strip() + (
                    f" (failed: {answer['error']})" if answer.get("error") else ""
                )
                if replayed:
                    continue  # Recorded the first time it was made.
                history.append(
                    f"{len(history) + 1}. {described}"
                    + (f" (failed: {answer['error']})" if answer.get("error") else "")
                    + f" → {page.get('title') or page.get('url')}"
                )
                _visit(visited, page)
                self._report_step(browser, len(history), action, said, described, thought, page)
                if in_a_row >= IN_A_ROW and answer.get("error"):
                    stuck = stuck_at(described, failures.get(key, {}), page)
                    break
        except BrowserUnavailable as gone:
            raise AgentOutputError(self.role, gone.code) from None
        finally:
            await browser.close()
        report = as_research(brief, final, visited)
        if stuck is not None and final is None:
            report["stuck"] = stuck
        return report

    def _ask_owner(
        self,
        brief: str,
        why: str,
        action: dict[str, Any],
        element: Any,
        page: dict[str, Any],
        replay: dict[str, Any],
    ) -> None:
        """A move that needs the owner's word: what was done so far is kept for
        replay, and the task pauses until they answer (raises PolicyAsk)."""
        assert self.gate is not None  # noqa: S101 - checked by the caller
        host = urlsplit(str(page.get("url") or "")).hostname or "a site"
        label = str((element or {}).get("label") or "")[:60]
        kept = self._keep_replay(brief, replay)
        self.gate.ask_owner(
            f"browser:{host}:{label or action['kind']}"[:120],
            f"The office browser wants to {why} on {host}.",
            clean_url(str(page.get("url") or "")),
            run_id=self.run_id,
            project_id=self.project_id,
            step=brief[:200],
        )
        # Already allowed for this task: nothing to replay later.
        if kept is not None:
            kept.unlink(missing_ok=True)

    def _report_step(
        self,
        browser: Any,
        number: int,
        action: dict[str, Any],
        said: str,
        described: str,
        thought: str,
        page: dict[str, Any],
    ) -> None:
        """One move, for the task page: what was done, and the page it led to."""
        if self.on_step is None:
            return
        try:
            self.on_step(
                {
                    "step": number,
                    "action": action["kind"],
                    "said": said,
                    "label": described[:160],
                    "thought": thought[:300],
                    "url": clean_url(str(page.get("url") or "")),
                    "title": str(page.get("title") or ""),
                },
                browser.shot(page),
            )
        except Exception:  # noqa: BLE001 - a missed record never stops the step
            logger.warning('{"event":"browser_step_not_recorded"}')


def _move_inputs(
    brief: str,
    heard: list[str],
    failures: dict[str, Any],
    history: list[str],
    left: int,
    page: dict[str, Any],
) -> str:
    """What the model reads before choosing its next move."""
    notes = (
        "The owner's notes (follow them; they override the brief):\n"
        + "\n".join(f"- {n}" for n in heard)
        + "\n\n"
        if heard
        else ""
    )
    return (
        f"The step's brief:\n{brief}\n\n"
        + notes
        + failed_moves(failures)
        + "Moves so far:\n"
        + ("\n".join(history[-12:]) or "(none)")
        + "\n\n"
        + f"Moves left: {left}.\n\n"
        + page_for_agent(page)
    )


def _note_failure(failures: dict[str, Any], key: str, described: str, error: str) -> None:
    seen = failures.setdefault(key, {"move": described, "times": 0, "error": ""})
    seen["times"] = int(seen.get("times") or 0) + 1
    seen["error"] = error[:80]


def _visit(visited: list[dict[str, str]], page: dict[str, Any]) -> None:
    """A new address the step reached, once."""
    if page.get("url") and page.get("url") != "about:blank":
        if not visited or visited[-1]["url"] != clean_url(str(page["url"])):
            visited.append(
                {"url": clean_url(str(page["url"])), "title": str(page.get("title") or "")}
            )

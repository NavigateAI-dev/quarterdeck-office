import copy
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from langgraph.graph.state import CompiledStateGraph

from app.agents.base import AgentOutputError, BaseAgent
from app.approvals import ApprovalScope
from app.context import ProjectContext
from app.failover import reason_for
from app.judgments import Judge, JudgmentUnavailable
from app.orchestrator.state import AnalysisState
from app.research.objective import strip_instructions
from app.runtimes.engineer import SANDBOX_FAULT
from app.schemas import PlanStep, TaskKind, TaskPlan
from app.schemas.engineering import EngineeringStatus

STOP_NODE = "stopped"
logger = logging.getLogger("quarterdeck.graph")


ANALYSIS_WORKFLOW = "v0_analysis"
# A research task an owner gave the researcher agent directly (from chat).
RESEARCH_WORKFLOW = "agent_research"
WORKFLOWS = frozenset({ANALYSIS_WORKFLOW, RESEARCH_WORKFLOW})
# One lead agent sizing the work; runs in app.lead.run, not this graph.
LEAD_WORKFLOW = "lead"
RUN_WORKFLOWS = WORKFLOWS | {LEAD_WORKFLOW}


class ApprovalGate(Protocol):
    """How the graph records and reads a human decision.

    The graph never decides. It records a request and reads back a durable
    decision, so authorization always comes from the record rather than from
    replayable graph state.
    """

    def open(self, objective: str, synthesis: dict[str, Any]) -> tuple[str, str]:
        """Persist the proposal. Returns the approval ID and its scope digest."""
        ...

    def decision_for(self, approval_id: str) -> tuple[str, ApprovalScope] | None:
        """Return the recorded decision and the exact scope it authorized."""
        ...


def coverage(state: AnalysisState) -> dict[str, Any]:
    """How much of what was asked the run actually delivered.

    Built from what the run itself recorded: the plan's acceptance criteria,
    the critic's last verdict and the issues it raised, and the research's own
    per-question statuses. A task that met two criteria of eight must say so in
    its first line rather than read as finished work.
    """
    plan = cast(dict[str, Any], dict(state).get("plan") or {})
    critique = cast(dict[str, Any], dict(state).get("critique") or {})
    research = cast(dict[str, Any], dict(state).get("research") or {})
    answers = [a for a in research.get("answers") or [] if isinstance(a, dict)]
    answered = sum(1 for a in answers if str(a.get("status")) == "answered")
    unmet = [
        str(issue)
        for field in ("critical_issues", "required_corrections", "missing_evidence")
        for issue in critique.get(field) or []
    ]
    return {
        "acceptance_criteria": [str(c) for c in plan.get("acceptance_criteria") or []],
        "questions_asked": len(answers),
        "questions_answered": answered,
        "unanswered_questions": [
            str(a.get("question")) for a in answers if str(a.get("status")) != "answered"
        ],
        "last_review_verdict": str(critique.get("verdict") or "none"),
        "unmet_by_review": unmet[:12],
        "blocked_sources": [
            f"{b.get('url')}: {b.get('status')}"
            for b in research.get("blocked_sources") or []
            if isinstance(b, dict)
        ],
        "stopped_reason": str(state.get("stopped_reason") or ""),
    }


def final_report(scope: ApprovalScope, engineering: dict[str, Any] | None) -> dict[str, Any]:
    """The user-facing result of an approved task, from evidence only.

    Connects what was approved (by digest) to what happened: retained patch,
    test outcome, publication, and everything still unresolved.
    """
    if engineering is None:
        return {
            "objective": scope.objective,
            "recommendation": scope.recommendation,
            "scope_digest": scope.digest(),
            "outcome": "decision_recorded",
            "branch": None,
            "changed_files": [],
            "tests": "not_run",
            "patch_retained": False,
            "merge_request": None,
            "unresolved": ["no_engineering_target", *scope.known_uncertainties],
        }
    tests = engineering.get("tests") or {}
    return {
        "objective": scope.objective,
        "recommendation": scope.recommendation,
        "scope_digest": scope.digest(),
        "outcome": engineering.get("status", "failed"),
        "branch": engineering.get("branch"),
        "changed_files": list(engineering.get("changed_files") or []),
        "tests": tests.get("status", "not_run"),
        "patch_retained": bool(engineering.get("diff")),
        "merge_request": engineering.get("pull_request_url"),
        "unresolved": [
            *[str(error) for error in engineering.get("errors") or []],
            *scope.known_uncertainties,
        ],
    }


@dataclass(frozen=True)
class AgentSet:
    """The analysis roles. The Chief plans and synthesizes.

    The deliverable step has one agent per task kind: the chain is fixed, but
    the contract the deliverable must satisfy is the plan's choice.
    """

    planner: BaseAgent[Any]
    researcher: BaseAgent[Any]
    strategist: BaseAgent[Any]
    critic: BaseAgent[Any]
    chief: BaseAgent[Any]
    summarist: BaseAgent[Any] | None = None
    # On call at three moments when the owner turns it on; never writes.
    advisor: BaseAgent[Any] | None = None
    # The researcher at the office's own browser, when the owner turned it on.
    browser: Any = None
    # Reviews a draft plan before any work starts.
    plan_reviewer: BaseAgent[Any] | None = None
    # Reads a coding task's diff before the owner merges it (36c).
    code_reviewer: BaseAgent[Any] | None = None
    # Installed skills a plan step may use, by id: name, description,
    # instructions, roles, source. Only the step that names one is given it.
    skills: dict[str, dict[str, Any]] = field(default_factory=dict)

    def deliverable_for(self, kind: str) -> BaseAgent[Any]:
        if kind == TaskKind.RESEARCH_SUMMARY and self.summarist is not None:
            return self.summarist
        return self.strategist


MOMENTS = {
    "before_plan": "Before the plan is committed: is this the right course?",
    "repeated_failure": "The same kind of failure has repeated: what should change?",
    "before_done": "Before the work is declared done: does it answer the ask?",
}


def advisor_view(state: Any, moment: str, plan_now: dict[str, Any]) -> str:
    """The whole task as the advisor reads it, bounded by ADVISOR_CHARS."""
    status = state.get("step_status") or {}
    results = state.get("step_results") or {}
    steps = [s for s in plan_now.get("steps") or [] if isinstance(s, dict)]
    progress = "\n".join(
        f"- {s.get('id')} ({s.get('kind')}, {status.get(s.get('id'), PENDING)}): {s.get('brief')}"
        for s in steps
    )
    parts = [
        f"Moment: {moment}. {MOMENTS.get(moment, '')}",
        render("Plan", plan_now),
        f"Where the steps stand:\n{progress}" if progress else "",
        f"Replans so far: {int(state.get('replans') or 0)}; revisions: "
        f"{int(state.get('revision_count') or 0)}.",
        render("What the steps found", results) if results else "",
        render("Research", state.get("research")),
        render("Strategy", state.get("strategy")),
        render("Critique", state.get("last_critique") or state.get("critique")),
    ]
    earlier = [e for e in state.get("advice") or [] if e.get("advice")]
    if earlier:
        parts.append(render("Your earlier advice", {"advice": earlier[-2:]}))
    text = "\n\n".join(part for part in parts if part)
    if len(text) > ADVISOR_CHARS:
        text = text[:ADVISOR_CHARS] + "\n[… the rest is cut to keep the consultation bounded]"
    return text


def advice_for_chief(entry: dict[str, Any] | None) -> str:
    """The advisor's word as the Chief reads it: advice, not an instruction."""
    if not entry or not (entry.get("advice") or entry.get("watch_for")):
        return ""
    lines = "\n".join(f"- {item}" for item in entry.get("advice") or [])
    watch = "\n".join(f"- {item}" for item in entry.get("watch_for") or [])
    said = (
        f"\n\nAdvisor (on call; apply what holds up, the decision is yours):\n{lines}"
        if lines
        else ""
    )
    if watch:
        said += f"\n\nThe advisor says to watch for:\n{watch}"
    return said


class TeamBoard(Protocol):
    """The task's room (T): every agent posts while it works and reads before it
    commits. Kept on the run's record by the dispatcher; a post said twice (a
    step run again after a pause) is kept once, by its key."""

    def post(self, entry: dict[str, Any]) -> None: ...

    def read(self) -> list[dict[str, Any]]: ...


POST_CHARS = 600
# The room as a step reads it: the most recent posts, within a size.
ROOM_POSTS = 14
ROOM_CHARS = 3500
# Huddles after waves, at most, per task: each one is a few model calls.
MAX_HUDDLES = 4
# Steps run again to answer a challenge, and offers taken as steps.
MAX_RERUNS = 2
MAX_OFFERS = 2
ROOM_KINDS = ("claim", "finding", "doubt", "challenge", "need", "offer", "note", "advice", "owner")


def room_line(post: dict[str, Any]) -> str:
    author = post.get("author_role") or "?"
    if post.get("step_id"):
        author += f" · {post['step_id']}"
    towards = f" → {post['to_step']}" if post.get("to_step") else ""
    return f"- [{author}] {post.get('kind')}{towards}: {post.get('text')}"


def room_text(lines: list[str]) -> str:
    text = "\n".join(lines)
    return text if len(text) <= ROOM_CHARS else text[-ROOM_CHARS:]


def short(text: str, limit: int = 80) -> str:
    """A step named by the first clause of its brief, short enough for a post."""
    first = re.split(r"[，,。；;：:\n]|(?<=[a-z])\. ", str(text or "").strip(), maxsplit=1)[
        0
    ].strip()
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def drafted(produced: dict[str, Any]) -> str:
    """What a piece of writing says first, for the room."""
    for key in ("recommendation", "summary", "answer", "question"):
        value = produced.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "A draft is written."


class AdviceMemory(Protocol):
    """Where each consultation is kept the moment it is made, outside the graph.

    A node that fails after consulting (a task paused at its limit) loses its
    update, so on resuming it would ask again: the owner's task paid for the
    same advice twice. The record survives the pause.
    """

    def recall(self) -> list[dict[str, Any]]: ...

    def keep(self, entries: list[dict[str, Any]]) -> None: ...


def advice_key(state: Any, moment: str, plan_now: dict[str, Any]) -> str:
    """What a consultation was about: the same key is the same question."""
    critique = state.get("critique")
    about = {
        "moment": moment,
        "plan": plan_now,
        "step_status": state.get("step_status") or {},
        "replans": int(state.get("replans") or 0),
        "revisions": int(state.get("revision_count") or 0),
        "critique": critique.get("verdict") if isinstance(critique, dict) else None,
    }
    encoded = json.dumps(about, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()[:24]


def render(label: str, payload: dict[str, Any] | None) -> str:
    if payload is None:
        return ""
    # ensure_ascii=False: an agent reading \u escapes instead of the actual
    # text is reading a worse version of its own upstream artifact.
    return f"{label}:\n{json.dumps(payload, indent=2, ensure_ascii=False)}"


def upstream(state: AnalysisState, *labels: str) -> str:
    """Render the named upstream artifacts as prompt input."""
    sections = []
    for label in labels:
        payload = cast(dict[str, Any] | None, dict(state).get(label))
        sections.append(render(label.capitalize(), payload))
    return "\n\n".join(section for section in sections if section)


def building(state: AnalysisState) -> bool:
    """Whether the task builds something."""
    plan = cast(dict[str, Any], dict(state).get("plan") or {})
    return str(plan.get("task_kind") or "") == TaskKind.BUILD


def build_scope_text(spec: dict[str, Any]) -> str:
    """The build spec as the coding agent and the owner read it."""
    features = "\n".join(f"- {item}" for item in spec.get("features") or [])
    checks = "\n".join(f"- {item}" for item in spec.get("checks") or [])
    return (
        f"{spec.get('summary', '')}\n\nWhat it does:\n{features}\n\n"
        f"How it is made: {spec.get('stack', '')}\n\n"
        f"How to open or run it: {spec.get('how_to_run', '')}\n\n"
        f"Checks before it is done:\n{checks}"
    ).strip()


BUILD_ANSWER = (
    "\n\nThis task builds something. Write the answer as the proposal to build it, for "
    "the owner to read before pressing Start building: lead says in plain words what will "
    "be made; deliverable_body lists what it does and how they will use it; next_steps says "
    "to press Start building and what happens then (a coding agent writes it in the office's "
    "own project folder, checks it, and hands over the files, a download and, for a website, "
    "a preview). Set proposes_action true. Set proposed_engineering_scope to the full build "
    "spec from the plan's build_spec, refined by what the work found, and "
    "engineering_acceptance_criteria to its checks. Do not write the code yourself."
)


# What a build's answer says once the build has run, by how it ended, in
# the task's own language (fix 1, 10 Oct: an English line sat on a Chinese plan).
BUILD_ENDINGS = {
    "runtime:stopped_by_owner": "The build was stopped, as you asked. Nothing was kept.",
    "build_unfinished": "The build did not finish: the coding agent stopped early, twice. "
    "Nothing was kept.",
    "tests_failed": "The build did not pass its own checks, even after a round to fix them, "
    "so nothing was kept.",
    "code_execution_disabled": "Nothing was built: code execution is off for this office.",
    "sandbox": "The coding agent's sandbox failed while it worked, and the build could not be "
    "finished. Nothing was kept.",
}
BUILD_ENDINGS_ZH = {
    "runtime:stopped_by_owner": "已按你的要求停止构建，没有保留任何内容。",
    "build_unfinished": "构建没有完成：编码代理两次提前停止，没有保留任何内容。",
    "tests_failed": "构建没有通过自身的检查（已给过一轮修复机会），因此没有保留任何内容。",
    "code_execution_disabled": "没有构建：本办公室已关闭代码执行。",
    "sandbox": "编码代理在工作时其沙箱发生故障，构建无法完成，没有保留任何内容。",
}
CJK = re.compile(r"[\u3400-\u9fff]")


def chinese(state: AnalysisState) -> bool:
    """Whether the task was asked in Chinese, by its objective and its answer so far."""
    current = dict(state)
    answer = cast(dict[str, Any], current.get("synthesis") or {})
    said = f"{current.get('objective') or ''} {answer.get('lead') or ''}"
    return len(CJK.findall(said)) >= 4


def build_outcome(state: AnalysisState, report: dict[str, Any]) -> dict[str, Any]:
    """The build's answer, rewritten to say what happened: it used to say
    "press Start building" long after the build had finished or failed.

    A failed build says, in the task's language, why it stopped (in the coding
    agent's own words too), what it had written, that the folder still holds
    the version from before, and what to do next. Nothing from before the
    build that no longer holds is left in it.
    """

    answer = dict(cast(dict[str, Any], dict(state).get("synthesis") or {}))
    if not answer:
        return {}
    zh = chinese(state)
    errors = [str(e) for e in report.get("errors") or []]
    said = str(report.get("summary") or "").strip()
    first = said.splitlines()[0][:300] if said else ""
    if report.get("status") == EngineeringStatus.COMPLETED:
        answer["lead"] = (
            ("已构建并通过检查。" if zh else "Built and checked. ")
            + first
            + (
                " 可在“What was built”中打开预览或下载。"
                if zh
                else " Open the preview or download it under What was built."
            )
        ).strip()
        answer["next_steps"] = (
            [
                "在“What was built”中打开预览或文件。",
                "下载 zip 保存，或把它发布上线。",
                "需要修改时用“Make the next version”，会在同一文件夹中继续。",
            ]
            if zh
            else [
                "Open the preview or the files under What was built.",
                "Download the zip to keep it or put it online.",
                "Ask for a change with Make the next version; it continues in the same folder.",
            ]
        )
        answer["recommendation"] = first or answer.get("recommendation") or ""
    else:
        endings = BUILD_ENDINGS_ZH if zh else BUILD_ENDINGS
        key = "sandbox" if SANDBOX_FAULT.search(said) else ""
        key = key or next((e for e in errors if e in endings), "")
        reason = endings.get(key) or (
            "构建没有完成，没有保留任何内容。"
            if zh
            else "The build did not finish. Nothing was kept."
        )
        written = len(report.get("changed_files") or [])
        parts = [reason]
        if written:
            parts.append(
                f"编码代理写了 {written} 个文件，但都没有保存；项目文件夹仍是之前的版本。"
                if zh
                else f"The coding agent wrote {written} files, but none were kept: the project "
                "folder still holds the version from before."
            )
        # Its own words only when it worked: a build that never ran has none.
        if first and (written or key == "sandbox"):
            parts.append(
                f"编码代理的说明：“{first}”" if zh else f"What the coding agent said: “{first}”"
            )
        answer["lead"] = " ".join(parts)[:800]
        answer["recommendation"] = (
            "按“Try the build again”重新构建；或用“Make the next version”说明要改什么。"
            if zh
            else "Press Try the build again, or say what to change with Make the next version."
        )
        answer["next_steps"] = (
            [
                "按“Try the build again”：同一个构建、同一个文件夹，按下“Start building”后开始。",
                "或用“Make the next version”说明要改什么。",
            ]
            if zh
            else [
                "Press Try the build again: the same build, in the same folder; it begins when "
                "you press Start building.",
                "Or say what to change with Make the next version.",
            ]
        )
    answer["not_checked"] = ""
    return {"synthesis": answer}


def as_build_proposal(state: AnalysisState, produced: dict[str, Any]) -> dict[str, Any]:
    """A build's answer always proposes the build, with the plan's spec when
    the Chief left the scope out: what the owner approves is never empty."""
    plan = cast(dict[str, Any], dict(state).get("plan") or {})
    spec = cast(dict[str, Any], plan.get("build_spec") or {})
    scope = str(produced.get("proposed_engineering_scope") or "").strip() or build_scope_text(spec)
    checks = list(produced.get("engineering_acceptance_criteria") or []) or [
        str(c) for c in spec.get("checks") or []
    ]
    return {
        **produced,
        "proposes_action": True,
        "proposed_engineering_scope": scope,
        "engineering_acceptance_criteria": checks[:12],
    }


def proposes_action(state: AnalysisState) -> bool:
    """Whether the answer asks the owner to authorise something to be done."""
    synthesis = cast(dict[str, Any], dict(state).get("synthesis") or {})
    scope = str(synthesis.get("proposed_engineering_scope") or "").strip()
    return bool(scope) and synthesis.get("proposes_action") is not False


# Where a step stands.
PENDING, DONE, EMPTY, FAILED = "pending", "done", "empty", "failed"
# Steps that gather evidence: reading the web, or using the office's browser.
GATHERS = ("research", "browse")


def providers_down(
    contract: Any, wave: list[Any], status: dict[str, str], errors: dict[str, str]
) -> list[str]:
    """The provider errors behind a wave in which no research could run, if that
    is what happened; empty otherwise.

    Only when no research step has finished at all and every research step of
    this wave failed for a provider reason: out of credit, a refused key, or
    one that is down, rate-limited or timing out.
    """

    if any(step.kind in GATHERS and status.get(step.id) == DONE for step in contract.steps):
        return []
    tried = [step.id for step in wave if step.kind in GATHERS]
    if not tried or any(step_id not in errors for step_id in tried):
        return []
    codes = [errors[step_id] for step_id in tried]
    if not all(reason_for(code) for code in codes):
        return []
    return sorted(set(codes))


# Bounds on a step plan: replans after a failed or empty step, and waves
# that complete nothing before the work goes to review with its gaps.
MAX_REPLANS = 2
# How much of the task the advisor reads at a consultation, in characters: the
# whole course, bounded so one consultation cannot swallow the task's budget.
ADVISOR_CHARS = 24000
MAX_STALLS = 2
# How many pages a research step reads, by the effort the Chief gave it.
EFFORT_PAGES: dict[str, Callable[[int], int]] = {
    "light": lambda pages: max(1, min(pages, 2)),
    "standard": lambda pages: pages,
    # A little more, not double: doubled pages pushed research prompts past
    # what the live office's researcher model accepts, and every such step
    # was rejected.
    "deep": lambda pages: min(8, pages + 2),
}
REPLAN_RULES = (
    "Return the whole plan with its steps. Keep every step that is done exactly as it "
    "is, with the same id, brief and dependencies, so its work is kept. Replace a step "
    "that failed or found nothing with a better-aimed one under a new id, or drop it if "
    "the task can be answered without it. A replacement is narrower than what it replaces, "
    "not deeper: one question, standard effort, the most direct official source. "
    "When the critique asks for a revision, add "
    "steps aimed at exactly what it says is missing, and let the writing step depend on "
    "them so it uses what they find. Say in added_because why each new step is there. "
    "Keep the reader_brief as it is and every step in its service: add no step the reader "
    "does not need, such as checking procedures, audits or measurements nobody asked for. "
    "A teammate's offer is a suggestion; take it only when it serves the reader."
)


# Rounds of plan review before work starts.
PLAN_REVIEWS = 2


def plan_diff(before: TaskPlan, after: TaskPlan) -> str:
    """What a revision changed in the steps, in a few words; empty for nothing."""
    old = {step.id: step for step in before.steps}
    new = {step.id: step for step in after.steps}
    added = [i for i in new if i not in old]
    dropped = [i for i in old if i not in new]
    rewritten = [
        i
        for i in new
        if i in old and (new[i].brief != old[i].brief or new[i].depends_on != old[i].depends_on)
    ]
    parts = []
    if added:
        parts.append(f"{len(added)} added")
    if dropped:
        parts.append(f"{len(dropped)} dropped")
    if rewritten:
        parts.append(f"{len(rewritten)} rewritten")
    return ", ".join(parts)


# Which roles may follow a skill, by the kind of step.
STEP_ROLES = {
    "research": {"researcher"},
    "browse": {"researcher"},
    "write": {"strategist", "summarist", "chief"},
}


def left_undone(state: AnalysisState) -> list[str]:
    """The planned steps that never finished, as the owner would read them."""
    status = state.get("step_status") or {}
    if not state.get("plan"):
        return []
    return [step.brief for step in plan_of(state).steps if status.get(step.id) != DONE]


FINISHED_EARLY = (
    "\n\nThe owner finished this task early: write the answer from the work above "
    "as it stands. Do not ask for more research. State what was not done as unknowns."
)


def as_found(state: AnalysisState) -> dict[str, Any]:
    """An answer made from the notes alone, when no writing call can run.

    The owner finished the task to keep what it had found; a refused or failed
    last call must not turn that into nothing.
    """
    research = cast(dict[str, Any], dict(state).get("research") or {})
    strategy = cast(dict[str, Any], dict(state).get("strategy") or {})
    facts = [str(f) for f in research.get("key_findings") or [] if f] or [
        str(e) for e in research.get("evidence") or [] if e
    ]
    written = str(strategy.get("recommendation") or strategy.get("summary") or "").strip()
    first = [str(i) for i in research.get("implications") or [] if i][:1]
    recommendation = written or (first[0] if first else "")
    undone = [f"Not done before the task was finished: {brief}" for brief in left_undone(state)]
    return {
        "facts": facts or ["Nothing was established before the task was finished."],
        "assumptions": [],
        "inferences": [str(i) for i in research.get("implications") or [] if i],
        "recommendation": recommendation
        or "No recommendation: the task was finished before one was written.",
        "unknowns": [str(u) for u in research.get("unknowns") or [] if u] + undone,
        "proposes_action": False,
        "proposed_engineering_scope": "",
        "engineering_acceptance_criteria": [],
        "deliverable_body": [],
    }


def skills_offered(skills: dict[str, dict[str, Any]]) -> str:
    """What the Chief is told it may attach: names and descriptions, never the
    instructions, which only the step that uses a skill is given."""
    if not skills:
        return ""
    lines = []
    for skill_id, skill in sorted(skills.items()):
        kinds = [
            kind for kind, roles in STEP_ROLES.items() if roles & set(skill.get("roles") or [])
        ]
        if kinds:
            lines.append(
                f"- {skill_id} (for {' or '.join(kinds)} steps): {skill.get('description', '')}"
            )
    if not lines:
        return ""
    return (
        "\n\nSkills you may attach to a step, by id in its `skill` field. Attach one only "
        "when it fits the step's work exactly; most steps need none:\n" + "\n".join(lines)
    )


def skill_for(
    step: PlanStep, skills: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any] | None]:
    """Whether a step's skill applies, and the skill when it does."""
    if not step.skill:
        return "", None
    skill = skills.get(step.skill)
    if skill is None:
        return "unknown", None
    if not STEP_ROLES[step.kind] & set(skill.get("roles") or []):
        return "wrong_role", None
    return "applied", skill


def with_skill(worker: Any, skill: dict[str, Any]) -> Any:
    """A copy of the worker that follows the skill, told how the office differs."""
    followed = copy.copy(worker)
    followed.instructions = (
        f"{getattr(worker, 'instructions', '')}\n\n"
        f"For this step, follow the skill '{skill.get('name')}'"
        f"{' from ' + str(skill.get('source')) if skill.get('source') else ''}. The office runs "
        "no commands and writes no files: where the skill says to write a file, put that "
        "content in your answer; where it says to run something, skip that part; where it "
        "says to ask the user, work from the context and name what you would have asked. "
        f"Its instructions:\n{skill.get('instructions', '')}"
    )
    return followed


def plan_of(state: AnalysisState) -> TaskPlan:
    return TaskPlan.model_validate(state["plan"])


def limits_of(state: AnalysisState) -> str:
    """What the answer could not see, and what only the owner could say.

    Told to the synthesis so the answer states its limits plainly rather than
    the task refusing to answer at all.
    """
    plan = plan_of(state) if state.get("plan") else None
    missing = list(plan.unreachable_evidence if plan else []) + list(
        state.get("unreachable_criteria") or []
    )
    asked = [q for q in (plan.owner_questions if plan else []) if q.strip()]
    parts = []
    if missing:
        listed = "\n".join(f"- {item}" for item in missing)
        parts.append(
            "Evidence this task could not reach:\n"
            f"{listed}\nSay in the answer what it could not see and how that limits "
            "the conclusion. Do not present a public proxy as if it were that evidence."
        )
    if asked:
        listed = "\n".join(f"- {item}" for item in asked)
        parts.append(
            "Questions only the owner can answer; the answer should say how each "
            f"would change the recommendation:\n{listed}"
        )
    return "".join(f"\n\n{part}" for part in parts)


def digest_of(payload: dict[str, Any] | None) -> str:
    """A stable digest of one role's output, for comparing revisions."""
    return hashlib.sha256(
        json.dumps(payload or {}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


CONCERN_FIELDS = ("critical_issues", "missing_evidence", "required_corrections")
REPEAT_THRESHOLD = 0.6
# A concern judged at least this likely to be restated counts as unresolved.
STILL_RAISED_PROBABILITY = 0.5
# A criterion judged less likely than this to be reachable from public pages is
# reported as unreachable; conservative, so only clearly private asks are flagged.
UNREACHABLE_PROBABILITY = 0.3


def concern_items(critique: dict[str, Any] | None) -> list[str]:
    return [str(item) for field in CONCERN_FIELDS for item in (critique or {}).get(field) or []]


async def judged_repeat(
    judge: Judge | None, previous: dict[str, Any] | None, current: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """Whether the new critique leaves most earlier concerns unresolved.

    Asked per concern as a semantic judgment when a judge is configured, because
    a reworded concern shares few words with the original. Falls back to the
    lexical measure when the judge is absent or fails; the record names the
    method that decided.
    """
    if previous is None:
        return False, {"method": "none"}
    if digest_of(previous) == digest_of(current):
        return True, {"method": "identical"}
    earlier = concern_items(previous)
    if judge is not None and earlier:
        review = {field: current.get(field) or [] for field in CONCERN_FIELDS}
        try:
            probabilities = await judge.unresolved(earlier, review)
        except JudgmentUnavailable:
            return repeats(previous, current), {"method": "lexical_fallback"}
        still = sum(p >= STILL_RAISED_PROBABILITY for p in probabilities)
        return still / len(earlier) >= REPEAT_THRESHOLD, {
            "method": "typesafe",
            "still_raised": still,
            "total": len(earlier),
        }
    return repeats(previous, current), {"method": "lexical"}


def concerns(critique: dict[str, Any] | None) -> set[str]:
    """The critic's concerns as a bag of words, for comparing across cycles."""
    words: set[str] = set()
    for name in CONCERN_FIELDS:
        for item in (critique or {}).get(name) or []:
            words |= {word for word in re.findall(r"[a-z]{4,}", str(item).lower())}
    return words


def repeats(previous: dict[str, Any] | None, current: dict[str, Any] | None) -> bool:
    """Whether a critique restates concerns the revision was meant to answer.

    Real critiques are never byte-identical, so an exact comparison alone never
    fires. The measure is how much of the previous concern set comes back, not
    how similar the two are overall: a revision that leaves most of the earlier
    concerns standing has not answered them, whatever the new wording.
    """
    if previous is None:
        return False
    if digest_of(previous) == digest_of(current):
        return True
    before, after = concerns(previous), concerns(current)
    if not before:
        return False
    return len(before & after) / len(before) >= REPEAT_THRESHOLD


# How much of the first attempt's gap is carried into the second query.
MAX_FOCUS = 400


def asked_of(state: AnalysisState) -> str:
    """The work the owner asked for, without the conversation around it.

    Used wherever the objective reaches an agent. Stripping it for planning
    alone was not enough: the chief still read the original and answered the
    confirmation line at the end of it, so a research task concluded "yes,
    confirmed to proceed".
    """
    return strip_instructions(str(state["objective"]))


def unanswered(report: dict[str, Any]) -> bool:
    """Whether a research part left its question unanswered.

    Read from the part's own statuses; a part that produced no answers at all
    is judged by whether it found anything.
    """
    answers = [a for a in report.get("answers") or [] if isinstance(a, dict)]
    if answers:
        return any(str(a.get("status")) != "answered" for a in answers)
    return not (report.get("key_findings") or [])


def found_nothing(report: dict[str, Any]) -> bool:
    """Whether a research step answered none of its question at all.

    Stricter than `unanswered`: a partial answer is progress. On the live
    office, prices a vendor does not publish made every step of a comparison
    "unanswered", and the task replanned twice, redoing all its research.
    """
    answers = [a for a in report.get("answers") or [] if isinstance(a, dict)]
    if answers:
        return all(str(a.get("status")) == "unanswered" for a in answers)
    return not (report.get("key_findings") or [])


def retry_focus(report: dict[str, Any]) -> str:
    """What to look for on a second attempt: what the first said was missing."""
    wanted: list[str] = []
    for answer in report.get("answers") or []:
        if isinstance(answer, dict) and str(answer.get("status")) != "answered":
            wanted.extend(str(item) for item in answer.get("missing") or [])
    wanted.extend(str(item) for item in report.get("unknowns") or [])
    blocked = [
        str(source.get("url"))
        for source in report.get("blocked_sources") or []
        if isinstance(source, dict)
    ]
    focus = " ".join(wanted[:4])[:MAX_FOCUS]
    if blocked:
        focus += f" Avoid these, which refused us: {', '.join(blocked[:3])}."
    return focus.strip()


def evidence_gap(state: AnalysisState) -> str:
    """The first evidence gap the critic named, used to widen a rerun's search."""
    critique = cast(dict[str, Any] | None, dict(state).get("critique"))
    missing = (critique or {}).get("missing_evidence") or []
    return str(missing[0]) if missing else ""


def context_of(state: AnalysisState) -> ProjectContext:
    return ProjectContext.model_validate(state["context"])


async def _invoke(
    agent: BaseAgent[Any], objective: str, state: AnalysisState, inputs: str, focus: str = ""
) -> dict[str, Any]:
    output = await agent.run(objective, context_of(state), inputs, focus)
    # mode="json" so nested models become JSON-safe values a checkpoint can hold.
    return cast(dict[str, Any], output.model_dump(mode="json"))


class RunStopped(Exception):
    """The owner paused or cancelled the run; raised at a step boundary."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


def merge_reports(question: str, parts: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Combine parallel research reports into one ResearchReport-shaped result.

    Each part answered one subquestion, so the merge keeps that structure: one
    answer per question, carrying its own status and evidence. Prefixing every
    finding with the question it came from, as this once did, made a report
    where the same sixty characters opened every line and no reader could tell
    which question had actually been answered.
    """

    def gather(field: str) -> list[str]:
        return [str(item) for _, report in parts for item in report.get(field) or []]

    answers: list[dict[str, Any]] = []
    for part_question, report in parts:
        given = [dict(answer) for answer in report.get("answers") or []]
        if given:
            # One part answers one question. A part that answered every
            # question in the plan gives four copies of the report and
            # overflows the deliverable, so only its own answer is kept.
            own = [a for a in given if str(a.get("question", "")).strip() == part_question.strip()]
            answers.extend(own or given[:1])
            continue
        # A part that answered without using the per-question shape is still
        # one question's worth of work: its status comes from what it produced.
        findings = [str(item) for item in report.get("key_findings") or []]
        unknowns = [str(item) for item in report.get("unknowns") or []]
        answers.append(
            {
                "question": part_question,
                "status": "unanswered" if not findings else ("partial" if unknowns else "answered"),
                "answer": " ".join(findings) or "The retrieved evidence did not answer this.",
                "evidence": [str(s.get("url")) for s in report.get("sources") or []][:8],
                "missing": unknowns[:8],
            }
        )

    blocked: list[dict[str, Any]] = []
    seen_blocked: set[str] = set()
    for _, report in parts:
        for source in report.get("blocked_sources") or []:
            url = str(source.get("url"))
            if url not in seen_blocked:
                seen_blocked.add(url)
                blocked.append(dict(source))

    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, report in parts:
        for source in report.get("sources") or []:
            if str(source.get("url")) not in seen:
                seen.add(str(source.get("url")))
                sources.append(source)
    confidences = sorted({str(report.get("confidence")) for _, report in parts})
    return {
        "research_question": question,
        "answers": answers,
        "blocked_sources": blocked,
        "key_findings": gather("key_findings"),
        "evidence": gather("evidence"),
        "contradictory_evidence": gather("contradictory_evidence"),
        "unknowns": gather("unknowns"),
        "implications": gather("implications"),
        "sources": sources,
        "confidence": confidences[0]
        if len(confidences) == 1
        else "mixed: " + ", ".join(confidences),
    }


class RunFailure(Exception):
    """An analysis run stopped because a role could not produce valid output."""

    def __init__(self, node: str, code: str) -> None:
        self.node = node
        self.code = code
        super().__init__(f"{node}:{code}")


async def run_analysis(
    graph: CompiledStateGraph[AnalysisState, Any, AnalysisState, AnalysisState],
    state: AnalysisState,
    config: dict[str, Any] | None = None,
    on_node: Callable[[str, dict[str, Any]], None] | None = None,
) -> AnalysisState:
    """Execute the chain, reporting each node's update as it completes.

    Node updates are streamed rather than collected at the end, so a stage that
    succeeded before a later failure is still durably recorded by the caller.
    """
    accumulated: dict[str, Any] = dict(state) if state else {}
    try:
        async for update in graph.astream(state, config=config, stream_mode="updates"):  # type: ignore[arg-type]
            for node, values in update.items():
                if not isinstance(values, dict):
                    continue
                accumulated.update(values)
                if on_node is not None:
                    on_node(str(node), values)
    except AgentOutputError as exc:
        raise RunFailure(exc.role, exc.code) from None
    if graph.checkpointer is None:
        # Without a checkpointer the merged updates are the only record. State
        # keys use last-value semantics, so merging reproduces the final state.
        return cast(AnalysisState, accumulated)
    snapshot = await graph.aget_state(config)  # type: ignore[arg-type]
    return cast(AnalysisState, snapshot.values)

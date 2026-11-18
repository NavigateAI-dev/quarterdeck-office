from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProjectContext(BaseModel):
    """Organization and project scope supplied to every agent invocation.

    Agents receive the selected project's context, and one thing from beyond
    it: `related`, earlier answers from any project judged relevant to this
    task's question, told as what was found then.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    organization: str = Field(min_length=1, max_length=128)
    # The date the run was created, so work can state when it was researched.
    as_of: date | None = None
    project_id: UUID
    project_name: str = Field(min_length=1, max_length=128)
    background: str = Field(default="", max_length=8192)
    constraints: list[str] = Field(default_factory=list, max_length=32)
    # Owner decisions recorded for this project, newest first.
    decisions: list[str] = Field(default_factory=list, max_length=10)
    # Recommendations of this project's earlier finished runs, newest first.
    prior_outcomes: list[str] = Field(default_factory=list, max_length=5)
    # The owner's local sources granted to this project.
    local_sources: list[str] = Field(default_factory=list, max_length=50)
    # Lessons earlier runs produced and the owner approved. Approved only:
    # a candidate never reaches an agent.
    lessons: list[str] = Field(default_factory=list, max_length=12)
    # The business line this work is for, and how work is judged inside it.
    # Empty for a project that belongs to none.
    venture: str = Field(default="", max_length=128)
    doctrine: str = Field(default="", max_length=4096)
    # What this business line is trying to achieve in the window this work
    # falls in. A task that serves one is told which.
    objectives: list[str] = Field(default_factory=list, max_length=8)
    serves: str = Field(default="", max_length=255)
    # What the office already knows about the subjects this task is about.
    dossiers: list[str] = Field(default_factory=list, max_length=8)
    # What the owner wants at the end, said before the work starts.
    deliverable: str = Field(default="", max_length=600)
    # What the task this one continues established, when the owner asked a
    # follow-up. Told as what was found, never as settled fact.
    continues: list[str] = Field(default_factory=list, max_length=40)
    # Tools from servers the owner granted this project and reviewed. Only
    # tools that could actually be called are named.
    tools: list[str] = Field(default_factory=list, max_length=40)
    # Earlier answers from anywhere in the office judged to bear on this task.
    related: list[str] = Field(default_factory=list, max_length=6)
    # The discussion it was handed off in, its last lines: what was being
    # said when the owner asked for it.
    discussion: list[str] = Field(default_factory=list, max_length=12)
    # Earlier links of this task's line of work, beyond the task it continues,
    # judged to bear on it and kept within a budget.
    lineage: list[str] = Field(default_factory=list, max_length=12)
    # The owner's calendar for the next days, cut to what they let agents see
    # titles and times by default, never notes or attendees.
    schedule: str = Field(default="", max_length=6000)
    # What agents may read from the owner's wall of widgets on the floor.
    wall: list[str] = Field(default_factory=list, max_length=8)
    # What the owner attached for this task, among its documents.
    attached: list[str] = Field(default_factory=list, max_length=10)
    # The office's own browser is on, so a plan may use a browse step.
    browser: bool = False
    # The owner asked for this task to be done in the office's browser.
    browse_asked: bool = False
    # Started by the owner's press in a discussion: it asks before it
    # spends more on revising its own answer. Read by the office, not told
    # to the agents.
    from_discussion: bool = False

    def render(self) -> str:
        lines = [
            f"Organization: {self.organization}",
            f"Project: {self.project_name} ({self.project_id})",
        ]
        if self.as_of is not None:
            lines.append(f"Date of this work: {self.as_of.isoformat()}")
        if self.background:
            lines.append(f"Background:\n{self.background}")
        if self.constraints:
            lines.append("Constraints:\n" + "\n".join(f"- {c}" for c in self.constraints))
        if self.venture:
            lines.append(f"Business line: {self.venture}")
        if self.doctrine:
            # Before anything else it is told: how work is judged here decides
            # what counts as evidence, and the same agents serve several lines.
            lines.append(f"How work is judged in this business line:\n{self.doctrine}")
        if self.objectives:
            lines.append(
                "What this business line is working towards now:\n"
                + "\n".join(f"- {item}" for item in self.objectives)
            )
        if self.serves:
            lines.append(f"This task serves: {self.serves}")
        if self.deliverable:
            lines.append(f"What this work should come to:\n{self.deliverable}")
        if self.attached:
            lines.append(
                "The owner attached these for this task; they are among the owner's local "
                "sources below. Read and use them:\n"
                + "\n".join(f"- {item}" for item in self.attached)
            )
        if self.browser:
            lines.append(
                "The office's own browser is on: a browse step can use a real browser on "
                "the public web. Anything that signs in, submits, sends, pays or books asks "
                "the owner first."
            )
        if self.browse_asked:
            lines.append(
                "The owner asked for this to be done in the office's browser: plan at least "
                "one browse step for it."
            )
        if self.schedule:
            lines.append(
                f"{self.schedule}\nPlan around it: work meant for a meeting is ready before "
                "it starts. Never put anything on the owner's calendar or contact anyone in it."
            )
        if self.wall:
            lines.append(
                "From the owner's wall on the office floor, which they let you read:\n"
                + "\n\n".join(self.wall)
            )
        if self.tools:
            lines.append(
                "Tools you may use, beyond your own:\n"
                + "\n".join(f"- {item}" for item in self.tools)
            )
        if self.continues:
            lines.append("\n".join(self.continues))
        if self.discussion:
            lines.append(
                "The owner asked for this in a discussion. What was said just before, "
                "oldest first:\n" + "\n".join(f"- {item}" for item in self.discussion)
            )
        if self.lineage:
            lines.append(
                "Earlier in this line of work, before the task it continues, judged relevant. "
                "What was concluded then, not settled fact:\n"
                + "\n".join(f"- {item}" for item in self.lineage)
            )
        if self.related:
            lines.append(
                "Related work elsewhere in this office, judged relevant to this task. What "
                "was found then, not settled fact: build on it, and say so if your own work "
                "contradicts it.\n" + "\n\n".join(self.related)
            )
        if self.dossiers:
            lines.append(
                "What the office already knows about the subjects of this task:\n"
                + "\n".join(f"- {item}" for item in self.dossiers)
            )
        if self.lessons:
            lines.append(
                "What this project has already learned, approved by the owner:\n"
                + "\n".join(f"- {lesson}" for lesson in self.lessons)
            )
        if self.decisions:
            lines.append("Recorded decisions:\n" + "\n".join(f"- {d}" for d in self.decisions))
        if self.prior_outcomes:
            lines.append(
                "Earlier outcomes in this project:\n"
                + "\n".join(f"- {o}" for o in self.prior_outcomes)
            )
        if self.local_sources:
            lines.append(
                "Owner's local sources available to this project:\n"
                + "\n".join(f"- {s}" for s in self.local_sources)
            )
        return "\n\n".join(lines)

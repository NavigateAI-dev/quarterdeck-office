"""Owner and member requests arriving from a chat platform.

The paired owner is proven by a one-time code the office shows on the owner's
computer and the owner sends as a direct message. The owner may then add other
people in the office, under Messaging; a member may ask and may give work, on
the owner's keys and within the owner's budgets. Everyone else is a stranger
and is answered nothing at all, so a workspace can hold an office bot without
announcing to the room that it exists.
New work is proposed first and starts only after the owner replies yes in the
same thread; nothing paid starts from a single message. A project the office
does not have yet is proposed by name the same way, and created only on that
yes. Approvals are given from chat only by the owner's button on a low-risk
proposal (`app.approvals.low_risk`): one whose approval only records the
decision, so nothing runs, is written, installed or sent. Anything else needs
the office on the owner's computer, because a chat account should not be able
to authorise a write to the outside world. A typed "approve" is never one.
"""

import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from app.channels import slack_format
from app.channels.threads import (  # noqa: F401 - HISTORY_TURNS and PROPOSAL_SECONDS re-exported
    HISTORY_TURNS,
    PROPOSAL_SECONDS,
    WATCH,
    MemoryThreads,
    ThreadStore,
)
from app.office_settings import MonthlyLimitReached as LimitReached
from app.office_settings import OfficePaused
from app.watches import CAP_DEFAULT, WatchRejected

PAIRING_SECONDS = 600
CONFIDENT = 0.7
# A project the judge thinks likely is proposed, with a new one as the other
# choice; below this, a new project is proposed as before.
LIKELY = 0.4
# Declining the likely project for a new one.
NEW = frozenset({"new", "new project", "a new project", "new one"})
CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")
YES = frozenset({"yes", "y", "yes please", "go", "go ahead", "start", "ok", "confirm"})
# Declining a proposal, typed or pressed.
NOT_NOW = frozenset({"no", "not now", "no thanks", "not yet", "cancel"})
# What a pressed proposal button shows once it is answered.
ANSWERED = {
    slack_format.START: "Start",
    slack_format.NEW: "New project",
    slack_format.NOT_NOW: "Not now",
}
APPROVAL = re.compile(r"\b(approve|approval|reject|merge|publish|deploy)\b", re.IGNORECASE)
# "call it Acme pricing" — accepting a proposal while renaming the project.
NAMED_YES = re.compile(r"^(?:yes[,\s]+)?(?:call|name) it[:\s]+(.+)$", re.IGNORECASE)
# Words that open a request rather than describe the work itself.
OPENERS = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+|i(?:'| a)?m?\s+want(?:ing)?\s+to\s+|"
    r"let's\s+|lets\s+|start\s+|create\s+|open\s+|set\s+up\s+|make\s+|begin\s+|"
    r"a\s+|an\s+|the\s+|new\s+|project\s+|task\s+|for\s+|on\s+|about\s+|to\s+)+",
    re.IGNORECASE,
)
PROJECT_NAME_LIMIT = 48
# "idea: …", "💡 …" or "想法：…" keeps a thought under Ideas, with no model call;
# "friction: …", "curiosity: …" or "opportunity: …" (摩擦, 好奇, 机会) keeps it
# with that kind.
IDEA = re.compile(
    r"^\s*(?:(?P<word>idea|想法|点子|friction|摩擦|curiosity|好奇|opportunity|机会)"
    r"\s*[:：\-–—]|💡|:bulb:)\s*(?P<text>\S.*)$",
    re.I | re.S,
)
IDEA_KIND = {
    "friction": "friction",
    "摩擦": "friction",
    "curiosity": "curiosity",
    "好奇": "curiosity",
    "opportunity": "opportunity",
    "机会": "opportunity",
}
IDEA_REACTION = "bulb"


def idea_said(text: str) -> tuple[str, str | None] | None:
    """The thought and its kind when a message keeps an idea, else None."""
    found = IDEA.match(text)
    if found is None:
        return None
    word = (found.group("word") or "").lower()
    return found.group("text").strip(), IDEA_KIND.get(word)


ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# How often a watch looks, as the owner reads it.
EVERY_SAID = {60: "every hour", 240: "every 4 hours", 1440: "once a day"}
# Why a watch could not start, said in the thread.
WATCH_REFUSED = {
    "address_not_allowed": "I cannot watch that address: it is not a public web page.",
    "empty_search": "I could not tell what to search for. Say it again with the words to watch.",
}


@dataclass(frozen=True)
class InboundRequest:
    workspace_id: str
    user_id: str
    channel_id: str
    text: str
    event_id: str
    thread_ts: str | None
    # A button pressed instead of a message typed: which one, what it
    # carries, and the message it was on, so that message can show the answer.
    action: str | None = None
    value: str = ""
    message_ts: str | None = None
    message_text: str = ""
    # Files shared with the message: Slack's id, name, type, size and address.
    files: tuple[dict[str, Any], ...] = ()
    # A reaction put on a message: its name ("bulb"); the message is
    # `message_ts` in `channel_id`.
    reaction: str | None = None


class Office(Protocol):
    def owner_of(self, workspace: str) -> str | None: ...
    def role_of(self, workspace: str, user: str) -> str | None:
        """'owner', 'member', or None for anyone this office does not know."""
        ...

    def set_owner(self, workspace: str, user: str) -> None: ...
    def first_time(self, event_id: str) -> bool: ...
    async def route(
        self, text: str, recent: list[dict[str, str]] | None = None
    ) -> dict[str, tuple[str, float]]: ...
    def project_id(self, name: str) -> str | None: ...
    def thread_project(self, channel: str, thread: str | None) -> tuple[str, str] | None:
        """The project (id, name) of the work already started in this thread."""
        ...

    def paused_for_new_tasks(self) -> bool: ...
    def open_project(self, name: str) -> str:
        """The project by that name, created if the office has none."""
        ...

    def start(self, project_id: str, objective: str, channel: str, thread: str | None) -> str: ...
    def start_watch(self, watch: dict[str, Any], channel: str, thread: str | None) -> str:
        """Start a watch the owner pressed Start on; what it is told comes back here."""
        ...

    def work_offer(self, project: str) -> str:
        """Who will do proposed work, in one sentence."""
        ...

    async def status_line(self) -> str: ...
    def pending_count(self) -> int: ...
    async def converse(
        self,
        history: list[dict[str, str]],
        text: str,
        agent: dict[str, Any] | None = None,
        files: tuple[dict[str, Any], ...] = (),
        chat_ref: str | None = None,
        by_owner: bool = False,
    ) -> Any:
        """The reply: words, a `ChatAnswer` (words, pictures, a task to offer), or None.
        `chat_ref` names the thread its model calls are kept against."""
        ...

    def active_task(self, channel: str, thread: str | None) -> dict[str, Any] | None:
        """The task asked for in this thread that is still working, if any."""
        ...

    def note_task(self, task: dict[str, Any], text: str) -> str:
        """Give a working task the owner's note; returns what to say back."""
        ...

    async def decide(self, action: str, value: str) -> tuple[bool, str]:
        """Approve or reject a low-risk proposal; whether it was decided, and what to say."""
        ...

    async def act(self, action: str, run_id: str) -> str:
        """Stop, rework, skip or retry a task from a button; returns what to say."""
        ...

    async def edit(self, channel: str, ts: str, text: str) -> None:
        """Replace a message the office posted, without its buttons."""
        ...

    def addressed_agent(self, text: str) -> dict[str, Any] | None:
        """The agent this message speaks to by name, if any."""
        ...


Reply = Callable[..., Awaitable[None]]


class Pairing:
    """One-time codes, one per workspace, held in memory for ten minutes."""

    def __init__(self) -> None:
        self._codes: dict[str, tuple[str, float]] = {}

    def issue(self, workspace: str) -> str:
        code = "".join(secrets.choice(ALPHABET) for _ in range(8))
        code = f"{code[:4]}-{code[4:]}"
        self._codes[workspace] = (code, time.monotonic() + PAIRING_SECONDS)
        return code

    def redeem(self, workspace: str, text: str) -> bool:
        held = self._codes.get(workspace)
        if held is None or time.monotonic() > held[1]:
            return False
        if held[0] not in CODE.findall(text.upper()):
            return False
        del self._codes[workspace]
        return True


def _clip(text: str, limit: int = 120) -> str:
    said = " ".join(text.split())
    return said if len(said) <= limit else said[: limit - 1].rstrip() + "…"


def proposed_project_name(objective: str) -> str:
    """A project name taken from what the owner asked for.

    Only ever a proposal: the owner sees it before anything is created and can
    answer "call it <something else>".
    """
    text = objective.strip().splitlines()[0] if objective.strip() else ""
    text = OPENERS.sub("", text).strip(" .,:;!?-\u2014")
    # Stop at the first clause break so a long sentence does not become a name.
    for separator in (" so that ", " so we ", " because ", ", ", "; ", " - "):
        head = text.split(separator)[0]
        if head:
            text = head
    if len(text) > PROJECT_NAME_LIMIT:
        cut = text[:PROJECT_NAME_LIMIT].rsplit(" ", 1)[0]
        text = cut or text[:PROJECT_NAME_LIMIT]
    text = text.strip(" .,:;!?-\u2014")
    if not text:
        return "Slack request"
    return text[0].upper() + text[1:]


class InboundHandler:
    def __init__(
        self, office: Office, reply: Reply, pairing: Pairing, threads: ThreadStore | None = None
    ) -> None:
        self.office = office
        self.reply = reply
        self.pairing = pairing
        # The proposal waiting in each thread and its recent turns, so a yes
        # and a follow-up question find what they refer to. The office keeps
        # them in its database; without one they last as long as the process.
        self.threads: ThreadStore = threads or MemoryThreads()

    async def handle(self, request: InboundRequest) -> None:
        if not self.office.first_time(request.event_id):
            return
        say = self._sayer(request)
        owner = self.office.owner_of(request.workspace_id)
        if owner is None:
            if self.pairing.redeem(request.workspace_id, request.text):
                self.office.set_owner(request.workspace_id, request.user_id)
                await say("Paired. You are now the owner of this office in Slack.")
            else:
                await say(
                    "This office is not paired yet. Open Messaging in the office on your "
                    "computer, choose Pair Slack, and send me the code shown there."
                )
            return
        role = self.office.role_of(request.workspace_id, request.user_id)
        if role is None:
            # A stranger gets no signal either way, so a workspace can hold an
            # office bot without announcing to the room that it exists.
            return
        if request.reaction:
            await self._reacted(request, role)
            return
        if request.action:
            await self._pressed(request, role, say)
            return
        await self._typed(request, role, say)

    async def _reacted(self, request: InboundRequest, role: str) -> None:
        """A 💡 the owner put on a message keeps that message as an idea.

        Said only to the owner (an ephemeral message), so a channel is not told.
        Anyone else's reaction is nothing to this office.
        """
        keep = getattr(self.office, "keep_idea", None)
        if role != "owner" or request.reaction != IDEA_REACTION or keep is None:
            return
        channel, ts = request.channel_id, str(request.message_ts or "")
        text = await self.office.message_text(channel, ts)  # type: ignore[attr-defined]
        if not text:
            said = (
                "I could not read that message to keep it as an idea. Send it to me as "
                "“idea: …” instead."
            )
        elif keep(text, f"slack:{channel}:{ts}"):
            said = f"✓ Kept under Ideas: “{_clip(text)}”. Plan it for a day in the office."
        else:
            said = "That message is already kept under Ideas."
        await self.office.whisper(channel, request.user_id, said)  # type: ignore[attr-defined]

    async def _pressed(
        self, request: InboundRequest, role: str, say: Callable[..., Awaitable[None]]
    ) -> None:
        """A button. A proposal's buttons are the words they stand for;
        a task's buttons act on it, and only for the owner."""
        action = str(request.action)
        if action in slack_format.TYPED:
            if request.message_ts:
                # The buttons go, so the same proposal cannot be answered twice.
                await self.office.edit(
                    request.channel_id,
                    request.message_ts,
                    f"{request.message_text}\n\n_Answered: {ANSWERED[action]}_",
                )
            typed = replace(request, text=slack_format.TYPED[action], action=None)
            await self._typed(typed, role, say)
            return
        if role != "owner":
            await say("Only the office's owner can do that.")
            return
        if action in slack_format.DECISIONS:
            decided, said = await self.office.decide(action, request.value)
            if decided and request.message_ts:
                # The card keeps what it said and loses its buttons, so the
                # same approval cannot be pressed twice.
                await self.office.edit(
                    request.channel_id, request.message_ts, f"{request.message_text}\n\n_{said}_"
                )
            else:
                await say(said)
            return
        await say(await self.office.act(action, request.value))

    async def _typed(
        self, request: InboundRequest, role: str, say: Callable[..., Awaitable[None]]
    ) -> None:
        text = request.text.strip()
        key = (request.channel_id, request.thread_ts)
        idea = idea_said(text)
        keep = getattr(self.office, "keep_idea", None)
        if idea is not None and keep is not None:
            # Kept as written: no routing and no model, so no chat allowance.
            if role != "owner":
                await say("Ideas are kept for the office's owner only.")
                return
            thought, kind = idea
            if keep(thought, f"slack:{request.channel_id}:{request.event_id}", kind):
                under = f"Ideas, as {kind.capitalize()}" if kind else "Ideas"
                await say(
                    f"✓ Kept under {under}: “{_clip(thought)}”. Plan it for a day in the office."
                )
            else:
                await say("That one is already kept under Ideas.")
            return
        renamed = NAMED_YES.match(text)
        wants_new = text.lower().rstrip(".!") in NEW
        accepted = text.lower().rstrip(".!") in YES or renamed is not None or wants_new
        if text.lower().rstrip(".!") in NOT_NOW and self.threads.take(key) is not None:
            await say("Okay, not started. Ask again whenever you like.")
            return
        held = self.threads.take(key) if accepted else None
        if held is not None and held[0] == WATCH:
            await say(self._start_watch(request, held[2]))
            return
        if held is not None:
            project_id, name, objective = held
            if renamed is not None:
                name = renamed.group(1).strip().strip(".!")[:PROJECT_NAME_LIMIT]
                project_id = None  # A name the owner typed always means that project.
            elif wants_new:
                name = proposed_project_name(objective)
                project_id = None
            try:
                if project_id is None:
                    # Checked before creating, so a paused office leaves no
                    # empty project behind.
                    if self.office.paused_for_new_tasks():
                        raise OfficePaused("new_tasks")
                    project_id = self.office.open_project(name)
                self.office.start(project_id, objective, request.channel_id, request.thread_ts)
            except OfficePaused:
                await say(
                    "New tasks are switched off in the office at the moment. Turn them back "
                    "on in the office on your computer, under Settings."
                )
                return
            except LimitReached:
                await say(
                    "I could not start it: the office has reached its monthly spending limit. "
                    "Raise it in the office on your computer, under Settings."
                )
                return
            await say(f"Started in {name}. I will post here when it needs you or finishes.")
            return
        # A reply in the thread of a task still working is for that task: a
        # note its team takes at the next step, or the hint a stuck step asked
        # for. Only the owner steers work.
        if role == "owner" and request.thread_ts:
            working = self.office.active_task(request.channel_id, request.thread_ts)
            if working is not None:
                await say(self.office.note_task(working, text))
                return
        # Routing comes first: a request to start work is a request to start
        # work even when the sentence happens to contain the word "approve".
        turns = self.threads.turns(key)
        # The thread so far is read with the message: "I shared you the
        # screenshot here" is a remark about the conversation, not new work.
        answers = (
            await self.office.route(text, list(turns)) if turns else await self.office.route(text)
        )
        action, action_p = answers.get("action", ("none", 0.0))
        if action == "start_task" and action_p >= CONFIDENT:
            await self._offer_work(request, key, say, text, answers)
            return
        if APPROVAL.search(text):
            await say(
                "Approvals are made in the office on your computer, not from chat. "
                f"{self.office.pending_count()} item(s) are waiting there."
            )
            return
        # "Researcher, what did you find?" is answered by that agent, in its
        # own voice, in whatever channel this bot is already in.
        addressed = self.office.addressed_agent(text)
        # The calls behind the answer are kept against this thread, for a while.
        where = f"slack:{request.channel_id}:{request.thread_ts or ''}"
        by_owner = role == "owner"
        if request.files:
            said = await self.office.converse(
                list(turns), text, addressed, request.files, chat_ref=where, by_owner=by_owner
            )
        else:
            said = await self.office.converse(
                list(turns), text, addressed, chat_ref=where, by_owner=by_owner
            )
        files: list[tuple[str, bytes]] = []
        if said is not None and not isinstance(said, str):
            # It looked something up, or the request needs the team.
            if getattr(said, "task", None) or getattr(said, "watch", None):
                # The lead may answer what it can before offering more (§15).
                first = str(getattr(said, "text", "") or "")
                if first:
                    await self._say_and_remember(key, say, text, first, addressed)
                if getattr(said, "task", None):
                    await self._offer_work(request, key, say, str(said.task), answers)
                else:
                    await self._offer_watch(request, key, say, role, dict(said.watch))
                return
            files = list(getattr(said, "files", None) or [])
            said = str(getattr(said, "text", "") or "") or None
        answer = said
        if answer is None:
            answer = await self._fixed_reply(action if action_p >= CONFIDENT else "none")
            addressed = None
        await self._say_and_remember(key, say, text, answer, addressed)
        if files:
            share = getattr(self.office, "share", None)
            if share is not None:
                await share(request.channel_id, request.thread_ts, files)

    async def _offer_work(
        self,
        request: InboundRequest,
        key: tuple[str, str | None],
        say: Callable[..., Awaitable[None]],
        objective: str,
        answers: dict[str, tuple[str, float]],
    ) -> None:
        """Propose the work, with Start and Not now; nothing starts until the press."""
        text = request.text.strip()
        # Work asked in a thread that already has some belongs with it:
        # "the six industries above" means the ones found here.
        home = self.office.thread_project(request.channel_id, request.thread_ts)
        if home is not None:
            home_id, home_name = home
            self.threads.propose(key, (home_id, home_name, objective))
            await self._say_and_remember(
                key,
                say,
                text,
                f"{self.office.work_offer(home_name)}\n> {objective[:300]}\n"
                "It continues this thread's work. It may use paid model calls. Press Start "
                '(or reply yes), or reply "call it <name>" to put it in another project.',
                buttons=False,
            )
            return
        name, project_p = answers.get("project", ("none", 0.0))
        found = self.office.project_id(name) if project_p >= LIKELY and name != "none" else None
        if found is not None and project_p >= CONFIDENT:
            self.threads.propose(key, (found, name, objective))
            await self._say_and_remember(
                key,
                say,
                text,
                f"{self.office.work_offer(name)}\n> {objective[:300]}\n"
                "It may use paid model calls. Press Start (or reply yes), or reply "
                '"call it <name>" to put it in another project.',
                buttons=False,
            )
            return
        if found is not None:
            # Likely, not certain: the related project is proposed, and a
            # new one is one word away. Either way nothing starts yet.
            proposed = proposed_project_name(objective)
            self.threads.propose(key, (found, name, objective))
            await self._say_and_remember(
                key,
                say,
                text,
                f"This looks like it belongs in *{name}*, with the related work there:\n"
                f"> {objective[:300]}\nPress Start (or reply yes) to start it there, or New "
                f"project (or reply new) for *{proposed}* instead, "
                'or reply "call it <name>" for another. '
                "It may use paid model calls.",
                buttons=True,
            )
            return
        # No project for this yet. The office proposes one by name and
        # creates nothing until the owner says yes.
        proposed = proposed_project_name(objective)
        self.threads.propose(key, (None, proposed, objective))
        await self._say_and_remember(
            key,
            say,
            text,
            f"I have no project for this yet. Press Start (or reply yes) and I will "
            f"create *{proposed}* and start there:\n> {objective[:300]}\n"
            'It may use paid model calls. To use another name, reply "call it <name>".',
            buttons=False,
        )

    async def _offer_watch(
        self,
        request: InboundRequest,
        key: tuple[str, str | None],
        say: Callable[..., Awaitable[None]],
        role: str,
        watch: dict[str, Any],
    ) -> None:
        """Offer a watch with Start and Not now; nothing runs until the press.

        Only the owner: a watch keeps spending, a little, on its own after Start.
        """
        text = request.text.strip()
        if role != "owner":
            await self._say_and_remember(
                key,
                say,
                text,
                "Only the office's owner can set up a watch: it runs on "
                "their budget. Ask them to set it up.",
            )
            return
        self.threads.propose(key, (WATCH, str(watch["title"]), json.dumps(watch)))
        what = (
            f"the page {watch['target']}"
            if watch["kind"] == "page"
            else f"a search for “{watch['target']}”"
        )
        about = watch.get("condition") or "any real change"
        await self._say_and_remember(
            key,
            say,
            text,
            f"I can watch this for you: *{watch['title']}*\n"
            f"> I look at {what} {EVERY_SAID.get(watch['every_minutes'], 'every 4 hours')} "
            f"and tell you here about: {about}\n"
            "Looking is free. Only when it changed does one small model call decide whether "
            f"to tell you, at most ${CAP_DEFAULT:.2f} a day. Press Start (or reply yes) to "
            "begin; stop it any time under Watching on the floor.",
            buttons=False,
        )

    def _start_watch(self, request: InboundRequest, held: str) -> str:
        try:
            return self.office.start_watch(json.loads(held), request.channel_id, request.thread_ts)
        except WatchRejected as rejected:
            return WATCH_REFUSED.get(
                rejected.code,
                f"I could not start that watch ({rejected.code}). Set it up under Watching "
                "on the floor instead.",
            )

    async def _fixed_reply(self, action: str) -> str:
        if action == "office_status":
            return await self.office.status_line()
        if action == "pending_approvals":
            return f"{self.office.pending_count()} item(s) are waiting for you in the office."
        if action == "start_task":
            return "Which project is this for? Please rephrase with the project name."
        return "I did not follow that. Could you rephrase what you need?"

    async def _say_and_remember(
        self,
        key: tuple[str, str | None],
        say: Callable[..., Awaitable[None]],
        question: str,
        answer: str,
        persona: dict[str, Any] | None = None,
        buttons: bool | None = None,
    ) -> None:
        """Say it and keep it in the thread's memory. `buttons` makes it a
        proposal with Start and Not now; True also offers a new project."""
        self.threads.remember(key, question, answer)
        if buttons is None:
            await say(answer, persona)
            return
        await say(answer, persona, slack_format.proposal_blocks(answer, likely_project=buttons))

    def _sayer(self, request: InboundRequest) -> Callable[..., Awaitable[None]]:
        async def say(
            text: str,
            persona: dict[str, Any] | None = None,
            blocks: list[dict[str, Any]] | None = None,
        ) -> None:
            if blocks is None:
                await self.reply(request.channel_id, request.thread_ts, text, persona)
            else:
                await self.reply(request.channel_id, request.thread_ts, text, persona, blocks)

        return say

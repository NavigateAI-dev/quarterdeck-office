"""Inbound Slack requests: pairing, owner-only, dedup, confirmation, no approvals."""

from typing import Any

import pytest

from app.channels import slack_format
from app.channels.inbound import InboundHandler, InboundRequest, Pairing


class Replies:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None, str]] = []
        self.personas: list[str | None] = []
        self.blocks: list[list | None] = []

    async def __call__(
        self,
        channel: str,
        thread: str | None,
        text: str,
        persona: dict | None = None,
        blocks: list | None = None,
    ) -> None:
        self.sent.append((channel, thread, text))
        self.personas.append(persona["name"] if persona else None)
        self.blocks.append(blocks)

    blocks: list = []


class Office:
    """Stands in for the office: owner mapping, routing, starting runs, status."""

    def __init__(self, owner: str | None = None) -> None:
        self.owner = owner
        self.seen: set[str] = set()
        self.started: list[tuple[str, str]] = []
        self.origins: list[tuple[str, str | None]] = []
        self.route_answer: dict[str, tuple[str, float]] = {}
        self.projects = {"Market intel": "p-1"}
        self.members = set()
        self.created = []
        self.paused_new_tasks = False

    def owner_of(self, workspace: str) -> str | None:
        return self.owner

    members: set[str] = set()

    def role_of(self, workspace: str, user: str) -> str | None:
        if user == self.owner:
            return "owner"
        return "member" if user in self.members else None

    def set_owner(self, workspace: str, user: str) -> None:
        self.owner = user

    def first_time(self, event_id: str) -> bool:
        if event_id in self.seen:
            return False
        self.seen.add(event_id)
        return True

    recents: list = []

    async def route(self, text: str, recent: list | None = None) -> dict[str, tuple[str, float]]:
        self.recents = [*self.recents, recent]
        return self.route_answer

    def project_id(self, name: str) -> str | None:
        return self.projects.get(name)

    home: tuple[str, str] | None = None

    def thread_project(self, channel: str, thread: str | None) -> tuple[str, str] | None:
        return self.home

    paused_new_tasks = False
    created: list[str] = []

    def paused_for_new_tasks(self) -> bool:
        return self.paused_new_tasks

    def open_project(self, name: str) -> str:
        self.created.append(name)
        return self.projects.setdefault(name, f"p-new-{len(self.projects)}")

    def start(self, project_id: str, objective: str, channel: str, thread: str | None) -> str:
        self.started.append((project_id, objective))
        self.origins.append((channel, thread))
        return "run-1"

    def work_offer(self, project: str) -> str:
        return f"I can start this in {project}:"

    async def status_line(self) -> str:
        return "All services ok."

    def pending_count(self) -> int:
        return 2

    answer: str | None = None
    conversations: list[tuple[list[dict[str, str]], str]] = []

    agents: list[dict] = []

    async def converse(
        self,
        history: list[dict[str, str]],
        text: str,
        agent: dict | None = None,
        chat_ref: str | None = None,
        by_owner: bool = False,
    ) -> str | None:
        self.by_owner = by_owner
        self.conversations = [*self.conversations, (history, text)]
        self.spoke_as = agent["name"] if agent else None
        if self.answer is None:
            return None
        return f"{agent['name']} here. {self.answer}" if agent else self.answer

    def addressed_agent(self, text: str) -> dict | None:
        lowered = text.lower()
        return next((a for a in self.agents if a["name"].lower() in lowered), None)

    # A task still working in a thread, and what was done to tasks.
    working: dict | None = None
    notes: list[tuple[str, str]] = []
    acted: list[tuple[str, str]] = []
    edits: list[tuple[str, str, str]] = []

    def active_task(self, channel: str, thread: str | None) -> dict | None:
        return self.working

    def note_task(self, task: dict, text: str) -> str:
        self.notes = [*self.notes, (task["run_id"], text)]
        return "Passed to the team as a note."

    async def act(self, action: str, run_id: str) -> str:
        self.acted = [*self.acted, (action, run_id)]
        return f"Done: {action}"

    async def edit(self, channel: str, ts: str, text: str) -> None:
        self.edits = [*self.edits, (channel, ts, text)]

    decided: list[tuple[str, str]] = []
    decision: tuple[bool, str] = (True, "✓ Approved by you in Slack.")

    async def decide(self, action: str, value: str) -> tuple[bool, str]:
        self.decided = [*self.decided, (action, value)]
        return self.decision


def request(
    text: str, user: str = "U_OWNER", event: str = "e1", thread: str = "t1"
) -> InboundRequest:
    return InboundRequest(
        workspace_id="T1",
        user_id=user,
        channel_id="D1",
        text=text,
        event_id=event,
        thread_ts=thread,
    )


def handler(office: Office, pairing: Pairing | None = None) -> tuple[InboundHandler, Replies]:
    replies = Replies()
    return InboundHandler(office, replies, pairing or Pairing()), replies  # type: ignore[arg-type]


async def test_a_pairing_code_links_the_sender_as_owner() -> None:
    office, pairing = Office(owner=None), Pairing()
    code = pairing.issue("T1")
    run, replies = handler(office, pairing)
    await run.handle(request(f"my code is {code}", user="U_ME"))
    assert office.owner == "U_ME"
    assert "Paired" in replies.sent[0][2]
    # The code is single-use.
    await run.handle(request(f"{code}", user="U_OTHER", event="e2"))
    assert office.owner == "U_ME"


async def test_messages_from_anyone_but_the_owner_are_ignored_silently() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    run, replies = handler(office)
    await run.handle(request("start a pricing brief", user="U_STRANGER"))
    assert replies.sent == [] and office.started == []


async def test_before_pairing_nobody_can_give_work() -> None:
    office = Office(owner=None)
    run, replies = handler(office)
    await run.handle(request("start a pricing brief", user="U_ANY"))
    assert office.started == []
    assert "pair" in replies.sent[0][2].lower()


async def test_a_redelivered_event_is_handled_once() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("office_status", 0.95), "project": ("none", 0.9)}
    run, replies = handler(office)
    await run.handle(request("status?", event="same"))
    await run.handle(request("status?", event="same"))
    assert len(replies.sent) == 1


async def test_new_work_is_proposed_and_started_only_after_yes() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    run, replies = handler(office)
    await run.handle(request("Prepare this week's pricing brief", thread="t9"))
    assert office.started == []
    assert "reply yes" in replies.sent[-1][2]
    assert replies.sent[-1][1] == "t9"  # answered in the thread
    await run.handle(request("yes", event="e2", thread="t9"))
    assert office.started == [("p-1", "Prepare this week's pricing brief")]
    assert office.origins == [("D1", "t9")]  # progress is posted back to this thread
    assert "Started" in replies.sent[-1][2]


async def test_yes_without_a_pending_proposal_starts_nothing() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("none", 0.9), "project": ("none", 0.9)}
    run, replies = handler(office)
    await run.handle(request("yes", thread="other"))
    assert office.started == []


async def test_approvals_are_refused_from_chat() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("none", 0.9), "project": ("none", 0.9)}
    run, replies = handler(office)
    await run.handle(request("approve the pending engineering change"))
    assert office.started == []
    assert "on your computer" in replies.sent[-1][2]


async def test_status_and_pending_questions_are_answered() -> None:
    office = Office(owner="U_OWNER")
    run, replies = handler(office)
    office.route_answer = {"action": ("office_status", 0.95), "project": ("none", 0.9)}
    await run.handle(request("is everything ok?", event="a"))
    office.route_answer = {"action": ("pending_approvals", 0.95), "project": ("none", 0.9)}
    await run.handle(request("what needs me?", event="b"))
    assert "All services ok." in replies.sent[0][2]
    assert "2" in replies.sent[1][2]


async def test_an_unclear_request_asks_for_clarification() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.4), "project": ("none", 0.3)}
    run, replies = handler(office)
    await run.handle(request("hmm"))
    assert office.started == []
    assert (
        "rephrase" in replies.sent[-1][2].lower() or "which project" in replies.sent[-1][2].lower()
    )


def test_slack_events_become_requests_only_for_dms_and_mentions() -> None:
    from app.channels.socket_mode import request_from_event

    bot = "U_BOT"
    dm = {
        "type": "message",
        "channel_type": "im",
        "user": "U1",
        "text": "hi",
        "ts": "1.1",
        "channel": "D1",
    }
    mention = {
        "type": "app_mention",
        "user": "U1",
        "text": "<@U_BOT> brief",
        "ts": "2.2",
        "channel": "C1",
    }
    own = dm | {"user": bot}
    edited = dm | {"subtype": "message_changed"}
    from_bot = dm | {"bot_id": "B9"}
    channel_chatter = dm | {"channel_type": "channel"}
    assert request_from_event({"event": dm, "event_id": "E1", "team_id": "T1"}, bot) is not None
    parsed = request_from_event({"event": mention, "event_id": "E2", "team_id": "T1"}, bot)
    assert parsed is not None and parsed.text == "brief"
    for event in (own, edited, from_bot, channel_chatter):
        assert request_from_event({"event": event, "event_id": "E3", "team_id": "T1"}, bot) is None


@pytest.mark.asyncio
async def test_every_envelope_is_acknowledged_before_it_is_handled() -> None:
    import json

    from app.channels.socket_mode import process_frame

    acked: list[str] = []
    handled: list[Any] = []

    async def ack(text: str) -> None:
        acked.append(json.loads(text)["envelope_id"])

    async def handle(request: Any) -> None:
        assert acked == ["env-1"]  # acknowledged first
        handled.append(request)

    frame = json.dumps(
        {
            "type": "events_api",
            "envelope_id": "env-1",
            "payload": {
                "event_id": "E1",
                "team_id": "T1",
                "event": {
                    "type": "message",
                    "channel_type": "im",
                    "user": "U1",
                    "text": "hi",
                    "ts": "1.1",
                    "channel": "D1",
                },
            },
        }
    )
    outcome = await process_frame(frame, "U_BOT", ack, handle)
    assert outcome == "handled" and len(handled) == 1
    assert (
        await process_frame(
            json.dumps({"type": "disconnect", "reason": "refresh_requested"}), "U_BOT", ack, handle
        )
        == "reconnect"
    )


async def test_questions_get_a_conversational_answer_with_thread_history() -> None:
    office = Office(owner="U_OWNER")
    office.answer = "Run 8 is waiting for your approval."
    office.route_answer = {"action": ("none", 0.9), "project": ("none", 0.9)}
    run, replies = handler(office)
    await run.handle(request("what did the last run find?", event="a", thread="t"))
    await run.handle(request("and why is it waiting?", event="b", thread="t"))
    assert replies.sent[-1][2] == "Run 8 is waiting for your approval."
    history, text = office.conversations[-1]
    assert text == "and why is it waiting?"
    assert history[0] == {"role": "user", "content": "what did the last run find?"}


async def test_only_the_owner_may_change_the_to_dos_from_chat() -> None:
    office = Office(owner="U_OWNER")
    office.members = {"U_COLLEAGUE"}
    office.answer = "Noted."
    office.route_answer = {"action": ("none", 0.9), "project": ("none", 0.9)}
    run, _ = handler(office)
    await run.handle(request("the tax is paid", event="a"))
    assert office.by_owner is True
    await run.handle(request("the tax is paid", event="b", user="U_COLLEAGUE"))
    assert office.by_owner is False


async def test_a_confident_new_task_is_still_proposed_not_chatted_about() -> None:
    office = Office(owner="U_OWNER")
    office.answer = "chatty"
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    run, replies = handler(office)
    await run.handle(request("Prepare a pricing brief"))
    assert "reply yes" in replies.sent[-1][2] and office.conversations == []


async def test_approvals_are_refused_even_when_chat_is_available() -> None:
    office = Office(owner="U_OWNER")
    office.answer = "Sure, approved!"
    run, replies = handler(office)
    await run.handle(request("approve run 8"))
    assert "on your computer" in replies.sent[-1][2]


async def test_an_agent_named_in_a_message_answers_in_its_own_voice() -> None:
    office = Office(owner="U_OWNER")
    office.agents = [{"name": "Scout", "role": "researcher", "profile_id": "p1"}]
    office.answer = "I checked the sources."
    office.route_answer = {"action": ("none", 0.9), "project": ("none", 0.9)}
    run, replies = handler(office)
    await run.handle(request("Scout, what did you find?"))
    assert office.spoke_as == "Scout"
    assert replies.personas[-1] == "Scout"
    assert replies.sent[-1][2].startswith("Scout here.")


async def test_a_message_naming_nobody_is_answered_by_the_office() -> None:
    office = Office(owner="U_OWNER")
    office.agents = [{"name": "Scout", "role": "researcher", "profile_id": "p1"}]
    office.answer = "All quiet."
    office.route_answer = {"action": ("none", 0.9), "project": ("none", 0.9)}
    run, replies = handler(office)
    await run.handle(request("what happened today?"))
    assert office.spoke_as is None and replies.personas[-1] is None


async def test_a_fixed_reply_never_pretends_to_be_an_agent() -> None:
    office = Office(owner="U_OWNER")
    office.agents = [{"name": "Scout", "role": "researcher", "profile_id": "p1"}]
    office.answer = None  # the model is unavailable
    office.route_answer = {"action": ("office_status", 0.95), "project": ("none", 0.9)}
    run, replies = handler(office)
    await run.handle(request("Scout, is everything ok?"))
    assert replies.personas[-1] is None
    assert "All services ok." in replies.sent[-1][2]


async def test_a_project_the_office_does_not_have_is_proposed_by_name() -> None:
    """The gap the owner hit: asking for new work created nothing and said nothing."""
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("none", 0.2)}
    run, replies = handler(office)
    await run.handle(request("start a new project to research the EU AI act for our clients"))
    # Proposed, not created: nothing exists until the owner says yes.
    assert office.created == [] and office.started == []
    offer = replies.sent[-1][2]
    assert "Research the EU AI act for our clients" in offer
    assert "reply yes" in offer.lower()

    await run.handle(request("yes", event="e2"))
    assert office.created == ["Research the EU AI act for our clients"]
    assert len(office.started) == 1
    assert "Started in Research the EU AI act for our clients" in replies.sent[-1][2]


async def test_the_owner_can_name_the_new_project_when_accepting() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("none", 0.2)}
    run, replies = handler(office)
    await run.handle(request("look into the EU AI act"))
    await run.handle(request("call it EU AI Act 2027", event="e2"))
    assert office.created == ["EU AI Act 2027"]
    assert len(office.started) == 1


async def test_a_paused_office_creates_no_project_when_it_refuses() -> None:
    """A refusal must not leave an empty project behind."""
    office = Office(owner="U_OWNER")
    office.paused_new_tasks = True
    office.route_answer = {"action": ("start_task", 0.95), "project": ("none", 0.2)}
    run, replies = handler(office)
    await run.handle(request("research the EU AI act"))
    await run.handle(request("yes", event="e2"))
    assert office.created == [] and office.started == []
    assert "switched off" in replies.sent[-1][2].lower()


async def test_the_word_approve_does_not_hijack_a_request_to_start_work() -> None:
    """The reply the owner actually got: a work request answered as an approval."""
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    run, replies = handler(office)
    await run.handle(request("start a pricing brief in Market intel and approve it when done"))
    answer = replies.sent[-1][2]
    assert "Approvals are made in the office" not in answer
    assert "reply yes" in answer.lower()


async def test_a_real_approval_request_still_points_at_the_computer() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("pending_approvals", 0.9), "project": ("none", 0.2)}
    run, replies = handler(office)
    await run.handle(request("can you approve the publication for me?"))
    assert "Approvals are made in the office on your computer" in replies.sent[-1][2]
    assert office.started == []


async def test_a_member_the_owner_added_can_give_work() -> None:
    """The owner asked for this: others in the workspace, on the owner's keys."""
    office = Office(owner="U_OWNER")
    office.members = {"U_COLLEAGUE"}
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    run, replies = handler(office)
    await run.handle(request("start a pricing brief", user="U_COLLEAGUE"))
    assert "reply yes" in replies.sent[-1][2].lower()
    await run.handle(request("yes", user="U_COLLEAGUE", event="e2"))
    assert office.started == [("p-1", "start a pricing brief")]


async def test_a_member_cannot_approve_anything() -> None:
    """Approvals stay with the owner, on the owner's computer."""
    office = Office(owner="U_OWNER")
    office.members = {"U_COLLEAGUE"}
    office.route_answer = {"action": ("pending_approvals", 0.9), "project": ("none", 0.2)}
    run, replies = handler(office)
    await run.handle(request("approve the publication", user="U_COLLEAGUE"))
    assert "Approvals are made in the office on your computer" in replies.sent[-1][2]
    assert office.started == []


async def test_someone_the_owner_has_not_added_is_answered_nothing() -> None:
    """Silence, so a workspace can hold the bot without announcing the office."""
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    run, replies = handler(office)
    await run.handle(request("start a pricing brief", user="U_STRANGER"))
    assert replies.sent == [] and office.started == []


async def test_removing_a_member_returns_them_to_silence() -> None:
    office = Office(owner="U_OWNER")
    office.members = {"U_COLLEAGUE"}
    office.route_answer = {"action": ("none", 0.9), "project": ("none", 0.2)}
    run, replies = handler(office)
    office.members.clear()
    await run.handle(request("hello?", user="U_COLLEAGUE"))
    assert replies.sent == []


async def test_a_proposal_expires_rather_than_waiting_forever() -> None:
    """Remembered across restarts, a yes weeks later must not start old work."""
    from app.channels.inbound import PROPOSAL_SECONDS, MemoryThreads

    now = [1_000.0]
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    run = InboundHandler(office, Replies(), Pairing(), MemoryThreads(clock=lambda: now[0]))  # type: ignore[arg-type]
    await run.handle(request("Prepare this week's pricing brief", thread="t9"))
    now[0] += PROPOSAL_SECONDS + 1
    office.route_answer = {"action": ("none", 0.9), "project": ("none", 0.9)}
    await run.handle(request("yes", event="e2", thread="t9"))
    assert office.started == []


async def test_a_thread_keeps_only_its_last_turns() -> None:
    from app.channels.inbound import HISTORY_TURNS, MemoryThreads

    threads = MemoryThreads()
    for n in range(HISTORY_TURNS):
        threads.remember(("D1", "t1"), f"q{n}", f"a{n}")
    turns = threads.turns(("D1", "t1"))
    assert len(turns) == HISTORY_TURNS
    assert turns[-1] == {"role": "assistant", "content": f"a{HISTORY_TURNS - 1}"}


# Questions go into an existing related project.


async def test_a_likely_project_is_proposed_rather_than_a_new_one() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.5)}
    run, replies = handler(office)
    await run.handle(request("How do our rivals price their plans?"))
    said = replies.sent[-1][2]
    assert "Market intel" in said and "new" in said
    assert office.started == [] and office.created == []
    await run.handle(request("yes", event="e2"))
    assert office.started == [("p-1", "How do our rivals price their plans?")]
    assert office.created == []


async def test_new_starts_a_new_project_instead_of_the_likely_one() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.5)}
    run, _ = handler(office)
    await run.handle(request("Assess the Dutch payments market"))
    await run.handle(request("new", event="e2"))
    assert len(office.created) == 1 and office.created[0] != "Market intel"
    assert office.started[0][0] != "p-1"


async def test_an_unlikely_match_still_proposes_a_new_project() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.2)}
    run, replies = handler(office)
    await run.handle(request("Assess the Dutch payments market"))
    assert "no project for this yet" in replies.sent[-1][2]


async def test_a_confident_offer_says_how_to_put_it_elsewhere() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    run, replies = handler(office)
    await run.handle(request("start a pricing brief"))
    assert "call it" in replies.sent[-1][2]


async def test_new_work_in_a_thread_stays_in_that_threads_project() -> None:
    office = Office(owner="U_OWNER")
    office.home = ("p-9", "SME growth")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("none", 0.9)}
    run, replies = handler(office)
    await run.handle(request("Now rank the six industries above"))
    assert "SME growth" in replies.sent[-1][2]
    await run.handle(request("yes", event="e2"))
    assert office.started == [("p-9", "Now rank the six industries above")]
    assert office.created == []


def press(action: str, value: str = "", user: str = "U_OWNER", event: str = "p1") -> InboundRequest:
    return InboundRequest(
        workspace_id="T1",
        user_id=user,
        channel_id="D1",
        text="",
        event_id=event,
        thread_ts="t9",
        action=action,
        value=value,
        message_ts="m1",
        message_text="Shall I start this?",
    )


async def test_a_proposal_comes_with_start_and_not_now_buttons() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    run, replies = handler(office)
    await run.handle(request("Prepare this week's pricing brief", thread="t9"))
    buttons = replies.blocks[-1][-1]["elements"]
    assert [b["action_id"] for b in buttons] == [slack_format.START, slack_format.NOT_NOW]


async def test_pressing_start_starts_it_once_and_answers_the_proposal() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    run, replies = handler(office)
    await run.handle(request("Prepare this week's pricing brief", thread="t9"))
    await run.handle(press(slack_format.START))
    await run.handle(press(slack_format.START, event="p2"))
    assert office.started == [("p-1", "Prepare this week's pricing brief")]
    assert office.edits[0][:2] == ("D1", "m1")
    assert office.edits[0][2].endswith("_Answered: Start_")
    assert "Started" in replies.sent[1][2]


async def test_not_now_lets_the_proposal_go() -> None:
    office = Office(owner="U_OWNER")
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    run, replies = handler(office)
    await run.handle(request("Prepare this week's pricing brief", thread="t9"))
    await run.handle(request("not now", event="e2", thread="t9"))
    await run.handle(request("yes", event="e3", thread="t9"))
    assert office.started == []
    assert "not started" in replies.sent[1][2].lower()


async def test_a_task_button_acts_only_for_the_owner() -> None:
    office = Office(owner="U_OWNER")
    office.members = {"U_MEMBER"}
    run, replies = handler(office)
    await run.handle(press(slack_format.STOP, "run-7", user="U_MEMBER"))
    assert office.acted == []
    assert "owner" in replies.sent[-1][2]
    await run.handle(press(slack_format.STOP, "run-7", event="p2"))
    assert office.acted == [(slack_format.STOP, "run-7")]
    assert replies.sent[-1][2] == f"Done: {slack_format.STOP}"


async def test_an_approval_press_edits_the_card_instead_of_answering() -> None:
    office = Office(owner="U_OWNER")
    run, replies = handler(office)
    await run.handle(press(slack_format.APPROVE, "a-1:digest"))
    assert office.decided == [(slack_format.APPROVE, "a-1:digest")]
    assert office.edits == [("D1", "m1", "Shall I start this?\n\n_✓ Approved by you in Slack._")]
    assert replies.sent == [], "the edited card says it; no second message"


async def test_a_refused_approval_says_why_and_keeps_the_card() -> None:
    office = Office(owner="U_OWNER")
    office.decision = (False, "That approval was already decided.")
    run, replies = handler(office)
    await run.handle(press(slack_format.REJECT, "a-1:digest"))
    assert office.edits == []
    assert replies.sent[-1][2] == "That approval was already decided."


async def test_only_the_owner_can_approve_from_slack() -> None:
    office = Office(owner="U_OWNER")
    office.members = {"U_MEMBER"}
    run, replies = handler(office)
    await run.handle(press(slack_format.APPROVE, "a-1:digest", user="U_MEMBER"))
    assert office.decided == [] and "owner" in replies.sent[-1][2]


async def test_a_strangers_press_gets_no_answer() -> None:
    office = Office(owner="U_OWNER")
    run, replies = handler(office)
    await run.handle(press(slack_format.STOP, "run-7", user="U_STRANGER"))
    assert office.acted == [] and replies.sent == []


async def test_the_owners_reply_in_a_working_tasks_thread_goes_to_the_task() -> None:
    office = Office(owner="U_OWNER")
    office.working = {"run_id": "run-7", "status": "RUNNING", "stuck": False}
    office.route_answer = {"action": ("start_task", 0.95), "project": ("Market intel", 0.9)}
    run, replies = handler(office)
    await run.handle(request("Focus on Germany first", thread="t9"))
    assert office.notes == [("run-7", "Focus on Germany first")]
    assert office.started == [] and office.conversations == []
    assert "note" in replies.sent[-1][2]


async def test_a_members_reply_in_a_working_thread_is_not_a_note() -> None:
    office = Office(owner="U_OWNER")
    office.members = {"U_MEMBER"}
    office.working = {"run_id": "run-7", "status": "RUNNING", "stuck": False}
    office.answer = "It is going well."
    run, _ = handler(office)
    await run.handle(request("How is it going?", user="U_MEMBER", thread="t9"))
    assert office.notes == []


# ---- ideas from Slack ----


class IdeaOffice(Office):
    """An office that keeps ideas, reads a reacted message, and whispers."""

    def __init__(self, owner: str | None = "U_OWNER") -> None:
        super().__init__(owner)
        self.kept: list[tuple[str, str]] = []
        self.whispers: list[tuple[str, str, str]] = []
        self.messages: dict[tuple[str, str], str] = {}

    def keep_idea(self, text: str, ref: str, kind: str | None = None) -> bool:
        if any(r == ref for _, r in self.kept):
            return False
        self.kept.append((text, ref))
        return True

    async def message_text(self, channel: str, ts: str) -> str | None:
        return self.messages.get((channel, ts))

    async def whisper(self, channel: str, user: str, text: str) -> None:
        self.whispers.append((channel, user, text))


@pytest.mark.parametrize(
    ("typed", "kept"),
    [
        ("idea: a print button for the colouring site", "a print button for the colouring site"),
        ("Idea - call the printer", "call the printer"),
        ("💡 rain alert before cycling", "rain alert before cycling"),
        ("想法：周五复盘", "周五复盘"),
    ],
)
async def test_an_idea_typed_in_slack_is_kept_without_a_model(typed: str, kept: str) -> None:
    office = IdeaOffice()
    office.route_answer = {"action": ("start_task", 0.99), "project": ("Market intel", 0.9)}
    run, replies = handler(office)
    await run.handle(request(typed))
    assert [text for text, _ in office.kept] == [kept]
    assert office.recents == [] and office.conversations == [], "no routing, no model"
    assert replies.sent[0][2].startswith("✓ Kept under Ideas")


async def test_a_sentence_about_ideas_is_not_kept_as_one() -> None:
    office = IdeaOffice()
    office.answer = "Here are some."
    run, _ = handler(office)
    await run.handle(request("ideas for the newsletter?"))
    assert office.kept == []


async def test_a_member_cannot_keep_ideas_for_the_owner() -> None:
    office = IdeaOffice()
    office.members = {"U_MEMBER"}
    run, replies = handler(office)
    await run.handle(request("idea: buy a boat", user="U_MEMBER"))
    assert office.kept == [] and "owner" in replies.sent[0][2]


def bulb(user: str = "U_OWNER", event: str = "r1") -> InboundRequest:
    return InboundRequest(
        workspace_id="T1",
        user_id=user,
        channel_id="C1",
        text="",
        event_id=event,
        thread_ts=None,
        reaction="bulb",
        message_ts="1700.01",
    )


async def test_a_bulb_on_a_message_keeps_it_once_and_says_so_privately() -> None:
    office = IdeaOffice()
    office.messages[("C1", "1700.01")] = "What if we sold via agencies?"
    run, replies = handler(office)
    await run.handle(bulb())
    await run.handle(bulb(event="r2"))
    assert office.kept == [("What if we sold via agencies?", "slack:C1:1700.01")]
    assert replies.sent == [], "nothing is posted in the channel"
    assert office.whispers[0][1] == "U_OWNER" and "Kept under Ideas" in office.whispers[0][2]
    assert "already" in office.whispers[1][2]


async def test_a_bulb_from_anyone_else_is_ignored_silently() -> None:
    office = IdeaOffice()
    office.members = {"U_MEMBER"}
    office.messages[("C1", "1700.01")] = "Hello"
    run, replies = handler(office)
    await run.handle(bulb(user="U_MEMBER"))
    assert office.kept == [] and office.whispers == [] and replies.sent == []


async def test_a_message_it_cannot_read_is_said_privately() -> None:
    office = IdeaOffice()
    run, _ = handler(office)
    await run.handle(bulb())
    assert office.kept == [] and "idea:" in office.whispers[0][2]


def test_a_bulb_reaction_is_parsed_and_others_are_not() -> None:
    from app.channels.socket_mode import request_from_event

    def event(reaction: str, item_type: str = "message") -> dict[str, Any]:
        return {
            "team_id": "T1",
            "event_id": "E9",
            "event": {
                "type": "reaction_added",
                "user": "U_OWNER",
                "reaction": reaction,
                "item": {"type": item_type, "channel": "C1", "ts": "1700.01"},
            },
        }

    parsed = request_from_event(event("bulb"), "U_BOT")
    assert parsed is not None
    assert (parsed.reaction, parsed.channel_id, parsed.message_ts) == ("bulb", "C1", "1700.01")
    assert request_from_event(event("thumbsup"), "U_BOT") is None
    assert request_from_event(event("bulb", "file"), "U_BOT") is None

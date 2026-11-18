"""What the office posts in Slack: the answer in Slack's marks, and its buttons."""

from app.channels import slack_format
from app.channels.results import stop_text
from app.documents.model import Bullets, Document, Paragraph, Section, Sources, Table


def test_marks_become_slacks_own() -> None:
    text = "**Price** is *rising*, see [the report](https://example.org/r)"
    expected = "*Price* is _rising_, see <https://example.org/r|the report>"
    assert slack_format.mrkdwn(text) == expected


def test_angle_brackets_cannot_become_a_mention() -> None:
    assert slack_format.mrkdwn("<!channel> & <@U1>") == "&lt;!channel&gt; &amp; &lt;@U1&gt;"


def test_the_answer_leaves_out_the_working_and_the_sources() -> None:
    document = Document(
        "Vendors",
        sections=[
            Section("Recommendation", [Paragraph("Pick **B**."), Bullets(["cheaper", "faster"])]),
            Section("Sources", [Sources(["https://example.org"])]),
            Section("How it got there", [Paragraph("Searched a lot.")], supporting=True),
        ],
    )
    text, cut = slack_format.answer_text(document)
    assert not cut
    assert text.startswith("*Recommendation*\nPick *B*.\n• cheaper\n• faster")
    assert "Searched a lot" not in text
    assert "example.org" not in text


def test_a_table_reads_as_lines_and_says_how_many_rows_are_left() -> None:
    rows = [[f"V{index}", str(index)] for index in range(12)]
    document = Document("t", sections=[Section("Compared", [Table(["Vendor", "Price"], rows)])])
    text, _ = slack_format.answer_text(document)
    assert "• Vendor: V0; Price: 0" in text
    assert "…and 4 more rows in the file." in text


def test_a_long_answer_is_cut_at_a_line_end_and_says_so() -> None:
    lines = [Paragraph(f"Line {index} " + "x" * 80) for index in range(200)]
    text, cut = slack_format.answer_text(Document("t", sections=[Section("Long", lines)]))
    assert cut
    assert len(text) <= slack_format.ANSWER_LIMIT
    assert text.endswith("x")


def test_text_is_split_into_blocks_slack_accepts() -> None:
    pieces = slack_format.chunks("\n".join("y" * 1000 for _ in range(7)))
    assert all(len(piece) <= slack_format.BLOCK_TEXT for piece in pieces)
    assert "".join(piece.replace("\n", "") for piece in pieces) == "y" * 7000


def test_a_proposal_offers_start_and_not_now() -> None:
    blocks = slack_format.proposal_blocks("Shall I?", likely_project=False)
    buttons = blocks[-1]["elements"]
    assert [b["action_id"] for b in buttons] == [slack_format.START, slack_format.NOT_NOW]
    assert buttons[0]["style"] == "primary"


def test_a_likely_project_also_offers_a_new_one() -> None:
    blocks = slack_format.proposal_blocks("Shall I?", likely_project=True)
    assert slack_format.NEW in [b["action_id"] for b in blocks[-1]["elements"]]


def _task(buttons: tuple[dict[str, object], ...]) -> list[dict[str, object]]:
    card = slack_format.Card("Agent task", "Compare CRMs", "running", "Running", buttons=buttons)
    return slack_format.card_blocks(card)


def test_stop_asks_before_it_stops_and_carries_the_task() -> None:
    blocks = _task(slack_format.task_buttons("run-1", None, running=True, stuck=False))
    stop = blocks[-1]["elements"][-1]  # type: ignore[index]
    assert stop["action_id"] == slack_format.STOP
    assert stop["value"] == "run-1"
    assert stop["confirm"]["title"]["text"] == "Stop this task?"


def test_a_stuck_task_offers_skip_and_try_again() -> None:
    blocks = _task(slack_format.task_buttons("run-1", None, running=False, stuck=True))
    actions = [b["action_id"] for b in blocks[-1]["elements"]]  # type: ignore[index]
    assert actions == [slack_format.SKIP, slack_format.RETRY, slack_format.STOP]


def test_finished_progress_without_a_link_has_no_buttons() -> None:
    blocks = _task(slack_format.task_buttons("r", None, running=False, stuck=False))
    assert all(block["type"] != "actions" for block in blocks)


def test_rework_warns_it_may_cost_and_open_links_to_the_task() -> None:
    blocks = slack_format.finished_blocks("Done", "run-1", "https://office.example/#task/run-1")
    rework, opener = blocks[-1]["elements"]
    assert "paid" in rework["confirm"]["text"]["text"]
    assert opener["url"] == "https://office.example/#task/run-1"


def test_known_stop_reasons_are_said_in_words() -> None:
    assert stop_text("providers_unavailable:openai") == (
        "The model providers were unavailable or refusing."
    )
    assert stop_text("researcher:unverified_source") == "researcher:unverified source"
    assert stop_text(None) == "The office could not finish on its own."


def test_a_schedule_says_when_it_runs() -> None:
    from types import SimpleNamespace

    from app.scheduler import cadence

    weekly = SimpleNamespace(frequency="weekly", weekday=4, local_time="17:30")
    daily = SimpleNamespace(frequency="daily", weekday=None, local_time="08:00")
    assert cadence(weekly) == "every Friday at 17:30"  # type: ignore[arg-type]
    assert cadence(daily) == "every day at 08:00"  # type: ignore[arg-type]


def test_a_card_reads_context_title_body_facts_then_buttons() -> None:
    card = slack_format.Card(
        "Agent task",
        "Compare <three> CRMs",
        "running",
        "Running · step 3",
        about=("Tim", "Northwind"),
        body="✓ Planning",
        facts=(("Time", "12 min"), ("Cost", "$0.38 of $2.00")),
        footer="_Reply here._",
        buttons=slack_format.task_buttons("run-1", None, running=True, stuck=False),
    )
    blocks = slack_format.card_blocks(card)
    assert [b["type"] for b in blocks] == [
        "context",
        "section",
        "section",
        "section",
        "context",
        "actions",
    ]
    assert blocks[0]["elements"][0]["text"] == (
        "🔵 *Running · step 3*  ·  Agent task  ·  Tim  ·  Northwind"
    )
    assert blocks[1]["text"]["text"] == "*Compare &lt;three&gt; CRMs*", "the title is escaped"
    assert [f["text"] for f in blocks[3]["fields"]] == ["*Time*\n12 min", "*Cost*\n$0.38 of $2.00"]
    text = slack_format.card_text(card)
    assert "Time: 12 min  ·  Cost: $0.38 of $2.00" in text and "✓ Planning" in text


def test_a_low_risk_approval_offers_approve_and_reject_with_its_digest() -> None:
    buttons = slack_format.approval_buttons("a-1", "d" * 64, "https://o.example/#task/r", True)
    approve, reject, seen = buttons
    assert approve["action_id"] == slack_format.APPROVE and approve["style"] == "primary"
    assert approve["value"] == "a-1:" + "d" * 64 and approve["confirm"]
    assert reject["action_id"] == slack_format.REJECT and reject["confirm"]
    assert seen["url"] == "https://o.example/#task/r"


def test_any_other_approval_offers_only_the_way_to_read_it() -> None:
    buttons = slack_format.approval_buttons("a-1", "d" * 64, "https://o.example/#task/r", False)
    assert [b["action_id"] for b in buttons] == [slack_format.OPEN]
    assert slack_format.approval_buttons("a-1", "d" * 64, None, False) == ()


def test_a_limit_warning_is_an_alert_card_with_its_severity() -> None:
    from app.channels.notifications import limit_card, limit_text
    from app.db.models import LimitAlert

    alert = LimitAlert(
        scope="office", subject="office", name="", window="month", threshold=100, share=1.02
    )
    card = limit_card(alert, None)
    assert (card.kind, card.tone, card.status) == ("System alert", "alert", "Limit reached")
    assert dict(card.facts) == {"Severity": "High", "Limit": "The office's month", "Used": "102%"}
    assert "New work waits" in card.body
    assert limit_text(alert) == (
        "Quarterdeck: The office has used all of this month's spending limit. New work waits "
        "until the month turns or you raise it. Open your office to see it."
    )
    near = LimitAlert(
        scope="task", subject="t", name="Pricing", window="run", threshold=80, share=0.81
    )
    assert dict(limit_card(near, None).facts)["Severity"] == "Medium"

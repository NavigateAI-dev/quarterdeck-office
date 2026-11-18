"""An objective is the work, not the conversation around it."""

from app.research.objective import strip_instructions

KYC = (
    "拟议项目：*咖啡店的收银系统*\n\n"
    "调研范围：\n"
    "- 小型咖啡店常用的 POS 收银与会员系统\n"
    "- 提供这些能力的 vendor、产品、价格及集成方式\n\n"
    "请回复「是」确认；确认后才能在办公室电脑上启动。"
)


def test_a_confirmation_prompt_does_not_become_the_task() -> None:
    """The KYC run answered "yes, confirmed to start" instead of the question."""
    kept = strip_instructions(KYC)
    assert "确认" not in kept, kept
    assert "POS" in kept and "vendor" in kept


def test_an_english_confirmation_line_is_removed() -> None:
    kept = strip_instructions(
        "Research the EU AI act for our clients.\nPlease reply yes to confirm."
    )
    assert kept == "Research the EU AI act for our clients."


def test_the_work_itself_is_never_removed() -> None:
    plain = "Compare three KYC vendors for Dutch EMIs"
    assert strip_instructions(plain) == plain


def test_an_objective_that_is_only_an_instruction_is_left_alone() -> None:
    """Stripping everything would leave the office with no task at all."""
    only = "Please confirm the vendor list"
    assert strip_instructions(only) == only


def test_a_short_task_with_a_chinese_confirmation_line_is_cleaned() -> None:
    asked = "Compare KYC vendors\n\n请回复「是」确认；确认后才能在办公室电脑上启动。"
    kept = strip_instructions(asked)
    assert kept == "Compare KYC vendors"


def test_an_instruction_in_the_middle_is_kept() -> None:
    """Only trailing lines are conversation; a middle line may be the work."""
    text = "Confirm which vendors are certified.\nThen compare their coverage in NL."
    assert strip_instructions(text) == text


def test_every_step_is_given_the_work_not_the_conversation() -> None:
    """Stripping it for planning alone left the chief answering "yes, confirm".

    The first re-run still concluded 是，确认继续开展调研 because only the planner
    saw the cleaned objective.
    """
    from pathlib import Path

    # The pipeline's nodes live one module per phase since the split.
    body = "".join(path.read_text() for path in Path("app/orchestrator/nodes").glob("*.py"))
    assert 'state["objective"]' not in body, "an agent is still given the raw objective"
    assert body.count("asked_of(state)") >= 5

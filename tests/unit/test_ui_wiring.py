"""The office's buttons and forms must reach real handlers.

These read the shipped scripts rather than a browser: they catch a button
whose handler was renamed or never written, and a setup step that only works
because another screen happened to load its data first.
"""

import re
from pathlib import Path

UI = Path("app/api/web/office")
# Handled centrally in main.js rather than by a screen.
BUILT_IN = {"close", "retry", "sign-out"}
# What each screen keeps in memory once it has rendered.
SCREEN_CACHES = (
    "Conn",
    "AgentState",
    "Knowledge",
    "Tools",
    "Projects",
    "Board",
    "TaskRoom",
    "RoomState",
    "Setup",
)
ACTION_IN_MARKUP = re.compile(r'data-action="([a-z][a-z-]*)')
ACTION_IN_BUTTON = re.compile(r'btn\(\s*(?:[^,]|\([^)]*\))+,\s*[`"]([a-z][a-z-]*)')
FORM_IN_MARKUP = re.compile(r'<form[^>]*id="([a-z][a-z-]*)"')
FORM_IN_MODAL = re.compile(r'formModal\(\s*(?:[^,]|\([^)]*\))+,\s*"([a-z][a-z-]*)"')
# A key may be written `name: …`, `"name": …` or, for a named function, `name,`.
KEY = re.compile(r'^\s{2}"?([a-z][a-z-]*)"?(?::\s|,$)', re.M)
DIRECT = re.compile(r'Office\.(actions|forms)\[?"?([a-z][a-z-]*)"?\]?\s*=\s*(?!Object)')


def scripts() -> list[Path]:
    return sorted(UI.glob("*.js"))


def registered(kind: str) -> set[str]:
    """Every handler name the office registers, however it registers it."""
    names: set[str] = set()
    for script in scripts():
        text = script.read_text()
        for match in re.finditer(rf"Office\.{kind} = Object\.assign\(", text):
            block = text[match.end() :]
            depth = 0
            for index, character in enumerate(block):
                depth += character in "{(["
                depth -= character in "})]"
                if depth <= 0 and character == ")":
                    block = block[:index]
                    break
            names.update(KEY.findall(block))
        names.update(name for kind_found, name in DIRECT.findall(text) if kind_found == kind)
    return names


def used(patterns: list[re.Pattern[str]]) -> set[str]:
    names: set[str] = set()
    for script in scripts():
        text = script.read_text()
        for pattern in patterns:
            names.update(pattern.findall(text))
    return names


def test_every_button_reaches_a_handler() -> None:
    missing = used([ACTION_IN_MARKUP, ACTION_IN_BUTTON]) - registered("actions") - BUILT_IN
    assert missing == set(), f"buttons with no handler: {sorted(missing)}"


def test_every_form_reaches_a_handler() -> None:
    missing = used([FORM_IN_MARKUP, FORM_IN_MODAL]) - registered("forms") - {"token-form"}
    assert missing == set(), f"forms with no handler: {sorted(missing)}"


def _balanced(text: str) -> str:
    """The value that follows a key, up to the comma that ends it."""
    depth = 0
    for index, character in enumerate(text):
        depth += character in "{(["
        depth -= character in "})]"
        if depth < 0:
            return text[:index]
        if depth == 0 and character == ",":
            return text[:index]
    return text


def handler_body(name: str) -> str:
    """One action handler's source, following a named function when it is one."""
    for script in scripts():
        text = script.read_text()
        start = re.search(rf'^\s{{2}}"?{re.escape(name)}"?:\s', text, re.M)
        if not start:
            continue
        value = _balanced(text[start.end() :]).strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", value):
            for other in scripts():
                source = other.read_text()
                found = re.search(rf"^(?:async )?function {re.escape(value)}\(", source, re.M)
                if found:
                    # Read the function's body, which starts at its first brace.
                    brace = source.index("{", found.end())
                    return _balanced(source[brace + 1 :])
            return ""
        return value
    return ""


def test_setup_steps_load_their_own_data() -> None:
    """Setup opens other screens' dialogs, so each must fetch what it shows.

    Without this, a dialog opened from Setup renders empty because the screen
    that fills its cache was never visited.
    """
    setup = (UI / "setup.js").read_text()
    actions = re.findall(r'\["[^"]+", "([a-z][a-z-]*)"\]', setup)
    assert actions, "setup declares no step actions"
    for action in actions:
        if action.startswith("setup-"):
            continue  # setup's own dialogs read the state this screen loaded
        body = handler_body(action)
        assert body, f"no handler found for setup step action: {action}"
        cached = [name for name in SCREEN_CACHES if f"{name}." in body]
        if cached and "api(" not in body:
            raise AssertionError(
                f"{action} reads {cached[0]} but fetches nothing: opened from Setup it would be "
                "empty, because the screen that fills that cache was never visited"
            )


def test_each_ceiling_offers_the_limit_it_actually_raises() -> None:
    """A cheaper model saves money, not tokens.

    Offering "use a cheaper model" for a token ceiling would be advice that
    cannot work, so the token wording says so instead.
    """
    board = (UI / "board.js").read_text()
    block = board[board.index("const CEILING = {") : board.index("async function ceilingQuestion")]
    for kind, expected in (
        ("tokens", "max_tokens"),
        ("cost", "max_cost_usd"),
        ("time", "max_minutes"),
    ):
        section = block[block.index(f"  {kind}: {{") :]
        section = section[: section.index("},")]
        assert f'field: "{expected}"' in section, f"{kind} must raise {expected}"
    tokens = block[block.index("  tokens: {") : block.index("  cost: {")]
    assert "not use fewer" in tokens, "the token ceiling must not promise a cheaper model"
    cheaper = block[block.index("  cost: {") : block.index("  time: {")]
    assert "cheaper model" in cheaper, "a cost ceiling is where a cheaper model helps"


def test_the_tools_screen_offers_exactly_the_actions_section_18_holds_back() -> None:
    """A tag the screen offers must be one the API accepts, and every action
    section 18 denies or gates must be offerable, or a tool doing it could not
    be held back from the screen."""
    from app.policy import POLICY, Permission

    script = (UI / "tools.js").read_text()
    block = script[
        script.index("const SECTION_18 = {") : script.index("};", script.index("const SECTION_18"))
    ]
    offered = dict(re.findall(r'^\s{2}([a-z_]+): \[".*", "(deny|approval)"\]', block, re.M))
    held = {
        action: "deny" if permission is Permission.DENY else "approval"
        for action, permission in POLICY.items()
        if permission is not Permission.AUTO
    }
    assert offered == held

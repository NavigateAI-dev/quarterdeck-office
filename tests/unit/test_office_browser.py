"""The office's own browser: where it may go, what it asks."""

import socket
from typing import Any

import pytest

from app.browser import safety
from app.browser.agent import BrowserMove, as_research, element_of, page_for_agent


@pytest.fixture
def resolve(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    table = {
        "example.com": "93.184.216.34",
        "office.lan": "192.168.1.20",
        "sneaky.test": "127.0.0.1",
    }

    def fake(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        if host not in table:
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table


def test_only_the_public_web_is_reachable(resolve: dict[str, str]) -> None:
    assert safety.url_allowed("https://example.com/page")
    for refused in (
        "http://office.lan/",
        "http://sneaky.test/",
        "http://localhost:8000/",
        "http://127.0.0.1:8000/health",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "chrome://settings",
        "http://api:8000/",
    ):
        assert not safety.url_allowed(refused), refused


@pytest.mark.parametrize(
    ("action", "element", "submit", "asks"),
    [
        ("click", {"label": "Sign in", "type": "button"}, False, True),
        ("click", {"label": "立即购买", "type": "button"}, False, True),
        ("click", {"label": "Pay now", "type": "submit"}, False, True),
        ("click", {"label": "Next page", "type": ""}, False, False),
        ("type", {"label": "Password", "type": "password"}, False, True),
        ("type", {"label": "Search", "type": "search"}, False, False),
        ("type", {"label": "Email", "type": "email"}, True, True),
        # 8 Oct: a site search is not a form that does something in the world.
        ("type", {"label": "Search Wikipedia", "type": "search"}, True, False),
        ("type", {"label": "Zoeken", "type": "text"}, True, False),
        ("type", {"label": "搜索", "type": "text"}, True, False),
        ("scroll", None, False, False),
    ],
)
def test_presses_that_act_in_the_world_ask_the_owner(
    action: str, element: dict[str, str] | None, submit: bool, asks: bool
) -> None:
    assert (safety.needs_owner(action, element, submit) is not None) is asks


PAGE = {
    "url": "https://example.com/",
    "title": "Example Domain",
    "text": "This domain is for use in examples.",
    "elements": [
        {
            "n": 1,
            "tag": "a",
            "type": "",
            "role": "",
            "label": "Learn more",
            "href": "https://iana.org",
            "value": "",
            "in_view": True,
        },
        {
            "n": 2,
            "tag": "input",
            "type": "password",
            "role": "",
            "label": "Password",
            "href": "",
            "value": "",
            "in_view": False,
        },
    ],
}


def test_the_page_is_told_as_text_with_numbered_elements() -> None:
    told = page_for_agent(PAGE)
    assert "Address: https://example.com/" in told and "[1] a: Learn more" in told
    assert "[2] password: Password [below]" in told
    assert "Start with goto" in page_for_agent({"url": "about:blank"})
    assert element_of(PAGE, 2)["type"] == "password" and element_of(PAGE, 9) is None


def test_what_the_step_found_reads_as_research() -> None:
    from app.schemas import ResearchReport

    move = BrowserMove(
        action="done", answer="Three plans are offered.", findings=["Basic is $5 (pricing page)"]
    )
    found = as_research(
        "What plans does it offer?",
        move,
        [{"url": "https://example.com/pricing", "title": "Pricing"}],
    )
    ResearchReport.model_validate(found)
    assert found["key_findings"] == ["Basic is $5 (pricing page)"]
    assert found["sources"][0]["url"] == "https://example.com/pricing"
    unfinished = as_research("x", None, [])
    ResearchReport.model_validate(unfinished)
    assert unfinished["confidence"] == "low"


class Asking:
    """The owner's rule: asks until the owner allows, then lets it through."""

    def __init__(self) -> None:
        self.allowed = False
        self.asked: list[str] = []

    def ask_owner(self, subject: str, reason: str, target: str, **data: Any) -> None:
        from app.policies.engine import Decision, Verdict
        from app.policies.store import PolicyAsk

        self.asked.append(reason)
        if not self.allowed:
            raise PolicyAsk(Decision(Verdict.ASK, reason, subject), 1)


SHOP = {
    "url": "https://shop.example.com/cart",
    "title": "Cart",
    "text": "Your cart",
    "elements": [
        {"n": 1, "tag": "a", "type": "", "label": "Cart", "href": "", "value": "", "in_view": True},
        {
            "n": 2,
            "tag": "button",
            "type": "submit",
            "label": "Pay now",
            "href": "",
            "value": "",
            "in_view": True,
        },
    ],
}


def fake_browser(acts: list[dict[str, Any]]) -> Any:
    class Fake:
        def __init__(self, exchange: Any, session: Any = None) -> None:
            pass

        async def open(self) -> dict[str, Any]:
            return {"page": {"url": "about:blank", "elements": []}}

        async def act(self, action: dict[str, Any]) -> dict[str, Any]:
            acts.append(dict(action))
            return {"page": SHOP | {"shot": f"shots/s/{len(acts)}.jpg"}}

        async def close(self) -> None:
            return None

        def shot(self, page: dict[str, Any]) -> bytes:
            return b"jpg"

    return Fake


async def test_after_the_owner_allows_the_step_replays_its_moves_and_carries_on(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CU4, 8 Oct: allowing a press used to start the step over, paying again."""
    from types import SimpleNamespace

    from app.browser import agent as browsing
    from app.policies.store import PolicyAsk

    acts: list[dict[str, Any]] = []
    monkeypatch.setattr(browsing, "ExchangeBrowser", fake_browser(acts))
    settings = SimpleNamespace(
        structured_output_attempts=1, research_model_alias="r", browser_exchange=tmp_path
    )
    agent = browsing.BrowserAgent(None, settings)  # type: ignore[arg-type]
    agent.gate = Asking()
    agent.replay_dir = tmp_path / "run"
    recorded: list[dict[str, Any]] = []
    agent.on_step = lambda step, shot: recorded.append(step)
    script = [
        BrowserMove(action="goto", text="https://shop.example.com/cart"),
        BrowserMove(action="click", element=2),
        BrowserMove(action="done", answer="Paid.", findings=["The order went through"]),
    ]
    calls: list[str] = []

    async def scripted(objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        calls.append(inputs)
        return script.pop(0)

    monkeypatch.setattr(agent, "run", scripted)
    with pytest.raises(PolicyAsk):
        await agent.browse("Pay for the cart", None)  # type: ignore[arg-type]
    assert [a["kind"] for a in acts] == ["goto"], "it stopped before pressing Pay"
    assert len(calls) == 2 and len(recorded) == 1
    agent.gate.allowed = True
    found = await agent.browse("Pay for the cart", None)  # type: ignore[arg-type]
    assert [a["kind"] for a in acts] == ["goto", "goto", "click"], "replayed, then pressed"
    assert len(calls) == 3, "the replay asked the model nothing; one call to finish"
    assert [r["action"] for r in recorded] == ["goto", "click"], "each move recorded once"
    assert found["key_findings"] == ["The order went through"]
    assert not list((tmp_path / "run").glob("replay-*.json")), "the replay is used once"


def test_an_address_is_kept_without_its_secrets() -> None:
    """8 Oct: a login page's address carried its sign-in token into the record."""
    kept = safety.clean_url(
        "https://auth.wikimedia.org/w/Special:UserLogin?returnto=Rotterdam"
        "&centralauthLoginToken=04d5&session_id=9&page=2#top"
    )
    assert kept == "https://auth.wikimedia.org/w/Special:UserLogin?returnto=Rotterdam&page=2"


LOOT = {
    "url": "https://www.loot-drop.io/",
    "title": "Loot Drop",
    "text": "Case studies",
    "elements": [{"n": 1, "tag": "a", "label": "Case studies", "in_view": True}],
}


def stuck_browser(acts: list[dict[str, Any]]) -> Any:
    """A site whose link never answers, like loot-drop.io on 8 Oct."""

    class Fake:
        def __init__(self, exchange: Any, session: Any = None) -> None:
            pass

        async def open(self) -> dict[str, Any]:
            return {"page": LOOT}

        async def act(self, action: dict[str, Any]) -> dict[str, Any]:
            acts.append(dict(action))
            if action["kind"] == "click":
                return {"page": LOOT, "error": "action_timed_out"}
            return {"page": LOOT}

        async def close(self) -> None:
            return None

        def shot(self, page: dict[str, Any]) -> bytes:
            return b"jpg"

    return Fake


def browsing_agent(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, acts: list[Any]) -> Any:
    from types import SimpleNamespace

    from app.browser import agent as browsing

    monkeypatch.setattr(browsing, "ExchangeBrowser", stuck_browser(acts))
    settings = SimpleNamespace(
        structured_output_attempts=1, research_model_alias="r", browser_exchange=tmp_path
    )
    agent = browsing.BrowserAgent(None, settings)  # type: ignore[arg-type]
    agent.replay_dir = tmp_path / "run"
    return agent


def always(move: BrowserMove, seen: list[str]) -> Any:
    async def scripted(objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        seen.append(inputs)
        return move

    return scripted


async def test_a_failing_move_is_not_repeated_and_the_step_stops_as_stuck(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OI1: the same link clicked ten times, each timing out."""
    acts: list[dict[str, Any]] = []
    agent = browsing_agent(tmp_path, monkeypatch, acts)
    seen: list[str] = []
    monkeypatch.setattr(agent, "run", always(BrowserMove(action="click", element=1), seen))
    found = await agent.browse("Read the case studies", None)  # type: ignore[arg-type]
    assert len(acts) == 2, "a move that failed twice is not sent a third time"
    assert len(seen) == 3
    assert found["stuck"]["what"] == "click 'Case studies' on www.loot-drop.io"
    assert found["stuck"]["tries"] == 2
    assert "Do not repeat them" in seen[-1]


async def test_a_retry_remembers_what_failed_before(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    acts: list[dict[str, Any]] = []
    agent = browsing_agent(tmp_path, monkeypatch, acts)
    seen: list[str] = []
    monkeypatch.setattr(agent, "run", always(BrowserMove(action="click", element=1), seen))
    await agent.browse("Read the case studies", None)  # type: ignore[arg-type]
    acts.clear()
    seen.clear()
    again = await agent.browse("Read the case studies", None)  # type: ignore[arg-type]
    assert acts == [], "after a pause the same failing click is not made again"
    assert "click 'Case studies' on www.loot-drop.io: action_timed_out" in seen[0]
    assert again["stuck"]


async def test_a_note_sent_while_it_browses_is_read_before_the_next_move(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    acts: list[dict[str, Any]] = []
    agent = browsing_agent(tmp_path, monkeypatch, acts)
    notes = [["Use the sitemap at /sitemap.xml"]]
    agent.new_notes = lambda: notes.pop() if notes else []
    script = [
        BrowserMove(action="scroll"),
        BrowserMove(action="done", answer="Found the list.", findings=["42 case studies"]),
    ]
    seen: list[str] = []

    async def scripted(objective: str, context: Any, inputs: str = "", focus: str = "") -> Any:
        seen.append(inputs)
        return script.pop(0)

    monkeypatch.setattr(agent, "run", scripted)
    found = await agent.browse("Read the case studies", None, notes="Focus on 2025")  # type: ignore[arg-type]
    assert "Use the sitemap at /sitemap.xml" in seen[0] and "Focus on 2025" in seen[0]
    assert "stuck" not in found


async def test_a_step_skipped_while_it_runs_stops_at_the_next_move(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    acts: list[dict[str, Any]] = []
    agent = browsing_agent(tmp_path, monkeypatch, acts)
    agent.skipped = lambda: {"s2"}
    seen: list[str] = []
    monkeypatch.setattr(agent, "run", always(BrowserMove(action="scroll"), seen))
    found = await agent.browse("Read the case studies", None, step_id="s2")  # type: ignore[arg-type]
    assert seen == [] and acts == []
    assert "stuck" not in found

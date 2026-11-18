"""The office's rules, as policies: composition, counters and refusals."""

from uuid import uuid4

import pytest

from app.policies import builtin  # noqa: F401 - registers the built-in policies
from app.policies.engine import (
    REGISTRY,
    ConfiguredPolicy,
    Decision,
    Hook,
    PolicyEvent,
    PolicyKind,
    Verdict,
    evaluate,
    register,
)


def policy(kind: str, scope: str = "office", **params: object) -> ConfiguredPolicy:
    return ConfiguredPolicy(id=uuid4(), kind=kind, scope=scope, params=dict(params))


def fetch_event(url: str = "https://example.com/pricing", **state: int) -> PolicyEvent:
    return PolicyEvent(
        hook=Hook.TOOL_CALL, target="web_fetch", data={"url": url}, state=dict(state)
    )


def test_with_no_policies_the_office_works_as_before() -> None:
    assert evaluate(fetch_event(), []).verdict is Verdict.ALLOW


def test_a_refusal_says_which_rule_refused_and_why() -> None:
    decision = evaluate(fetch_event(), [policy("no_web")])
    assert decision.verdict is Verdict.DENY
    assert decision.policy == "no_web"
    assert "own documents" in decision.reason


def test_the_most_restrictive_answer_wins_whatever_the_order() -> None:
    strict = policy("no_web")
    gentle = policy("ask_before_web")
    assert evaluate(fetch_event(), [gentle, strict]).verdict is Verdict.DENY
    assert evaluate(fetch_event(), [strict, gentle]).verdict is Verdict.DENY


def test_a_refusal_stops_later_policies_from_being_asked() -> None:
    asked: list[str] = []

    def noisy(event: PolicyEvent, params: dict[str, object]) -> Decision | None:
        asked.append(event.target)
        return Decision(Verdict.ALLOW)

    register(
        PolicyKind(
            id="noisy",
            title="Noisy",
            about="Records that it was asked.",
            hooks=(Hook.TOOL_CALL,),
            parameters={},
            evaluate=noisy,
        )
    )
    evaluate(fetch_event(), [policy("no_web"), policy("noisy")])
    assert asked == []


def test_an_allow_list_permits_its_hosts_and_refuses_the_rest() -> None:
    allowed = [policy("web_allow_list", hosts=["docs.example.com", "*.gov.uk"])]
    assert evaluate(fetch_event("https://docs.example.com/a"), allowed).verdict is Verdict.ALLOW
    assert evaluate(fetch_event("https://tax.service.gov.uk/x"), allowed).verdict is Verdict.ALLOW
    refused = evaluate(fetch_event("https://elsewhere.test/x"), allowed)
    assert refused.verdict is Verdict.DENY and "elsewhere.test" in refused.reason


def test_asking_before_the_web_stops_asking_once_the_owner_has_agreed() -> None:
    rule = [policy("ask_before_web")]
    assert evaluate(fetch_event(), rule).verdict is Verdict.ASK
    assert evaluate(fetch_event(web_allowed=1), rule).verdict is Verdict.ALLOW


def test_a_task_that_does_a_lot_pauses_for_the_owner() -> None:
    rule = [policy("risk_score", ask_at=3)]
    first = evaluate(fetch_event(), rule)
    assert first.verdict is Verdict.ALLOW and first.add == {"risk": 1}
    later = evaluate(fetch_event(risk=2), rule)
    assert later.verdict is Verdict.ASK and "pausing for you" in later.reason


def test_tool_calls_can_be_limited_per_task() -> None:
    rule = [policy("max_tool_calls", limit=2)]
    assert evaluate(fetch_event(tool_calls=1), rule).verdict is Verdict.ALLOW
    stopped = evaluate(fetch_event(tool_calls=2), rule)
    assert stopped.verdict is Verdict.DENY and "its limit" in stopped.reason


def test_a_costly_model_waits_for_the_owner() -> None:
    event = PolicyEvent(hook=Hook.MODEL_CALL, target="chief", data={"model": "claude-opus-5"})
    decision = evaluate(event, [policy("expensive_models", models=["opus"])])
    assert decision.verdict is Verdict.ASK and "opus" in decision.reason.lower()
    cheap = PolicyEvent(hook=Hook.MODEL_CALL, target="chief", data={"model": "gpt-5-mini"})
    assert evaluate(cheap, [policy("expensive_models", models=["opus"])]).verdict is Verdict.ALLOW


def test_a_policy_only_answers_at_its_own_hook() -> None:
    model_event = PolicyEvent(hook=Hook.MODEL_CALL, target="researcher", data={"model": "m"})
    assert evaluate(model_event, [policy("no_web")]).verdict is Verdict.ALLOW


def test_every_shipped_policy_describes_itself() -> None:
    for kind in REGISTRY.values():
        assert kind.title and kind.about and kind.hooks
        assert kind.about.endswith(".")


@pytest.mark.parametrize("kind", ["no_web", "web_allow_list", "risk_score"])
def test_shipped_policies_are_registered(kind: str) -> None:
    assert kind in REGISTRY

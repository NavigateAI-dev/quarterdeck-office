"""What must not leave: found in any language, and masked without breaking messages."""

import json

import pytest

from app.privacy.detect import masked, scan
from app.privacy.outbound import outgoing


def kinds(text: str) -> list[str]:
    return [finding.kind for finding in scan(text)]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("My card is 4111 1111 1111 1111.", "card"),
        ("请把 4111111111111111 的账单发给我", "card"),
        ("IBAN NL91ABNA0417164300", "iban"),
        ("rekening NL91 ABNA 0417 1643 00 graag", "iban"),
        ("Key sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456", "api_key"),
        ("token xoxb-123456789012-abcdefghijkl", "api_key"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "api_key"),
        ("password: Hunter2!", "password"),
        ("密码是 abc123", "password"),
        ("Mijn BSN is 111222333", "bsn"),
        ("身份证号 11010519491231002X，", "cn_id"),
        ("护照 E12345678。", "passport"),
        ("passport number: AB1234567", "passport"),
        ("手机 13800138000", "cn_mobile"),
        ("mail zhang@example.cn now", "email"),
        ("call +31 6 12345678.", "phone"),
    ],
)
def test_each_kind_is_found(text: str, kind: str) -> None:
    assert kinds(text) == [kind]


@pytest.mark.parametrize(
    "text",
    [
        "Acme weekly update about delivery routes; invoice 1200 due Friday.",
        "我们讨论了配送路线和营业执照，分期3支付货款1200。",
        "order 111222333 shipped",  # passes a BSN's check, and nothing names it one
        "NL91ABNA0417164301",  # one digit off
        "4111 1111 1111 1112",
        "1780000000000 is a timestamp",
        "11010519491331002X",  # no thirteenth month
        "the passport renewal is due",
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert masked(text)[0] == text


def test_masking_replaces_only_the_value_and_keeps_contact_details() -> None:
    text, done = masked("password: Hunter2! IBAN NL91ABNA0417164300 mail jan@example.com")
    assert "Hunter2" not in text and "NL91" not in text
    assert "password: [password hidden by the office]" in text
    assert "jan@example.com" in text
    assert done == {"password": 1, "iban": 1}


def test_masking_twice_changes_nothing() -> None:
    once = masked("BSN 111222333")[0]
    assert masked(once)[0] == once


def test_outgoing_masks_every_place_words_travel_and_leaves_the_rest() -> None:
    given = [
        {"role": "system", "content": "BSN 111222333"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "IBAN NL91ABNA0417164300"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,4111111111111111"},
                },
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "4111111111111111",
                    "type": "function",
                    "function": {"name": "t", "arguments": json.dumps({"p": 'password: "x"y'})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "card 4111 1111 1111 1111"},
    ]
    before = json.dumps(given)
    sent = outgoing(given)
    assert json.dumps(given) == before, "what the office keeps stays as it was said"
    assert "111222333" not in sent[0]["content"]
    assert "NL91" not in sent[1]["content"][0]["text"]
    assert sent[1]["content"][1] == given[1]["content"][1]
    call = sent[2]["tool_calls"][0]
    assert call["id"] == "4111111111111111"
    assert "hidden by the office" in json.loads(call["function"]["arguments"])["p"]
    assert "4111" not in sent[3]["content"]


async def test_the_last_step_before_a_provider_masks() -> None:
    from types import SimpleNamespace
    from typing import Any

    from app.gateway.direct import DirectCompleter

    completer = DirectCompleter(
        "openai", "m", "k", SimpleNamespace(llm_timeout_seconds=5, llm_max_output_tokens=10), "m"
    )
    sent: list[Any] = []

    async def caught(messages: list[dict[str, Any]], *_: Any) -> Any:
        sent.append(messages)
        raise TimeoutError

    completer._openai = caught  # type: ignore[method-assign]
    with pytest.raises(Exception, match="provider_timeout"):
        await completer.complete("m", [{"role": "user", "content": "BSN 111222333"}])
    assert "111222333" not in sent[0][0]["content"]

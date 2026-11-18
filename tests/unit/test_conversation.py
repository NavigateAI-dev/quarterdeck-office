from app.channels.conversation import agent_persona, build_messages, used_tokens


def test_unknown_usage_is_charged_at_the_reservation() -> None:
    assert used_tokens(None, 5, reserved=700) == 700
    assert used_tokens(600, 200, reserved=900) == 800


def test_messages_carry_facts_history_and_the_question_in_order() -> None:
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    messages = build_messages({"approvals_waiting": 1}, history, "c")
    assert messages[0]["role"] == "system" and '"approvals_waiting": 1' in messages[0]["content"]
    assert "Never send the owner to their computer" in messages[0]["content"]
    assert "Approvals are the one thing made only in the office" in messages[0]["content"]
    assert messages[1:] == [*history, {"role": "user", "content": "c"}]


def test_an_agent_speaks_as_itself_with_its_instructions_and_plain_skills() -> None:
    entry = {
        "name": "Pricing researcher",
        "role": "researcher",
        "instructions": "Always quote list prices in EUR.",
        "skills": [
            {"name": "Source hygiene", "instructions": "Prefer vendor pages.", "permissions": []},
            {"name": "Web fetch", "instructions": "Fetch pages.", "permissions": ["network"]},
        ],
    }
    system = build_messages({}, [], "hi", agent_persona(entry))[0]["content"]
    assert system.startswith("You are Pricing researcher, the researcher agent")
    assert "quote list prices in EUR" in system and "Prefer vendor pages." in system
    assert "Fetch pages." not in system
    assert (
        "Approvals are the one thing made only in the office" in system
    )  # the office rules still apply

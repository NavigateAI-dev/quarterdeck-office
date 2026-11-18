"""Who a room message speaks to, and what a reply may offer."""

from app.room_replies import addressed, lead_of, split_proposal

AGENTS = [
    {"profile_id": "p1", "name": "Iris", "role": "researcher"},
    {"profile_id": "p2", "name": "Chief of Staff", "role": "chief"},
    {"profile_id": "p3", "name": "Vera", "role": "critic"},
    {"profile_id": "p4", "name": "Wren", "role": "summarist"},
]


def names(found: list[dict]) -> list[str]:
    return [entry["name"] for entry in found]


def test_an_agent_is_addressed_by_name_or_role() -> None:
    agents, unknown, archived = addressed("@Iris and @critic, what did you find?", AGENTS, [])
    assert names(agents) == ["Iris", "Vera"]
    assert unknown == [] and archived == []


def test_a_role_label_reaches_its_agent() -> None:
    agents, _, _ = addressed("@Writer draft it", AGENTS, [])
    assert names(agents) == ["Wren"]


def test_a_name_with_a_space_is_matched_whole() -> None:
    agents, _, _ = addressed("@Chief of Staff what next?", AGENTS, [])
    assert names(agents) == ["Chief of Staff"]


def test_the_order_is_the_order_they_were_named_and_once_each() -> None:
    agents, _, _ = addressed("@Vera then @Iris then @vera again", AGENTS, [])
    assert names(agents) == ["Vera", "Iris"]


def test_a_word_without_an_at_addresses_no_one() -> None:
    agents, unknown, _ = addressed("the critic said so, and iris agreed", AGENTS, [])
    assert agents == [] and unknown == []


def test_an_email_address_is_not_a_mention() -> None:
    agents, unknown, _ = addressed("write to iris@example.com", AGENTS, [])
    assert agents == [] and unknown == []


def test_an_unknown_name_is_reported_not_guessed() -> None:
    agents, unknown, _ = addressed("@Bob can you check?", AGENTS, [])
    assert agents == [] and unknown == ["Bob"]


def test_an_archived_agent_is_named_as_archived() -> None:
    agents, unknown, archived = addressed("@Otto are you there?", AGENTS, ["Otto"])
    assert agents == [] and unknown == [] and archived == ["Otto"]


def test_the_lead_is_the_chief() -> None:
    assert lead_of(AGENTS)["name"] == "Chief of Staff"


def test_without_a_chief_the_planner_leads_then_anyone() -> None:
    planner = [{"profile_id": "x", "name": "Pia", "role": "planner"}, AGENTS[0]]
    assert lead_of(planner)["name"] == "Pia"
    assert lead_of([AGENTS[0]])["name"] == "Iris"
    assert lead_of([]) is None


def test_a_proposal_line_becomes_an_offer_and_leaves_the_text() -> None:
    text, objective = split_proposal(
        "I could not read DNB.\nPROPOSE: Read DNB's Q&A on simplified CDD."
    )
    assert text == "I could not read DNB."
    assert objective == "Read DNB's Q&A on simplified CDD."


def test_without_a_proposal_line_the_text_is_unchanged() -> None:
    assert split_proposal("All four are answered.") == ("All four are answered.", None)


def test_a_proposal_mentioned_mid_text_is_not_an_offer() -> None:
    text = "If you want, I can PROPOSE: something, but it is not needed."
    assert split_proposal(text) == (text, None)


def test_an_empty_proposal_is_not_an_offer() -> None:
    assert split_proposal("Done.\nPROPOSE:   ") == ("Done.", None)


# Speaking to everyone: every active agent answers, the lead first.


def test_everyone_is_called_by_at_everyone_all_or_team() -> None:
    from app.room_replies import everyone_called

    for text in ("@everyone a joke please", "@all check in", "@team where are we?"):
        assert everyone_called(text), text


def test_everyone_is_called_in_plain_words_when_no_one_is_named() -> None:
    from app.room_replies import everyone_called

    for text in (
        "hey everyone, tell me a joke",
        "Everybody: one line each",
        "all of you, check in",
        "each of you say what you found",
    ):
        assert everyone_called(text), text
    assert not everyone_called("@Iris what does everyone in the market think?")
    assert not everyone_called("what did the critic say?")


def test_everyone_answers_lead_first_then_in_order() -> None:
    from app.room_replies import everyone_in

    assert names(everyone_in(AGENTS)) == ["Chief of Staff", "Iris", "Vera", "Wren"]

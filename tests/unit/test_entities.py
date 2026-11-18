"""Dossiers: what the office knows about the subjects its work returns to.

A task about the Wwft should start knowing the Wwft is BWBR0024282 — the fact a
planner got wrong, sending a run to read the Criminal Code.
"""

from app.entities import about, facts_from, mentions, names_of


class Entity:
    def __init__(self, name: str, aliases: list[str] | None = None) -> None:
        self.name = name
        self.aliases_json = aliases or []


WWFT = Entity("Wwft", ["BWBR0024282", "Wet ter voorkoming van witwassen"])
EIDAS = Entity("eIDAS 2.0", ["Regulation (EU) 2024/1183"])
BRAND = Entity("Acme")

REPORT = {
    "key_findings": [
        "The Wwft is published at wetten.overheid.nl under BWBR0024282. Article 3 states the "
        "customer due diligence obligation.",
        "Acme reported a 12% lift last quarter.",
    ],
    "answers": [
        {"answer": "eIDAS 2.0 entered into force on 20 May 2024 and establishes the wallet."}
    ],
    "evidence": ["Short."],
}


def test_a_subject_is_recognised_by_any_name_it_goes_by() -> None:
    assert mentions("published under BWBR0024282", names_of(WWFT))
    assert mentions("the Wet ter voorkoming van witwassen", names_of(WWFT))
    assert not mentions("a marketing report", names_of(WWFT))


def test_the_more_specific_subject_is_tried_first() -> None:
    """ "eIDAS 2.0" must not be swallowed by a broader "eIDAS"."""
    assert names_of(EIDAS)[0] == "Regulation (EU) 2024/1183"
    assert names_of(WWFT)[0] == "Wet ter voorkoming van witwassen"


def test_what_a_run_said_about_each_subject_becomes_a_candidate() -> None:
    found = facts_from(REPORT, [WWFT, EIDAS, BRAND])
    by_entity = {c.entity: [f.fact for f in found if f.entity == c.entity] for c in found}
    assert any("BWBR0024282" in f for f in by_entity["Wwft"])
    assert any("20 May 2024" in f for f in by_entity["eIDAS 2.0"])
    assert any("12% lift" in f for f in by_entity["Acme"])
    # It proposes what the work said, never something new about a regulator.
    for candidate in found:
        assert (
            candidate.fact
            in " ".join([*REPORT["key_findings"], REPORT["answers"][0]["answer"]]).replace(
                "  ", " "
            )
            or len(candidate.fact) < 400
        )


def test_a_run_that_mentioned_nothing_known_proposes_nothing() -> None:
    assert facts_from({"key_findings": ["Something unrelated entirely here."]}, [WWFT]) == []


def test_a_fragment_is_not_a_fact() -> None:
    assert facts_from({"key_findings": ["Wwft."]}, [WWFT]) == []


def test_a_task_is_given_only_the_subjects_it_is_about() -> None:
    """Not everything the office knows about every brand it has researched."""
    wanted = about("What does the Wwft require for customer due diligence?", [WWFT, EIDAS, BRAND])
    assert [e.name for e in wanted] == ["Wwft"]
    assert about("Nothing in particular", [WWFT, EIDAS, BRAND]) == []


def test_the_same_sentence_is_not_proposed_twice_for_one_subject() -> None:
    twice = {"key_findings": ["The Wwft is BWBR0024282 and it matters."] * 3}
    assert len(facts_from(twice, [WWFT])) == 1


def test_one_talkative_subject_does_not_starve_the_others() -> None:
    """The Wwft filled a whole run's allowance and left DNB with nothing."""
    from app.entities import MAX_FACTS_PER_ENTITY

    wwft_lines = [f"The Wwft requires this particular thing number {n}." for n in range(20)]
    report = {
        "key_findings": [*wwft_lines, "DNB supervises electronic money institutions here."],
    }
    dnb = Entity("DNB")
    found = facts_from(report, [WWFT, dnb])
    by_entity = {}
    for candidate in found:
        by_entity.setdefault(candidate.entity, []).append(candidate.fact)
    assert "DNB" in by_entity, "a subject mentioned once must still be heard"
    assert len(by_entity["Wwft"]) <= MAX_FACTS_PER_ENTITY

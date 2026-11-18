"""Reading a long source at its sections rather than its first pages.

The KYC task failed because the office read 12,000 characters of a 164,941
character law: navigation, a table of contents and a preamble. These tests use
a document with that same shape.
"""

from app.research.passages import (
    Passage,
    assemble,
    outline,
    score_section,
    sections,
    select_passages,
    strip_furniture,
    terms_of,
)

NAVIGATION = (
    "Direct naar content\nMenu\nZoeken\nInloggen\nU bent nu hier: Wettenbank Primaire navigatie\n"
)
CONTENTS = (
    "Inhoudsopgave\nHoofdstuk 1\nArtikel 1\nHoofdstuk 2. Bepalingen betreffende "
    "cliëntenonderzoek\nArtikel 3\nArtikel 8\n"
)
PREAMBLE = "Wij Willem-Alexander, bij de gratie Gods, " + ("overwegende dat " * 400)
ARTICLE_1 = "Artikel 1\nIn deze wet wordt verstaan onder: " + ("begrip en definitie. " * 300)
ARTICLE_3 = (
    "Artikel 3\nToon relaties in LiDO\nMaak een permanente link\n"
    "1 Een instelling verricht ter voorkoming van witwassen cliëntenonderzoek. "
    "2 Het cliëntenonderzoek stelt de instelling in staat om de cliënt te "
    "identificeren en de uiteindelijk belanghebbende te achterhalen. " * 40
)
ARTICLE_8 = "Artikel 8\nEen instelling verricht verscherpt cliëntenonderzoek indien. " * 30
ARTICLE_44 = "Artikel 44\nDeze wet treedt in werking op een bij koninklijk besluit. " * 30
# Joined with newlines: a heading only counts when it starts a line, which is
# what text extracted from a real page gives.
LAW = "\n".join([NAVIGATION, CONTENTS, PREAMBLE, ARTICLE_1, ARTICLE_3, ARTICLE_8, ARTICLE_44])
QUESTION = "Wwft cliëntenonderzoek verplichting, verscherpt onderzoek, UBO, EMI"


def test_the_law_is_split_at_its_own_articles() -> None:
    headings = [heading for heading, _ in outline(LAW)]
    assert "Artikel 3" in headings and "Artikel 8" in headings
    assert any(heading.startswith("Hoofdstuk 2") for heading in headings)


def test_navigation_is_dropped_but_content_is_not() -> None:
    cleaned = strip_furniture(LAW)
    assert "Direct naar content" not in cleaned
    assert "Toon relaties in LiDO" not in cleaned
    assert "cliëntenonderzoek" in cleaned


def test_the_obligation_is_reached_not_the_table_of_contents() -> None:
    """The failure this exists for: 12,000 characters of a 164,941 character law."""
    first_window = LAW[:12_000]
    assert "Een instelling verricht ter voorkoming" not in first_window, (
        "the fixture must reproduce the real shape: the obligation is past the cut"
    )
    selected = assemble(select_passages(LAW, QUESTION, budget=20_000))
    assert "Een instelling verricht ter voorkoming van witwassen cliëntenonderzoek" in selected
    assert "verscherpt cliëntenonderzoek" in selected


def test_a_long_article_is_not_outranked_for_being_long() -> None:
    """Article 3 was squeezed out by shorter articles that mention more terms.

    In a law the long article is usually the obligation itself.
    """
    wanted = terms_of(QUESTION)
    long_article = score_section("Artikel 3", ARTICLE_3, wanted)
    assert long_article > 0
    chosen = [passage.heading for passage in select_passages(LAW, QUESTION, budget=20_000)]
    assert "Artikel 3" in chosen


def test_the_selection_stays_in_the_document_order() -> None:
    chosen = [p.heading for p in select_passages(LAW, QUESTION, budget=20_000) if p.heading]
    assert chosen == sorted(chosen, key=lambda h: LAW.index(h)), chosen


def test_a_short_page_is_returned_whole() -> None:
    page = "A short page about cliëntenonderzoek with nothing to select."
    assert select_passages(page, QUESTION) == [Passage(heading="", text=page, score=1.0)]


def test_a_document_sharing_no_terms_still_returns_its_content() -> None:
    """Never worse than before: unmatched is not a reason to return navigation."""
    passages = select_passages(LAW, "entirely unrelated quantum chromodynamics", budget=8_000)
    assert passages, "a document with no shared terms must still be readable"
    body = assemble(passages)
    assert "Direct naar content" not in body
    assert len(body) > 1_000


def test_the_budget_is_respected() -> None:
    total = sum(p.size for p in select_passages(LAW, QUESTION, budget=9_000))
    assert total <= 9_000 + len(ARTICLE_3), "one oversized section may still be included whole"


def test_the_excerpt_says_it_is_a_selection() -> None:
    body = assemble(select_passages(LAW, QUESTION, budget=20_000))
    assert "Selected sections of a longer document" in body
    assert "[Artikel 3]" in body, "a passage names the section it came from"


def test_nothing_between_the_headings_is_lost() -> None:
    """A table of contents is matched as headings too, so the preamble sits
    inside a section rather than before the first one. It must still be kept."""
    found = sections(strip_furniture(LAW))
    assert any("Wij Willem-Alexander" in body for _, body in found)
    assert sum(len(body) for _, body in found) > len(strip_furniture(LAW)) * 0.9

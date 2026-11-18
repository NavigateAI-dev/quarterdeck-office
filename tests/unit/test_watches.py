"""Watching a page: what counts as a change, and what the model said."""

from app.watches import _verdict, changes, digest, normalized


def test_spacing_and_blank_lines_are_not_a_change() -> None:
    before = normalized("Price:   €59\n\n\nIn stock")
    after = normalized("  Price: €59  \nIn stock\n")
    assert digest(before) == digest(after)


def test_only_the_lines_that_changed_reach_the_model() -> None:
    before = normalized("Menu\nPrice: €59\nFooter")
    after = normalized("Menu\nPrice: €49\nFooter")
    said = changes(before, after)
    assert said == "-Price: €59\n+Price: €49"


def test_a_verdict_without_words_tells_nothing() -> None:
    assert _verdict('{"tell": true, "message": ""}')["tell"] is False
    assert _verdict("not json")["tell"] is False
    told = _verdict('Sure: {"tell": true, "message": "Now €49.", "done": true}')
    assert told == {"tell": True, "message": "Now €49.", "done": True}

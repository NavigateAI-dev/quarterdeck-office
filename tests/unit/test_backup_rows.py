"""R10: restored rows are compared by content, whatever order they come back in."""

from app.backup import rows_match


def test_the_same_rows_in_another_order_match() -> None:
    archived = ['{"id": ["n", 1], "name": ["n", "a"]}', '{"id": ["n", 2], "name": ["n", "b"]}']
    restored = list(reversed(archived))
    assert rows_match(archived, restored) == (True, "2 rows, content matches")


def test_one_changed_value_does_not_match() -> None:
    archived = ['{"id": ["n", 1], "name": ["n", "original"]}']
    restored = ['{"id": ["n", 1], "name": ["n", "changed"]}']
    matched, detail = rows_match(archived, restored)
    assert matched is False and "1 rows differ" in detail


def test_key_order_inside_a_row_does_not_matter() -> None:
    archived = ['{"a": ["n", 1], "b": ["n", {"y": 1, "x": 2}]}']
    restored = ['{"b": ["n", {"x": 2, "y": 1}], "a": ["n", 1]}']
    assert rows_match(archived, restored)[0] is True

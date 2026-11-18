from typing import Any

from app.sources.retrieval import Passage, chunk_text, lexical_candidates, select_passages


def passage(text: str, n: int = 0) -> Passage:
    return Passage(source="Research", path="notes.md", sha256="a" * 64, chunk=n, text=text)


def test_text_is_chunked_on_paragraph_boundaries_within_the_size() -> None:
    text = "\n\n".join(f"Paragraph {i} " + "word " * 50 for i in range(10))
    chunks = chunk_text(text, size=600)
    assert all(len(chunk) <= 600 for chunk in chunks)
    assert chunks[0].startswith("Paragraph 0")
    assert "".join(chunks).count("Paragraph") == 10


def test_an_oversized_paragraph_is_split_rather_than_dropped() -> None:
    chunks = chunk_text("x" * 1500, size=600)
    assert len(chunks) == 3
    assert sum(len(chunk) for chunk in chunks) == 1500


def test_lexical_candidates_prefer_overlapping_terms() -> None:
    items = [passage("The canteen menu"), passage("Q3 revenue grew in Europe", 1)]
    assert lexical_candidates("How did revenue change in Q3?", items, limit=1)[0].chunk == 1


def test_citations_are_local_addresses_with_the_chunk() -> None:
    assert passage("x", 3).citation == "local://docs/Research/notes.md#chunk-3"
    spaced = Passage(source="Market Intel", path="Q3 Brief.md", sha256="a" * 64, chunk=0, text="")
    assert spaced.citation == "local://docs/Market%20Intel/Q3%20Brief.md#chunk-0"


class Scorer:
    def __init__(self, scores: list[float], fail: bool = False) -> None:
        self.scores = scores
        self.fail = fail

    async def relevance(self, question: str, passages: list[str]) -> list[float]:
        if self.fail:
            from app.judgments import JudgmentUnavailable

            raise JudgmentUnavailable("typesafe_unavailable")
        return self.scores


async def test_judged_relevance_orders_and_filters_passages() -> None:
    items = [passage("a", 0), passage("b", 1), passage("c", 2)]
    chosen, method = await select_passages("q", items, Scorer([1.0, 3.0, 2.0]), limit=5)
    assert [p.chunk for p in chosen] == [1, 2]
    assert method == "typesafe"


async def test_without_a_judge_the_lexical_order_is_used() -> None:
    items = [passage("revenue q3", 0), passage("canteen", 1)]
    chosen, method = await select_passages("revenue", items, None, limit=1)
    assert [p.chunk for p in chosen] == [0]
    assert method == "lexical"


async def test_a_failed_judge_falls_back_to_lexical() -> None:
    chosen, method = await select_passages(
        "revenue", [passage("revenue", 0)], Scorer([], fail=True), limit=3
    )
    assert method == "lexical_fallback"
    assert len(chosen) == 1


def unused(_: Any) -> None:
    return None

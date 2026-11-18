"""B4.2: earlier links of a line of work, judged and within a budget."""

from uuid import uuid4

from app.lineage import LINE_CHARS, Candidate, judged_line


class Judge:
    """Scores 3 a passage mentioning any of `about`, else 0."""

    def __init__(self, *about: str, fail: bool = False) -> None:
        self.about = about
        self.fail = fail

    async def relevance(self, question: str, passages: list[str]) -> list[float]:
        from app.judgments import JudgmentUnavailable

        if self.fail:
            raise JudgmentUnavailable("typesafe_unavailable")
        return [3.0 if any(word in p for word in self.about) else 0.0 for p in passages]


def link(text: str, hops: int) -> Candidate:
    return Candidate(run_id=uuid4(), hops=hops, text=text)


async def test_only_what_bears_on_the_question_is_told_nearest_first() -> None:
    links = [link("far: licence fees", 4), link("near: licence types", 2), link("dogs", 3)]
    told = await judged_line(Judge("licence"), "Which licence?", links)
    assert [c.text for c in told] == ["near: licence types", "far: licence fees"]


async def test_the_budget_holds_however_long_the_line() -> None:
    links = [link(f"licence step {n} " + "x" * 600, n + 2) for n in range(10)]
    told = await judged_line(Judge("licence"), "Which licence?", links)
    assert told and sum(len(c.text) for c in told) <= LINE_CHARS
    assert [c.hops for c in told] == sorted(c.hops for c in told)


async def test_without_a_judgment_or_a_line_nothing_is_told() -> None:
    links = [link("licence types", 2)]
    assert await judged_line(None, "Which licence?", links) == []
    assert await judged_line(Judge("licence", fail=True), "Which licence?", links) == []
    assert await judged_line(Judge("licence"), "Which licence?", []) == []

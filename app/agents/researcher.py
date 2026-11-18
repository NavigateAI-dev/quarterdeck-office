import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from app.agents.base import AgentOutputError, BaseAgent, Preparation
from app.agents.tool_round import ToolEvidence, gather
from app.config import Settings
from app.context import ProjectContext
from app.events import InvocationRecorder
from app.gateway.direct import Completer
from app.judgments import JudgmentUnavailable
from app.mcp.calling import McpTools
from app.research.passages import assemble as assemble_passages
from app.research.passages import select_passages, terms_of
from app.schemas.agent_output import ResearchReport
from app.sources.retrieval import Passage
from app.tools.fetch import FetchError, Page, PageFetcher, same_site
from app.tools.web import (
    MAX_QUERY_LENGTH,
    UNKNOWN_SOURCE_TYPE,
    SearchError,
    SearchResult,
    SearchTool,
)

OFFICIAL_PAGES = 6
# Links followed from official pages: at most this many, judged relevant at least this much.
FOLLOWED_PAGES = 3
# Each hop follows up to FOLLOWED_PAGES; across both hops at most this many.
TOTAL_FOLLOWED = 6
LEXICAL_FOLLOWED = 2
LINK_CANDIDATES = 40
LINK_RELEVANCE = 0.6
WORD = re.compile(r"[a-z0-9]{3,}")
STOP_WORDS = frozenset({"the", "and", "does", "what", "which", "for", "with", "how", "are", "from"})
OFFICIAL_REDIRECTS = 3
OFFICIAL_EXCERPT = 12_000
# A primary source is kept whole and then read at its sections: the obligation
# in a law sits far past any fixed prefix of it.
OFFICIAL_WHOLE = 400_000
# Judging whether a named page is the document the plan meant.
MIN_TOPIC_TERMS = 4
MIN_TOPIC_LENGTH = 2_000
TITLE_CHARS = 120
# Calls to the owner's granted tools one invocation may make.
MCP_CALLS = 3

Retriever = Callable[[UUID, str], Awaitable[list[Passage]]]


def normalize_url(url: str) -> str:
    """Compare URLs without trailing-slash differences, and web URLs without
    fragments. A local:// citation keeps its fragment: it names the chunk."""
    url = url.strip()
    if url.startswith(("https://", "http://")):
        url = url.split("#", 1)[0]
    return url.rstrip("/")


class ResearcherAgent(BaseAgent[ResearchReport]):
    role = "researcher"
    output_schema = ResearchReport
    instructions = """You are the Researcher. Return JSON only matching the schema.
Cite only the supplied sources, copying each URL exactly.

Classify every source yourself. A source is primary when it is the subject's
own record: its official documentation, repository, release notes, licence, or
specification. It is secondary when it is commentary, comparison, or summary
written by someone else, including vendor blog posts about their own product.
Do not copy a classification from the source listing.

The owner's local documents, when supplied, are primary sources: cite each
passage by its local:// address exactly as given.

Prefer primary sources, report contradictory evidence, and never present an
unsupported assumption as a fact.

Answer the question you were asked. In answers, give one entry for that
question and no others — you are answering one question, not the whole plan —
with a status: answered when the retrieved evidence settles it, partial when it
settles some of it, unanswered when the evidence does not support an answer.
Say what the evidence shows, cite it, and in missing name what would be
needed. An unanswered question is an honest result; describing what a source
would probably say is not.

Do not repeat the question inside each finding: the question is already on its
answer.

When a source is listed as blocked, record it in blocked_sources with the
status it returned, and never describe what it contains."""

    def __init__(
        self,
        gateway: Completer,
        settings: Settings,
        search: SearchTool,
        recorder: InvocationRecorder | None = None,
        fetcher: PageFetcher | None = None,
        retriever: Retriever | None = None,
        link_judge: Any = None,
        mcp: McpTools | None = None,
    ) -> None:
        super().__init__(gateway, recorder, settings.structured_output_attempts)
        self.model_alias = settings.research_model_alias
        self.search = search
        self.limit = settings.search_max_results
        self.fetcher = fetcher
        self.max_pages = settings.fetch_max_pages
        self.last_evidence: dict[str, str] = {}
        # Provenance of the local passages supplied to the last invocation.
        self.last_local: dict[str, dict[str, Any]] = {}
        self.retriever = retriever
        # Judges which links on official pages are worth following.
        self.link_judge = link_judge
        # Set per run from the plan; fetched directly as candidate sources.
        self.official_urls: list[str] = []
        # Retrieval date of every source supplied to the last invocation.
        self.last_accessed: dict[str, str] = {}
        # Requested address -> retrieved address, for named pages that redirected.
        self.last_aliases: dict[str, str] = {}
        self.requested_by_final: dict[str, str] = {}
        # Sources that answered with a refusal, and what they answered.
        self.refused_sources: dict[str, str] = {}
        # Sources that answered, but with a document about something else.
        self.mismatched_sources: dict[str, str] = {}
        # Pages the office could not read itself and reached through a reader.
        self.read_through_reader: set[str] = set()
        # Pages read through their platform's own public data: first-hand,
        # and the answer can say what was read (a transcript, a repository).
        self.read_by_platform: dict[str, str] = {}
        # Opened in the office's own browser when nothing else could read them.
        self.read_in_browser: set[str] = set()
        # Official pages are shared by every parallel subtask of one run: the
        # copies made per subquestion share these objects, so a plan naming
        # seven sources costs seven requests rather than seven per subtask.
        # Asking the same host four times at once is how the office earned a
        # 429 from the law it had been told to read.
        self.official_cache: dict[str, Page] = {}
        self.official_failures: dict[str, str] = {}
        self.official_lock = asyncio.Lock()
        # Tools from servers the owner granted this project, set per run.
        self.mcp = mcp

    async def prepare(
        self, objective: str, context: ProjectContext, focus: str = ""
    ) -> Preparation:
        """Search for evidence, widening the search when a gap is named.

        A rerun that repeated the original query would return the same results
        and cite the same sources, so a revision could never improve the
        evidence. A named gap adds a second, targeted query.
        """
        question = f"{objective} {focus}".strip()
        official = await self._official_and_followed(question)
        passages: list[Passage] = []
        if self.retriever is not None:
            passages = await self.retriever(context.project_id, question)
        tooled = await self._tool_evidence(question, context)
        results = await self._searched(objective, focus, bool(passages or official or tooled))
        searched_on = datetime.now(UTC).date().isoformat()
        accessed = self._with_official(results, official, searched_on)
        if not results and not passages and not tooled:
            raise AgentOutputError(self.role, "no_search_results")
        section = self._sources_section(results, accessed, searched_on, passages, tooled)
        self.last_local = {normalize_url(p.citation): p.provenance() for p in passages}
        accessed.update({normalize_url(p.citation): searched_on for p in passages})
        accessed.update({address: searched_on for address, _, _ in tooled})
        self.last_accessed = accessed
        section += self._official_section(official, question)
        section += self._how_pages_were_read()
        section += await self._page_content(results)
        # Kept for the source-classification check that follows this invocation.
        # One run uses one agent instance at a time, on the single worker.
        self.last_evidence = {normalize_url(r.url): f"{r.title}. {r.snippet}" for r in results}
        self.last_evidence.update(
            {address: f"{label}. {text[:200]}" for address, label, text in tooled}
        )
        return Preparation(
            prompt_section=section,
            allowed_sources={
                **{alias: UNKNOWN_SOURCE_TYPE for alias in self.last_aliases},
                **{normalize_url(r.url): r.source_type for r in results},
                **{normalize_url(p.citation): "primary" for p in passages},
                **{address: UNKNOWN_SOURCE_TYPE for address, _, _ in tooled},
            },
            search_results=len(results) + len(passages) + len(tooled),
        )

    async def _official_and_followed(self, question: str) -> list[Any]:
        """The plan's own pages, and two hops of the links they list."""
        official = await self._official_pages(question)
        seen = {normalize_url(page.url) for page in official}
        followed = await self._followed_pages(question, official, seen, FOLLOWED_PAGES)
        # A second hop reaches pages listed only in an index the first hop found.
        followed += await self._followed_pages(
            question, followed, seen, min(FOLLOWED_PAGES, TOTAL_FOLLOWED - len(followed))
        )
        return list(official) + followed

    async def _searched(self, objective: str, focus: str, has_other: bool) -> list[SearchResult]:
        """The search, widened by a named gap; failing only when nothing else was found."""
        try:
            results = await self.search.search(objective, limit=self.limit)
            if focus.strip():
                extra = await self.search.search(
                    f"{objective} {focus}"[:MAX_QUERY_LENGTH], limit=self.limit
                )
                seen = {normalize_url(r.url) for r in results}
                results = results + [r for r in extra if normalize_url(r.url) not in seen]
        except SearchError:
            # Local documents alone are enough evidence to proceed; without
            # them a search failure is still the invocation's failure.
            if not has_other:
                raise
            results = []
        return results

    def _with_official(
        self, results: list[SearchResult], official: list[Any], searched_on: str
    ) -> dict[str, str]:
        """When each source was read; official pages join the results as sources."""
        accessed = {normalize_url(r.url): searched_on for r in results}
        known = set(accessed)
        for page in official:
            key = normalize_url(page.url)
            accessed[key] = page.retrieved_at.date().isoformat()
            requested = normalize_url(self.requested_by_final.get(key, page.url))
            if requested != key:
                # The plan's address redirected: citing it means citing this page.
                self.last_aliases[requested] = page.url
            if key not in known:
                results.append(
                    SearchResult(
                        title=f"Official page: {page.url}", url=page.url, snippet=page.text[:200]
                    )
                )
        return accessed

    @staticmethod
    def _sources_section(
        results: list[SearchResult],
        accessed: dict[str, str],
        searched_on: str,
        passages: list[Passage],
        tooled: list[ToolEvidence],
    ) -> str:
        """The sources the answer may cite: search results, local documents, tools."""
        listing = "\n".join(
            f"- {r.title} | {r.url} | {r.source_type} | retrieved "
            f"{accessed.get(normalize_url(r.url), searched_on)} | {r.snippet}"
            for r in results
        )
        section = f"\n\nSources (use only these):\n{listing}" if results else ""
        if passages:
            section += (
                "\n\nThe owner's local documents (primary sources; cite by the local:// "
                "address exactly):\n"
                + "\n\n".join(f"[{p.citation}] {p.path}\n{p.text}" for p in passages)
            )
        if tooled:
            section += (
                "\n\nResults from tools the owner granted this project (cite each by its "
                "mcp:// address exactly, and classify it yourself):\n"
                + "\n\n".join(f"[{address}] {label}\n{text}" for address, label, text in tooled)
            )
        return section

    @staticmethod
    def _official_section(official: list[Any], question: str) -> str:
        """The plan's own pages, read at the sections that bear on the question."""
        if not official:
            return ""
        # A long primary source is read at the sections that bear on the
        # question. Taking its first pages returns a table of contents.
        read = []
        for page in official:
            excerpt = assemble_passages(select_passages(page.text, question))
            read.append(f"[{page.url}] retrieved {page.retrieved_at.date().isoformat()}\n{excerpt}")
        return (
            "\n\nOfficial pages named in the plan, retrieved directly (quote these for "
            "prices and limits):\n" + "\n\n".join(read)
        )

    def _how_pages_were_read(self) -> str:
        """Pages read another way than directly, and sources that were not the document."""
        section = ""
        if self.read_through_reader:
            through = "\n".join(f"- {url}" for url in sorted(self.read_through_reader))
            section += (
                "\n\nThese pages refused the office and were read through a reader service "
                "instead. Cite them as normal, and say in the answer that they were read "
                f"through a reader rather than directly:\n{through}"
            )
        if self.read_in_browser:
            opened = "\n".join(f"- {url}" for url in sorted(self.read_in_browser))
            section += (
                "\n\nThese pages needed a browser to show their content and were opened in the "
                f"office's own browser. Cite them as normal:\n{opened}"
            )
        if self.read_by_platform:
            said = {
                "github": "GitHub's public data",
                "youtube": "the video's details and transcript",
                "bilibili": "Bilibili's public video details",
                "v2ex": "the V2EX topic and its replies",
                "feed": "the feed's entries",
            }
            platform = "\n".join(
                f"- {url}: {said.get(via, via)}"
                for url, via in sorted(self.read_by_platform.items())
            )
            section += (
                "\n\nThese were read from their platform's own public data, first-hand. "
                f"Cite them as you would the page:\n{platform}"
            )
        if self.mismatched_sources:
            listed = "\n".join(
                f"- {url}: the page retrieved is {title!r}"
                for url, title in self.mismatched_sources.items()
            )
            section += (
                "\n\nAddresses named in the plan that returned a document about something "
                "else. They are not evidence here. Record each in blocked_sources with the "
                "status not_the_document_named, and say the plan named the wrong "
                f"address:\n{listed}"
            )
        if self.refused_sources:
            # Named so the report can say a source refused us, rather than
            # recording what it would have told us as simply unknown.
            refusals = "\n".join(f"- {url}: {code}" for url, code in self.refused_sources.items())
            section += (
                "\n\nSources named in the plan that could not be retrieved. Report each as a "
                "blocked source with its status, and do not describe what they contain:\n"
                + refusals
            )
        return section

    async def _page_content(self, results: list[SearchResult]) -> str:
        """The pages' own words: what the search provider read, then pages fetched."""
        section = ""
        supplied = [r for r in results if r.text]
        if supplied:
            # The provider already read these. Fetching them again would cost
            # a request and reach a wall the provider got past.
            section += (
                "\n\nPage content returned by the search provider. Quote and cite this "
                "rather than the snippets:\n"
                + "\n\n".join(f"[{r.url}]\n{r.text}" for r in supplied)
            )
        unread = [r.url for r in results if not r.text]
        if self.fetcher is not None and unread:
            pages = await self.fetcher.fetch_many(unread[: self.max_pages])
            if pages:
                excerpts = "\n\n".join(f"[{p.url}]\n{p.text}" for p in pages)
                section += (
                    "\n\nPage content retrieved from those sources. Quote and cite this "
                    "rather than the snippets, and prefer a source that is the subject's "
                    "own documentation or repository:\n" + excerpts
                )
        return section

    async def _tool_evidence(self, question: str, context: ProjectContext) -> list[ToolEvidence]:
        """Call the granted tools the model judges useful for this question."""
        return await gather(
            self,
            self.mcp,
            question,
            context,
            purpose="before researching a question",
            limit=MCP_CALLS,
        )

    @staticmethod
    def _off_topic(pages: dict[str, Page], question: str) -> set[str]:
        """Named pages that are about something else — judged relatively.

        A plan names its sources by address, and a model can name a plausible
        one that is the wrong document: a run asked about customer due
        diligence and was handed the Dutch Criminal Code.

        But the question and the document need not share a language. A Chinese
        question about KYC yields the terms eidas, emi, kyc, and none of them
        appear anywhere in the Dutch law that answers it — so judging each page
        on its own rejected the very source the plan got right.

        So the test is relative: if no named page shares anything with the
        question, the terms cannot discriminate here and every page is kept.
        Only when some pages do match is a page that matches nothing refused.
        """
        wanted = terms_of(question)
        if len(wanted) < MIN_TOPIC_TERMS:
            return set()
        overlap = {
            url: len(wanted & terms_of(page.text))
            for url, page in pages.items()
            if len(page.text) >= MIN_TOPIC_LENGTH
        }
        if not overlap or max(overlap.values()) == 0:
            return set()
        return {url for url, shared in overlap.items() if shared == 0}

    async def _official_pages(self, question: str = "") -> list[Page]:
        self.last_aliases = {}
        self.requested_by_final = {}
        self.refused_sources = {}
        if self.fetcher is None:
            return []
        wanted = [u for u in self.official_urls if u.startswith("https://")][:OFFICIAL_PAGES]
        async with self.official_lock:
            fetcher = self.fetcher

            async def read(url: str) -> None:
                try:
                    self.official_cache[url] = await fetcher.fetch(
                        url, follow_redirects=OFFICIAL_REDIRECTS, max_chars=OFFICIAL_WHOLE
                    )
                except FetchError as refusal:
                    # A source that refused us is a fact about the research, not
                    # an absence. A regulator that returns 403 must be reported
                    # as blocked rather than quietly becoming an unknown.
                    self.official_failures[url] = refusal.code

            # Read together, not one after another.
            await asyncio.gather(
                *(
                    read(url)
                    for url in wanted
                    if url not in self.official_cache and url not in self.official_failures
                )
            )
        retrieved = {url: self.official_cache[url] for url in wanted if url in self.official_cache}
        off_topic = self._off_topic(retrieved, question) if question else set()
        pages = []
        for url in wanted:
            page = retrieved.get(url)
            if page is None:
                self.refused_sources[url] = self.official_failures.get(url, "fetch_failed")
                continue
            if page.read_via == "reader":
                self.read_through_reader.add(url)
            elif page.read_via == "browser":
                self.read_in_browser.add(url)
            elif page.read_via != "direct":
                self.read_by_platform[url] = page.read_via
            if url in off_topic:
                # Named in the plan, retrieved, and about something else.
                self.mismatched_sources[url] = page.text[:TITLE_CHARS].strip()
                continue
            self.requested_by_final[normalize_url(page.url)] = url
            pages.append(page)
        return pages

    async def _followed_pages(
        self, question: str, pages: list[Page], fetched: set[str], limit: int
    ) -> list[Page]:
        """Follow a few same-site links from official pages that bear on the question."""
        if self.fetcher is None or not pages or limit <= 0:
            return []
        candidates: list[tuple[str, str]] = []
        for page in pages:
            for text, url in page.links:
                key = normalize_url(url)
                if key in fetched or not same_site(page.url, url):
                    continue
                fetched.add(key)
                candidates.append((text, url))
        if not candidates:
            return []
        # Rank by words shared with the question before capping, so a relevant
        # link deep in a long index is still considered; ties keep page order.
        wanted = {w for w in WORD.findall(question.lower()) if w not in STOP_WORDS}

        def overlap(link: tuple[str, str]) -> int:
            text, url = link
            return len(wanted & set(WORD.findall(f"{text} {httpx.URL(url).path}".lower())))

        order = sorted(range(len(candidates)), key=lambda i: (-overlap(candidates[i]), i))
        candidates = [candidates[i] for i in order[:LINK_CANDIDATES]]
        chosen: list[str] = []
        if self.link_judge is not None:
            try:
                scores = await self.link_judge.link_relevance(
                    question, [{"text": t, "url": u} for t, u in candidates]
                )
                ranked = sorted(zip(scores, range(len(candidates)), candidates, strict=False))
                chosen = [
                    url for score, _, (_, url) in reversed(ranked) if score >= LINK_RELEVANCE
                ][:limit]
            except JudgmentUnavailable:
                chosen = []
        if not chosen:
            wanted = {w for w in WORD.findall(question.lower()) if w not in STOP_WORDS}
            overlaps = [
                # Anchor text and path only: the host names the subject on every link.
                (len(wanted & set(WORD.findall(f"{t} {httpx.URL(u).path}".lower()))), i, u)
                for i, (t, u) in enumerate(candidates)
            ]
            overlaps.sort(key=lambda item: (-item[0], item[1]))
            chosen = [url for overlap, _, url in overlaps if overlap > 0][
                : min(limit, LEXICAL_FOLLOWED)
            ]
        fetcher = self.fetcher

        async def read(url: str) -> Page | None:
            try:
                return await fetcher.fetch(
                    url, follow_redirects=OFFICIAL_REDIRECTS, max_chars=OFFICIAL_EXCERPT
                )
            except FetchError:
                return None

        # Read together, in the order chosen.
        followed = [page for page in await asyncio.gather(*map(read, chosen)) if page]
        for page in followed:
            fetched.add(normalize_url(page.url))
        return followed

    async def run(
        self, objective: str, context: ProjectContext, inputs: str = "", focus: str = ""
    ) -> ResearchReport:
        report = await super().run(objective, context, inputs, focus)
        # Access dates come from the application's own retrieval record.
        dated = []
        for source in report.sources:
            key = normalize_url(str(source.url))
            final = self.last_aliases.get(key)
            update: dict[str, Any] = {"accessed_on": self.last_accessed.get(key)}
            if final is not None:
                # Record the page actually retrieved, not the address that redirected.
                update["url"] = final
                update["accessed_on"] = self.last_accessed.get(normalize_url(final))
            dated.append(source.model_copy(update=update))
        return report.model_copy(update={"sources": dated})

    def validate_output(self, output: ResearchReport, preparation: Preparation) -> None:
        for source in output.sources:
            declared = preparation.allowed_sources.get(normalize_url(str(source.url)))
            if declared is None:
                raise AgentOutputError(self.role, "unverified_source")
            # The URL must come from the searched set. The classification is the
            # researcher's to make, unless the provider asserted one.
            if declared != UNKNOWN_SOURCE_TYPE and source.source_type != declared:
                raise AgentOutputError(self.role, "misreported_source_type")

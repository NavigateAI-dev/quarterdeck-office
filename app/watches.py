"""Watching pages and topics.

The owner names a page (an address) or a topic (search words), how often to
look, and, if they like, what to be told about ("when the price drops below
€50"). Each check reads it for free, through the office's own reader or its
search, and compares it with the last check. Only when it changed (or, with a
condition, on the first look) is one small call made on the lead's model, with
just the lines that changed, to decide whether to tell the owner and in what
words. Told is posted to the Slack channel the owner chose (the delivery
ledger, once per check) and shown on the floor's Watching widget.

The owner's press on Start is the consent: after it, checks run on their own,
within the watch's daily cap and the month's limit. Nothing is filled in,
signed in to or bought; a check only reads.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app import ideas
from app.browser.safety import web_address
from app.db.models import (
    ChannelApp,
    ChannelDelivery,
    ChannelDestination,
    ChannelInstallation,
    Watch,
    WatchCheck,
)
from app.office_settings import MonthlyLimitReached, check_monthly_limit, effective
from app.tools.fetch_client import ExchangeFetcher
from app.tools.web import StaticSearchTool, build_search_tool
from app.topic_briefs import lead

logger = logging.getLogger(__name__)

KINDS = ("page", "search")
# How often a watch looks, in minutes: hourly, every four hours, daily.
EVERY = (60, 240, 1440)
CAP_DEFAULT = 0.05
CAP_MAX = 1.0
KEPT_CHARS = 12_000
CHANGE_CHARS = 3_000
TOLD_CHARS = 600
# Watches looked at in one pass of the scheduler.
PER_PASS = 3
# A watch that fails this many checks in a row stops, and says why.
FAILURES_TO_STOP = 5
EVENT = "watch"

ASK = """You watch a web page or a search for the owner of a private office.
You are given what changed since the last look (or, on the first look, what is
there now) and, if the owner set one, what they want to be told about.
Tell the owner only when it matters to them: with a condition, only when it is
met; without one, only a meaningful change (not dates, counters, adverts or
layout). Write one or two plain sentences in the owner's language, naming what
changed and the number or fact that matters.
Set "done" true only when a condition was met and watching further is no use.
Return JSON only: {"tell": true|false, "message": "...", "done": true|false}"""


class WatchRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def normalized(text: str) -> list[str]:
    """The lines worth comparing: trimmed, blank lines gone, at most KEPT_CHARS."""
    lines = [" ".join(line.split()) for line in str(text).splitlines()]
    kept: list[str] = []
    total = 0
    for line in lines:
        if not line:
            continue
        total += len(line)
        if total > KEPT_CHARS:
            break
        kept.append(line)
    return kept


def digest(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def changes(before: list[str], after: list[str]) -> str:
    """The lines added and removed, as the model reads them, capped."""
    said = [
        line
        for line in difflib.unified_diff(before, after, lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    return "\n".join(said)[:CHANGE_CHARS]


def create(
    session: Session,
    *,
    title: str,
    kind: str,
    target: str,
    condition: str = "",
    every_minutes: int = 240,
    post_to_id: UUID | None = None,
    post_thread: str | None = None,
    ends_on: date | None = None,
    daily_cap_usd: float = CAP_DEFAULT,
    now: datetime,
) -> Watch:
    """A watch, checked first right away. The caller has the owner's press."""
    if kind not in KINDS:
        raise WatchRejected("unknown_kind")
    target = target.strip()
    if kind == "page":
        address = web_address(target)
        if address is None:
            raise WatchRejected("address_not_allowed")
        target = address
    elif not target:
        raise WatchRejected("empty_search")
    if every_minutes not in EVERY:
        raise WatchRejected("unknown_interval")
    if not 0 < daily_cap_usd <= CAP_MAX:
        raise WatchRejected("cap_out_of_range")
    if ends_on is not None and ends_on < ideas.owner_today(session, now):
        raise WatchRejected("in_the_past")
    if post_to_id is not None and session.get(ChannelDestination, post_to_id) is None:
        raise WatchRejected("channel_not_found")
    watch = Watch(
        title=(title.strip() or target)[:200],
        kind=kind,
        target=target[:2000],
        condition=condition.strip()[:1000],
        every_minutes=every_minutes,
        post_to_id=post_to_id,
        post_thread=(post_thread or None) and post_thread[:64],
        ends_on=ends_on,
        daily_cap_usd=daily_cap_usd,
        status="active",
        next_check_at=now,
    )
    session.add(watch)
    session.flush()
    return watch


def spent_today(session: Session, watch_id: UUID, now: datetime) -> float:
    zone = ideas.owner_zone(session)
    local = now.astimezone(zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    found = session.scalar(
        select(func.sum(WatchCheck.cost_usd)).where(
            WatchCheck.watch_id == watch_id, WatchCheck.at >= start
        )
    )
    return float(found or 0)


def due(session: Session, now: datetime) -> list[UUID]:
    """Active watches whose time has come; one past its last day ends here."""
    today = ideas.owner_today(session, now)
    found: list[UUID] = []
    for watch in session.scalars(
        select(Watch)
        .where(Watch.status == "active", Watch.next_check_at <= now)
        .order_by(Watch.next_check_at)
        .limit(PER_PASS * 4)
    ):
        if watch.ends_on is not None and watch.ends_on < today:
            watch.status, watch.stopped_reason = "ended", "its last day passed"
            continue
        found.append(watch.id)
    return found[:PER_PASS]


Read = Callable[[str], Awaitable[str]]
Search = Callable[[str], Awaitable[list[Any]]]
Complete = Callable[[list[dict[str, str]]], Awaitable[Any]]


async def look(kind: str, target: str, read: Read | None, search: Search | None) -> str:
    """What is there now: the page's text, or the search's results as lines."""
    if kind == "page":
        if read is None:
            raise WatchRejected("reader_not_running")
        return await read(target)
    if search is None:
        raise WatchRejected("search_not_configured")
    results = await search(target)
    return "\n".join(f"{r.title} — {r.url}\n{r.snippet}" for r in results)


def _verdict(text: str) -> dict[str, Any]:
    found = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(found.group(0)) if found else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    message = " ".join(str(data.get("message") or "").split())[:TOLD_CHARS]
    return {
        "tell": bool(data.get("tell")) and bool(message),
        "message": message,
        "done": bool(data.get("done")),
    }


async def check(
    session_factory: sessionmaker[Session],
    watch_id: UUID,
    now: datetime,
    read: Read | None,
    search: Search | None,
    complete: Complete | None,
    month_left: Callable[[Session], bool] = lambda session: True,
) -> str:
    """One check of one watch; returns what happened (kept on the watch)."""
    with session_factory() as session:
        watch = session.get(Watch, watch_id)
        if watch is None or watch.status != "active":
            return "gone"
        kind, target, condition = watch.kind, watch.target, watch.condition or ""
        before = list(watch.last_lines_json or [])
        known = watch.last_digest
        cap, title = watch.daily_cap_usd, watch.title
        every = timedelta(minutes=watch.every_minutes)
    try:
        lines = normalized(await look(kind, target, read, search))
    except Exception as error:  # noqa: BLE001 - one failed look is recorded, not raised
        code = getattr(error, "code", None) or type(error).__name__
        return _record(session_factory, watch_id, now, every, "failed", error=str(code)[:64])
    if not lines:
        return _record(session_factory, watch_id, now, every, "failed", error="empty_page")
    now_digest = digest(lines)
    first = known is None
    if not first and now_digest == known:
        return _record(session_factory, watch_id, now, every, "same")
    if first and not condition:
        # The first look is the baseline: nothing to compare yet, and free.
        return _record(session_factory, watch_id, now, every, "baseline", lines=lines)
    with session_factory() as session:
        # Never past the cap: a call is expected to cost what the last one did.
        last = session.scalar(
            select(WatchCheck.cost_usd)
            .where(WatchCheck.watch_id == watch_id, WatchCheck.cost_usd.is_not(None))
            .order_by(WatchCheck.at.desc())
            .limit(1)
        )
        expected = float(last or 0)
        capped = spent_today(session, watch_id, now) + expected > cap or not month_left(session)
    if capped or complete is None:
        # Not compared now; the change stays to be looked at next time.
        return _record(session_factory, watch_id, now, every, "capped")
    told_what = (f"The owner wants to be told: {condition}\n\n" if condition else "") + (
        "What is there now:\n" + "\n".join(lines)[:CHANGE_CHARS]
        if first
        else "What changed (- removed, + added):\n" + changes(before, lines)
    )
    messages = [
        {"role": "system", "content": ASK},
        {"role": "user", "content": f"Watching: {title} ({target})\n\n{told_what}"},
    ]
    completion = await complete(messages)
    usage = completion.usage
    verdict = _verdict(str(completion.content or ""))
    return _record(
        session_factory,
        watch_id,
        now,
        every,
        "told" if verdict["tell"] else "changed",
        lines=lines,
        said=verdict["message"] if verdict["tell"] else None,
        cost=completion.estimated_cost,
        tokens=int((usage.prompt_tokens or 0) + (usage.completion_tokens or 0)),
        done=verdict["done"] and bool(condition),
    )


def _record(
    session_factory: sessionmaker[Session],
    watch_id: UUID,
    now: datetime,
    every: timedelta,
    outcome: str,
    *,
    lines: list[str] | None = None,
    said: str | None = None,
    error: str | None = None,
    cost: float | None = None,
    tokens: int | None = None,
    done: bool = False,
) -> str:
    with session_factory() as session, session.begin():
        watch = session.get(Watch, watch_id)
        if watch is None:
            return "gone"
        row = WatchCheck(
            watch_id=watch_id,
            at=now,
            outcome=outcome,
            said=said,
            error=error,
            cost_usd=cost,
            tokens=tokens,
        )
        session.add(row)
        session.flush()
        watch.last_checked_at = now
        watch.next_check_at = now + every
        if lines is not None:
            watch.last_lines_json = lines
            watch.last_digest = digest(lines)
        if outcome in ("changed", "told"):
            watch.last_changed_at = now
        if outcome == "failed":
            watch.failures = (watch.failures or 0) + 1
            watch.last_error = error
            if watch.failures >= FAILURES_TO_STOP:
                watch.status = "stopped"
                watch.stopped_reason = (
                    f"{FAILURES_TO_STOP} checks in a row could not read it ({error})"
                )
        else:
            watch.failures = 0
            watch.last_error = None
        if said:
            watch.last_said, watch.last_said_at = said, now
            _post(session, watch, row.id, said)
        if done:
            watch.status, watch.stopped_reason = "ended", "what you asked about happened"
    return outcome


def _post(session: Session, watch: Watch, check_id: int, said: str) -> None:
    """To the channel the owner chose, once per check, by the delivery ledger."""
    if watch.post_to_id is None:
        return
    found = session.execute(
        select(ChannelDestination, ChannelApp.id)
        .join(ChannelInstallation, ChannelInstallation.id == ChannelDestination.installation_id)
        .join(ChannelApp, ChannelApp.id == ChannelInstallation.app_id)
        .where(ChannelDestination.id == watch.post_to_id, ChannelInstallation.status == "ACTIVE")
    ).first()
    if found is None:
        return
    destination, app_id = found
    where = f" ({watch.target})" if watch.kind == "page" else f" (search: {watch.target})"
    session.execute(
        insert(ChannelDelivery)
        .values(
            destination_id=destination.id,
            run_id=None,
            event_kind=EVENT,
            idempotency_key=f"watch:{watch.id}:{check_id}",
            text=f"👀 {watch.title}: {said}{where}"[:3000],
            thread_ref=watch.post_thread,
            status="QUEUED",
            app_id=app_id,
        )
        .on_conflict_do_nothing(index_elements=[ChannelDelivery.idempotency_key])
    )


def view(session: Session, watch: Watch) -> dict[str, Any]:
    destination = session.get(ChannelDestination, watch.post_to_id) if watch.post_to_id else None
    return {
        "id": str(watch.id),
        "title": watch.title,
        "kind": watch.kind,
        "target": watch.target,
        "condition": watch.condition or "",
        "every_minutes": watch.every_minutes,
        "status": watch.status,
        "stopped_reason": watch.stopped_reason,
        "ends_on": watch.ends_on.isoformat() if watch.ends_on else None,
        "daily_cap_usd": watch.daily_cap_usd,
        "post_to": {"id": str(destination.id), "name": destination.name} if destination else None,
        "last_checked_at": watch.last_checked_at.isoformat() if watch.last_checked_at else None,
        "next_check_at": watch.next_check_at.isoformat() if watch.next_check_at else None,
        "last_changed_at": watch.last_changed_at.isoformat() if watch.last_changed_at else None,
        "last_said": watch.last_said,
        "last_said_at": watch.last_said_at.isoformat() if watch.last_said_at else None,
        "last_error": watch.last_error,
        "spent_today_usd": round(spent_today(session, watch.id, datetime.now(UTC)), 4),
    }


def listed(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(Watch)
        .where(Watch.status != "deleted")
        .order_by((Watch.status == "active").desc(), Watch.created_at.desc())
    )
    return [view(session, row) for row in rows]


def widget_context(session: Session, now: datetime, purpose: str) -> str:
    """What agents read: what the owner watches and the latest said."""
    rows = list(session.scalars(select(Watch).where(Watch.status == "active").limit(10)))
    if not rows:
        return ""
    lines = [
        f"- {w.title}"
        + (
            f": {w.last_said} ({w.last_said_at:%a %d %b %H:%M})"
            if w.last_said and w.last_said_at
            else ""
        )
        for w in rows
    ]
    return "What the owner is watching (the office checks it on its own):\n" + "\n".join(lines)


def tools(
    settings: Any,
    session_factory: sessionmaker[Session],
    agent_complete: Callable[[dict[str, Any], list[dict[str, str]]], Awaitable[Any]] | None,
) -> tuple[Read | None, Search | None, Complete | None]:
    """Read through the office's own fetcher (its browser when browsing is on),
    search with the search the office already uses, think on the lead's model.
    No new outside service."""

    read: Read | None = None
    if settings.fetch_exchange is not None:
        with session_factory() as session:
            browsing = bool(effective(session, settings)["browser_on"])
        fetcher = ExchangeFetcher(
            settings.fetch_exchange,
            browser_exchange=settings.browser_exchange if browsing else None,
        )

        async def read_page(address: str) -> str:
            fetcher.cache.clear()  # Every check reads it fresh.
            page = await fetcher.fetch(address)
            return str(page.text)

        read = read_page
    tool = build_search_tool(settings)
    search: Search | None = None
    if not isinstance(tool, StaticSearchTool):

        async def search_words(words: str) -> list[Any]:
            return list(await tool.search(words, limit=8))

        search = search_words
    complete: Complete | None = None
    if agent_complete is not None:

        async def on_lead(messages: list[dict[str, str]]) -> Any:

            with session_factory() as session:
                agent = lead(session)
            if agent is None:
                raise WatchRejected("no_lead")
            return await agent_complete(agent, messages)

        complete = on_lead
    return read, search, complete


def month_left_for(settings: Any) -> Callable[[Session], bool]:

    def left(session: Session) -> bool:
        try:
            check_monthly_limit(session, settings)
        except MonthlyLimitReached:
            return False
        return True

    return left


async def watch_pass(
    session_factory: sessionmaker[Session],
    settings: Any,
    agent_complete: Callable[[dict[str, Any], list[dict[str, str]]], Awaitable[Any]] | None,
    now: datetime | None = None,
) -> int:
    """The watches whose time has come, a few at a time (the scheduler's tick)."""
    now = now or datetime.now(UTC)
    with session_factory() as session, session.begin():
        ids = due(session, now)
    if not ids:
        return 0
    read, search, complete = tools(settings, session_factory, agent_complete)
    done = 0
    for watch_id in ids:
        try:
            await check(
                session_factory, watch_id, now, read, search, complete, month_left_for(settings)
            )
            done += 1
        except Exception:
            logger.exception('{"event":"watch_check_failed","watch":"%s"}', watch_id)
    return done

"""Reading web pages, in a process that holds nothing worth stealing.

The office's researcher reads pages chosen from search results and model
output. That request must not be made by the process that holds the database
and the secret store, so it is made here: a service with network access, no
credentials, no database, and no volume but the exchange it is asked through.

Two boundaries apply to every address: it must resolve to a public one, and,
when the owner has set an allow-list, it must be on it.
"""

import asyncio
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.tools.fetch import MAX_EXCERPT_CHARS, READER_BASE, FetchError, PageFetcher

EXCHANGE_JOBS = "jobs"
HEARTBEAT = "fetcher.heartbeat"
HEARTBEAT_INTERVAL = 5.0
REQUEST_TIMEOUT = 30.0


def allowed_hosts() -> list[str]:
    raw = os.environ.get("FETCH_ALLOW_HOSTS", "")
    return [part.strip().lower().lstrip("*.") for part in raw.split(",") if part.strip()]


def host_permitted(host: str, allow_list: list[str]) -> bool:
    """With no list every public host is permitted; with one, only its hosts."""
    if not allow_list:
        return True
    host = host.lower()
    return any(host == item or host.endswith(f".{item}") for item in allow_list)


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload))
    os.replace(temporary, path)


async def handle(
    job: dict[str, Any], fetcher: PageFetcher, allow_list: list[str]
) -> dict[str, Any]:
    if job.get("probe"):
        # What the office can reach, checked from where it reads.
        from app.tools.platforms import probe_all

        rows = await probe_all(fetcher.client, allow_list, fetcher.reader_base, fetcher.video_info)
        return {"reach": rows}
    url = str(job.get("url", ""))
    from httpx import URL

    host = URL(url).host if url else ""
    if not host_permitted(host, allow_list):
        return {"error": "host_not_allowed"}
    try:
        page = await fetcher.fetch(
            url,
            follow_redirects=int(job.get("follow_redirects", 0)),
            max_chars=int(job.get("max_chars", MAX_EXCERPT_CHARS)),
        )
    except FetchError as error:
        return {"error": error.code}
    except Exception:  # noqa: BLE001 - a page must never stop the service
        return {"error": "fetch_failed"}
    payload = asdict(page)
    payload["retrieved_at"] = page.retrieved_at.isoformat()
    payload["links"] = [list(link) for link in page.links]
    return {"page": payload}


# Pages read at once: a research step asks for several together.
CONCURRENT_JOBS = 6


def claim(exchange: Path) -> Path | None:
    """The oldest waiting job, taken so no other reader takes it too."""
    jobs = exchange / EXCHANGE_JOBS
    try:
        pending = sorted(jobs.glob("*.job.json"), key=lambda path: path.stat().st_mtime)
    except FileNotFoundError:
        return None
    for waiting in pending:
        claimed = waiting.with_suffix(".running")
        try:
            os.replace(waiting, claimed)
        except FileNotFoundError:
            continue
        return claimed
    return None


async def serve_once(exchange: Path, fetcher: PageFetcher) -> bool:
    claimed = claim(exchange)
    if claimed is None:
        return False
    await process(exchange, fetcher, claimed)
    return True


async def process(exchange: Path, fetcher: PageFetcher, claimed: Path) -> None:
    jobs = exchange / EXCHANGE_JOBS
    try:
        job = json.loads(claimed.read_text())
        job_id = str(job["id"])
        if not job_id.isalnum():
            raise ValueError("job_id")
    except (OSError, ValueError, KeyError, TypeError):
        claimed.unlink(missing_ok=True)
        return
    payload = await handle(job, fetcher, allowed_hosts())
    _write_atomic(jobs / f"{job_id}.result.json", payload)
    claimed.unlink(missing_ok=True)


async def serve_forever(exchange: Path, poll_seconds: float = 0.2) -> None:
    (exchange / EXCHANGE_JOBS).mkdir(parents=True, exist_ok=True)
    # Set empty to turn the reader fallback off; unset keeps the default reader.
    reader = os.environ.get("READER_BASE_URL", READER_BASE).strip() or None
    async with PageFetcher(timeout=REQUEST_TIMEOUT, reader_base=reader) as fetcher:
        running: set[asyncio.Task[None]] = set()
        while True:
            (exchange / HEARTBEAT).write_text(str(time.time()))
            # Several pages at once, each in its own task; a slow page
            # no longer holds up the ones behind it.
            took = False
            while len(running) < CONCURRENT_JOBS:
                claimed = claim(exchange)
                if claimed is None:
                    break
                took = True
                task = asyncio.create_task(process(exchange, fetcher, claimed))
                running.add(task)
                task.add_done_callback(running.discard)
            if not took:
                await asyncio.sleep(poll_seconds)


def main() -> None:
    asyncio.run(serve_forever(Path(os.environ.get("FETCH_EXCHANGE", "/fetch-exchange"))))


if __name__ == "__main__":
    main()

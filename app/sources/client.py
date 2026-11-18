"""API side of the sources exchange: ask the reader service to index a root."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from app.sources.reader import EXCHANGE_JOBS, HEARTBEAT

# A large export can take minutes to walk.
ARCHIVE_SECONDS = 1800.0
# How long a live list, search or read may take before the reader is called away.
LIVE_SECONDS = 30.0
# A post's pictures: up to 9, a minute each at most.
PICTURES_SECONDS = 600.0


class SourceError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Indexer(Protocol):
    async def index(self, root: str) -> dict[str, Any]: ...

    async def folders(self) -> list[str]: ...

    async def index_staged(self, directory: str) -> dict[str, Any]: ...

    async def files(self, root: str) -> dict[str, Any]: ...

    async def search(self, root: str, query: str) -> dict[str, Any]: ...

    async def read(self, root: str, path: str, offset: int = 0) -> dict[str, Any]: ...

    async def parse_archive(self, name: str) -> dict[str, Any]: ...

    async def ocr_images(self, name: str) -> dict[str, Any]: ...


class ExchangeIndexer:
    def __init__(self, exchange: Path, timeout_seconds: float = 120.0, poll: float = 0.2) -> None:
        self.exchange = exchange
        self.timeout_seconds = timeout_seconds
        self.poll = poll

    async def index(self, root: str) -> dict[str, Any]:
        return await self._request({"kind": "index", "root": root})

    async def folders(self) -> list[str]:
        return [str(item) for item in (await self._request({"kind": "list"}))["folders"]]

    async def index_staged(self, directory: str) -> dict[str, Any]:
        return await self._request({"kind": "index_staged", "root": directory})

    # A space's folder read live: quick asks, so a shorter wait.
    async def files(self, root: str) -> dict[str, Any]:
        return await self._request({"kind": "files", "root": root}, LIVE_SECONDS)

    async def search(self, root: str, query: str) -> dict[str, Any]:
        return await self._request({"kind": "search", "root": root, "query": query}, LIVE_SECONDS)

    async def read(self, root: str, path: str, offset: int = 0) -> dict[str, Any]:
        return await self._request(
            {"kind": "read", "root": root, "path": path, "offset": offset}, LIVE_SECONDS
        )

    async def parse_archive(self, name: str) -> dict[str, Any]:
        return await self._request({"kind": "archive", "name": name}, ARCHIVE_SECONDS)

    async def ocr_images(self, name: str) -> dict[str, Any]:
        return await self._request({"kind": "ocr_images", "name": name}, PICTURES_SECONDS)

    async def _request(self, body: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        jobs = self.exchange / EXCHANGE_JOBS
        jobs.mkdir(parents=True, exist_ok=True)
        job_id = uuid4().hex
        job = jobs / f"{job_id}.job.json"
        temporary = jobs / f".{job_id}.tmp"
        temporary.write_text(json.dumps({"id": job_id, **body}))
        os.replace(temporary, job)
        result = jobs / f"{job_id}.result.json"
        deadline = asyncio.get_running_loop().time() + (timeout or self.timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            if result.exists():
                payload: dict[str, Any] = json.loads(result.read_text())
                result.unlink(missing_ok=True)
                return payload
            await asyncio.sleep(self.poll)
        job.unlink(missing_ok=True)  # Withdraw so a late reader does not run it.
        raise SourceError("reader_unavailable")


def reader_heartbeat_age(exchange: Path) -> float | None:
    import time

    try:
        return max(time.time() - float((exchange / HEARTBEAT).read_text()), 0.0)
    except (OSError, ValueError):
        return None

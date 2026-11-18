"""Browser tests run against a throwaway office, never one in use.

The tests click, create, rename and archive as they go. Run against the
owner's office they left test agents, discussions, business lines and
projects in it, so by default each run starts its own office in its own
PostgreSQL schema (TEST_DATABASE_URL), restored from the office's newest
backup so there is real work to read, and drops it afterwards.

To aim them at a running office instead, set OFFICE_URL and
OFFICE_TESTS_MAY_WRITE=yes: the second says, on purpose, that they will
write into it. Never point them at an office in use.
"""

import os
import sys
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from driver import Page, browser, find_browser  # noqa: E402
from throwaway import database_ready, newest_backup, running_office  # noqa: E402
from throwaway import sign_in_url as throwaway_sign_in  # noqa: E402

# Every test in this folder drives a browser.
pytestmark = pytest.mark.browser


def owner_token() -> str | None:
    token = os.environ.get("OWNER_API_TOKEN")
    if token:
        return token
    env = Path(".env")
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        if line.startswith("OWNER_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def chosen_office() -> str | None:
    """A running office the tests were deliberately aimed at, or None."""
    url = os.environ.get("OFFICE_URL")
    if url and os.environ.get("OFFICE_TESTS_MAY_WRITE") == "yes":
        return url.rstrip("/")
    return None


@pytest.fixture(scope="session")
def office_under_test() -> Iterator[dict[str, Any]]:
    if find_browser() is None:
        pytest.skip("no browser installed")
    aimed = chosen_office()
    if aimed is not None:
        token = owner_token()
        if not token:
            pytest.skip("no owner token to sign in with")
        try:
            with urllib.request.urlopen(f"{aimed}/health", timeout=3):  # noqa: S310
                pass
        except (urllib.error.URLError, OSError):
            pytest.skip(f"no office answering at {aimed}")
        yield {"base": aimed, "token": token}
        return
    if os.environ.get("OFFICE_URL"):
        pytest.skip("OFFICE_URL is set without OFFICE_TESTS_MAY_WRITE=yes; these tests write")
    if not database_ready():
        pytest.skip("Set TEST_DATABASE_URL to run the browser tests on a throwaway office")
    with running_office(newest_backup()) as office:
        yield office


@pytest.fixture
async def page(office_under_test: dict[str, Any]) -> AsyncIterator[Page]:
    async with browser(throwaway_sign_in(office_under_test)) as open_page:
        await open_page.settle(2.0)
        # These read the task room's own answer card; the editor beside it is
        # tested on its own office (test_answer_editor.py), so it starts closed.
        await open_page.evaluate("localStorage.setItem('office.editor.open', '0')")
        yield open_page

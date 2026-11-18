"""K1: adding a skill from skills.sh or GitHub, pinned, instructions only."""

import httpx
import pytest

from app.skills import ecosystem
from app.skills.ecosystem import EcosystemError, parse_source

SHA = "a" * 40
SKILL = """---
name: to-questionnaire
description: Turn something the owner cannot answer alone into a questionnaire.
---
# To questionnaire

Ask who it goes to. Write it to to-questionnaire-<slug>.md in the current directory.
Run `npx prettier --write` on the file. See REFERENCE.md for the structure.
Search the web for the recipient's organisation.
"""


@pytest.fixture(autouse=True)
def resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin DNS: these exercise the address checks, not the network."""
    import socket

    def resolved(host: str, port: int, *args: object, **kwargs: object) -> list:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolved)


class GitHub:
    """A repository as the GitHub API and raw host present it."""

    def __init__(self, files: dict[str, str], audit_html: str | None = None) -> None:
        self.files = files
        self.audit_html = audit_html
        self.seen: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.seen.append(url)
        if url.endswith("/commits/HEAD"):
            return httpx.Response(200, json={"sha": SHA})
        if "/git/trees/" in url:
            tree = [{"path": path, "type": "blob"} for path in self.files]
            return httpx.Response(200, json={"sha": SHA, "tree": tree, "truncated": False})
        prefix = f"https://raw.githubusercontent.com/acme/skills/{SHA}/"
        if url.startswith(prefix):
            path = url[len(prefix) :]
            if path in self.files:
                return httpx.Response(200, text=self.files[path])
            return httpx.Response(404)
        if url.startswith("https://www.skills.sh/"):
            if self.audit_html is None:
                return httpx.Response(404)
            return httpx.Response(200, text=self.audit_html)
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


AUDITS = (
    "<div><h3>Security Audits</h3><div><span>Gen Agent Trust Hub</span><span>Pass</span></div>"
    "<div><span>Socket</span><span>Pass</span></div><div><span>Snyk</span><span>Warn</span></div></div>"
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("acme/skills", ("acme", "skills", None)),
        ("acme/skills to-questionnaire", ("acme", "skills", "to-questionnaire")),
        (
            "npx skills add https://github.com/acme/skills --skill to-questionnaire",
            ("acme", "skills", "to-questionnaire"),
        ),
        (
            "https://www.skills.sh/acme/skills/to-questionnaire",
            ("acme", "skills", "to-questionnaire"),
        ),
        ("https://skills.sh/acme/skills", ("acme", "skills", None)),
        (
            "https://github.com/acme/skills/tree/main/skills/to-questionnaire",
            ("acme", "skills", "to-questionnaire"),
        ),
    ],
)
def test_every_way_of_naming_a_skill_resolves(text: str, expected: tuple) -> None:
    assert parse_source(text) == expected


@pytest.mark.parametrize("text", ["", "not a source", "https://example.com/acme/skills", "acme"])
def test_a_source_the_office_cannot_read_is_refused(text: str) -> None:
    with pytest.raises(EcosystemError):
        parse_source(text)


async def test_a_skill_is_found_pinned_and_read_as_instructions_only() -> None:
    repo = GitHub(
        {
            "skills/to-questionnaire/SKILL.md": SKILL,
            "skills/to-questionnaire/REFERENCE.md": "# Structure\nOne question per line.",
            "skills/to-questionnaire/scripts/fill.py": "print('x')",
            "skills/other/SKILL.md": "---\nname: other\ndescription: d\n---\nbody",
        },
        AUDITS,
    )
    found = await ecosystem.fetch("acme", "skills", "to-questionnaire", [], client=repo.client())
    assert found.name == "to-questionnaire"
    assert found.commit == SHA and found.path == "skills/to-questionnaire/SKILL.md"
    assert "Ask who it goes to." in found.instructions
    assert "One question per line." in found.instructions, "reference notes are included"
    assert found.refused == ["scripts/fill.py"]
    assert found.audits == {"gen": "Pass", "socket": "Pass", "snyk": "Warn"}
    kinds = {item["kind"] for item in found.assumptions}
    assert {"files", "shell"} <= kinds
    assert found.permissions == ["web_search"]
    assert found.suggested_role in ("summarist", "strategist")
    assert all(SHA in url for url in repo.seen if "raw.githubusercontent" in url), "pinned"


async def test_a_repository_with_several_skills_asks_which() -> None:
    repo = GitHub(
        {
            "skills/a/SKILL.md": "---\nname: a\ndescription: d\n---\nbody",
            "skills/b/SKILL.md": "---\nname: b\ndescription: d\n---\nbody",
        }
    )
    with pytest.raises(EcosystemError) as asked:
        await ecosystem.fetch("acme", "skills", None, [], client=repo.client())
    assert asked.value.code == "choose_skill" and asked.value.choices == ["a", "b"]


async def test_a_single_skill_at_the_root_needs_no_name() -> None:
    repo = GitHub({"SKILL.md": SKILL})
    found = await ecosystem.fetch("acme", "skills", None, [], client=repo.client())
    assert found.path == "SKILL.md" and found.audits == {}


async def test_a_named_skill_the_repository_lacks_is_not_found() -> None:
    repo = GitHub({"skills/a/SKILL.md": "---\nname: a\ndescription: d\n---\nbody"})
    with pytest.raises(EcosystemError) as missing:
        await ecosystem.fetch("acme", "skills", "zzz", [], client=repo.client())
    assert missing.value.code == "skill_not_found"


async def test_a_pinned_commit_is_read_as_it_was() -> None:
    repo = GitHub({"SKILL.md": SKILL})
    found = await ecosystem.fetch("acme", "skills", None, [], commit=SHA, client=repo.client())
    assert found.commit == SHA
    assert not any(url.endswith("/commits/HEAD") for url in repo.seen)


async def test_instructions_are_kept_within_the_limit() -> None:
    long = SKILL + ("x" * 100 + "\n") * 400
    repo = GitHub({"SKILL.md": long, "REFERENCE.md": "y" * 30_000})
    found = await ecosystem.fetch("acme", "skills", None, [], client=repo.client())
    assert len(found.instructions) <= ecosystem.MAX_INSTRUCTIONS
    assert "REFERENCE.md" in found.omitted


async def test_a_host_outside_the_allow_list_is_refused() -> None:
    repo = GitHub({"SKILL.md": SKILL})
    with pytest.raises(EcosystemError) as refused:
        await ecosystem.fetch("acme", "skills", None, ["example.org"], client=repo.client())
    assert refused.value.code == "host_not_allowed"


def test_the_manifest_carries_its_source_and_is_unreviewed_until_installed() -> None:
    found = ecosystem.Found(
        owner="acme",
        repo="skills",
        path="skills/q/SKILL.md",
        commit=SHA,
        name="to-questionnaire",
        description="Turn a gap into a questionnaire.",
        instructions="Ask who it goes to.",
        refused=["scripts/fill.py"],
        omitted=[],
        assumptions=[{"kind": "files", "text": "writes files"}],
        audits={"gen": "Pass"},
        permissions=[],
        suggested_role="summarist",
    )
    manifest = ecosystem.manifest(found, role="summarist", version="1.0.0")
    assert manifest["skill_id"] == "to-questionnaire"
    assert manifest["source"]["commit"] == SHA and manifest["source"]["repo"] == "acme/skills"
    assert manifest["roles"] == ["summarist"]
    assert manifest["publisher"] == "acme"


@pytest.mark.parametrize(
    "name, description, role",
    [
        (
            "seo-audit",
            "When the user wants to audit, review, or diagnose SEO issues.",
            "researcher",
        ),
        ("verification-before-completion", "Evidence before claims.", "critic"),
        ("to-questionnaire", "Turn a decision into a questionnaire.", "summarist"),
        ("copywriting", "Write marketing copy.", "summarist"),
    ],
)
def test_the_role_is_suggested_from_the_name_first(name: str, description: str, role: str) -> None:
    """Live finding: seo-audit was suggested for the critic because its
    description says 'review'."""
    from app.skills.ecosystem import _role

    assert (_role(name) or _role(description) or "summarist") == role


# Is there anything newer than what was installed?

NEWER = "b" * 40


class History:
    """The GitHub API's commit history and comparison for one skill folder."""

    def __init__(self, latest: str, status: str) -> None:
        self.latest = latest
        self.status = status
        self.seen: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.seen.append(url)
        if "/commits?" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": self.latest,
                        "commit": {
                            "message": "Tighten the questions\n\nLonger body.",
                            "committer": {"date": "2026-09-28T10:00:00Z"},
                        },
                    }
                ],
            )
        if "/compare/" in url:
            return httpx.Response(200, json={"status": self.status})
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


async def test_a_change_to_the_skills_folder_after_the_install_is_an_update() -> None:
    history = History(NEWER, "ahead")
    change = await ecosystem.latest_change(
        "acme", "skills", "skills/to-questionnaire", SHA, [], client=history.client()
    )
    assert change == {
        "commit": NEWER,
        "date": "2026-09-28T10:00:00Z",
        "message": "Tighten the questions",
        "newer": True,
    }
    assert any(
        "path=skills%2Fto-questionnaire" in url or "path=skills/to-questionnaire" in url
        for url in history.seen
    ), "only the skill's own folder counts"
    assert any(f"/compare/{SHA}...{NEWER}" in url for url in history.seen)


async def test_a_folder_unchanged_since_the_install_is_not_an_update() -> None:
    """The installed commit is the repository's head at the time; the folder's
    newest commit is usually older. Older is not an update."""
    for status in ("behind", "identical"):
        change = await ecosystem.latest_change(
            "acme",
            "skills",
            "skills/to-questionnaire",
            SHA,
            [],
            client=History("c" * 40, status).client(),
        )
        assert change["newer"] is False


async def test_the_installed_commit_itself_is_not_an_update() -> None:
    history = History(SHA, "identical")
    change = await ecosystem.latest_change(
        "acme", "skills", "skills/to-questionnaire", SHA, [], client=history.client()
    )
    assert change["newer"] is False
    assert not any("/compare/" in url for url in history.seen), "no comparison needed"

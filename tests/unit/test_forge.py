import httpx
import pytest

from app.tools.forge import (
    ForgeError,
    GitLabForge,
    authenticated_remote,
    redact,
)

TOKEN = "glpat-fixture-token-value"


def forge(handler: object, **kwargs: object) -> GitLabForge:
    defaults: dict[str, object] = {
        "base_url": "https://gitlab.example.com",
        "project_path": "group/sub/project",
        "token": TOKEN,
    }
    return GitLabForge(**(defaults | kwargs), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def merge_request(iid: int = 7) -> dict[str, object]:
    return {"iid": iid, "web_url": f"https://gitlab.example.com/g/p/-/merge_requests/{iid}"}


@pytest.mark.asyncio
async def test_a_merge_request_is_created_for_the_branch() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        # raw_path is what goes on the wire; .path shows it decoded.
        seen["path"] = request.url.raw_path.decode()
        seen["token"] = request.headers.get("PRIVATE-TOKEN", "")
        seen["body"] = httpx.Request("POST", "http://x", content=request.content).content.decode()
        return httpx.Response(201, json=merge_request())

    async with forge(handler) as tool:
        result = await tool.publish("quarterdeck/attempt-1", "Title", "Body", "main")
    assert result.created
    assert result.identifier == "7"
    # The project path is URL-encoded, so nested groups address correctly.
    assert seen["path"] == "/api/v4/projects/group%2Fsub%2Fproject/merge_requests"
    assert seen["token"] == TOKEN
    assert "quarterdeck/attempt-1" in str(seen["body"])


@pytest.mark.asyncio
async def test_an_existing_merge_request_is_reused() -> None:
    """A lost response must not produce a second merge request."""
    creations = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal creations
        if request.method == "GET":
            return httpx.Response(200, json=[merge_request(11)])
        creations += 1
        return httpx.Response(201, json=merge_request(12))

    async with forge(handler) as tool:
        first = await tool.publish("branch", "T", "B", "main")
        second = await tool.publish("branch", "T", "B", "main")
    assert creations == 0
    assert first.url == second.url
    assert not first.created


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(401), "forge_unauthorized"),
        (httpx.Response(403), "forge_unauthorized"),
        (httpx.Response(404), "forge_project_not_found"),
        (httpx.Response(500), "forge_rejected"),
        (httpx.Response(200, text="not json"), "malformed_forge_response"),
    ],
)
async def test_failures_are_sanitized(response: httpx.Response, code: str) -> None:
    async with forge(lambda request: response) as tool:
        with pytest.raises(ForgeError, match=code):
            await tool.publish("branch", "T", "B", "main")


@pytest.mark.asyncio
async def test_errors_never_carry_the_response_body_or_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"internal detail {TOKEN}")

    async with forge(handler) as tool:
        with pytest.raises(ForgeError) as caught:
            await tool.publish("branch", "T", "B", "main")
    assert TOKEN not in str(caught.value)
    assert "internal detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_a_timeout_is_reported_without_connection_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private endpoint", request=request)

    async with forge(handler) as tool:
        with pytest.raises(ForgeError, match="forge_timeout"):
            await tool.publish("branch", "T", "B", "main")


def test_the_push_url_requires_https() -> None:
    with pytest.raises(ForgeError, match="insecure_forge_url"):
        authenticated_remote("http://gitlab.example.com", "group/project", TOKEN)


def test_the_push_url_carries_the_token_for_one_call_only() -> None:
    remote = authenticated_remote("https://gitlab.example.com", "group/project", TOKEN)
    assert remote == f"https://oauth2:{TOKEN}@gitlab.example.com/group/project.git"


def test_redaction_removes_the_token() -> None:
    assert TOKEN not in redact(f"fatal: auth failed for {TOKEN}", TOKEN)


@pytest.mark.asyncio
async def test_a_nested_group_project_addresses_one_path_segment() -> None:
    """A project inside groups must not decode into separate path segments."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json=[])

    async with forge(handler, project_path="work/team/sub/repo") as tool:
        await tool.find("branch")
    assert "projects/work%2Fteam%2Fsub%2Frepo/merge_requests" in seen[0]


def test_no_merge_capability_exists() -> None:
    """SPEC section 18 denies merging, so the client has no such method."""
    assert not any("merge" in name and "request" not in name for name in dir(GitLabForge))
    assert not hasattr(GitLabForge, "accept")

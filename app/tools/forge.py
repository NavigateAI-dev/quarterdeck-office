import asyncio
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ForgeError(Exception):
    """Sanitized publication failure. Never carries response bodies or tokens."""

    def __init__(self, code: str, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True)
class ChangeRequest:
    """A published, reviewable change. GitLab calls this a merge request."""

    url: str
    identifier: str
    branch: str
    created: bool


class Forge(Protocol):
    async def publish(
        self, branch: str, title: str, description: str, base_branch: str
    ) -> ChangeRequest: ...


class _MergeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    web_url: str = Field(min_length=1)
    iid: int


class GitLabForge:
    """Creates a merge request for an already-pushed branch.

    Merging is deliberately absent: SPEC section 18 denies it outright, so the
    capability does not exist on this client rather than being guarded.
    """

    def __init__(
        self,
        base_url: str,
        project_path: str,
        token: str,
        timeout_seconds: float = 30.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.project = quote(project_path, safe="")
        self.timeout = timeout_seconds
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def __aenter__(self) -> "GitLabForge":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.client.aclose()

    def _url(self, path: str) -> httpx.URL:
        """Build a URL that preserves the encoded project path.

        GitLab addresses a project by its path encoded as one segment, and a
        normal URL string decodes %2F back to a separator, which would address
        the wrong path for any project inside a group.
        """
        base = httpx.URL(str(self.client.base_url))
        return httpx.URL(base, raw_path=(base.raw_path.rstrip(b"/") + path.encode()))

    async def _call(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.client.request(method, self._url(path), **kwargs)  # type: ignore[arg-type]
        except (httpx.TimeoutException, TimeoutError):
            raise ForgeError("forge_timeout") from None
        except httpx.TransportError:
            raise ForgeError("forge_unreachable") from None
        if response.status_code in (401, 403):
            raise ForgeError("forge_unauthorized", response.status_code)
        if response.status_code == 404:
            raise ForgeError("forge_project_not_found", response.status_code)
        if response.is_error:
            raise ForgeError("forge_rejected", response.status_code)
        return response

    async def find(self, branch: str) -> ChangeRequest | None:
        """Look for an open merge request for this branch.

        A lost response must not produce a second merge request, so publication
        always looks before it creates.
        """
        response = await self._call(
            "GET",
            f"/api/v4/projects/{self.project}/merge_requests",
            params={"source_branch": branch, "state": "opened"},
        )
        try:
            payload = response.json()
        except ValueError:
            raise ForgeError("malformed_forge_response") from None
        if not isinstance(payload, list) or not payload:
            return None
        try:
            existing = _MergeRequest.model_validate(payload[0])
        except ValidationError:
            raise ForgeError("malformed_forge_response") from None
        return ChangeRequest(
            url=existing.web_url, identifier=str(existing.iid), branch=branch, created=False
        )

    async def publish(
        self, branch: str, title: str, description: str, base_branch: str
    ) -> ChangeRequest:
        existing = await self.find(branch)
        if existing is not None:
            return existing
        response = await self._call(
            "POST",
            f"/api/v4/projects/{self.project}/merge_requests",
            json={
                "source_branch": branch,
                "target_branch": base_branch,
                "title": title[:255],
                "description": description[:20000],
                "remove_source_branch": False,
                "squash": False,
            },
        )
        try:
            created = _MergeRequest.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ForgeError("malformed_forge_response") from None
        return ChangeRequest(
            url=created.web_url, identifier=str(created.iid), branch=branch, created=True
        )


def authenticated_remote(base_url: str, project_path: str, token: str) -> str:
    """Build a push URL carrying the token.

    The token is supplied per call and never written to a git config, a
    credential helper, or the repository, so it cannot outlive the push.
    """
    host = httpx.URL(base_url)
    if host.scheme != "https":
        raise ForgeError("insecure_forge_url")
    return f"https://oauth2:{token}@{host.host}/{project_path.strip('/')}.git"


def redact(text: str, token: str) -> str:
    """Remove the token from anything that might be stored or logged."""
    return text.replace(token, "***") if token else text

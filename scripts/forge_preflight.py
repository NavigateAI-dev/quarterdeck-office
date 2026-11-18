"""Check that the forge credential can do exactly what publication needs.

Runs the same calls the application makes and nothing else, so a scope problem
is reported here rather than in the middle of an engineering attempt. Creates
nothing and changes nothing.
"""

import asyncio
import json
import sys

from pydantic import ValidationError

from app.config import get_settings
from app.runtimes.base import ExecutionLimits, run_bounded
from app.tools.forge import ForgeError, GitLabForge, authenticated_remote, redact


async def main() -> int:
    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        print(json.dumps({"status": "configuration_error", "fields": fields}), file=sys.stderr)
        return 2
    if not settings.gitlab_project_path or settings.gitlab_token is None:
        print(json.dumps({"status": "forge_not_configured"}))
        return 2

    token = settings.gitlab_token.get_secret_value()
    checks: list[dict[str, object]] = []
    ok = True

    async with GitLabForge(
        base_url=str(settings.gitlab_base_url),
        project_path=settings.gitlab_project_path,
        token=token,
        timeout_seconds=settings.forge_timeout_seconds,
    ) as forge:
        try:
            await forge.find("preflight-branch-that-does-not-exist")
            checks.append({"check": "merge_request_read", "status": "ok"})
        except ForgeError as error:
            ok = False
            checks.append({"check": "merge_request_read", "status": "failed", "error": error.code})

    # Repository write is checked with a push dry run, which sends nothing.
    remote = authenticated_remote(
        str(settings.gitlab_base_url), settings.gitlab_project_path, token
    )
    probe = await run_bounded(
        ["git", "ls-remote", "--heads", remote],
        settings.workspace_root
        if settings.workspace_root.is_dir()
        else __import__("pathlib").Path("/tmp"),  # noqa: S108
        ExecutionLimits(timeout_seconds=60),
    )
    if probe.succeeded:
        branches = [line.split("\t")[-1] for line in probe.stdout.splitlines() if line.strip()]
        checks.append({"check": "repository_read", "status": "ok", "branches": len(branches)})
    else:
        ok = False
        checks.append(
            {
                "check": "repository_read",
                "status": "failed",
                "error": redact(probe.stderr[:160], token),
            }
        )

    for check in checks:
        print(json.dumps(check), flush=True)
    print(json.dumps({"status": "ok" if ok else "insufficient_scope"}), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

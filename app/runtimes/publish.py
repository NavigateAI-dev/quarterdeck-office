from dataclasses import dataclass
from pathlib import Path

from app.runtimes.base import ExecutionLimits, run_bounded
from app.tools.forge import ChangeRequest, Forge, ForgeError, authenticated_remote, redact

GIT = "git"

# The worker has no git identity and must not read one from user config.
COMMIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Quarterdeck",
    "GIT_AUTHOR_EMAIL": "quarterdeck@localhost",
    "GIT_COMMITTER_NAME": "Quarterdeck",
    "GIT_COMMITTER_EMAIL": "quarterdeck@localhost",
}


@dataclass(frozen=True)
class Publisher:
    """Pushes an engineering branch and opens a reviewable change request.

    The credential is used here and nowhere else. It is never placed in the
    engineering runtime's environment, written to git configuration, or
    included in an artifact, and it is redacted from any error surfaced.
    """

    forge: Forge
    base_url: str
    project_path: str
    token: str
    push_timeout_seconds: float = 120.0

    async def commit(self, repository: Path, message: str) -> bool:
        """Commit the approved changes so the branch carries them.

        The runtime edits the working tree and commits nothing, so an
        uncommitted change would publish an empty branch. Staging happens only
        after the diff has been verified against the approved paths.
        """
        limits = ExecutionLimits(environment=COMMIT_IDENTITY, timeout_seconds=60)
        staged = await run_bounded([GIT, "add", "--all"], repository, limits)
        if not staged.succeeded:
            raise ForgeError("stage_failed")
        committed = await run_bounded(
            [GIT, "commit", "--quiet", "--message", message[:2000]], repository, limits
        )
        if not committed.succeeded:
            raise ForgeError("commit_failed")
        return True

    async def push(self, repository: Path, branch: str) -> None:
        remote = authenticated_remote(self.base_url, self.project_path, self.token)
        result = await run_bounded(
            [GIT, "push", "--porcelain", remote, f"HEAD:refs/heads/{branch}"],
            repository,
            ExecutionLimits(timeout_seconds=self.push_timeout_seconds),
        )
        if not result.succeeded:
            raise ForgeError(f"push_failed:{redact(result.stderr[:200], self.token)}")

    async def publish(
        self, repository: Path, branch: str, title: str, description: str, base_branch: str
    ) -> ChangeRequest:
        await self.commit(repository, f"{title}\n\n{description}")
        await self.push(repository, branch)
        return await self.forge.publish(branch, title, description, base_branch)

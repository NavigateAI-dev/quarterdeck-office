"""The coding CLIs the office knows, by key and by the name the owner reads.

One list: the approval text, the task's steps, Settings and the coder on the
Mac all name a CLI from here. No dependencies, so the approval layer can read
it without pulling in anything else."""

AGENTS = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "opencode": "opencode",
    "dsh": "DeepSeek Harness",
}
DEFAULT = "claude_code"


def label_of(key: str | None) -> str:
    """The CLI's name; the default's for an unknown or empty key."""
    return AGENTS.get(key or "", AGENTS[DEFAULT])

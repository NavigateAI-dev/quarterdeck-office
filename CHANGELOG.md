# Changelog

What changed between versions of Quarterdeck. Dates are the day the version was
cut. Anything that needs a hand during an upgrade says so under **Upgrading**.

## 0.1.0 — 2026-11-18

The first public release, from Navigate AI. Quarterdeck was built and used
daily as a private office before this; this release starts a fresh history.

What it includes:

- **A lead agent and a team.** The lead answers and does small work at once,
  showing each tool it used. Bigger work is planned, researched, criticised
  and written by a team, and starts only when you press **Start**.
- **Your own model providers.** Anthropic and any OpenAI-compatible API
  (OpenAI, DeepSeek, Kimi, Qwen, GLM, MiniMax, OpenRouter, SiliconFlow and
  more), called directly. A model per role, a backup per provider, provider
  balances, and budgets per task, project and month.
- **Approvals.** Anything that would build, run, publish, spend or widen what
  the office may do waits for your press, bound to the exact proposal.
- **Coding on your Mac.** Quarterdeck Coder runs your own coding CLI (Claude
  Code, Codex, opencode or DeepSeek Harness) in its own sandbox, on an
  `office/` branch; the office checks the result before you merge.
- **Knowledge and folders.** Documents and folders you choose, read by a
  reader with no network, granted per project.
- **Where you work.** Discussions, a task board, recurring work, calendars,
  documents as Word, Excel, slides or web pages, tool servers (MCP), skills,
  Slack, and your phone over your home network or Tailscale.
- **Operations.** One-command launch, upgrade with backup and smoke test,
  restore drills, rollback, and AI history protection and export.

**Upgrading:** nothing; this is the first public version. Install with
`python3 -m scripts.launch` (see [docs/operations.md](docs/operations.md)).

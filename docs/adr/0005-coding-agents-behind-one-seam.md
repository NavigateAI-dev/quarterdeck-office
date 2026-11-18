# 0005. Coding agents behind one seam; their questions go to the owner

Date: 21 Oct 2026 · Status: Accepted · Decided by: the owner

## Context
Codex and Claude Code were wired separately, with near-identical loops and names hard-coded in
several places. DeepSeek Harness speaks the Agent Client Protocol (ACP), and ACP agents can stop
mid-task to ask permission.

## Decision
One coding-provider seam (`app/coding_providers.py`); the office keeps everything with authority:
the approval, the checks, landing and merging. An in-house ACP client drives any ACP agent. An
agent's permission question goes to the owner as buttons on the run; unanswered after ten
minutes it is refused. DeepSeek Harness is installed pinned at exactly 0.2.1-rc.2; upgrading it
is a separate, deliberate change.

## Consequences
A new coding agent is one entry in the registry. Agents never answer their own permission asks.
DeepSeek Harness reports no spend (see 0009).

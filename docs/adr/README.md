# Decision records

One file per decision that shapes the office: what was decided, why, and what follows from it.
Short on purpose. A record is never edited to say something else; a later decision that changes
it is a new record that says which one it replaces, and the old one is marked so.

| # | Decision | Date | Status |
|---|---|---|---|
| [0001](0001-lead-agent-answers-by-default.md) | The lead agent answers; the full team is one press away | 15 Oct 2026, amended 23 Oct | Accepted |
| [0002](0002-providers-called-directly.md) | Call model providers directly; retire the LiteLLM gateway | 22 Oct 2026 | Accepted |
| [0003](0003-event-log-is-the-record.md) | A run's event log is the record; one writer of its state | 19 Oct 2026 | Accepted |
| [0004](0004-workspaces-read-first-owner-merges.md) | The owner's folders: read first; work on office/ branches; the owner merges | 21 Oct 2026 | Accepted |
| [0005](0005-coding-agents-behind-one-seam.md) | Coding agents behind one seam; their questions go to the owner | 21 Oct 2026 | Accepted |
| [0006](0006-plugins-on-demand.md) | User-installed plugins only when a real one is named | 22 Oct 2026 | Accepted |
| [0007](0007-quality-judged-on-real-use.md) | Quality is judged on real use, by the owner, not a paid judge | 15 Oct 2026 | Accepted |
| [0008](0008-confirm-before-widening.md) | Confirm anything that widens what the office may do; no Apply/Cancel | 23 Oct 2026 | Accepted |
| [0009](0009-dsh-spend-waits-for-dsh.md) | DeepSeek Harness spend: wait for dsh to report it | 24 Oct 2026 | Accepted |
| [0010](0010-approval-by-digest.md) | An approval is bound to the exact proposal, by its digest | V1, recorded 24 Oct | Accepted |
| [0011](0011-untrusted-content-in-services-without-keys.md) | Untrusted content is handled by services that hold no keys | V1, recorded 24 Oct | Accepted |
| [0012](0012-tool-servers-over-http-only.md) | Tool servers over HTTP only; their claims are not trusted | V1, recorded 24 Oct | Accepted |

## Template

```markdown
# NNNN. Title in a sentence

Date: · Status: Accepted | Replaced by NNNN | Withdrawn · Decided by: the owner

## Context
What was true, and what forced a choice.

## Decision
What we do now, in a few lines.

## Consequences
What follows: what gets easier, what gets harder, what to watch.
```

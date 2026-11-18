# 0010. An approval is bound to the exact proposal, by its digest

Date: before the reshape (V1), recorded 24 Oct 2026 · Status: Accepted

## Context
An owner who approves "build this" approves something specific: a repository, a base commit, the
paths it may change, the checks that must pass, where the result lands, which agent writes it, and
the evidence the proposal rests on. If any of that could change after approval, the approval would
cover work the owner never saw.

## Decision
Everything an approval authorizes is one immutable `ApprovalScope` (`app/approvals.py`), and the
owner approves its digest. The scope is re-read from the durable record before execution; any
change (a new artifact version, a moved base commit, a different path) changes the digest and the
approval no longer applies. A new field is left out of the digest at its default, so approvals
waiting at the time stay valid.

## Consequences
Nothing outside the scope is authorized by approving it. Adding a field to the scope needs care:
give it a default and keep it out of the digest at that default.

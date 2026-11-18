# 0008. Confirm anything that widens what the office may do; no Apply/Cancel

Date: 23 Oct 2026 · Status: Accepted · Decided by: the owner

## Context
The Settings design asked for Apply/Cancel on every change and a confirm step for anything that
widens what the office may do. Every settings screen saves as it goes.

## Decision
A confirm step, in one place (`widen.js`, consulted before any write the page sends): turning on
the browser, raising a limit, letting a tool run unasked, turning a rule off, opening the office to
the network, adding a chat member and the like say what changes and wait for Confirm. Narrowing
never asks. Apply/Cancel per change is not adopted.

## Consequences
Risky changes are guarded without reworking every screen. A new widening write must be added to
`widen.js`, and browser tests that make one must press Confirm.

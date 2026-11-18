# 0004. The owner's folders: read first; work on office/ branches; the owner merges

Date: 21 Oct 2026 · Status: Accepted · Decided by: the owner

## Context
The office should work with the owner's own projects, which live under `~/dev`. A writable mount
would put the owner's repositories inside the networked container where coding agents run.

## Decision
One approved root, `~/dev`; folders inside it are given to a business space one by one. Read
access is on: read live, without network, cited. Work access is built and **off** until the owner
mounts a writable folder: approved coding work then runs in the office's own clone and lands only
as an `office/<work>` branch in the owner's repository, never the default branch or the checkout.
Merging is the owner's own act, fast-forward only. There is no "full access".

## Consequences
The owner's work is never changed without their merge. Turning work access on is the owner's word
(23 Oct: kept off). Pushing a branch would be its own approval and is not built.

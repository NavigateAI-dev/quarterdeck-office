# 0006. User-installed plugins only when a real one is named

Date: 22 Oct 2026 · Status: Accepted · Decided by: the owner

## Context
The plan included user-installed plugins: discovery, a manifest of permissions approved at
install, and an out-of-process runner for untrusted code. Asked which plugin they wanted, the
owner had none in mind.

## Decision
Not built until the owner names a concrete plugin. When they do, first check whether a tool server
(MCP), reviewed per tool in Settings, already serves it; build the plugin system only if not.

## Consequences
No framework without a user. The kernel's hook bus exists, but the run's hook points stay unwired
until this is taken up.

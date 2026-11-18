# 0012. Tool servers (MCP) over HTTP only; their claims are not trusted

Date: before the reshape (V1), recorded 24 Oct 2026 · Status: Accepted

## Context
MCP servers come in two transports: Streamable HTTP, and stdio, which launches the server as a
subprocess with the owner's permissions. Servers also describe their own tools, for example as
read-only.

## Decision
Streamable HTTP only; running third-party code in the office's process or user is refused, as it
is for plugins. A tool's own annotations are shown and never believed: whether a call runs, asks
first or is refused is the owner's decision per tool. A server's address is checked like any
fetched page: public HTTPS, resolved, and against the office's host allow-list.

## Consequences
A tool that only exists as a local stdio server cannot be used directly. Every tool call is
under the owner's per-tool decision, the office's rules and the side-effect ledger (0003).

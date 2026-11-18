# 0011. Untrusted content is handled by services that hold no keys

Date: before the reshape (V1), recorded 24 Oct 2026 · Status: Accepted

## Context
Web pages, the owner's documents, a build's code and its packages are untrusted: any of them can
try to make the office do something. The office itself holds the owner's keys and records.

## Decision
Work on untrusted content runs in its own service with none of the office's secrets: the `fetcher`
(pages), the `browser`, the `sources` reader (the owner's folders, mounted read-only, no network),
the `runner` (build checks: no network, read-only root) and the `installer` (packages, install
scripts off). They exchange work with the office through exchange folders, not the network.

## Consequences
A page or a file that tricks its reader reaches no key and no record. New work on untrusted
content goes in one of these services, or a new one like them, never in `api`.

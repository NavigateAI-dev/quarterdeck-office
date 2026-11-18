# 0002. Call model providers directly; retire the LiteLLM gateway

Date: 22 Oct 2026 · Status: Accepted · Decided by: the owner

## Context
Roles reached their models through a LiteLLM proxy. On 21 Oct a LiteLLM release was compromised
(a credential stealer in its packages), and a critical advisory covered the version the office
pinned. The live gateway was clean and was patched, but the proxy held every model key and was a
third party in every call.

## Decision
No gateway. Each role has a connection the owner chose (Settings → Models); the office calls the
provider directly, with that connection's key from the office's encrypted store, priced from a
cached price catalogue. On upgrade, each role's model and key from `.env` became a connection.
No other proxy was adopted: those assessed had their own advisories or routed prompts through a
third party.

## Consequences
One less service and no third party holding keys. A role with no connection says so (setup,
status, Settings) rather than falling back. Providers must be OpenAI- or Anthropic-compatible.
The price catalogue is still downloaded from LiteLLM's public price list: data, never executed.

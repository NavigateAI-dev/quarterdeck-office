# 0009. DeepSeek Harness spend: wait for dsh to report it

Date: 24 Oct 2026 · Status: Accepted · Decided by: the owner

## Context
DeepSeek Harness 0.2.1-rc.2 reports no billed tokens or cost over ACP: its usage update says only
how full its context is. Estimating from that would overstate (cached input costs far less);
reading the DeepSeek balance before and after would count other calls on the same key.

## Decision
Neither. A build by DeepSeek Harness says on its steps that its cost is not counted toward the
task's budget and shows on the DeepSeek account, until dsh reports usage.

## Consequences
A dsh build is bounded by its time limit, not by the budget. Revisit when a dsh release reports
usage; recording it then is small.

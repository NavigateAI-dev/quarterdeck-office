# 0003. A run's event log is the record; one writer of its state

Date: 19 Oct 2026 · Status: Accepted · Decided by: the plan (S4), approved by the owner

## Context
A run's truth was spread over LangGraph checkpoints, `runs.state_json` and `run_events`, written
from many places. The page polled. Side effects other than builds had no record of having been
attempted, so an interrupted call could be repeated.

## Decision
Every change to a run's state goes through `app/run_state.py` and is logged; an architecture
test refuses any other writer. Postgres notifies the office of new events, and the page follows
them over one stream instead of polling. A tool server call is written down before it is sent and
settled after; one interrupted is "outcome unknown", for the owner. Each model call is kept, for
replay in tests without a key.

## Consequences
A run can be explained and rebuilt from its log, and the page updates as things happen. New
state lives on the log, not in ad hoc columns. Replay of whole team runs is not done yet.

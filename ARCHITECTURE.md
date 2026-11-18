# Architecture

How Quarterdeck is built. The decisions that shaped it are in
[docs/adr/](docs/adr/). Where this file and the code disagree,
the code is right and this file is a bug.

## 1. What it is

A private office of AI colleagues for one owner, on the owner's own computer. The owner asks in
a discussion, on the Desk, or in Slack; a **lead agent** answers, and for larger work a **team**
(planner, researchers, strategist, critic, Chief) works through a plan in waves. Nothing that
costs money beyond its limit, reaches outside, or changes the owner's files happens without the
owner's word, and every run is durable: it survives a restart and says what it did.

## 2. Processes and data

Everything runs in Docker Compose (`docker-compose.yml`); `scripts.launch` starts it and
`scripts.upgrade` upgrades it (pause, back up, rebuild, migrate, adopt, smoke test, resume).

| Service | What it does | Network | Secrets |
|---|---|---|---|
| `api` | The office: HTTP API, the page, the run worker, the lead, Slack | yes | the encrypted store |
| `migrate` | `alembic upgrade head`, once, before `api` | db only | no |
| `postgres` | Every record: runs, events, messages, settings, usage | internal | — |
| `runner` | Build checks for coding work | **none** | none, read-only root |
| `installer` | A build's packages, when the owner allows | yes | none, install scripts off |
| `fetcher` | Reads web pages for the agents | yes | **none** |
| `browser` | The office's own Chromium, when browsing is on | its own | none |
| `sources` | Reads the owner's granted folders | **none** | none, folder mounted `:ro` |
| `remote` | Tailscale, idle until connected | tailnet | its own state |

Services that read untrusted content (`fetcher`, `browser`, `sources`, `runner`) hold no keys and
talk to `api` through exchange folders, not the network. Model keys live in the encrypted secret
store (`app/secret_store.py`, root key in `.secrets/root.key`), never in `.env` or the database,
and are read only for the call that needs them (`app/credentials.py`).

## 3. Code layout

`app/` is a Python 3.12 package, typed strictly (`mypy`), linted (`ruff`, `bandit`). The page is
plain JavaScript with no build step.

| Where | What |
|---|---|
| `app/kernel/` | Scopes, the event bus, registries, a hook bus, plugin loading. Imports nothing else from `app` (gate). Holds no feature logic. |
| `app/db/` | SQLAlchemy models (`db/models/`) and the session. `SCHEMA_REVISION` in `db/session.py` equals the migration head; `migrations/` holds the Alembic history. |
| `app/api/` | HTTP: `routers/` (one module per area), `schemas/` (request models), `web/` (the page: `index.html`, `office/*.js`, `*.css`). Only delivery code imports `app.api` (gate). |
| `app/runtime/` | The run worker (`worker.py`): a queue, places, claims, and preparing each run's roles (`model_stack.py`: routing, backups, downgrade). |
| `app/orchestrator/` | The team pipeline on LangGraph: `graph.py`, `pipeline.py`, `nodes/` (planning, work, critique, synthesis, approval, team), `dispatcher.py` (claim, run, persist, pause), `gate.py` + `approval_store.py` (approvals), `checkpoints.py`. |
| `app/lead/` | The lead agent: a native-tool loop (`loop.py`), its tools (`tools.py`: todo, delegate, review, propose a team run), citation checks (`checks.py`), its run (`run.py`) and chat (`chat.py`). |
| `app/agents/` | The team's roles on `BaseAgent` (`base.py`: schema in the prompt, local validation, corrective retry). |
| `app/gateway/` | The shared result and error types (`client.py`) and direct provider calls (`direct.py`: OpenAI-compatible and Anthropic). |
| `app/tools/` | `ToolSpec` and `ToolPipeline` (`pipeline.py`), search, page fetching, DeepSeek search/fetch. |
| `app/mcp/`, `app/policies/` | Tool servers (MCP) with per-tool decisions, metering and the side-effect ledger; the office's rules (deny or ask, never grant). |
| `app/channels/` | Slack: Socket Mode, inbound requests, the office's answers, deliveries. |
| `app/runtimes/` | Checking coding work: the attempt's own clone (`workspace.py`), keeping and landing it (`engineer.py`), the isolated runner (`isolated.py`) and the package installer (`installer.py`). No coding agent runs here. |
| `app/coder/` | The coder on the owner's Mac (Stage 36): jobs handed to Quarterdeck Coder (`jobs.py`, helper in `scripts/mac_coder.py`), the hand-off and the office's own checks (`handoff.py`), each CLI's output as steps (`parsers.py`), the reviewer (`review.py`). |
| `app/privacy/` | `detect` finds secrets and identity numbers by pattern and checksum; `outbound` masks them in `DirectCompleter`, the last step before a provider. |
| `app/archive/` | The owner's conversations from other AI tools, kept apart from what the office decided or learned: `adapters` parses exports inside the keyless `sources` reader, `service` stores and searches them, `memories` proposes what to keep from one conversation, checks each quote, and exports what the owner kept, `protection` holds the owner's choices and the record of who reached it, `exports` writes it out as a zip, `tools` gives agents `search_memory` and `get_conversation`. |
| `app/followups/` | What was due on a day: `service` gathers it from the calendar and planned ideas, says what to ask and what a recap reports; `tools` records the owner's answers from discussions and the Slack bot. Asked by the Progress check routine. |
| `app/routines/` | Recurring work: `service` keeps routines, `run` fires each as a message its agent answers in its discussion, `tools` lets agents propose them, `presets` holds ready-made ones as files. |
| `app/documents/`, `app/sources/`, `app/research/`, `app/memory/`, `app/skills/`, `app/calendars/`, `app/browser/` | Deliverables, the owner's folders, research helpers, lessons, skills, calendars, the browser agent. |
| `app/*.py` | One module per domain feature (ideas, topics, rooms, verdicts, workspaces, office models, settings…). A feature starts as one flat module and becomes a package only when it has several parts. |

Tests mirror it: `tests/unit` (no database, ~50 s), `tests/integration` (PostgreSQL via
`TEST_DATABASE_URL`, ~9 min; API tests in `test_api_<area>.py`, fixtures in `conftest.py`,
helpers in `api_support.py`), `tests/browser` (`-m browser`, a throwaway office per module,
~25 min), `tests/architecture` (the gates in §9).

## 4. How a request becomes an answer

**In a discussion.** `POST /topics/{id}/messages` → `api/routers/shared._say` stores the owner's
message → `handoff.Handoff.handle` (background) judges what it asks → `room_replies.RoomReplies`
answers: the lead runs on the agent's model (or the thread's own model, `topic_settings`), with
the room's tools, inside the agent's daily allowance; each call is kept against the message. A reply that needs
real work offers **Start**; pressing it → `handoff.start_work` → `run_start.start_run` with the
lead (the full team when the owner asks for it, or "Redo with full team").

**From the Desk or the task box.** `POST /runs` → `run_start.start_run` → the lead, unless the task
asks for a formatted deliverable, the browser, or the full team (`answering_workflow`).

**From Slack.** `channels/inbound.py` → `channels/inbound_office.SlackOffice` → `lead/chat.chat_reply`
(a message with a shared picture takes the older planning path, which can show it to the model).

**Running it.** `start_run` records the run and its context and queues a `Job`. The worker claims
it (a lease, renewed by a heartbeat; a recovery sweep reclaims abandoned runs), prepares its roles
(§6), and runs either `lead/run.execute_lead_run` or `orchestrator/dispatcher.execute_run` (the
team: plan → waves of steps → critique → synthesis → approval when it proposes something to do).
In the team, every step boundary is a checkpoint; a pause, a cancel, a budget ceiling, a rule asking
the owner, or a stuck browser step stops it there and records why, and it resumes from there. The
lead has no checkpoint: a resumed lead task starts its loop again, told its step list and finished
briefs.

**The lead's delegation.** `delegate` runs up to eight briefs at once, each its own loop with a
fresh context. A brief returns a `StepResult`, never its transcript: how it ended is set by the
office, and any address its own tools did not return is taken out before the lead reads it. A
pause, a cancel, a spent budget or an owner question stops every brief, and a note from the owner
reaches them all. The lead stops at 40 model calls and a brief at 20, with the best answer so far.

## 5. State: the record is the truth

- **Runs and their events.** `runs` holds a run's status and its live state; `run_events` is its
  log. `app/run_state.py` is the **only** writer of a run's state (gate), so every change is a
  logged `run_state_changes` row and the state can be rebuilt from the log.
- **Live.** Postgres triggers `pg_notify` on events, messages, controls and status;
  `app/live.py` listens once and fans out over `GET /stream` (Server-Sent Events) to the page.
- **Side effects.** A tool server (MCP) call is written to `side_effects` before it is sent and
  settled after; one interrupted mid-call is "outcome unknown", for the owner to settle, never
  silently repeated (`app/side_effects.py`, `app/mcp/calling.py`). Calendar changes keep their own
  record (`calendar_changes`); the Mac calendar helper does not yet report its own actions.
- **Model calls.** Each call a model answered is kept (`model_calls`, `model_payloads`) for replay
  in tests without an API key (`app/model_calls.py`).
- **Approvals.** A proposal is approved by its digest: the exact scope (repository, base commit,
  paths, checks, where it lands). Changing any of it changes the digest and voids the approval
  (`approvals.py`, `orchestrator/gate.py`).

## 6. Models

There is no gateway (retired 22 Oct, [ADR 0002](docs/adr/0002-providers-called-directly.md)).
Each role has a **connection** the owner chose (Settings → Models, `office_settings.role_models`;
a project or a thread may choose its own). `office_models.OfficeModels` answers a role's alias
with its connection; `connection_completer` resolves the key at call time; `gateway/direct.py`
calls the provider and prices the call from the cached price catalogue (`model_catalog.py`).

Each role's completer is wrapped, innermost first (`runtime/worker._prepare`):

```
provider call  →  backup on failure (failover.py)  →  kept for replay (model_calls.py)
→  recorded as usage (events.py)  →  the office's rules (policies/guard.py)  →  the budget (budget.py)
```

So a call that fails over is still recorded, checked and budgeted as the first one was; a rule's
refusal is reported as a rule, not as spent budget. The model router (`model_router.py`) may move
an easy task's dear roles to a cheaper tested model at the start (Settings → Model choice).

## 7. Tools

Every tool the agents call is a `ToolSpec` run through `tools/pipeline.ToolPipeline`: find →
check the arguments (path-qualified errors back to the model) → the owner's stance → the rules →
run within its timeout → spill long results (`lead/spill.py`, read the rest with `read_more`).
MCP tools are reviewed per tool by the owner (allowed, asks first, refused); a call that asks
first stops the run with a button, never a guess. A discussion is told which connected servers its project
may not use, and which projects may. Tool results are untrusted data: a tool or a
page can never grant a permission.

## 8. Safety, in one place

- Spend: per-task and monthly limits, read again while a task runs; a ceiling pauses and asks.
- Reach: a tool server call is a side effect on the record, asked of the owner where a rule or
  the owner's stance says so.
- Code: the office runs no coding agent. Quarterdeck Coder on the owner's Mac runs their own CLI in
  a worktree on an `office/` branch under `~/dev`; the office checks the branch's files itself (its
  own diff, the approved paths, the configured command in the isolated runner), keeps the patch,
  lands a build in its folder, and offers Merge for work in the owner's folder. Merging is the
  owner's press, done on the Mac, only into a clean checkout. There is no "full access".
- The page: anything that widens what the office may do waits for the owner's Confirm
  (`web/office/widen.js`, consulted by `api()` before it sends).
- Secrets: never printed, never in prompts, never given to an agent; the `fetcher`, `browser`,
  `sources` and `runner` services hold none.

## 9. The rules, and the gates that keep them

From DeepSeek Harness's design as this office keeps them:

| Rule | Here | Kept by |
|---|---|---|
| R1 small kernel, behaviour at named points | `app/kernel` holds no features; the hook bus exists, but the run's hook points are not wired yet (part of the deferred plugin stage, ADR 0006) | gate: kernel size and imports |
| R2 one-way dependencies | delivery code (`app/api`) on top; no module-level import cycles | gates: no cycles; no new local imports past the baseline (38); only delivery imports `app.api`; every module imports on its own |
| R3 one tool pipeline | `ToolPipeline` for tasks, discussions and Slack | review |
| R4 the log is the truth | `run_state.py` the one writer; events logged | gate: only `run_state` writes a run's state |
| R5 typed hand-offs | the lead's delegated work returns `StepResult` (summary, findings, sources, how it ended); role outputs are schema-validated | tests |
| R6 checkpoint before side effects | `side_effects` ledger, approval digests | tests |
| R7 rules as gates; replay without keys | `tests/architecture`; kept model calls | the gates themselves |

Size: no file over **1,000 lines** (gate). Functions aim at **150 lines** or fewer; the
exceptions are the route-registration functions, whose length is their nested routes.

## 10. Where new behaviour goes

| You are adding | Put it in | And |
|---|---|---|
| An HTTP endpoint | `app/api/routers/<area>.py`, its request model in `app/api/schemas/` | logic in a domain module, not the router |
| A screen or a control | `app/api/web/office/<feature>.js` (+ `index.html` script tag, styles in `shell.css`) | null-safe DOM access; widening writes listed in `widen.js` |
| A domain feature | `app/<feature>.py` | a migration if it stores something; bump `SCHEMA_REVISION` |
| A table or a column | `app/db/models/`, `migrations/versions/NNNN_*.py` | include it in project deletion if it hangs off a project |
| A tool for the agents | a `ToolSpec` (or an MCP server the owner reviews) | through `ToolPipeline`; a side effect goes on the ledger |
| A rule (deny or ask) | `app/policies/` | a rule can only deny or ask |
| A model provider | a preset in `app/providers.py` | OpenAI- or Anthropic-compatible through `gateway/direct.py` |
| A coding CLI | `AGENTS` in `app/coding_names.py`, its command in `scripts/mac_coder.py`, its output reader in `app/coder/parsers.py` | runs on the owner's Mac in its own sandbox, never in the office |
| A change to a run's state | `app/run_state.py` | nowhere else (gate) |
| A setting | `app/config.py` **and** `.env.example` **and** the compose `api` environment | the deployment tests check all three |
| A decision that shapes the above | `docs/adr/NNNN-<title>.md` | and the plan stage that carries it out |

## 11. Running and checking

```sh
.venv/bin/ruff format app tests && .venv/bin/ruff check app tests && .venv/bin/mypy app
.venv/bin/python -m pytest -q tests/unit tests/architecture
TEST_DATABASE_URL=… .venv/bin/python -m pytest -q tests/integration
TEST_DATABASE_URL=… .venv/bin/python -m pytest -q -m browser tests/browser
```

`tests/browser/seeded_office.py <port> [<backup archive>]` starts a throwaway office (its own
schema, no spending) to look at a change in a real browser.

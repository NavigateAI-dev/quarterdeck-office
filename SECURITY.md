# Security

This is a private office: one owner, one machine, no public surface. By default
it answers this computer only. You can open it to your phone in two ways, each
off until you turn it on: your home network, or your own Tailscale network.
Neither puts it on the public internet.

## Who can reach it

Checked against the code on 5 October 2026; sign-in limits and the gaps
below rechecked on 10 October.

**This computer (the default).** The office listens on `127.0.0.1:8000`
(`OFFICE_BIND`, `docker-compose.yml`); its database listens
on `127.0.0.1` only. Nothing else on the network can connect.

**Your home network (opt-in).** `python3 -m scripts.access home` publishes the
office on the Mac's own network (`OFFICE_BIND=0.0.0.0`); Settings then checks
the address answers as this office before trusting it. Know what that means:

- Traffic is plain HTTP. Anyone on the same Wi-Fi who can watch it can read
  the session cookie and act as you until it expires. Use it on a network you
  trust, or use Tailscale instead.
- Every device on the network can reach the sign-in page. Everything else
  needs a sign-in: the owner token, a one-time code from Settings (single
  use, valid two minutes), or a name and password if you set one. On plain
  HTTP that password crosses the Wi-Fi readable too; away from home, sign in
  over Tailscale.
- **Turn off** in Settings stops the office trusting the address; the port
  stays published until `python3 -m scripts.access home --off`.

**Your Tailscale network (opt-in).** The `remote` service runs Tailscale Serve
inside the office's own network namespace: HTTPS, reachable only from devices
signed in to your tailnet. It has no published port, no Docker socket and no
added privileges, and it never turns on Funnel (Tailscale's public route); a
route with Funnel allowed is reported as not ready. It is set up, turned off
and forgotten from Settings.

**In every mode:**

- Sessions are a cookie (HttpOnly, SameSite=Strict), stored only as a hash,
  ending after 12 hours idle or 7 days. "Keep me signed in on this device"
  makes it 30 days, idle or not. `python3 -m scripts.revoke_sessions` ends
  them all.
- A sign-in password (off until you set one in Settings → Mobile & remote
  access) is kept only as a salted scrypt hash. Five wrong tries from one
  address, or twenty from all together, stop password sign-in for 15 minutes,
  even with the right password. The owner key and the launcher's codes are
  counted apart, so you cannot be locked out. Changing or turning it off asks
  for the current password.
- A change made from the browser needs the office's own origin and a
  per-session token, so another site cannot act through your browser.
- The office answers only under its own names: this computer's, and the
  addresses you set up and it verified. That stops another website's name
  being pointed at it (DNS rebinding). It does not limit who can connect: a
  device on your network can claim to be `localhost`, and still has to sign in.
- Wrong owner keys and one-time codes, on the sign-in page and on the API,
  are counted per address and across all of them (five a minute from one
  address, fifty from all) and answered 429 once over the limit. Only wrong
  guesses are slowed: the right key or code always gets in, so a device on
  your network cannot keep you out (`app/api/sessions.py`, `LoginThrottle`).
- The session cookie is marked Secure when it is opened under the verified
  `https://` Tailscale address, decided from the office's own record of its
  addresses, never from a forwarded header.

The gaps the 5 October review found here (R13–R15 in `CODEBASE_REVIEW.md`)
are fixed, with one part left: the `remote` container drops every capability
but `DAC_OVERRIDE` and runs with `no-new-privileges`, but its root filesystem
is not read-only, because tailscaled writes outside its state volume.

## Reporting a problem

If you run this copy, you are the person to tell. If you received it from
someone else, mail them directly and do not open a public issue.

## What the office holds

Provider API keys, Slack tokens and any other secret are encrypted at rest under
a root key kept at `.secrets/root.key`, outside every data volume and outside
every backup. Secrets are entered in the office UI and are never printed in chat
or written to logs.

## If a key may have leaked

1. Revoke it at the provider first.
2. `python3 -m scripts.rotate_secret_key` to re-encrypt the store under a new
   root key.
3. `python3 -m scripts.revoke_sessions` if a browser session may also be exposed.
4. Enter the replacement keys in the office UI.

## What the office sends to TypeSafe

The office asks TypeSafe small typed questions while it works, when a
`TYPESAFE_API_KEY` is set: what a message is asking for, whether a critique
repeats itself, whether a claim is supported, whether a link is worth
following, which project a Slack question belongs to (with each project's
description and recent task titles), and which recent finished answers bear on
a new task. Those carry the text being judged.

Turning a finished task into project memory sends more, and only when you ask
for it. The boundary is deliberate and tested:

- no hook, no completion trigger, no schedule calls the evaluator;
- a dry run shows exactly what would be sent and sends nothing;
- a projection is one line per lesson with the trace fact behind it, capped, and
  anything shaped like a credential is replaced first;
- page bodies, artifacts and prompts are never part of it;
- a probability ranks a lesson and never approves it.

Unset `TYPESAFE_API_KEY` and none of this happens; the office falls back to its
own lexical checks.

## Boundaries that are load-bearing

- Only people you have added in the office can talk to its chat bots. Everyone
  else is answered nothing at all. A member may ask and may give work, on your
  keys and inside your budgets; approvals are never given from chat, by anyone.

- Provider keys are kept encrypted in the office's store and read only for the
  call that needs them; agents never see them. (The LiteLLM gateway that held
  them until 22 October 2026 was retired after a security advisory.)
- The isolated runner and the sources reader have no network and no secrets.
- Page fetching runs in a credential-free service behind a host allow-list, with
  loopback, private ranges and cloud metadata refused.
- Paid model runs and external writes require an explicit yes.
- Deployment to production infrastructure is rejected by the settings validator,
  not by a flag.

## Newer boundaries (29 September 2026)

Each of these was checked against the code on that date.

- **Tool-server sign-in.** Signing in to an MCP server with your account uses
  OAuth 2.1 with PKCE (`app/mcp/oauth.py`). The vendor returns your browser to
  `/mcp/oauth/callback`, which is public because that redirect does not carry
  the office's SameSite=Strict cookie. It is bound to you instead by its
  `state`: a random value that only an owner-authenticated **Sign in** request
  can create, redeemed once and refused after ten minutes. A sign-in in
  progress (its PKCE verifier and any client secret) is kept encrypted in the
  secret store under a hash of its state, never the state itself, so it
  survives a restart. A missing, reused or
  expired state, or an error from the vendor, stores nothing. Every metadata,
  registration and token address passes the same public-HTTPS and allow-list
  checks as the server itself. The tokens are stored encrypted in the secret
  store as the server's credential, refreshed there, and never returned by the
  API: a server's view says only whether it has a credential and whether it is
  signed in. They reach the server and nothing else, never a prompt, an event
  or a log.
- **Skills from outside.** A skill added from skills.sh or GitHub
  (`app/skills/ecosystem.py`) is read over public HTTPS within the allow-list,
  with no credential, and kept as instructions only. It is pinned to the commit
  you previewed, so what you reviewed is what runs. Scripts and anything
  executable are refused and listed, never installed. What the skill assumes
  the office cannot do is shown beside it. skills.sh's three audits (Gen Agent
  Trust Hub, Socket, Snyk) are shown as information and decide nothing:
  skills.sh itself says it cannot guarantee any skill. A skill the Chief
  attaches to a plan step is given to that step's worker only, and a skill
  whose permissions the run lacks is left off.
- **Provider balances.** A balance is read with that connection's own key and
  only from the provider's own fixed endpoint (`app/provider_balance.py`:
  `api.deepseek.com/user/balance`, `openrouter.ai/api/v1/key`), never from the
  connection's configured address, at most every five minutes. The key is
  never part of a response. Providers that publish this only to a separate
  admin key are named as such; the office asks for no admin key.
- **No third-party requests from the pages.** Geist is bundled under
  `app/api/web/fonts` with its licence, and the office loads no remote fonts,
  scripts or stylesheets, so opening a page tells no one outside this machine.
- **Paid tool calls are metered.** A tool you priced at review is checked
  before it is sent (`app/mcp/metering.py`): a call that would pass the task's
  cost ceiling is not sent and the task stops to ask; once the office's or the
  project's monthly limit is reached, the call is refused and the task goes on
  without it. A sent call is recorded at its price whatever came back. A tool
  that changes after review loses its price with its review.
- **Owner questions (E1).** A task that cannot go on without you stops with at
  most three questions, and only then. You answer on the task, through the same
  owner-only review endpoint as any other decision; the answers reach the
  research as your direction and authorise nothing. The Chief is told what the
  task can reach (the public web, the project's granted documents and tools,
  earlier answers, you) and that private evidence it cannot see is a limit to
  state, not something to find another way to. A task started in a Slack thread
  continues only the earlier task of that thread in the same project.
- **Rooms decide nothing by typing.** In a project room, "yes", "approved" or
  "go ahead" start and approve nothing. New work and direction go through a
  button the room itself put on that message, each pressable once, and an
  engineering approval is not answered in the room at all: its card links to
  the proposal.
- **The lease heartbeat.** A running task holds its worker's lease for 180
  seconds and renews it every 60 while it runs, not only when a step starts
  (`app/orchestrator/dispatcher.py`). Before this, a long research round
  on the live office outlasted its lease and recovery took the task from the
  worker still running it, so the same work could run twice.
- **Your calendars (7 October 2026).** A calendar link or app-specific password
  is sealed in the secret store and never returned by any route; the page
  shows only the calendar's name. Calendars are read straight from their own
  servers (`app/calendars/fetch.py`), never through the page reader, so a
  private calendar address does not leave the office. Only web addresses are
  read, a calendar is cut off at 10 MB, and a CalDAV reply declaring XML
  entities is refused before parsing. What agents (and so model providers)
  see is the owner's choice: busy/free, titles and times (the default), or
  everything (`agent_view` in `app/calendars/store.py`), for tasks and, since 7 October, for conversations (the Ask chat, discussions, Slack). Only the owner
  changes a calendar, from the Calendar screen; an agent may propose an event,
  added only by the owner's press. A prep brief starts only on the owner's press.
- **Calendar sign-ins (7 October 2026).** Google and Microsoft are reached
  with OAuth 2.0 and PKCE (S256); each sign-in's state is random, works once
  and lapses after ten minutes, and the public `/calendars/oauth/callback`
  does nothing without it. Since CAL10 the scopes also allow changing events
  (calendar.events, Calendars.ReadWrite), used only for the owner's own edits
  from the Calendar screen; agents never write. The owner's sign-in app and the tokens are sealed in the
  secret store; a company's error text is never carried, only a code. The
  Mac helper (`scripts/mac_calendar.py`) reads the owner token from the
  office's own `.env` and talks only to the office on 127.0.0.1; macOS asks
  the owner before it reads anything, and only after the owner's press.
- **The office's own browser (8 October 2026).** Chromium runs in the
  `browser` service only: its own network (`browser_net`, shared with no other
  service), no secrets, no mounts but its exchange folder, all capabilities
  dropped, an unprivileged user. Every request a page makes, and every page it
  lands on after a redirect, must be on the public internet; private,
  loopback, link-local and reserved addresses, files and browser pages are
  refused. Each step gets a fresh browser context with no cookies, downloads
  or device permissions. Pressing what signs in, submits, sends, pays, books
  or deletes, and typing a password, asks the owner (`app/browser/safety.py`).
  It is off until the owner turns it on.

## Boundaries added 8–10 October 2026

The office now builds software, publishes it if you ask, and does more from
Slack. Each item was checked against the code on 10 October.

- **Watches (WP6).** A watch reads only a public `http(s)` address (the same
  `web_address` check as quick lookups) through the office's own fetch
  service, or the search provider already configured; the office browser only
  when Browsing is on. It never fills in, signs in or buys. Started only by the
  owner's press (`confirm: true`); its model calls are capped per day and by the
  month's limit, and what it says goes only to the destination the owner chose.
- **Ideas from Slack (ID3).** "idea: …" and a 💡 are kept for the paired owner
  only; a 💡 reads that one message (`conversations.history`/`replies`) with
  the history scopes, and the confirmation is ephemeral, to the owner alone.

- **Build previews (B5, V1).** A build's website is served by the office at
  `/previews/{run}/{token}/…` with a sandboxing policy (`PREVIEW_POLICY` in
  `app/builds.py`): `sandbox allow-scripts allow-modals allow-popups
  allow-forms`, `connect-src 'none'`, `frame-ancestors 'self'`. The page runs
  in an opaque origin: it can run its own scripts but cannot read the office,
  call its API or carry your session. Because it carries no session, it is
  reached by a private link instead: an HMAC of the task under the owner key,
  compared in constant time; a wrong link is a plain 404. Older versions
  (`/v/{commit}/…`) accept only the build's own commits.
- **Live previews (LB1).** While a build is written, `/previews/{run}/{token}/live/…`
  serves the same way, under the same task link and policy. It reads only the
  attempt's clone, and only if that clone lies inside the engineering folder
  (`clone_of` in `app/live_build.py`); anything else is a 404. HTML pages get a
  small pointer script (`app/api/build_preview.py`) that can do one thing:
  post the tag, text and position of what you clicked to the office's window.
  The office's page accepts a message only from that preview frame
  (`event.source` checked in `LiveBuild`, `app/api/web/office/tasks.js`).
  The build's own scripts run in the same frame and could post the same kind
  of message, so what arrives is treated as text: cut short, shown to you
  before you send it, and quoted to the agent as untrusted (`pointed`).
- **Report pages (SL5).** Any answer can be opened at `/pages/{artifact}/{token}`
  (`app/api/report_pages.py`): an HMAC of that one artifact version, compared in
  constant time, served with `default-src 'none'` (inline styles and `data:`
  images only, no script, no request out, `form-action 'none'`), `no-referrer`
  and `noindex`. Anyone with the link who can reach your office can read it.
- **These links do not expire.** Preview and page links are derived from the
  owner key; changing `OWNER_API_TOKEN` is the only way to withdraw them all.
- **Steering a build (LB3).** Comments while a build runs come only from the
  owner: the office's own page (owner session) or the owner's reply in the
  build's Slack thread (members' replies are not taken). A comment is quoted
  to the coding agent as the owner's instruction and never run as a command
  (`app/runtimes/steering.py`). Resuming a Codex session keeps the sandbox:
  `codex exec resume` has no `--sandbox` flag, so the office passes
  `-c sandbox_mode="workspace-write"` with `--ignore-user-config` and
  `--ignore-rules` (`resume_command` in `app/runtimes/codex.py`); a spike
  confirmed a write outside the build is still refused. A forgotten pause stops
  the build after 30 minutes. The checks still decide whether a version lands.
- **Codex runs on your ChatGPT subscription.** Its credential is the Codex home
  mounted from `CODEX_CREDENTIALS` (read-write, because the OAuth token
  refreshes itself); the live office points it at your own `~/.codex`. Three
  consequences, known and accepted for now:
  - builds that can be steered are no longer `--ephemeral`, so their sessions
    are saved in that Codex home and appear in your own Codex history;
  - your personal Codex skills in that home are loaded into office builds;
    `--ignore-user-config` drops `config.toml` but not skills. Keeping them
    out needs a separate Codex home, which means signing Codex in once more
    for the office (open item, BF1 fix 4, the owner's choice);
  - a build's coding agent never sees any other secret: its environment
    carries only its own credential (`environment` in `app/runtimes/codex.py`).
- **The installer service (PK1).** When you allow packages, the `installer`
  service (`app/runtimes/installer.py`) installs them: it has network but no
  secrets and no volume but the engineering one, a read-only root, every
  capability dropped. It runs only two fixed commands, never one it is handed:
  `npm install` with install scripts off, and `pip install` of ready-made
  wheels into `py_packages/`. Package code runs later only in the coding
  agent's sandbox and the isolated runner, neither of which has network.
- **Putting a website online (PB1).** Netlify and GitHub tokens are sealed in
  the secret store and sent only to that company (`app/hosting.py`).
  Publishing is your press after ticking that it will be public. GitHub Pages
  only ever writes a repository this build created: a name already taken is
  refused (`repository_name_taken`), never written into.
- **Slack buttons (SL2).** A proposal's buttons stand for the words they
  replace (Start counts as a typed "yes", from you or a member, as before).
  Buttons that act on a task (Stop, Rework, Skip, Try again) count only from
  the owner; anyone else is told so. Approvals stay in the office.
- **Files shared in Slack (QA3).** Downloaded only from `https://files.slack.com`
  with the bot token, at most four per message, pictures up to 5 MB and text
  files (txt, csv, md, json) up to 200 KB, cut off while reading if larger
  (`download` in `app/channels/slack.py`, `_shared` in
  `app/channels/inbound_office.py`). Shared files are material, never
  instructions.
- **Quick lookups in chat (QA1).** A question that needs something live runs at
  most three read-only calls (search, read one page, look at one page in the
  office's browser), with no form filled, nothing signed in to or bought
  (`app/channels/lookup.py`). Addresses must pass the browser's own rules
  (`app/browser/safety.url_allowed`). What it finds is untrusted material,
  never instructions. It runs without a press, as you agreed on 9 October,
  and both model calls count against the bot's daily chat allowance.
  Known gap: the "look" call uses the office's browser whenever the browser
  service is running, without checking the Browsing setting that tasks obey.
- **Reading the web (WP2–WP4).** The office's browser opens a page only as a
  fallback, when the page service could not read it, only when Browsing is on,
  and at most four pages per task (`BROWSER_READS` in
  `app/tools/fetch_client.py`). Nothing new leaves the office. The outside
  services that already existed are unchanged: Brave for web search, and the
  `r.jina.ai` reader as the page service's fallback for pages that refuse it
  (it then sees the page address). `READER_BASE_URL=""` turns that fallback off.
- **No new search or fetch providers (owner's decision, 9 October).** TinyFish
  was declined, as were live Exa and Tavily search: no searches or page
  addresses go to a new third party.

# Quarterdeck

**A personal working agent for one person, on their own Mac.**
The first open-source project from **Navigate AI**, the organisation that
builds and maintains it.

*Why the name:* the quarterdeck is where a ship's captain stands and gives
the orders. Here you are the captain: the agents do the work, and the
decisions stay with you.

Quarterdeck is a personal working agent built for a sole user who prefers to work
on their own Mac, locally, using the API resources of the model providers they
choose. You set up the working environment and the ways of working you prefer:
which models each role uses, what the agents may read and do, how much they may
spend, and when they must ask you first. And because the whole office is plain,
readable source on your own machine, you can build on it and adjust it with
the local coding agents you already use, such as Codex, Claude Code or
opencode.

## At a glance

- **Yours, on your machine.** It runs in Docker on your Mac and answers only
  that computer by default. Your tasks, documents and history stay in your own
  database; nothing is hosted for you.
- **Bring your own models.** Connect the providers you already pay for:
  Anthropic, OpenAI, DeepSeek, Kimi, Qwen, GLM, MiniMax, OpenRouter or any
  OpenAI-compatible API. Give each role its own model, set a backup per
  provider, and keep budgets per task, project and month.
- **One lead, a team when you want it.** A lead agent answers and does small
  work straight away. Bigger work is planned, researched, criticised and
  written by a team of agents, and starts only when you press **Start**.
- **Nothing happens behind your back.** Anything that would build, run,
  publish, spend or widen what the office may do waits for your approval, and
  every answer says what it could not check.
- **Coding with your own tools.** When work needs code, the office hands it to
  the coding CLI on your Mac (Claude Code, Codex, opencode or DeepSeek
  Harness), signed in with your own plan and in its own sandbox, on a separate
  branch. The office reviews the result; nothing reaches your own branches
  until you merge it.
- **Your knowledge, read safely.** Folders and documents you choose are read
  by a reader with no network, and each project sees only what you grant it.
- **Where you work.** A browser office with discussions, a task board,
  recurring work and calendars, plus Slack and, if you turn it on, your phone
  over your home network or Tailscale.

**Who it is for:** one person who wants a private, configurable team of AI
agents on their own Mac, paid for with their own API keys, and who is happy to
shape it with a coding agent. **Who it is not for (yet):** teams, hosted use,
or anyone who wants a one-click app. It is Mac-first, and Docker Desktop is
required.

Five-minute overview for new readers: [docs/launch-kit.md](docs/launch-kit.md).
To try it: [Install](#install).

> **Status.** This is the first public release, shared as is under the
> [Apache License 2.0](LICENSE) by Navigate AI. It has been built and used
> daily on one Mac and has not yet been installed by anyone else, so expect
> rough edges. Issues and pull requests are welcome. Mac-only parts (the calendar helper,
> the coder on the Mac, the folder picker) are described below.

## See it in action

[![Quarterdeck in one minute: ask, plan, press Start, approve, get a document](docs/media/quarterdeck-showcase.gif)](docs/media/quarterdeck-showcase.mp4)

Twenty seconds of highlights play here, silent with captions. The full
one-minute tour is
[docs/media/quarterdeck-showcase.mp4](docs/media/quarterdeck-showcase.mp4).
It shows the real interface with sample data: no model was called while
recording, so the replies, the plan and the report on screen are examples.

## Install

### What you need

| Requirement | Why |
| --- | --- |
| A Mac | Quarterdeck is Mac-first. The calendar helper, the coder on the Mac and the folder picker are macOS only. |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) with Compose v2, running | The office and its database run in containers. |
| Python 3.9 or later | The system `python3` is enough: the launcher uses nothing else. |
| Git | To get a copy of this repository. |
| 5 GB of free disk space | The launcher refuses to start with less. |
| Ports 8000 and 5433 free on this computer | The office and its database answer there, on `127.0.0.1` only. |
| An internet connection | To build the images on first start, and to reach your model providers. |
| An API key from at least one model provider | Not needed to start, but the agents cannot work without one: Anthropic, OpenAI, DeepSeek, Kimi, Qwen, GLM, MiniMax, OpenRouter or any OpenAI-compatible API. You pay the provider directly. |

Optional, and each can be added later: a coding CLI on your Mac (Claude Code,
Codex, opencode or DeepSeek Harness) for work that needs code, a Slack
workspace, and Tailscale to reach the office from your phone away from home.

### Steps

1. Get the code:

   ```sh
   git clone https://github.com/NavigateAI-dev/quarterdeck.git
   cd quarterdeck
   ```

2. Start Docker Desktop and wait until it says it is running.

3. Start the office:

   ```sh
   python3 -m scripts.launch
   ```

   The launcher checks Docker, disk space and the two ports, creates a private
   `.env` and a credential key on first start, builds and starts the services,
   and opens http://127.0.0.1:8000/ in your browser, already signed in. The
   first start builds the images, so it takes a while; later starts are quick.
   If a check fails, it says what to fix.

4. Follow **Set up your office** in the browser: name the office, add a model
   provider under **Connections** (keys are entered there and nowhere else),
   and set your spending limits. To look around before entering a key, install
   the sample task offered there: it calls nothing and costs nothing.

5. Ask your first question in the box on the Desk.

To stop the office, run `docker compose stop` in this folder; your data stays
in its Docker volumes. Run `python3 -m scripts.launch` again to start it and
sign in. More detail is under [Local startup](#local-startup), and upgrading,
backing up and rolling back are in [`docs/operations.md`](docs/operations.md).

## Background and design

Self-hosted, approval-gated AI work organisation: a personal office of agents
that plan, research, weigh, criticise, write and build, for one owner on one
machine. How it is built: [ARCHITECTURE.md](ARCHITECTURE.md). Why it is built
that way: the decision records in [`docs/adr/`](docs/adr/README.md). How to
install, upgrade, back up and roll back, and what leaves your machine:
[`docs/operations.md`](docs/operations.md).


## What the office does

### Screens

The office is at http://127.0.0.1:8000/. It is drawn as a
place, **The Floor**, with a bar along the top:

| Place | What it is for |
| --- | --- |
| The floor | Your home: a plan of rooms (the Chief's office, the research bay, the writing room, the review desk) with each colleague at a desk showing the task in hand, a meeting room for each discussion under way, your **in-tray** by the door with everything that needs you, a box to ask the Chief, and your wall of widgets |
| Calendar | Your calendars by day, week, month or year; add, change or delete events |
| Rooms | Every discussion with the office, each its own chat with the agents (yours on the right, theirs on the left, replies and threads), its work as live cards, and a **whiteboard** beside it: what was decided, what is open, ideas pinned and what was sent from the room |
| Library | Every task and the board; a task's page holds its answer (in an editor beside it) and, in tabs, its Workflow, its Team room and its line of work |
| Business spaces | Each business line with its decisions, goals, discussions, projects and materials; projects with their own limits, models, notification channel and default deliverable |
| Office settings | Cards for Accounts, Colleagues, Limits and models, Knowledge, Tools, Skills, Rules, Connections, Calendars, Mobile & remote access, and the Handbook |

Every page below these has **Back** at the top left, to where you came from or
up a level when you opened the page directly, and a trail naming each level,
each above the page a link.

In the header is the **Island**. It says "Ready" when the office is idle, and
while work runs it shows each running task with a ring for how far it has come
through its plan and a ring for how much of its budget it has used, plus the
month's spend against your limit. It turns amber while anything waits on you,
and springs open to say when a task finishes or starts waiting (only on a
change, never on opening the page). The office's month, each project's month
and each task's budget warn at 80% and at 100%, once each per crossing, and a
provider account running low or out warns the same way. A soft chime is
available in the Island, off by default. Click it to see the running tasks.

A page left open across an update (a phone's home-screen app especially) is
told so: a bar says **The office has been updated · Reload**, and the page
reloads by itself the next time you move to another screen.

The interface uses Archivo and JetBrains Mono (Geist is still bundled), served
by the office itself so opening a page makes no outside request: navy ink on
a blueprint grey, and one orange for whatever needs you. Motion runs on
springs, and "Reduce motion" in your system settings turns it all off.
Answers and documents are set at a reading measure.

### Signing in

The launcher opens the browser already signed in, through a one-time code.
For a phone or another computer, set a sign-in name and password (10
characters or more) under Office settings → Mobile & remote access → **Sign in
with a password**; the sign-in page then asks for them. **Keep me signed in on
this device** keeps that device signed in for 30 days. Only a salted hash of
the password is kept, and five wrong tries stop password sign-in for 15
minutes (the launcher and the owner token still work).

### Talk first

The office opens on **the floor**: one box to say anything, with no project,
task or form needed, and around it only what concerns you now. What you say
starts a **discussion**; the Chief answers unless you name someone. The office
reads what each message commits to: talk is answered; "note this" files an
idea; "look into …" or "do …" is answered by the agents with what they know,
and offered as work with a **Start** button (shown as a live card in the
discussion and delivered back into it): nothing that spends money starts from
what you type. "Hold off" pauses that work; "next week" brings the discussion
back then; "redo it with …" offers to rework what was delivered. Bring a
colleague in (or name them with @) and they are given the discussion's brief;
"@Mia compare …" is Mia's to do. A quiet discussion gets a three-line brief —
where we were, decided, open — shown when you come back. Any message can
branch into a discussion of its own, linked both ways. The in-tray holds only
decisions that are yours, with where each came from; delivered results and
parked ideas sit on the wall. Select a passage of a delivered document, say
what is wrong, and that passage is revised into a new version, the earlier one
kept. A decision comes only from you (Record as decision on your own message),
and may replace an earlier one; related work stays within its business line
unless you allow more. A discussion belongs to no project until you place it;
a project's room is its first discussion.

Answering in a discussion, an agent is given that discussion's work with its
answers (finished or not), who its colleagues are and what each does, the
office's other recent work within the business line, your calendar as far as
you allow it, and the widgets you let agents read; it says when it answers
from general knowledge instead. It may call the tools granted to the
discussion's project before it replies (reading your Miro boards, say): tools
you **allowed** run on their own and the reply names each one; a tool set to
**Asks you first** is proposed under the reply with what it would send and an
**Allow this call** button, which runs it once. Paid tools stay with tasks,
where a cost limit counts them. Ask a colleague to block time and they propose
an event with **Add to my calendar…**; nothing is added without your press.

Answering an agent's question about work in progress passes your answer to
that work, and a short reply ("yes", "是的") never starts anything. A long
reply has **Copy** and **Open in editor**, which makes it a document of its
own, versioned and downloadable.

**Attach files or a folder** where work starts: the box to the Chief, a
discussion's message box and the new task form have **＋ Files…**, **＋ A
folder…** and **From documents**. What you pick is read on this computer by the
office's reader (PDF, Word, Markdown, text and more), kept among the office's
documents and made available to the work's project.

### How work flows

1. **You ask.** From New task, a discussion, a project room, a follow-up, a
   schedule, or Slack. A task has a title: leave the Title field empty and a
   short one is written from what you ask, in code and at no cost. A task
   also says what it should come to: a report, a comparison, a one-page brief,
   slides, **a dashboard with charts**, or **working software (a website or
   app)**, and how you want it handed over.
2. **The office tells the task what it knows.** The project's background,
   approved lessons, granted and attached documents and tools, recorded
   decisions, its business line's standards, your next two days from your
   calendar (as far as you allow), the widgets you let agents read ("From your
   wall"), and up to three recent finished answers that bear on the question
   (judged with TypeSafe; without `TYPESAFE_API_KEY` nothing is added). A
   follow-up is told the task it continues and, further back, the earlier
   links of its line of work. The task page's "What this task is told" shows
   exactly this, as it was when the task began.
3. **The Chief writes who the answer is for**, what they will do with it and
   what good enough looks like to them, and every role works from that. It
   then drafts a small plan (two to four steps for most tasks), has it
   challenged against that brief and revised (at most two rounds), and
   starts it. A step is `research`, `write`, or, when the office's own browser
   is on, `browse`; a step can carry a skill. The Plan tab shows each step,
   what it waits for and where it stands, live; the **Workflow** tab draws the
   task as it runs; the **Team room** tab shows what its agents post as they
   work. With **agents as a team** on (Settings → Models), steps read each
   other's posts and the critic, writer and advisor huddle after each wave.
   With the **advisor on call** (off by default), a stronger model advises the
   Chief at three moments.
4. **Steps run in waves**, at most four at once, with your limits, pauses and
   notes applied between waves. Pages are read five at a time, cleaned of
   menus, cookie notices and footers before a model sees them, and read once
   per task; a page that refuses or is empty until its script runs is opened in
   the office's own browser when browsing is on (at most four per task). The
   researcher also reads GitHub, YouTube transcripts, Bilibili, V2EX and RSS
   feeds; Settings → Reach shows what the office can reach right now. Up to
   three tasks run at the same time (Settings → Models → Tasks at once, 1 to
   8). When a step finds nothing, the Chief replans what is left (at most
   twice) and keeps what was done.
5. **The Critic reviews as the reader would**: at most three issues, ranked by
   what they cost the reader, each with why and the fix, and one plain
   sentence for you. Writing problems are fixed in the final answer, never by
   more research.
6. **The answer is written**, leading with the answer, then the useful part,
   what to do next and briefly what could not be checked, with the facts and
   reasoning folded underneath. It opens in an **editor** beside the task: a
   report edits like a Word page, slides like a deck (with speaker notes).
   Your edits save as new versions and every download carries them; select a
   passage to ask about it or ask for a rewrite (one paid call, on your press,
   shown as a suggestion). Any table of numbers is drawn as a chart.
   **Open as a page** gives a clean read-only page at a private link.
7. **You are asked to approve only when the answer proposes something to be
   done**: built, run, published or spent. A research answer that proposes
   nothing simply completes.

**Stepping into a running task.** A bar at the top of a running or paused
task, **Message the team**, reaches the agents within a move or two, even in
the middle of a step, and says when it was read. Each waiting step in the Plan
tab has **Skip**. An agent that is stuck (the office's browser repeating a move
that fails) stops and asks: **Give it a hint**, **Skip this part**, **Try
again as it is**, or **Finish with what it has**.

What else can pause a task for you: a spending, token or time limit (type your
own new limit, or **Finish with what it has**, which writes the answer from the
work done so far); a rule or a tool marked "needs approval"; anything the
office's browser would sign in, submit, send, pay, book or delete; a proposal
waiting for approval; or up to three questions when the Chief cannot even
understand the request without you. When every research step fails because the
model providers are down, the task says **The research could not run** and
offers **Try the research again**. Private evidence the office cannot see is
not a reason to stop: it is a limit the answer states.

A finished task is not a dead end. **Ask a follow-up** starts a new task told
what this one concluded. **Rework** reopens the same task (left without a note,
it improves what its review raised); it counts as new work against the limits.
You can also ask about a finished task in the browser chat, which answers from
its record at no cost and starts nothing.

### Building software

Ask the office to make something (a website, an app, a script, a tool) and it
plans a **build**: a short spec of what will be made, how, how you open it and
what proves it works. Nothing runs until you press **Start building**. Then:

- The coding CLI on your Mac writes it: Claude Code, Codex, opencode or
  DeepSeek Harness, signed in with your own plan and inside its own sandbox,
  in a separate worktree on an `office/` branch (see the paragraph after this
  list). The office then runs the build's own checks (`sh check.sh`) in its
  isolated runner with no network, and keeps the files only if they pass. When
  the checks fail, you can send the work back for another round.
- **Building now** is a live card: the agent's own plan ticking itself off,
  "Now: …" for the command or edit in progress, each edit opening to its
  change, and the page as it is being made beside it. **Say something while it
  builds**: type a comment, or **Point at something** on the page. "At its
  next step" (the default) gives your words to the same session once it
  finishes what it is doing; "Now" interrupts it; **Pause after this step**
  holds it, and its time limit, until you press **Continue**. **Stop the
  build** ends it within seconds, keeping nothing.
- The task then shows **What was built**: the files, **Download as zip**, and
  for a website a **Preview**, served sandboxed so it can never reach your
  office. Every finished build is a numbered **version**; **All versions**
  lists them, each can be previewed or downloaded, and **Restore as a new
  version** brings an older one back without overwriting anything. **Make the
  next version…** asks only what should change and continues in the same
  folder. A failed build says why in the task's own language and offers **Try
  the build again**.
- **Packages** are off until you allow them (Settings → Models → Coding agent
  → Allow packages). A build may then list npm packages or pip wheels, which
  the `installer` service installs with network but no keys and with install
  scripts off. The coding agent itself never has network, and installed
  packages are never saved into the project or the zip.
- **Put it online…** publishes a built website (any version, the latest by
  default) to **Netlify** (an `https://….netlify.app` address at once) or
  **GitHub Pages** (a new public repository in your account). You give a token
  once (sealed on this computer) and tick that it will be public. Publishing
  again updates the same place; a GitHub repository name you already use is
  refused, never overwritten.

Building needs `ENABLE_CODE_EXECUTION=true` in `.env` and Quarterdeck Coder
running on the Mac (Stage 36). The office runs no coding agent itself: the
helper runs your own coding CLI (Claude Code, Codex, opencode or DeepSeek Harness)
with your own sign-in and plan, inside the CLI's own sandbox, in a separate
worktree on an `office/` branch under `~/dev`. Start it from the office's
folder with `.venv/bin/python -m scripts.mac_coder start` (`stop`, `status`,
and `found` to list the CLIs it sees). Choose the CLI under Settings → Limits
and models. When it finishes, the office checks the result itself, in its
isolated runner, before a build is kept or your Merge is offered.

### The agents

| Role | What it does |
| --- | --- |
| Chief | Writes the reader brief and plans the task as steps, replans when a step comes back empty or the Critic asks for more, writes the answer, leads the project room, and asks you questions when it must |
| Researcher | Answers one research question per step from web search, pages it reads, the project's granted documents, earlier answers and up to three calls to the project's granted tools; may cite only what it was given |
| Strategist | Does the writing step where a task needs a weighed recommendation or proposal; a plain summary goes to a summary writer instead |
| Critic | Reviews the plan before work starts and the research and writing before the answer: at most three ranked issues; may make up to two lookups with tools you allowed outright |

A browse step is driven by an agent through the office's own browser, and a
build by the coding agent you chose. Each role has its own model, set for the
office and optionally per project, through any OpenAI-compatible provider
(presets include DeepSeek, OpenRouter, Moonshot AI (Kimi), Alibaba Cloud
(Qwen), Z.ai / Zhipu (GLM), MiniMax and SiliconFlow). You can customise a role
into a named agent with its own instructions, skills and chat bot. A business
line can hold each role to its own standard of proof.

### The office's own browser

A sandboxed browser your agents can use for what reading pages cannot do: a
site's own search or filters, going through pages, a form that shows a price
or availability. Turn it on under **Settings → Models → The office's own
browser** (off by default); the Chief then plans a browse step when one is
needed, or when you tick **Use the office's browser for this** on a new task.
It runs in the `browser` container on a network of its own: it cannot reach
the office, your computer's network or your files, and has none of your
logins. Each task gets a fresh browser. Anything that signs in, submits,
sends, pays, books or deletes, and typing a password, stops the task and asks
you; once you allow it, the browser replays its earlier moves at no cost and
carries on. The task shows **The office browser**: the page it is on and every
step it took, with screenshots.

### Project rooms

Every project has a room where you talk with its agents, laid out as a chat:
your messages on the right, the agents' and the office's on the left, and a
typing bubble while agents answer. Ask a question and the Chief answers; name
an agent with @ and that agent answers, in its own voice, from what the office
recorded about this project. Name up to six and they answer in turn; "everyone"
(or @everyone, @all, @team) reaches every active agent, the lead first. An
agent never answers another agent on its own. Hover a message to **Reply** or
**Reply in thread**.

The project's tasks report into the same room as they run. Typing never decides
or spends: an agent that thinks new work is needed offers **Start as a task**.
A message sent while a task is running can be sent to that task as direction.
What the project's tasks are waiting on sits at the bottom of the room. Each
reply shows its cost and counts toward the month's spending, and each agent has
a daily allowance for room replies.

### Accounts and tool servers (MCP)

Office settings → **Accounts** lists what the office can connect, each by your
consent on the company's own page: Google (Gmail and Drive, read only),
Microsoft 365 (Outlook mail and OneDrive, read only), and services such as
Slack, Notion, Linear, Atlassian, Miro and Dropbox. Tabs: All, Connections,
Logins, API keys. Google and Microsoft use the sign-in app you made for the
calendar and its return address; for Gmail and Drive, enable the Gmail API and
Google Drive API in that Google project. Each connected account says which
projects may use it (**Projects…**), and disconnecting deletes its sign-in from
this computer.

Underneath, these are tool servers. You can also point the office at a remote
MCP server (Streamable HTTP, over HTTPS, inside the same host allow-list that
bounds page fetching). The Tools screen has a **catalog** of vendor-run
servers, each checked by hand; Add fills in the form. `python3 -m
scripts.mcp_catalog [--registry]` re-checks the catalog for a person to review;
the office never installs from the registry by itself. Servers that sign in
with your account (OAuth with PKCE) say Needs sign-in; the office never sees
your password, and its tokens are encrypted, refreshed before they expire, and
never shown to you or to an agent.

Nothing a server offers is callable because the server says so. You **review
each tool**: allowed, needs your approval, or refused (**Decide the undecided
at once** sets one choice for the tools that only read and one for the rest),
and a tool that changes after your review is unreviewed again. SPEC section 18
outranks the review: a tool that does something the office never allows is
refused whatever you set, and one that changes code or records always stops for
you. A server is **granted to a project**; every call also passes the office's
rules. A tool whose vendor bills per call is given a price, and each call
counts toward the task's cost limit and the monthly limits.

### Skills

Skills are instructions only. The office ships a small curated catalog
(`comparison-table`, `competitor-brief`, `source-quality-check`), and **Add
from skills.sh** on the Skills screen takes a skills.sh link, a GitHub link or
`owner/repo skill`. The office reads the skill, pins it to the exact commit it
read, and shows you what it is before anything is installed: its instructions,
the three security audits skills.sh publishes, any code it carries (never
installed), and what it assumes the office cannot do. You choose which role
uses it. The Chief attaches a skill to the plan step it fits. The office checks
for updates, and a business line can have its own pack of skills.

### Calendars and your wall

Office settings → **Calendars** connects your calendars: **Use the calendars on
this Mac** (macOS asks in its own dialog; a small helper app, `scripts.mac_calendar`,
started by the launcher and the upgrade, reads every calendar in your Calendar
app), **Sign in with Google** or **Sign in with Microsoft** (a sign-in app you
make once, guided), or a calendar link or CalDAV account. Calendars sync every
ten minutes. The Calendar screen shows them as an agenda (the default on a phone) or by day, week, month or year, and
you can add, change or delete events in the Mac's calendars and in those
connected by a Google or Microsoft sign-in; your colleagues never write to your
calendar. **Your day** runs across the floor; while you are in a meeting the
in-tray holds things and notifications wait. Before a meeting the Chief offers
a **prep brief**, started only by your press and capped at $0.30.

**Ideas**, the board under your day, catches a thought in seconds: write it
on the "Keep a thought for later" line and press Enter, or press `i` on any
page. Keeping one calls no model. A new idea has one-tap **Today**,
**Tomorrow** and **Monday**; **More…** gives any date, Someday, a project,
**Block time** on your calendar, Start or Drop. **Sort my ideas…** asks the lead
for suggestions (merge, reword, a day, a project): one paid call, its cost said
first, and nothing changes until you accept a suggestion. From Friday the board
asks for what is not planned yet, and a Slack channel can be told too. On its day it is under
**Today**, and your colleagues read it with their work. One whose day has
passed stays on top, marked. **Start** says what it may spend (up to $0.50)
before the team takes it up in a discussion; **Done** is for what you handled
yourself. "Note this" in a discussion lands here too, and so
does "idea: …" sent to the bot in Slack, or a 💡 you put on any Slack message
(the bot confirms privately). On a phone, open the office at `#idea` (bookmark
it or add it to your home screen), or tap the 💡 button.

**Watching** has the office check a page or a topic for you: an address or
search words, how often, and if you like what to tell you about ("the price
drops below €50"). Each check reads it for free and compares; only a change
costs one small model call, within a daily amount you set (5 cents by default).
What it finds goes to the Slack channel you chose and to the widget.

The calendar's day, Ideas, Watching, Delivered, Parked for later and This month are
**widgets on your wall**. **Arrange** moves them, sizes them and switches
whether agents read them; **× Take off** keeps what the widget knew unless you
choose **Clean it out**.

### Slack

The office's bots answer in Slack (direct messages and @mentions), from the
office's records. You add other people under Connections → Chat channels; a
member can ask and give work, on your keys and inside your budgets, and can
never approve anything. Anyone you have not added is answered nothing.

- **Quick lookups.** A question that needs something live is looked up in the
  thread, in up to three read-only steps: a web search, reading one page, or
  opening one page in the office's browser and seeing it (radar, maps,
  charts). The answer says what it looked at and when, with the screenshot.
  Nothing is filled in, signed in to or bought. It runs without pressing
  Start, inside the bot's daily chat allowance. The office's own chat on your
  computer does the same.
- **Bigger work is offered**, with **Start** and **Not now**, and goes to the
  project it belongs to: a clear match is offered; a likely one is proposed
  (with **New project**); reply "call it <name>" to put it elsewhere. Routing
  reads the recent turns of the thread, so a remark about the conversation is
  never turned into a project.
- **It reads what you share**: pictures are seen by the model, and text files
  (txt, csv, md, json) are read.
- **The work arrives in the thread**: the answer itself, cut at a readable
  length, the whole document as a file (the format you asked for, otherwise
  Word), up to three charts as pictures, and a link to the task and "Read it
  as a page" when the office has an outside address. One progress message is
  edited in place as the steps go by, build steps included.
- **Buttons**: **Stop** on a running task, **Skip this step** and **Try
  again** on a stuck one, **Rework** and **Open in the office** on a finished
  one. Only your presses act on a task; Stop and Rework ask first.
- **Reply to steer**: while a task from a thread is working, your reply goes
  to its team as a note (to a build, as a comment); if the task is stuck, your
  reply is the hint it asked for.
- **Standing reports**: a schedule can post each result to a Slack channel
  (**Post to Slack…** on the schedule's row under Settings). The bot must be
  invited to that channel.
- **Approvals stay in the office.** Typing in Slack never approves anything.

Thread memory and a proposal waiting for your yes are kept in the database, so
they survive a restart; a proposal lapses after a day. Connecting Slack is
under "Connecting Slack" below.

### From your phone

Office settings → **Mobile & remote access**. On the home network: run
`python3 -m scripts.access home` once, then **Check and turn on**. From
anywhere: the `remote` container joins your private Tailscale network as
"quarterdeck" and Tailscale Serve routes `https://quarterdeck.<tailnet>.ts.net` to
the office. It never uses Funnel, port forwarding or the Docker socket.
**Connect your phone** shows a QR code holding a one-time sign-in code, or set
a password (above). The office answers only on 127.0.0.1 and the addresses it
has checked, and refuses every other host. Guide:
[docs/mobile-access.md](docs/mobile-access.md); phone checklist (not yet
run): [docs/mobile-access-checklist.md](docs/mobile-access-checklist.md).

### Performance

Settings → **Performance** shows each model's reliability and response times,
how long each kind of step takes (typical, and the slowest one in ten), how
pages were read and why some failed, why tasks failed, and a **benchmark** of
eight fixed questions. The benchmark is paid work: it starts only on your
press, after showing the cost estimate, and runs in the project "Office
benchmark". Settings → Models also has a card per provider (who uses it, what
it has left, its last call, errors in the last hour) and its **backup**: when
a provider runs out of credit, refuses its key or fails three calls in a row,
its calls go to the backup you chose, inside the same budget and rules.

### What else it keeps

Business lines with their standards and objectives, subjects with their
dossiers, reviewed project memory (exportable as Agent Skills), schedules and
templates, and labels. [`CHANGELOG.md`](CHANGELOG.md) describes each.

## Data and privacy

Everything the office records stays on this machine: PostgreSQL and the
workspace live in named Docker volumes, and every published port binds to
localhost unless you publish the office on your home network. What leaves it,
and only when you have configured it:

- prompts and records to the model providers you connected, through each
  provider's own API;
- search queries to the search provider (`SEARCH_PROVIDER`: `brave`, `exa` or
  `tavily`);
- page requests from the credential-free fetch service, which names itself and
  does not imitate a browser; a page that refuses the office is read through a
  reader service (Jina Reader) instead, and the report says which. Set
  `READER_BASE_URL=` (empty) in `.env` to turn that fallback off;
- page visits from the office's own browser, when you have turned it on;
- tool calls, with what an agent sends them, to the tool servers and accounts
  you granted;
- calendar reads and your own edits, to Google, Microsoft or the calendar
  address you connected;
- a build's spec and files to the coding agent's provider (OpenAI for Codex,
  Anthropic for Claude Code); package downloads from npm or PyPI when you
  allow packages; a website's files to Netlify or GitHub when you press Put it
  online;
- a skill's files from GitHub when you preview it;
- your provider keys to their own provider, to read what is left on the
  account;
- messages and files to and from Slack, when a chat channel is connected.

Keys and tokens are encrypted at rest (see Credential store). Backups exclude
the root key. Fonts are bundled, so opening a page tells no one.


## Local startup

Prerequisites: Docker Desktop with Compose v2. The launcher needs only the
system `python3` (3.9 or later); no provider credentials are needed to start.

```sh
python3 -m scripts.launch
```

It checks Docker, disk space and ports, creates a private `.env` and credential
root key on first start, starts the services, waits for health, starts the Mac calendar helper, and
opens the browser already signed in through a one-time code in the URL
fragment. The owner token is never printed. `--no-open` prints the single-use
link instead. For development without the launcher:

```sh
uv sync --frozen
uv run python -m scripts.init_local
docker compose up -d --build api runner
```

### Services

`docker-compose.yml` defines these services:

| Service | What it does |
| --- | --- |
| `postgres` | The database, on `127.0.0.1:5433` |
| `migrate` | Applies migrations once before the API starts |
| `api` | The office: the interface, the API and the run worker |
| `runner` | Runs build and repository checks: no network, no secrets, read-only root |
| `installer` | Installs a build's packages when you allow them: network, no secrets, install scripts off |
| `fetcher` | Reads web pages for the agents, with no credentials |
| `browser` | The office's own browser (Chromium), on a network of its own |
| `sources` | Reads your granted folder: no network, folder mounted read-only |
| `remote` | Tailscale for remote access; idle until you connect it |
| `tests` | The test image (profile `test`) |

The launcher starts `api`, `runner`, `sources` and `installer`;
`scripts.upgrade` starts `api`, `runner`,
`sources`, `fetcher`, `remote` and `installer`. Neither starts `browser`: if
Settings says the office's browser is "on, not running", start it with
`docker compose up -d --build browser` (and `fetcher` the same way if the
launcher brought the office up).

### Operations

`docker compose exec -T api python -m scripts.backup` writes a private archive
to `./backups`; `scripts.restore` restores into an empty office;
`scripts.verify_restore` compares a restored office with its archive;
`scripts.revoke_sessions` signs every browser out; `scripts.rotate_secret_key`
rotates the credential root key. `scripts.restore_drill` proves the newest
archive restores, in a throwaway stack with its own volumes and ports, without
touching the office you are using. `scripts.upgrade` performs the whole
upgrade: it stops taking new work, waits for work in flight to reach a step
boundary, backs up, rebuilds, migrates, smoke tests and resumes. It refuses to
continue without a fresh backup, and a failed smoke test fails the upgrade and
leaves the office paused with the rollback printed. `scripts.package` builds
the release archive another machine starts from: the source, the Compose
files and the papers, with a checksum and an install note, and none of your
keys, backups or granted folders. It is not signed or notarised, and the
archive says so. `scripts.mcp_catalog` re-checks the tool server catalog and
changes nothing. `python3 -m scripts.pick_folder` chooses the folder the
office may read, with the macOS folder dialog. `python3 -m scripts.mac_calendar
start|stop|status` runs the calendar helper by hand. `python -m
scripts.quality_eval` re-runs planning, review and writing on finished tasks
and compares them blind (paid; it writes nothing to the office).

### Connecting Slack

1. At api.slack.com/apps, **Create New App → From a manifest**, paste
   [`docs/slack-app-manifest.yaml`](docs/slack-app-manifest.yaml), and
   install it to your workspace. The manifest asks for the bot scopes
   `chat:write`, `chat:write.customize`, `channels:read`, `groups:read`,
   `app_mentions:read`, `im:history`, `im:read`, `im:write`, `users:read`,
   `files:write` and `files:read`, turns on Socket Mode and Interactivity, and
   subscribes to `app_mention` and `message.im`. No public address is needed.
2. In the office, Office settings → Connections → Chat channels → **Connect
   workspace**, and paste the Bot User OAuth Token (`xoxb-…`).
3. **App credentials**: paste an app-level token (`xapp-…`) with the
   `connections:write` scope, so you can message the office from Slack.
4. **Pair Slack**: send the code shown to the bot as a direct message. Every
   office bot in that workspace then listens to you only.
5. To post into a channel (notifications or standing reports), add it under
   Chat channels and invite the bot to it in Slack.

A Slack app made before 9 October lacks `files:write` and `files:read` and has
Interactivity off. Until it is updated, answers still arrive as text, without
files or buttons, and shared files are not read. The Chat channels card says
so; **How to update it** lists the steps (add the two scopes, switch on
Interactivity, reinstall the app, then **Check again**).

### Upgrading

Upgrade with `python3 -u -m scripts.upgrade` from the office's folder. It
pauses new work, takes a backup, rebuilds the services, migrates the database
and smoke tests before taking work again.
[CHANGELOG.md](CHANGELOG.md) says what changed;
[docs/operations.md](docs/operations.md) says where data goes and how to
install, upgrade, back up, restore and roll back.
[SECURITY.md](SECURITY.md) says what to do if a key may have leaked, and who
can reach the office in each way you can open it.

`init_local` creates a private `.env` with a generated database password
and owner API token. It never overwrites an existing file.
`.env` is ignored by Git
and excluded from Docker build contexts. Compose passes database passwords
separately and the application URL-encodes them. For host development, URL-encode
reserved characters when changing the password embedded in `DATABASE_URL`.

- Interface: http://127.0.0.1:8000/
- API reference: http://127.0.0.1:8000/docs
- Database readiness and migration check: http://127.0.0.1:8000/health
- Process liveness: http://127.0.0.1:8000/health/live
- PostgreSQL: `127.0.0.1:5433`, database/user `quarterdeck`

All published ports bind to localhost unless `scripts.access home` publishes
the office on the Mac's network address. An access layer is required before
any public deployment. Code execution and merge-request publication are off by
default, and agent deployment cannot be enabled. Every data route requires the
owner: the browser through a session cookie (HttpOnly, with origin and CSRF
checks), scripts through the owner bearer token. Health probes, the static
browser shell, the session sign-in routes and the three OAuth callbacks
(Slack, tool servers, and calendars and accounts, whose redirects cannot carry
the cookie) remain public. Build previews (`/previews/…`) and answer pages
(`/pages/…`) are served by private links signed with the owner secret, so
anyone with such a link who can reach your office can open it. Do not expose
this service.

The one-shot `migrate` service applies migrations before the API starts.
PostgreSQL and workspace data use persistent named volumes. `docker compose
stop` stops services without deleting data. Do not use `down --volumes` unless
you intend to erase local data.

## Model configuration

The office calls each provider directly, with that provider's own key from the
encrypted credential store below. There is no model gateway: the LiteLLM
gateway was retired on 22 October 2026 after a security advisory and a
compromised release, and no third-party proxy holds the keys.

Add providers in the browser under Connections (any OpenAI-compatible
provider, with its model catalogue and prices), then choose a model for each
role under Settings → Models: Chief, Researcher, Strategist and Critic each need
one, and a project can choose its own. Until a role has a model its work stops
with `role_has_no_model`, and Settings, the setup steps and the office status
name the roles still without one. Code asks for a role's model by its logical
alias (`CHIEF_MODEL_ALIAS=0001`, `RESEARCH_MODEL_ALIAS=0002`,
`STRATEGY_MODEL_ALIAS=0003`, `CRITIC_MODEL_ALIAS=0004`); the alias names the
role, never a provider model.

```sh
docker compose exec -T api python -m scripts.smoke_test_models
docker compose exec -T api python -m scripts.verify_agent_contracts
```

This makes real, billable requests using your connections. It prints one JSON
record per role with pass/fail, resolved model, latency, tokens, and cost. It
exits nonzero if any role fails or has no model; response bodies and
credentials are not printed. The application retries transient errors at most
twice by default. Each call has a total timeout and output token limit.
`verify_agent_contracts` then makes one real request per role and checks the
response against that role's output contract, using fixed search results so the
Researcher check needs no search credential.

A call's cost is its tokens at the model's published price, from a price
catalogue downloaded at most once a day and kept on disk (data only, read
without the network when a call finishes); a model without a published price
counts no cost, and a local one costs nothing. Pictures go only to a model
published as reading them.

Settings → Models shows a card per provider and reads what is left on each
account with its own key, where the provider publishes it (DeepSeek's balance,
OpenRouter's key limit, Moonshot and SiliconFlow; others need a separate admin
key and say so), at most every five minutes, and warns when one runs low or
out. An account out of balance is reported as `insufficient_balance`, not as a
rejected request. A backup per provider takes over its calls, tasks and chat
replies alike, when it runs out, refuses its key or fails three calls in a
row; by default work goes back once it answers again.

Structured output is prompt-driven and validated locally for every provider,
because JSON mode support differs by provider. On the direct Anthropic path the
role schema is also sent as structured output; the local validation still
applies. Role schemas are large: the Strategist used 2,738 output tokens in
verification, and on models that think by default the thinking counts toward
the same cap, so `LLM_MAX_OUTPUT_TOKENS` defaults to 16000
(`DELIVERABLE_MAX_OUTPUT_TOKENS`, 16384, for long-form writing;
`CHAT_MAX_OUTPUT_TOKENS`, 4096, for chat and room replies), as written into
`.env` by `init_local`. A truncated response is reported as
`incomplete_response` and never parsed; a deliverable cut off at the limit is
asked for again, shorter. `LLM_TIMEOUT_SECONDS` defaults to 240 (at most 300);
raise it if you select a slower model.

Model/search operating costs are distinct from forbidden agent-initiated
purchases.

## Credential store

Provider connection keys (V1 `/connections`) are stored encrypted, not in
`.env` or the database. `init_local` creates a 32-byte root key at
`.secrets/root.key` (mode 0600, never overwritten), which Compose mounts
read-only into the API. The encrypted store is
`/workspace/secrets/credentials.json`; each entry is AES-GCM with the secret's
name bound in, so entries cannot be swapped. The database holds only a
`secret:connection-<id>` reference.

```
PUT    /connections/{id}/credential   {"secret": "..."}   store (never echoed)
DELETE /connections/{id}/credential                       revoke locally
POST   /connections/{id}/check-credential                 presence only, no provider call
```

Tool server keys, the tokens from a tool server or account sign-in, calendar
links and sign-ins, the Claude Code key and the Netlify and GitHub publishing
tokens are kept in the same store, reach only their own service, and are never
shown to you or to an agent.

`env:NAME` references remain supported. Validation errors never echo request
input. Losing the root key makes stored keys unrecoverable; re-enter them. Keep
the key out of backups of the store. Rotate with:

```sh
uv run python -m scripts.rotate_secret_key
```

Rotation writes `root.key.new`, re-encrypts every entry, then swaps the key.
If interrupted, rerun it; it resumes from whichever key reads the store.

## Agent contracts

Each role has a strict Pydantic output contract; output that does not validate
never advances the workflow. The contract's JSON Schema is sent on every
request, because a model cannot match a contract it has never been shown: asked
only to "return JSON", models return well-formed JSON in a shape of their own
invention. A Markdown code fence around the object is tolerated.

Every invocation receives one `ProjectContext`, so no project sees another
project's background or constraints.

The Researcher gathers before answering and may cite only what it was given:
search results, pages it read, passages from the project's granted documents,
and results of the tool calls it made. A fabricated URL is rejected as
`unverified_source` and an upgraded `source_type` as `misreported_source_type`.
When there is nothing at all (no results, no passages, no tool results) the
invocation fails as `no_search_results` before any model request is made. A
source that refuses the office is named as refused, with the status it
answered, rather than becoming an unknown.

Set `SEARCH_PROVIDER` to `brave`, `exa` or `tavily` with a `SEARCH_API_KEY`
for real research; the Brave adapter is verified against the live API, and
Exa and Tavily return the page text with each result. The default `static`
provider returns no results and exists for tests, so research cannot be
accepted without real sources. Unlike the provider model keys, the search key
is held by the application, because the search tool is application code.

Queries and result counts are bounded, and provider failures are reported as
codes, never response bodies. Brave's free plan permits one query per second
while research reruns within a revision loop, so a rate-limited query waits for
the provider's own reset window and retries a bounded number of times rather
than failing the run.

Each invocation emits an `InvocationEvent` with role, alias, project, status,
latency, token usage, cost, and error code. The model the provider says served
the call is recorded alongside the alias. Prompts and model output are
deliberately excluded so the events are safe to log.

Artifacts are written as immutable versions under a project-scoped path, with
atomic publication and confinement to the workspace root. Reads require the
owning project ID.

## Orchestration

Each task gets its own plan (D1). The Chief's plan carries `steps`: a small
graph of typed steps from a fixed catalogue, `research` (one question, for the
Researcher), `browse` (for the office's own browser, offered only when it is
on) and `write` (the deliverable, for the Strategist or a summary writer), each
with a brief, the steps it depends on, an effort (light, standard, deep) and
optionally a skill. Code validates it: known types, unique ids, known
dependencies, no cycles, at most eight steps. Before it runs, a plan reviewer
(on the Critic's model) challenges the draft against the reader brief, at most
two rounds. Review and the answer are not steps; they always run. A build is
its own kind of task: its plan is a build spec that goes straight to a
proposal for you to start.

A `work` node runs every ready step at once, at most four (`MAX_PARALLEL_SUBTASKS`),
as one graph step, so a checkpoint, the budget, pause and the owner's direction
all apply between waves. After each wave a progress check in code decides:
another wave; a `replan` when a step failed or found nothing (the Chief rewrites
the steps not yet done, at most twice); or review, when nothing is left or
nothing moves. In a step-planned task the Critic's REVISE is a replan too: the
Chief adds steps aimed at what was missing, bounded by `MAX_REVISION_LOOPS`,
and a replan that adds nothing stops as no progress. A stuck step is kept
pending and the task pauses to ask you (`POST /runs/{id}/help`: a hint, skip
or retry). A plan without steps (every run recorded before D1) runs the old
fixed chain of plan, research, strategy, critique and synthesis, so recorded
runs resume as before.

Before planning, the Chief is told what the task can reach (the web, the
project's documents and granted tools, earlier answers, and you) and plans
against that. It stops the plan only to ask the owner questions it cannot
proceed without (`GET /runs/{id}/question`). A passing critique is never
overruled by the check for a stuck revision loop, which runs only when the
Critic asks for another revision.

`POST /runs` records the run row and returns `202` with a run ID. The worker
executes up to `runs_at_once` runs together (Settings → Models, from
`MAX_CONCURRENT_RUNS`, 3 by default; the rest wait in order), and the queue
holds only pointers to durable rows. Each run is claimed in the database
before it executes, so one submitted twice still runs once. Each node's output is persisted as an immutable artifact version the
moment that node completes, so a later failure never discards earlier work.
Artifact identity is derived from the run and stage, which is what stops a
replayed dispatch from creating a second copy of work already recorded. A run
that crashes unexpectedly is failed once with its reason in words, rather than
retried for ever.

LangGraph checkpoints are keyed by run ID and are authoritative for
continuation; run rows are the domain projection. Checkpoint tables are created
by LangGraph, separately from the Alembic-managed domain tables. On startup,
runs left mid-flight are reclaimed and resumed from their checkpoints.

Structured output gets a bounded corrective retry: models intermittently return
JSON that does not satisfy the contract, and one corrective attempt recovers
most of these. Citing a source that was not supplied is corrected the same way.
A retry cannot weaken the check, because acceptance still requires every source
to be in the searched set, so persistent fabrication still fails the run.
Invalid output never advances a run.

## Approval and autonomy policy

The autonomy policy (SPEC section 18) lives in `app/policy.py`, not in prompts.
A denied action is refused regardless of approval: no human decision and no
configuration flag can promote it. The same section outranks your review of a
tool server's tools: a tool tagged as doing something the office never allows
is refused whatever you set, and one that changes code or records always stops
for you. Around it sits the policy engine (the Rules screen): named policies
that ALLOW, DENY or ASK at the model call, tool call and result hook points,
layered office then project, the most restrictive verdict winning, every
decision recorded with its reason.

Approval is asked only when there is something to authorise (D1). The synthesis
says `proposes_action`, true only when the owner is asked to authorise
something to be done: built, executed, published or spent. An answer that
proposes nothing completes without an approval; one that proposes an action
with a scope waits for you. A synthesis recorded before this, with a scope,
still waits, as it did then. **Finish with what it has** gives a final answer
that waits for nothing.

When the chain reaches approval the run stops with `WAITING_FOR_APPROVAL` and a
pending approval record holding the exact proposal. The proposal binds the
objective, recommendation, engineering scope, permitted paths, and the
identity, version, and content digest of every artifact used as evidence. Its
SHA-256 digest is what the owner approves. For a build, the bound scope also
says whether packages may be installed; **Start building** is that approval.

Decisions go through `POST /approvals/{id}/approve|reject|revise` and require
the owner (the browser session or the bearer token). The request carries the scope digest and an optional
comment, and nothing else: there is no approver field and no approved flag,
because both are derived server-side. A decision whose digest no longer matches
the stored proposal is refused as `stale_scope`, and resolving an approval
twice is refused as `already_resolved`. Rejected and superseded approvals are
preserved rather than deleted.

A decision record is created only on explicit approval, never on rejection and
never by an agent. Rejection ends the run without invoking the engineering
boundary at all. A revision requires a validated research or strategy target
and produces a new critique and a new approval; in a step-planned task it is a
replan, as the Critic's is. Rework of a finished task (`POST /runs/{id}/rework`)
counts as new work, and if its new answer proposes an action it asks for
approval again. Typing in a project room or in Slack never approves anything:
approvals are buttons in the office. Approved engineering is carried out once,
even if the office restarts in the middle of it.

Set `OWNER_API_TOKEN` (at least 32 characters, generated by `init_local`) and
`OWNER_NAME`. Approval endpoints return 503 while no token is configured, so
the gate cannot be bypassed by leaving it unset.

## Engineering runtime

Engineering runs only against a recorded approval, and only inside its approved
constraints. Each attempt gets its own git clone under the workspace root, and
its base commit is recorded before anything executes.

There are two targets. A **build** (above) works in the office's own git
project folder for that build, under `builds/` on the `engineering_data`
volume; the approval binds it with every path permitted and `sh check.sh` as
its test command, and checked work is fast-forwarded into that folder, each
version a commit. **Configured engineering** works on the repository named in
`ENGINEERING_*`, as below.

Completion requires evidence rather than a report: the diff is read from git
and every changed path must match the approved permitted paths, and the
approved test command must pass. A runtime timeout, a failing test, a change
outside scope, a moved base commit, or no change at all are each recorded as an
incomplete attempt. A runtime's account of its own work is never sufficient.

Repository test commands are untrusted code, so they do not run in the API
container. With `ENGINEERING_TEST_RUNNER=isolated` (the Compose default) the
API hands each command to the `runner` service through the `engineering_data`
volume. The runner has no network, no secrets, no Codex or credential mounts, a
read-only root filesystem, dropped capabilities, and CPU, memory and process
limits; it re-validates that every job's directory lies inside its volume. If it
does not answer, the test fails as `runner_unavailable` rather than running
locally. Test dependencies must be installed in the runner image, vendored in
the repository, or (for builds, when you allow packages) installed by the
`installer` service, which runs only `npm install --ignore-scripts` or `pip
install --only-binary=:all:` and never a command it is handed. `local` exists
for host development only.

Every child process runs as a fixed argument vector with no shell, a timeout,
an output cap, its own process group (killed with everything it started when
the command ends or times out), no input, and an environment allowlist. No
provider, gateway, owner, or GitHub value is passed to it.

The office runs no coding agent itself. Approved coding is handed to
Quarterdeck Coder, a small helper on your Mac
(`.venv/bin/python -m scripts.mac_coder start`), which runs the coding CLI you
chose (Claude Code, Codex, opencode or DeepSeek Harness) with your own sign-in
and plan, inside that CLI's own sandbox and never with a flag that skips it,
in a separate git worktree on an `office/` branch. The CLI's questions about
permissions come to you. When it finishes, the office checks the result in
its isolated runner before a build is kept or your Merge is offered; nothing
is pushed, and nothing reaches your own branches without your press.

`ENABLE_CODE_EXECUTION` is false by default, which selects an executor that
runs the same permission checks and executes nothing. With it enabled, the
engineering node runs the approved task through the chosen coding agent. For
configured engineering the target repository, base branch, permitted paths, and
test command are operator configuration (`ENGINEERING_*`), never proposed by a
model, and they are bound into the approval scope so changing any of them
changes the digest and invalidates the approval.

`tests/integration/test_coder_handoff.py` runs the whole hand-off against the
database with the helper played in-process; `tests/unit/test_mac_coder.py` runs
the helper on real git folders with a stand-in CLI.

```sh
docker compose --profile test run --rm tests pytest -q
```

git-dependent suites are skipped on a host without a usable git and run in the
test image, which is the same runtime plus development dependencies.

## Interface

The interface is at http://127.0.0.1:8000/ (its files are served under `/ui/`)
and its screens are listed under "What the office does" above. It is plain HTML
and JavaScript with no build step, served by the application from
`app/api/web`: one file per screen in `app/api/web/office/`, registered as
`Office.screens.*`, with The Floor's look in `floor.css`, the motion (a small
spring solver, the selection pill and the Island) in `motion.js` and
`motion.css`, the answer editor in `editor.js`, and the fonts in `fonts/`. It
reads the office by polling: the task room and the project room read new events
and messages by cursor, the live build card reads `GET /runs/{id}/build/live`
every two seconds, and the Island reads `GET /office/pulse`. Every response
carries the interface's build in an `X-Office-Build` header, which is how an
open page knows to show the update bar.

A step is shown as done when the office has recorded it, so progress reflects
persisted work rather than a claim about it; a running task's plan and step
statuses are written to its record as each step completes, which is what the
Plan tab and the rings read. Agent errors and stop reasons are displayed as
sentences rather than hidden or shown as codes.

The browser is signed in by the launcher through a one-time code in the URL
fragment, or with your sign-in name and password, which becomes an HttpOnly
session cookie (sent only encrypted over Tailscale); writes also need the CSRF
header and a matching origin. Wrong sign-ins and wrong owner tokens are slowed,
and a right one always gets in. `scripts.revoke_sessions` signs every browser
out. That is adequate for a local single-owner deployment and is not a
substitute for an access layer.

## Forge publication

The specification names GitHub. Section 17 requires repository metadata, branch
creation, change-request creation, and URL retrieval, all of which GitLab
provides as merge requests, so the forge is an interface and GitLab is the
implementation. The deviation is recorded in the implementation plan. (Putting
a built website on GitHub Pages is separate: it uses your own GitHub token on
your press, and opens no merge request.)

Publication runs only when `ENABLE_GITHUB_PR` is on and the approval permits a
pull request. It pushes the attempt branch and opens one merge request, and it
looks for an existing merge request first, so a retry after a lost response
reuses it instead of creating a second. Merging is absent rather than guarded:
SPEC section 18 denies it, so the client has no such method.

The approved changes are committed before the push. The runtime edits the
working tree and commits nothing, so an uncommitted change would publish a
branch containing no changes at all. Staging happens only after the diff has
been verified against the approved paths.

```
GITLAB_BASE_URL=https://gitlab.com
GITLAB_PROJECT_PATH=group/project
GITLAB_TOKEN=<project or fine-grained token>
ENABLE_GITHUB_PR=true
```

`ENABLE_GITHUB_PR` is the specification's flag name and governs publication
whichever forge is configured. Enabling it without a project path and token is
refused by configuration validation.

Prefer a token scoped to the single target project. A classic personal access
token carries your access across every group and project you can reach, which
is the wrong blast radius for an automated agent. The token is used only for
the push and the merge request API, never placed in the engineering runtime's
environment, never written to git configuration, and redacted from push errors,
which otherwise echo the remote URL with the credential in it.

```sh
docker compose exec -T api python -m scripts.forge_preflight
```

The preflight makes only the calls the application makes and creates nothing,
so a scope problem is reported before an engineering attempt rather than during
one.

## Hardening

Deterministic probes cover what does not need a provider: context leakage
between runs that share a worker, permission behaviour under graph replay,
state survival through a checkpoint round trip, and each of the twelve provider
failure codes exercised through the graph.

A revision loop stops when it stops making progress: when a rerun reproduces
the previous artifact, or when most of the previous critique's concerns come
back unanswered. Both end the run as `NEEDS_HUMAN_REVIEW` with a reason naming
what happened, instead of spending the whole revision budget first.

```sh
uv run python -m scripts.hardening_runs
```

That submits several deliberately varied objectives, including ones the
evidence base cannot answer, and prints one record per run plus a summary by
failure category. It makes real model and search calls; budget two to four
minutes and a few cents per run.

## Development and verification

The same checks run on GitHub for every push and pull request
(`.github/workflows/ci.yml`): lint and types, the
unit and architecture tests, and the integration tests against PostgreSQL.
Locally:

```sh
uv sync --group browser          # the browser group brings Playwright's types for mypy
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest tests/unit tests/architecture -q --tb=short
uv run pytest tests/integration -q --tb=short
```

Run the unit and integration suites separately: some test files share a name
across the two folders. The tests read settings at import time, so they need a
`.env` with at least `DATABASE_URL` (`python3 -m scripts.init_local` writes
one). Integration suites are skipped unless `TEST_DATABASE_URL` is set, and the
git-dependent suites are skipped where git cannot run. To run the database
suites against the local Compose database without printing its password:

```sh
uv run python -c 'import os, pytest; from app.config import get_settings; os.environ["TEST_DATABASE_URL"] = get_settings().database_url.get_secret_value(); raise SystemExit(pytest.main(["-q", "--tb=short", "tests/integration"]))'
```

Browser tests drive a real browser against a running office and are asked for
by name:

```sh
uv run pytest -m browser
```

They skip themselves when no browser is installed, no office answers, or no
owner token is available. They run on a throwaway office restored from your
newest backup, so they never write into the office you use. They use whatever
Chromium is already on the machine over the DevTools protocol, so they add no
test dependency, and they start it with a mock keychain so macOS never asks
for a password.

Each database suite creates and removes its own UUID-named schema. Together
they verify migration upgrade and downgrade, ORM and schema agreement,
checkpoint resume on a fresh process, claim exclusivity, artifact idempotency,
the API contract, and that a pending approval survives a full application
restart. Use a development database, not production. Short tracebacks avoid
dumping driver locals that can contain credentials.

The container suite additionally runs the git-dependent workspace and
engineering tests:

```sh
docker compose --profile test run --rm tests pytest -q
```

For a host-run API, keep PostgreSQL running and use the host URLs in `.env`:

```sh
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8001
```

The Docker API may stay on port 8000 while host development uses port 8001.
The settings module does not connect to services at import time.

## Where things live

| Module | Owns |
| --- | --- |
| `app/config.py` | Application configuration |
| `app/db/` | Domain persistence |
| `app/gateway/` | Model calls: shared result types, direct provider completion |
| `app/agents/` | Role contracts and one invocation path |
| `app/tools/` | Web search and page fetching |
| `app/browser/` | The office's own browser: the service, the browse agent, its safety checks |
| `app/orchestrator/` | Graph, routing, checkpoints, dispatch, approvals |
| `app/runtimes/` | Bounded execution, workspaces, the Codex adapter, steering, the isolated runner and the installer |
| `app/builds.py`, `app/coding_agents.py`, `app/live_build.py`, `app/hosting.py` | Build folders and versions; Codex or Claude Code; the live build view; Netlify and GitHub Pages |
| `app/api/` | HTTP surface and the run worker |
| `app/api/web/` | The office interface (one file per screen under `office/`) |
| `app/policies/` | The policy engine: named rules at hook points |
| `app/mcp/`, `app/accounts.py` | Tool servers: client, catalog, OAuth sign-in, review, calls, metering; Google and Microsoft accounts |
| `app/skills/` | The curated skill catalog and skills from skills.sh and GitHub |
| `app/channels/` | Slack: conversation, quick lookups, members, notifications, results and progress in threads, thread memory |
| `app/calendars/`, `app/widgets.py` | Calendars (read, sync, sign-ins, edits); the wall of widgets |
| `app/sources/` | Granted folders, the isolated reader, retrieval |
| `app/documents/` | Deliverables as documents, charts, and their Word, Excel, slides, web and Markdown files |
| `app/memory/` | Reviewed project memory and its Agent Skill export |
| `app/rooms.py`, `app/room_replies.py`, `app/room_tools.py` | Project rooms, agents' replies and their tool calls |
| `app/office_pulse.py`, `app/provider_balance.py`, `app/provider_cards.py`, `app/failover.py` | The Island's pulse, limit warnings, provider balances, cards and backups |
| `app/work_performance.py` | Step times, page reading and the benchmark |
| `app/related_work.py`, `app/project_routing.py` | Related work across projects; where a Slack request belongs |
| `scripts/` | Launch, upgrade, backup, restore, package, the calendar helper and operational checks |

Migrations live in `migrations/versions/`. `SCHEMA_REVISION` in `app/db/session.py` must
equal the Alembic head, which a unit test enforces. LangGraph owns its
checkpoint tables and creates them separately, outside Alembic.

The decisions behind this layout are in [`docs/adr/`](docs/adr/README.md).

## Licence

Apache License 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).

"use strict";

// The office explaining itself: how to run it, how it works, and how it is
// doing right now. The health and performance panels read from the running
// office rather than from prose, and the API list is generated from the app's
// own schema, so this page cannot quietly go out of date.

const SECTIONS = [
  ["running", "Running it"],
  ["concepts", "How it works"],
  ["using", "Day to day"],
  ["health", "Health now"],
  ["performance", "Performance"],
  ["api", "API"],
];

const Handbook = { tab: "running", api: null };

const COMMANDS = [
  [
    "Start the office",
    "python3 -m scripts.launch",
    "Checks Docker, disk and ports, starts the services, waits for health, and opens the browser signed in through a one-time code.",
  ],
  [
    "Stop it",
    "docker compose down",
    "Stops every service. Your data stays in its volumes; nothing is deleted.",
  ],
  [
    "Upgrade",
    "python3 -m scripts.upgrade",
    "Stops taking new work, lets what is running reach a step boundary, backs up, rebuilds, migrates, smoke tests, then takes work again. It refuses to continue without a fresh backup.",
  ],
  [
    "Back up",
    "docker compose exec -T api python -m scripts.backup",
    "Writes one private archive of the database and workspace to ./backups. It excludes the credential root key on purpose.",
  ],
  [
    "Prove a backup restores",
    "python3 -m scripts.restore_drill",
    "Restores the newest archive into a throwaway office with its own volumes and ports, compares it against the archive, then destroys it. Never touches this office.",
  ],
  [
    "Restore for real",
    "python3 -m scripts.restore backups/<archive>",
    "Into an empty, migrated office. It refuses if the target holds any data.",
  ],
  [
    "Rotate the credential key",
    "python3 -m scripts.rotate_secret_key",
    "Re-encrypts the stored provider keys under a new root key. Do this if a key may have leaked, after revoking it at the provider.",
  ],
  [
    "Sign every browser out",
    "python3 -m scripts.revoke_sessions",
    "Ends all owner sessions, including this one. You sign in again with the launcher.",
  ],
];

const CONCEPTS = [
  [
    "Project",
    "A work area. It holds its own tasks, knowledge, limits, models and notification channel, and has a room where you talk with the agents. Anything it does not set, it takes from the office.",
  ],
  [
    "Task and run",
    "You give the office an objective; a run is one attempt at it. A run keeps its plan, its evidence, its review and its artifacts, and can be paused and resumed from the step it reached.",
  ],
  [
    "Steps",
    "The Chief plans each task as steps suited to it: one quick look-up for a single fact, a research step per part of a comparison run side by side, or just the writing when the material is in hand. A step that finds nothing makes the Chief replan the rest, at most twice, keeping what was done. The critic then reviews the work; when it asks for more, the Chief adds steps aimed at exactly that. Review and the answer always happen.",
  ],
  [
    "Talk first",
    "Start on My desk by saying anything; no project or form is needed. What you say becomes a discussion, and the office reads what each message commits to: talk is answered, \u201cnote this\u201d files an idea, \u201clook into\u201d or \u201cdo\u201d is answered and offered as work with a Start button, up to $0.50, its result back in the discussion: nothing that spends money starts from what you type, \u201chold off\u201d pauses it, and \u201cnext week\u201d brings the discussion back then. When it is not sure, it asks with one button. A reply is an answer, never new work: answering an agent's question about work in progress passes your answer to that work. Short replies such as \u201cyes\u201d or \u201cthanks\u201d never start anything.",
  ],
  [
    "Agents as a team",
    "Every task has a room where its agents post what they take on, find, doubt, challenge and offer. With the team on (Settings → Models; a project can differ), each step reads its teammates' posts before it writes, and after each wave the critic, the writer and the advisor huddle. A challenge to a finished step runs it again (twice a task at most), an offer becomes a step (twice), and what stays unresolved is stated in the answer. It costs about a third more and gave the better answer when measured.",
  ],
  [
    "Several at once",
    "Up to three tasks run at the same time; the rest wait their turn. Settings → Models → Tasks at once sets how many, 1 to 8. Each keeps its own budget, and a task that reaches the front after the month's limit is spent pauses and says so instead of starting.",
  ],
  [
    "Questions first",
    "When the Chief cannot understand the question without you (\"the six industries above\"), it asks up to three questions and the task waits for your answers before any research. Evidence the office cannot see, such as interviews or internal figures, is not a reason to stop: the answer states it as a limit.",
  ],
  [
    "Agents",
    "The roles are built in, and each has an agent you can edit: its instructions, its model, its skills. Changing one makes a new version; a run records the version it used. The Chief attaches a skill to the plan step it fits, and only that step's worker follows it.",
  ],
  [
    "Approval",
    "You are asked only when an answer proposes something to be done: built, run, published or spent. A research answer does not wait for you. A proposal names what it would do, to what, and with what risk, and the digest it was approved under must still match when it runs.",
  ],
  [
    "Budgets",
    "Each task has a token, cost and time ceiling, and the office and each project have a monthly limit. Each warns once at 80% and once at 100%. Reaching a ceiling pauses the task and asks; it does not throw the work away.",
  ],
  [
    "Rules",
    "Policies decide allow, deny or ask before a model call, a tool call, or on a result. They layer: the project's first, then the office's, and the most restrictive wins.",
  ],
  [
    "Knowledge",
    "Folders you grant to a project. They are read by a service with no network and no credentials, and a run may only retrieve from sources its project was granted.",
  ],
  [
    "Tools",
    "Servers that give your agents a capability the office does not have. Nothing a server offers is usable until you review each tool and grant the server to a project. The researcher uses them to find evidence; the critic, to check a claim. A tool that sends email, deletes outside data, spends money, deploys or merges is never used, whatever you set.",
  ],
  [
    "Memory",
    "What a finished task learned about its sources and its gaps. You review each lesson; only the ones you keep are told to later tasks.",
  ],
  [
    "Related work",
    "When a task starts, the office reads recent finished answers from every project and tells it about up to three that bear on its question: what was asked, what was concluded, and where. They are earlier findings to build on, not fact. If relevance cannot be judged, nothing is added.",
  ],
  [
    "Channels",
    "Slack, if you pair it. The office and each agent can have their own bot. New work asked in Slack goes to the project it belongs to. Only a low-risk approval, one that just records a decision, can be approved there with its button; anything that runs code or reaches outside the office is approved here.",
  ],
];

// How to do things, screen by screen. Every button, tab and screen named here
// must match what is on screen; those live in the other office scripts.
const GUIDES = [
  [
    "Giving work",
    [
      [
        "Start a task",
        "Press ＋ New task on Home, Tasks, the Board or a project. Opened from a task, a project or a room, the form picks that project for you. Leave the title empty and a short one is written from what you ask.",
      ],
      [
        "Where to see the steps",
        "Open the task and choose the Plan tab. It shows the Chief's plan: each step, whether it researches or writes, what it waits for, the skill it uses, and whether it is waiting, done, found nothing or failed. It updates while the task runs.",
      ],
      [
        "How the agents talked",
        "Open the task and choose the Team room tab. Posts are grouped by stage: planning, each wave, the huddle after it, review and the answer, with each reply under the post it answers. Fold a stage by its heading. Below them is your conversation about the task: ask the agents anything there at any time, and while it works choose Tell the team at work or Tell a step from the same box.",
      ],
      [
        "Questions before work starts",
        "If the Chief needs you first, the task says \"The Chief has questions before starting\". Press Answer and continue, write your answers, then Continue with my answers. Leave out anything you would rather not say; the answer will note it. Close the task ends it instead.",
      ],
      [
        "Approval",
        "Only an answer that proposes something to be done asks you. The task then says \"Your approval is needed\"; press Review proposal to read it and decide.",
      ],
      [
        "What this task is told",
        "A card on each task lists what it was told when it began: the task it continues, related work in the office, what it should come to, lessons, sources and the rest. Open one to read it. It shows what this task was given, not what a new one would get today.",
      ],
      [
        "Finished, and not quite right",
        "Rework reopens the same task: say what should change and which part to redo (the research, the strategy, or let the office decide from your note). It writes a new answer and keeps the earlier one. It counts as new work, and a proposal asks for your approval again.",
      ],
      [
        "Finished, and a new question",
        "Ask a follow-up starts a new task that is told what this one concluded and is free to contradict it. Further back, the earlier tasks in the same line are told in a line each, only when they bear on the new question.",
      ],
      [
        "This line of work",
        "A card on each task shows what it built on and what built on it, however far back, what related work it was told about and which later tasks were told about it, and what it taught. Open any of them from there.",
      ],
      [
        "Not finished yet",
        "＋ New task from this opens the new-task form with Build on this task ticked: the new task is told what this one has found so far, and can go in any project. Untick it for a fresh start.",
      ],
    ],
  ],
  [
    "Discussions",
    [
      [
        "Start talking",
        "On the Desk, type in the box and press Enter (or Start). A thread opens and the office answers. It belongs to no project (Free exploration) until you press Change beside its title and choose one; a project's room is its first discussion.",
      ],
      [
        "Hand something off",
        "Say what you want done, such as \u201ccompare these five services\u201d. The agents answer what they can, and the office offers the work; press Start and a card shows it working, with Pause and Open the work. It may spend up to $0.50 before it asks you. Its result comes back into the discussion; say what should change and the same work is redone.",
      ],
      [
        "Bring someone in",
        "Press Bring someone in beside the discussion's title, or name a colleague with @. They join, are given the discussion's brief rather than its whole history, and answer when named. \u201c@Mia compare \u2026\u201d is Mia's to do.",
      ],
      [
        "Coming back, and branching",
        "A discussion that has gone quiet gets a three-line brief: where we were, what is decided, what is open. Opening it after a while starts with \u201cSince you were last here\u201d. To split off a tangent, hover a message and press Branch to a new topic.",
      ],
      [
        "What My desk shows",
        "Needs you holds only decisions that are yours, each with where it came from; answer a rule or a limit right there. Delivered shows this week's results. Ideas holds the thoughts you kept (on the floor, with i anywhere, or with \u201cNote this\u201d): plan each for a day and it comes back under Today, or Start it. Parked holds discussions you put off, which come back on their day.",
      ],
      [
        "Beside the discussion",
        "A panel beside each discussion shows what it left behind: ideas, what is decided, what is open, and its work. Press Open beside on delivered work to read the document there. Select a passage, say what is wrong, and press Revise this part: that passage is rewritten into a new version, and the earlier one stays. If it needs new evidence, you are offered Redo it with research.",
      ],
      [
        "Decisions",
        "A decision comes only from you. On your own message press Record as decision; every agent in that project follows it from then on. It can replace an earlier one, which stays as history. Each business line's space, from Business spaces, shows its decisions, goals, discussions, projects and materials.",
      ],
      [
        "Finding your way back",
        "Every page below My desk, Discussions, Work, Business spaces and Office settings has Back at the top left: it returns to the page you came from, or up a level when you opened the page directly. The trail beside it names each level above, and each is a link. Your browser's own back button works too. Prefer a side menu to the bar along the top? Choose it under Office settings \u2192 The menu, or press \u25e7 Side menu in the bar.",
      ],
      [
        "From your phone",
        "Office settings \u2192 Mobile & remote access. Home network works while the phone is on your Wi-Fi: run python3 -m scripts.access home once on this Mac, then Check and turn on. Remote access works anywhere through your private Tailscale network: Connect to Tailscale, approve quarterdeck, then Turn on remote access. Either way press Connect your phone and scan the code; it signs the phone in once. The office is never put on the public internet, and the Mac must stay on and awake.",
      ],
      [
        "Ideas, pauses and later",
        "\u201cNote this\u201d files an idea and starts nothing. \u201cHold off\u201d pauses the discussion's work, keeping what it did. \u201cLet's look again next week\u201d brings the discussion back on that day.",
      ],
    ],
  ],
  [
    "Project rooms",
    [
      [
        "Talk with the team",
        "Every project has a room, under Rooms in the sidebar. Your messages sit on the right, the agents' on the left. Ask a question and the Chief answers; name an agent with @ and that agent answers. Name up to six and they answer in turn, each reading the others first; say everyone (or @everyone) and every agent answers, the Chief first. Each reply shows what it cost.",
      ],
      [
        "Reply, and threads",
        "Hover a message (on a phone the buttons are always there). Reply quotes it in your message, and an agent you reply to answers. Reply in thread opens a panel with the message and its replies; the conversation shows how many replies it has, and the agent already in the thread answers unless you name another.",
      ],
      [
        "Huddles in the room",
        "Each huddle of a project's tasks is one line in its room: who raised what, what the Chief did and the first challenge, in the agents' own words. Read it opens the task's Team room. A question asked from a Team room shows here too, marked with its task.",
      ],
      [
        "A room of its own",
        "＋ New room, on the Rooms page or under Rooms in the sidebar, makes a project and opens its empty room without starting a task. A task links to its room with Open the room.",
      ],
      [
        "Typing decides nothing",
        "An agent that thinks new work is needed offers ＋ Start as a task, which opens the form for you to reword and start. While a task runs, a message can be sent to it as direction, and the room shows whether it was taken in.",
      ],
      [
        "Waiting for you, and Documents",
        "What the project's tasks are waiting on sits at the bottom of the room: allow or refuse, raise a limit or stop the task there. The Documents tab keeps what the tasks came to, to read here or take away as a file.",
      ],
    ],
  ],
  [
    "Tools",
    [
      [
        "The catalog",
        "On the Tools screen, Browse the catalog lists tool servers suited to office work, each run by the vendor and checked by hand. Add fills in the form for you. ＋ Add server takes any MCP server by its HTTPS address.",
      ],
      [
        "Sign in with your account",
        "Some servers sign in with your account rather than a key. Once added, the card says Needs sign-in; press Sign in to go to the vendor's own page and come back to Tools. The office never sees your password. When a sign-in lapses, press Sign in again; until then tasks go on without that server.",
      ],
      [
        "Review each tool",
        "Review tools shows what each tool says it does, what it takes, and what the server claims about itself. Choose Allowed, Asks me first (the task pauses for your yes) or Refused, and tick what it does. A tool you say sends email, spends money and the like is held back whatever you choose.",
      ],
      [
        "Prices for paid tools",
        "If the vendor bills you per call, give the tool a price when you review it: the most one call costs. Every call counts at that price toward the task's cost limit and the monthly limits. A call that would pass the task's limit is not made and the task asks you.",
      ],
      [
        "Which projects may use it",
        "Manage access grants a server to projects. A tool is usable only once you have allowed it and its server is granted to the task's project.",
      ],
    ],
  ],
  [
    "Skills",
    [
      [
        "Add from skills.sh",
        "On the Skills screen, press Add from skills.sh and paste a skills.sh link, a GitHub link or owner/repo skill-name, then Read it. Nothing is installed yet.",
      ],
      [
        "What the preview shows",
        "Its name and description, where it came from and the exact version it was pinned at, the security audits skills.sh publishes, any code it carries (never installed: skills here are instructions only), what it assumes the office cannot do, and its full instructions.",
      ],
      [
        "Which role uses it",
        "Choose it under Used by: the researcher, the writer, the strategist or the critic. Then Install this version. The Chief attaches it to a plan step it fits, and the Plan tab shows which.",
      ],
    ],
  ],
  [
    "Keeping an eye on it",
    [
      [
        "The Island",
        "The bar at the top of every page (at the bottom on a phone) says what the office is doing: Ready when idle, or the task and its step while work runs. Click it while work runs or waits to see every task, the month's spend and what is left with each provider.",
      ],
      [
        "Rings and amber",
        "Each running task has a ring for how far it has come through its steps and one for how much of its budget it has used; the month has one against your limit. The Island turns amber while anything waits on you.",
      ],
      [
        "Messages and sound",
        "It springs open to say when a task finishes or starts waiting, only when that changes. A soft chime is off by default: open the Island and press Sound off to turn it on.",
      ],
      [
        "80% and 100%",
        "The office's month, each project's month and each task's budget warn once at 80% and once at 100%, as does a provider account running low or out.",
      ],
      [
        "Settings → Models",
        "What's left with each provider reads the balance or key limit with each connection's own key, where the provider publishes it. An empty account is named as such.",
      ],
      [
        "Settings → Models → Tasks at once",
        "How many tasks run at the same time, and how many run and wait now. A change applies to the next task to start.",
      ],
      [
        "Settings → Performance",
        "How each model is doing: how often it answered, typical and slow response times, and its speed, over the last 30 days. A failing model shows here first.",
      ],
      [
        "Reduce motion",
        "Turn on Reduce motion in your computer's or phone's accessibility settings and the office stops animating: everything lands at once.",
      ],
    ],
  ],
  [
    "Slack",
    [
      [
        "Where questions go",
        "The office reads what each project is about before choosing one. A clear match is offered; a likely one is proposed: reply yes to start it there, or new for a new project. Work asked in a thread that already started a task goes to that task's project. Reply \"call it <name>\" to put it in any other project.",
      ],
    ],
  ],
];

function guidesPanel() {
  return GUIDES.map(
    ([group, items]) =>
      `<h2 class="spaced">${esc(group)}</h2><div class="grid2">${items
        .map(([name, text]) => `<div class="card"><h3>${esc(name)}</h3><p class="muted">${esc(text)}</p></div>`)
        .join("")}</div>`
  ).join("");
}

function seconds(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (n < 90) return `${n.toFixed(n < 10 ? 1 : 0)}s`;
  if (n < 5400) return `${Math.round(n / 60)} min`;
  return `${Math.round(n / 3600)} h`;
}

function serviceRow(name, part) {
  const state = part && typeof part === "object" ? part.state : part;
  const ok = state === "ok" || state === "present" || state === true;
  const seen = part && part.last_seen_seconds !== undefined ? ` · seen ${seconds(part.last_seen_seconds)} ago` : "";
  return `<div class="detail-line"><span>${esc(name)}</span><strong>${tag(ok ? "ok" : String(state ?? "unknown"), ok ? "" : "amber")}<span class="muted">${esc(seen)}</span></strong></div>`;
}

function healthPanel(status) {
  const gb = (bytes) => `${(Number(bytes || 0) / 1024 ** 3).toFixed(0)} GB`;
  const backup = status.backups?.latest_age_seconds;
  return `<div class="grid2">
    <div class="card"><h2>Services</h2>
      ${serviceRow("Database", status.database)}
      ${serviceRow("Role models", status.models)}
      ${serviceRow("Isolated runner", status.runner)}
      ${serviceRow("Knowledge reader", status.sources_reader)}
      ${serviceRow("Page reader", status.page_reader)}
      ${serviceRow("Credential store", { state: status.credential_store?.root_key })}
      <div class="detail-line"><span>Waiting to run</span><strong>${esc(String(status.worker?.queued ?? 0))}</strong></div>
    </div>
    <div class="card"><h2>This installation</h2>
      <div class="detail-line"><span>Version</span><strong>${esc(status.version || "—")}</strong></div>
      <div class="detail-line"><span>Database revision</span><strong>${esc(status.database?.revision || "—")}</strong></div>
      <div class="detail-line"><span>Judgments</span><strong>${esc(status.judgments || "off")}</strong></div>
      <div class="detail-line"><span>Free disk</span><strong>${esc(gb(status.storage?.free_bytes))}</strong></div>
      <div class="detail-line"><span>Newest backup</span><strong>${backup === null || backup === undefined ? tag("none yet", "amber") : `${seconds(backup)} old`}</strong></div>
      <div class="detail-line"><span>Code execution</span><strong>${status.code_execution ? tag("on", "amber") : "off"}</strong></div>
      <div class="detail-line"><span>Publication</span><strong>${status.publication ? tag("on", "amber") : "off"}</strong></div>
    </div>
  </div>`;
}

function performancePanel(usage) {
  const p = usage.performance || {};
  const t = usage.totals || {};
  const failed = Number(p.failures || 0);
  const calls = Number(p.calls || 0);
  const rows = (usage.by_role || [])
    .map(
      (row) =>
        `<div class="detail-line"><span>${esc(ROLE_LABEL[row.key] || row.key)}</span><strong>${row.calls} calls · ${seconds(row.avg_seconds)} average · ${row.failures || 0} failed</strong></div>`
    )
    .join("");
  return `<div class="grid2">
    <div class="card"><h2>Model calls, last ${esc(String(usage.days))} days</h2>
      <div class="detail-line"><span>Calls</span><strong>${calls}</strong></div>
      <div class="detail-line"><span>Failed</span><strong>${failed ? tag(`${failed} (${Math.round((failed / Math.max(calls, 1)) * 100)}%)`, "amber") : "0"}</strong></div>
      <div class="detail-line"><span>Average</span><strong>${seconds(p.avg_seconds)}</strong></div>
      <div class="detail-line"><span>Slowest 5%</span><strong>${seconds(p.p95_seconds)}</strong></div>
      <div class="detail-line"><span>Slowest call</span><strong>${seconds(p.slowest_seconds)}</strong></div>
      <div class="detail-line"><span>Waiting now</span><strong>${esc(String(p.queued_now ?? 0))}</strong></div>
    </div>
    <div class="card"><h2>What it cost</h2>
      <div class="detail-line"><span>Tokens</span><strong>${Number(t.tokens || 0).toLocaleString()}</strong></div>
      <div class="detail-line"><span>Spent</span><strong>${money(t.cost_usd || 0)}${t.cost_complete === false ? ` <span class="muted">at least</span>` : ""}</strong></div>
      <div class="detail-line"><span>Models connected</span><strong>${esc(String(usage.models_configured ?? 0))}</strong></div>
      <h3>By role</h3>
      ${rows || `<p class="muted">No calls in this window.</p>`}
    </div>
  </div>`;
}

function apiPanel(spec) {
  if (!spec) return emptyCard("The API description could not be read.", "It is served at /openapi.json.");
  const groups = {};
  Object.entries(spec.paths || {}).forEach(([path, methods]) => {
    const group = path.split("/")[1] || "root";
    Object.entries(methods).forEach(([method, detail]) => {
      if (!["get", "post", "put", "delete", "patch"].includes(method)) return;
      (groups[group] = groups[group] || []).push({
        method: method.toUpperCase(),
        path,
        summary: (detail.summary || detail.description || "").split("\n")[0],
      });
    });
  });
  const total = Object.values(groups).reduce((sum, rows) => sum + rows.length, 0);
  const blocks = Object.keys(groups)
    .sort()
    .map(
      (group) => `<div class="card"><h3>/${esc(group)}</h3>${groups[group]
        .map(
          (row) =>
            `<div class="detail-line"><span><code>${esc(row.method)} ${esc(row.path)}</code>${row.summary ? `<br><small class="muted">${esc(row.summary)}</small>` : ""}</span></div>`
        )
        .join("")}</div>`
    )
    .join("");
  return `<p class="muted">${total} routes, read from the office's own description at <code>/openapi.json</code>. Every one needs your owner session or token.</p>
    <div class="grid2">${blocks}</div>`;
}

async function renderHandbook(content) {
  const tabs = SECTIONS.map(
    ([id, label]) =>
      `<button type="button" role="tab" aria-selected="${Handbook.tab === id}" class="${Handbook.tab === id ? "active" : ""}" data-action="handbook-tab:${id}">${esc(label)}</button>`
  ).join("");

  let body = "";
  if (Handbook.tab === "running") {
    body = `<p class="muted">Every command runs on this computer, from the folder holding the office.</p>
      ${COMMANDS.map(
        ([name, command, note]) =>
          `<div class="card"><h3>${esc(name)}</h3><pre class="artifact-text">${esc(command)}</pre><p class="muted">${esc(note)}</p></div>`
      ).join("")}
      <div class="card"><h3>Where things live</h3>
        <div class="detail-line"><span>Your data</span><strong>Docker volumes on this machine</strong></div>
        <div class="detail-line"><span>Backups</span><strong><code>./backups</code></strong></div>
        <div class="detail-line"><span>Credential root key</span><strong><code>.secrets/root.key</code>, outside every backup</strong></div>
        <div class="detail-line"><span>Settings you can edit here</span><strong>Settings → office and per project</strong></div>
      </div>`;
  } else if (Handbook.tab === "concepts") {
    body = `<div class="grid2">${CONCEPTS.map(
      ([name, text]) => `<div class="card"><h3>${esc(name)}</h3><p class="muted">${esc(text)}</p></div>`
    ).join("")}</div>`;
  } else if (Handbook.tab === "using") {
    body = guidesPanel();
  } else if (Handbook.tab === "health") {
    const status = await api("/status");
    body = healthPanel(status);
  } else if (Handbook.tab === "performance") {
    const usage = await api("/usage/summary?days=30");
    body = performancePanel(usage);
  } else {
    if (!Handbook.api) Handbook.api = await api("/openapi.json").catch(() => null);
    body = apiPanel(Handbook.api);
  }

  content.innerHTML =
    heading(
      "HANDBOOK",
      "How this office runs",
      "What to type, what the parts are, where to find things, and how it is doing right now. The last three read from the running office, not from prose."
    ) + `<div class="subnav" role="tablist">${tabs}</div>${body}`;
}

Office.screens.handbook = { title: "Handbook", render: renderHandbook, refresh: 20000 };

Office.actions = Object.assign(Office.actions || {}, {
  "handbook-tab": (id) => {
    Handbook.tab = id;
    rerender();
  },
});

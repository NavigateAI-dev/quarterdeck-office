"use strict";

// Settings: office health, projects and their data, repeatable work, and a
// plain-text way to ask the office something.

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const Settings = { templates: [], projects: [], tab: "office", days: 30 };

function statusRows(status) {
  const age = (seconds) => (seconds === null || seconds === undefined ? "never" : seconds < 7200 ? `${Math.round(seconds / 60)} min ago` : `${Math.round(seconds / 3600)} h ago`);
  const state = (part) => (part?.state === "ok" ? "Ready" : part?.state || "unknown");
  return [
    ["Database", `${state(status.database)} · schema ${status.database.revision}`],
    ["Role models", status.models?.state === "ok" ? "Every role has one" : `None yet for ${(status.models?.missing || []).join(", ") || "some roles"}`],
    ["Test runner", `${state(status.runner)}${status.runner.last_seen_seconds === null || status.runner.last_seen_seconds === undefined ? "" : ` · seen ${status.runner.last_seen_seconds}s ago`}`],
    ["Document reader", state(status.sources_reader)],
    ["Queued tasks", String(status.worker.queued)],
    ["Free disk", `${(status.storage.free_bytes / 1024 ** 3).toFixed(1)} GB`],
    ["Last backup", age(status.backups.latest_age_seconds)],
    ["Credential key", status.credential_store.root_key === "present" ? "Present on this computer" : "Missing"],
    ["Judgments", status.judgments === "typesafe" ? "TypeSafe" : "Built-in rules only"],
    ["Code execution", status.code_execution ? "Enabled (with your approval)" : "Disabled"],
    ["Publishing", status.publication ? "Enabled (with your approval)" : "Disabled"],
  ];
}

const SETTINGS_TABS = [
  ["office", "Office"],
  ["models", "Models"],
  ["reach", "Reach"],
  ["budget", "Budget & limits"],
  ["usage", "Token use"],
  ["performance", "Performance"],
];

function number(value, suffix = "") {
  return value === null || value === undefined ? "not set" : `${Number(value).toLocaleString()}${suffix}`;
}
function budgetView(data, usage) {
  const s = data.settings;
  const c = data.configured;
  const limit = s.monthly_cost_limit_usd;
  const spent = data.spent_this_month_usd;
  const share = limit ? Math.min(100, Math.round((spent / limit) * 100)) : 0;
  return `<div class="grid2">
    <div class="card"><h2>What one task may spend</h2>
      <p class="muted">Applies to new tasks. A task already running keeps the budget it started with. Empty means the value from configuration.</p>
      <form id="budget-form">
        ${field("Tokens per task", "run_max_tokens", { type: "number", required: false, value: s.run_max_tokens ?? "", attrs: 'min="1" max="50000000"', hint: `Configuration: ${number(c.run_max_tokens)}` })}
        ${field("Cost per task (USD)", "run_max_cost_usd", { type: "number", required: false, value: s.run_max_cost_usd ?? "", attrs: 'min="0.01" max="10000" step="0.01"', hint: c.run_max_cost_usd ? `Configuration: $${c.run_max_cost_usd}` : "Configuration: no cost cap" })}
        ${field("Minutes per task", "run_max_minutes", { type: "number", required: false, value: s.run_max_minutes ?? "", attrs: 'min="1" max="1440"', hint: `Configuration: ${number(c.run_max_minutes, " min")}` })}
        ${field("Chat tokens per bot per day", "chat_daily_max_tokens", { type: "number", required: false, value: s.chat_daily_max_tokens ?? "", attrs: 'min="0" max="5000000"', hint: `Configuration: ${number(c.chat_daily_max_tokens)}` })}
        <div class="button-row"><button class="primary">Save</button></div>
      </form>
    </div>
    <div class="card"><h2>This month</h2>
      <div class="metric"><label>Spent so far</label><strong>${money(spent)}</strong><span>${usage.totals.cost_complete ? "all calls priced" : "some providers report no price"}</span></div>
      <form id="limit-form">
        ${field("Monthly limit (USD)", "monthly_cost_limit_usd", { type: "number", required: false, value: limit ?? "", attrs: 'min="0.01" max="100000" step="0.01"', hint: "Empty means no limit. At the limit, new tasks are refused and scheduled tasks skip; work already running finishes." })}
        <div class="button-row"><button class="primary">Save limit</button></div>
      </form>
      ${limit ? `<div class="meter"><span style="width:${share}%"></span></div><p class="muted">${share}% of ${money(limit)} used.</p>` : ""}
    </div></div>`;
}


// How many tasks run at the same time (MW), and how many do now.
function routingCard(office) {
  // Each task on the cheapest tested model likely to do it well.
  const automatic = (office?.settings?.model_routing || "auto") === "auto";
  return `<div class="card advisor-card" data-routing-card><h2>Model choice ${automatic ? tag("automatic") : tag("fixed", "gray")}</h2>
      <p class="muted">Automatic: when a task starts, the office reads its request and decides how strong a model it needs. A hard task keeps every role on its own model; for an easier one, roles on a much dearer model move to your cheapest tested model that is enough (a quick lookup), or a middle one (ordinary research). Nothing is ever moved up. The task's room says what moved and why. A model you pin to a role in a project, or to an agent, always stays. Fixed: every task uses the models chosen per role below.</p>
      <div class="button-row">${btn(automatic ? "Use fixed models" : "Choose automatically", `routing-switch:${automatic ? "fixed" : "auto"}`, automatic ? "" : "primary")}</div>
    </div>`;
}

function runsAtOnceCard(office) {
  const places = Number(office.settings?.runs_at_once) || Number(office.configured?.runs_at_once) || 3;
  const running = office.worker?.running ?? 0;
  const waiting = office.worker?.waiting ?? 0;
  const choices = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => [String(n), n === 1 ? "1 task (one after another)" : `${n} tasks`]);
  return `<div class="card advisor-card" data-runs-card><h2>Tasks at once</h2>
      <p class="muted" data-runs-now>Running ${esc(running)} of ${esc(places)}${waiting ? ` · ${esc(waiting)} waiting for a place` : ""}.</p>
      <p class="muted">How many tasks work at the same time; the rest wait their turn. More at once finishes a queue sooner and spends sooner: each task keeps its own budget, and a task that reaches the front after the month's limit is spent pauses instead of starting. A change applies to the next task to start.</p>
      <form id="runs-at-once-form">${field("Tasks at once", "runs_at_once", { type: "select", value: String(places), choices })}
        <div class="button-row"><button class="primary">Save</button></div></form>
    </div>`;
}

// The office's own browser: off until the owner turns it on.
function browserSwitchCard(office) {
  const on = Boolean(office.settings.browser_on);
  const running = Boolean(office.browser_running);
  const state = on ? (running ? tag("on") : tag("on, not running", "amber")) : tag("off", "gray");
  return `<div class="card advisor-card" data-browser-card><h2>The office's own browser ${state}</h2>
      <p class="muted">A sandboxed browser your agents can use for what reading pages cannot do: a site's own search, going through pages, a form that shows a price or availability. It runs apart from the office, with none of your logins or files, on the public web only. Anything that signs in, submits, sends, pays or books stops and asks you first, and every step is kept on the task with its screenshot. Each step costs a model call, counted against the task's budget.</p>
      ${on && !running ? `<p class="muted">The browser service is not running. It starts with the office after the next upgrade.</p>` : ""}
      <div class="button-row">${btn(on ? "Turn off" : "Turn on", `browser-switch:${on ? "off" : "on"}`, on ? "" : "primary")}</div>
    </div>`;
}

// The coder on the owner's Mac (Stage 36): their own CLI, their own sign-in.
function coderCard(coder) {
  if (!coder) return "<h2>Coder on this Mac</h2><p class=\"muted\">Could not be read.</p>";
  const state = coder.connected ? tag("connected") : coder.seen_at ? tag("away", "amber") : tag("not started", "gray");
  const rows = (coder.clis || [])
    .map((c) => {
      const ready = c.signed_in ? tag("signed in") : tag(c.detail ? "needs you" : "found", "amber");
      return `<li class="coding-row"><strong>${esc(c.label || c.id)}</strong> ${esc(c.version || "")} ${ready}${c.detail ? ` <span class="muted">${esc(c.detail)}</span>` : ""}</li>`;
    })
    .join("");
  const seen = coder.seen_at ? `Last heard from ${esc(new Date(coder.seen_at).toLocaleString())}.` : "";
  return `<h2>Coder on this Mac ${state}</h2>
    <p class="muted">The office does no coding itself: a small helper on your Mac runs your own coding CLI (Claude Code, Codex, opencode or DeepSeek Harness), signed in with your own plan and inside its own sandbox, in a separate worktree on an office/ branch under ${esc(coder.root || "~/dev")}. The office says what to do and where, shows its steps, and reviews what it made. It never pushes, and nothing reaches your own branches without your press.</p>
    ${coder.connected ? "" : `<p class="muted">${seen} Start it from the office's folder in your terminal:</p><pre class="command">${esc(coder.start)}</pre>`}
    ${rows ? `<ul class="plain coding-agents">${rows}</ul>` : coder.connected ? `<p class="muted">It found no coding CLI on this Mac.</p>` : ""}
    <div class="button-row">${btn("Look again", "coder-refresh")}</div>`;
}

function fillCoder(content) {
  const card = content?.querySelector("#coder-card") || document.getElementById("coder-card");
  if (card) card.innerHTML = coderCard(Settings.coder);
}

// Which coding CLI on the owner's Mac does the office's coding (Stage 36).
function codingAgentCard(coding) {
  if (!coding) return "";
  const rows = (coding.agents || [])
    .filter((a) => a.installed || coding.agent === a.id)
    .map((a) => {
      const chosen = coding.agent === a.id;
      const state = !a.installed
        ? tag("not on your Mac", "amber")
        : !a.runs
          ? tag("not run yet", "gray")
          : a.ready
            ? tag(chosen ? "codes" : "ready")
            : tag("sign in to it", "amber");
      const use = a.installed && a.ready && a.runs && !chosen ? btn("Use this", `coding-agent:${a.id}`) : "";
      return `<li class="coding-row"><strong>${esc(a.label)}</strong> ${state} ${use}</li>`;
    })
    .join("");
  return `<h2>Which CLI codes</h2>
    <p class="muted">When you approve coding, Quarterdeck Coder runs this CLI on your Mac, with your own sign-in and plan, in its own sandbox. The office checks what it made before anything is kept. Only CLIs the helper found are listed.</p>
    ${coding.code_execution ? "" : `<p class="muted"><strong>Code execution is off for this office</strong>, so nothing is built until it is turned on (ENABLE_CODE_EXECUTION).</p>`}
    ${rows ? `<ul class="plain coding-agents">${rows}</ul>` : `<p class="muted">${coding.helper_connected ? "The helper found no coding CLI on your Mac." : "Start Quarterdeck Coder to see the CLIs on your Mac."}</p>`}
    <h3>Packages ${coding.packages ? tag("allowed") : tag("not allowed", "gray")}</h3>
    <p class="muted">Allowed, a build may list packages (npm, or pip wheels) and the office installs them in a separate installer that has network but no keys, with the packages' own install scripts switched off, before it checks the build. Off, builds use plain files only.${coding.packages && !coding.installer_running ? " <strong>The installer is not running</strong>; it starts with the office after the next upgrade." : ""}</p>
    <div class="button-row">${btn(coding.packages ? "Don't allow packages" : "Allow packages", `build-packages:${coding.packages ? "off" : "on"}`, coding.packages ? "" : "primary")}</div>`;
}

function fillCodingAgent(content) {
  // Within this render's own content: the screen may still be off the page.
  const card = content?.querySelector("#coding-agent-card") || document.getElementById("coding-agent-card");
  if (card) card.innerHTML = codingAgentCard(Settings.coding) || "<h2>Which CLI codes</h2><p class=\"muted\">Could not be read.</p>";
}

Office.actions = Object.assign(Office.actions || {}, {
  "coder-refresh": async () => {
    Settings.coder = await api("/coder").catch(() => null);
    fillCoder();
  },
  "build-packages": async (choice) => {
    const saved = await attempt(() => api("/settings/coding-agent", { method: "PUT", body: { packages: choice === "on" } }), choice === "on" ? "New builds may install packages." : "New builds use plain files only.");
    if (saved) {
      Settings.coding = saved;
      fillCodingAgent();
    }
  },
  "coding-agent": async (agent) => {
    const saved = await attempt(() => api("/settings/coding-agent", { method: "PUT", body: { agent } }), "Saved. New builds use it.");
    if (saved) {
      Settings.coding = saved;
      fillCodingAgent();
    }
  },
});

function roleModelsView(office, models) {
  const onCall = Boolean(office.settings.advisor_on);
  const asTeam = Boolean(office.settings.team_on);
  // The providers first: who uses what, what each has left, their backups.
  return `${providersView(Office.providers, models, office)}<div class="card advisor-card" data-team-card><h2>Agents as a team ${asTeam ? tag("on") : tag("off", "gray")}</h2>
      <p class="muted">Every task has a room where its agents post what they take on, find, doubt and challenge; you can read it on the task's Team room tab. With the team on, each step reads its teammates' posts before it writes, and after each wave the critic and the writer (and the advisor, when on call) react at once, so their feedback shapes the next steps instead of arriving at the end. The huddles are a few short model calls each, counted against the task's budget. A project can turn it on or off for itself.</p>
      <div class="button-row">${btn(asTeam ? "Turn off" : "Turn on", `team-switch:${asTeam ? "off" : "on"}`, asTeam ? "" : "primary")}</div>
    </div>
    ${routingCard(office)}
    ${runsAtOnceCard(office)}
    <div class="card advisor-card" data-related-card><h2>Related work across business lines ${office.settings.related_across_lines ? tag("allowed") : tag("kept apart", "gray")}</h2>
      <p class="muted">When a task starts, the office tells it about finished work that bears on its question. Kept apart, that is only work from the same business line, so one line's client material is not mixed into another's. Allow it to use any line's work.</p>
      <div class="button-row">${btn(office.settings.related_across_lines ? "Keep them apart" : "Allow across lines", `related-switch:${office.settings.related_across_lines ? "off" : "on"}`, "")}</div>
    </div>
    ${browserSwitchCard(office)}
    <div class="card advisor-card" id="coder-card" data-coder-card>${coderCard(Settings.coder)}</div>
    <div class="card advisor-card" id="coding-agent-card" data-coding-card><h2>Which CLI codes</h2><p class="muted">Reading…</p></div>
    <div class="card advisor-card" data-advisor-card><h2>Advisor on call ${onCall ? tag("on") : tag("off", "gray")}</h2>
      <p class="muted">A stronger model that reads the whole task at three moments: before the plan runs, when the same failure repeats, and before the work is declared done. It advises; the Chief decides and writes. It stays silent on every other turn, and what it reads counts against the task's budget. Choose its model below; without one it uses the Chief's. A project can turn it on or off for itself.</p>
      <div class="button-row">${btn(onCall ? "Turn off" : "Turn on", `advisor-switch:${onCall ? "off" : "on"}`, onCall ? "" : "primary")}</div>
    </div>
`;
}

function usageView(usage) {
  const rows = (items) =>
    items.length
      ? items.map((r) => `<div class="detail-line"><span>${esc(r.key)}<br><small class="muted">${r.calls} calls${r.failures ? ` · ${r.failures} failed` : ""}</small></span><strong>${Number(r.tokens).toLocaleString()} tokens<br><small class="muted">${money(r.cost_usd)}</small></strong></div>`).join("")
      : `<p class="muted">Nothing yet in this window.</p>`;
  const days = usage.by_day.slice(-14);
  const peak = Math.max(1, ...days.map((d) => d.tokens));
  return `<div class="grid2">
    <div class="card"><h2>Last ${usage.days} days</h2>
      <div class="metric"><label>Tokens</label><strong>${Number(usage.totals.tokens).toLocaleString()}</strong><span>${usage.totals.calls} model calls</span></div>
      <div class="metric"><label>Cost</label><strong>${money(usage.totals.cost_usd)}</strong><span>${usage.totals.cost_complete ? "complete" : "lower bound; some calls unpriced"}</span></div>
      <div class="bars">${days.map((d) => `<span title="${esc(d.day)}: ${d.tokens} tokens" style="height:${Math.max(3, Math.round((d.tokens / peak) * 100))}%"></span>`).join("")}</div>
      <p class="muted">Daily tokens${days.length ? ` · ${esc(days[0].day)} to ${esc(days[days.length - 1].day)}` : ""}. Chat used ${Number(usage.chat.tokens).toLocaleString()} tokens (${money(usage.chat.cost_usd)}).</p>
    </div>
    <div class="card"><h2>By role</h2>${rows(usage.by_role)}<h3 class="spaced">By model</h3>${rows(usage.by_model)}</div>
  </div>`;
}

// How each model is doing: reliability as a ring, then how fast and how slow.
function modelHealthCards(usage) {
  const rows = usage.model_health || [];
  if (!rows.length) return `<p class="muted">No model calls in this window yet.</p>`;
  return `<div class="model-health">${rows
    .map((m) => {
      const speed = m.tokens_per_second == null ? "speed unknown" : `${m.tokens_per_second} tokens/s`;
      const failed = m.failures ? `${m.failures} failed${m.common_failure ? `, mostly ${String(m.common_failure).replace(/_/g, " ")}` : ""}` : "none failed";
      return `<div class="health-row">${Island.ring(`health:${m.model}`, m.reliability, m.reliability < 0.9 ? "near" : "", 34)}<div><strong>${esc(m.model)}</strong><small>${esc(Math.round(m.reliability * 100))}% answered · ${esc(m.calls)} calls · ${esc(failed)}</small></div><div class="health-times"><span>${esc(m.p50_seconds)}s <small>typical</small></span><span>${esc(m.p95_seconds)}s <small>slow</small></span><span>${esc(speed)}</span></div></div>`;
    })
    .join("")}</div>`;
}

function performanceView(usage, status) {
  const p = usage.performance;
  const state = (part) => (part?.state === "ok" ? "Ready" : part?.state || "unknown");
  if (typeof leadQualityDraw === "function") setTimeout(leadQualityDraw, 0);
  return `<div id="lead-quality" class="card spaced lead-quality" aria-label="The lead on real use"><h2>The lead on real use</h2><p class="muted">Reading…</p></div>
    <div class="grid2">
    <div class="card"><h2>Model calls</h2>
      <div class="detail-line"><span>Calls (last ${usage.days} days)</span><strong>${p.calls}</strong></div>
      <div class="detail-line"><span>Failed</span><strong>${p.failures}${p.calls ? ` · ${Math.round((p.failures / p.calls) * 100)}%` : ""}</strong></div>
      <div class="detail-line"><span>Average time</span><strong>${p.avg_seconds}s</strong></div>
      <div class="detail-line"><span>Slowest 5%</span><strong>over ${p.p95_seconds}s</strong></div>
      <div class="detail-line"><span>Slowest call</span><strong>${p.slowest_seconds}s</strong></div>
      <div class="detail-line"><span>Waiting to run now</span><strong>${p.queued_now}</strong></div>
      <p class="muted">A slow or failing model shows here first. Retries are counted as part of their call.</p>
    </div>
    <div class="card"><h2>Services</h2>${statusRows(status).map(([k, v]) => `<div class="detail-line"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join("")}
      <p class="muted">Backups: <code>python3 -m scripts.backup</code> · restore: <code>python3 -m scripts.restore</code>.</p>
    </div></div>
    ${workCards(Settings.work)}
    <div class="card spaced"><h2>How each model is doing</h2>
      <p class="muted">Last ${esc(usage.days)} days. "Typical" is the middle call, "slow" the slowest one in twenty; speed is output tokens a second while answering.</p>
      ${modelHealthCards(usage)}</div>`;
}

// Steps, page reading, failures and the benchmark.
const READ_WAY = { direct: "Read directly", browser: "Opened in the office's browser", reader: "Through the reader service", failed: "Could not be read", feed: "Feeds", github: "GitHub", youtube: "YouTube" };
function stepTime(value) {
  const n = Number(value || 0);
  return n >= 90 ? `${Math.round(n / 60)} min` : `${n}s`;
}
function workCards(work) {
  if (!work) return `<div class="card spaced" data-work><p class="muted">The office's own records could not be read just now.</p></div>`;
  const steps = (work.steps || []).length
    ? `<table class="plain-table"><thead><tr><th>Step</th><th>Times</th><th>Typical</th><th>Slow (1 in 10)</th></tr></thead><tbody>${work.steps.map((s) => `<tr><td>${esc(s.label)}</td><td>${esc(s.count)}</td><td>${esc(stepTime(s.typical_seconds))}</td><td>${esc(stepTime(s.slow_seconds))}</td></tr>`).join("")}</tbody></table>`
    : `<p class="muted">No steps in this window yet.</p>`;
  const pages = work.pages || {};
  const share = pages.count ? Math.round((pages.read / pages.count) * 100) : null;
  const ways = (pages.ways || []).length
    ? `<table class="plain-table"><thead><tr><th>How</th><th>Pages</th><th>Typical time</th><th>Typical length</th></tr></thead><tbody>${pages.ways.map((w) => `<tr><td>${esc(READ_WAY[w.via] || w.via)}</td><td>${esc(w.count)}</td><td>${esc((w.typical_ms / 1000).toFixed(1))}s</td><td>${esc(Number(w.typical_chars).toLocaleString())} chars</td></tr>`).join("")}</tbody></table>`
    : `<p class="muted">Pages read from now on are counted here.</p>`;
  const why = (pages.why_failed || []).map((f) => `${esc(f.code.replace(/_/g, " "))} (${esc(f.count)})`).join(" · ");
  const failed = (work.failures || []).length
    ? work.failures.map((f) => `<div class="detail-line"><span>${esc(stopText(f.reason) || f.reason)}</span><strong>${esc(f.count)}</strong></div>`).join("")
    : `<p class="muted">No task failed in this window.</p>`;
  const tasks = work.tasks || {};
  const bench = work.benchmark || {};
  const runs = (bench.runs || []).slice(0, 8);
  const lead = work.lead || {};
  const ended = { done: "finished", limit: "stopped at a limit", failed: "failed", stopped: "stopped" };
  const delegation = lead.briefs
    ? `<div class="detail-line"><span>Tasks that delegated</span><strong>${esc(lead.tasks_delegating)}</strong></div>
      <div class="detail-line"><span>Briefs per task, typically</span><strong>${esc(lead.typical_briefs)}</strong></div>
      <div class="detail-line"><span>Tool calls per brief, typically</span><strong>${esc(lead.typical_calls ?? "–")}</strong></div>
      ${Object.entries(lead.ended || {}).map(([status, count]) => `<div class="detail-line"><span>Briefs ${esc(ended[status] || status)}</span><strong>${esc(count)}</strong></div>`).join("")}
      <div class="detail-line"><span>Briefs with output, sources and scope</span><strong>${esc(lead.fully_described)} of ${esc(lead.briefs)}</strong></div>
      <div class="detail-line"><span>Addresses taken out</span><strong>${esc(lead.addresses_taken_out)}</strong></div>`
    : `<p class="muted">The lead has not delegated in this time.</p>`;
  const benchRows = runs.length
    ? `<table class="plain-table"><thead><tr><th>Started</th><th>Status</th><th>Minutes</th><th>Cost</th><th>Calls</th><th>Briefs not finished</th><th>Unconfirmed sources</th></tr></thead><tbody>${runs.map((r) => `<tr><td><a href="#task/${esc(r.run_id)}">${esc(when(r.started_at))}</a></td><td>${esc(String(r.status).toLowerCase())}</td><td>${esc(r.minutes ?? "…")}</td><td>$${esc(Number(r.cost_usd).toFixed(2))}</td><td>${esc(r.calls ?? "–")}</td><td>${esc(r.unfinished ?? 0)}</td><td>${esc(r.unresolved ?? 0)}</td></tr>`).join("")}</tbody></table>`
    : `<p class="muted">Not run yet.</p>`;
  return `<div class="grid2 spaced" data-work>
    <div class="card"><h2>How long each step takes</h2>
      <p class="muted">Last ${esc(work.days)} days · ${esc(tasks.finished || 0)} tasks finished${tasks.typical_minutes ? `, typically in ${esc(tasks.typical_minutes)} min` : ""}${tasks.typical_cost_usd ? ` for $${esc(Number(tasks.typical_cost_usd).toFixed(2))}` : ""}.</p>
      ${steps}</div>
    <div class="card"><h2>Reading pages</h2>
      <p class="muted">${pages.count ? `${esc(pages.read)} of ${esc(pages.count)} pages read (${esc(share)}%).` : ""} Pages are read several at a time and cleaned of menus and banners before a model sees them; one that refuses is opened in the office's own browser when browsing is on.</p>
      ${ways}${why ? `<p class="muted">Why some failed: ${why}</p>` : ""}</div>
    <div class="card"><h2>Why tasks failed</h2>${failed}</div>
    <div class="card"><h2>How the lead delegates</h2>${delegation}</div>
    <div class="card"><h2>Benchmark</h2>
      <p class="muted">${esc((bench.questions || []).length)} fixed questions of the kind the office is given, run as tasks, so a change shows as a number. Each run uses paid model calls${bench.estimate_usd ? `, about $${esc(Number(bench.estimate_usd).toFixed(2))} at what tasks cost lately` : ""}.</p>
      ${benchRows}
      <div class="button-row">${btn("Run the benchmark…", "benchmark-run")}</div></div>
  </div>`;
}



// Where a standing report goes: a chat channel, or nowhere but the office.
function postToField(current) {
  const channels = Settings.channels || [];
  if (!channels.length)
    return `<p class="muted">To post each result to a Slack channel, add the channel under Connections → Chat channels first.</p>`;
  return field("Post the result to", "post_to", {
    type: "select",
    required: false,
    value: current || "",
    choices: [["", "Nowhere, keep it in the office"], ...channels.map((d) => [d.id, d.name])],
    hint: "The answer and its file are posted there for everyone in that channel, each time it runs.",
  });
}

function officeView(templates, schedules, projects, models) {
  Office.schedules = schedules;
  const names = new Map(templates.map((t) => [t.id, t.name]));
  const templateRows = templates
    .map(
      (t) => `<div class="setting-row"><div><h3>${esc(t.name)}</h3><p>${esc(t.objective)}${t.project_id ? "" : " · project deleted"}</p></div><div class="button-row tight">${t.project_id ? btn("Run now", `template-run:${t.id}`) + btn("Schedule", `schedule-new:${t.id}`) : ""}${btn("Delete", `template-delete:${t.id}`, "danger")}</div></div>`
    )
    .join("");
  const scheduleRows = schedules
    .map((s) => {
      const cadence = s.frequency === "weekly" ? `Every ${DAYS[s.weekday]}` : "Every day";
      const next = s.enabled ? `Next: ${(s.preview || []).slice(0, 2).join(", ")}` : `Off (${s.disabled_reason || "disabled"})`;
      const posted = (Settings.channels || []).find((d) => d.id === s.post_to);
      const where = posted ? ` · posted to ${esc(posted.name)}` : "";
      return `<div class="setting-row" data-schedule="${esc(s.id)}"><div><h3>${esc(names.get(s.template_id) || "Template")}</h3><p>${esc(cadence)} at ${esc(s.local_time)} ${esc(s.timezone)} · ${esc(next)}${s.last_outcome ? ` · last: ${esc(s.last_outcome)}` : ""}${where}</p></div><div class="button-row tight">${btn(posted ? "Post to…" : "Post to Slack…", `schedule-post:${s.id}`)}${btn(s.enabled ? "Turn off" : "Turn on", `schedule-toggle:${s.id}:${s.enabled ? "disable" : "enable"}`)}${btn("Delete", `schedule-delete:${s.id}`, "danger")}</div></div>`;
    })
    .join("");
  const projectRows = projects
    .map((p) => `<div class="setting-row"><div><h3>${esc(p.name)}</h3><p>Created ${esc(when(p.created_at))}</p></div><div class="button-row tight"><a class="button" href="/projects/${esc(p.id)}/export" download>Export</a>${btn("Delete", `project-delete:${p.id}`, "danger")}</div></div>`)
    .join("");
  const keyRows = models
    .map((m) => `<div class="detail-line"><span>${esc(m.display_name)}<br><small class="muted">${esc(m.provider)} · ${esc(m.model_id)}</small></span><strong>${esc(m.credential_configured ? "Key stored" : "No key")}<br><small class="muted">${esc((CONNECTION_STATUS[m.status] || [m.status])[0])}</small></strong></div>`)
    .join("");
  const switches = [
    ["pause_new_tasks", "New tasks", "Nothing new starts, here or from chat. Running work finishes."],
    ["pause_schedules", "Scheduled tasks", "Schedules skip while this is on, and run again when it is off."],
    ["pause_chat", "Chat", "Your bots answer that the office is paused, and start nothing."],
  ];
  const office = Settings.office?.settings || {};
  return `${typeof textSizeCard === "function" ? textSizeCard() : ""}<div class="card"><h2>Switches</h2>
      <p class="muted">Each stops new work of one kind. Work already running finishes, and nothing is lost.</p>
      ${switches
        .map(
          ([key, title, about]) => `<div class="setting-row"><div><h3>${esc(title)} ${office[key] ? tag("paused", "amber") : tag("running")}</h3><p>${esc(about)}</p></div>
            ${btn(office[key] ? "Start again" : "Pause", `office-switch:${key}:${office[key] ? "off" : "on"}`, office[key] ? "primary" : "")}</div>`
        )
        .join("")}
    </div>
    <div class="grid2 spaced">
    <div class="card"><h2>Ask your office</h2><p class="muted">Describe what you need in plain words. The office proposes; nothing starts until you confirm.</p>
      <form class="compose" id="intake-form"><input name="text" maxlength="4000" required placeholder="e.g. What needs my approval?" aria-label="Ask your office"><button class="primary">Ask</button></form>
      <div id="intake-result" aria-live="polite"></div></div>
    <div class="card"><h2>Model access & keys</h2>${keyRows || `<p class="muted">No model connections yet. Add one, then choose it for each role under Models; agents cannot work without one.</p>`}
      <div class="button-row"><a class="button" href="#connections">Manage models & keys ↗</a></div>
      <p class="muted">Keys are stored encrypted on this computer, never shown again and never given to an agent.</p></div>
  </div>
  <div class="grid2 spaced">
    <div class="card"><h2>Templates &amp; schedules</h2>${templateRows || `<p class="muted">Save a finished task as a template from its task room, then run or schedule it here.</p>`}${scheduleRows ? `<h3 class="spaced">Schedules</h3>${scheduleRows}` : ""}<p class="muted">Scheduled tasks run only while the office is running, and use paid model calls.</p></div>
    <div class="card"><h2>Projects & data</h2>${projectRows || `<p class="muted">Projects appear when you start a task.</p>`}
      <div class="setting-row"><div><h3>Signed in as ${esc(Office.owner || "owner")}</h3><p>Only you can open this office. Sign-in links from the launcher work once.</p></div>${btn("Sign out", "sign-out")}</div></div>
  </div>`;
}

async function renderSettings(content) {
  const tab = Settings.tab || "office";
  const [status, projects, templates, schedules, office, usage, models] = await Promise.all([
    api("/status"),
    api("/projects"),
    api("/templates"),
    api("/schedules"),
    api("/settings"),
    api(`/usage/summary?days=${Settings.days || 30}`),
    api("/connections"),
  ]);
  // Channels a standing report can be posted to: chat channels, not direct messages.
  const channels = await api("/channels").catch(() => null);
  Settings.channels = (channels?.destinations || []).filter((d) => !String(d.external_id).startsWith("D") && d.name !== "Chat thread");
  // How fast and how surely the office works, from its own records.
  Settings.work = tab === "performance" ? await api(`/performance/work?days=${Settings.days || 30}`).catch(() => null) : null;
  if (tab === "models") {
    [Office.providers, Office.providerUsage, Settings.coding, Settings.coder] = await Promise.all([
      api("/providers/cards").catch(() => null),
      api(`/providers/usage?days=${Settings.providerDays || 7}`).catch(() => null),
      api("/settings/coding-agent").catch(() => null),
      api("/coder").catch(() => null),
    ]);
  }
  // Checked from the fetch service, which calls out to each platform: kept a
  // while, and checked again when the owner asks.
  const reachable = tab === "reach" ? await api(`/office/reach${Settings.reachFresh ? "?fresh=true" : ""}`).catch(() => null) : null;
  Settings.reachFresh = false;
  Settings.projects = projects.filter((p) => p.status === "ACTIVE");
  Settings.templates = templates;
  Settings.office = office;
  content.innerHTML =
    heading("SETTINGS", "Your office. Your rules.", "Spending, models, data and how the office is running.") +
    `<div class="subnav" role="tablist">${SETTINGS_TABS.map(([id, label]) => btn(esc(label), `settings-tab:${id}`, tab === id ? "active" : "", 'role="tab"')).join("")}</div>` +
    (tab === "models"
      ? roleModelsView(office, models)
      : tab === "reach"
      ? reachView(reachable)
      : tab === "budget"
      ? budgetView(office, usage)
      : tab === "usage"
        ? usageView(usage)
        : tab === "performance"
          ? performanceView(usage, status)
          : officeView(templates, schedules, Settings.projects, models));
  if (tab === "models") fillCodingAgent(content);
}

// What the office can reach, after Agent Reach's doctor: each source,
// whether it works, the method in use, and how to fix what does not.
const REACH_STATE = { ok: ["Works", ""], warn: ["Needs a look", "amber"], off: ["Off", "gray"] };

function reachView(reachable) {
  if (!reachable) return emptyCard("Could not check", "The office could not reach its fetch service. Try again in a moment.");
  const rows = (reachable.sources || [])
    .map((row) => {
      const [label, tone] = REACH_STATE[row.state] || [row.state, "gray"];
      return `<div class="reach-row reach-${esc(row.state)}" data-reach="${esc(row.id)}">
        <div class="reach-name"><strong>${esc(row.name)}</strong><span class="muted">${esc(row.what)}</span></div>
        <div class="reach-state">${tag(label, tone)}${row.via ? `<span class="muted">${esc(row.via)}</span>` : ""}</div>
        <div class="reach-detail"><p>${esc(row.detail)}</p>${row.fix ? `<p class="reach-fix">${esc(row.fix)}</p>` : ""}</div>
      </div>`;
    })
    .join("");
  const working = (reachable.sources || []).filter((row) => row.state === "ok").length;
  const ago = Number(reachable.checked_seconds_ago || 0);
  return `<div class="card reach">
    <div class="card-top"><div><h2>What the office can reach</h2>
      <p class="muted">${working} of ${(reachable.sources || []).length} sources work now. Checked ${ago < 60 ? "just now" : `${Math.round(ago / 60)} min ago`} from the service that reads every page, which holds no passwords and signs in nowhere.</p></div>
      ${btn("Check again", "reach-check")}</div>
    ${rows}
    <p class="muted reach-note">Platforms that need your account (X, Reddit, LinkedIn) are not read with your browser's cookies. Reach them through a tool server under Tools, which you review and grant per project.</p>
  </div>`;
}

Office.actions = Object.assign(Office.actions || {}, {
  "reach-check": () => {
    Settings.reachFresh = true;
    toast("Checking each source…");
    rerender();
  },
});

Office.screens.settings = { title: "Settings", render: renderSettings };

Office.actions = Object.assign(Office.actions || {}, {
  "routing-switch": async (state) => {
    const body = { model_routing: state === "fixed" ? "fixed" : "auto" };
    if ((await attempt(() => api("/settings", { method: "PUT", body }), state === "fixed" ? "New tasks use the models chosen per role." : "New tasks choose their model automatically.")) !== undefined) rerender();
  },
  "related-switch": async (state) => {
    const body = { related_across_lines: state === "on" };
    if ((await attempt(() => api("/settings", { method: "PUT", body }), state === "on" ? "Related work may come from any business line." : "Related work stays within each business line.")) !== undefined) rerender();
  },
  "team-switch": async (state) => {
    const body = { team_on: state === "on" };
    if ((await attempt(() => api("/settings", { method: "PUT", body }), state === "on" ? "New tasks work as a team." : "New tasks work as a relay again.")) !== undefined) rerender();
  },
  "browser-switch": async (state) => {
    const body = { browser_on: state === "on" };
    if ((await attempt(() => api("/settings", { method: "PUT", body }), state === "on" ? "New tasks may use the office's browser." : "The office's browser is off.")) !== undefined) rerender();
  },
  "advisor-switch": async (state) => {
    const body = { advisor_on: state === "on" };
    if ((await attempt(() => api("/settings", { method: "PUT", body }), state === "on" ? "The advisor is on call for new tasks." : "The advisor is off.")) !== undefined) rerender();
  },
  "office-switch": async (arg) => {
    const [key, state] = arg.split(":");
    const body = { [key]: state === "on" };
    if ((await attempt(() => api("/settings", { method: "PUT", body }), state === "on" ? "Paused." : "Running again.")) !== undefined) rerender();
  },
  // The benchmark is paid work, so it starts only on this press, after saying so.
  "benchmark-run": async () => {
    const bench = Settings.work?.benchmark || {};
    const cost = bench.estimate_usd ? `about $${Number(bench.estimate_usd).toFixed(2)}` : "an amount not yet known";
    if (!window.confirm(`Run the benchmark? It starts ${(bench.questions || []).length} tasks in the project "Office benchmark" and uses paid model calls, ${cost}.`)) return;
    const started = await attempt(
      () => api("/performance/benchmark", { method: "POST", body: { confirm: true } }),
      "The benchmark is running. Its tasks show here as they finish."
    );
    if (started) rerender();
  },
  "settings-tab": (tab) => {
    Settings.tab = tab;
    rerender();
  },
  "template-run": async (id) => {
    if (!window.confirm("Start a task from this template? It makes paid model calls.")) return;
    const run = await attempt(() => api(`/templates/${id}/run`, { method: "POST" }), "Task started.");
    if (run) go("task", run.id);
  },
  "template-delete": async (id) => {
    if (!window.confirm("Delete this template? Its schedules stop.")) return;
    if ((await attempt(() => api(`/templates/${id}`, { method: "DELETE" }), "Template deleted.")) !== undefined) rerender();
  },
  "schedule-new": (id) => {
    Settings.scheduleTemplate = id;
    formModal(
      "Schedule this template",
      "schedule-form",
      `${field("How often", "frequency", { type: "select", choices: [["daily", "Every day"], ["weekly", "Every week"]] })}
       ${field("Day (weekly)", "weekday", { type: "select", required: false, choices: DAYS.map((d, i) => [String(i), d]) })}
       ${field("Time", "local_time", { type: "time", value: "09:00" })}
       ${postToField("")}
       <p class="muted">Time zone: ${esc(Intl.DateTimeFormat().resolvedOptions().timeZone)}. Each run makes paid model calls.</p>`,
      "Add schedule"
    );
  },
  "schedule-post": (id) => {
    Settings.scheduleEditing = id;
    const current = (Office.schedules || []).find((s) => s.id === id)?.post_to || "";
    formModal("Post each result to", "schedule-post-form", postToField(current), "Save");
  },
  "schedule-toggle": async (arg) => {
    const [id, action] = arg.split(":");
    if ((await attempt(() => api(`/schedules/${id}/${action}`, { method: "POST" }))) !== undefined) rerender();
  },
  "schedule-delete": async (id) => {
    if ((await attempt(() => api(`/schedules/${id}`, { method: "DELETE" }), "Schedule deleted.")) !== undefined) rerender();
  },
  "project-delete": async (id) => {
    const project = Settings.projects.find((p) => p.id === id);
    const typed = window.prompt(`This permanently deletes "${project?.name}", its tasks and artifacts. Type the project name to confirm:`);
    if (!typed) return;
    if ((await attempt(() => api(`/projects/${id}`, { method: "DELETE", body: { confirm: typed } }), "Project deleted.")) !== undefined) rerender();
  },
  "intake-start": async () => {
    const proposal = Settings.proposal;
    if (!proposal) return;
    const run = await attempt(() => api("/runs", { method: "POST", body: proposal }), "Task started.");
    if (run) go("task", run.id);
  },
});

async function saveSettings(form) {
  const data = new FormData(form);
  const body = {};
  for (const [key, value] of data.entries()) {
    const text = String(value).trim();
    body[key] = text === "" ? null : Number(text);
  }
  if ((await attempt(() => api("/settings", { method: "PUT", body }), "Saved. New tasks use it.")) === undefined) return;
  rerender();
}

Office.forms = Object.assign(Office.forms || {}, {
  "role-models-form": async (form) => {
    const data = new FormData(form);
    const role_models = {};
    for (const [role] of BUILT_IN) role_models[role] = data.get(`role_${role}`) || null;
    if ((await attempt(() => api("/settings", { method: "PUT", body: { role_models } }), "Saved. New tasks use these models.")) === undefined) return;
    rerender();
  },
  "runs-at-once-form": async (form) => {
    const value = Number(new FormData(form).get("runs_at_once"));
    if ((await attempt(() => api("/settings", { method: "PUT", body: { runs_at_once: value } }), `Up to ${value} at once.`)) === undefined) return;
    rerender();
  },
  "budget-form": saveSettings,
  "limit-form": saveSettings,
  "schedule-post-form": async (form) => {
    const post_to = String(new FormData(form).get("post_to") || "") || null;
    const saved = await attempt(
      () => api(`/schedules/${Settings.scheduleEditing}/post`, { method: "PUT", body: { post_to } }),
      post_to ? "Each result will be posted there." : "Results stay in the office."
    );
    if (saved === undefined) return;
    closeModal();
    rerender();
  },
  "schedule-form": async (form) => {
    const data = new FormData(form);
    const frequency = String(data.get("frequency"));
    const body = {
      post_to: String(data.get("post_to") || "") || null,
      template_id: Settings.scheduleTemplate,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      frequency,
      local_time: String(data.get("local_time")),
      weekday: frequency === "weekly" ? Number(data.get("weekday") || 0) : null,
    };
    if ((await attempt(() => api("/schedules", { method: "POST", body }), "Schedule added.")) === undefined) return;
    closeModal();
    rerender();
  },
  "intake-form": async (form) => {
    const text = String(new FormData(form).get("text") || "").trim();
    const out = $("#intake-result");
    if (!out || !text) return;
    out.innerHTML = `<p class="muted">Thinking…</p>`;
    Settings.proposal = null;
    try {
      const reply = await api("/intake", { method: "POST", body: { text } });
      if (reply.action === "start_task" && reply.proposal) {
        Settings.proposal = { project_id: reply.proposal.project_id, objective: reply.proposal.objective };
        const project = Settings.projects.find((p) => p.id === reply.proposal.project_id)?.name || "your project";
        out.innerHTML = `<div class="approval-preview"><p>New work for <strong>${esc(project)}</strong>: “${esc(text)}”</p><p class="muted">It uses paid model calls.</p>${btn("Start this task", "intake-start", "primary")}</div>`;
      } else if (reply.action === "office_status") {
        const problems = statusRows(reply.result).filter(([, v]) => /unknown|missing|down|error|unavailable/i.test(v));
        out.innerHTML = `<p>${problems.length ? `Needs attention: ${esc(problems.map(([k]) => k).join(", "))}.` : "Everything is working."}</p>`;
      } else if (reply.action === "pending_approvals") {
        out.innerHTML = `<p>${reply.result.length} decision(s) waiting for you. ${btn("Show them", "go-review")}</p>`;
      } else if (reply.action === "clarify") {
        out.innerHTML = `<p>${reply.reason === "project_unclear" ? "Which project is this for? Name it in your request." : "I could not tell what you need. Try rephrasing."}</p>`;
      } else {
        out.innerHTML = "<p>That can't be done from here. Approvals and settings have their own screens.</p>";
      }
    } catch (error) {
      if (!(error instanceof SignedOut)) out.innerHTML = `<p class="error-text">${esc(explain(error))}</p>`;
    }
  },
});

Office.actions["go-review"] = () => {
  TaskRoom.filter = "review";
  go("tasks");
};

// Office settings: what configures the office, out of the way of the
// day's work. Each card opens the screen it always was.
const OFFICE_PARTS = [
  ["accounts", "Accounts", "Google, Microsoft 365, Slack, Notion and more: connect them with your consent, choose which projects use them."],
  ["agents", "Colleagues", "Your agents: their instructions, models, skills and chat bots."],
  ["settings", "Limits and models", "Spending limits, the model for each role, agents as a team, tasks at once, reach and performance."],
  ["knowledge", "Knowledge", "Folders and documents your projects may read."],
  ["tools", "Tools", "Tool servers your agents may call, each tool reviewed by you."],
  ["skills", "Skills", "Instructions your agents follow for a kind of step."],
  ["policies", "Rules", "What is allowed, refused, or asks you first."],
  ["connections", "Connections", "Model providers and chat channels such as Slack."],
  ["calendars", "Calendars", "Your Google, iCloud and other calendars, read so your colleagues work around your day."],
  ["access", "Mobile & remote access", "Open the office from your phone, at home or through your private tailnet."],
  ["handbook", "Handbook", "How to run the office, how it works, and its health now."],
];

function renderOfficeHub(content) {
  content.innerHTML =
    heading("OFFICE SETTINGS", "How your office is set up.", "Everything that configures the office. Day-to-day work lives on the Desk, in your threads and in the Library.", `<div class="button-row">${btn("Open Settings", "settings-open", "primary")}</div>`) +
    `<div class="office-hub">${OFFICE_PARTS.map(([id, name, about]) => `<a class="card" href="#${id}"><strong>${esc(name)}</strong><span>${esc(about)}</span></a>`).join("")}</div>`;
}

Office.screens.office = { title: "Office settings", render: renderOfficeHub };

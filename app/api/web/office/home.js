"use strict";

// Home: what needs the owner, what is moving, and what just happened.

function greeting(name) {
  const hour = new Date().getHours();
  const part = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const first = String(name || "").split(/\s+/)[0];
  return first && first !== "owner" ? `${part}, ${first}.` : `${part}.`;
}

function describeEvent(event) {
  const payload = event.payload || {};
  const role = ROLE_LABEL[NODE_ROLE[payload.node]] || "The office";
  const step = NODE_LABEL[payload.node] || payload.node;
  switch (event.kind) {
    case "run_started":
      return ["The office", "started the task."];
    case "run_resumed":
      return ["The office", "resumed the task."];
    case "step_started":
      return [role, `started ${step}.`];
    case "step_completed":
      return [role, `finished ${step}.`];
    case "instructions_received":
      return [role, payload.mid_step ? `read your message in the middle of ${step}, and is acting on it.` : `picked up your direction during ${step}.`];
    case "stuck_ask":
      return [role, `is stuck (${payload.what || "a step cannot get on"}) and asks for your help.`];
    case "help_given":
      return ["The office", payload.action === "hint" ? "carried on with your hint." : payload.action === "skip" ? "skipped that part, as you asked." : "tried again, without repeating what failed."];
    case "run_paused":
      if (payload.reason === "monthly_limit_reached") return ["The office", "paused the task before it started: this month's spending limit is reached."];
      return ["The office", "paused the task, as you asked."];
    case "run_cancelled":
      return ["The office", "cancelled the task, as you asked."];
    case "run_failed":
      return [ROLE_LABEL[payload.node] || "The office", `stopped: ${payload.code || "an error"}.`];
    case "run_finished":
      return ["The office", `finished: ${(STATUS[payload.status] || [payload.status])[0].toLowerCase()}.`];
    case "model_failed_over": {
      // The backup the owner chose for that provider.
      const why = { no_credit: "ran out of credit", key_refused: "refused its key", failing: "kept failing" }[payload.reason] || "kept failing";
      return ["The office", `moved ${ROLE_LABEL[payload.role] || payload.role || "a role"} to ${payload.to}, your backup: ${payload.from} ${why}. The task's budget is unchanged.`];
    }
    case "model_routed": {
      // The model router's tier for this task, as a ceiling.
      const sure = typeof payload.confidence === "number" ? ` (${Math.round(payload.confidence * 100)}% sure)` : "";
      const roles = Array.isArray(payload.roles) ? payload.roles : [];
      if (payload.outcome === "routed" && payload.tier === "performance") return ["The office", `kept every role on its own model: this task needs the strongest${sure}.`];
      if (payload.outcome === "routed" && roles.length) {
        const who = roles.map((role) => ROLE_LABEL[role] || role).join(", ");
        return ["The office", `ran ${who} on ${payload.model_id || "a cheaper model"}: the ${payload.tier} tier is enough for this task${sure}.`];
      }
      if (payload.outcome === "routed") return ["The office", `kept every role on its own model: each is already as cheap as the ${payload.tier} tier${sure}.`];
      if (payload.outcome === "nothing_to_route") return ["The office", "kept every role on its own model: no tested model with a known price to move to."];
      return ["The office", `kept every role on its own model: the model router ${payload.outcome === "low_confidence" ? "was not sure enough" : "was not available"}.`];
    }
    case "lead_step": {
      // One lead sizing the work; each tool it used, as it used it.
      if (payload.kind === "answer") return ["The lead", "wrote the answer."];
      const who = payload.brief ? `Brief ${payload.brief}` : "The lead";
      const tool = { search: "searched the web", read: "read a page", read_more: "read further in a long result", todo: "updated its step list", delegate: "handed briefs to researchers", review: "asked the critic to check the draft", propose_team_run: "proposed a full team run" }[payload.tool] || `used ${String(payload.tool || "a tool").replace(/^mcp:/, "").replace(/:/g, " · ")}`;
      return [who, payload.status === "ok" ? `${tool}.` : `${tool}: ${String(payload.reason || payload.status || "failed").replace(/_/g, " ")}.`];
    }
    case "team_run_proposed":
      return ["The lead", `proposes a full team run: ${payload.objective || "this request"}${payload.why ? ` (${payload.why})` : ""}. To run it, start a new task with Full team ticked.`];
    case "model_downgraded":
      return ["The office", `moved the task to ${payload.model}, the cheaper model, past $${payload.after_usd}.`];
    case "skill_applied":
      return ["The office", `applied the skill ${payload.skill_id}.`];
    case "skill_skipped":
      return ["The office", `skipped the skill ${payload.skill_id} (${payload.reason}).`];
    case "mcp_call": {
      const what = `${payload.tool} on ${payload.server}`;
      if (payload.outcome === "ok") return ["The office", `called ${what}.`];
      if (payload.outcome === "monthly_limit_reached") return ["The office", `did not call ${what}: this month's spending limit is reached.`];
      if (payload.outcome === "project_limit_reached") return ["The office", `did not call ${what}: the project's monthly limit is reached.`];
      return ["The office", `could not use ${what} (${String(payload.outcome || "failed").replace(/_/g, " ")}).`];
    }
    case "rework_requested":
      return ["You", `sent the ${payload.target || "work"} back to be redone.`];
    case "mcp_unavailable":
      return ["The office", `went on without ${payload.server}: ${payload.reason === "sign_in_required" ? "it needs you to sign in again" : "its key is missing"}.`];
    default:
      return ["The office", event.kind.replace(/_/g, " ") + "."];
  }
}

function taskCard(task, featured) {
  const needs = NEEDS_OWNER.includes(task.status);
  const project = task.project ? `${task.project} · ` : "";
  const detail = `${project}${task.requested_from === "chat" ? "Asked from chat · " : ""}${when(task.started_at) || "Not started"}`;
  if (featured) {
    return `<div class="card task-feature"><div class="section-head">${statusTag(task.status)}<span class="muted">${esc(task.project)}</span></div>
      <h3>${esc(task.objective)}</h3>
      <p>${needs ? "Your office has stopped here for you." : "Your office is working on this."}</p>
      <div class="task-foot"><span>${esc(detail)}</span>${btn("Open task room ↗", `open:${task.run_id}`)}</div></div>`;
  }
  return `<button type="button" class="card compact-task" data-action="open:${esc(task.run_id)}">${avatar(task.project || task.objective)}<div><strong>${esc(task.objective)}</strong><p>${esc(detail)}</p></div>${statusTag(task.status)}</button>`;
}

// My desk: one place to start talking, and only what concerns the
// owner now. Running counts and spend sit in one line at the foot.
const NEEDS_WHY = {
  WAITING_FOR_APPROVAL: "It proposes something to be done, and nothing is done without you.",
  NEEDS_HUMAN_REVIEW: "It stopped for your review: accept it, or say what to change.",
  BLOCKED: "It cannot go on until you decide.",
  PAUSED: "It is paused and keeps everything it did.",
};

function deskTitle(task) {
  return task.title || String(task.objective || "").split("\n")[0].slice(0, 90);
}

// Where something came from, as a way back to it.
function deskFrom(from) {
  if (!from) return "";
  const href = from.kind === "topic" ? `#topic/${esc(from.id)}` : `#project/${esc(from.id)}`;
  return `<a href="${href}">${esc(from.title || (from.kind === "topic" ? "its discussion" : "its project"))}</a>`;
}

// One decision waiting on the owner: what happened, where from, what is needed,
// and the buttons that answer it right here.
function deskNeed(need) {
  let actions = btn("Open", `open:${need.run_id}`, "primary");
  if (need.kind === "policy") {
    actions = btn("Refuse", `room-policy:${need.record_id}:no`, "danger") + btn("Allow for this task", `room-policy:${need.record_id}:yes`, "primary");
  } else if (need.kind === "agent") {
    // A coding agent's permission question: answered here, as in its thread.
    actions = (need.options || [])
      .map((o) => btn(o.name || o.id, `agent-answer:${need.run_id}:${need.ask_id}:${o.id}`, String(o.kind || "").startsWith("reject") ? "danger" : "primary"))
      .join("");
  } else if (need.kind === "budget") {
    const kind = CEILING[need.limit] ? need.limit : "tokens";
    const choices = ceilingChoices(need.run_id, { kind, budget: need.budget || {}, spent: need.spent || {} });
    return `<div class="desk-need desk-ceiling" data-need="budget"><div><strong>${esc(need.what)}</strong><span class="why">${need.why ? `${esc(need.why)} · ` : ""}From ${deskFrom(need.from)}. ${esc(need.need)}</span>${choices}</div><div class="button-row tight">${actions}</div></div>`;
  }
  return `<div class="desk-need" data-need="${esc(need.kind)}"><div><strong>${esc(need.what)}</strong><span class="why">${need.why ? `${esc(need.why)} · ` : ""}From ${deskFrom(need.from)}. ${esc(need.need)}</span></div><div class="button-row tight">${actions}</div></div>`;
}

function deskDelivered(item) {
  return `<a class="desk-row" href="#task/${esc(item.run_id)}"><span class="avatar" aria-hidden="true">▤</span><div><strong>${esc(item.title)}</strong><span class="muted">${item.concluded ? `${esc(roomShort(item.concluded, 140))} · ` : ""}${esc(when(item.completed_at))}</span></div></a>`;
}

function deskReturning(topic) {
  const day = new Date(topic.revisit_at).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  return `<a class="desk-row" href="#topic/${esc(topic.id)}"><span class="avatar" aria-hidden="true">↺</span><div><strong>${esc(topic.title)}</strong><span class="muted">Comes back ${esc(day)}, with where it stood</span></div></a>`;
}

// The Desk: only what concerns the
// owner now. What needs them, with the buttons that answer it here; what is in
// motion; their wall (delivered work, parked discussions, ideas, the calendar:
// the widgets they arranged) and the team beside it. The floor plan of mostly
// "Free" desks it replaces is gone.
function deskDate() {
  return new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}

function deskHeadline(name, needs) {
  const said = needs === 0 ? "Nothing needs you." : needs === 1 ? "One thing needs you." : `${needs} things need you.`;
  return `${greeting(name)} ${said}`;
}

// One task at work: what it is doing now, how far, and what it has spent.
function deskMoving(task) {
  const step = NODE_LABEL[task.step] || (task.step ? String(task.step).replace(/_/g, " ") : "Starting");
  const progress = task.progress && task.progress.total ? `${task.progress.done} / ${task.progress.total} ${task.progress.unit}` : "";
  const budget = task.budget || {};
  const spend = typeof budget.spent_cost === "number" ? `$${budget.spent_cost.toFixed(2)}${budget.max_cost ? ` of $${Number(budget.max_cost).toFixed(2)}` : ""}` : "";
  const meta = [progress, spend].filter(Boolean).join(" · ");
  return `<a class="desk-moving" href="#task/${esc(task.run_id)}"><i class="side-spin" aria-hidden="true"></i><span class="desk-moving-what"><strong>${esc(task.title)}</strong><span>${esc(step)}${task.project ? ` · ${esc(task.project)}` : ""}</span></span>${meta ? `<span class="desk-moving-meta">${esc(meta)}</span>` : ""}</a>`;
}

// The colleagues, each with what they are doing now.
function deskTeam(colleagues, running) {
  if (!colleagues.length) return "";
  const rows = colleagues
    .slice(0, 8)
    .map((agent, i) => {
      const busy = running.find((task) => NODE_ROLE[task.step] === agent.role);
      return `<a class="desk-colleague" href="#agent/${esc(agent.id)}">${avatar(agent.name, i)}<span>${esc(agent.name)} · ${esc(ROLE_LABEL[agent.role] || agent.role)}</span><span class="side-time">${busy ? "working" : "free"}</span></a>`;
    })
    .join("");
  return `<section class="desk-aside-block" aria-label="Team"><p class="side-label">Team</p>${rows}</section>`;
}

function deskSection(title, count, body, tone = "") {
  return `<section class="desk-section" aria-label="${esc(title)}"><div class="desk-section-head"><h2>${esc(title)}</h2>${
    count ? `<span class="desk-section-count ${tone}">${esc(count)}</span>` : ""
  }</div>${body}</section>`;
}

async function renderHome(content) {
  // The wall first: the calendar is read only while it is on it.
  const wallRead = typeof wallLoad === "function" ? wallLoad() : Promise.resolve(null);
  const [overview, desk, pulse, agents, day] = await Promise.all([
    api("/office/overview"),
    api("/office/desk"),
    api("/office/pulse").catch(() => ({})),
    api("/agents").catch(() => []),
    wallRead.then(() => (typeof calDay === "function" && typeof wallHas === "function" && wallHas("calendar") ? calDay() : null)),
  ]);
  const counts = overview.counts;
  Office.topics = desk.continue || Office.topics;
  const colleagues = (Array.isArray(agents) ? agents : []).filter((a) => a.status === "ACTIVE" && a.enabled_version);
  const running = Array.isArray(pulse?.running) ? pulse.running : [];
  const setup = Office.setup;
  const setupCard =
    setup && !setup.completed_at
      ? `<div class="attention ${setup.ready ? "calm" : ""}"><span class="attention-icon">◈</span><div><strong>${setup.ready ? "Your office is ready" : "Finish setting up your office"}</strong><p>${esc(
          setup.ready
            ? "Everything needed is in place. Mark setup finished when you are happy with it."
            : `${setup.required_done} of ${setup.required_total} steps done: ${setup.steps.filter((s) => !s.done && !s.optional).map((s) => s.title.toLowerCase()).join(", ")}.`
        )}</p></div><a class="button" href="#setup">Open setup →</a></div>`
      : "";
  const needs = desk.needs || [];
  // In a meeting, what can wait is held and says until when.
  const held = day?.hold && day?.status?.state === "busy" && needs.length ? ` · held until ${calClock(day.status.until)}` : "";
  const wall = typeof wallRender === "function" ? wallRender({ overview, desk, day }) : { wide: "", side: "" };
  content.innerHTML =
    `<div class="heading desk-heading"><div><div class="eyebrow">Desk · ${esc(deskDate())}${esc(held)}</div><h1>${esc(deskHeadline(overview.owner_name, needs.length))}</h1></div></div>` +
    setupCard +
    `<div class="desk-layout">
      <div class="desk-main">
        ${startBox("desk-start", "An idea, a question, a link…")}
        ${deskSection(
          "Needs you",
          needs.length,
          needs.length ? `<div class="desk-needs">${needs.map(deskNeed).join("")}</div>` : `<p class="muted">Nothing is waiting for you. The team carries on.</p>`,
          "needs"
        )}
        ${running.length ? deskSection("In motion", running.length, `<div class="desk-list">${running.map(deskMoving).join("")}</div>`) : ""}
        ${wall.wide}
      </div>
      <aside class="desk-aside" aria-label="Beside your desk">
        ${wall.side}
        ${deskTeam(colleagues, running)}
      </aside>
    </div>`;
  updateTaskCount(counts.needs_owner + counts.in_motion);
}

Office.screens.home = { title: "Desk", render: renderHome, refresh: 15000, quiet: () => Boolean(document.activeElement?.closest?.("[data-start-topic], [data-idea-box]")) || Boolean(typeof Wall === "object" && (Wall.drag || Wall.arranging)) };

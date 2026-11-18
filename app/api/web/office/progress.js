"use strict";

// Progress cards for projects and tasks, after a planning-card design:
// an accent per card, a ring, three figures, a timeline with "today" and a
// pace forecast, the parts with small bars, and what comes next. Drawn from
// GET /office/progress. A project's timeline is its month against its limit;
// a task's is its time limit.

const PROGRESS_STATE = {
  needs_you: ["Needs you", "amber"],
  working: ["Working", ""],
  quiet: ["Quiet", "gray"],
};
const PART_DONE = ["COMPLETED"];
const PART_BAD = ["FAILED", "CANCELLED", "REJECTED"];

function progressRing(share, caption) {
  const r = 26;
  const length = 2 * Math.PI * r;
  const value = share === null || share === undefined ? null : Math.max(0, Math.min(1, share));
  const shown = value === null ? "—" : `${Math.round(value * 100)}<small>%</small>`;
  return `<div class="pc-ring" role="img" aria-label="${esc(caption)}: ${value === null ? "nothing to count yet" : `${Math.round(value * 100)}%`}" data-ring="${value === null ? "" : value}">
    <svg viewBox="0 0 64 64" aria-hidden="true"><circle class="pc-ring-track" cx="32" cy="32" r="${r}"></circle>
    <circle class="pc-ring-value" cx="32" cy="32" r="${r}" stroke-dasharray="${length.toFixed(2)}" stroke-dashoffset="${((1 - (value || 0)) * length).toFixed(2)}"></circle></svg>
    <span>${shown}</span></div>`;
}

function progressTile(label, value, note, lead, alert) {
  return `<div class="pc-tile${lead ? " lead" : ""}${alert ? " alert" : ""}"><span>${esc(label)}</span><strong>${value}</strong>${note ? `<small>${esc(note)}</small>` : ""}</div>`;
}

function dayLabel(iso) {
  if (!iso) return "";
  const date = new Date(`${iso}T12:00:00`);
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function minutesLabel(minutes) {
  const m = Math.round(Number(minutes || 0));
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

// The month against the project's own limit: spend filled, today marked.
function projectTimeline(month) {
  const elapsed = Math.round((month.elapsed || 0) * 100);
  const limited = Boolean(month.limit);
  const fill = limited ? Math.min(100, Math.round((month.share || 0) * 100)) : elapsed;
  const ahead = limited && month.share > month.elapsed;
  let pace;
  if (month.forecast === null || month.forecast === undefined) pace = `${money(month.spent)} so far. Too early in the month to forecast.`;
  else if (!limited) pace = `At this pace: <b>${money(month.forecast)}</b> by ${esc(dayLabel(month.month_end))}. No limit of its own; the office's applies.`;
  else {
    const share = Math.round((month.forecast_share || 0) * 100);
    pace = `At this pace: <b>${money(month.forecast)}</b> by ${esc(dayLabel(month.month_end))}, <b class="${share >= 100 ? "pc-warn" : ""}">${share}%</b> of its limit.`;
  }
  return `<div class="pc-timeline">
      <div class="pc-track${ahead ? " ahead" : ""}"><span class="pc-fill" style="width:${fill}%"></span>
        <span class="pc-now" style="left:${elapsed}%"><em>today</em></span><span class="pc-end" aria-hidden="true">◆</span></div>
      <div class="pc-scale"><span>${esc(dayLabel(month.month_start))}</span><span>${limited ? `${money(month.spent)} of ${money(month.limit)}` : `${money(month.spent)} spent`}</span><span>${esc(dayLabel(month.month_end))}</span></div>
    </div>
    <p class="pc-pace">${pace}</p>`;
}

// The task's time limit: time used filled, now marked, the finish forecast.
function taskTimeline(card) {
  const limit = Number(card.max_minutes || 0);
  const elapsed = Number(card.elapsed_minutes || 0);
  const used = limit ? Math.min(100, Math.round((elapsed / limit) * 100)) : 0;
  const remaining = card.pace?.remaining_minutes;
  const finish = limit && remaining !== null && remaining !== undefined ? Math.min(100, Math.round(((elapsed + remaining) / limit) * 100)) : null;
  let pace;
  if (card.reason) pace = `Waiting: <b>${esc(card.reason)}</b>.`;
  else if (remaining === null || remaining === undefined) pace = "The pace shows once its first step is done.";
  else if (card.pace.over_limit_by) pace = `At this pace it needs about <b>${esc(minutesLabel(remaining))}</b> more, <b class="pc-warn">${esc(minutesLabel(card.pace.over_limit_by))}</b> past its time limit.`;
  else pace = `At this pace it finishes in about <b>${esc(minutesLabel(remaining))}</b>${limit ? `, inside its ${esc(minutesLabel(limit))}` : ""}.`;
  return `<div class="pc-timeline">
      <div class="pc-track"><span class="pc-fill" style="width:${used}%"></span>
        <span class="pc-now" style="left:${used}%"><em>now</em></span>
        ${finish !== null && !card.reason ? `<span class="pc-forecast" style="left:${finish}%" title="Forecast finish"></span>` : ""}
        <span class="pc-end" aria-hidden="true">◆</span></div>
      <div class="pc-scale"><span>${esc(card.started_at ? when(card.started_at) : "not started")}</span><span>${esc(minutesLabel(elapsed))} running</span><span>${limit ? `limit ${esc(minutesLabel(limit))}` : "no time limit"}</span></div>
    </div>
    <p class="pc-pace">${pace}</p>`;
}

function partRow(name, share, figure, tone, action) {
  const width = Math.round(Math.max(0, Math.min(1, share || 0)) * 100);
  const label = action ? `<button type="button" class="pc-part-name" data-action="${esc(action)}" title="${esc(name)}">${esc(name)}</button>` : `<span class="pc-part-name" title="${esc(name)}">${esc(name)}</span>`;
  return `<div class="pc-part ${tone || ""}">${label}<span class="pc-bar"><i style="width:${width}%"></i></span><span class="pc-figure">${esc(figure)}</span></div>`;
}

function projectProgressCard(card, project) {
  const [stateText, stateTone] = PROGRESS_STATE[card.state] || PROGRESS_STATE.quiet;
  const counts = card.counts || {};
  const month = card.month || {};
  const parts = (card.tasks || [])
    .map((t) => {
      const finished = PART_DONE.includes(t.status);
      const bad = PART_BAD.includes(t.status);
      const share = finished ? 1 : t.total ? t.done / t.total : 0;
      const figure = finished ? "done" : bad ? (t.status === "FAILED" ? "failed" : "stopped") : NEEDS_OWNER.includes(t.status) ? "needs you" : `${t.done}/${t.total}`;
      return partRow(t.title, share, figure, bad ? "bad" : NEEDS_OWNER.includes(t.status) ? "waiting" : "", `open:${t.run_id}`);
    })
    .join("");
  const next = (card.next || []).length
    ? card.next.map((n) => `<div class="pc-next-row"><span>${esc(n.reason)}</span><button type="button" data-action="open:${esc(n.run_id)}">${esc(n.title)}</button></div>`).join("")
    : `<p class="muted">Nothing waits on you here.</p>`;
  const sub = [card.line, card.last_activity ? `active ${when(card.last_activity)}` : "no work yet"].filter(Boolean).join(" · ");
  return `<article class="pc accent-${esc(card.accent)}" data-progress-card="project" data-id="${esc(card.id)}">
    <div class="pc-head"><div class="pc-title">
      <h2><span class="pc-dot" aria-hidden="true"></span>${esc(card.name)}</h2>
      <p>${tag(stateText, stateTone)} <span class="muted">${esc(sub)}</span></p></div>
      ${progressRing(card.ring, "Tasks finished")}</div>
    ${project ? lineLabel({ project_id: project.id, name: card.line, set_by: project.venture_set_by, suggestion: suggestionFor(project, Projects.ventures) }) : ""}
    <div class="pc-tiles">
      ${progressTile("This month", money(month.spent), month.limit ? `of ${money(month.limit)}` : "no limit of its own", true)}
      ${progressTile("Running", esc(counts.running || 0), `${counts.done || 0} of ${counts.total || 0} done`)}
      ${progressTile("Needs you", esc(counts.needs_you || 0), counts.failed ? `${counts.failed} failed` : "", false, counts.needs_you > 0)}
    </div>
    ${projectTimeline(month)}
    ${parts ? `<div class="pc-parts">${parts}</div>` : `<p class="muted pc-empty">No tasks yet.</p>`}
    <div class="pc-next"><h3>Next</h3>${next}</div>
    <div class="button-row">${btn("Open project", `project-open:${card.id}`, "pc-go")}${btn("＋ New task here", `project-task:${card.id}`)}</div>
  </article>`;
}

function taskProgressCard(card) {
  const budget = card.budget || {};
  const moved = card.progress || {};
  const parts = (card.steps || []).length
    ? card.steps
        .map((s) => {
          const share = s.status === "done" ? 1 : 0;
          const figure = { done: "done", failed: "failed", empty: "found nothing", pending: "waiting" }[s.status] || s.status;
          return partRow(s.label, share, figure, s.status === "failed" ? "bad" : s.status === "empty" ? "waiting" : "");
        })
        .join("")
    : partRow(`The ${moved.unit === "stages" ? "fixed steps" : "plan"}`, moved.total ? moved.done / moved.total : 0, `${moved.done || 0}/${moved.total || 0}`);
  const tokens = budget.max_tokens ? `of ${Number(budget.max_tokens).toLocaleString()}` : "no token limit";
  const next = card.reason
    ? `<p>${esc(card.status === "PAUSED" ? "It is paused. Open it to see why, and resume it or raise its limit." : "Open it to decide what happens next.")}</p>`
    : (card.next || []).length
      ? card.next.map((label) => `<div class="pc-next-row"><span>step</span><b>${esc(label)}</b></div>`).join("")
      : `<p class="muted">Its steps are all under way; review and the answer come next.</p>`;
  return `<article class="pc accent-${esc(card.accent)}${card.reason ? " is-waiting" : " is-running"}" data-progress-card="task" data-id="${esc(card.run_id)}">
    <div class="pc-head"><div class="pc-title">
      <h2><span class="pc-dot" aria-hidden="true"></span>${esc(card.title)}</h2>
      <p>${statusTag(card.status)} <span class="muted">${esc(card.project)}</span></p></div>
      ${progressRing(card.ring, `${moved.unit === "stages" ? "Stages" : "Steps"} done`)}</div>
    <div class="pc-tiles">
      ${progressTile("Tokens", Number(budget.spent_tokens || 0).toLocaleString(), tokens, budget.share === null || budget.share < 0.8, budget.share >= 0.8)}
      ${progressTile("Cost", money(budget.spent_cost), budget.max_cost ? `of ${money(budget.max_cost)}` : "")}
      ${progressTile("Steps", `${esc(moved.done || 0)}/${esc(moved.total || 0)}`, moved.unit === "stages" ? "fixed steps" : "with review and answer")}
    </div>
    ${taskTimeline(card)}
    <div class="pc-parts">${parts}</div>
    <div class="pc-next"><h3>Next</h3>${next}</div>
    <div class="button-row">${btn("Open task", `open:${card.run_id}`, "pc-go")}</div>
  </article>`;
}

"use strict";

// The board: every task in the column its real state puts it in, with how much
// work it took, and how the office's weeks compare.

const Board = { data: null, project: "", effort: "", label: "" };

const BAND_LABEL = { light: "Light", standard: "Standard", heavy: "Heavy" };

function effortTag(effort) {
  const parts = [`${effort.tokens.toLocaleString()} tokens`];
  if (effort.revisions) parts.push(`${effort.revisions} revision${effort.revisions > 1 ? "s" : ""}`);
  if (effort.subquestions > 1) parts.push(`${effort.subquestions} research questions`);
  if (effort.sources) parts.push(`${effort.sources} sources`);
  if (effort.artifacts) parts.push(`${effort.artifacts} artifacts`);
  return `<span class="tag ${effort.band === "heavy" ? "clay" : effort.band === "standard" ? "" : "gray"}" title="${esc(parts.join(" · "))}">${esc(BAND_LABEL[effort.band])}</span>`;
}

function duration(minutes) {
  if (minutes === null || minutes === undefined) return "";
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return `${Math.round(minutes)} min`;
  if (minutes < 60 * 24) return `${(minutes / 60).toFixed(1)} h`;
  return `${Math.round(minutes / 60 / 24)} d`;
}

const PAUSABLE = ["PENDING", "RUNNING", "RECOVERING"];
const CLOSABLE = [...ACTIVE, ...NEEDS_OWNER];

// What the owner may do to a task from the board, by the state it is in. Each
// choice is a real transition the office performs, not a label.
function movesFor(status) {
  const moves = [];
  if (PAUSABLE.includes(status)) moves.push(["pause", "Paused"]);
  if (status === "PAUSED") moves.push(["resume", "In progress"]);
  if (CLOSABLE.includes(status)) moves.push(["cancel", "Closed"]);
  return moves;
}

function statusPicker(card) {
  const moves = movesFor(card.status);
  const here = (STATUS[card.status] || [card.status])[0];
  if (!moves.length) return `<span class="muted">${esc(here)}</span>`;
  return `<select class="status-pick" data-run="${esc(card.run_id)}" aria-label="Status of ${esc(card.objective.slice(0, 60))}">
      <option value="" selected>${esc(here)}</option>
      ${moves.map(([action, label]) => `<option value="${esc(action)}">Move to ${esc(label)}</option>`).join("")}
    </select>`;
}

function boardCard(card) {
  const waiting = card.minutes_with_you ? `waiting ${duration(card.minutes_with_you)}` : "";
  const running = card.minutes_running ? `${duration(card.minutes_running)} of work` : "";
  const detail = [card.project, running, waiting].filter(Boolean).join(" · ");
  const movable = movesFor(card.status).length > 0;
  return `<div class="board-card ${movable ? "movable" : ""}" role="button" tabindex="0" data-action="peek:${esc(card.run_id)}" data-run="${esc(card.run_id)}" data-status="${esc(card.status)}" title="${movable ? "Drag anywhere on this card to move it, or use the status box" : ""}">
      <span class="board-top">${effortTag(card.effort)}<span class="muted">${card.cost_usd ? `$${card.cost_usd.toFixed(2)}` : ""}</span></span>
      <strong>${esc(card.title || card.objective)}</strong>
      <span class="muted">${esc(detail)}</span>
      ${card.step && ACTIVE.includes(card.status) ? `<span class="muted">now: ${esc(NODE_LABEL[card.step] || card.step)}</span>` : ""}
      ${card.error ? `<span class="muted">${esc(card.error)}</span>` : ""}
      <span class="labels">${card.project ? tag(card.project, "gray") : ""}${(card.labels || []).map((label) => tag(label, "gray")).join("")}${card.requested_from === "chat" ? tag("from chat", "gray") : ""}</span>
      <span class="card-foot">${statusPicker(card)}</span>
    </div>`;
}

function flowChart(flow) {
  const peak = Math.max(1, ...flow.map((w) => Math.max(w.started, w.completed)));
  return `<div class="flow">${flow
    .map(
      (week) => `<div class="flow-week" title="${esc(week.week_ending)}: ${week.started} started, ${week.completed} finished, $${week.cost_usd.toFixed(2)}">
        <span class="flow-bars">
          <span class="flow-bar started" style="height:${Math.max(2, (week.started / peak) * 100)}%"></span>
          <span class="flow-bar completed" style="height:${Math.max(2, (week.completed / peak) * 100)}%"></span>
        </span>
        <small>${esc(week.week_ending.slice(5))}</small>
      </div>`
    )
    .join("")}</div>`;
}

function measures(board) {
  const flow = board.flow;
  const week = flow[flow.length - 1] || { started: 0, completed: 0, cost_usd: 0, tokens: 0 };
  const before = flow[flow.length - 2] || { completed: 0 };
  const change = week.completed - before.completed;
  const q = board.quality;
  const bands = q.effort_bands;
  const total = Math.max(1, bands.light + bands.standard + bands.heavy);
  const share = (n) => Math.round((n / total) * 100);
  return `<div class="grid2">
    <div class="card"><h2>Volume &amp; velocity</h2>
      <div class="metrics tight">
        <div class="metric"><label>Started this week</label><strong>${week.started}</strong><span>${week.tokens.toLocaleString()} tokens</span></div>
        <div class="metric"><label>Finished this week</label><strong>${week.completed}</strong><span>${change === 0 ? "same as last week" : `${change > 0 ? "↑" : "↓"} ${Math.abs(change)} from last week`}</span></div>
        <div class="metric"><label>Typical time to finish</label><strong>${duration(q.median_working_minutes) || "—"}</strong><span>median, work only</span></div>
        <div class="metric"><label>Spent this week</label><strong>$${week.cost_usd.toFixed(2)}</strong><span>${q.median_cost_usd ? `$${q.median_cost_usd.toFixed(2)} per task` : "per-task cost unknown"}</span></div>
      </div>
      ${flowChart(flow)}
      <p class="muted hint"><span class="key started"></span> started &nbsp; <span class="key completed"></span> finished · each bar is one week</p>
    </div>
    <div class="card"><h2>How the work goes</h2>
      <div class="detail-line"><span>Finished without a revision</span><strong>${q.first_pass_rate === null ? "—" : `${Math.round(q.first_pass_rate * 100)}%`}</strong></div>
      <div class="detail-line"><span>Typical wait for your decision</span><strong>${duration(q.median_waiting_minutes) || "—"}</strong></div>
      <div class="detail-line"><span>Finished</span><strong>${q.completed}</strong></div>
      <div class="detail-line"><span>Stopped, failed or rejected</span><strong>${q.stopped}</strong></div>
      <div class="detail-line"><span>Effort mix</span><strong>${share(bands.light)}% light · ${share(bands.standard)}% standard · ${share(bands.heavy)}% heavy</strong></div>
      <p class="muted hint">Effort counts tokens, revisions, parallel research questions and saved artifacts. Waiting time is how long a task sat with you, which the office cannot shorten by itself.</p>
    </div>
  </div>`;
}

async function renderBoard(content) {
  const board = await api("/office/board");
  Board.data = board;
  const projects = [...new Set(board.columns.flatMap((c) => c.cards.map((card) => card.project)))].filter(Boolean).sort();
  const labels = [...new Set(board.columns.flatMap((c) => c.cards.flatMap((card) => card.labels || [])))].sort();
  const keep = (card) =>
    (!Board.project || card.project === Board.project) &&
    (!Board.effort || card.effort.band === Board.effort) &&
    (!Board.label || (card.labels || []).includes(Board.label));
  const columns = board.columns
    .map((column) => {
      const cards = column.cards.filter(keep);
      const drop =
        column.key === "paused" ? "pause" : column.key === "closed" ? "cancel" : column.key === "working" ? "resume" : "";
      const zoneText = {
        pause: "Drop a task here to pause it",
        cancel: "Drop a task here to close it",
        resume: "Drop a paused task here to resume it",
      }[drop];
      const zone = drop ? `<div class="drop-zone" data-zone="${drop}">${zoneText}</div>` : "";
      return `<section class="board-column" data-drop="${drop}"><header class="board-head"><h2>${esc(column.title)}</h2><span class="count">${cards.length}</span></header>
        <p class="muted hint">${esc(column.about)}</p>
        <div class="board-cards">${cards.map(boardCard).join("") || `<p class="muted">Nothing here.</p>`}</div>
        ${zone}</section>`;
    })
    .join("");
  content.innerHTML =
    heading("THE BOARD", "Where every task stands.", "Columns are the office's real states, so a card moves when the work does.", btn("＋ New task", "new-task", "primary")) +
    `<div class="subnav board-filters">
      <div class="filter">${field("Project", "board-project", { type: "select", required: false, value: Board.project, choices: [["", "All projects"], ...projects.map((p) => [p, p])] })}</div>
      <div class="filter">${field("Effort", "board-effort", { type: "select", required: false, value: Board.effort, choices: [["", "Any effort"], ["light", "Light"], ["standard", "Standard"], ["heavy", "Heavy"]] })}</div>
      <div class="filter">${field("Label", "board-label", { type: "select", required: false, value: Board.label, choices: [["", "Any label"], ...labels.map((l) => [l, l])] })}</div>
    </div>
    <div class="board">${columns}</div>
    <h2 class="spaced">How the office is doing</h2>
    ${measures(board)}`;
}

Office.screens.board = { title: "Board", render: renderBoard, refresh: 20000 };

// ---- moving a card to pause or close it ----
// Pointer dragging rather than HTML5 drag-and-drop: it behaves the same in
// every browser and on a touch screen, and a card may only be dropped where a
// real action exists, so the board can never show a state the office is not in.

function allowedDrop(status, action) {
  if (action === "pause") return PAUSABLE.includes(status);
  if (action === "resume") return status === "PAUSED";
  if (action === "cancel") return CLOSABLE.includes(status);
  return false;
}

async function moveCard(runId, action) {
  if (action === "cancel" && !window.confirm("Close this task? Work done so far is kept, but it will not continue.")) return;
  const request =
    action === "resume"
      ? () => api(`/runs/${runId}/resume`, { method: "POST" })
      : () => api(`/runs/${runId}/controls`, { method: "POST", body: { kind: action } });
  const said = {
    pause: "Pausing at the next step boundary.",
    resume: "Resumed; it returns to In progress.",
    cancel: "Task closed.",
  };
  if ((await attempt(request, said[action])) !== undefined) rerender();
}

function columnUnder(x, y) {
  const element = document.elementFromPoint(x, y);
  return element ? element.closest("[data-drop]") : null;
}

let dragMoved = false;

const DRAG_THRESHOLD = 6; // pixels, so a click stays a click

document.addEventListener("pointerdown", (event) => {
  const card = event.target.closest?.(".board-card.movable");
  if (!card || event.button !== 0 || event.target.closest(".status-pick")) return;
  // Wait for real movement: below the threshold this is a click, which opens
  // the task instead.
  const from = { x: event.clientX, y: event.clientY };
  const maybe = (moving) => {
    if (Math.hypot(moving.clientX - from.x, moving.clientY - from.y) < DRAG_THRESHOLD) return;
    stop();
    startDrag(card, moving);
  };
  const stop = () => {
    document.removeEventListener("pointermove", maybe);
    document.removeEventListener("pointerup", stop);
  };
  document.addEventListener("pointermove", maybe);
  document.addEventListener("pointerup", stop);
});

document.addEventListener("click", (event) => {
  if (!event.target.closest?.(".board-card")) return;
  // A click that ended a drag is not a click on the card.
  if (dragMoved) {
    dragMoved = false;
    event.stopPropagation();
  }
});

document.addEventListener("keydown", (event) => {
  const card = event.target.closest?.(".board-card.movable");
  if (!card || event.key !== "m") return;
  event.preventDefault();
  offerMoves(card);
});

document.addEventListener("change", (event) => {
  const picker = event.target.closest?.(".status-pick");
  if (picker) {
    const action = picker.value;
    picker.value = "";
    if (action) moveCard(picker.dataset.run, action);
    return;
  }
  const filter = { "f-board-project": "project", "f-board-effort": "effort", "f-board-label": "label" }[
    event.target.id
  ];
  if (!filter) return;
  Board[filter] = event.target.value;
  rerender();
});

function startDrag(card, event) {
  const status = card.dataset.status;
  const runId = card.dataset.run;
  const ghost = card.cloneNode(true);
  ghost.classList.add("ghost");
  ghost.style.width = `${card.offsetWidth}px`;
  document.body.append(ghost);
  card.classList.add("dragging");
  const columns = [...document.querySelectorAll("[data-drop]")];
  columns.forEach((column) => {
    const action = column.dataset.drop;
    column.classList.toggle("droppable", Boolean(action) && allowedDrop(status, action));
    column.classList.toggle("not-droppable", Boolean(action) && !allowedDrop(status, action));
  });
  dragMoved = false;
  const place = (x, y) => {
    ghost.style.left = `${x + 12}px`;
    ghost.style.top = `${y + 12}px`;
  };
  place(event.clientX, event.clientY);
  let over = null;
  const move = (moving) => {
    dragMoved = true;
    place(moving.clientX, moving.clientY);
    const column = columnUnder(moving.clientX, moving.clientY);
    const action = column?.dataset.drop;
    const valid = Boolean(action) && allowedDrop(status, action);
    if (over && over !== column) over.classList.remove("over");
    over = valid ? column : null;
    if (over) over.classList.add("over");
  };
  const finish = (ending) => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", finish);
    document.removeEventListener("keydown", escape);
    ghost.remove();
    card.classList.remove("dragging");
    columns.forEach((column) => column.classList.remove("droppable", "not-droppable", "over"));
    if (!ending || !over) return;
    moveCard(runId, over.dataset.drop);
  };
  const escape = (key) => {
    if (key.key !== "Escape") return;
    over = null;
    finish(null);
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", finish);
  document.addEventListener("keydown", escape);
}

// The same moves without a mouse: focus a card and press m.
function offerMoves(card) {
  const status = card.dataset.status;
  const runId = card.dataset.run;
  const moves = [
    ["pause", "Pause it", "Agents stop at the next step boundary."],
    ["resume", "Resume it", "Agents pick the task up again."],
    ["cancel", "Close it", "It will not continue; the work so far is kept."],
  ].filter(([action]) => allowedDrop(status, action));
  if (!moves.length) {
    toast("This task has finished, so it cannot be moved.");
    return;
  }
  modal(
    "Move this task",
    `<p class="muted">Or drag the card to that column, or use the status box on the card.</p>${moves
      .map(([action, label, about]) => `<div class="setting-row"><div><h3>${esc(label)}</h3><p>${esc(about)}</p></div>${btn(label, `move:${runId}:${action}`, action === "cancel" ? "danger" : "primary")}</div>`)
      .join("")}`,
    `<div class="button-row">${btn("Cancel", "close")}</div>`
  );
}

// ---- the drawer: read and act on a task without leaving the board ----

async function peek(runId) {
  const holder = document.createElement("aside");
  holder.className = "drawer";
  holder.innerHTML = `<p class="muted">Loading…</p>`;
  document.body.append(holder);
  const close = () => holder.remove();
  document.addEventListener("keydown", function escape(event) {
    if (event.key !== "Escape") return;
    document.removeEventListener("keydown", escape);
    close();
  });
  let run;
  let controls = [];
  try {
    [run, controls] = await Promise.all([api(`/runs/${runId}`), api(`/runs/${runId}/controls`)]);
  } catch (error) {
    if (!(error instanceof SignedOut)) holder.innerHTML = `<p class="error-text">${esc(explain(error))}</p>`;
    return;
  }
  Drawer.run = run;
  TaskRoom.run = run;
  // A paused task may be waiting on the owner rather than merely stopped.
  Drawer.ceiling = await ceilingQuestion(runId, run.status);
  const directions = controls.filter((c) => c.kind === "instruction").slice(-3);
  const messages = roleMessages(run).slice(-2).reverse();
  holder.innerHTML = `
    <header class="drawer-head">
      <div>${statusTag(run.status)}<span class="muted"> · ${esc(run.workflow_name === "agent_research" ? "research task" : "office task")}</span></div>
      ${btn("Close", "drawer-close", "", 'aria-label="Close panel"')}
    </header>
    <h2>${esc(run.title || run.objective)}</h2>
    ${run.error ? `<p class="muted">${esc(stopText(run.error))}</p>` : ""}
    ${ceilingCard(Drawer.ceiling)}
    <div class="button-row">
      <a class="button primary" href="#task/${esc(run.id)}">Open task room ↗</a>
      ${run.project_id ? `<a class="button" href="#room/${esc(run.project_id)}">Open the room</a>` : ""}
      ${PAUSABLE.includes(run.status) ? btn("Pause", "drawer-pause") : ""}
      ${run.status === "PAUSED" && !Drawer.ceiling ? btn("Resume", "drawer-resume") : ""}
      ${run.status === "WAITING_FOR_APPROVAL" ? btn("Review proposal", "approval") : ""}
      ${["NEEDS_HUMAN_REVIEW", "BLOCKED", "FAILED"].includes(run.status) ? btn("Decide", "review") : ""}
    </div>
    <h3>Labels</h3>
    <form id="labels-form" class="compose">
      <input name="label" maxlength="32" placeholder="Add a label" aria-label="Add a label">
      <button>Add</button>
    </form>
    <p class="labels">${(run.labels || []).map((label) => `<span class="tag">${esc(label)} <button type="button" class="chip-x" data-action="label-remove:${esc(label)}" aria-label="Remove ${esc(label)}">×</button></span>`).join("") || `<span class="muted">None yet.</span>`}</p>
    <h3>Latest from the team</h3>
    ${messages.length ? messages.map((m) => `<div class="drawer-message"><strong>${esc(m.who)}</strong> <span class="muted">· ${esc(m.role)}</span><p>${esc((m.text || "").slice(0, 320))}</p></div>`).join("") : `<p class="muted">Nothing posted yet.</p>`}
    ${directions.length ? `<h3>Your direction</h3>${directions.map((c) => `<div class="drawer-message"><p>${esc(c.text)}</p><span class="muted">${esc(c.target_node ? `for ${NODE_LABEL[c.target_node] || c.target_node}` : "for the whole team")} · ${esc(c.status.toLowerCase())}</span></div>`).join("")}` : ""}
  `;
}

// A task that reached a ceiling is paused with a question, not failed. The
// choices offered depend on which ceiling: a cheaper model saves money, so it
// is worth offering for a cost ceiling and misleading for a token one.
const CEILING = {
  tokens: {
    title: "It reached its token limit",
    what: "Tokens are the amount of text the team read and wrote. A cheaper model would not use fewer of them, so the choice is a bigger allowance or stopping here.",
    unit: "tokens",
    field: "max_tokens",
  },
  cost: {
    title: "It reached its spending limit",
    what: "Raise the limit to let it finish. If this project has a cheaper model set under its settings, the task moves to it automatically past the amount you set there.",
    unit: "dollars",
    field: "max_cost_usd",
  },
  time: {
    title: "It reached its time limit",
    what: "The task ran longer than the minutes allowed. Give it more minutes to finish what it started.",
    unit: "minutes",
    field: "max_minutes",
  },
};

// The open question and the task it belongs to, shared by every screen that
// shows the panel. It lived on the drawer once, which meant the same buttons
// did nothing in the task room.
const Ceiling = { question: null, runId: null };

async function ceilingQuestion(runId, status) {
  // The office decides whether a question is open; a long run has more events
  // than one page and a screen should not have to scan them.
  Ceiling.question = null;
  Ceiling.runId = runId;
  if (status !== "PAUSED") return null;
  let asked = null;
  try {
    asked = await api(`/runs/${runId}/question`);
  } catch (error) {
    if (error instanceof SignedOut) throw error;
    return null;
  }
  Ceiling.stuck = asked?.kind === "stuck" ? asked : null;
  if (!asked || asked.kind !== "budget") return null;
  const kind = CEILING[asked.limit] ? asked.limit : "tokens";
  Ceiling.question = { kind, node: asked.node, budget: asked.budget || {}, spent: asked.spent || {}, looping: asked.looping || null };
  return Ceiling.question;
}

function ceilingAmounts(question) {
  const shape = CEILING[question.kind];
  const limit = Number(question.budget[shape.field] || 0);
  const spent =
    question.kind === "cost"
      ? Number(question.spent.cost_usd || 0)
      : question.kind === "time"
        ? null
        : Number(question.spent.tokens || 0);
  // Half as much again, rounded to something a person would type.
  const step = question.kind === "cost" ? Math.max(0.5, Math.ceil(limit) / 2) : Math.round(limit / 2);
  return { shape, limit, spent, suggested: question.kind === "cost" ? Number((limit + step).toFixed(2)) : limit + step };
}

// What the owner can do at a ceiling, the same wherever it is asked:
// their own amount (the suggestion is only filled in), finishing with the work
// done so far, or stopping without an answer.
function ceilingChoices(runId, question) {
  const { shape, limit, spent, suggested } = ceilingAmounts(question);
  const cost = question.kind === "cost";
  const floor = Math.max(limit, spent || 0);
  const step = cost ? "0.01" : "1";
  return `<form class="ceiling-own" data-form="ceiling-own" data-run="${esc(runId)}" data-kind="${esc(question.kind)}" data-floor="${esc(floor)}">
      <label><span>New limit in ${esc(shape.unit)}</span>
        <input type="number" name="amount" value="${esc(suggested)}" min="${esc(cost ? (floor + 0.01).toFixed(2) : floor + 1)}" step="${step}" required aria-label="New limit in ${esc(shape.unit)}"></label>
      ${question.looping ? `<label class="ceiling-hint"><span>A hint for it (optional)</span><input name="hint" maxlength="4000" placeholder="For example: use the sitemap instead"></label>` : ""}
      <button class="primary">Raise and continue</button>
    </form>
    <div class="button-row tight">
      ${btn("Finish with what it has", `ceiling-finish:${runId}`)}
      ${btn("Stop without an answer", `ceiling-stop:${runId}`, "room-tool")}
    </div>
    <p class="muted ceiling-help">Finish writes the answer from the work done so far, in one last writing pass, with no more research or replanning. Stop keeps what exists but writes no answer.</p>`;
}

// What a browser error means, in words.
const STUCK_ERROR = {
  action_timed_out: "the page did not respond in time",
  element_not_found: "what it pressed was no longer on the page",
  navigation_failed: "the page would not open",
};

function stuckWhy(stuck) {
  const tries = Number(stuck.tries || 0);
  const error = STUCK_ERROR[stuck.error] || String(stuck.error || "it did not work").replace(/_/g, " ");
  return `It tried to ${stuck.what || "get on"}${tries > 1 ? ` ${tries} times` : ""}, and ${error}.`;
}

// A task that cannot get on asks for help, not money: a hint it follows
// from here, skipping that part, trying again, or finishing with what it has.
function stuckCard(runId, stuck) {
  if (!stuck) return "";
  return `<div class="card attention stuck-card" data-stuck>
    <h3>Stuck: it needs your help</h3>
    <p>${esc(stuckWhy(stuck))} It stopped instead of trying the same thing again, and kept everything it found so far.</p>
    ${stuck.brief ? `<p class="muted">The step: ${esc(stuck.brief)}</p>` : ""}
    <form class="stuck-hint" data-form="stuck-hint" data-run="${esc(runId)}">
      <label class="field" for="stuck-hint-${esc(runId)}">Give it a hint</label>
      <textarea id="stuck-hint-${esc(runId)}" name="text" rows="2" maxlength="4000" required placeholder="For example: the case studies are listed at /library, or use the sitemap."></textarea>
      <button class="primary">Send hint and continue</button>
    </form>
    <div class="button-row tight">
      ${btn("Skip this part", `stuck-help:${runId}:skip`)}
      ${btn("Try again as it is", `stuck-help:${runId}:retry`)}
      ${btn("Finish with what it has", `ceiling-finish:${runId}`)}
    </div>
  </div>`;
}

function ceilingCard(question) {
  if (!question) return "";
  if (question.looping) {
    // Spent going in circles: more budget alone would buy more of the same.
    const circling = `<p class="ceiling-looping"><strong>It was going in circles.</strong> ${esc(stuckWhy(question.looping))} More budget alone would likely repeat that: add a hint below, or finish with what it has.</p>`;
    return ceilingCardPlain(question).replace("<!--looping-->", circling);
  }
  return ceilingCardPlain(question);
}

function ceilingCardPlain(question) {
  const { shape, limit, spent } = ceilingAmounts(question);
  const show = (value) => (question.kind === "cost" ? money(value) : Number(value).toLocaleString());
  const where = question.node ? ` at ${esc(NODE_LABEL[question.node] || question.node)}` : "";
  return `<div class="card attention" data-ceiling>
    <h3>${esc(shape.title)}</h3>
    <p class="muted">It stopped${where} and kept everything it had done. Nothing is lost; it carries on from the same place.</p>
    <div class="detail-line"><span>Used</span><strong>${spent === null ? "—" : show(spent)} of ${show(limit)} ${esc(shape.unit)}</strong></div>
    <p class="muted">${esc(shape.what)}</p>
    <!--looping-->
    ${ceilingChoices(Ceiling.runId, question)}
  </div>`;
}

// Answered from the task, the drawer, the desk or a project room: redraw where it was asked.
async function ceilingAnswered() {
  closeModal();
  document.querySelector(".drawer")?.remove();
  if (Office.view === "room" && typeof roomRefresh === "function") {
    await roomRefresh();
    roomDrawWaiting();
    return;
  }
  rerender();
}

const Drawer = { run: null, ceiling: null };

async function raiseCeiling(runId, kind, amount) {
  if (!runId || !CEILING[kind]) return toast("Reopen the task and try again.", true);
  if (!Number.isFinite(amount) || amount <= 0) return toast("Give a number above zero.", true);
  const body = { [CEILING[kind].field]: amount };
  if ((await attempt(() => api(`/runs/${runId}/budget`, { method: "POST", body }), "Raised. It is carrying on from where it stopped.")) === undefined) return;
  await ceilingAnswered();
}

Office.forms = Object.assign(Office.forms || {}, {
  "ceiling-own": async (form) => {
    const kind = form.dataset.kind || "";
    const typed = Number(new FormData(form).get("amount"));
    const amount = kind === "cost" ? Number(typed.toFixed(2)) : Math.round(typed);
    if (Number.isFinite(amount) && amount <= Number(form.dataset.floor || 0)) {
      toast("Choose more than it has already used, or it would stop again at once.", true);
      return;
    }
    const hint = String(new FormData(form).get("hint") || "").trim();
    if (hint) {
      // Sent first, so the step that resumes is the one that reads it.
      const sent = await attempt(() => api(`/runs/${form.dataset.run}/controls`, { method: "POST", body: { kind: "instruction", text: hint } }));
      if (sent === undefined) return;
    }
    await raiseCeiling(form.dataset.run || "", kind, amount);
  },
  "stuck-hint": async (form) => {
    const text = String(new FormData(form).get("text") || "").trim();
    if (!text) return;
    const done = await attempt(() => api(`/runs/${form.dataset.run}/help`, { method: "POST", body: { action: "hint", text } }), "Sent. It is carrying on with your hint.");
    if (done !== undefined) await ceilingAnswered();
  },
  // Registered like every other form, so the page never submits natively.
  "labels-form": async (form) => {
    const label = String(new FormData(form).get("label") || "").trim();
    const run = Drawer.run;
    if (!label || !run) return;
    const labels = [...(run.labels || []), label];
    if ((await attempt(() => api(`/runs/${run.id}/labels`, { method: "PUT", body: { labels } }), "Label added.")) === undefined) return;
    document.querySelector(".drawer")?.remove();
    await rerender();
    peek(run.id);
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "runs-open": () => {
    const moving = Office.moving || [];
    if (moving.length === 1) return go("task", moving[0].run_id);
    go("board");
  },
  move: (arg) => {
    const [runId, action] = arg.split(":");
    closeModal();
    moveCard(runId, action);
  },
  peek,
  "drawer-close": () => document.querySelector(".drawer")?.remove(),
  "drawer-pause": async () => {
    if ((await control("pause")) !== undefined) {
      document.querySelector(".drawer")?.remove();
      rerender();
    }
  },
  "stuck-help": async (arg) => {
    const [runId, action] = String(arg).split(":");
    const said = action === "skip" ? "Skipped. It is carrying on without that part." : "Trying again. It will not repeat what failed.";
    const done = await attempt(() => api(`/runs/${runId}/help`, { method: "POST", body: { action } }), said);
    if (done !== undefined) await ceilingAnswered();
  },
  "ceiling-finish": async (runId) => {
    if (!runId) return toast("Reopen the task and try again.", true);
    const done = await attempt(
      () => api(`/runs/${runId}/finish`, { method: "POST" }),
      "Finishing. The Chief is writing the answer from the work done so far."
    );
    if (done !== undefined) await ceilingAnswered();
  },
  "ceiling-stop": async (runId) => {
    if (!runId) return toast("Reopen the task and try again.", true);
    if (!window.confirm("Stop without an answer? What it has done stays readable, but no answer is written. Finish with what it has writes one.")) return;
    if ((await attempt(() => api(`/runs/${runId}/controls`, { method: "POST", body: { kind: "cancel" } }), "Stopped.")) === undefined) return;
    await ceilingAnswered();
  },
  "drawer-resume": async () => {
    if ((await attempt(() => api(`/runs/${Drawer.run.id}/resume`, { method: "POST" }), "Resumed.")) !== undefined) {
      document.querySelector(".drawer")?.remove();
      rerender();
    }
  },
  "label-remove": async (label) => {
    const labels = (Drawer.run.labels || []).filter((one) => one !== label);
    const saved = await attempt(() => api(`/runs/${Drawer.run.id}/labels`, { method: "PUT", body: { labels } }), "Label removed.");
    if (saved === undefined) return;
    const id = Drawer.run.id;
    document.querySelector(".drawer")?.remove();
    await rerender();
    peek(id);
  },
});

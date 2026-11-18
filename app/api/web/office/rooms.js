"use strict";

// Project rooms: the owner talking with the office's agents, and the project's
// tasks reporting into the same conversation as they run.
//
// The room is drawn once and then only its conversation is redrawn, so what
// the owner is typing survives every refresh. What is new arrives by cursor.

const RoomState = { project: null, agents: [], mentionAt: -1, mentionPick: 0, timer: null, tab: "messages", documents: null, replyTo: null, thread: null };
// Messages from one author this close together read as one turn.
const ROOM_GROUP_MS = 5 * 60 * 1000;
// An answer quotes what it answers unless that is among the last few lines.
const ROOM_QUOTE_NEAR = 6;
// A discussion (topic) is read by cursor; a project's room is its first topic.
const roomReader = new RoomReader((topicId, cursor) =>
  api(`/topics/${topicId}/feed?after_event=${cursor.after_event}&after_message=${cursor.after_message}`)
);
const ROOM_POLL_MS = 2500;
const ROOM_STATUS = { answered: ["Answered", ""], partial: ["In part", "amber"], unanswered: ["Unanswered", "amber"] };

const ROOM_DIRECTION = {
  QUEUED: ["Queued", "gray", "waiting for the task's next step"],
  RECEIVED: ["Received", "blue", "the task has it"],
  APPLIED: ["Applied", "", "the task took it in"],
};

Object.assign(ERRORS, {
  room_too_many_agents: "Up to six agents can answer one message. Name fewer, or send a second message.",
  already_done: "That button has already been used.",
  office_paused: "New work is paused in Settings, so the task was not started.",
  monthly_limit_reached: "This month's spending limit is reached, so the task was not started.",
});

// ---- buttons on a message ----

function roomShort(text, limit) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  return clean.length > limit ? `${clean.slice(0, limit - 1)}…` : clean;
}

// What each of the office's tools is called while it runs and once it has.
const ROOM_STEP_WORDS = {
  search: ["Searching the web", "Searched the web"],
  read: ["Reading a page", "Read a page"],
  look: ["Looking at a page", "Looked at a page"],
  search_memory: ["Searching your AI history", "Searched your AI history"],
  get_conversation: ["Reading an earlier conversation", "Read an earlier conversation"],
  todo: ["Updating its list", "Updated its list"],
  delegate: ["Handing out work", "Handed out work"],
  review: ["Reviewing the work", "Reviewed the work"],
};

function roomAddressHost(address) {
  try {
    return new URL(address).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function roomStepWords(step) {
  const tool = String(step?.tool || "");
  const running = step?.status === "running";
  const known = ROOM_STEP_WORDS[tool];
  if (known) {
    const host = ["read", "look"].includes(tool) && step.source ? roomAddressHost(step.source) : "";
    return host ? `${known[1].replace("a page", "")}${host}` : known[running ? 0 : 1];
  }
  const parts = tool.startsWith("mcp:") ? tool.slice(4).split(":").join(" · ") : tool.replaceAll("_", " ");
  return `${running ? "Using" : "Used"} ${parts}`;
}

// Runs of the same thing are one line; how each went is always said.
function roomStepsHtml(steps) {
  const lines = [];
  (Array.isArray(steps) ? steps : []).forEach((step) => {
    const words = roomStepWords(step);
    const status = String(step?.status || "");
    const reason = String(step?.reason || "");
    const last = lines[lines.length - 1];
    if (last && last.words === words && last.status === status && last.reason === reason) last.count += 1;
    else lines.push({ words, status, reason, count: 1 });
  });
  if (!lines.length) return "";
  const rows = lines.map(({ words, status, reason, count }) => {
    const times = count > 1 ? ` <span class="muted">×${count}</span>` : "";
    if (status === "ok") return `<li class="room-step">${composerIcon("check")}<span>${esc(words)}${times}</span></li>`;
    if (status === "running") return `<li class="room-step running"><i class="side-spin" aria-hidden="true"></i><span>${esc(words)}…${times}</span></li>`;
    if (status === "ask") return `<li class="room-step asks"><b aria-hidden="true">?</b><span>${esc(words)}${times}: asks you first</span></li>`;
    const why = status === "stopped" ? "did not finish" : `did not work${reason ? ` (${esc(reason)})` : ""}`;
    return `<li class="room-step bad"><b aria-hidden="true">!</b><span>${esc(words)}${times}: ${why}</span></li>`;
  });
  return `<ul class="room-steps plain">${rows.join("")}</ul>`;
}

// Only what a tool really returned reaches this list (checked in the lead's citations).
function roomSourcesHtml(action) {
  const addresses = (action.addresses || []).map((address) => {
    const text = String(address);
    return /^https?:\/\//i.test(text)
      ? `<a class="room-source" href="${esc(text)}" target="_blank" rel="noopener noreferrer" title="${esc(text)}">${esc(roomAddressHost(text) || text)}</a>`
      : `<span class="room-source" title="${esc(text)}">${esc(text.split("/").pop() || text)}</span>`;
  });
  const conversations = (action.conversations || []).map((ref) => btn("AI history", `archive-open:${ref}`, "room-source"));
  const all = [...addresses, ...conversations];
  return all.length ? `<div class="room-sources"><span class="muted">Sources</span>${all.join("")}</div>` : "";
}

function roomMessageActions(item, withVerdict = true) {
  const id = String(item.id || "").replace(/^m:/, "");
  const parts = (item.actions || []).map((action, index) => {
    if (action.kind === "attached") {
      return `<p class="room-attached">Attached <a href="#knowledge">${esc(action.name)}</a> <span class="muted">· ${esc(action.documents)} document${action.documents === 1 ? "" : "s"}</span></p>`;
    }
    if (action.kind === "steps") return roomStepsHtml(action.steps);
    if (action.kind === "sources") return roomSourcesHtml(action);
    if (action.kind === "tools_used") {
      // What the agent called before replying: named, never hidden.
      const calls = (action.calls || []).map((c) => `${esc(c.server)} · ${esc(c.tool)}${c.outcome === "ok" ? "" : ` (${esc(c.outcome || "refused")})`}`).join(", ");
      return calls ? `<p class="room-tools-used">${composerIcon("tool")}<span>Used ${calls}</span></p>` : "";
    }
    if (action.kind === "routine_stopped") {
      return `<p class="room-action-done">${tag("Stopped")} ${esc(action.title)}</p>`;
    }
    if (action.kind === "routine_create" || action.kind === "routine_update") {
      if (action.routine_id) return `<p class="room-action-done">${tag("Set")} ${esc(action.title)} <a href="#routines">Routines</a></p>`;
      const details = action.kind === "routine_create"
        ? `${esc(action.when)}. It does this on its own, reports here and counts against your monthly limit.<br>${esc(roomShort(action.prompt, 400))}`
        : `Changes: ${esc(roomShort(JSON.stringify({ ...action.changes, id: undefined }), 400))}`;
      return `<div class="room-proposed-call"><span class="floor-room-label">Asks you first</span><strong>${esc(action.title)}</strong><p>${details}</p>${btn(action.kind === "routine_create" ? "Set this routine" : "Apply this change", `room-start-now:${id}:${index}`, "room-action primary")}</div>`;
    }
    if (action.kind === "tool_call") {
      // Proposed by the agent; this tool asks the owner first, so only a press runs it.
      if (action.result_id) return `<p class="room-action-done">${tag("Ran")} ${esc(action.tool)} on ${esc(action.server)}</p>`;
      const args = JSON.stringify(action.arguments || {});
      return `<div class="room-proposed-call"><span class="floor-room-label">Asks you first</span><strong>${esc(action.tool)} on ${esc(action.server)}</strong><code>${esc(args.length > 240 ? `${args.slice(0, 240)}…` : args)}</code>${btn(action.running ? "Running…" : "Allow this call", `room-tool-call:${id}:${index}`, "room-action primary", action.running ? "disabled" : "")}</div>`;
    }
    if (action.kind === "record_decision") {
      return btn("Record as decision", `topic-decide:${item.reply_to || ""}`, "room-action");
    }
    if (action.kind === "rework") {
      if (action.done) return `<p class="room-action-done">${tag("Reworking")} the work, with research</p>`;
      return btn(esc(action.label || "Redo it with research"), `room-rework:${id}:${index}`, "room-action");
    }
    if (action.kind === "open_topic") {
      return `<a class="button room-action" href="#topic/${esc(action.topic_id)}">Open the new discussion →</a>`;
    }
    if (action.kind === "start_work") {
      if (action.run_id) return `<p class="room-action-done">${tag("Started")} <a href="#task/${esc(action.run_id)}">${esc(roomShort(action.objective, 90))}</a></p>`;
      return btn("Start now", `room-start-now:${id}:${index}`, "room-action primary");
    }
    if (action.kind === "start_task") {
      if (action.run_id) {
        return `<p class="room-action-done">${tag("Started")} <a href="#task/${esc(action.run_id)}">${esc(roomShort(action.objective, 90))}</a></p>`;
      }
      const label = `＋ Start this: ${esc(roomShort(action.objective, 80))}`;
      return btn(label, `room-start:${id}:${index}`, "room-action");
    }
    if (action.kind === "add_event") {
      // Proposed by an agent; only the owner's press adds it.
      const when = `${new Date(action.starts_at).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}–${new Date(action.ends_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
      if (action.change_id) return `<p class="room-action-done">${tag("Added")} ${esc(action.title)} · ${esc(when)} · <a href="#calendar/week/${esc(calKey(new Date(action.starts_at)))}">Open calendar</a></p>`;
      return `<div class="room-proposed-event"><span class="floor-room-label">Proposed for your calendar</span><strong>${esc(action.title)}</strong><span>${esc(when)}</span>${btn("Add to my calendar…", `room-calendar:${id}:${index}`, "room-action primary")}</div>`;
    }
    if (action.kind === "send_as_direction") {
      if (action.control_id) {
        const [label, cls, meaning] = ROOM_DIRECTION[action.status] || [String(action.status || "").toLowerCase(), "gray", ""];
        return `<p class="room-action-done">${tag(label, cls)} Sent to <a href="#task/${esc(action.run_id)}">${esc(action.task || "the task")}</a>${meaning ? ` · <span class="muted">${esc(meaning)}</span>` : ""}</p>`;
      }
      // A task that has finished cannot take direction; no button that can only be refused.
      if (action.open === false) return "";
      const label = `Send as direction to ${esc(roomShort(action.task, 60))}`;
      return btn(label, `room-direct:${id}:${index}`, "room-action");
    }
    return "";
  });
  // The owner's verdict on an agent's reply (3.0).
  const verdict = withVerdict && typeof roomVerdictRow === "function" ? roomVerdictRow(item) : "";
  if (verdict) parts.push(verdict);
  return parts.length ? `<div class="room-actions">${parts.join("")}</div>` : "";
}

function roomFindAction(arg) {
  const [id, index] = String(arg).split(":");
  const item = roomReader.items.find((entry) => entry.id === `m:${id}`);
  const action = item?.actions?.[Number(index)];
  if (!action) console.error("Room action not found:", arg);
  return { id, index: Number(index), item, action };
}

async function roomPress(arg, body) {
  const { id, index, item } = roomFindAction(arg);
  const topicId = RoomState.topic?.id;
  if (!item || !topicId) return undefined;
  const done = await attempt(() => api(`/topics/${topicId}/messages/${id}/actions/${index}`, { method: "POST", body }));
  if (done?.action) {
    // The cursor only brings new items, so the pressed one is updated here.
    item.actions[index] = { ...done.action, status: done.action.control_id ? "QUEUED" : undefined };
    roomDraw();
  }
  return done;
}

// Where sent directions have got to. The cursor does not revisit old
// messages, so a direction still on its way is read from its task's queue.
async function roomRefreshDirections() {
  const waiting = roomReader.items.flatMap((item) => (item.actions || []).filter((a) => a.control_id && a.status !== "APPLIED"));
  if (!waiting.length) return false;
  let changed = false;
  for (const runId of [...new Set(waiting.map((a) => a.run_id))]) {
    const controls = await api(`/runs/${runId}/controls`).catch(() => []);
    if (!Array.isArray(controls)) continue;
    for (const action of waiting.filter((a) => a.run_id === runId)) {
      const now = controls.find((c) => c.id === action.control_id)?.status;
      if (now && now !== action.status) {
        action.status = now;
        changed = true;
      }
    }
  }
  return changed;
}

// ---- the list of rooms ----

async function renderRoomList(content) {
  const projects = await api("/projects");
  if (!Array.isArray(projects)) throw new Error("could not load projects");
  const active = projects.filter((p) => p.status === "ACTIVE");
  Office.rooms = active;
  renderNav();
  content.innerHTML =
    heading("ROOMS", "Talk with the team, where the work is.", "Every project has a room. Ask an agent with @, or ask the Chief by naming no one.", btn("＋ New room", "room-new", "primary")) +
    (active.length
      ? `<div class="grid3">${active
          .map(
            (p) => `<a class="card room-card" href="#room/${esc(p.id)}"><h2><span class="muted">#</span> ${esc(p.name)}</h2><p>${esc(p.description || "No description yet.")}</p></a>`
          )
          .join("")}</div>`
      : emptyCard("No rooms yet.", "A room comes with every project. Make one to start talking with the team.", btn("＋ New room", "room-new", "primary")));
}

// ---- one room ----

function roomAuthorAvatar(author) {
  if (author.kind === "office") return `<span class="avatar room-office" aria-hidden="true">qd</span>`;
  if (author.kind === "owner") return `<span class="avatar owner" aria-hidden="true">${esc((author.name || "O").slice(0, 1).toUpperCase())}</span>`;
  return avatar(author.name || author.role, undefined).replace("<span", '<span aria-hidden="true"');
}

function roomCost(value) {
  if (value === null || value === undefined) return "";
  return value < 1 ? `${(value * 100).toFixed(value < 0.1 ? 1 : 0)}¢` : money(value);
}

function roomRole(author) {
  return author.kind === "agent" ? ROLE_LABEL[author.role] || author.role || "" : "";
}

function roomCard(card, runId) {
  if (!card) return "";
  const open = runId ? `<a href="#task/${esc(runId)}">Open the task</a>` : "";
  if (card.stage === "plan" && card.questions?.length) {
    return `<div class="room-card-body"><ol>${card.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ol>${
      card.sources?.length ? `<p class="muted">${card.sources.length} official source${card.sources.length === 1 ? "" : "s"} to read · ${open}</p>` : `<p class="muted">${open}</p>`
    }</div>`;
  }
  if (card.stage === "research" && card.answers?.length) {
    const answers = card.answers
      .map((a) => {
        const [label, cls] = ROOM_STATUS[a.status] || [a.status, "gray"];
        return `<li>${tag(label, cls)} ${esc(a.question)}</li>`;
      })
      .join("");
    const refused = (card.refused || []).map((r) => `${esc(r.url)} (${esc(r.status)})`).join(", ");
    return `<div class="room-card-body"><ul class="plain room-answers">${answers}</ul><p class="muted">${esc(card.sources || 0)} sources${refused ? ` · refused us: ${refused}` : ""} · ${open}</p></div>`;
  }
  if (card.stage === "critique" && card.verdict) {
    const verdict = card.verdict === "PASS" ? tag("Passed") : card.verdict === "REVISE" ? tag(`Revise · ${card.target || "work"}`, "amber") : tag("Stopped", "clay");
    return `<div class="room-card-body">${verdict}${card.issues?.length ? `<ul>${card.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}<p class="muted">${open}</p></div>`;
  }
  if (card.stage === "synthesis" && card.recommendation) {
    return `<div class="room-card-body"><p class="room-answer">${esc(card.recommendation)}</p><p class="muted">${open}</p></div>`;
  }
  // Nothing to show but the way to the task, which the line above already names.
  return "";
}

// A long reply is often a draft worth keeping: it can be opened in the editor
// as a document of its own.
const ROOM_LONG_CHARS = 280;
const ROOM_COPIED_MS = 1500;
function roomLong(item, mine) {
  const text = String(item.text || "");
  return item.kind === "message" && !mine && (text.length >= ROOM_LONG_CHARS || text.split("\n").length >= 6);
}

// Under a message, as assistant-ui's action bar: quiet until it is wanted.
function roomActionBar(item, id, mine, author) {
  const verdict = typeof roomVerdictRow === "function" ? roomVerdictRow(item) : "";
  return `<div class="room-tools">${iconBtn("copy", "Copy", `room-copy:${id}`)}${verdict}${iconBtn("reply", "Reply", `room-reply:${id}`)}${iconBtn("thread", "Reply in thread", `room-thread:${id}`)}${iconBtn("work", "Branch to a new topic", `topic-branch:${id}`)}${mine ? iconBtn("decision", "Record as decision", `topic-decide:${id}`) : ""}${author.kind === "agent" ? iconBtn("calls", "Model calls", `room-calls:${id}`) : ""}${roomLong(item, mine) ? iconBtn("edit", "Open in editor", `room-open-doc:${id}`) : ""}</div>`;
}

function roomItem(item) {
  // A routine's run: one line, its instructions folded (room_days.js).
  if (item.routine && typeof roomRunItem === "function") return roomRunItem(item);
  const author = item.author || {};
  const role = roomRole(author);
  const meta = [when(item.at), role, roomCost(item.cost_usd)].filter(Boolean).map(esc).join(" · ");
  // The task a step belongs to, named once and short, as the way into it.
  const task =
    item.kind === "step" && item.run_id
      ? ` · <a class="room-task" href="#task/${esc(item.run_id)}" title="${esc(item.task || "")}">${esc(item.task || "Open the task")}</a>`
      : item.kind === "message" && item.run_id
        ? ` · about <button type="button" class="room-task room-open-team" data-action="team-room:${esc(item.run_id)}" title="${esc(item.task || "")}">${esc(item.task || "a task")}</button>`
        : "";
  if (item.kind === "huddle") {
    // A huddle of one task's team, in one line; the whole of it is on its Team room tab.
    return `<li class="room-line room-huddle"><span class="room-line-mark" aria-hidden="true">⇄</span><span><strong>${esc(item.task || "A task")}</strong> · ${esc(item.text)}</span><span class="muted">${esc(when(item.at))}</span>${
      item.run_id ? `<button type="button" class="room-open-team" data-action="team-room:${esc(item.run_id)}">Read it</button>` : ""
    }</li>`;
  }
  if (item.kind === "task") {
    // History only. Whether a question is still open is shown under "Waiting
    // for you", read fresh; a line here saying "answer it" would outlive the answer.
    // A call the office stopped in the middle of: the owner says what happened (5c).
    const settle = item.effect_id
      ? btn("It went through", `effect-settle:${item.effect_id}:done`, "room-tool") + btn("It did not", `effect-settle:${item.effect_id}:failed`, "room-tool")
      : "";
    return `<li class="room-line"><span class="room-line-mark" aria-hidden="true">·</span><span>${esc(item.text)}</span><span class="muted">${esc(when(item.at))}</span>${settle}${
      item.run_id ? `<a href="#task/${esc(item.run_id)}">Open</a>` : ""
    }</li>`;
  }
  const mine = author.kind === "owner";
  const id = String(item.id || "");
  const replies = RoomState.threads?.get(id) || [];
  const tools = item.kind === "message" ? roomActionBar(item, id, mine, author) : "";
  return `<li class="room-message ${esc(author.kind || "")}${mine ? " mine" : ""}${item._grouped ? " grouped" : ""}" data-mid="${esc(id)}">${
    mine ? "" : item._grouped ? `<span class="room-avatar-gap" aria-hidden="true"></span>` : roomAuthorAvatar(author)
  }<div class="room-said">${
    item._grouped ? "" : `<p class="room-meta"><strong>${esc(mine ? "You" : author.name || "")}</strong> <span class="muted">${meta}${task}</span></p>`
  }<div class="room-bubble">${item._quote ? roomQuote(item._quote) : ""}<p class="room-text">${esc(item.text)}</p>${
    item.kind === "step" ? roomCard(item.card, item.run_id) : ""
  }</div>${roomMessageActions(item, item.kind !== "message")}${replies.length ? roomThreadLink(id, replies) : ""}${tools}</div></li>`;
}

// What a message replies to, short, as a way back to it.
function roomQuote(target) {
  const who = target.author?.kind === "owner" ? "You" : target.author?.name || "Someone";
  return `<button type="button" class="room-quote" data-action="room-jump:${esc(target.id)}"><strong>${esc(who)}</strong> ${esc(roomShort(target.text, 120))}</button>`;
}

// Under a message with a thread: how many replies, who, and when last.
function roomThreadLink(id, replies) {
  const names = [...new Set(replies.map((r) => (r.author?.kind === "owner" ? "You" : r.author?.name)).filter(Boolean))];
  const last = replies[replies.length - 1];
  return `<button type="button" class="room-thread-link" data-action="room-thread:${esc(id)}">${esc(replies.length)} repl${replies.length === 1 ? "y" : "ies"} <span class="muted">· ${esc(names.slice(0, 4).join(", "))} · last ${esc(when(last.at))}</span></button>`;
}

function roomDay(at) {
  const day = new Date(at);
  if (Number.isNaN(day.getTime())) return "";
  const today = new Date();
  const yesterday = new Date(today.getTime() - 86400000);
  if (day.toDateString() === today.toDateString()) return "Today";
  if (day.toDateString() === yesterday.toDateString()) return "Yesterday";
  return day.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}

// The conversation as a chat: thread replies kept out of the main stream,
// a day line between days, one author's run of messages under one name,
// and an answer that quotes what it answers when that is not right above.
function roomLines(items) {
  const byId = new Map(items.map((item) => [item.id, item]));
  RoomState.threads = new Map();
  for (const item of items) {
    if (!item.thread_id) continue;
    const list = RoomState.threads.get(item.thread_id) || [];
    list.push(item);
    RoomState.threads.set(item.thread_id, list);
  }
  const main = items.filter((item) => !item.thread_id);
  const html = [];
  let day = "";
  let previous = null;
  main.forEach((item, index) => {
    const today = roomDay(item.at);
    if (today && today !== day) {
      html.push(`<li class="room-day" role="separator"><span>${esc(today)}</span></li>`);
      day = today;
      previous = null;
    }
    if (item.kind === "delivery") {
      html.push(roomDelivery(item));
      previous = null;
      return;
    }
    const talking = item.kind === "message" || item.kind === "step";
    if (!talking) {
      html.push(roomItem(item));
      previous = null;
      return;
    }
    const target = item.reply_to ? byId.get(item.reply_to) : null;
    const near = main.slice(Math.max(0, index - ROOM_QUOTE_NEAR), index).some((other) => other.id === item.reply_to);
    const quote = target && (item.author?.kind === "owner" || !near) ? target : null;
    const grouped =
      previous &&
      !quote &&
      previous.author?.name === item.author?.name &&
      previous.author?.kind === item.author?.kind &&
      new Date(item.at) - new Date(previous.at) < ROOM_GROUP_MS;
    html.push(roomItem({ ...item, _grouped: Boolean(grouped), _quote: quote }));
    previous = item;
    // Work handed off here shows as a live card after the office's word on it.
    const cards = (roomReader.work || []).filter((w) => w.origin_message_id && (item.reply_to === w.origin_message_id && item.author?.kind === "office"));
    cards.forEach((w) => html.push(roomWorkCard(w)));
    if (cards.length) previous = null;
  });
  return html.join("");
}

// A piece of work handed off in the discussion, as it stands now.
const WORK_STATE = {
  PENDING: ["Queued", "gray"],
  RECOVERING: ["Picking up again", "gray"],
  RUNNING: ["Working", "blue"],
  PAUSED: ["Paused", "amber"],
  COMPLETED: ["Delivered", ""],
  CLOSED: ["Delivered", ""],
  NEEDS_HUMAN_REVIEW: ["Needs your review", "amber"],
  WAITING_FOR_APPROVAL: ["Waiting for your approval", "amber"],
  FAILED: ["Stopped", "clay"],
  CANCELLED: ["Cancelled", "gray"],
};

function roomWorkCard(work) {
  const [label, cls] = WORK_STATE[work.status] || [work.status, "gray"];
  const share = work.steps ? Math.round((100 * work.steps_done) / work.steps) : ["COMPLETED", "CLOSED"].includes(work.status) ? 100 : 8;
  const step = work.steps ? `Step ${Math.min(work.steps_done + 1, work.steps)} of ${work.steps}` : NODE_LABEL[work.step] || "";
  const spend = `${money(work.spent_usd || 0)}${work.max_cost_usd ? ` of ${money(work.max_cost_usd)}` : ""}`;
  const working = ["PENDING", "RUNNING", "RECOVERING"].includes(work.status);
  const actions = [
    working ? btn("Pause", `work-pause:${work.run_id}`, "room-tool") : "",
    work.status === "PAUSED" ? btn("Resume", `work-resume:${work.run_id}`, "room-tool") : "",
    `<a class="button room-tool" href="#task/${esc(work.run_id)}">Open the work</a>`,
  ].join("");
  return `<li class="room-work" data-work="${esc(work.run_id)}"><div class="room-work-card">
    <div class="room-work-top">${working ? '<i class="side-spin" aria-hidden="true"></i>' : `<i class="side-dot ${["COMPLETED", "CLOSED"].includes(work.status) ? "done" : work.status === "FAILED" ? "failed" : ["NEEDS_HUMAN_REVIEW", "WAITING_FOR_APPROVAL", "PAUSED"].includes(work.status) ? "needs" : ""}" aria-hidden="true"></i>`}${work.handed_to ? `<span class="room-work-who">${avatar(work.handed_to)}${esc(work.handed_to)}</span>` : ""}<strong>${esc(work.title)}</strong>${tag(label, cls)}<span class="muted">${esc(step)}</span></div>
    <div class="room-work-bar" aria-hidden="true"><span style="width:${Math.max(4, Math.min(100, share))}%"></span></div>
    <div class="room-work-foot"><span class="muted">${esc(spend)} · the result comes back here</span>${actions}</div>
  </div></li>`;
}

// Finished work, back in the discussion it was asked in.
function roomDelivery(item) {
  return `<li class="room-work room-delivery" data-delivery="${esc(item.run_id || "")}"><div class="room-work-card">
    <div class="room-work-top">${tag("Delivered", "")}<span class="muted">${esc(when(item.at))}</span></div>
    <p class="room-text">${esc(item.text)}</p>
    <div class="room-work-foot"><span class="muted">Select a passage beside to comment on it, or say what should change.</span>${item.run_id ? `${btn("Open beside", `rail-doc:${item.run_id}`, "room-tool")}<a class="button room-tool" href="#task/${esc(item.run_id)}">Open the result</a>` : ""}</div>
  </div></li>`;
}

function roomDraw() {
  const list = $("#room-feed");
  if (!list) return;
  const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
  list.innerHTML = roomReader.items.length
    ? roomLines(roomReader.items) + roomTyping()
    : `<li class="room-empty"><p><strong>Nobody has said anything here yet.</strong></p><p class="muted">Ask a question below. The Chief answers unless you name someone with @; @everyone asks every agent, and up to six can answer one message. Replies come from what the office has recorded, and cost a little each.</p></li>`;
  roomMarkArrivals(list);
  const replying = $("#room-replying");
  if (replying) {
    const names = roomReader.replying.map((r) => r.name).filter(Boolean);
    replying.textContent = names.length ? `${names.join(", ")} ${names.length === 1 ? "is" : "are"} replying…` : "";
    // Said by the typing bubble; this line stays for screen readers only.
    replying.hidden = !names.length;
    replying.classList.add("visually-hidden");
  }
  roomDrawThread();
  if (typeof roomDaysDraw === "function") roomDaysDraw();
  if (typeof composerDraw === "function") composerDraw();
  if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
  roomDrawToBottom();
}

// The feed is redrawn whole, so only a message not drawn before gets the
// arrival motion; opening a thread moves nothing.
function roomMarkArrivals(list) {
  const topicId = RoomState.topic?.id || "";
  const known = RoomState.drawnFor === topicId ? RoomState.drawn : null;
  const seen = new Set();
  list.querySelectorAll("[data-mid]").forEach((message) => {
    seen.add(message.dataset.mid);
    if (known && !known.has(message.dataset.mid)) message.classList.add("room-arrives");
  });
  RoomState.drawn = seen;
  RoomState.drawnFor = topicId;
}

// The round button above the box, when the foot of the conversation is out of sight.
function roomDrawToBottom() {
  const button = document.getElementById("room-to-bottom");
  if (!button) return;
  button.hidden = window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
}
window.addEventListener("scroll", roomDrawToBottom, { passive: true });

function roomLiveSteps() {
  return roomStepsHtml(roomReader.replying.flatMap((r) => (Array.isArray(r.steps) ? r.steps : [])));
}

// Agents answering now, as a typing bubble at the foot of the conversation.
function roomTyping() {
  const names = roomReader.replying.map((r) => r.name).filter(Boolean);
  if (!names.length) return "";
  return `<li class="room-message room-typing" aria-hidden="true">${roomAuthorAvatar({ kind: "agent", name: names[0] })}<div class="room-said"><p class="room-meta"><strong>${esc(names.join(", "))}</strong></p>${roomLiveSteps()}<div class="room-bubble"><span class="room-dots"><i></i><i></i><i></i></span></div></div></li>`;
}

// ---- replying to one message, and threads ----

function roomFind(id) {
  return roomReader.items.find((item) => item.id === id) || null;
}

function roomDrawReplyTo() {
  const holder = $("#room-reply-to");
  if (!holder) return;
  const target = RoomState.replyTo ? roomFind(RoomState.replyTo) : null;
  holder.hidden = !target;
  holder.innerHTML = target
    ? `<span>Replying to <strong>${esc(target.author?.kind === "owner" ? "yourself" : target.author?.name || "")}</strong>: ${esc(roomShort(target.text, 100))}</span>${btn("✕", "room-reply-cancel", "room-tool", 'aria-label="Stop replying"')}`
    : "";
}

function roomThreadItem(item) {
  const author = item.author || {};
  const mine = author.kind === "owner";
  return `<li class="room-message ${esc(author.kind || "")}${mine ? " mine" : ""}" data-mid="${esc(item.id)}">${mine ? "" : roomAuthorAvatar(author)}<div class="room-said"><p class="room-meta"><strong>${esc(mine ? "You" : author.name || "")}</strong> <span class="muted">${esc(when(item.at))}${item.cost_usd ? ` · ${esc(roomCost(item.cost_usd))}` : ""}</span></p><div class="room-bubble"><p class="room-text">${esc(item.text)}</p></div>${roomMessageActions(item)}</div></li>`;
}

// The thread panel: drawn once when opened, then only its list is redrawn,
// so what is being typed in it survives every read.
function roomOpenThread(id) {
  const root = roomFind(id);
  if (!root) {
    console.error("Thread root not found:", id);
    return;
  }
  RoomState.thread = root.thread_id || id;
  let panel = $("#room-thread");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "room-thread";
    panel.className = "room-thread-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Thread");
    document.body.appendChild(panel);
  }
  panel.innerHTML = `<div class="room-thread-head"><strong>Thread</strong><span class="muted">${esc(RoomState.topic?.title || "")}</span>${btn("✕", "room-thread-close", "room-tool", 'aria-label="Close the thread"')}</div>
    <ol class="room-feed room-thread-feed" id="room-thread-feed" aria-live="polite"></ol>
    <form id="room-thread-form" class="room-composer room-thread-composer">
      <textarea id="room-thread-text" name="text" rows="2" maxlength="4000" placeholder="Reply in the thread… (@name an agent, or the one already here answers)" aria-label="Reply in the thread"></textarea>
      <div class="room-composer-row"><span class="muted">Enter sends, Shift+Enter starts a new line.</span><button class="primary">Reply</button></div>
    </form>`;
  const textarea = $("#room-thread-text");
  textarea?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("#room-thread-form")?.requestSubmit();
    } else if (event.key === "Escape") {
      roomCloseThread();
    }
  });
  document.body.classList.add("room-thread-open");
  roomDrawThread();
  textarea?.focus();
}

function roomCloseThread() {
  RoomState.thread = null;
  $("#room-thread")?.remove();
  document.body.classList.remove("room-thread-open");
}

function roomDrawThread() {
  const list = $("#room-thread-feed");
  if (!list || !RoomState.thread) return;
  const root = roomFind(RoomState.thread);
  const replies = roomReader.items.filter((item) => item.thread_id === RoomState.thread);
  const names = roomReader.replying.map((r) => r.name).filter(Boolean);
  list.innerHTML =
    (root ? roomThreadItem(root) : "") +
    `<li class="room-day" role="separator"><span>${esc(replies.length)} repl${replies.length === 1 ? "y" : "ies"}</span></li>` +
    replies.map(roomThreadItem).join("") +
    (names.length ? `<li class="room-message room-typing" aria-hidden="true">${roomAuthorAvatar({ kind: "agent", name: names[0] })}<div class="room-said">${roomLiveSteps()}<div class="room-bubble"><span class="room-dots"><i></i><i></i><i></i></span></div></div></li>` : "");
  list.scrollTop = list.scrollHeight;
}

async function roomRefresh() {
  const topicId = RoomState.topic?.id;
  if (!topicId || !["room", "topic"].includes(Office.view)) return;
  const before = roomReader.items.length;
  const replyingBefore = JSON.stringify(roomReader.replying);
  const waitingBefore = JSON.stringify(roomReader.waiting);
  const workBefore = JSON.stringify(roomReader.work);
  await roomReader.read(topicId);
  const workMoved = JSON.stringify(roomReader.work) !== workBefore;
  const moved = await roomRefreshDirections();
  if (JSON.stringify(roomReader.waiting) !== waitingBefore) roomDrawWaiting();
  if (moved || workMoved || roomReader.items.length !== before || JSON.stringify(roomReader.replying) !== replyingBefore) roomDraw();
  if (workMoved || roomReader.items.length !== before) {
    // What the discussion left behind changes with what is said and done.
    const topic = await api(`/topics/${topicId}`).catch(() => null);
    if (topic && RoomState.topic?.id === topicId) RoomState.topic = topic;
    if (RoomState.rail?.mode === "draft") RoomState.rail.answer = null;
    roomDrawRail();
  }
}

// ---- what the project's tasks are waiting on ----

function roomQuestion(question) {
  const task = `<a href="#task/${esc(question.run_id)}">${esc(question.task)}</a>`;
  if (question.kind === "policy") {
    return `<div class="attention room-question"><span class="attention-icon" aria-hidden="true">⚖</span><div><strong>${task} is asking you</strong><p>${esc(question.reason)}</p></div>
      <div class="button-row tight">${btn("Refuse", `room-policy:${question.record_id}:no`, "danger")}${btn("Allow for this task", `room-policy:${question.record_id}:yes`, "primary")}</div></div>`;
  }
  if (question.kind === "budget") {
    const kind = CEILING[question.limit] ? question.limit : "tokens";
    const asked = { kind, budget: question.budget || {}, spent: question.spent || {} };
    const { shape, limit, spent } = ceilingAmounts(asked);
    const show = (value) => (kind === "cost" ? money(value) : Number(value).toLocaleString());
    // "$2.00 of $2.00", not "$2.00 of $2.00 dollars": money says its own unit.
    const unit = kind === "cost" ? "" : ` ${shape.unit}`;
    const used = spent === null ? "" : ` It has used ${show(spent)} of ${show(limit)}${unit}.`;
    return `<div class="attention room-question" data-ceiling><span class="attention-icon" aria-hidden="true">!</span><div><strong>${task}: ${esc(shape.title.toLowerCase())}</strong><p>It stopped and kept everything it had done.${esc(used)}</p>${ceilingChoices(question.run_id, asked)}</div></div>`;
  }
  if (question.kind === "stuck") {
    return `<div class="attention room-question"><span class="attention-icon" aria-hidden="true">?</span><div><strong>${task} is stuck</strong><p>${esc(question.reason || "")} It asks for a hint, a skip, or to finish.</p></div>
      <a class="button primary" href="#task/${esc(question.run_id)}">Help it →</a></div>`;
  }
  if (question.kind === "approval") {
    return `<div class="attention room-question"><span class="attention-icon" aria-hidden="true">!</span><div><strong>${task} needs your approval</strong><p>Read the proposal before deciding: nothing is carried out or published without you.</p></div>
      <a class="button primary" href="#task/${esc(question.run_id)}">Review the proposal →</a></div>`;
  }
  return `<div class="attention room-question"><span class="attention-icon" aria-hidden="true">!</span><div><strong>${task} needs your review</strong><p>${esc(stopText(question.reason))}</p></div>
    <a class="button primary" href="#task/${esc(question.run_id)}">Decide what happens →</a></div>`;
}

function roomDrawWaiting() {
  const holder = $("#room-waiting");
  if (!holder) return;
  if ($("#composer-takeover") && typeof composerDraw === "function") {
    // In a thread the decision takes the message box's place.
    holder.hidden = true;
    composerDraw();
    return;
  }
  const questions = roomReader.waiting || [];
  holder.hidden = !questions.length;
  holder.innerHTML = questions.length ? `<h2 class="room-waiting-title">Waiting for you</h2>${questions.map(roomQuestion).join("")}` : "";
}

async function roomAnswered(work, success) {
  const done = await attempt(work, success);
  if (done === undefined) return;
  // Answered from the desk: the desk is redrawn without it.
  if (Office.view === "home") {
    rerender();
    return;
  }
  await roomRefresh();
  roomDrawWaiting();
}

// ---- the documents tab ----

function roomDocument(doc) {
  // The format the task asked for comes first, as in the task room.
  const ordered = [...DOWNLOADS].sort(([a], [b]) => (b === doc.format) - (a === doc.format));
  const downloads = ordered
    .map(([format, label]) => `<a class="button ${format === doc.format ? "primary" : ""}" href="/artifacts/${esc(doc.artifact_id)}/download?format=${esc(format)}" download>${esc(label)}</a>`)
    .join("");
  return `<li class="card room-document"><div class="card-top"><h3>${esc(doc.task)}</h3>${statusTag(doc.status)}</div>
    <p class="muted">${esc(doc.asked_for)} · version ${esc(doc.version)} · ${esc(when(doc.created_at))}</p>
    <div class="button-row tight">${btn("Read", `artifact:${doc.artifact_id}`, "primary")}${downloads}<a class="button" href="#task/${esc(doc.run_id)}">Open the task</a></div></li>`;
}

function roomDrawDocuments() {
  const holder = $("#room-documents");
  if (!holder) return;
  const docs = RoomState.documents;
  if (docs === null) {
    holder.innerHTML = `<p class="muted">Loading…</p>`;
    return;
  }
  holder.innerHTML = docs.length
    ? `<ol class="room-documents">${docs.map(roomDocument).join("")}</ol>`
    : emptyCard("No documents yet.", "When a task in this project finishes, what it came to is kept here, newest first.");
}

function roomShowTab(tab) {
  RoomState.tab = tab === "documents" ? "documents" : "messages";
  document.querySelectorAll("[data-room-tab]").forEach((button) => {
    const on = button.dataset.roomTab === RoomState.tab;
    button.setAttribute("aria-selected", String(on));
    button.classList.toggle("active", on);
  });
  const messages = $("#room-messages");
  const documents = $("#room-documents");
  if (messages) messages.hidden = RoomState.tab !== "messages";
  if (documents) documents.hidden = RoomState.tab !== "documents";
}

async function roomLoadDocuments() {
  const projectId = RoomState.project?.id;
  if (!projectId) return;
  const docs = await api(`/projects/${projectId}/documents`).catch((error) => {
    console.error(error);
    return [];
  });
  if (RoomState.project?.id !== projectId) return;
  RoomState.documents = Array.isArray(docs) ? docs : [];
  const count = $("#room-documents-count");
  if (count) count.textContent = String(RoomState.documents.length);
  roomDrawDocuments();
}

// A project's room is its first topic: the old address still works.
async function renderProjectRoom(content, projectId) {
  if (!projectId) {
    go("discussions");
    return;
  }
  const home = await api(`/projects/${projectId}/topic`);
  await renderTopic(content, home.id);
}

// Where a discussion belongs, said plainly, and changeable.
function topicScope(topic) {
  if (topic.home) {
    return `<span class="topic-scope"><strong>Reference:</strong> ${esc(topic.project?.name || "")}${topic.business_line ? ` · ${esc(topic.business_line)}` : ""}</span>`;
  }
  const where = topic.project ? `${esc(topic.project.name)}${topic.business_line ? ` · ${esc(topic.business_line)}` : ""}` : "Free exploration";
  // The business line opens its space when the topic has one.
  return `<span class="topic-scope"><strong>Reference:</strong> this discussion · ${where}${btn("Change", "topic-scope", "topic-scope-change")}</span>`;
}

async function renderTopic(content, topicId) {
  if (!topicId) {
    go("discussions");
    return;
  }
  if (RoomState.topic?.id !== topicId) {
    RoomState.tab = "messages";
    RoomState.documents = null;
    RoomState.replyTo = null;
    RoomState.rail = { mode: dockKeptTab() };
    roomCloseThread();
  }
  const [topic, agents] = await Promise.all([api(`/topics/${topicId}`), api("/agents").catch(() => [])]);
  RoomState.topic = topic;
  RoomState.project = topic.project;
  RoomState.agents = (Array.isArray(agents) ? agents : []).filter((a) => a.status === "ACTIVE" && a.enabled_version);
  await roomReader.read(topicId);
  // The trail with this discussion's own name as its last step.
  renderTrail({ title: topic.title });
  // Who is in this discussion: you, and the colleagues brought in.
  const team = (topic.participants || [])
    .map((a, i) => `<span title="${esc(a.name)} · ${esc(ROLE_LABEL[a.role] || a.role)}">${avatar(a.name, i)}</span>`)
    .join("");
  const project = topic.project;
  const brief = topic.brief || {};
  const welcome = topic.welcome_back && brief.where
    ? `<div class="topic-welcome" data-welcome-back><div class="topic-welcome-head"><strong>Since you were last here</strong>${btn("Read the whole discussion", "topic-welcome-close", "room-tool")}</div>
        <dl><dt>Where we were</dt><dd>${esc(brief.where)}</dd><dt>Decided</dt><dd>${esc(brief.decided || "Nothing yet.")}</dd><dt>Open</dt><dd>${esc(brief.open || "Nothing open.")}</dd></dl></div>`
    : "";
  const parent = topic.parent
    ? `<p class="topic-parent">↳ Branched from <a href="#topic/${esc(topic.parent.id)}">${esc(topic.parent.title)}</a>. It carried a brief, not the whole history.</p>`
    : "";
  const branches = (topic.branches || []).length
    ? `<p class="topic-parent">Branches: ${topic.branches.map((b) => `<a href="#topic/${esc(b.id)}">${esc(b.title)}</a>`).join(" · ")}</p>`
    : "";
  content.innerHTML = `
    <div class="heading room-heading"><div><div class="eyebrow">${esc(project?.name || "Free exploration")} › Thread</div><div class="title-line"><h1>${esc(topic.title)}</h1>${btn("✎ Rename", `title-rename:topic:${topic.id}`, "rename-btn", 'aria-label="Rename this discussion"')}</div>
      <div class="topic-meta">${topicScope(topic)}</div></div>
      <div class="room-people"><span class="muted">In this discussion</span><span title="${esc(Office.owner || "Owner")} · owner"><span class="avatar owner">${esc((Office.owner || "O").slice(0, 1).toUpperCase())}</span></span>${team}
      ${btn("Bring someone in", "topic-bring", "")}${btn("Board", "rail-toggle", "rail-open-btn", 'aria-controls="topic-rail"')}${project ? `<a class="button" href="#project/${esc(project.id)}">Project</a>` : ""}</div></div>
    ${parent}${branches}${welcome}
    <div class="topic-layout"><div class="topic-main">
    ${
      project
        ? `<div class="subnav" role="tablist" aria-label="Discussion">
      <button type="button" role="tab" id="room-tab-messages" aria-controls="room-messages" data-room-tab="messages" data-action="room-tab:messages">Messages</button>
      <button type="button" role="tab" id="room-tab-documents" aria-controls="room-documents" data-room-tab="documents" data-action="room-tab:documents">Documents <span id="room-documents-count" class="count"></span></button>
    </div>
    <section id="room-documents" role="tabpanel" aria-labelledby="room-tab-documents" hidden></section>`
        : ""
    }
    <div id="room-messages" role="tabpanel" aria-labelledby="room-tab-messages">
    <nav class="room-days" id="room-days" aria-label="Runs of this routine, newest first" hidden></nav>
    <ol class="room-feed" id="room-feed" aria-live="polite" aria-label="Conversation"></ol>
    <p class="room-replying muted" id="room-replying" role="status" hidden></p>
    <section class="room-waiting" id="room-waiting" aria-label="Waiting for you" hidden></section>
    <div id="composer-stack" class="composer-stack" aria-label="The work in this thread" hidden></div>
    <section id="composer-takeover" class="composer-takeover" aria-label="Waiting for you" aria-live="polite" hidden></section>
    <form id="room-form" class="room-composer">
      <button type="button" id="room-to-bottom" class="icon-tool room-to-bottom" data-action="room-to-bottom" aria-label="Go to the latest message" title="Go to the latest message" hidden>${composerIcon("down")}</button>
      <label class="visually-hidden" for="room-text">Message this thread</label>
      <div id="composer-goal" class="composer-goal" hidden></div>
      <div id="room-reply-to" class="room-reply-to" hidden></div>
      <div class="attach-chips" id="room-form-chips">${attachChips("room-form")}</div>
      <textarea id="room-text" name="text" rows="1" maxlength="4000" autocomplete="off" placeholder="Message the lead, or @ a colleague…" aria-describedby="room-hint" aria-controls="room-mentions"></textarea>
      <ul id="room-mentions" class="room-mentions" role="listbox" aria-label="Agents to mention" hidden></ul>
      <div class="attach-pick-holder" id="room-form-pick" hidden></div>
      <div class="room-composer-row">
        <div class="composer-tools" role="group" aria-label="Add to your message">
          ${btn("+", "composer-menu", "composer-tool composer-plus", 'aria-label="Add or run a command" aria-haspopup="menu" aria-expanded="false" aria-controls="composer-menu"')}${btn("@", "room-mention", "composer-tool room-at", 'aria-label="Mention a colleague"')}
          <span id="composer-access-slot"></span>
        </div>
        <span id="room-hint" class="muted">Enter sends · Shift Enter for a new line · drop or paste files</span>
        <div id="composer-mode" class="composer-mode" role="group" aria-label="While the work runs, Enter will" hidden>${btn("Queue", "composer-mode:queue", "", 'data-mode="queue" aria-pressed="true"')}${btn("Steer", "composer-mode:steer", "", 'data-mode="steer" aria-pressed="false"')}</div>
        <span id="composer-model-slot"></span>
        <button class="primary composer-send" id="room-send" aria-label="Send" title="Send">${iconFace("send", "Send")}</button>
      </div>
      <div id="composer-menu" class="composer-menu" role="menu" aria-label="Add or run a command" hidden></div>
      <div id="composer-pop" class="composer-pop" hidden></div>
    </form>
    <p id="composer-status" class="composer-status" aria-label="This thread so far" hidden></p>
    </div>
    </div><div class="topic-rail" id="topic-rail" role="complementary" aria-label="Beside the discussion"></div></div>`;
  // Drawn after the markup exists; the conversation is redrawn on its own.
  setTimeout(() => {
    roomShowTab(RoomState.tab);
    if (project) {
      roomDrawDocuments();
      roomLoadDocuments();
    }
    roomDraw();
    roomDrawWaiting();
    roomWireComposer();
    if (typeof composerWireBox === "function") composerWireBox();
    roomDrawRail();
    // A routine's room opens on its newest run; others on the brief or the latest word.
    if (typeof roomDaysOpening === "function" && roomDaysOpening()) return;
    if (topic.welcome_back) window.scrollTo(0, 0);
    else window.scrollTo(0, document.body.scrollHeight);
  }, 0);
  // Coming back is measured from now; the brief has been shown.
  api(`/topics/${topicId}/seen`, { method: "POST" }).catch(() => null);
  roomStartPolling();
}

function roomStartPolling() {
  // Route changes clear Office.timers, so a timer no longer listed there is gone.
  if (RoomState.timer && Office.timers.includes(RoomState.timer)) return;
  RoomState.timer = setInterval(() => {
    if (document.hidden) return;
    // While the office's stream is open it redraws the thread; this is a fallback.
    // Steps are not written anywhere the stream hears, so an answer in progress is read here.
    if (!roomReader.replying.length && typeof liveQuiet === "function" && liveQuiet("thread")) return;
    roomRefresh().catch((error) => console.error(error));
  }, ROOM_POLL_MS);
  Office.timers.push(RoomState.timer);
}

// ---- mentioning an agent ----

function roomMentionQuery(textarea) {
  const upto = textarea.value.slice(0, textarea.selectionStart);
  const match = upto.match(/(?:^|[^\w.@])@([\w-]*)$/);
  return match ? { at: upto.length - match[1].length - 1, query: match[1].toLowerCase() } : null;
}

function roomMentionOptions(query) {
  return RoomState.agents.filter((a) => [a.name, a.role, ROLE_LABEL[a.role] || ""].some((label) => label.toLowerCase().startsWith(query)));
}

function roomShowMentions() {
  const textarea = $("#room-text");
  const list = $("#room-mentions");
  if (!textarea || !list) return;
  const found = roomMentionQuery(textarea);
  const options = found ? roomMentionOptions(found.query) : [];
  RoomState.mentionAt = found && options.length ? found.at : -1;
  if (RoomState.mentionAt < 0) {
    list.hidden = true;
    textarea.removeAttribute("aria-activedescendant");
    return;
  }
  RoomState.mentionPick = Math.min(RoomState.mentionPick, options.length - 1);
  list.innerHTML = options
    .map(
      (a, i) =>
        `<li role="option" id="room-mention-${i}" aria-selected="${i === RoomState.mentionPick}" data-mention="${esc(a.name)}">${avatar(a.name, i)}<span>${esc(a.name)}</span><small>${esc(ROLE_LABEL[a.role] || a.role)}${
          a.role === "chief" ? " · answers by default" : ""
        }</small></li>`
    )
    .join("");
  list.hidden = false;
  textarea.setAttribute("aria-activedescendant", `room-mention-${RoomState.mentionPick}`);
}

function roomInsertMention(name) {
  const textarea = $("#room-text");
  if (!textarea || RoomState.mentionAt < 0) return;
  const before = textarea.value.slice(0, RoomState.mentionAt);
  const after = textarea.value.slice(textarea.selectionStart);
  textarea.value = `${before}@${name} ${after}`;
  const caret = before.length + name.length + 2;
  textarea.setSelectionRange(caret, caret);
  RoomState.mentionAt = -1;
  RoomState.mentionPick = 0;
  roomShowMentions();
  textarea.focus();
}

function roomWireComposer() {
  const textarea = $("#room-text");
  const list = $("#room-mentions");
  if (!textarea || !list) {
    console.error("Room composer not found: #room-text / #room-mentions");
    return;
  }
  $("#room-form")?.addEventListener("click", (event) => {
    if (event.target.matches("#room-form, .room-composer-row")) textarea.focus();
  });
  textarea.addEventListener("input", () => {
    RoomState.mentionPick = 0;
    roomShowMentions();
  });
  textarea.addEventListener("keydown", (event) => {
    const open = !list.hidden;
    const count = list.children.length;
    if (open && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      RoomState.mentionPick = (RoomState.mentionPick + (event.key === "ArrowDown" ? 1 : count - 1)) % count;
      roomShowMentions();
    } else if (open && (event.key === "Enter" || event.key === "Tab")) {
      event.preventDefault();
      const chosen = list.children[RoomState.mentionPick];
      if (chosen) roomInsertMention(chosen.dataset.mention || "");
    } else if (open && event.key === "Escape") {
      event.preventDefault();
      RoomState.mentionAt = -1;
      list.hidden = true;
    } else if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (typeof Composer === "object") Composer.submitMode = event.metaKey || event.ctrlKey ? composerOther(Composer.mode) : Composer.mode;
      if (!textarea.value.trim() && typeof composerWork === "function" && composerWork()) return; // Enter never stops; the button or Esc Esc does
      $("#room-form")?.requestSubmit();
    } else if (event.key === "Escape" && !textarea.value.trim() && typeof composerWork === "function") {
      const work = composerWork();
      if (!work) return;
      const now = Date.now();
      if (now - Composer.lastEsc < DOUBLE_ESC_MS) {
        Composer.lastEsc = 0;
        composerStop(work);
      } else {
        Composer.lastEsc = now;
      }
    }
  });
  textarea.addEventListener("input", () => {
    if (typeof composerDrawBox === "function" && typeof composerWork === "function") composerDrawBox(composerWork());
  });
  list.addEventListener("mousedown", (event) => {
    const option = event.target.closest("[data-mention]");
    if (!option) return;
    event.preventDefault();
    roomInsertMention(option.dataset.mention || "");
  });
}

Office.screens.rooms = { title: "Discussions", nav: "discussions", render: renderRoomList };
Office.screens.room = { title: "Discussion", nav: "discussions", render: renderProjectRoom };
Office.screens.topic = { title: "Discussion", nav: "discussions", render: renderTopic };
Office.screens.discussions = { title: "Rooms", render: renderDiscussions };

// ---- Discussions: every topic, and a box to start one ----

function topicWhere(topic) {
  if (topic.project) return `${topic.project.name}${topic.business_line ? ` · ${topic.business_line}` : ""}`;
  return "Free exploration";
}

function topicCard(topic) {
  const brief = topic.brief || {};
  const last = brief.where
    ? `<dl class="topic-brief"><dt>Where we were</dt><dd>${esc(brief.where)}</dd><dt>Decided</dt><dd>${esc(brief.decided || "Nothing yet.")}</dd><dt>Open</dt><dd>${esc(brief.open || "Nothing open.")}</dd></dl>`
    : topic.last
      ? `<p class="topic-last"><strong>${esc(topic.last.kind === "owner" ? "You" : topic.last.author)}:</strong> ${esc(topic.last.text)}</p>`
      : "";
  const working = (topic.back ? tag("Back today", "amber") : "") + (topic.working ? tag(`${topic.working} working`, "blue") : "");
  return `<a class="card topic-card" href="#topic/${esc(topic.id)}" data-topic="${esc(topic.id)}">
    <div class="topic-card-top"><h3>${esc(topic.title)}</h3><span class="muted">${esc(when(topic.last_activity_at))}</span></div>
    ${last}
    <div class="topic-card-foot">${tag(topicWhere(topic), topic.project ? "" : "gray")}${working}</div></a>`;
}

// One box that starts talking, shared by the desk and Discussions. The same
// compact box as a thread's: it grows with the words, keeps a
// draft in this browser, takes pasted or dropped files, and Enter starts.
function startBox(id, placeholder) {
  const draft = remembered(`draft:start:${id}`, "") || "";
  return `<form id="${esc(id)}" class="start-box start-composer" data-start-topic>
    <label class="visually-hidden" for="${esc(id)}-text">Say anything</label>
    <div class="attach-chips" id="${esc(id)}-chips">${attachChips(id)}</div>
    <textarea id="${esc(id)}-text" name="text" rows="1" maxlength="4000" autocomplete="off" placeholder="${esc(placeholder)}" aria-describedby="${esc(id)}-hint">${esc(draft)}</textarea>
    <div class="attach-pick-holder" id="${esc(id)}-pick" hidden></div>
    <div class="start-box-row">
      <div class="composer-tools" role="group" aria-label="Add to what you say">
        ${btn("Files", `attach-files:${id}`, "composer-tool", 'aria-label="Attach files"')}${btn("Folder", `attach-folder:${id}`, "composer-tool", 'aria-label="Attach a folder"')}${btn("Documents", `attach-existing:${id}`, "composer-tool", 'aria-label="Attach from your documents"')}
      </div>
      <button class="primary composer-send" aria-label="Start" title="Start">${iconFace("send", "Start")}</button>
      <span id="${esc(id)}-hint" class="start-hint muted">Enter starts a thread in Free exploration · Shift Enter for a new line · drop or paste files</span>
    </div>
  </form>`;
}

function startBoxOf(target) {
  const form = target?.closest?.("form[data-start-topic]");
  const textarea = form?.querySelector("textarea[name=text]");
  return form && textarea ? { form, textarea } : null;
}

function startGrow(textarea) {
  if (typeof composerGrow === "function") composerGrow(textarea);
}

function startTakeFiles(form, list) {
  const files = [...(list || [])].filter((file) => file && file.size >= 0 && file.name);
  if (!files.length) return false;
  attachUpload(form.id, files, files.length === 1 ? files[0].name : `${files[0].name} and ${files.length - 1} more`);
  return true;
}

// Wired once for every start box, whenever it is drawn.
document.addEventListener("input", (event) => {
  const box = startBoxOf(event.target);
  if (!box || event.target !== box.textarea) return;
  startGrow(box.textarea);
  remember(`draft:start:${box.form.id}`, box.textarea.value);
});
document.addEventListener("focusin", (event) => {
  const box = startBoxOf(event.target);
  if (box && event.target === box.textarea) startGrow(box.textarea);
});
document.addEventListener("keydown", (event) => {
  const box = startBoxOf(event.target);
  if (!box || event.target !== box.textarea) return;
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  box.form.requestSubmit();
});
document.addEventListener("paste", (event) => {
  const box = startBoxOf(event.target);
  if (box && startTakeFiles(box.form, event.clipboardData?.files)) event.preventDefault();
});
document.addEventListener("dragover", (event) => {
  const box = startBoxOf(event.target);
  if (box && [...(event.dataTransfer?.types || [])].includes("Files")) {
    event.preventDefault();
    box.form.classList.add("is-dropping");
  }
});
document.addEventListener("dragleave", (event) => startBoxOf(event.target)?.form.classList.remove("is-dropping"));
document.addEventListener("drop", (event) => {
  const box = startBoxOf(event.target);
  if (!box) return;
  box.form.classList.remove("is-dropping");
  if (startTakeFiles(box.form, event.dataTransfer?.files)) event.preventDefault();
});

async function startTopic(form) {
  const text = String(new FormData(form).get("text") || "").trim();
  if (!text) {
    toast("Say something first.", true);
    return;
  }
  if (attachBusy(form.id)) return;
  const button = form.querySelector("button.primary");
  if (button) button.disabled = true;
  const client_key = requestKey("start", text);
  const attachments = attachedIds(form.id);
  const started = await attempt(() => api("/topics", { method: "POST", body: { text, client_key, ...(attachments.length ? { attachments } : {}) } }));
  if (button) button.disabled = false;
  if (!started?.topic?.id) return;
  attachClear(form.id);
  remember(`draft:start:${form.id}`, "");
  requestSent("start");
  roomReader.replying = Array.isArray(started.replying) ? started.replying : [];
  go("topic", started.topic.id);
}

async function renderDiscussions(content) {
  const [open, resting] = await Promise.all([api("/topics?status=open"), api("/topics?status=resting").catch(() => [])]);
  if (!Array.isArray(open)) throw new Error("could not load discussions");
  Office.topics = open;
  renderNav();
  content.innerHTML =
    heading("DISCUSSIONS", "Talk first.", "Start with a sentence. Work forms in the conversation, and its results come back to it.") +
    startBox("discussion-start", "Say anything — an idea, a question, a link. The Chief picks it up.") +
    (open.length
      ? `<div class="topic-grid">${open.map(topicCard).join("")}</div>`
      : emptyCard("No discussions yet.", "Say something above to start one. A project's room is its first discussion.")) +
    (Array.isArray(resting) && resting.length ? `<h2 class="spaced">Resting</h2><div class="topic-grid">${resting.map(topicCard).join("")}</div>` : "");
}

async function openTopicScope() {
  const topic = RoomState.topic;
  if (!topic) return;
  const projects = await api("/projects").catch(() => []);
  const active = (Array.isArray(projects) ? projects : []).filter((p) => p.status === "ACTIVE");
  const choices = [["", "Free exploration — no project yet"], ...active.map((p) => [p.id, p.name])];
  formModal(
    "Where does this discussion belong?",
    "topic-scope-form",
    `${field("Project", "project_id", { type: "select", required: false, value: topic.project?.id || "", choices })}
     <p class="muted">Agents read the project's background, decisions and documents when they answer here. Free exploration reads none of them, and can be placed later.</p>`,
    "Save"
  );
}

Office.actions = Object.assign(Office.actions || {}, {
  "topic-scope": () => openTopicScope(),
});

Office.forms = Object.assign(Office.forms || {}, {
  "topic-scope-form": async (form) => {
    const topic = RoomState.topic;
    if (!topic) return;
    const value = String(new FormData(form).get("project_id") || "");
    const saved = await attempt(() => api(`/topics/${topic.id}`, { method: "PUT", body: { project_id: value || null } }), "Saved.");
    if (!saved) return;
    closeModal();
    rerender();
  },
  "discussion-start": (form) => startTopic(form),
  "desk-start": (form) => startTopic(form),
});

function openNewRoom() {
  formModal(
    "A new room",
    "room-new-form",
    `${field("Name", "name", { attrs: 'maxlength="255" placeholder="Wwft research"' })}
     ${field("What it is for (optional)", "description", { type: "textarea", required: false, attrs: 'rows="3" maxlength="8192"', hint: "The agents are told this whenever they answer in the room." })}
     <p class="muted">A room comes with a project: its tasks, knowledge, limits and models live there too, and you can set them up later from the project.</p>`,
    "Create the room"
  );
}

Office.actions = Object.assign(Office.actions || {}, {
  "room-new": openNewRoom,
  // A proposal starts in place, as any hand-off does: its card shows the
  // scope and can be paused at once. No form to fill in first.
  "room-start": async (arg) => {
    const done = await roomPress(arg, {});
    if (done) {
      await roomReader.read(RoomState.topic.id);
      roomDraw();
    }
  },
  "room-tab": (tab) => {
    roomShowTab(tab);
    // Read again on opening: a task may have finished since.
    if (RoomState.tab === "documents") roomLoadDocuments();
  },
  "room-policy": (arg) => {
    const [recordId, answer] = String(arg).split(":");
    return roomAnswered(
      () => api(`/policies/records/${recordId}/decide`, { method: "POST", body: { allow: answer === "yes" } }),
      answer === "yes" ? "Allowed for this task. It carries on." : "Refused. The task stays paused."
    );
  },
  "room-direct": async (arg) => {
    const done = await roomPress(arg, {});
    if (done) toast("Sent. It waits for the task's next step.");
  },
  "room-mention": () => {
    const textarea = $("#room-text");
    if (!textarea) return;
    const at = textarea.selectionStart;
    const lead = at > 0 && !/\s/.test(textarea.value[at - 1]) ? " @" : "@";
    textarea.value = textarea.value.slice(0, at) + lead + textarea.value.slice(at);
    textarea.setSelectionRange(at + lead.length, at + lead.length);
    textarea.focus();
    roomShowMentions();
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "room-new-form": async (form) => {
    const data = new FormData(form);
    const name = String(data.get("name") || "").trim();
    const description = String(data.get("description") || "").trim();
    if (!name) {
      toast("Give the room a name.", true);
      return;
    }
    const project = await attempt(() => api("/projects", { method: "POST", body: { name, description } }));
    if (!project?.id) return;
    closeModal();
    Office.rooms = [project, ...(Office.rooms || [])];
    // Opened, so the new room is where you can see it.
    Office.roomsOpen = true;
    remember("roomsOpen", true);
    go("room", project.id);
  },
  "room-start-form": async (form) => {
    const data = new FormData(form);
    const objective = String(data.get("objective") || "").trim();
    if (!objective) {
      toast("Say what the task should do.", true);
      return;
    }
    const done = await roomPress(String(data.get("action") || ""), { objective });
    if (!done) return;
    closeModal();
    toast("Started. Its steps will appear here as it works.");
  },
  "room-form": async (form) => {
    const textarea = $("#room-text");
    const topicId = RoomState.topic?.id;
    const text = String(new FormData(form).get("text") || "").trim();
    const work = typeof composerWork === "function" ? composerWork() : null;
    if (textarea && work) {
      // Work runs in this thread: the words go to it, queued or steering.
      const mode = Composer.submitMode || Composer.mode;
      Composer.submitMode = null;
      if (!text) {
        await composerStop(work);
        return;
      }
      textarea.disabled = true;
      const done = await composerSendToWork(work, text, mode);
      textarea.disabled = false;
      textarea.focus();
      if (done === undefined) return;
      textarea.value = "";
      composerSent();
      Composer.notesAt = 0;
      await composerDraw();
      return;
    }
    if (!textarea || !topicId || !text) return;
    if (attachBusy("room-form")) return;
    textarea.disabled = true;
    const client_key = requestKey(`topic:${topicId}`, text);
    const attachments = attachedIds("room-form");
    const body = {
      ...(RoomState.replyTo ? { text, reply_to: RoomState.replyTo, client_key } : { text, client_key }),
      ...(attachments.length ? { attachments } : {}),
      ...(typeof ComposerPlus !== "undefined" && ComposerPlus.plan ? { plan: true } : {}),
    };
    const sent = await attempt(() => api(`/topics/${topicId}/messages`, { method: "POST", body }));
    if (sent) requestSent(`topic:${topicId}`);
    textarea.disabled = false;
    textarea.focus();
    if (!sent) return;
    textarea.value = "";
    if (typeof composerSent === "function") composerSent();
    attachClear("room-form");
    RoomState.replyTo = null;
    roomDrawReplyTo();
    roomShowMentions();
    // Say who is answering at once; the replies arrive by the next reads.
    roomReader.replying = Array.isArray(sent.replying) ? sent.replying : [];
    await roomReader.read(topicId);
    roomDraw();
    window.scrollTo(0, document.body.scrollHeight);
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "room-thread-form": async (form) => {
    const textarea = $("#room-thread-text");
    const topicId = RoomState.topic?.id;
    const text = String(new FormData(form).get("text") || "").trim();
    if (!textarea || !topicId || !text || !RoomState.thread) return;
    textarea.disabled = true;
    const client_key = requestKey(`thread:${RoomState.thread}`, text);
    const sent = await attempt(() => api(`/topics/${topicId}/messages`, { method: "POST", body: { text, thread_id: RoomState.thread, client_key } }));
    if (sent) requestSent(`thread:${RoomState.thread}`);
    textarea.disabled = false;
    textarea.focus();
    if (!sent) return;
    textarea.value = "";
    roomReader.replying = Array.isArray(sent.replying) ? sent.replying : [];
    await roomReader.read(topicId);
    roomDraw();
  },
});

// The copy icon turns to a tick for a moment.
function roomShowCopied(id) {
  const button = document.querySelector(`[data-action="room-copy:${CSS.escape(id)}"]`);
  if (!button) return;
  button.innerHTML = iconFace("check", "Copied");
  setTimeout(() => {
    button.innerHTML = iconFace("copy", "Copy");
  }, ROOM_COPIED_MS);
}

Office.actions = Object.assign(Office.actions || {}, {
  "room-to-bottom": () => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }),
  "room-copy": async (id) => {
    const text = document.querySelector(`[data-mid="${CSS.escape(id)}"] .room-text`)?.innerText || "";
    if (!text) {
      console.error("Message text not found:", id);
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Without clipboard permission (an http address on the home network),
      // the words are selected so the owner can copy them by hand.
      const node = document.querySelector(`[data-mid="${CSS.escape(id)}"] .room-text`);
      const range = document.createRange();
      if (node) range.selectNodeContents(node);
      getSelection()?.removeAllRanges();
      getSelection()?.addRange(range);
      if (!document.execCommand("copy")) {
        toast("Selected: press ⌘C (Ctrl+C) to copy.");
        return;
      }
    }
    toast("Copied.");
    roomShowCopied(id);
  },
  "room-open-doc": async (id) => {
    const topicId = RoomState.topic?.id;
    const messageId = String(id).replace(/^m:/, "");
    if (!topicId || !messageId) {
      console.error("No topic or message to open:", topicId, id);
      return;
    }
    const shown = await attempt(() => api(`/topics/${topicId}/messages/${messageId}/document`, { method: "POST" }));
    if (shown) await AnswerEditor.openDocument(shown);
  },
  "room-reply": (id) => {
    RoomState.replyTo = id;
    roomDrawReplyTo();
    $("#room-text")?.focus();
  },
  "room-reply-cancel": () => {
    RoomState.replyTo = null;
    roomDrawReplyTo();
  },
  "room-thread": (id) => roomOpenThread(id),
  "room-thread-close": () => roomCloseThread(),
  "room-jump": (id) => {
    const target = roomFind(id);
    if (target?.thread_id) {
      roomOpenThread(target.thread_id);
      return;
    }
    const element = document.querySelector(`#room-feed [data-mid="${CSS.escape(id)}"]`);
    if (!element) return;
    element.scrollIntoView({ behavior: "smooth", block: "center" });
    element.classList.add("room-flash");
    setTimeout(() => element.classList.remove("room-flash"), 1600);
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "room-calendar": async (arg) => {
    const { action } = roomFindAction(arg);
    if (!action) return;
    const writable = await attempt(() => api("/calendars/writable"));
    if (!Array.isArray(writable) || !writable.length) return toast("Connect this Mac's calendars to add events.", true);
    CalView.writable = writable;
    // Saved from the form: the button then records it as added, once.
    CalView.after = async (change) => {
      await roomPress(arg, { change_id: change.id });
    };
    calEditForm(null, new Date(action.starts_at), { title: action.title, ends_at: action.ends_at });
  },
  "room-start-now": async (arg) => {
    const done = await roomPress(arg, {});
    if (done) {
      await roomReader.read(RoomState.topic.id);
      roomDraw();
    }
  },
  "work-pause": async (runId) => {
    if ((await attempt(() => api(`/runs/${runId}/controls`, { method: "POST", body: { kind: "pause" } }), "Paused. Everything done so far is kept.")) === undefined) return;
    await roomRefresh();
  },
  "work-resume": async (runId) => {
    if ((await attempt(() => api(`/runs/${runId}/resume`, { method: "POST" }), "Resumed.")) === undefined) return;
    await roomRefresh();
  },
});

// ---- Beside the discussion ----
// While exploring: what the discussion left behind. While reviewing: the
// delivered document, where a selected passage takes a comment and is revised.

function railLeft() {
  const topic = RoomState.topic || {};
  const brief = topic.brief || {};
  const ideas = (topic.ideas || []).map((i) => `<li>${esc(roomShort(i.text, 140))}</li>`).join("");
  const work = (roomReader.work || [])
    .map((w) => {
      const [label, cls] = WORK_STATE[w.status] || [w.status, "gray"];
      const done = ["COMPLETED", "CLOSED", "NEEDS_HUMAN_REVIEW"].includes(w.status);
      return `<li><span>${esc(w.title)}</span>${tag(label, cls)}${done ? btn("Open beside", `rail-doc:${w.run_id}`, "room-tool") : ""}</li>`;
    })
    .join("");
  return `<section><h3>Ideas pinned</h3>${ideas ? `<ul>${ideas}</ul>` : `<p class="muted">“Note this” pins one here.</p>`}</section>
    <section><h3>Decided</h3>${
      (RoomState.decisions || []).length
        ? `<ul>${RoomState.decisions.map((d) => `<li>${esc(d.text)}${d.source?.topic_id === topic.id ? "" : ` <span class="muted">(${esc(d.source?.title || "earlier")})</span>`}</li>`).join("")}</ul>`
        : `<p>${esc(brief.decided || "Nothing recorded yet. The discussion can rest here.")}</p>`
    }</section>
    <section><h3>Open</h3><p>${esc(brief.open || "Nothing open yet.")}</p></section>
    <section><h3>Work from this thread</h3>${work ? `<ul class="rail-work">${work}</ul>` : `<p class="muted">Say what you want done, and it shows here.</p>`}</section>`;
}

async function railDoc() {
  const rail = RoomState.rail;
  if (!rail.answer) {
    rail.answer = await api(`/runs/${rail.runId}/answer`).catch(() => null);
    rail.version = rail.answer?.current?.version;
  }
  const answer = rail.answer;
  if (!answer) return `<p class="muted">No answer yet. The draft shows here once its first version is written.</p>`;
  const chosen = answer.versions.find((v) => v.version === rail.version) || answer.current;
  const doc = await api(`/artifacts/${chosen.artifact_id}/document`).catch(() => null);
  const chips = answer.versions
    .map((v) => `<button type="button" class="chip ${v.version === chosen.version ? "active" : ""}" data-action="rail-version:${v.version}" title="${esc(v.note || "")}">Version ${v.version}</button>`)
    .join("");
  const note = chosen.note ? `<p class="rail-note">What changed: ${esc(chosen.note)}</p>` : "";
  return `<h2 class="dock-title">${esc(doc?.title || "The answer")}</h2>
    <div class="rail-versions">${chips}<a class="button room-tool" href="/artifacts/${esc(chosen.artifact_id)}/download?format=docx" download>Download</a></div>${note}
    <div id="rail-comment" class="rail-comment" hidden></div>
    <div class="artifact-body document rail-doc" id="rail-doc" data-run="${esc(answer.run_id)}">${doc?.html || ""}</div>
    <p class="muted">Select any passage to comment on it. A revision keeps the earlier version and says what changed.</p>`;
}

async function roomDrawRail() {
  const holder = $("#topic-rail");
  if (!holder || !RoomState.topic) return;
  if (RoomState.decisionsFor !== RoomState.topic.id) {
    RoomState.decisionsFor = RoomState.topic.id;
    RoomState.decisions = await api(`/topics/${RoomState.topic.id}/decisions`).catch(() => []);
  }
  const rail = RoomState.rail || (RoomState.rail = { mode: dockKeptTab() });
  if (rail.drawing) return;
  rail.drawing = true;
  try {
    holder.innerHTML = await dockRender(rail);
  } finally {
    rail.drawing = false;
  }
  $("#rail-doc")?.addEventListener("mouseup", railSelected);
  $("#rail-doc")?.addEventListener("keyup", railSelected);
}

// A passage selected in the document: offer to comment on it, right above.
function railSelected() {
  const selection = window.getSelection();
  const quote = String(selection?.toString() || "").trim();
  const box = $("#rail-comment");
  if (!box || quote.length < 3) return;
  RoomState.rail.quote = quote.slice(0, 1800);
  box.hidden = false;
  box.innerHTML = `<form id="rail-comment-form"><p class="rail-quote">“${esc(roomShort(quote, 220))}”</p>
    <label class="visually-hidden" for="rail-comment-text">What should change here</label>
    <textarea id="rail-comment-text" name="comment" rows="3" maxlength="2000" required placeholder="What should change here? e.g. the evidence is thin"></textarea>
    <div class="button-row tight">${btn("Cancel", "rail-comment-cancel", "room-tool")}<button class="primary">Revise this part</button></div></form>`;
  $("#rail-comment-text")?.focus();
}

Office.actions = Object.assign(Office.actions || {}, {
  "effect-settle": async (arg) => {
    const [id, status] = String(arg).split(":");
    try {
      await api(`/side-effects/${id}/settle`, { method: "POST", body: { status } });
      toast(status === "done" ? "Noted: it went through." : "Noted: it did not go through.");
    } catch (error) {
      if (String(error?.message || "").includes("side_effect_not_unknown")) toast("That one is already settled.");
      else throw error;
    }
  },
  "rail-doc": (runId) => {
    RoomState.rail = { mode: "draft", runId };
    document.querySelector("#topic-rail")?.classList.add("open");
    roomDrawRail();
  },
  "rail-version": (version) => {
    RoomState.rail.version = Number(version);
    roomDrawRail();
  },
  "rail-toggle": () => {
    document.querySelector("#topic-rail")?.classList.toggle("open");
    roomDrawRail();
  },
  "rail-close": () => document.querySelector("#topic-rail")?.classList.remove("open"),
  "rail-comment-cancel": () => {
    const box = $("#rail-comment");
    if (box) box.hidden = true;
  },
  "room-tool-call": async (arg, target) => {
    const { id, index, item, action } = roomFindAction(arg);
    const topicId = RoomState.topic?.id;
    if (!action || !topicId) return;
    if (target) {
      target.disabled = true;
      target.textContent = "Running…";
    }
    const done = await attempt(() => api(`/topics/${topicId}/messages/${id}/tool-calls/${index}`, { method: "POST" }), "Done. The result is in the discussion.");
    if (done?.action && item) item.actions[index] = done.action;
    else if (target) {
      target.disabled = false;
      target.textContent = "Allow this call";
    }
    roomDraw();
  },
  "room-rework": async (arg) => {
    const { item, action } = roomFindAction(arg);
    if (!action) return;
    if ((await attempt(() => api(`/runs/${action.run_id}/rework`, { method: "POST", body: { comment: action.comment } }), "Reworking it. The new version comes back here.")) === undefined) return;
    action.done = true;
    if (item) roomDraw();
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "rail-comment-form": async (form) => {
    const topic = RoomState.topic;
    const comment = String(new FormData(form).get("comment") || "").trim();
    const runId = RoomState.rail?.runId;
    if (!topic || !runId || !comment || !RoomState.rail.quote) return;
    const sent = await attempt(
      () => api(`/topics/${topic.id}/revise`, { method: "POST", body: { run_id: runId, quote: RoomState.rail.quote, comment } }),
      "Revising that part. The new version comes back here."
    );
    if (sent === undefined) return;
    const box = $("#rail-comment");
    if (box) box.hidden = true;
  },
});

// ---- Decisions, only from you ----

async function openDecide(id) {
  const topic = RoomState.topic;
  const message = roomFind(id);
  if (!topic || !message) return;
  if (!topic.project) {
    toast("Place this discussion in a project first (Change, beside its title): a decision needs somewhere to hold.", true);
    return;
  }
  const current = await api(`/topics/${topic.id}/decisions`).catch(() => []);
  const choices = [["", "Nothing — it is a new decision"], ...current.map((d) => [d.id, roomShort(d.text, 90)])];
  formModal(
    "Record as a decision",
    "topic-decide-form",
    `<input type="hidden" name="message_id" value="${esc(id)}">
     ${field("The decision", "text", { type: "textarea", value: message.text, attrs: 'rows="3" maxlength="2000"' })}
     ${field("It replaces", "replaces", { type: "select", required: false, choices })}
     <p class="muted">Every agent working in ${esc(topic.project.name)} follows it from now on. What it replaces is kept as history, and no longer told to them.</p>`,
    "Record"
  );
}

Office.actions = Object.assign(Office.actions || {}, {
  "topic-decide": (id) => openDecide(id),
});

Office.forms = Object.assign(Office.forms || {}, {
  "topic-decide-form": async (form) => {
    const topic = RoomState.topic;
    const data = new FormData(form);
    if (!topic) return;
    const body = { message_id: String(data.get("message_id")), text: String(data.get("text") || "").trim(), replaces: String(data.get("replaces") || "") || null };
    const made = await attempt(() => api(`/topics/${topic.id}/decisions`, { method: "POST", body }), "Recorded. Agents here follow it from now on.");
    if (!made) return;
    closeModal();
    RoomState.decisionsFor = null;
    await roomRefresh();
    roomDrawRail();
  },
});

// ---- Colleagues and branches ----

function openBringIn() {
  const topic = RoomState.topic;
  if (!topic) return;
  const inside = new Set((topic.participants || []).map((p) => p.profile_id));
  const choices = (RoomState.agents || []).filter((a) => !inside.has(a.id)).map((a) => [a.id, `${a.name} · ${ROLE_LABEL[a.role] || a.role}`]);
  if (!choices.length) {
    toast("Every colleague is already in this discussion.");
    return;
  }
  formModal(
    "Bring someone in",
    "topic-bring-form",
    `${field("Colleague", "profile_id", { type: "select", choices })}
     <p class="muted">They are given this discussion's brief, not its whole history, and answer when you name them. Naming someone with @ brings them in too.</p>`,
    "Bring in"
  );
}

function openBranch(id) {
  const message = roomFind(id);
  if (!message) return;
  const brief = RoomState.topic?.brief || {};
  const carried = [brief.where, brief.open ? `Open there: ${brief.open}` : ""].filter(Boolean).join(" ") || `From ${RoomState.topic?.title || "the discussion"}.`;
  formModal(
    "Branch to a new topic",
    "topic-branch-form",
    `<input type="hidden" name="message_id" value="${esc(id)}">
     <p class="muted topic-branch-quote">“${esc(roomShort(message.text, 220))}”</p>
     ${field("Name", "title", { value: roomShort(message.text, 70), attrs: 'maxlength="200"' })}
     ${field("It starts with this brief, which you can edit", "brief", { type: "textarea", required: false, value: carried, attrs: 'rows="4" maxlength="1200"' })}
     <p class="muted">A link is left here, and this discussion carries on.</p>`,
    "Branch"
  );
}

Office.actions = Object.assign(Office.actions || {}, {
  "topic-bring": () => openBringIn(),
  "topic-branch": (id) => openBranch(id),
  "topic-welcome-close": () => {
    document.querySelector("[data-welcome-back]")?.remove();
    window.scrollTo(0, document.body.scrollHeight);
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "topic-bring-form": async (form) => {
    const topic = RoomState.topic;
    const profileId = String(new FormData(form).get("profile_id") || "");
    if (!topic || !profileId) return;
    if ((await attempt(() => api(`/topics/${topic.id}/participants`, { method: "POST", body: { profile_id: profileId } }), "They are in.")) === undefined) return;
    closeModal();
    rerender();
  },
  "topic-branch-form": async (form) => {
    const topic = RoomState.topic;
    const data = new FormData(form);
    if (!topic) return;
    const body = { message_id: String(data.get("message_id")), title: String(data.get("title") || "").trim() || null, brief: String(data.get("brief") || "").trim() || null };
    const child = await attempt(() => api(`/topics/${topic.id}/branch`, { method: "POST", body }));
    if (!child?.id) return;
    closeModal();
    go("topic", child.id);
  },
});

// Into a task's Team room, from a line in the project room.
Office.actions = Object.assign(Office.actions || {}, {
  "team-room": (runId) => {
    TaskRoom.tab = "room";
    location.hash = `task/${runId}`;
  },
});

// A thread belongs to its room: leaving the room closes it.
window.addEventListener("hashchange", () => {
  if (!location.hash.startsWith("#room/")) roomCloseThread();
});

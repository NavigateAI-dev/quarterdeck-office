"use strict";

// The thread's message box while work runs (learned from
// DeepSeek Harness's input bar): Queue or Steer, explicit; Stop; a strip above the box
// with the work and the owner's waiting words; and a decision taking the box's
// place, answered with Enter or Esc. Drawn into rooms.js's thread screen.

const Composer = {
  mode: "queue", // what Enter does while work runs; ⌘/Ctrl+Enter does the other
  notes: [], // the owner's words waiting on the running work
  notesFor: "",
  notesAt: 0,
  setAside: new Set(), // decisions the owner pressed Esc on, until the thread is left
  lastEsc: 0,
};

const COMPOSER_WORKING = ["PENDING", "RUNNING", "RECOVERING", "PAUSED"];
const NOTES_EVERY_MS = 4000;
const DOUBLE_ESC_MS = 500;

// The work running in this thread now, if any.
function composerWork() {
  return (roomReader.work || []).find((work) => COMPOSER_WORKING.includes(work.status)) || null;
}

function composerOther(mode) {
  return mode === "queue" ? "steer" : "queue";
}

async function composerLoadNotes(work) {
  if (!work) {
    Composer.notes = [];
    Composer.notesFor = "";
    return;
  }
  if (Composer.notesFor === work.run_id && Date.now() - Composer.notesAt < NOTES_EVERY_MS) return;
  try {
    const controls = await api(`/runs/${work.run_id}/controls`);
    Composer.notes = (Array.isArray(controls) ? controls : []).filter(
      (c) => ["queued", "instruction"].includes(c.kind) && c.status === "QUEUED"
    );
    Composer.notesFor = work.run_id;
    Composer.notesAt = Date.now();
  } catch (error) {
    if (!(error instanceof SignedOut)) console.warn("Waiting notes could not be read:", error);
  }
}

function composerNoteRow(note, work) {
  const label = note.kind === "queued" ? "Queued" : "Steering";
  const steer = note.kind === "queued" ? btn("Steer now", `composer-steer:${work.run_id}:${note.id}`, "room-tool") : "";
  return `<div class="composer-note"><span class="composer-eyebrow">${esc(label)}</span><span class="composer-note-text">${esc(roomShort(note.text || "", 200))}</span>${btn("Edit", `composer-edit:${work.run_id}:${note.id}`, "room-tool")}${steer}${btn("✕", `composer-remove:${work.run_id}:${note.id}`, "room-tool", 'aria-label="Remove this message"')}</div>`;
}

// The strip above the box: what the work is doing, and the owner's waiting words.
function composerStack(work) {
  if (!work) return "";
  const done = work.steps ? Math.min(work.steps_done, work.steps) : 0;
  const share = work.steps ? Math.round((100 * done) / work.steps) : 8;
  const where = [work.steps ? `${done} of ${work.steps} steps` : NODE_LABEL[work.step] || "Working", work.doing || ""].filter(Boolean).join(" · ");
  const spend = `${money(work.spent_usd || 0)}${work.max_cost_usd ? ` of ${money(work.max_cost_usd)}` : ""}`;
  const paused = work.status === "PAUSED" ? " · paused" : "";
  return `<div class="composer-plan"><span class="composer-eyebrow">Work</span><span class="composer-plan-what">${esc(roomShort(work.title || "The work", 80))} · ${esc(where)}${esc(paused)}</span><span class="composer-bar" aria-hidden="true"><span style="width:${Math.max(4, share)}%"></span></span><span class="composer-spend">${esc(spend)}</span></div>${Composer.notes
    .map((note) => composerNoteRow(note, work))
    .join("")}`;
}

// The round send button: an arrow, or a square when pressing it stops the work.
function composerSendFace(send, icon, label) {
  if (send.title === label) return;
  send.innerHTML = iconFace(icon, label);
  send.title = label;
  send.setAttribute("aria-label", label);
}

// The box itself: the mode switch, the send button's words, and the hint.
function composerDrawBox(work) {
  const mode = $("#composer-mode");
  const send = document.getElementById("room-send");
  const hint = $("#room-hint");
  const text = $("#room-text");
  if (!mode || !send || !hint || !text) {
    console.error("Composer parts not found: #composer-mode / #room-send / #room-hint / #room-text");
    return;
  }
  if (!work) {
    mode.hidden = true;
    composerSendFace(send, "send", "Send");
    send.dataset.stop = "";
    text.placeholder = "Message the lead, or @ a colleague…";
    hint.textContent = "Enter sends · Shift Enter for a new line · drop or paste files";
    return;
  }
  mode.hidden = false;
  mode.querySelectorAll("[data-mode]").forEach((button) => {
    const on = button.dataset.mode === Composer.mode;
    button.setAttribute("aria-pressed", String(on));
    button.classList.toggle("on", on);
  });
  const empty = !text.value.trim();
  send.dataset.stop = empty ? "1" : "";
  composerSendFace(send, empty ? "stop" : "send", empty ? "Stop" : Composer.mode === "queue" ? "Queue message" : "Steer now");
  text.placeholder = Composer.mode === "queue" ? "A note for after this work…" : "Redirect the work at its next step…";
  hint.textContent =
    Composer.mode === "queue"
      ? "Enter queues for after the work · ⌘ Enter steers it now · Esc Esc stops"
      : "Enter steers the work now · ⌘ Enter queues for after · Esc Esc stops";
}

// What the owner must decide here, taking the box's place.
function composerDecision() {
  return (roomReader.waiting || []).find((q) => !Composer.setAside.has(`${q.kind}:${q.run_id}:${q.record_id || ""}`)) || null;
}

function composerTakeover(question) {
  const task = esc(question.task || "The work");
  const key = `${question.kind}:${question.run_id}:${question.record_id || ""}`;
  const later = btn("Later", `composer-later:${key}`, "", 'data-esc="1"');
  const card = (title, body, buttons) =>
    `<div class="composer-decision"><div class="composer-decision-strip"><i class="side-dot needs" aria-hidden="true"></i><span>Waiting for you</span></div><div class="composer-decision-body"><strong>${title}</strong>${body}<div class="composer-decision-actions">${buttons}</div><p class="composer-keys">Enter ${esc(
      question.kind === "policy" ? "allows" : "opens it"
    )} · Esc ${esc(question.kind === "policy" ? "refuses" : "later")}</p></div></div>`;
  if (question.kind === "policy") {
    return card(
      `${task} is asking you`,
      `<p>${esc(question.reason || "")}</p>`,
      `${btn("Refuse", `room-policy:${question.record_id}:no`, "danger", 'data-esc="1"')}${btn("Allow for this task", `room-policy:${question.record_id}:yes`, "primary", 'data-enter="1"')}`
    );
  }
  if (question.kind === "agent") {
    // A coding agent's permission question: its own choices, refused if unanswered.
    const options = Array.isArray(question.options) ? question.options : [];
    const allow = options.find((o) => !String(o.kind || "").startsWith("reject"));
    const buttons = options
      .map((o) => {
        const reject = String(o.kind || "").startsWith("reject");
        const keys = o === allow ? 'data-enter="1"' : reject ? 'data-esc="1"' : "";
        return btn(o.name || o.id, `agent-answer:${question.run_id}:${question.ask_id}:${o.id}`, reject ? "danger" : o === allow ? "primary" : "", keys);
      })
      .join("");
    const until = question.expires_at ? ` Refused at ${new Date(question.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} if you do not answer.` : "";
    return card(`${task}: a coding agent asks you`, `<p>${esc(question.reason || "")}</p><p class="muted">${esc(until)}</p>`, buttons);
  }
  if (question.kind === "budget") {
    return card(`${task} stopped at its limit`, `<p>It kept everything it did. Raise the limit to go on, or open it to stop.</p>`, `${later}<a class="button primary" data-enter="1" href="#task/${esc(question.run_id)}">Raise or stop →</a>`);
  }
  const say = {
    approval: ["needs your approval", "Read the proposal: nothing is carried out or published without you.", "Review the proposal →"],
    stuck: ["is stuck", `${question.reason || ""} It asks for a hint, a skip, or to finish.`, "Help it →"],
  }[question.kind] || ["needs your review", stopText(question.reason), "Decide what happens →"];
  return card(`${task} ${esc(say[0])}`, `<p>${esc(say[1])}</p>`, `${later}<a class="button primary" data-enter="1" href="#task/${esc(question.run_id)}">${esc(say[2])}</a>`);
}

// Everything the box shows, redrawn with the thread.
async function composerDraw() {
  const form = $("#room-form");
  const stack = $("#composer-stack");
  const takeover = $("#composer-takeover");
  if (!form || !stack || !takeover) return;
  const question = composerDecision();
  takeover.hidden = !question;
  form.hidden = Boolean(question);
  takeover.innerHTML = question ? composerTakeover(question) : "";
  const work = composerWork();
  await composerLoadNotes(work);
  stack.hidden = !work;
  stack.innerHTML = composerStack(work);
  composerDrawBox(work);
  if (typeof composerPlusDraw === "function") composerPlusDraw();
}

// Enter decides in the takeover; Esc sets it aside or refuses. Never while typing elsewhere.
function composerTakeoverKeys(event) {
  const takeover = $("#composer-takeover");
  if (!takeover || takeover.hidden || event.isComposing || event.metaKey || event.ctrlKey || event.altKey) return;
  const editing = event.target?.closest?.("input, textarea, [contenteditable]");
  if (editing) return;
  const chosen = event.key === "Enter" ? takeover.querySelector('[data-enter="1"]') : event.key === "Escape" ? takeover.querySelector('[data-esc="1"]') : null;
  if (!chosen) return;
  event.preventDefault();
  chosen.click();
}
document.addEventListener("keydown", composerTakeoverKeys);

// Sending while work runs: queued for after it, or steering it now.
async function composerSendToWork(work, text, mode) {
  if (mode === "queue") {
    return attempt(() => api(`/runs/${work.run_id}/queue`, { method: "POST", body: { text } }), "Queued. It is said here when the work ends.");
  }
  return attempt(
    () => api(`/runs/${work.run_id}/controls`, { method: "POST", body: { kind: "instruction", text } }),
    "Steering: the work takes it at its next step."
  );
}

async function composerStop(work) {
  const done = await attempt(() => api(`/runs/${work.run_id}/controls`, { method: "POST", body: { kind: "pause" } }), "Stopping at the next step. It keeps everything it did; resume it from the card.");
  if (done !== undefined) await roomRefresh();
}

Office.actions = Object.assign(Office.actions || {}, {
  "agent-answer": async (arg) => {
    const [runId, askId, ...rest] = String(arg).split(":");
    const option = rest.join(":");
    const said = await attempt(() => api(`/runs/${runId}/agent-asks/${askId}`, { method: "POST", body: { option } }), "Answered. The agent goes on with your choice.");
    if (said !== undefined) await (typeof roomRefresh === "function" ? roomRefresh() : rerender());
  },
  "composer-mode": async (mode) => {
    if (!["queue", "steer"].includes(mode)) {
      console.error(`Unknown composer mode: ${mode}`);
      return;
    }
    Composer.mode = mode;
    composerDrawBox(composerWork());
    $("#room-text")?.focus();
  },
  "composer-later": async (arg) => {
    Composer.setAside.add(arg);
    await composerDraw();
    $("#room-text")?.focus();
  },
  "composer-remove": async (arg) => {
    const [runId, controlId] = String(arg).split(":");
    if ((await attempt(() => api(`/runs/${runId}/controls/${controlId}`, { method: "DELETE" }), "Removed.")) === undefined) return;
    Composer.notesAt = 0;
    await composerDraw();
  },
  "composer-steer": async (arg) => {
    const [runId, controlId] = String(arg).split(":");
    if ((await attempt(() => api(`/runs/${runId}/controls/${controlId}/steer`, { method: "POST" }), "Steering: the work takes it at its next step.")) === undefined) return;
    Composer.notesAt = 0;
    await composerDraw();
  },
  "composer-edit": async (arg) => {
    const [runId, controlId] = String(arg).split(":");
    const note = Composer.notes.find((n) => n.id === controlId);
    if (!note) {
      console.error(`Waiting note not found: ${controlId}`);
      return;
    }
    formModal(
      "Change your message",
      "composer-edit-form",
      `<input type="hidden" name="run" value="${esc(runId)}"><input type="hidden" name="control" value="${esc(controlId)}"><label class="field" for="composer-edit-text">It has not been taken yet</label><textarea id="composer-edit-text" name="text" rows="3" maxlength="4000" required>${esc(note.text || "")}</textarea>`,
      "Save"
    );
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "composer-edit-form": async (form) => {
    const data = new FormData(form);
    const runId = String(data.get("run") || "");
    const controlId = String(data.get("control") || "");
    const text = String(data.get("text") || "").trim();
    if (!runId || !controlId || !text) return;
    if ((await attempt(() => api(`/runs/${runId}/controls/${controlId}`, { method: "PUT", body: { text } }), "Changed.")) === undefined) return;
    closeModal();
    Composer.notesAt = 0;
    await composerDraw();
  },
});

// ---- The everyday box (from DSH's input bar) ----
// Grows with what is typed up to about 14 lines; a draft kept per thread in
// this browser; files pasted or dropped in are attached as with Files.

const COMPOSER_MAX_LINES = 14;

function composerDraftKey() {
  const topicId = RoomState.topic?.id;
  return topicId ? `draft:${topicId}` : "";
}

function composerGrow(textarea) {
  const line = parseFloat(getComputedStyle(textarea).lineHeight) || 24;
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, line * COMPOSER_MAX_LINES + 16)}px`;
}

// The files in a paste or a drop, attached to the thread's message.
function composerTakeFiles(list) {
  const files = [...(list || [])].filter((file) => file && file.size >= 0 && file.name);
  if (!files.length) return false;
  const label = files.length === 1 ? files[0].name : `${files[0].name} and ${files.length - 1} more`;
  attachUpload("room-form", files, label);
  return true;
}

function composerWireBox() {
  const textarea = $("#room-text");
  const form = $("#room-form");
  if (!textarea || !form) {
    console.error("Composer box not found: #room-text / #room-form");
    return;
  }
  const key = composerDraftKey();
  if (key && !textarea.value) textarea.value = remembered(key, "") || "";
  composerGrow(textarea);
  textarea.addEventListener("input", () => {
    composerGrow(textarea);
    if (key) remember(key, textarea.value);
  });
  textarea.addEventListener("paste", (event) => {
    if (composerTakeFiles(event.clipboardData?.files)) event.preventDefault();
  });
  form.addEventListener("dragover", (event) => {
    if ([...(event.dataTransfer?.types || [])].includes("Files")) {
      event.preventDefault();
      form.classList.add("is-dropping");
    }
  });
  form.addEventListener("dragleave", () => form.classList.remove("is-dropping"));
  form.addEventListener("drop", (event) => {
    form.classList.remove("is-dropping");
    if (composerTakeFiles(event.dataTransfer?.files)) event.preventDefault();
  });
  if (typeof composerPlusWire === "function") composerPlusWire();
}

// Sent: the draft is gone and the box is one line again.
function composerSent() {
  const textarea = $("#room-text");
  const key = composerDraftKey();
  if (key) remember(key, "");
  if (textarea) composerGrow(textarea);
  if (typeof composerPlusSent === "function") composerPlusSent();
}

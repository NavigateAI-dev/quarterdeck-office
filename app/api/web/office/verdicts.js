"use strict";

// The owner's verdict on an answer: 👍 or 👎 on each
// reply in a discussion and on a finished task's answer, and "Redo with the
// full team" when the lead fell short. How the lead is judged: on real use,
// not by a paid judge. Pressing never calls a model;
// Redo starts work, within the thread's usual cap, as Start does.

function verdictButtons(current, upAction, downAction) {
  const up = current?.verdict === "up";
  const down = current?.verdict === "down";
  return `${btn(composerIcon("good"), upAction, `verdict-btn${up ? " on" : ""}`, `aria-pressed="${up}" aria-label="A good answer" title="A good answer"`)}${btn(composerIcon("bad"), downAction, `verdict-btn${down ? " on" : ""}`, `aria-pressed="${down}" aria-label="Not good enough" title="Not good enough"`)}`;
}

// Under an agent's reply: the verdict, and the full team when it fell short.
function roomVerdictRow(item) {
  if (item?.author?.kind !== "agent" || !item.reply_to) return "";
  const id = String(item.id || "").replace(/^m:/, "");
  const current = item.verdict || null;
  const redo = current?.redo_run_id
    ? `<a class="verdict-done" href="#task/${esc(current.redo_run_id)}">Redone by the full team →</a>`
    : btn("Redo with full team", `verdict-redo:${id}`, "verdict-redo");
  const note = current?.note ? `<span class="verdict-note">“${esc(roomShort(current.note, 80))}”</span>` : "";
  return `<div class="verdict-row" data-verdict-for="${esc(id)}">${verdictButtons(current, `verdict-reply:${id}:up`, `verdict-reply:${id}:down`)}${note}${current?.verdict === "down" || current?.redo_run_id ? redo : ""}</div>`;
}

function verdictItem(id) {
  return typeof roomReader !== "undefined" ? roomReader.items.find((entry) => entry.id === `m:${id}`) : null;
}

async function verdictReply(id, verdict, note) {
  const topicId = RoomState.topic?.id;
  if (!topicId) return;
  const body = note ? { verdict, note } : { verdict };
  const saved = await attempt(() => api(`/topics/${topicId}/messages/${id}/verdict`, { method: "PUT", body }), verdict === "up" ? "Noted: a good answer." : "Noted. Redo it with the full team if you like.");
  if (saved === undefined) return;
  const item = verdictItem(id);
  if (item) item.verdict = saved;
  closeModal();
  if (typeof roomDraw === "function") roomDraw();
}

function verdictWhyDialog(id) {
  formModal(
    "What was wrong?",
    "verdict-why",
    `<input type="hidden" name="target" value="${esc(id)}">
     <label for="verdict-why-text">Optional: a line on what was missing or wrong</label>
     <textarea id="verdict-why-text" name="note" maxlength="1000" rows="3" placeholder="e.g. No source for the rate; it was last year's"></textarea>
     <p class="muted">Kept with the verdict, to judge the lead on real use. Nothing is sent to a model.</p>`,
    "Save"
  );
  $("#verdict-why-text")?.focus();
}

// A finished task's answer: the same verdict, read and saved by the task.
async function taskVerdictDraw(run) {
  const slot = $("#task-verdict");
  if (!slot || !run?.id) return;
  const got = await api(`/runs/${run.id}/verdict`).catch((error) => {
    console.error("Could not read the task's verdict", error);
    return null;
  });
  const current = got?.verdict || null;
  slot.innerHTML = `<span class="verdict-ask">Was this answer good?</span>${verdictButtons(current, `verdict-run:${run.id}:up`, `verdict-run:${run.id}:down`)}${current?.note ? `<span class="verdict-note">“${esc(roomShort(current.note, 80))}”</span>` : ""}`;
}

function taskVerdictSlot(run) {
  if (run?.status !== "COMPLETED") return "";
  setTimeout(() => taskVerdictDraw(run), 0);
  return `<div id="task-verdict" class="verdict-row task-verdict" aria-label="Your verdict on this answer"></div>`;
}

Office.forms = Object.assign(Office.forms || {}, {
  "verdict-why": async (form) => {
    const data = new FormData(form);
    // Named "target": an input named "id" would hide the form's own id from the dispatcher.
    const id = String(data.get("target") || "");
    const note = String(data.get("note") || "").trim();
    if (id.startsWith("run:")) {
      const runId = id.slice(4);
      const saved = await attempt(() => api(`/runs/${runId}/verdict`, { method: "PUT", body: note ? { verdict: "down", note } : { verdict: "down" } }), "Noted.");
      if (saved === undefined) return;
      closeModal();
      taskVerdictDraw({ id: runId });
      return;
    }
    await verdictReply(id, "down", note);
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "verdict-reply": (arg) => {
    const [id, verdict] = String(arg).split(":");
    if (verdict === "down") return verdictWhyDialog(id);
    return verdictReply(id, "up");
  },
  "verdict-redo": (id) => {
    const item = verdictItem(id);
    modal(
      "Redo with the full team?",
      `<p>The full team takes up the question this reply answered, here in this discussion, within this thread's usual limit. The reply is marked 👎.</p>${item ? `<p class="idea-quote">${esc(roomShort(item.text, 200))}</p>` : ""}`,
      `<div class="button-row">${btn("Not now", "close")}${btn("Redo with full team", `verdict-redo-go:${id}`, "primary")}</div>`
    );
  },
  "verdict-redo-go": async (id) => {
    const topicId = RoomState.topic?.id;
    if (!topicId) return;
    const saved = await attempt(() => api(`/topics/${topicId}/messages/${id}/redo`, { method: "POST" }), "Started with the full team. The result comes back here.");
    if (saved === undefined) return;
    const item = verdictItem(id);
    if (item) item.verdict = saved;
    closeModal();
    if (typeof roomDraw === "function") roomDraw();
  },
  "verdict-run": async (arg) => {
    const [runId, verdict] = String(arg).split(":");
    if (verdict === "down") return verdictWhyDialog(`run:${runId}`);
    const saved = await attempt(() => api(`/runs/${runId}/verdict`, { method: "PUT", body: { verdict: "up" } }), "Noted: a good answer.");
    if (saved !== undefined) taskVerdictDraw({ id: runId });
  },
});

// Settings → Models & spending → Performance: the lead beside the full team
// on real use, with the owner's verdicts and reworks.
async function leadQualityDraw() {
  const card = $("#lead-quality");
  if (!card) return;
  const report = await api("/quality/lead?days=30").catch((error) => {
    console.error("Could not read the lead's numbers", error);
    return null;
  });
  if (!report) {
    card.innerHTML = `<h2>The lead on real use</h2><p class="muted">Could not be read just now.</p>`;
    return;
  }
  const secs = (v) => (v === null || v === undefined ? "–" : v >= 120 ? `${Math.round(v / 60)} min` : `${Math.round(v)} s`);
  const num = (v) => (v === null || v === undefined ? "–" : String(Math.round(v * 10) / 10));
  const row = (label, path) => {
    const p = report.paths?.[path];
    if (!p) return `<tr><th scope="row">${esc(label)}</th><td colspan="5" class="muted">No work in this period</td></tr>`;
    return `<tr><th scope="row">${esc(label)}</th><td class="num">${esc(p.runs)}</td><td class="num">${esc(num(p.median_calls))}</td><td class="num">${esc(secs(p.median_seconds))}</td><td class="num">${esc(money(p.median_cost_usd || 0))}</td><td class="num">${esc(p.failed)}</td></tr>`;
  };
  const v = report.verdicts || {};
  const r = report.replies || {};
  const judged = (v.up || 0) + (v.down || 0);
  card.innerHTML = `<h2>The lead on real use <small class="muted">last ${esc(report.days)} days</small></h2>
    <div class="pv-table-frame"><table class="pv-table"><caption class="visually-hidden">Per piece of work, lead beside team</caption>
      <thead><tr><th scope="col">Work by</th><th scope="col" class="num">Tasks</th><th scope="col" class="num">Calls each</th><th scope="col" class="num">Time each</th><th scope="col" class="num">Cost each</th><th scope="col" class="num">Failed</th></tr></thead>
      <tbody>${row("The lead", "lead")}${row("The full team", "team")}</tbody></table></div>
    <div class="grid2 lead-quality-facts">
      <div><div class="detail-line"><span>Replies in discussions</span><strong>${esc(r.count || 0)}</strong></div>
        <div class="detail-line"><span>Their cost</span><strong>${esc(money(r.total_cost_usd || 0))}</strong></div>
        <div class="detail-line"><span>Citations no tool returned</span><strong>${esc(r.unconfirmed_citations || 0)}</strong></div></div>
      <div><div class="detail-line"><span>Your verdicts</span><strong>${judged ? `👍 ${esc(v.up || 0)} · 👎 ${esc(v.down || 0)}` : "None yet"}</strong></div>
        <div class="detail-line"><span>Redone with the full team</span><strong>${esc(v.redone_with_team || 0)}</strong></div>
        <div class="detail-line"><span>Reworked within a day</span><strong>${esc(report.reworked_within_a_day || 0)}</strong></div></div>
    </div>
    <p class="muted">Medians per piece of work. Press 👍 or 👎 under a reply or a finished task to judge the answers; nothing here calls a model.</p>`;
}

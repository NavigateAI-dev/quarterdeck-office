// A task's model calls, as the models got them (app/model_calls.py):
// the "Model calls" tab of a task's page, and the calls behind a reply in a discussion. Each row is one call: role, model,
// tokens, time, outcome; opened, it shows the messages sent and the answer.
// Read when the tab is open, and again at most every 10 s.

const MODEL_CALLS_EVERY_MS = 10000;
// How much of one message the dialog shows before "… (n more characters)".
const MODEL_CALL_SHOWN = 4000;
const ModelCallsTab = { run: "", at: 0, calls: null };

function modelCallsTab() {
  return `<div id="model-calls" class="model-calls"><p class="muted">Reading the calls…</p></div>`;
}

async function loadModelCalls(runId, content) {
  const holder = content.querySelector("#model-calls");
  if (!holder) {
    console.error("Element not found: #model-calls");
    return;
  }
  const fresh = ModelCallsTab.run === runId && Date.now() - ModelCallsTab.at < MODEL_CALLS_EVERY_MS;
  if (!fresh) {
    const calls = await api(`/runs/${runId}/model-calls`).catch((error) => {
      if (!(error instanceof SignedOut)) console.warn("Model calls could not be read:", error);
      return null;
    });
    ModelCallsTab.run = runId;
    ModelCallsTab.at = Date.now();
    ModelCallsTab.calls = Array.isArray(calls) ? calls : null;
  }
  const calls = ModelCallsTab.calls;
  if (calls === null) {
    holder.innerHTML = `<p class="muted">The calls could not be read just now.</p>`;
    return;
  }
  if (!calls.length) {
    holder.innerHTML = `<p class="muted">No model calls kept for this task yet. Calls are kept from 20 October 2026 on.</p>`;
    return;
  }
  holder.innerHTML = `<p class="muted">Every call this task made, as the model got it. Kept so any step can be played again exactly.</p><ol class="model-call-list">${modelCallRows(calls)}</ol>`;
}

// One row per call: role, model, size, time and outcome; opened, the whole call.
function modelCallRows(calls) {
  return calls
    .map((call) => {
      const tokens = [call.prompt_tokens, call.completion_tokens].every((n) => typeof n === "number")
        ? `${call.prompt_tokens.toLocaleString()} in · ${call.completion_tokens.toLocaleString()} out`
        : "";
      const seconds = typeof call.latency_seconds === "number" ? `${call.latency_seconds.toFixed(1)} s` : "";
      const outcome = call.status === "ok" ? "" : tag(String(call.status || "").replace(/_/g, " "), "amber");
      return `<li class="model-call"><button type="button" class="model-call-open" data-action="model-call:${esc(call.id)}">
        <strong>${esc(ROLE_LABEL[call.role] || call.role)}</strong>
        <span class="muted">${esc(call.model || call.model_alias)}</span>
        <span class="muted">${esc(call.messages)} messages${tokens ? ` · ${esc(tokens)}` : ""}${seconds ? ` · ${esc(seconds)}` : ""}</span>
        ${outcome}<span class="muted">${esc(when(call.at))}</span></button></li>`;
    })
    .join("");
}

function modelCallText(value) {
  const said = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const text = String(said ?? "");
  return text.length > MODEL_CALL_SHOWN ? `${text.slice(0, MODEL_CALL_SHOWN)}… (${(text.length - MODEL_CALL_SHOWN).toLocaleString()} more characters)` : text;
}

Office.actions = Object.assign(Office.actions || {}, {
  // Under an agent's reply in a discussion: the calls behind it (kept from 26 October 2026).
  "room-calls": async (id) => {
    const topicId = RoomState.topic?.id;
    const message = String(id || "").replace(/^m:/, "");
    if (!topicId || !message) {
      console.error("Model calls: no discussion or message", { topicId, id });
      return;
    }
    const calls = await attempt(() => api(`/topics/${topicId}/messages/${message}/model-calls`), null);
    if (!Array.isArray(calls)) return;
    modal(
      "How this reply was made",
      calls.length
        ? `<p class="muted">Every call behind this reply, as the model got it. Open one to read it.</p><ol class="model-call-list">${modelCallRows(calls)}</ol>`
        : `<p class="muted">No model calls are kept for this reply. Calls behind replies are kept from 26 October 2026 on.</p>`
    );
  },
  "model-call": async (id) => {
    const call = await attempt(() => api(`/model-calls/${id}`), null);
    if (!call) return;
    const messages = (call.messages || [])
      .map((message) => {
        const role = message?.role || "message";
        const content = message?.content ?? message?.tool_calls ?? "";
        return `<section class="model-call-message"><h3>${esc(role)}</h3><pre>${esc(modelCallText(content))}</pre></section>`;
      })
      .join("");
    const answer = call.response
      ? `<section class="model-call-message"><h3>Answer</h3><pre>${esc(modelCallText(call.response.content || call.response.tool_calls || ""))}</pre></section>`
      : `<p class="muted">No answer: ${esc(String(call.status || "").replace(/_/g, " "))}.</p>`;
    modal(`${ROLE_LABEL[call.role] || call.role} · ${call.model || call.model_alias}`, `<div class="model-call-detail">${messages}${answer}</div>`);
  },
});

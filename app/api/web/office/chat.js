"use strict";

// Asking the office about what is on screen. It answers from what the office
// records; it approves nothing and starts nothing. The panel says which task
// or project it is asking about, because an agent that silently decides what
// "this" means is the thing that makes an office feel untrustworthy.

const Chat = { turns: [], open: false, busy: false };

function chatSubject() {
  if (Office.view === "task" && (TaskRoom.run || Office.param)) {
    return { view: "task", run_id: TaskRoom.run?.id || Office.param, label: "this task" };
  }
  if (Office.view === "project" && (Projects.current?.id || Office.param)) {
    return {
      view: "project",
      project_id: Projects.current?.id || Office.param,
      label: "this project",
    };
  }
  return { view: Office.view || "home", label: `the ${Office.view || "home"} screen` };
}

function chatBody() {
  const turns = Chat.turns.length
    ? Chat.turns
        .map(
          (turn) =>
            `<div class="chat-turn ${turn.role}"><p>${esc(turn.content)}</p></div>`
        )
        .join("")
    : `<p class="muted">Ask about a task, a project, or how the office is doing. It answers from its own records.</p>`;
  return `<div class="chat-log" id="chat-log">${turns}</div>
    <p class="labels"><span class="chip" title="What the office is told you are looking at">Asking about <strong>${esc(chatSubject().label)}</strong></span></p>
    <form class="compose" id="office-chat-form">
      <input name="text" maxlength="4000" placeholder="Ask the office…" aria-label="Ask the office" required ${Chat.busy ? "disabled" : ""}>
      <button class="primary" ${Chat.busy ? "disabled" : ""}>${Chat.busy ? "Asking…" : "Ask"}</button>
    </form>
    <p class="muted">It answers only. Approvals and new work are still yours.</p>`;
}

function openChat() {
  Chat.open = true;
  modal("Ask the office", chatBody(), "");
  const field = document.querySelector("#office-chat-form input");
  if (field) field.focus();
}

function redrawChat() {
  const content = $("#modal-content");
  if (!content || !Chat.open) return;
  openChat();
}

Office.actions = Object.assign(Office.actions || {}, {
  "chat-open": openChat,
});

Office.forms = Object.assign(Office.forms || {}, {
  "office-chat-form": async (form) => {
    const text = String(new FormData(form).get("text") || "").trim();
    if (!text || Chat.busy) return;
    const subject = chatSubject();
    Chat.turns.push({ role: "user", content: text });
    Chat.busy = true;
    redrawChat();
    const said = await attempt(() =>
      api("/chat", {
        method: "POST",
        body: {
          text,
          history: Chat.turns.slice(-8).map((t) => ({ role: t.role, content: t.content })),
          view: subject.view,
          run_id: subject.run_id || null,
          project_id: subject.project_id || null,
        },
      })
    );
    Chat.busy = false;
    if (said !== undefined) Chat.turns.push({ role: "assistant", content: said.reply });
    redrawChat();
    const log = $("#chat-log");
    if (log) log.scrollTop = log.scrollHeight;
  },
});

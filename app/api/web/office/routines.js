"use strict";

// Routines: work the office repeats on its own, answered in a discussion.
const Routines = { items: [], presets: [], channels: [], projects: [], todos: { open: [], finished: [] }, editing: null };
const ROUTINE_LAST = {
  started: "Working on it now",
  delivered: "Last one delivered",
  quiet: "Last time: nothing to report",
  no_answer: "Last time: no answer came",
  not_answered: "Last time: not answered, see its discussion",
  skipped_too_late: "Last one missed: the office was not running at that time",
  skipped_paused: "Last one missed: scheduled tasks are paused",
};

function routineRow(r) {
  const posted = Routines.channels.find((d) => d.id === r.post_to);
  const where = posted ? ` · also to ${esc(posted.name)}` : "";
  const state = r.enabled ? tag("On", "green") : tag("Off", "gray");
  const cost = typeof r.last_cost_usd === "number" ? ` · last cost $${r.last_cost_usd.toFixed(3)}` : "";
  const last = ROUTINE_LAST[r.last_outcome] ? `<p class="muted">${esc(ROUTINE_LAST[r.last_outcome])}${cost}</p>` : "";
  const home = r.topic_id ? `<a href="#topic/${esc(r.topic_id)}">${esc(r.topic_title || "Its discussion")}</a>` : "Its discussion is gone";
  return `<div class="setting-row" data-routine="${esc(r.id)}"><div><h3>${esc(r.title)} ${state}</h3>
      <p>${esc(r.when)} ${esc(r.timezone)} · ${home}${where}</p><p class="muted">${esc(r.prompt)}</p>${last}</div>
    <div class="button-row tight">${r.topic_id ? btn("Send one now", `routine-send:${r.id}`) : ""}${btn("Change", `routine-edit:${r.id}`)}${btn(r.enabled ? "Pause" : "Turn on", `routine-toggle:${r.id}`)}${btn("History", `routine-history:${r.id}`)}${btn("Delete", `routine-delete:${r.id}`)}</div></div>`;
}

const TODO_STATE = { open: ["Not heard", "gray"], doing: ["Going", "blue"], done: ["Done", "green"], dropped: ["Dropped", "gray"], parked: ["No answer, stopped asking", "gray"] };

function todoRow(t) {
  const state = TODO_STATE[t.status] || [t.status, "gray"];
  const active = t.status === "open" || t.status === "doing";
  const buttons = active ? `${btn("Done", `todo-set:${t.id}:done`)}${btn("Update", `todo-edit:${t.id}`)}${btn("Not a to-do", `todo-set:${t.id}:not_a_task`)}` : "";
  return `<div class="setting-row" data-todo="${esc(t.id)}"><div><h3>${esc(t.title)} ${tag(state[0], state[1])}</h3>
      <p>Due ${esc(t.due_on)}</p>${t.progress ? `<p class="muted">${esc(t.progress)}</p>` : ""}</div>
    <div class="button-row tight">${buttons}</div></div>`;
}

async function renderRoutines(content) {
  const [listed, channels, projects, todos] = await Promise.all([
    api("/routines"),
    api("/channels").catch(() => null),
    api("/projects").catch(() => []),
    api("/follow-ups").catch(() => null),
  ]);
  Routines.todos = { open: Array.isArray(todos?.open) ? todos.open : [], finished: Array.isArray(todos?.finished) ? todos.finished : [] };
  Routines.items = Array.isArray(listed?.routines) ? listed.routines : [];
  Routines.presets = Array.isArray(listed?.presets) ? listed.presets : [];
  Routines.channels = (channels?.destinations || []).filter((d) => !String(d.external_id).startsWith("D") && d.name !== "Chat thread");
  Routines.projects = Array.isArray(projects) ? projects : [];
  const starters = Routines.presets.map((p) => btn(p.title, `routine-new:${p.slug}`)).join("");
  content.innerHTML = `${heading("ROUTINES", "Work your office repeats on its own.", "Ask for one in any discussion, for example “every morning at 7:30, recap my day”, or set one here. Each is answered in its discussion and, if you choose, posted to a channel. It needs this computer awake at that time.")}
    <div class="card"><h2>Start one</h2><div class="button-row">${starters}${btn("Your own", "routine-new:", "primary")}</div></div>
    <div class="card spaced"><h2>Your routines</h2>${Routines.items.map(routineRow).join("") || `<p class="muted">None yet.</p>`}</div>
    <div class="card spaced"><h2>To-dos being followed</h2><p class="muted">From your calendar and the ideas you planned for a day. The Progress check routine asks about them, and the recaps report what finished.</p>${[...Routines.todos.open, ...Routines.todos.finished].map(todoRow).join("") || `<p class="muted">Nothing due.</p>`}</div>`;
}

function routineForm(values) {
  const channels = Routines.channels;
  const hour = values.local_time || "09:00";
  const isNew = !values.id;
  formModal(
    values.id ? `Change ${values.title}` : "New routine",
    "routine-form",
    `${field("Name", "title", { value: values.title || "" })}
     ${field("What to do each time", "prompt", { type: "textarea", value: values.prompt || "", attrs: 'maxlength="4000" rows="6"', hint: "Say everything you want from it, in the language you want it in." })}
     ${field("How often", "frequency", { type: "select", value: values.frequency || "daily", choices: [["daily", "Every day"], ["weekly", "Every week"], ["hours", "Every few hours"], ["cron", "Cron expression"], ["once", "Once"]] })}
     ${field("Time", "local_time", { type: "time", required: false, value: hour })}
     ${field("Day (weekly)", "weekday", { type: "select", required: false, value: String(values.weekday ?? 0), choices: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((d, i) => [String(i), d]) })}
     ${field("Hours between (every few hours)", "every_hours", { type: "number", required: false, value: values.every_hours || "" })}
     ${field("Cron expression", "cron", { required: false, value: values.cron || "", hint: "Five fields, for example 0 9 * * 1-5." })}
     ${field("Date and time (once)", "once_at", { type: "datetime-local", required: false })}
     ${field("Stay quiet when", "quiet_when", { required: false, value: values.quiet_when || "", hint: "Optional, for example: no deadline is at risk. Then nothing is posted." })}
     ${isNew ? field("Project", "project_id", { type: "select", required: false, value: "", choices: [["", "No project"], ...Routines.projects.map((p) => [p.id, p.name])], hint: "It can use the tools granted to this project." }) : ""}
     ${channels.length ? field("Also post it to", "post_to", { type: "select", required: false, value: values.post_to || "", choices: [["", "Nowhere, keep it in the office"], ...channels.map((d) => [d.id, d.name])] }) : `<p class="muted">To post it to Slack too, add the channel under Connections → Chat channels first.</p>`}
     <p class="muted">Time zone: ${esc(Intl.DateTimeFormat().resolvedOptions().timeZone)}.</p>`,
    "Save"
  );
}

Office.actions = Object.assign(Office.actions || {}, {
  "todo-set": async (value) => {
    const [id, status] = String(value).split(":");
    if ((await attempt(() => api(`/follow-ups/${id}`, { method: "PUT", body: { status } }), "Recorded.")) !== undefined) rerender();
  },
  "todo-edit": (id) => {
    const todo = Routines.todos.open.find((t) => t.id === id);
    if (!todo) {
      console.error("To-do not found:", id);
      return;
    }
    Routines.editing = { todo: id };
    formModal(
      todo.title,
      "todo-form",
      `${field("Where it stands", "note", { type: "textarea", value: todo.progress || "", attrs: 'maxlength="2000" rows="3"' })}
       ${field("Ask again on", "next_on", { type: "date", required: false, value: "" })}`,
      "Save"
    );
  },
  "routine-new": (slug) => {
    const preset = Routines.presets.find((p) => p.slug === slug);
    Routines.editing = { preset: preset?.slug || null };
    routineForm(preset || {});
  },
  "routine-edit": (id) => {
    const routine = Routines.items.find((r) => r.id === id);
    if (!routine) {
      console.error("Routine not found:", id);
      return;
    }
    Routines.editing = { id };
    routineForm(routine);
  },
  "routine-send": async (id) => {
    if ((await attempt(() => api(`/routines/${id}/send`, { method: "POST" }), "Started. It arrives in its discussion.")) !== undefined) rerender();
  },
  "routine-toggle": async (id) => {
    const routine = Routines.items.find((r) => r.id === id);
    if (!routine) {
      console.error("Routine not found:", id);
      return;
    }
    if ((await attempt(() => api(`/routines/${id}`, { method: "PUT", body: { enabled: !routine.enabled } }), routine.enabled ? "Paused." : "Turned on.")) !== undefined) rerender();
  },
  "routine-delete": async (id) => {
    if (!window.confirm("Delete this routine? Its discussion stays.")) return;
    if ((await attempt(() => api(`/routines/${id}`, { method: "DELETE" }), "Routine deleted.")) !== undefined) rerender();
  },
  "routine-history": async (id) => {
    const seen = await attempt(() => api(`/routines/${id}/history`));
    if (seen === undefined) return;
    const rows = Array.isArray(seen?.history) ? seen.history : [];
    modal(
      "History",
      rows.map((h) => `<div class="detail-line"><span>${esc(when(h.at))}${typeof h.cost_usd === "number" ? ` · $${h.cost_usd.toFixed(3)}` : ""}</span></div><p>${esc(h.answer || "No answer came.")}</p>`).join("") || "<p>It has not run yet.</p>"
    );
  },
});

Office.screens.routines = { title: "Routines", render: renderRoutines };

Office.forms = Object.assign(Office.forms || {}, {
  "todo-form": async (form) => {
    const data = new FormData(form);
    const id = Routines.editing?.todo;
    if (!id) {
      console.error("No to-do being edited");
      return;
    }
    const body = { note: String(data.get("note") || "").trim() };
    const next = String(data.get("next_on") || "");
    if (next) body.next_on = next;
    if ((await attempt(() => api(`/follow-ups/${id}`, { method: "PUT", body }), "Recorded.")) === undefined) return;
    closeModal();
    rerender();
  },
  "routine-form": async (form) => {
    const data = new FormData(form);
    const text = (name) => String(data.get(name) || "").trim();
    const frequency = text("frequency");
    const body = {
      title: text("title"),
      prompt: text("prompt"),
      quiet_when: text("quiet_when"),
      frequency,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    };
    if (frequency === "daily" || frequency === "weekly") body.local_time = text("local_time") || "09:00";
    if (frequency === "weekly") body.weekday = Number(text("weekday") || 0);
    if (frequency === "hours") body.every_hours = Number(text("every_hours"));
    if (frequency === "cron") body.cron = text("cron");
    if (frequency === "once" && text("once_at")) body.once_at = new Date(text("once_at")).toISOString();
    if (form.elements.post_to) body.post_to = text("post_to") || null;
    const editing = Routines.editing || {};
    if (!editing.id) {
      body.preset = editing.preset || null;
      body.project_id = text("project_id") || null;
    }
    const save = () => (editing.id ? api(`/routines/${editing.id}`, { method: "PUT", body }) : api("/routines", { method: "POST", body }));
    if ((await attempt(save, "Routine saved.")) === undefined) return;
    closeModal();
    rerender();
  },
});

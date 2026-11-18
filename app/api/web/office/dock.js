// The dock beside a thread: three
// tabs. Board is the work's plan, wave by wave or the lead's own checklist,
// and what the thread has kept; Draft the latest answer, where a passage takes
// a comment; Calendar the owner's day, so the work fits around it.
// rooms.js opens, closes and redraws it (roomDrawRail); this file draws it.

const DOCK_TABS = [
  ["board", "Board"],
  ["draft", "Draft"],
  ["files", "Files"],
  ["calendar", "Calendar"],
];
const DOCK_DONE = ["COMPLETED", "CLOSED", "NEEDS_HUMAN_REVIEW"];
const DOCK_STEP_DONE = ["done", "empty", "failed"];
// The thread redraws the dock as it hears news; the plan and the day are read
// again at most this often.
const DOCK_FLOW_EVERY_MS = 10000;
const DOCK_DAY_EVERY_MS = 60000;

const Dock = { flow: null, flowFor: "", flowAt: 0, day: null, dayAt: 0, files: null, filesFor: "", filesAt: 0, browse: null };

// The tab this browser last looked at, Board when it has none.
function dockKeptTab() {
  const kept = remembered("dockTab", "board");
  return DOCK_TABS.some(([id]) => id === kept) ? kept : "board";
}

function dockWorks() {
  return [...(roomReader.work || [])].sort((a, b) => String(b.started_at || "").localeCompare(String(a.started_at || "")));
}

// The work the Board follows: what runs now, or else the latest.
function dockWork() {
  return (typeof composerWork === "function" && composerWork()) || dockWorks()[0] || null;
}

function dockTabs(mode) {
  const tabs = DOCK_TABS.map(
    ([id, label]) =>
      `<button type="button" role="tab" id="dock-tab-${id}" class="dock-tab" data-action="dock-tab:${id}" aria-selected="${mode === id}" aria-controls="dock-panel">${esc(label)}</button>`
  ).join("");
  return `<div class="dock-tabs" role="tablist" aria-label="Beside the thread">${tabs}${btn("✕", "rail-close", "room-tool rail-close", 'aria-label="Close"')}</div>`;
}

async function dockRender(rail) {
  const mode = DOCK_TABS.some(([id]) => id === rail.mode) ? rail.mode : "board";
  const body =
    mode === "draft" ? await dockDraft(rail) : mode === "files" ? await dockFiles() : mode === "calendar" ? await dockCalendar() : await dockBoard();
  return `${dockTabs(mode)}<div class="dock-panel" id="dock-panel" role="tabpanel" aria-labelledby="dock-tab-${mode}">${body}</div>`;
}

// ---- Board ----

async function dockFlow(work) {
  const fresh = Dock.flowFor === work.run_id && Date.now() - Dock.flowAt < DOCK_FLOW_EVERY_MS;
  if (!fresh) {
    Dock.flow = await api(`/runs/${work.run_id}/flow`).catch((error) => {
      if (!(error instanceof SignedOut)) console.warn("The plan could not be read:", error);
      return null;
    });
    Dock.flowFor = work.run_id;
    Dock.flowAt = Date.now();
  }
  return Dock.flow;
}

function dockMark(state) {
  if (state === "working") return '<i class="side-spin" aria-hidden="true"></i>';
  return `<i class="side-dot ${state === "done" ? "done" : "idle"}" aria-hidden="true"></i>`;
}

// What the owner may do with a step still to come (13d): have it done next,
// or skip it. Only while the work runs; a running or finished step is left be.
const DOCK_STEERABLE = ["PENDING", "RUNNING", "RECOVERING", "PAUSED"];

function dockStepTools(step, work) {
  if (!work?.run_id || !DOCK_STEERABLE.includes(work.status) || step.status !== "pending") return "";
  const target = `${work.run_id}:${step.id}`;
  return `<span class="dock-step-tools">${btn("Do next", `dock-step-next:${target}`, "room-tool", `aria-label="Do next: ${esc(roomShort(step.brief, 60))}"`)}${btn("Skip", `dock-step-skip:${target}`, "room-tool", `aria-label="Skip: ${esc(roomShort(step.brief, 60))}"`)}</span>`;
}

// One wave of a team plan: the steps that can run together.
function dockWave(level, steps, first, work) {
  const state = steps.every((s) => DOCK_STEP_DONE.includes(s.status)) ? "done" : steps.some((s) => s.status === "running") ? "working" : "waiting";
  const rows = steps
    .map(
      (s, i) =>
        `<li class="dock-step is-${esc(s.status)}"><span>${first + i} · ${esc(roomShort(s.brief || s.kind, 90))}</span><span class="dock-who">${esc(ROLE_LABEL[s.role] || s.role || "")}</span>${dockStepTools(s, work)}</li>`
    )
    .join("");
  return `<section class="dock-wave is-${state}"><p class="composer-eyebrow">${dockMark(state)} Wave ${level} · ${state}</p><ol class="dock-steps">${rows}</ol></section>`;
}

// The lead's own checklist, as it keeps it.
function dockTodo(todo) {
  const rows = todo
    .map((item) => {
      const state = item.status === "done" ? "done" : item.status === "in_progress" ? "working" : "waiting";
      return `<li class="dock-step is-${state}">${dockMark(state)}<span>${esc(item.text)}</span></li>`;
    })
    .join("");
  return `<section class="dock-wave"><ol class="dock-steps dock-todo">${rows}</ol></section>`;
}

async function dockPlan(work) {
  if (Array.isArray(work.todo) && work.todo.length) return dockTodo(work.todo);
  const flow = await dockFlow(work);
  const steps = Array.isArray(flow?.steps) ? flow.steps.filter((s) => s.level) : [];
  if (!steps.length) return `<p class="muted">${esc(work.doing || "No plan yet. The steps show here once there is one.")}</p>`;
  const levels = [...new Set(steps.map((s) => s.level))].sort((a, b) => a - b);
  let first = 1;
  return levels
    .map((level) => {
      const wave = steps.filter((s) => s.level === level);
      const drawn = dockWave(level, wave, first, work);
      first += wave.length;
      return drawn;
    })
    .join("");
}

async function dockBoard() {
  const work = dockWork();
  const plan = work
    ? `<section class="dock-plan" aria-label="The plan"><h2 class="dock-title">${esc(work.title)}</h2>${await dockPlan(work)}</section>`
    : `<p class="muted">Nothing is being worked on here. Say what you want done, and its plan shows here.</p>`;
  return `${plan}<div class="dock-kept">${railLeft()}</div>`;
}

// ---- Draft ----

async function dockDraft(rail) {
  const delivered = dockWorks().find((w) => DOCK_DONE.includes(w.status));
  // An older thread names its work only in the feed, as task or delivery lines.
  const fed = [...(roomReader.items || [])].reverse().find((i) => i.run_id && ["delivery", "task"].includes(i.kind));
  const runId = rail.runId || delivered?.run_id || fed?.run_id;
  if (!runId) {
    const busy = dockWork();
    return `<p class="muted">${busy ? "The draft shows here once its first version is written." : "Nothing delivered in this thread yet."}</p>`;
  }
  if (rail.runId !== runId) {
    rail.runId = runId;
    rail.answer = null;
  }
  return railDoc();
}

// ---- Files: the space's folders, and what this thread's work read ----

async function dockFiles() {
  const topic = typeof RoomState === "object" ? RoomState.topic : null;
  if (!topic?.id) return `<p class="muted">Open a thread to see its folders.</p>`;
  const fresh = Dock.filesFor === topic.id && Date.now() - Dock.filesAt < DOCK_FLOW_EVERY_MS;
  if (!fresh) {
    Dock.files = await api(`/topics/${topic.id}/workspaces`).catch((error) => {
      if (!(error instanceof SignedOut)) console.warn("The thread's folders could not be read:", error);
      return null;
    });
    Dock.filesFor = topic.id;
    Dock.filesAt = Date.now();
  }
  const seen = Dock.files;
  if (!seen) return `<p class="muted">The folders could not be read just now.</p>`;
  // A folder opened, or a file in it (canvas 9, the read-only part; 7b).
  if (Dock.browse && Dock.browse.topic === topic.id) return dockBrowse(Dock.browse);
  if (!seen.space) {
    return `<p class="muted">This thread is in no business space. A space's folders show here once it is in one.</p>`;
  }
  const folders = seen.folders.length
    ? `<ul class="dock-day">${seen.folders
        .map(
          (f) =>
            `<li class="dock-event"><span class="dock-when">${esc(f.access)}</span><button type="button" class="dock-open" data-action="dock-folder:${esc(f.id)}"><strong>${esc(f.name)}</strong> <span class="muted">~/dev/${esc(f.path)}</span></button></li>`
        )
        .join("")}</ul>`
    : `<p class="muted">${esc(seen.space.name)} has no folders yet.</p>`;
  const read = seen.read.length
    ? `<ul class="dock-day">${seen.read
        .map((r) => `<li class="dock-event"><span class="dock-when">${esc(when(r.at))}</span><span>${esc(r.folder)}/${esc(r.path)}</span></li>`)
        .join("")}</ul>`
    : `<p class="muted">Nothing read from them in this thread yet.</p>`;
  return `<p class="composer-eyebrow">${esc(seen.space.name)} · folders</p>${folders}
    <p class="composer-eyebrow">Read in this thread</p>${read}
    ${btn("Manage folders", "settings-open:workspaces", "room-tool")}`;
}

async function dockBrowse(browse) {
  if (browse.file) {
    const file = browse.file;
    const more = file.more
      ? btn("Read more", `dock-file:${file.path}:${file.offset + file.text.length}`, "room-tool")
      : "";
    return `<div class="dock-crumbs">${btn("← Files", "dock-folder:" + browse.id, "room-tool")}<span class="muted">${esc(browse.name)} / ${esc(file.path)}</span></div>
      <pre class="dock-file">${esc(file.text)}</pre>
      <div class="button-row tight">${btn("Mention in chat", `dock-mention:${file.path}`, "room-tool")}${more}</div>
      <p class="muted">Read-only here. Changes go through the thread.</p>`;
  }
  const rows = (browse.files || [])
    .map(
      (f) =>
        `<li class="dock-event"><span class="dock-when">${esc(String(f.modified_at || "").slice(0, 10))}</span><button type="button" class="dock-open" data-action="dock-file:${esc(f.path)}:0">${esc(f.path)}</button></li>`
    )
    .join("");
  return `<div class="dock-crumbs">${btn("← Folders", "dock-folders", "room-tool")}<strong>${esc(browse.name)}</strong></div>
    ${rows ? `<ul class="dock-day">${rows}</ul>` : `<p class="muted">No readable files in this folder.</p>`}
    ${browse.more ? `<p class="muted">… and ${esc(browse.more)} more; ask the lead to search them.</p>` : ""}`;
}

// ---- Calendar ----

async function dockCalendar() {
  if (typeof calDay !== "function") return `<p class="muted">The calendar is not available here.</p>`;
  if (!Dock.day || Date.now() - Dock.dayAt > DOCK_DAY_EVERY_MS) {
    Dock.day = await calDay();
    Dock.dayAt = Date.now();
  }
  const day = Dock.day;
  if (!day || !day.connected) {
    return `<a class="dock-connect" href="#calendars"><strong>Connect your calendar</strong><span>Read only, so the work fits around your day.</span></a>`;
  }
  const events = [...(day.events || [])].sort((a, b) => Number(b.all_day) - Number(a.all_day) || String(a.starts_at).localeCompare(String(b.starts_at)));
  const rows = events
    .map(
      (e) =>
        `<li class="dock-event${e.busy ? "" : " is-free"}"><span class="dock-when">${esc(e.all_day ? "All day" : calShort(e.starts_at))}</span><span>${esc(e.title || "Busy")}</span></li>`
    )
    .join("");
  const status = typeof calStatusLine === "function" ? calStatusLine(day.status) : "";
  return `<p class="composer-eyebrow">Today${status ? ` · ${esc(status)}` : ""}</p>
    ${rows ? `<ul class="dock-day">${rows}</ul>` : `<p class="muted">Nothing in your calendar today.</p>`}
    <a class="dock-more" href="#calendar">Open the calendar →</a>`;
}

Office.actions = Object.assign(Office.actions || {}, {
  "dock-folders": () => {
    Dock.browse = null;
    roomDrawRail();
  },
  "dock-folder": async (id) => {
    const folder = (Dock.files?.folders || []).find((f) => f.id === id);
    const topic = RoomState.topic?.id;
    if (!folder || !topic) return;
    const listed = await attempt(() => api(`/workspaces/${id}/files`), null);
    if (!listed) return;
    Dock.browse = { topic, id, name: folder.name, files: listed.files || [], more: listed.more || 0, file: null };
    roomDrawRail();
  },
  "dock-file": async (arg) => {
    const browse = Dock.browse;
    if (!browse) return;
    const cut = String(arg).lastIndexOf(":");
    const path = String(arg).slice(0, cut);
    const offset = Number(String(arg).slice(cut + 1)) || 0;
    const read = await attempt(() => api(`/workspaces/${browse.id}/read?path=${encodeURIComponent(path)}&offset=${offset}`), null);
    if (!read) return;
    browse.file = read;
    roomDrawRail();
  },
  // The file's address into the thread's box, where the lead can read it.
  "dock-mention": (path) => {
    const box = document.getElementById("room-text");
    if (!box || !Dock.browse) {
      console.error("Element not found: #room-text");
      return;
    }
    const address = `local://ws/${Dock.browse.name}/${path}`;
    box.value = `${box.value}${box.value && !box.value.endsWith(" ") ? " " : ""}${address} `;
    box.dispatchEvent(new Event("input", { bubbles: true }));
    box.focus();
    document.querySelector("#topic-rail")?.classList.remove("open");
  },
  "dock-tab": (tab) => {
    if (!DOCK_TABS.some(([id]) => id === tab)) {
      console.error(`Unknown dock tab: ${tab}`);
      return;
    }
    remember("dockTab", tab);
    RoomState.rail = tab === "draft" ? { mode: "draft", runId: RoomState.rail?.runId } : { mode: tab };
    roomDrawRail();
  },
});

// The owner's "Do next" and "Skip" on a step of the running plan (13d).
async function dockStepControl(arg, kind, said) {
  const [runId, stepId] = String(arg).split(":");
  if (!runId || !stepId) return;
  if (kind === "skip" && !window.confirm("Skip this step? It is left out, and the answer says what was not covered.")) return;
  const sent = await attempt(() => api(`/runs/${runId}/controls`, { method: "POST", body: { kind, text: stepId } }), said);
  if (sent === undefined) return;
  Dock.flowAt = 0;
  if (typeof roomDrawRail === "function") roomDrawRail();
}

Office.actions = Object.assign(Office.actions || {}, {
  "dock-step-next": (arg) => dockStepControl(arg, "prioritize", "It goes next, once a place is free."),
  "dock-step-skip": (arg) => dockStepControl(arg, "skip", "Skipping it."),
});

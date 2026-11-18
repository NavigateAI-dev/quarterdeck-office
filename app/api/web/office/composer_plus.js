"use strict";

// The thread's input bar, learned from DSH's:
// a "+" menu (Add: files, a folder, a library document, the thread's goal, a
// plan first; Commands: model, access, export), the access and the model by
// the box, the goal pinned above it, and a status line under it. Each setting
// belongs to the thread and is kept by the office (`/topics/{id}/settings`).
// "Full access" is deliberately not offered: merging always comes to you.

const ComposerPlus = {
  topic: "", // whose settings are held below
  settings: null,
  usage: null,
  usageAt: 0,
  plan: false, // the next message asks for a plan first
  connections: null,
};
const COMPOSER_USAGE_MS = 15000;

// Outline icons in the text's colour, as DSH draws them (paths from Lucide, ISC licence).
const COMPOSER_ICON_PATHS = {
  file: '<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  library: '<path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/>',
  goal: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
  plan: '<path d="M3 5h.01M3 12h.01M3 19h.01M8 5h13M8 12h13M8 19h13"/>',
  model: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>',
  read: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  work: '<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
  export: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  turns: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  reply: '<polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>',
  thread: '<path d="M14 9a2 2 0 0 1-2 2H6l-4 4V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2z"/><path d="M18 9h2a2 2 0 0 1 2 2v11l-4-4h-6a2 2 0 0 1-2-2v-1"/>',
  decision: '<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/>',
  calls: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  edit: '<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>',
  send: '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
  down: '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
  stop: '<rect width="12" height="12" x="6" y="6" rx="2" fill="currentColor" stroke="none"/>',
  tool: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  good: '<path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/>',
  bad: '<path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/>',
};

function composerIcon(name) {
  const paths = COMPOSER_ICON_PATHS[name];
  return paths ? `<svg class="composer-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths}</svg>` : "";
}
// An icon in place of words: the name stays for tooltips, screen readers and find-in-page.
function iconFace(name, label) {
  return `${composerIcon(name)}<span class="visually-hidden">${esc(label)}</span>`;
}

function iconBtn(name, label, action, cls = "") {
  return `<button type="button" class="icon-tool ${esc(cls)}" data-action="${esc(action)}" aria-label="${esc(label)}" title="${esc(label)}">${iconFace(name, label)}</button>`;
}

const COMPOSER_ACCESS = {
  read: { mark: "read", name: "Read only", about: "Work started here reads your folders and changes nothing." },
  work: { mark: "work", name: "Work on a branch", about: "Coding work may change files on its own office/ branch; merging still comes to you." },
};

function composerPlusTopic() {
  return (typeof RoomState !== "undefined" && RoomState.topic?.id) || "";
}

function composerMenuItems() {
  const s = ComposerPlus.settings || {};
  return [
    { group: "Add" },
    { key: "file", mark: "file", name: "File", about: "Attach files from this computer", action: "attach-files:room-form" },
    { key: "folder", mark: "folder", name: "Folder", about: "Attach a whole folder", action: "attach-folder:room-form" },
    { key: "library", mark: "library", name: "From your library", about: "A document the office already has", action: "attach-existing:room-form" },
    { key: "goal", mark: "goal", name: "Goal", about: s.goal ? "Change what this thread is for" : "Set what this thread is for, and what its work may spend", action: "composer-goal" },
    { key: "plan", mark: "plan", name: ComposerPlus.plan ? "Plan first · on" : "Plan", about: "Ask for a plan before anything is done", action: "composer-plan" },
    { group: "Commands" },
    { key: "model", mark: "model", name: "Model", about: "The model this thread's replies use", action: "composer-pop:model" },
    { key: "access", mark: "read", name: "Access", about: "Whether work started here may change files", action: "composer-pop:access" },
    { key: "export", mark: "export", name: "Export", about: "Download this thread as a zip", action: "composer-export" },
  ];
}

function composerMenuOpen() {
  const menu = $("#composer-menu");
  const plus = document.querySelector(".composer-plus");
  if (!menu) {
    console.error("Element not found: #composer-menu");
    return;
  }
  composerPopClose();
  menu.innerHTML = composerMenuItems()
    .map((item) =>
      item.group
        ? `<p class="composer-menu-group" role="presentation">${esc(item.group)}</p>`
        : `<button type="button" role="menuitem" class="composer-menu-item" data-action="${esc(item.action)}" data-menu-close="1"><span class="composer-menu-mark">${composerIcon(item.mark)}</span><span class="composer-menu-name">${esc(item.name)}</span><span class="composer-menu-about">${esc(item.about)}</span></button>`
    )
    .join("");
  menu.hidden = false;
  plus?.setAttribute("aria-expanded", "true");
  menu.querySelector(".composer-menu-item")?.focus();
}

function composerMenuClose(refocus = false) {
  const menu = $("#composer-menu");
  if (menu) menu.hidden = true;
  document.querySelector(".composer-plus")?.setAttribute("aria-expanded", "false");
  if (refocus) $("#room-text")?.focus();
}

function composerPopClose() {
  const pop = $("#composer-pop");
  if (pop) pop.hidden = true;
}

async function composerSettingsLoad(force = false) {
  const topic = composerPlusTopic();
  if (!topic) return null;
  if (!force && ComposerPlus.topic === topic && ComposerPlus.settings) return ComposerPlus.settings;
  const settings = await api(`/topics/${topic}/settings`).catch((error) => {
    console.error("Could not read this thread's settings", error);
    return null;
  });
  if (ComposerPlus.topic !== topic) {
    ComposerPlus.plan = false;
    ComposerPlus.usage = null;
    ComposerPlus.usageAt = 0;
  }
  ComposerPlus.topic = topic;
  ComposerPlus.settings = settings;
  return settings;
}

async function composerSettingsSave(change, said) {
  const topic = composerPlusTopic();
  if (!topic) return;
  const saved = await attempt(() => api(`/topics/${topic}/settings`, { method: "PUT", body: change }), said);
  if (saved === undefined) return;
  ComposerPlus.settings = saved;
  composerPopClose();
  closeModal();
  composerPlusDraw();
}

function composerChip(id, icon, label, action, title) {
  return `<button type="button" id="${id}" class="composer-chip" data-action="${esc(action)}" aria-haspopup="dialog" title="${esc(title)}">${composerIcon(icon)}<span class="composer-chip-label">${label}</span><span class="composer-chip-caret" aria-hidden="true">▾</span></button>`;
}

function composerUsageLine(usage) {
  if (!usage) return "";
  const tokens = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K` : String(n));
  const parts = [
    `<span title="Your messages, and the steps of the work started here">${composerIcon("turns")} ${esc(usage.turns)} turn${usage.turns === 1 ? "" : "s"} · ${esc(usage.steps)} step${usage.steps === 1 ? "" : "s"}</span>`,
    `<span title="Replies and work together">${composerIcon("model")} ${esc(tokens(usage.tokens || 0))} tok · ${esc(money(usage.cost_usd || 0))}</span>`,
  ];
  const context = usage.context || {};
  if (context.percent !== null && context.percent !== undefined) {
    parts.push(`<span title="The last reply's share of its model's context window"><span class="composer-ring" style="--p:${Math.min(100, Number(context.percent))}" aria-hidden="true"></span> ${esc(context.percent)}% context</span>`);
  }
  return parts.join("");
}

async function composerUsageLoad(force = false) {
  const topic = composerPlusTopic();
  if (!topic) return null;
  if (!force && ComposerPlus.usage && Date.now() - ComposerPlus.usageAt < COMPOSER_USAGE_MS) return ComposerPlus.usage;
  const usage = await api(`/topics/${topic}/usage`).catch((error) => {
    console.error("Could not read this thread's numbers", error);
    return null;
  });
  ComposerPlus.usage = usage;
  ComposerPlus.usageAt = Date.now();
  return usage;
}

// Everything around the box that belongs to the thread, drawn with it.
async function composerPlusDraw() {
  const settings = await composerSettingsLoad();
  const access = $("#composer-access-slot");
  const model = $("#composer-model-slot");
  const goal = $("#composer-goal");
  const status = $("#composer-status");
  if (!access || !model || !goal || !status) return;
  if (!settings) {
    access.innerHTML = model.innerHTML = "";
    goal.hidden = status.hidden = true;
    return;
  }
  const chosen = COMPOSER_ACCESS[settings.access] || COMPOSER_ACCESS.read;
  access.innerHTML = composerChip("composer-access", chosen.mark, esc(chosen.name), "composer-pop:access", chosen.about);
  const fallback = settings.default_model ? `${esc(settings.default_model)} <span class="composer-chip-tag">default</span>` : "Office default";
  const modelLabel = settings.model ? esc(settings.model.model) : fallback;
  model.innerHTML = composerChip("composer-model", "model", modelLabel, "composer-pop:model", settings.model ? `${settings.model.name} · ${settings.model.provider}` : "The model the office chose for the lead's role");
  goal.hidden = !settings.goal && !ComposerPlus.plan;
  goal.innerHTML = [
    settings.goal ? `<span class="composer-goal-text">${composerIcon("goal")} <strong>Goal</strong> ${esc(settings.goal)} <span class="muted">· work up to ${esc(money(settings.cap_usd))}</span></span>${btn("Edit", "composer-goal", "composer-link")}` : "",
    ComposerPlus.plan ? `<span class="composer-plan-pill" role="status">${composerIcon("plan")} Plan first: the next message asks for a plan${btn("×", "composer-plan", "composer-link", 'aria-label="Do not ask for a plan first"')}</span>` : "",
  ].join("");
  const usage = await composerUsageLoad();
  status.innerHTML = composerUsageLine(usage);
  status.hidden = !usage;
}

function composerPlusSent() {
  if (ComposerPlus.plan) ComposerPlus.plan = false;
  ComposerPlus.usageAt = 0;
  composerPlusDraw();
}

async function composerPopModel() {
  const pop = $("#composer-pop");
  if (!pop) return;
  if (!Array.isArray(ComposerPlus.connections)) {
    const rows = await api("/connections").catch((error) => {
      console.error("Could not read the model connections", error);
      return [];
    });
    ComposerPlus.connections = Array.isArray(rows) ? rows : [];
  }
  const ready = ComposerPlus.connections.filter((c) => ["MODEL_AVAILABLE", "VERIFIED"].includes(c.status));
  const current = ComposerPlus.settings?.model?.id || "";
  const option = (id, name, about) =>
    `<button type="button" role="menuitemradio" aria-checked="${current === id}" class="composer-pop-item${current === id ? " on" : ""}" data-action="composer-model:${esc(id)}"><span class="composer-pop-name">${esc(name)}</span><span class="composer-pop-about">${esc(about)}</span>${current === id ? '<span class="composer-check" aria-hidden="true">✓</span>' : ""}</button>`;
  pop.innerHTML = `<p class="composer-menu-group">Model for this thread</p>
    ${option("", ComposerPlus.settings?.default_model ? `Office default · ${ComposerPlus.settings.default_model}` : "Office default", "What Settings → Models chose for the lead's role")}
    ${ready.map((c) => option(c.id, c.model_id, `${c.display_name} · ${c.provider}`)).join("")}
    ${ready.length ? "" : `<p class="composer-pop-note">No checked connections yet. Add one under Connections.</p>`}`;
  composerPopShow(pop);
}

function composerPopAccess() {
  const pop = $("#composer-pop");
  if (!pop) return;
  const settings = ComposerPlus.settings || {};
  const current = settings.access || "read";
  const option = (key) => {
    const access = COMPOSER_ACCESS[key];
    const off = key === "work" && !settings.work_available;
    return `<button type="button" role="menuitemradio" aria-checked="${current === key}" class="composer-pop-item${current === key ? " on" : ""}" data-action="composer-access:${key}" ${off ? 'aria-disabled="true"' : ""}><span class="composer-pop-name">${composerIcon(access.mark)} ${esc(access.name)}</span><span class="composer-pop-about">${esc(off ? "Off: give the office a writable folder first (Settings → Spaces)." : access.about)}</span>${current === key ? '<span class="composer-check" aria-hidden="true">✓</span>' : ""}</button>`;
  };
  pop.innerHTML = `<p class="composer-menu-group">Access for work started here</p>${option("read")}${option("work")}<p class="composer-pop-note">There is no full access: merging into your branch always comes to you.</p>`;
  composerPopShow(pop);
}

function composerPopShow(pop) {
  composerMenuClose();
  pop.hidden = false;
  pop.querySelector(".composer-pop-item")?.focus();
}

function composerGoalDialog() {
  const s = ComposerPlus.settings || {};
  formModal(
    "This thread's goal",
    "composer-goal-form",
    `<label for="composer-goal-text">What this thread is for</label>
     <textarea id="composer-goal-text" name="goal" maxlength="1000" rows="3" placeholder="e.g. Decide the launch price for the new plans">${esc(s.goal || "")}</textarea>
     <label for="composer-goal-cap">What work started here may spend before it asks (USD)</label>
     <input id="composer-goal-cap" name="cap" type="number" min="0.05" max="50" step="0.05" value="${esc(s.goal_cap_usd ?? "")}" placeholder="${esc(money(0.5))} by default">
     <p class="muted">The lead is told the goal with every reply. Nothing starts until you press Start.</p>`,
    "Save goal"
  );
  $("#composer-goal-text")?.focus();
}

function composerPlusWire() {
  const textarea = $("#room-text");
  if (!textarea || textarea.dataset.plusWired) return;
  textarea.dataset.plusWired = "1";
  // "/" in an empty box opens the menu, as DSH's commands do.
  textarea.addEventListener("keydown", (event) => {
    if (event.key === "/" && !textarea.value && !event.isComposing) {
      event.preventDefault();
      composerMenuOpen();
    }
  });
}

// Arrow keys move in the menu and the pickers; Esc closes them; a click outside too.
document.addEventListener("keydown", (event) => {
  const open = [$("#composer-menu"), $("#composer-pop")].find((el) => el && !el.hidden);
  if (!open) return;
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    composerMenuClose(true);
    composerPopClose();
    return;
  }
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const items = [...open.querySelectorAll("button")];
  const at = items.indexOf(document.activeElement);
  const next = event.key === "ArrowDown" ? (at + 1) % items.length : (at - 1 + items.length) % items.length;
  event.preventDefault();
  items[next]?.focus();
}, true);

document.addEventListener("click", (event) => {
  const inside = event.target?.closest?.("#composer-menu, #composer-pop, .composer-plus, .composer-chip");
  if (inside) return;
  composerMenuClose();
  composerPopClose();
});

Office.forms = Object.assign(Office.forms || {}, {
  "composer-goal-form": async (form) => {
    const data = new FormData(form);
    const goal = String(data.get("goal") || "").trim();
    const capText = String(data.get("cap") || "").trim();
    const cap = capText ? Number(capText) : null;
    if (cap !== null && !(cap >= 0.05 && cap <= 50)) return toast("Give a limit between $0.05 and $50, or leave it empty.", true);
    await composerSettingsSave({ goal: goal || null, goal_cap_usd: cap }, goal ? "Goal set." : "Goal cleared.");
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "composer-menu": () => {
    const menu = $("#composer-menu");
    if (menu && !menu.hidden) composerMenuClose(true);
    else composerMenuOpen();
  },
  "composer-pop": (which) => {
    composerMenuClose();
    if (which === "model") return composerPopModel();
    return composerPopAccess();
  },
  "composer-model": (id) => composerSettingsSave({ model_connection_id: id || null }, id ? "This thread uses that model now." : "Back to the office's model."),
  "composer-access": (key) => {
    if (key === "work" && !ComposerPlus.settings?.work_available) return toast("Work access is off: give the office a writable folder first.", true);
    return composerSettingsSave({ access: key }, key === "work" ? "Work started here may change files on its own branch." : "Work started here only reads.");
  },
  "composer-goal": () => {
    composerMenuClose();
    composerGoalDialog();
  },
  "composer-plan": () => {
    ComposerPlus.plan = !ComposerPlus.plan;
    composerMenuClose(true);
    composerPlusDraw();
  },
  "composer-export": () => {
    composerMenuClose();
    const topic = composerPlusTopic();
    if (!topic) return;
    const link = document.createElement("a");
    link.href = `/topics/${topic}/export.zip`;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
    toast("Downloading this thread.");
  },
});

// A menu entry that only wraps another action closes the menu first.
document.addEventListener("click", (event) => {
  if (event.target?.closest?.("[data-menu-close]")) composerMenuClose();
}, true);

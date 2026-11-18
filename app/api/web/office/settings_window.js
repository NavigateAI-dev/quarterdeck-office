// Settings as one window over whatever the owner is doing.
// Eighteen screens become nine sections, grouped by the decision being made. Each part is the screen it
// was, drawn inside the window; while the window is open, rerender() redraws
// the part here instead of the page behind (main.js).
// Opens from the sidebar's Settings, with ⌘, (Ctrl+,), or settings-open:<section>.

const SETTINGS_SECTIONS = [
  { group: "Office", id: "general", label: "General", parts: [["Way of working", "settings", "office"]] },
  { group: "Office", id: "team", label: "Team", parts: [["Colleagues", "agents"]] },
  {
    group: "Office",
    id: "models",
    label: "Models & spending",
    parts: [
      ["Who uses what", "settings", "models"],
      ["Limits", "settings", "budget"],
      ["Usage", "settings", "usage"],
      ["Performance", "settings", "performance"],
    ],
  },
  { group: "Office", id: "rules", label: "Permissions & rules", parts: [["Rules", "policies"]] },
  {
    group: "Reach",
    id: "connections",
    label: "Connections",
    parts: [
      ["Accounts", "accounts"],
      ["Providers & channels", "connections"],
    ],
  },
  {
    group: "Reach",
    id: "plugins",
    label: "Plugins & tools",
    parts: [
      ["Tool servers", "tools"],
      ["Skills", "skills"],
    ],
  },
  { group: "Reach", id: "archive", label: "AI history", parts: [["AI history", "archive"]] },
  { group: "Reach", id: "routines", label: "Routines", parts: [["Routines", "routines"]] },
  // A space's folders, read live.
  { group: "Reach", id: "workspaces", label: "Workspaces", parts: [["Folders", "workspaces"]] },
  { group: "Reach", id: "calendar", label: "Calendar", parts: [["Calendars", "calendars"]] },
  { group: "System", id: "access", label: "Access & devices", parts: [["Remote access", "access"]] },
  {
    group: "System",
    id: "health",
    label: "Health",
    parts: [
      ["Reach", "settings", "reach"],
      ["Handbook", "handbook"],
    ],
  },
];

// The section is read from this browser the first time Settings opens
// (main.js, which keeps it, loads after this file).
const SettingsWindow = { section: "", part: 0, drawing: null };

function settingsSection(id) {
  return SETTINGS_SECTIONS.find((s) => s.id === id) || SETTINGS_SECTIONS[0];
}

function settingsWindowOpen() {
  return Boolean(document.getElementById("settings-window")?.open);
}

// The dialog is made once, the first time Settings opens.
function settingsWindowDialog() {
  let dialog = document.getElementById("settings-window");
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = "settings-window";
  dialog.className = "settings-window";
  dialog.setAttribute("aria-labelledby", "settings-window-title");
  dialog.innerHTML = `<div class="sw-frame">
      <nav class="sw-nav" id="settings-nav" aria-label="Settings sections"></nav>
      <section class="sw-main">
        <div class="sw-head">
          <h2 id="settings-window-title">Settings</h2>
          ${btn("✕", "settings-close", "sw-close", 'aria-label="Close settings"')}
        </div>
        <div class="sw-parts subnav" id="settings-parts" role="tablist" aria-label="Parts of this section"></div>
        <div class="sw-body" id="settings-body"></div>
      </section>
    </div>`;
  // A click on the backdrop, outside the frame, closes it as Esc does.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  // What changed in Settings may show on the page behind: redraw it once closed.
  dialog.addEventListener("close", () => rerender());
  document.body.appendChild(dialog);
  return dialog;
}

function settingsNav() {
  let group = "";
  return SETTINGS_SECTIONS.map((section) => {
    const label = section.group !== group ? `<p class="side-label">${esc(section.group)}</p>` : "";
    group = section.group;
    const on = section.id === SettingsWindow.section;
    return `${label}<button type="button" class="side-row sw-section${on ? " active" : ""}" data-action="settings-section:${section.id}" ${on ? 'aria-current="true" autofocus' : ""}><span class="side-text">${esc(section.label)}</span></button>`;
  }).join("");
}

async function settingsWindowDraw() {
  const nav = document.getElementById("settings-nav");
  const parts = document.getElementById("settings-parts");
  const title = document.getElementById("settings-window-title");
  const body = document.getElementById("settings-body");
  if (!nav || !parts || !title || !body) {
    console.error("Settings window parts not found: #settings-nav / #settings-parts / #settings-window-title / #settings-body");
    return;
  }
  const section = settingsSection(SettingsWindow.section);
  const index = Math.min(SettingsWindow.part, section.parts.length - 1);
  const [, screenId, tab] = section.parts[index];
  const hadFocus = nav.contains(document.activeElement) || !document.getElementById("settings-window")?.contains(document.activeElement);
  nav.innerHTML = settingsNav();
  // Redrawing the sections must not drop the focus out of the window.
  if (hadFocus) nav.querySelector(".sw-section.active")?.focus();
  title.textContent = section.label;
  parts.hidden = section.parts.length < 2;
  parts.innerHTML = section.parts
    .map(([label], i) => btn(esc(label), `settings-part:${i}`, i === index ? "active" : "", `role="tab" aria-selected="${i === index}"`))
    .join("");
  const screen = Office.screens[screenId];
  if (!screen) {
    console.error(`Settings: no screen named ${screenId}`);
    body.innerHTML = emptyCard("This part could not load.", `No screen named ${screenId}.`);
    return;
  }
  // A slow draw must not overwrite a newer one.
  const token = Symbol("settings");
  SettingsWindow.drawing = token;
  const scroll = body.scrollTop;
  try {
    if (tab && typeof Settings === "object") Settings.tab = tab;
    const holder = document.createElement("div");
    await screen.render(holder, "");
    if (SettingsWindow.drawing !== token) return;
    body.dataset.screen = screenId;
    body.replaceChildren(...holder.childNodes);
    body.scrollTop = scroll;
  } catch (error) {
    if (error instanceof SignedOut) return;
    console.error(error);
    body.innerHTML = emptyCard("This part could not load.", explain(error), btn("Try again", "settings-part:" + index));
  }
}

async function settingsWindowShow(sectionId) {
  if (!SettingsWindow.section) SettingsWindow.section = settingsSection(remembered("settingsSection", "general")).id;
  if (sectionId) {
    SettingsWindow.section = settingsSection(sectionId).id;
    SettingsWindow.part = 0;
  }
  const dialog = settingsWindowDialog();
  // The sections first, so the one chosen takes the focus as it opens.
  const nav = document.getElementById("settings-nav");
  if (nav) nav.innerHTML = settingsNav();
  if (!dialog.open) dialog.showModal();
  remember("settingsSection", SettingsWindow.section);
  await settingsWindowDraw();
}

Office.actions = Object.assign(Office.actions || {}, {
  "settings-open": (sectionId) => settingsWindowShow(sectionId),
  "settings-close": () => document.getElementById("settings-window")?.close(),
  "settings-section": (sectionId) => {
    SettingsWindow.section = settingsSection(sectionId).id;
    SettingsWindow.part = 0;
    remember("settingsSection", SettingsWindow.section);
    return settingsWindowDraw();
  },
  "settings-part": (index) => {
    SettingsWindow.part = Math.max(0, Number(index) || 0);
    return settingsWindowDraw();
  },
});

// ⌘, (Ctrl+, elsewhere) opens or closes Settings from anywhere.
document.addEventListener("keydown", (event) => {
  if (event.key !== "," || !(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return;
  event.preventDefault();
  if (settingsWindowOpen()) document.getElementById("settings-window")?.close();
  else settingsWindowShow();
});

// Following a link out of a setting (an agent's page, a project) closes the window.
window.addEventListener("hashchange", () => {
  if (settingsWindowOpen()) document.getElementById("settings-window")?.close();
});

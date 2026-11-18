"use strict";

// Help on ⌘/ (Ctrl+/), or "?" when nothing is being typed (Settings design, §10):
// the keys the office answers to, where things are, and the way to the
// Handbook. Every key listed here is one the page really handles.

const HELP_KEYS = [
  ["Anywhere", [
    ["i", "Keep an idea"],
    ["⌘ ,", "Open or close Settings"],
    ["⌘ /", "This help"],
  ]],
  ["In a thread's box", [
    ["Enter", "Send (while work runs: queue or steer, as the switch says)"],
    ["Shift Enter", "A new line"],
    ["⌘ Enter", "The other of queue or steer, once"],
    ["/", "In an empty box: the + menu (add a file, a goal, a plan; model, access, export)"],
    ["@", "Mention a colleague"],
    ["Esc Esc", "Stop the work running in the thread"],
  ]],
  ["When a decision takes the box", [
    ["Enter", "Accept what it offers"],
    ["Esc", "Set it aside, or refuse"],
  ]],
  ["Elsewhere", [
    ["⌘ S", "Save, in the editor"],
    ["m", "Move a card, on the board"],
    ["↑ ↓", "Move in a menu or a list of names"],
  ]],
];

const HELP_PLACES = [
  ["Desk", "What needs you, what is moving, and your wall: ideas, calendar, delivered work."],
  ["Threads", "Talk with the lead and the team; work started there comes back there."],
  ["Library", "Folders and documents the office may read, and what it delivered."],
  ["Settings", "Who uses which model, limits, rules, connections and health (⌘ ,)."],
];

function helpBody() {
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");
  const key = (label) => esc(isMac ? label : label.replace(/⌘/g, "Ctrl"));
  const groups = HELP_KEYS.map(
    ([group, rows]) =>
      `<section class="help-group"><h3>${esc(group)}</h3><dl>${rows
        .map(([k, said]) => `<div><dt>${k.split(" ").map((part) => `<kbd>${key(part)}</kbd>`).join(" ")}</dt><dd>${esc(said)}</dd></div>`)
        .join("")}</dl></section>`
  ).join("");
  const places = HELP_PLACES.map(([name, said]) => `<li><strong>${esc(name)}</strong> ${esc(said)}</li>`).join("");
  return `<div class="help-panel">
    <div class="help-keys">${groups}</div>
    <section class="help-group"><h3>Where things are</h3><ul class="help-places">${places}</ul></section>
    <p class="muted">Nothing here spends: the office asks before anything costs money or reaches outside.</p>
  </div>`;
}

function helpOpen() {
  modal(
    "Help",
    helpBody(),
    `<div class="button-row">${btn("Open the Handbook", "help-handbook")}${btn("Settings", "help-settings")}${btn("Close", "close", "primary")}</div>`
  );
}

Office.actions = Object.assign(Office.actions || {}, {
  "help-open": () => helpOpen(),
  "help-handbook": () => {
    closeModal();
    go("handbook");
  },
  "help-settings": () => {
    closeModal();
    if (typeof settingsWindowShow === "function") settingsWindowShow();
  },
});

document.addEventListener("keydown", (event) => {
  const typing = event.target?.closest?.("input, textarea, select, [contenteditable=''], [contenteditable='true']");
  const commandSlash = event.key === "/" && (event.metaKey || event.ctrlKey) && !event.altKey;
  const question = event.key === "?" && !typing && !event.metaKey && !event.ctrlKey && !event.altKey;
  if (!(commandSlash || question) || event.isComposing || !Office.csrf) return;
  event.preventDefault();
  if ($("#modal")?.open && $("#modal-content")?.querySelector(".help-panel")) closeModal();
  else helpOpen();
});

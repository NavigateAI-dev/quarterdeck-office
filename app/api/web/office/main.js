"use strict";

// Navigation, the session, and one delegated handler for every action.

// Four places to work in: talk, what was handed off, and the business
// context. Everything that configures the office sits under Office settings.
const NAV = [
  ["home", "⌂", "The floor"],
  ["calendar", "▦", "Calendar"],
  ["discussions", "#", "Rooms"],
  ["tasks", "▤", "Library"],
  ["projects", "◫", "Business spaces", "Spaces"],
];
// Screens that live under one of the four places, or under Office settings.
const NAV_PARENT = {
  board: "tasks",
  task: "tasks",
  rooms: "discussions",
  room: "discussions",
  topic: "discussions",
  project: "projects",
  agents: "office",
  agent: "office",
  knowledge: "office",
  tools: "office",
  skills: "office",
  policies: "office",
  connections: "office",
  accounts: "office",
  calendars: "office",
  settings: "office",
  access: "office",
  handbook: "office",
  setup: "office",
};
const NAV_TOPICS = 6;
// The places at the top of the office: nothing above them to go back to.
const TOP_PLACES = new Set(["home", "calendar", "discussions", "tasks", "projects", "office"]);
// The pages visited in this window, oldest first, so Back returns to the page
// you came from; opened directly (a link, a home-screen icon) it goes up.
Office.visited = [];

function parentOf(view) {
  if (TOP_PLACES.has(view)) return null;
  if (view === "agent") return "agents";
  return NAV_PARENT[view] || Office.screens[view]?.nav || "home";
}

function trailOf(view) {
  const trail = [];
  for (let up = parentOf(view); up; up = parentOf(up)) trail.unshift(up);
  return trail;
}

function rememberVisit() {
  const here = location.hash || "#home";
  const seen = Office.visited;
  if (seen.length > 1 && seen[seen.length - 2] === here) seen.pop();
  else if (seen[seen.length - 1] !== here) seen.push(here);
  if (seen.length > 50) seen.splice(0, seen.length - 50);
}

function renderTrail(screen) {
  const crumb = $("#breadcrumb");
  if (!crumb) {
    console.error("Element not found: #breadcrumb");
    return;
  }
  const up = parentOf(Office.view);
  const cameFrom = Office.visited.length > 1;
  const backTitle = cameFrom ? "Back" : `Back to ${Office.screens[up]?.title || "The floor"}`;
  const back = up
    ? `<button type="button" id="back-button" class="back-button" data-action="go-back" aria-label="${esc(backTitle)}" title="${esc(backTitle)}"><span aria-hidden="true">←</span><span class="back-text">Back</span></button>`
    : "";
  const levels = trailOf(Office.view).map(
    (id) => `<a href="#${id}">${esc(Office.screens[id]?.title || id)}</a>`
  );
  const home = Office.view === "home" ? esc(Office.officeName) : `<a href="#home">${esc(Office.officeName)}</a>`;
  crumb.innerHTML = `${back}<span class="trail">${[home, ...levels, `<span aria-current="page">${esc(screen.title)}</span>`].join('<span class="trail-sep" aria-hidden="true">/</span>')}</span>`;
}

Office.actions = Object.assign(Office.actions || {}, {
  "go-back": () => {
    if (Office.visited.length > 1) {
      history.back();
      return;
    }
    location.hash = parentOf(Office.view) || "home";
  },
});
Office.officeName = "Quarterdeck";
Office.tasksOpen = remembered("tasksOpen", false);
Office.roomsOpen = remembered("roomsOpen", false);
Office.recentTasks = [];

// Recent tasks, grouped the way a person remembers them. The office had one
// list of everything; finding last Tuesday's task meant reading the lot.
const WHEN_GROUPS = [
  ["Today", 1],
  ["Yesterday", 2],
  ["Previous 7 days", 8],
  ["Previous 30 days", 31],
  ["Older", Infinity],
];
const NAV_TASKS = 15;
const NAV_ROOMS = 8;

function daysAgo(value) {
  if (!value) return Infinity;
  const started = new Date(value);
  if (Number.isNaN(started.getTime())) return Infinity;
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  return Math.floor((midnight - started) / 86400000) + 1;
}

function groupByWhen(tasks) {
  const groups = new Map();
  tasks.forEach((task) => {
    const age = daysAgo(task.started_at);
    const [label] = WHEN_GROUPS.find(([, within]) => age <= within) || ["Older"];
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(task);
  });
  return [...groups].filter(([, rows]) => rows.length);
}

function taskNav() {
  // Only where there is room for it: the sidebar is icons at 880px and a row
  // of icons on a phone, and a nested list belongs in neither.
  if (!Office.tasksOpen) return "";
  const tasks = (Office.recentTasks || []).slice(0, NAV_TASKS);
  if (!tasks.length) {
    return `<div class="nav-tasks"><p class="muted">No tasks yet.</p></div>`;
  }
  const open = Office.view === "task" ? Office.param : "";
  const groups = groupByWhen(tasks)
    .map(
      ([label, rows]) =>
        `<p class="nav-when">${esc(label)}</p>` +
        rows
          .map((task) => {
            // Several runs of one objective read identically in a narrow
            // column, so each says what became of it.
            const mark = NEEDS_OWNER.includes(task.status)
              ? '<i class="nav-dot owner" aria-hidden="true"></i>'
              : ACTIVE.includes(task.status)
                ? '<i class="nav-dot working" aria-hidden="true"></i>'
                : task.status === "FAILED"
                  ? '<i class="nav-dot failed" aria-hidden="true"></i>'
                  : "";
            const label = task.title || task.objective.split("\n")[0].trim().slice(0, 60);
            const said = (STATUS[task.status] || [task.status])[0];
            return `<a class="nav-task ${task.run_id === open ? "active" : ""}" href="#task/${esc(
              task.run_id
            )}" title="${esc(label)} — ${esc(said)}">${mark}<span>${esc(label)}</span></a>`;
          })
          .join("")
    )
    .join("");
  const more = (Office.recentTasks || []).length > NAV_TASKS;
  return `<div class="nav-tasks">${groups}${more ? `<a class="nav-task more" href="#tasks">All tasks →</a>` : ""}</div>`;
}

function roomNav() {
  // One line per active project, so a room is one click from anywhere; opened
  // and closed under its entry, as the recent tasks are.
  if (!Office.roomsOpen) return "";
  const rooms = (Office.rooms || []).slice(0, NAV_ROOMS);
  const add = `<button type="button" class="nav-task more nav-add" data-action="room-new">＋ New room</button>`;
  if (!rooms.length) return `<div class="nav-tasks nav-rooms"><p class="muted">No rooms yet.</p>${add}</div>`;
  const open = Office.view === "room" ? Office.param : "";
  const more = (Office.rooms || []).length > NAV_ROOMS;
  return `<div class="nav-tasks nav-rooms">${rooms
    .map((p) => `<a class="nav-task ${p.id === open ? "active" : ""}" href="#room/${esc(p.id)}" title="${esc(p.name)}"><span># ${esc(p.name)}</span></a>`)
    .join("")}${more ? `<a class="nav-task more" href="#rooms">All rooms →</a>` : ""}${add}</div>`;
}

async function refreshRoomNav() {
  try {
    const [topics, agents] = await Promise.all([api("/topics"), api("/agents").catch(() => [])]);
    if (Array.isArray(topics)) Office.topics = topics;
    if (Array.isArray(agents)) Office.colleagues = agents.filter((a) => a.status === "ACTIVE" && a.enabled_version);
    await refreshSidebar();
  } catch (error) {
    if (!(error instanceof SignedOut)) console.error(error);
  }
}

// The sidebar: Desk and the places, then each business
// space with its threads and their state, then Settings. One read for all of it.
const SIDE_PLACES = [
  ["home", "Desk", "D"],
  ["tasks", "Library", "L"],
  ["calendar", "Calendar", "C"],
  ["projects", "Spaces", "Sp"],
];

function renderNav() {
  const nav = $("#nav");
  if (!nav) {
    console.error("Element not found: nav");
    return;
  }
  const own = Office.screens[Office.view]?.nav || Office.view;
  const current = NAV_PARENT[own] || own;
  const needs = Number(Office.sidebar?.needs || 0);
  // `short` is what the narrow rail shows of each place.
  const row = (id, label, short, extra = "", attrs = "") =>
    `<a href="#${id}" class="side-row ${current === id ? "active" : ""}" title="${esc(label)}" ${current === id ? 'aria-current="page"' : ""} ${attrs}><span class="side-text" data-short="${esc(short)}">${esc(label)}</span>${extra}</a>`;
  const places = SIDE_PLACES.map(([id, label, short]) =>
    row(id, label, short, id === "home" && needs ? `<span class="side-count" aria-label="${needs} need you">${needs}</span>` : "")
  ).join("");
  nav.innerHTML =
    `<button type="button" class="side-new" data-action="thread-new"><span>New thread</span></button>` +
    `<div class="side-places">${places}</div>` +
    spacesNav() +
    `<div class="side-end">${row("office", "Settings", "⚙", "", 'data-action="settings-open"')}${row("handbook", "Help", "?", '<kbd class="side-key">⌘/</kbd>', 'data-action="help-open"')}</div>` +
    phoneTabs(current, needs);
  Motion.follow(nav, ".side-row.active", "nav");
}

// On a phone: four places in a bar at the bottom, in reach of
// the thumb; the sidebar itself is not shown there.
const PHONE_TABS = [
  ["home", "Desk"],
  ["threads", "Threads"],
  ["tasks", "Library"],
  ["more", "More"],
];

// The tab a place belongs under: a thread under Threads, a task under Library,
// everything else not in the bar under More.
function phoneTabOf(current) {
  if (current === "home" || current === "tasks") return current;
  if (current === "threads" || current === "discussions") return "threads";
  return "more";
}

function phoneTabs(current, needs) {
  const chosen = phoneTabOf(current);
  const tabs = PHONE_TABS.map(([id, label]) => {
    const on = id === chosen;
    const count = id === "home" && needs ? `<span class="phone-count" aria-label="${needs} need you">${needs}</span>` : "";
    return `<a href="#${id}" class="phone-tab${on ? " active" : ""}" ${on ? 'aria-current="page"' : ""}><span class="phone-mark" aria-hidden="true"></span>${esc(label)}${count}</a>`;
  }).join("");
  return `<div class="phone-tabs" role="navigation" aria-label="Places">${tabs}</div>`;
}

// Each business space and its threads: what needs you or is working first.
function spacesNav() {
  const spaces = Office.sidebar?.spaces;
  if (!Array.isArray(spaces)) return `<p class="muted side-empty">Reading your threads…</p>`;
  if (!spaces.length) return `<p class="muted side-empty">No threads yet. Start one on the Desk.</p>`;
  const open = ["topic", "task"].includes(Office.view) ? Office.param : "";
  return spaces
    .map(
      (space) =>
        `<section class="side-space" aria-label="${esc(space.name)}"><p class="side-label">${esc(space.name)}</p>${(space.threads || [])
          .map((thread) => threadRow(thread, open))
          .join("")}${space.more ? `<a class="side-more" href="#discussions">${space.more} more</a>` : ""}</section>`
    )
    .join("");
}

function threadRow(thread, open) {
  const state = String(thread.state || "idle");
  const mark = state === "working" ? '<i class="side-spin" aria-hidden="true"></i>' : `<i class="side-dot ${esc(state)}" aria-hidden="true"></i>`;
  const right = thread.badge
    ? `<span class="side-badge">${esc(thread.badge)}</span>`
    : `<span class="side-time">${esc(state === "working" ? "working" : sinceShort(thread.at))}</span>`;
  const active = thread.id === open ? " active" : "";
  return `<a class="side-thread${active}" href="${esc(thread.href)}" title="${esc(thread.title)}">${mark}<span class="side-title">${esc(thread.title)}</span>${right}</a>`;
}

// "now", "12m", "3h", "2d": how long since, as the sidebar says it.
function sinceShort(value) {
  if (!value) return "";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)}h`;
  return `${Math.round(minutes / (60 * 24))}d`;
}

// The element once the screen has drawn it, or null after `ms`.
async function appeared(id, ms) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    const found = document.getElementById(id);
    if (found) return found;
    await new Promise((done) => setTimeout(done, 50));
  }
  return null;
}

async function refreshSidebar() {
  try {
    Office.sidebar = await api("/office/sidebar");
    renderNav();
  } catch (error) {
    if (!(error instanceof SignedOut)) console.warn("The sidebar could not be read:", error);
  }
}

// The nav's own list, kept fresh without redrawing the page under the reader.
async function refreshTaskNav() {
  if (!Office.tasksOpen) return;
  try {
    const overview = await api("/office/overview");
    Office.recentTasks = overview.tasks || [];
    renderNav();
  } catch (error) {
    if (!(error instanceof SignedOut)) Office.recentTasks = Office.recentTasks || [];
  }
}

Office.actions = Object.assign(Office.actions || {}, {
  "tasks-toggle": async (_arg, target) => {
    Office.tasksOpen = !Office.tasksOpen;
    remember("tasksOpen", Office.tasksOpen);
    renderNav();
    if (Office.tasksOpen) await refreshTaskNav();
    // Keep the caret under the pointer rather than moving focus to the top.
    $(`[data-action="tasks-toggle"]`)?.focus();
    if (target) target.blur?.();
  },
  "rooms-toggle": async (_arg, target) => {
    Office.roomsOpen = !Office.roomsOpen;
    remember("roomsOpen", Office.roomsOpen);
    renderNav();
    if (Office.roomsOpen) await refreshRoomNav();
    $(`[data-action="rooms-toggle"]`)?.focus();
    if (target) target.blur?.();
  },
});

// A per-viewer convenience. Storage can be blocked or cleared, so neither
// reading nor writing it may break the office.
function remember(key, value) {
  try {
    window.localStorage.setItem(`office:${key}`, JSON.stringify(value));
  } catch (error) {
    void error;
  }
}

function remembered(key, fallback) {
  try {
    const held = window.localStorage.getItem(`office:${key}`);
    return held === null ? fallback : JSON.parse(held);
  } catch (error) {
    return fallback;
  }
}

// The menu along the top (the Floor) or down the side, as the owner was
// used to. Kept in this browser; a phone keeps its own row either way.
const NAV_LAYOUTS = { top: "along the top", side: "down the side" };

function navLayout() {
  const kept = remembered("navLayout", "top");
  return NAV_LAYOUTS[kept] ? kept : "top";
}

function applyNavLayout() {
  // One sidebar for every screen; the top menu is retired.
  const side = true;
  document.body.classList.toggle("nav-side", side);
  const toggle = document.getElementById("nav-layout");
  if (!toggle) {
    console.error("Element not found: nav-layout");
    return;
  }
  const label = side ? "Put the menu along the top" : "Put the menu down the side";
  toggle.dataset.action = `nav-layout:${side ? "top" : "side"}`;
  toggle.title = label;
  toggle.setAttribute("aria-label", label);
  toggle.innerHTML = side ? "▭ <span>Menu on top</span>" : "◧ <span>Side menu</span>";
  toggle.hidden = true;
}

Office.actions = Object.assign(Office.actions || {}, {
  // A new thread starts at the Desk's box: say anything, no form needed.
  "thread-new": async () => {
    if (Office.view !== "home") go("home");
    const box = await appeared("desk-start-text", 2000);
    if (!box) {
      console.error("Element not found: desk-start-text");
      return;
    }
    box.focus();
  },
  "nav-layout": async (layout) => {
    if (!NAV_LAYOUTS[layout]) {
      console.error(`Unknown menu layout: ${layout}`);
      return;
    }
    remember("navLayout", layout);
    applyNavLayout();
    renderNav();
    if (Office.view === "office") await rerender();
    toast(`The menu is now ${NAV_LAYOUTS[layout]}.`);
  },
});

let rendering = null;
// Opening a screen plays its entrance; a background refresh never does.
async function rerender(opening = false) {
  // While the Settings window is open, a screen redrawn after a change is the
  // one inside it, not the page behind.
  if (typeof settingsWindowOpen === "function" && settingsWindowOpen()) return settingsWindowDraw();
  const content = $("#content");
  const screen = Office.screens[Office.view] || Office.screens.home;
  if (!content || !screen) return;
  // A slow render must not overwrite a newer one.
  const token = Symbol("render");
  rendering = token;
  const scroll = window.scrollY;
  try {
    const holder = document.createElement("div");
    await screen.render(holder, Office.param);
    if (rendering !== token) return;
    // Swapped at once: screens wire themselves up right after rendering, and
    // a view transition's deferred swap left the room's composer unfound. The
    // entrance plays on the new content instead.
    content.replaceChildren(...holder.childNodes);
    window.scrollTo(0, scroll);
    if (opening) Motion.enter(content);
    // Each row of tabs gets a pill that springs to the tab chosen.
    content.querySelectorAll(".subnav").forEach((row, index) =>
      Motion.follow(row, ":scope > button.active", `tabs:${Office.view}:${index}`, "line")
    );
  } catch (error) {
    if (error instanceof SignedOut) return;
    // A page going away cuts its reads short; that is not a fault to show.
    if (Office.leaving) return;
    console.error(error);
    content.innerHTML = emptyCard("This screen could not load.", explain(error), btn("Try again", "retry"));
  }
}

// Background refresh never replaces a form the owner is typing in.
function isEditing() {
  const active = document.activeElement;
  return Boolean(active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName) && active.value);
}

async function route() {
  // Moving to another screen is a safe moment to take an update.
  if (reloadIfStale()) return;
  stopTimers();
  const [view, ...rest] = (location.hash.replace(/^#/, "") || "home").split("/");
  Office.view = Office.screens[view] ? view : "home";
  Office.param = rest.join("/");
  const screen = Office.screens[Office.view];
  rememberVisit();
  renderTrail(screen);
  renderNav();
  refreshTaskNav();
  refreshRoomNav();
  const content = $("#content");
  if (content) content.innerHTML = `<p class="muted loading">Loading…</p>`;
  await rerender(true);
  window.scrollTo(0, 0);
  if (screen.refresh) every(screen.refresh, async () => {
    // A screen showing something that cannot change asks not to be redrawn:
    // rebuilding the page under the reader loses whatever they had open.
    if (screen.quiet?.()) return;
    // While the stream is open, it redraws these screens; the timer is a fallback.
    if (typeof liveCovers === "function" && liveCovers(Office.view) && liveQuiet(`screen:${Office.view}`)) return;
    if (typeof settingsWindowOpen === "function" && settingsWindowOpen()) return;
    if (!document.hidden && !$("#modal")?.open && !isEditing()) await rerender();
  });
  refreshPulse();
  refreshSetup();
  refreshRuns();
  every(30000, refreshPulse);
  every(6000, async () => {
    if (typeof liveQuiet === "function" && liveQuiet("runs")) return;
    await refreshRuns();
  });
  every(30000, refreshRoomNav);
  if (!Office.balancesTimer) {
    Office.balancesTimer = setInterval(refreshBalances, 300000);
    refreshBalances();
  }
}

async function refreshSetup() {
  try {
    const state = await api("/setup");
    Office.setup = state;
    const link = $("#setup-link");
    const count = $("#setup-count");
    if (count) count.textContent = setupProgress(state);
    // The reminder stays until the owner says they are done with it.
    if (link) link.hidden = Boolean(state.completed_at);
  } catch (error) {
    if (!(error instanceof SignedOut)) console.error(error);
  }
}

// What is moving, what waits on the owner and the month's spend, from any
// screen. The Island shows it; the old tray stays hidden.
async function refreshRuns() {
  let pulse;
  try {
    pulse = await api("/office/pulse");
  } catch (error) {
    if (!(error instanceof SignedOut)) console.warn("The office pulse could not be read:", error);
    return;
  }
  Office.pulse = pulse;
  Office.moving = (pulse.running || []).map((task) => ({ ...task, objective: task.title }));
  Island.pulse(pulse);
}

// What is left with each provider, at most every five minutes.
async function refreshBalances() {
  try {
    Office.balances = await api("/connections/balances");
    Island.balances(Office.balances);
  } catch (error) {
    if (!(error instanceof SignedOut)) console.warn("Provider balances could not be read:", error);
  }
}

async function refreshPulse() {
  const pulse = $("#pulse");
  if (!pulse) return;
  try {
    const status = await api("/status");
    const problems = Object.entries(status).filter(([, part]) => part && typeof part === "object" && part.state && part.state !== "ok").map(([name]) => name);
    pulse.textContent = problems.length ? `Needs attention: ${problems.join(", ")}` : "All systems ready";
    pulse.classList.toggle("warn", problems.length > 0);
  } catch (error) {
    if (!(error instanceof SignedOut)) pulse.textContent = "Status unavailable";
  }
}

function signedIn(session) {
  Office.csrf = session.csrf_token;
  Office.owner = session.owner || "";
  const shell = $("#shell");
  const out = $("#signed-out");
  if (out) out.hidden = true;
  if (shell) shell.hidden = false;
  const initials = $("#owner-initials");
  if (initials) initials.textContent = (Office.owner || "O").slice(0, 2).toUpperCase();
  const name = $("#owner-name");
  if (name) name.textContent = Office.owner || "Owner";
  api("/office/overview")
    .then((overview) => {
      Office.officeName = overview.office_name || "Quarterdeck";
      const office = $("#office-name");
      if (office) office.textContent = Office.officeName;
      const initial = $("#office-initial");
      if (initial) initial.textContent = Office.officeName.charAt(0).toUpperCase();
      updateTaskCount(overview.counts.needs_owner + overview.counts.in_motion);
      Office.recentTasks = overview.tasks || [];
      renderNav();
    })
    .catch(() => {});
  route();
  if (typeof liveStart === "function") liveStart();
  // A new office is walked through its first steps (13e).
  if (typeof onboardingMaybe === "function") onboardingMaybe();
}

// The launcher opens the office with a one-time code in the URL fragment,
// which is never sent to the server. Exchange it, then remove it from the URL.
async function start() {
  const match = location.hash.match(/code=([A-Za-z0-9_-]+)/);
  if (match) {
    history.replaceState(null, "", location.pathname);
    try {
      signedIn(await api("/session", { method: "POST", body: { code: match[1] } }));
      return;
    } catch {
      showSignedOut("That sign-in link has expired. Run python3 -m scripts.launch again.");
      return;
    }
  }
  try {
    signedIn(await api("/session"));
  } catch {
    showSignedOut("");
  }
}

// `toggle` does not bubble, so it is captured. What the reader opened survives
// the next redraw.
document.addEventListener(
  "toggle",
  (event) => {
    const fold = event.target;
    if (fold instanceof HTMLDetailsElement && fold.classList.contains("working")) {
      TaskRoom.workingOpen = fold.open;
    }
    if (fold instanceof HTMLDetailsElement && fold.classList.contains("report-fold")) {
      TaskRoom.reportOpen = fold.open;
    }
  },
  true
);

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  // A sidebar caret sits inside its entry's link: it opens or closes the list
  // under the entry, and must not also follow the link.
  if (target.classList.contains("nav-caret")) event.preventDefault();
  // A link with an action (Settings in the sidebar) does it instead of going.
  if (target.tagName === "A") event.preventDefault();
  const [name, ...parts] = target.dataset.action.split(":");
  const arg = parts.join(":");
  if (name === "close") return closeModal();
  if (name === "retry") return rerender();
  if (name === "sign-out") {
    api("/session", { method: "DELETE" }).finally(() => showSignedOut("Signed out."));
    return;
  }
  const handler = Office.actions?.[name];
  if (!handler) {
    console.error(`No handler for action: ${name}`);
    return;
  }
  Promise.resolve(handler(arg, target)).catch((error) => toast(explain(error), true));
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (form.id === "token-form") {
    event.preventDefault();
    const input = $("#owner-token");
    const token = input?.value.trim() || "";
    if (input) input.value = "";
    api("/session", { method: "POST", body: { token } })
      .then(signedIn)
      .catch((error) => {
        const node = $("#signed-out-error");
        if (node) node.textContent = error.message === "too_many_attempts" ? "Too many attempts. Wait a minute." : "That key was not accepted.";
      });
    return;
  }
  if (form.id === "password-form") {
    event.preventDefault();
    const name = $("#login-name")?.value.trim() || "";
    const passwordInput = $("#login-password");
    const password = passwordInput?.value || "";
    const remember = Boolean($("#login-remember")?.checked);
    api("/session/password", { method: "POST", body: { name, password, remember } })
      .then((session) => {
        if (passwordInput) passwordInput.value = "";
        signedIn(session);
      })
      .catch((error) => {
        const node = $("#signed-out-error");
        if (node) node.textContent = error.message === "too_many_attempts" ? "Too many wrong tries. Wait 15 minutes, or sign in on the office computer." : "That name and password were not accepted.";
      });
    return;
  }
  // A form repeated on one screen (a limit question per task) names its handler in data-form.
  const handler = Office.forms?.[form.id] || Office.forms?.[form.dataset.form];
  if (!handler) return;
  event.preventDefault();
  Promise.resolve(handler(form)).catch((error) => toast(explain(error), true));
});

$("#modal")?.addEventListener("click", (event) => {
  if (event.target === $("#modal")) closeModal();
});
window.addEventListener("hashchange", () => {
  if (Office.csrf) route();
});
applyNavLayout();
start();

// The office live: one stream from the office per
// page (`GET /stream`, app/live.py). Each line names what was just written: a
// run event, a thread message, a run control, a run's new status. Whatever is
// on screen and concerned redraws at once, through the routes it always read.
// The timers stay as a fallback: while the stream is open they act only once a
// minute, and at their own pace again if it drops.

const LIVE_FALLBACK_MS = 60000;
// How long to gather notices before redrawing, per thing redrawn.
const LIVE_WAIT_MS = { thread: 150, task: 400, screen: 2500, sidebar: 1500, runs: 800 };
// Screens that show work across the office and redraw when it moves.
const LIVE_OFFICE_SCREENS = ["home", "tasks", "board"];

const Live = { source: null, connected: false, missed: false, hellos: 0, timers: {} };

// The screens this stream keeps up to date; others keep their own timers.
function liveCovers(view) {
  return view === "task" || LIVE_OFFICE_SCREENS.includes(view);
}

function liveConnected() {
  return Live.connected;
}

// A timer's tick while the stream is open: only the slow fallback acts.
function liveQuiet(key) {
  if (!Live.connected) return false;
  const last = Live.timers[`polled:${key}`] || 0;
  if (Date.now() - last < LIVE_FALLBACK_MS) return true;
  Live.timers[`polled:${key}`] = Date.now();
  return false;
}

// Once soon, however many notices ask for it in the meantime.
function liveSoon(key, work) {
  clearTimeout(Live.timers[key]);
  Live.timers[key] = setTimeout(() => {
    Promise.resolve()
      .then(work)
      .catch((error) => {
        if (error instanceof SignedOut) return;
        // A dropped connection (or a page going away) is not a fault of the page:
        // the next notice, or the fallback, reads it again.
        if (error instanceof TypeError) console.warn("A live redraw could not reach the office:", error.message);
        else console.error(error);
      });
  }, LIVE_WAIT_MS[key] || 1000);
}

function liveRedrawScreen() {
  const screen = Office.screens[Office.view];
  if (screen?.quiet?.()) return;
  if (typeof settingsWindowOpen === "function" && settingsWindowOpen()) return;
  if ($("#modal")?.open || isEditing()) return;
  return rerender();
}

// What a notice concerns, redrawn.
function liveHeard(notice) {
  if (document.hidden) {
    // Read again when the page is looked at.
    Live.missed = true;
    return;
  }
  const all = notice.table === "resync";
  const work = notice.table === "runs" || notice.table === "run_events";
  const topic = typeof RoomState === "object" ? RoomState.topic : null;
  if (Office.view === "topic" && topic && (all || notice.topic === topic.id)) liveSoon("thread", () => roomRefresh());
  if (Office.view === "task" && (all || notice.run === Office.param)) liveSoon("task", liveRedrawScreen);
  if (LIVE_OFFICE_SCREENS.includes(Office.view) && (all || work)) liveSoon("screen", liveRedrawScreen);
  if (all || work || notice.table === "room_messages") liveSoon("sidebar", () => refreshSidebar());
  if (all || notice.table === "runs") liveSoon("runs", () => refreshRuns());
}

function liveStart() {
  if (Live.source || typeof EventSource !== "function") return;
  const source = new EventSource("/stream");
  Live.source = source;
  // Said on every (re)connection. The first comes as the page has just been
  // read; after a reconnection, anything written meanwhile is read again.
  source.addEventListener("hello", () => {
    Live.connected = true;
    Live.hellos += 1;
    if (Live.hellos > 1) liveHeard({ table: "resync" });
  });
  source.onmessage = (event) => {
    let notice;
    try {
      notice = JSON.parse(event.data);
    } catch (error) {
      console.warn("A live notice could not be read:", event.data);
      return;
    }
    if (notice && typeof notice === "object") liveHeard(notice);
  };
  // The browser reconnects by itself; until then the timers carry on.
  source.onerror = () => {
    Live.connected = false;
  };
}

function liveStop() {
  Live.source?.close();
  Live.source = null;
  Live.connected = false;
}

// A page going away (reload, another site) lets go of the stream, and drops
// any redraw still waiting, so nothing is fetched as it unloads.
window.addEventListener("beforeunload", () => {
  Office.leaving = true;
});
window.addEventListener("pagehide", () => {
  Office.leaving = true;
  Object.keys(Live.timers)
    .filter((key) => !key.startsWith("polled:"))
    .forEach((key) => clearTimeout(Live.timers[key]));
  liveStop();
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && Live.missed) {
    Live.missed = false;
    liveHeard({ table: "resync" });
  }
});

"use strict";

// A room a routine feeds (a morning recap, an evening recap): each run's date in
// an index beside the thread, newest first; the newest run in view on opening;
// the routine's long instructions shown once, folded, not in full on every run.

// Runs listed before older ones fold away by month.
const ROOM_DAYS_RECENT = 7;
const ROOM_DAYS_KEY = "office.roomDays";
// Width the page needs for the index beside a still readable thread.
const ROOM_DAYS_BESIDE_PX = 192 + 520;
// What a routine's message opens with: app/routines/service.py asked_of().
const ROOM_RUN_MARK = " (a routine, not typed by me):";

const RoomDays = { drawn: "", open: null };

function roomDaysRead() {
  try {
    const kept = localStorage.getItem(ROOM_DAYS_KEY);
    if (kept === "open" || kept === "closed") return kept === "open";
  } catch (error) {
    console.warn("The index's folding could not be read:", error);
  }
  // Folded on a phone, so the thread comes first.
  return !window.matchMedia("(max-width: 760px)").matches;
}

function roomDaysKeep(open) {
  RoomDays.open = open;
  try {
    localStorage.setItem(ROOM_DAYS_KEY, open ? "open" : "closed");
  } catch (error) {
    console.warn("The index's folding could not be kept:", error);
  }
}

// The routine's runs in the main stream, oldest first.
function roomRuns(items) {
  return (items || []).filter((item) => item.routine && !item.thread_id);
}

function roomRunDate(at) {
  const day = new Date(at);
  if (Number.isNaN(day.getTime())) return "";
  return day.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function roomRunMonth(at) {
  const day = new Date(at);
  if (Number.isNaN(day.getTime())) return "";
  return day.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
}

function roomRunTime(at) {
  const day = new Date(at);
  if (Number.isNaN(day.getTime())) return "";
  return day.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

// "Morning recap (a routine, not typed by me): …" → "Morning recap".
function roomRunTitle(item) {
  const text = String(item.text || "");
  const at = text.indexOf(ROOM_RUN_MARK);
  return at > 0 ? text.slice(0, at).trim() : "Routine";
}

function roomRunLatest() {
  const runs = roomRuns(roomReader.items);
  return runs.length ? runs[runs.length - 1] : null;
}

// One run in the thread: a single line. What it asked is in the index, once;
// a run is folded open here only when it asked something different.
function roomRunItem(item) {
  const id = String(item.id || "");
  const latest = roomRunLatest();
  const differs = latest && latest.id !== id && latest.text !== item.text;
  const asks = differs
    ? `<details class="room-run-asks"><summary>What it asked this time</summary><p class="room-text">${esc(item.text)}</p></details>`
    : "";
  return `<li class="room-run" data-mid="${esc(id)}"><div class="room-run-head"><span class="room-run-mark" aria-hidden="true">↻</span><strong>${esc(roomRunTitle(item))}</strong><span class="muted">${esc(roomRunDate(item.at))} · ${esc(roomRunTime(item.at))}</span></div>${asks}</li>`;
}

function roomDaysEntry(run, sameDay) {
  const label = roomRunDate(run.at) + (sameDay ? ` · ${roomRunTime(run.at)}` : "");
  return `<li><button type="button" class="room-days-go" data-action="room-run:${esc(run.id)}" data-run="${esc(run.id)}">${esc(label)}</button></li>`;
}

function roomDaysList(newest) {
  const perDay = new Map();
  newest.forEach((run) => perDay.set(roomRunDate(run.at), (perDay.get(roomRunDate(run.at)) || 0) + 1));
  const entry = (run) => roomDaysEntry(run, perDay.get(roomRunDate(run.at)) > 1);
  const recent = newest.slice(0, ROOM_DAYS_RECENT).map(entry).join("");
  const months = new Map();
  newest.slice(ROOM_DAYS_RECENT).forEach((run) => {
    const month = roomRunMonth(run.at);
    months.set(month, [...(months.get(month) || []), run]);
  });
  const older = [...months]
    .map(
      ([month, runs]) =>
        `<li><details class="room-days-month"><summary>${esc(month)} <span class="muted">${esc(runs.length)}</span></summary><ol>${runs.map(entry).join("")}</ol></details></li>`
    )
    .join("");
  return `<ol class="room-days-list">${recent}${older}</ol>`;
}

// The index, drawn only when the runs or its folding change, so a month opened
// by hand stays open through every refresh.
function roomDaysDraw() {
  const nav = document.getElementById("room-days");
  const holder = document.getElementById("room-messages");
  if (!nav || !holder) return;
  const runs = roomRuns(roomReader.items);
  holder.classList.toggle("has-days", runs.length > 0);
  nav.hidden = !runs.length;
  if (!runs.length) {
    nav.innerHTML = "";
    RoomDays.drawn = "";
    return;
  }
  if (RoomDays.open === null) RoomDays.open = roomDaysRead();
  const latest = runs[runs.length - 1];
  const drawn = `${RoomDays.open}|${runs.map((run) => run.id).join(",")}|${latest.text.length}`;
  if (drawn !== RoomDays.drawn || !nav.firstElementChild) {
    RoomDays.drawn = drawn;
    const newest = [...runs].reverse();
    nav.innerHTML = `<div class="room-days-inner">
      <button type="button" class="room-days-toggle" data-action="room-days-toggle" aria-expanded="${RoomDays.open}" aria-controls="room-days-body">
        <span>Runs</span><span class="muted">${esc(runs.length)}</span><span class="room-days-chevron" aria-hidden="true">${RoomDays.open ? "‹" : "›"}</span>
      </button>
      <div id="room-days-body" class="room-days-body"${RoomDays.open ? "" : " hidden"}>
        ${roomDaysList(newest)}
        <details class="room-days-asks"><summary>What it asks</summary><p>${esc(latest.text)}</p></details>
      </div>
    </div>`;
  }
  holder.classList.toggle("days-folded", !RoomDays.open);
  roomDaysPlace();
}

// Beside the thread when the page has the width, above it when not.
function roomDaysPlace() {
  const holder = document.getElementById("room-messages");
  const main = holder?.closest(".topic-main");
  if (!holder || !main || !holder.classList.contains("has-days")) return;
  holder.classList.toggle("days-beside", main.getBoundingClientRect().width >= ROOM_DAYS_BESIDE_PX);
  // Above the thread it stays in reach under the page's top bar.
  const bar = document.querySelector("main > header");
  holder.style.setProperty("--room-days-top", `${bar?.offsetHeight || 64}px`);
}
window.addEventListener("resize", roomDaysPlace, { passive: true });

function roomRunShow(id, smooth) {
  const element = document.querySelector(`#room-feed [data-mid="${CSS.escape(id)}"]`);
  if (!element) {
    console.error("Run not found in the thread:", id);
    return false;
  }
  element.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
  document.querySelectorAll("#room-days .room-days-go").forEach((button) => {
    if (button.dataset.run === id) button.setAttribute("aria-current", "true");
    else button.removeAttribute("aria-current");
  });
  return true;
}

// On opening, the newest run at the top of the page, not the oldest day.
function roomDaysOpening() {
  const latest = roomRunLatest();
  return latest ? roomRunShow(latest.id, false) : false;
}

Office.actions = Object.assign(Office.actions || {}, {
  "room-run": (id) => {
    // Above the thread, an open index would cover the run it jumped to.
    const holder = document.getElementById("room-messages");
    if (RoomDays.open && !holder?.classList.contains("days-beside")) {
      RoomDays.open = false;
      roomDaysDraw();
    }
    if (!roomRunShow(id, true)) return;
    const element = document.querySelector(`#room-feed [data-mid="${CSS.escape(id)}"]`);
    element?.classList.add("room-flash");
    setTimeout(() => element?.classList.remove("room-flash"), 1600);
  },
  "room-days-toggle": () => {
    roomDaysKeep(!RoomDays.open);
    roomDaysDraw();
  },
});

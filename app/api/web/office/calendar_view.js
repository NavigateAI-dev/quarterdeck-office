"use strict";

// The owner's calendar to look at and edit: day, week,
// month and year. Edits to the Mac's calendars are made by the helper through
// macOS; a calendar that came as a link, or one macOS keeps read-only, says so.

const CAL_MODES = [
  ["agenda", "Agenda"],
  ["day", "Day"],
  ["week", "Week"],
  ["month", "Month"],
  ["year", "Year"],
];
const CAL_HOUR_PX = 56;
// The agenda lists this many days from the one in view.
const CAL_AGENDA_DAYS = 14;
// A phone opens on the agenda: seven columns do not fit a hand.
const CAL_NARROW = "(max-width: 760px)";
// `after` runs once an edit is queued: a proposal from a discussion marks itself added.
const CalView = { mode: "week", anchor: null, events: [], writable: [], byId: new Map(), after: null };

function calStartOfDay(date) {
  const day = new Date(date);
  day.setHours(0, 0, 0, 0);
  return day;
}

function calAddDays(date, days) {
  const moved = new Date(date);
  moved.setDate(moved.getDate() + days);
  return moved;
}

// Weeks start on Monday, as the owner's calendar does in Europe.
function calWeekStart(date) {
  const day = calStartOfDay(date);
  return calAddDays(day, -((day.getDay() + 6) % 7));
}

function calKey(date) {
  const d = new Date(date);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function calParse(key) {
  const [y, m, d] = String(key || "").split("-").map(Number);
  return y && m && d ? new Date(y, m - 1, d) : calStartOfDay(new Date());
}

function calRange(mode, anchor) {
  if (mode === "day") return [calStartOfDay(anchor), calAddDays(calStartOfDay(anchor), 1)];
  if (mode === "agenda") return [calStartOfDay(anchor), calAddDays(calStartOfDay(anchor), CAL_AGENDA_DAYS)];
  if (mode === "week") {
    const start = calWeekStart(anchor);
    return [start, calAddDays(start, 7)];
  }
  if (mode === "month") {
    const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
    const start = calWeekStart(first);
    return [start, calAddDays(start, 42)];
  }
  return [new Date(anchor.getFullYear(), 0, 1), new Date(anchor.getFullYear() + 1, 0, 1)];
}

function calStep(mode, anchor, direction) {
  if (mode === "day") return calAddDays(anchor, direction);
  if (mode === "agenda") return calAddDays(anchor, CAL_AGENDA_DAYS * direction);
  if (mode === "week") return calAddDays(anchor, 7 * direction);
  if (mode === "month") return new Date(anchor.getFullYear(), anchor.getMonth() + direction, 1);
  return new Date(anchor.getFullYear() + direction, 0, 1);
}

function calTitle(mode, anchor) {
  if (mode === "agenda") {
    const last = calAddDays(anchor, CAL_AGENDA_DAYS - 1);
    return `${anchor.toLocaleDateString(undefined, { day: "numeric", month: "short" })} – ${last.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}`;
  }
  if (mode === "day") return anchor.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  if (mode === "week") {
    const [start, end] = calRange("week", anchor);
    const last = calAddDays(end, -1);
    return `${start.toLocaleDateString(undefined, { day: "numeric", month: "short" })} – ${last.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}`;
  }
  if (mode === "month") return anchor.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  return String(anchor.getFullYear());
}

function calTime(value) {
  return calShort(value);
}

function calOnDay(event, day) {
  const start = calStartOfDay(day);
  const end = calAddDays(start, 1);
  return new Date(event.starts_at) < end && new Date(event.ends_at) > start;
}

// An event in its calendar's colour: a stripe and a tint; the text stays ink.
function calChip(event, extra = "") {
  const when = event.all_day ? "" : `<small>${esc(calShort(event.starts_at))}</small> `;
  return `<button type="button" class="calv-chip ${event.all_day ? "is-allday" : ""} ${extra}" style="--cal:${calColour(event)}" data-action="calv-open:${esc(event.id)}" title="${esc(event.title)}${event.all_day ? "" : ` · ${esc(calSpan(event.starts_at, event.ends_at))}`}">${when}<span>${esc(event.title)}</span></button>`;
}

// Day and week: one column per day, hours down the side, all-day events on top.
function calGrid(days) {
  const now = new Date();
  const header = days
    .map((day) => `<div class="calv-dayhead ${calKey(day) === calKey(now) ? "is-today" : ""}"><span>${esc(day.toLocaleDateString(undefined, { weekday: "short" }))}</span><strong>${day.getDate()}</strong></div>`)
    .join("");
  const allDay = days
    .map((day) => `<div class="calv-allday-cell">${CalView.events.filter((e) => e.all_day && calOnDay(e, day)).map((e) => calChip(e)).join("")}</div>`)
    .join("");
  const hours = Array.from({ length: 24 }, (_, h) => `<div class="calv-hour"><span>${esc(calHourLabel(h))}</span></div>`).join("");
  const columns = days
    .map((day) => {
      // Only events that overlap share the width; the rest keep all of it.
      const timed = calLanes(
        CalView.events
          .filter((e) => !e.all_day && calOnDay(e, day))
          .map((e) => {
            const start = Math.max(0, (new Date(e.starts_at) - day) / 60000);
            const end = Math.min(1440, (new Date(e.ends_at) - day) / 60000);
            return { e, start, end: Math.max(end, start + 20) };
          })
      );
      const blocks = timed
        .map(({ e, start, end, lane, lanes, span }) => {
          const minutes = end - start;
          const size = minutes < 40 ? " is-short" : minutes < 75 ? " is-mid" : "";
          const width = 100 / lanes;
          const passed = new Date(e.ends_at) < now ? " is-past" : "";
          return `<button type="button" class="calv-event${size}${passed}${e.busy === false ? " is-free" : ""}" style="--cal:${calColour(e)};top:${(start / 60) * CAL_HOUR_PX + 1}px;height:${(minutes / 60) * CAL_HOUR_PX - 3}px;left:calc(${lane * width}% + 2px);width:calc(${span * width}% - 5px)" data-action="calv-open:${esc(e.id)}" title="${esc(e.title)} · ${esc(calSpan(e.starts_at, e.ends_at))}"><strong>${esc(e.title)}</strong><small>${esc(calSpan(e.starts_at, e.ends_at))}</small></button>`;
        })
        .join("");
      const isToday = calKey(day) === calKey(now);
      const line = isToday ? `<div class="calv-now" style="top:${((now - day) / 3600000) * CAL_HOUR_PX}px" aria-label="Now"></div>` : "";
      return `<div class="calv-column ${isToday ? "is-today" : ""}" data-day="${calKey(day)}">${blocks}${line}</div>`;
    })
    .join("");
  const cols = `repeat(${days.length}, minmax(${days.length > 1 ? 110 : 200}px, 1fr))`;
  return `<div class="calv-grid-frame"><div class="calv-grid" style="--days:${days.length}">
      <div class="calv-row calv-head"><div class="calv-gutter"></div><div class="calv-cols" style="grid-template-columns:${cols}">${header}</div></div>
      <div class="calv-row calv-allday"><div class="calv-gutter"><span>all day</span></div><div class="calv-cols" style="grid-template-columns:${cols}">${allDay}</div></div>
      <div class="calv-scroll" id="calv-scroll"><div class="calv-row"><div class="calv-gutter calv-hours">${hours}</div><div class="calv-cols calv-body" style="grid-template-columns:${cols};height:${24 * CAL_HOUR_PX}px">${columns}</div></div></div>
    </div></div>`;
}

// A list of the days ahead: easy to read on a phone, and to scan on
// a computer. Days with nothing are left out; today is marked.
function calAgenda(anchor) {
  const today = calKey(new Date());
  const now = new Date();
  const days = Array.from({ length: CAL_AGENDA_DAYS }, (_, i) => calAddDays(anchor, i))
    .map((day) => {
      const events = CalView.events
        .filter((e) => calOnDay(e, day))
        .sort((a, b) => Number(b.all_day) - Number(a.all_day) || new Date(a.starts_at) - new Date(b.starts_at));
      if (!events.length) return "";
      const rows = events
        .map((e) => {
          const when = e.all_day ? "All day" : calSpan(e.starts_at, e.ends_at);
          const passed = !e.all_day && new Date(e.ends_at) < now ? " is-past" : "";
          return `<button type="button" class="calv-agenda-event${passed}" style="--cal:${calColour(e)}" data-action="calv-open:${esc(e.id)}"><span class="calv-agenda-time">${esc(when)}</span><span class="calv-agenda-title"><strong>${esc(e.title)}</strong>${e.location ? `<small>${esc(e.location)}</small>` : ""}</span><span class="calv-agenda-cal">${esc(e.calendar || "")}</span></button>`;
        })
        .join("");
      const isToday = calKey(day) === today;
      return `<section class="calv-agenda-day ${isToday ? "is-today" : ""}" aria-label="${esc(day.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" }))}">
        <button type="button" class="calv-agenda-date" data-action="calv-goto:day:${calKey(day)}"><strong>${day.getDate()}</strong><span>${esc(day.toLocaleDateString(undefined, { weekday: "short" }))}<br>${esc(day.toLocaleDateString(undefined, { month: "short" }))}${isToday ? `<em class="calv-agenda-today">Today</em>` : ""}</span></button>
        <div class="calv-agenda-list">${rows}</div></section>`;
    })
    .join("");
  return `<div class="calv-agenda">${days || `<p class="muted calv-agenda-none">Nothing on your calendar in these ${CAL_AGENDA_DAYS} days.</p>`}</div>`;
}

function calMonth(anchor) {
  const [start] = calRange("month", anchor);
  const today = calKey(new Date());
  const names = Array.from({ length: 7 }, (_, i) => `<div class="calv-weekday">${esc(calAddDays(start, i).toLocaleDateString(undefined, { weekday: "short" }))}</div>`).join("");
  const cells = Array.from({ length: 42 }, (_, i) => {
    const day = calAddDays(start, i);
    const events = CalView.events.filter((e) => calOnDay(e, day));
    const shown = events.slice(0, 3).map((e) => calChip(e, "is-small")).join("");
    const more = events.length > 3 ? btn(`+${events.length - 3} more`, `calv-goto:day:${calKey(day)}`, "calv-more") : "";
    return `<div class="calv-cell ${day.getMonth() !== anchor.getMonth() ? "is-other" : ""} ${calKey(day) === today ? "is-today" : ""}"><button type="button" class="calv-date" data-action="calv-goto:day:${calKey(day)}" aria-label="${esc(day.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" }))}">${day.getDate()}</button>${shown}${more}</div>`;
  }).join("");
  return `<div class="calv-month"><div class="calv-weekdays">${names}</div><div class="calv-cells">${cells}</div></div>`;
}

function calYear(anchor) {
  const today = calKey(new Date());
  const counts = new Map();
  CalView.events.forEach((e) => {
    for (let day = calStartOfDay(e.starts_at); day < new Date(e.ends_at) && counts.size < 4000; day = calAddDays(day, 1)) {
      counts.set(calKey(day), (counts.get(calKey(day)) || 0) + 1);
    }
  });
  const months = Array.from({ length: 12 }, (_, m) => {
    const first = new Date(anchor.getFullYear(), m, 1);
    const start = calWeekStart(first);
    const cells = Array.from({ length: 42 }, (_, i) => {
      const day = calAddDays(start, i);
      if (day.getMonth() !== m) return `<span class="calv-mini-empty"></span>`;
      const count = counts.get(calKey(day)) || 0;
      const level = count === 0 ? 0 : count === 1 ? 1 : count <= 3 ? 2 : 3;
      return `<button type="button" class="calv-mini-day level-${level} ${calKey(day) === today ? "is-today" : ""}" data-action="calv-goto:day:${calKey(day)}" title="${esc(day.toLocaleDateString())}: ${count} event${count === 1 ? "" : "s"}">${day.getDate()}</button>`;
    }).join("");
    return `<section class="calv-mini"><button type="button" class="calv-mini-name" data-action="calv-goto:month:${calKey(first)}">${esc(first.toLocaleDateString(undefined, { month: "long" }))}</button><div class="calv-mini-grid">${cells}</div></section>`;
  }).join("");
  return `<div class="calv-year">${months}</div>`;
}

async function renderCalendarView(content, param) {
  calSendZone();
  const [mode, key] = String(param || "").split("/");
  const narrow = window.matchMedia?.(CAL_NARROW).matches;
  CalView.mode = CAL_MODES.some(([id]) => id === mode) ? mode : CalView.chosen ? CalView.mode : narrow ? "agenda" : "week";
  CalView.anchor = key ? calParse(key) : CalView.anchor || calStartOfDay(new Date());
  const [start, end] = calRange(CalView.mode, CalView.anchor);
  const [day, writable, sources] = await Promise.all([
    api(`/calendars/day?start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}`),
    api("/calendars/writable").catch(() => []),
    api("/calendars").catch(() => ({ sources: [] })),
  ]);
  CalView.events = day.events || [];
  CalView.byId = new Map(CalView.events.map((e) => [String(e.id), e]));
  CalView.writable = Array.isArray(writable) ? writable : [];
  CalView.kinds = new Map((sources.sources || []).map((s) => [s.id, s.kind]));
  const modes = CAL_MODES.map(([id, label]) => `<button type="button" class="${CalView.mode === id ? "active" : ""}" aria-pressed="${CalView.mode === id}" data-action="calv-goto:${id}:${calKey(CalView.anchor)}">${label}</button>`).join("");
  const body =
    !day.connected
      ? emptyCard("No calendar connected yet.", "Connect your Mac's calendars, or sign in to Google or Microsoft.", `<a class="button primary" href="#calendars">Connect a calendar</a>`)
      : CalView.mode === "agenda"
        ? calAgenda(CalView.anchor)
        : CalView.mode === "month"
        ? calMonth(CalView.anchor)
        : CalView.mode === "year"
          ? calYear(CalView.anchor)
          : calGrid(CalView.mode === "day" ? [CalView.anchor] : Array.from({ length: 7 }, (_, i) => calAddDays(calWeekStart(CalView.anchor), i)));
  content.innerHTML =
    `<div class="calv-top"><div class="calv-nav">${btn("Today", `calv-goto:${CalView.mode}:${calKey(new Date())}`)}${btn("‹", `calv-step:-1`, "calv-arrow", 'aria-label="Earlier"')}${btn("›", `calv-step:1`, "calv-arrow", 'aria-label="Later"')}<h1>${esc(calTitle(CalView.mode, CalView.anchor))}</h1></div>
      <div class="calv-modes" role="group" aria-label="View">${modes}</div>
      ${CalView.writable.length ? btn("＋ New event", "calv-new", "primary") : ""}</div>` +
    `<p class="muted calv-note">${esc(day.status ? calStatusLine(day.status) : "")}${!CalView.writable.length ? " · Read only: connect this Mac's calendars, or sign in to Google or Microsoft, to add events." : ["day", "week"].includes(CalView.mode) ? " · Click an empty time to add an event there." : ""}</p>` +
    body;
  // Morning in view: set once the page is on screen, as a scroll set before is lost.
  setTimeout(() => {
    const scroller = document.getElementById("calv-scroll");
    if (scroller && !scroller.dataset.placed) {
      // Just before the first event, or at 7:00, with its hour label in view.
      const first = Math.min(7, ...CalView.events.filter((e) => !e.all_day).map((e) => new Date(e.starts_at).getHours()).filter((h) => h >= 5));
      scroller.scrollTop = Math.max(0, first * CAL_HOUR_PX - 14);
      scroller.dataset.placed = "1";
    }
  }, 0);
  content.querySelector(".calv-body")?.addEventListener("click", (event) => {
    if (event.target.closest(".calv-event")) return;
    const column = event.target.closest(".calv-column");
    if (!column || !CalView.writable.length) return;
    const top = event.clientY - column.getBoundingClientRect().top;
    const minutes = Math.max(0, Math.min(1410, Math.floor(top / (CAL_HOUR_PX / 2)) * 30));
    const start = calParse(column.dataset.day);
    start.setMinutes(minutes);
    calEditForm(null, start);
  });
}

// Why an event cannot be changed, in the owner's terms.
function calReadOnlyReason(event) {
  const kind = CalView.kinds?.get(event.source_id);
  if (kind === "mac") return "macOS keeps this calendar read-only: it is subscribed or shared with you.";
  if (kind === "google_api" || kind === "microsoft_api")
    return "Your account can only read this calendar, or it was connected for reading. To allow changes, sign in again under Calendars.";
  return "This calendar came in as a link, which can only be read. To change it here, connect it with Sign in or through this Mac.";
}

function calDetails(event) {
  const when = event.all_day
    ? new Date(event.starts_at).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })
    : `${new Date(event.starts_at).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} – ${calTime(event.ends_at)}`;
  const rows = [
    ["When", when],
    ["Calendar", event.calendar || "—"],
    event.location ? ["Where", event.location] : null,
    event.attendees ? ["People", `${event.attendees} invited`] : null,
  ]
    .filter(Boolean)
    .map(([label, value]) => `<div class="detail-line"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`)
    .join("");
  const notes = event.description ? `<p class="calv-notes">${esc(event.description).replace(/\n/g, "<br>")}</p>` : "";
  const actions = event.writable
    ? `<div class="button-row">${btn("Delete", `calv-delete:${event.id}`, "danger")}${btn("Edit", `calv-edit:${event.id}`, "primary")}</div>`
    : `<p class="muted">${esc(calReadOnlyReason(event))}</p><div class="button-row">${btn("Close", "close", "primary")}</div>`;
  modal(event.title, rows + notes, actions);
}

function calLocalInput(date) {
  const d = new Date(date);
  return { day: calKey(d), time: `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}` };
}

function calEditForm(event, start, preset = null) {
  const begins = event ? new Date(event.starts_at) : start || new Date();
  const ends = event ? new Date(event.ends_at) : preset?.ends_at ? new Date(preset.ends_at) : new Date(begins.getTime() + 3600000);
  const from = calLocalInput(begins);
  const to = calLocalInput(event?.all_day ? calAddDays(ends, -1) : ends);
  const choices = CalView.writable.map((c) => [c.id, `${c.name}${c.account ? ` · ${c.account}` : ""}`]);
  const chosen = event?.calendar_id && CalView.writable.some((c) => c.id === event.calendar_id) ? event.calendar_id : CalView.writable[0]?.id;
  formModal(
    event ? "Edit event" : "New event",
    "calv-form",
    `<input type="hidden" name="event_id" value="${esc(event?.id || "")}">
     ${field("Title", "calv-title", { value: event?.title || preset?.title || "", attrs: 'maxlength="1000"' })}
     ${field("Calendar", "calv-calendar", { type: "select", value: chosen, choices })}
     <label class="check"><input type="checkbox" name="calv-allday" ${event?.all_day ? "checked" : ""}> All day</label>
     <div class="calv-when">
       <div>${field("Starts", "calv-start-day", { type: "date", value: from.day })}</div>
       <div>${field("at", "calv-start-time", { type: "time", value: from.time, required: false })}</div>
       <div>${field("Ends", "calv-end-day", { type: "date", value: to.day })}</div>
       <div>${field("at", "calv-end-time", { type: "time", value: to.time, required: false })}</div>
     </div>
     ${field("Where", "calv-location", { required: false, value: event?.location || "", attrs: 'maxlength="1000"' })}
     ${field("Notes", "calv-notes", { type: "textarea", required: false, value: event?.description || "", attrs: 'maxlength="4000" rows="3"' })}
     ${event?.attendees ? `<p class="muted">People invited stay invited; changing the time here does not tell them.</p>` : ""}`,
    event ? "Save changes" : "Add to calendar"
  );
}

// Applied by the helper on the Mac within seconds; the view reloads when it has.
async function calAwait(change, said) {
  if (!change?.id) return;
  toast("Saving to your calendar…");
  for (let i = 0; i < 40; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const now = await api(`/calendars/changes/${change.id}`).catch(() => null);
    if (now?.status === "done") {
      toast(said);
      rerender();
      return;
    }
    if (now?.status === "unknown") {
      toast("Your calendar did not answer, so the change may or may not have been made. Check it there; it is not sent again.", true);
      rerender();
      return;
    }
    if (now?.status === "failed") {
      toast(explain(new Error(now.error || "calendar_change_failed")), true);
      rerender();
      return;
    }
  }
  toast("The calendar helper has not picked this up yet. It will as soon as it runs.", true);
}

Office.forms = Object.assign(Office.forms || {}, {
  "calv-form": async (form) => {
    const data = new FormData(form);
    const allDay = data.get("calv-allday") === "on";
    const at = (dayName, timeName, fallback) => {
      const day = calParse(String(data.get(dayName) || ""));
      const [h, m] = String(data.get(timeName) || fallback).split(":").map(Number);
      if (!allDay) day.setHours(h || 0, m || 0, 0, 0);
      return day;
    };
    const starts = at("calv-start-day", "calv-start-time", "09:00");
    let ends = at("calv-end-day", "calv-end-time", "10:00");
    if (allDay) ends = calAddDays(ends, 1);
    if (ends < starts) return toast("It ends before it starts.", true);
    const body = {
      calendar_id: String(data.get("calv-calendar") || ""),
      title: String(data.get("calv-title") || "").trim(),
      starts_at: starts.toISOString(),
      ends_at: ends.toISOString(),
      all_day: allDay,
      location: String(data.get("calv-location") || "").trim(),
      notes: String(data.get("calv-notes") || "").trim(),
    };
    const id = String(data.get("event_id") || "");
    const change = await attempt(() => api(id ? `/calendars/events/${id}` : "/calendars/events", { method: id ? "PUT" : "POST", body }));
    if (change === undefined) return;
    closeModal();
    const after = CalView.after;
    CalView.after = null;
    if (after) await after(change);
    calAwait(change, id ? "Changed in your calendar." : "Added to your calendar.");
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "calv-goto": (arg) => {
    const [mode, key] = String(arg).split(":");
    CalView.chosen = true;
    location.hash = `calendar/${mode}/${key}`;
  },
  "calv-step": (direction) => {
    const next = calStep(CalView.mode, CalView.anchor, Number(direction));
    location.hash = `calendar/${CalView.mode}/${calKey(next)}`;
  },
  "calv-open": (id) => {
    const event = CalView.byId.get(String(id));
    if (event) calDetails(event);
  },
  "calv-new": () => {
    CalView.after = null;
    const start = new Date(CalView.anchor);
    start.setHours(Math.max(9, new Date().getHours() + 1), 0, 0, 0);
    calEditForm(null, start);
  },
  "calv-edit": (id) => {
    CalView.after = null;
    const event = CalView.byId.get(String(id));
    if (event) calEditForm(event);
  },
  "calv-delete": async (id) => {
    const event = CalView.byId.get(String(id));
    if (!event || !window.confirm(`Delete “${event.title}” from your calendar? A repeating event loses only this occurrence.`)) return;
    const change = await attempt(() => api(`/calendars/events/${id}`, { method: "DELETE" }));
    if (change === undefined) return;
    closeModal();
    calAwait(change, "Deleted from your calendar.");
  },
});

Office.screens.calendar = { title: "Calendar", render: renderCalendarView, refresh: 60000, quiet: () => Boolean($("#modal")?.open) };

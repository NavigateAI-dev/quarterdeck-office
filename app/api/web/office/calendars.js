"use strict";

// The owner's calendars: connected read-only, so colleagues
// know when the owner is free and work around their day. A link or password
// is sent once and kept sealed by the office; the page never shows it again.

const Cal = { sources: [], preferences: null, apps: {} };

// Sign in on the company's own page. Each needs the owner's own
// sign-in app, made once; the guide names every click and the exact address.
const CAL_SIGN_IN = {
  google: {
    name: "Google",
    button: "Sign in with Google",
    secret: true,
    steps: [
      "Open console.cloud.google.com and create a project, for example “Quarterdeck”.",
      "APIs & Services → Library: find “Google Calendar API” and press Enable. To connect Gmail and Drive on the Accounts page too, enable “Gmail API” and “Google Drive API” as well.",
      "Google Auth Platform → Branding: name it “Quarterdeck” with your email. Audience: External, add yourself as a test user, then Publish the app, so your sign-in does not lapse after a week. Google will call it unverified; that is expected for an app only you use.",
      "Data access → Add scopes: “…/auth/calendar.readonly” and “…/auth/calendar.events” (reading, and changes you make here). For Gmail and Drive add “…/auth/gmail.readonly” and “…/auth/drive.readonly” too; both only read.",
      "Clients → Create client → Web application. Under Authorized redirect URIs add the address below, exactly. Create, then copy the Client ID and Client secret here.",
    ],
  },
  microsoft: {
    name: "Microsoft",
    button: "Sign in with Microsoft",
    secret: false,
    steps: [
      "Open entra.microsoft.com (any Microsoft account works) → App registrations → New registration, name “Quarterdeck”.",
      "Supported account types: “Accounts in any organizational directory and personal Microsoft accounts”.",
      "Redirect URI: choose “Public client/native (mobile & desktop)” and enter the address below, exactly. Register.",
      "Copy the “Application (client) ID” here. No secret is needed; at sign-in you allow reading and changing your calendars yourself.",
    ],
  },
};
const CAL_RETURN = {
  connected: ["Connected. Your day is on the floor.", false],
  sign_in_declined: ["You did not allow access, so nothing was connected.", true],
  sign_in_expired: ["That sign-in took longer than ten minutes. Press Sign in again.", true],
};

function calRedirect() {
  return `${location.origin.replace("://127.0.0.1", "://localhost")}/calendars/oauth/callback`;
}

// How each service is connected, and where the owner finds what to paste.
const CAL_PROVIDERS = {
  mac: { name: "This Mac (Calendar app)", kind: "mac", help: "" },
  google: {
    name: "Google Calendar",
    kind: "ics",
    help: "In Google Calendar on the web: Settings, then your calendar under “Settings for my calendars”, then “Integrate calendar”. Copy the “Secret address in iCal format” and paste it here.",
  },
  icloud: {
    name: "iCloud (iPhone, iPad, Mac)",
    kind: "caldav",
    help: "Your Apple ID email, and an app-specific password made at account.apple.com under Sign-In and Security. Your Apple ID password itself is never asked for.",
  },
  outlook: {
    name: "Outlook and Microsoft 365",
    kind: "ics",
    help: "In Outlook on the web: Settings, Calendar, Shared calendars, “Publish a calendar”. Choose “Can view all details”, publish, and copy the ICS link.",
  },
  fastmail: { name: "Fastmail", kind: "caldav", help: "Your Fastmail address and an app password made under Settings, Privacy & Security, with calendar access." },
  yahoo: { name: "Yahoo", kind: "caldav", help: "Your Yahoo address and an app password made under Account security." },
  nextcloud: { name: "Nextcloud", kind: "caldav", server: true, help: "Your Nextcloud's CalDAV address (it ends in /remote.php/dav), your user name and an app password." },
  other: { name: "Another calendar", kind: "ics", either: true, help: "Any link ending in .ics or starting with webcal://, or a CalDAV address with its user name and password." },
};

const CAL_SHARE = [
  ["busy", "Busy or free only", "When you are taken, never what it is."],
  ["titles", "Titles and times", "Enough to prepare for a meeting. Descriptions, attendees, links and places stay on this computer."],
  ["all", "Everything", "Notes, attendees and places, sent to your model providers when a task or a conversation uses them."],
];

// The Mac's calendars: what the owner sees between their press and the events.
function calMacState(source) {
  if (source.helper !== "running")
    return ["The calendar helper is not running", "It starts with the office. In the Quarterdeck folder run: python3 -m scripts.launch", "clay"];
  if (source.last_error === "mac_access_denied")
    return ["macOS said no", "Allow “Quarterdeck Calendar” in System Settings, Privacy & Security, Calendars, then press Ask again.", "clay"];
  if (source.last_error === "mac_access_restricted")
    return ["Calendar access is restricted on this Mac", "Screen Time or a device profile is blocking it.", "clay"];
  if (!source.last_synced_at)
    return ["Answer macOS", "A macOS dialog asks: “Quarterdeck Calendar” would like access to your calendars. Press Allow.", "amber"];
  return null;
}

function calSourceCard(source) {
  const provider = CAL_PROVIDERS[source.provider] || CAL_PROVIDERS.other;
  if (source.kind === "mac") {
    const waiting = calMacState(source);
    const accounts = source.calendars?.length ? `${source.calendars.length} calendar${source.calendars.length === 1 ? "" : "s"}: ${source.calendars.join(", ")}` : "Every calendar in the Calendar app";
    return `<article class="card cal-source" data-calendar="${esc(source.id)}" data-mac>
      <div class="card-top"><h3>This Mac</h3>${waiting ? tag(waiting[2] === "amber" ? "waiting" : "needs attention", waiting[2]) : tag("connected")}</div>
      <p class="muted">${esc(accounts)}</p>
      ${waiting ? `<p class="cal-state"><strong>${esc(waiting[0])}.</strong></p>` : ""}
      <p>${waiting ? esc(waiting[1]) : `${esc(source.event_count)} event${source.event_count === 1 ? "" : "s"} in the coming month · synced ${esc(when(source.last_synced_at))}`}</p>
      ${source.last_error === "mac_access_denied" ? `<p><a href="x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars">Open Calendars privacy settings</a></p>` : ""}
      <div class="button-row tight">${source.last_error ? btn("Ask again", "cal-mac", "primary") : btn("Sync now", `cal-sync:${source.id}`)}${btn("Disconnect", `cal-remove:${source.id}`, "danger")}</div>
    </article>`;
  }
  const state = source.last_error
    ? tag("needs attention", "clay")
    : source.last_synced_at
      ? tag("connected")
      : tag("not read yet", "gray");
  const what = source.calendars?.length ? `${source.calendars.length} calendar${source.calendars.length === 1 ? "" : "s"}: ${source.calendars.join(", ")}` : provider.name;
  return `<article class="card cal-source" data-calendar="${esc(source.id)}">
    <div class="card-top"><h3>${esc(source.name)}</h3>${state}</div>
    <p class="muted">${esc(what)}${source.username ? ` · ${esc(source.username)}` : ""}</p>
    <p>${source.last_error ? esc(explain(new Error(source.last_error))) : `${esc(source.event_count)} event${source.event_count === 1 ? "" : "s"} in the coming month · synced ${esc(when(source.last_synced_at) || "not yet")}`}</p>
    <div class="button-row tight">${btn("Sync now", `cal-sync:${source.id}`)}${
      source.kind === "google_api" || source.kind === "microsoft_api" ? btn("Sign in again", `cal-sign-in:${source.kind === "google_api" ? "google" : "microsoft"}`) : ""
    }${btn("Disconnect", `cal-remove:${source.id}`, "danger")}</div>
  </article>`;
}

function calPreferencesForm(prefs) {
  const share = CAL_SHARE.map(
    ([value, label, about]) => `<label class="cal-choice"><input type="radio" name="share" value="${value}" ${prefs.share === value ? "checked" : ""}><span><strong>${esc(label)}.</strong> ${esc(about)}</span></label>`
  ).join("");
  return `<form id="cal-preferences" class="cal-preferences">
    <fieldset><legend>What your colleagues may see</legend>${share}</fieldset>
    <fieldset><legend>How your day shapes their work</legend>
      <label class="cal-choice"><input type="checkbox" name="hold" ${prefs.hold ? "checked" : ""}><span><strong>Hold the in-tray while you are in a meeting.</strong> Notifications wait for your next free time.</span></label>
      <label class="cal-choice"><input type="checkbox" name="prep" ${prefs.prep ? "checked" : ""}><span><strong>Suggest prep for meetings.</strong> The Chief offers a brief before a meeting; nothing starts until you press Start.</span></label>
    </fieldset>
    <div class="button-row"><button class="primary">Save these choices</button></div>
  </form>`;
}

async function renderCalendars(content, returned) {
  calSendZone();
  const data = await api("/calendars");
  Cal.apps = data.apps || {};
  // Back from Google or Microsoft: say how it went once, then forget it.
  if (returned) {
    const [said, bad] = CAL_RETURN[returned] || [explain(new Error(returned)), true];
    toast(said, bad);
    history.replaceState(null, "", "#calendars");
    Office.param = "";
  }
  Cal.sources = data.sources || [];
  Cal.preferences = data.preferences || { share: "titles", hold: true, prep: true };
  const connected = Cal.sources.length
    ? `<div class="cal-grid">${Cal.sources.map(calSourceCard).join("")}</div>`
    : `<p class="muted">No calendar connected yet. Choose yours below.</p>`;
  const hasMac = Cal.sources.some((source) => source.kind === "mac");
  const mac = hasMac
    ? ""
    : `<div class="cal-mac"><div><strong>Use the calendars on this Mac</strong><span>macOS asks you in its own dialog. No password: every calendar in your Calendar app comes in, iCloud and any Google or Exchange account you added there.</span></div>${btn("Use this Mac's calendars", "cal-mac", "primary")}</div>`;
  const signIns = Object.entries(CAL_SIGN_IN)
    .map(([id, p]) => `<div class="cal-sign-in"><div><strong>${esc(p.button)}</strong><span>${
      Cal.apps[id] ? `${esc(p.name)}'s own page asks you to allow reading your calendars and the changes you make here. Nothing to copy.` : `Once: make a sign-in for your office at ${esc(p.name)} (about five minutes, guided). Then it is one click.`
    }</span></div><div class="button-row tight">${Cal.apps[id] ? btn("Set-up", `cal-app:${id}`) + btn(p.button, `cal-sign-in:${id}`, "primary") : btn("Set it up", `cal-app:${id}`, "primary")}</div></div>`)
    .join("");
  const add = Object.entries(CAL_PROVIDERS)
    .filter(([id]) => id !== "mac")
    .map(([id, p]) => `<button type="button" class="cal-add" data-action="cal-add:${id}"><strong>${esc(p.name)}</strong><span>${esc(p.kind === "caldav" ? "Account and app password" : p.either ? "A link or an account" : "A private calendar link")}</span></button>`)
    .join("");
  content.innerHTML =
    heading(
      "OFFICE SETTINGS · CALENDARS",
      "Your calendars",
      "The office reads your calendars on this computer, so your colleagues know when you are free and when to hold things. Only you change them, from the Calendar screen."
    ) +
    `<section class="cal-section" aria-label="Connected calendars"><h2>Connected</h2>${connected}</section>` +
    `<section class="cal-section" aria-label="Add a calendar"><h2>Add a calendar</h2>${mac}<p class="muted cal-or">${hasMac ? "Calendars not on this Mac:" : "Or sign in to one:"}</p><div class="cal-sign-ins">${signIns}</div><details class="cal-more"><summary>Or paste a calendar link or account password</summary><div class="cal-add-grid">${add}</div></details></section>` +
    `<section class="cal-section" aria-label="Calendar choices"><h2>Your choices</h2>${calPreferencesForm(Cal.preferences)}</section>`;
}

function calConnectForm(id) {
  const provider = CAL_PROVIDERS[id] || CAL_PROVIDERS.other;
  const link =
    field("Calendar link", "cal-url", { required: false, attrs: 'maxlength="4096" placeholder="https://… or webcal://…" autocomplete="off"' });
  const account =
    (provider.server || provider.either ? field("CalDAV address", "cal-server", { required: false, attrs: 'maxlength="2048" placeholder="https://…"' }) : "") +
    field(id === "icloud" ? "Apple ID email" : "User name", "cal-user", { required: false, attrs: 'maxlength="255" autocomplete="off"' }) +
    field("App-specific password", "cal-password", { type: "password", required: false, attrs: 'maxlength="1024" autocomplete="off"' });
  const body = provider.either
    ? `${link}<p class="muted">Or, instead of a link, a CalDAV account:</p>${account}`
    : provider.kind === "ics"
      ? link
      : account;
  return `<input type="hidden" name="provider" value="${esc(id)}">
    <p class="cal-help">${esc(provider.help)}</p>
    ${field("Name it", "cal-name", { required: false, value: id === "google" || id === "outlook" ? "Work" : id === "icloud" ? "Home" : "", attrs: 'maxlength="255"' })}
    ${body}
    <p class="muted">Kept sealed on this computer and never shown again. Read only: nothing is written to your calendar.</p>`;
}

Office.forms = Object.assign(Office.forms || {}, {
  "cal-connect": async (form) => {
    const data = new FormData(form);
    const provider = String(data.get("provider") || "other");
    const url = String(data.get("cal-url") || "").trim();
    const kind = (CAL_PROVIDERS[provider] || {}).either ? (url ? "ics" : "caldav") : (CAL_PROVIDERS[provider] || CAL_PROVIDERS.other).kind;
    const body = {
      kind,
      provider,
      name: String(data.get("cal-name") || "").trim(),
      url,
      username: String(data.get("cal-user") || "").trim(),
      password: String(data.get("cal-password") || ""),
      server_url: String(data.get("cal-server") || "").trim(),
    };
    const button = form.querySelector("button.primary");
    if (button) {
      button.disabled = true;
      button.textContent = "Reading your calendar…";
    }
    const added = await attempt(() => api("/calendars", { method: "POST", body }), "Connected. Your day is on the floor.");
    if (button) {
      button.disabled = false;
      button.textContent = "Connect";
    }
    if (added === undefined) return;
    closeModal();
    rerender();
  },
  "cal-preferences": async (form) => {
    const data = new FormData(form);
    const body = { share: String(data.get("share") || "titles"), hold: data.get("hold") === "on", prep: data.get("prep") === "on" };
    if ((await attempt(() => api("/calendars/preferences", { method: "PUT", body }), "Saved.")) !== undefined) rerender();
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "cal-app": async (form) => {
    const data = new FormData(form);
    const id = String(data.get("provider") || "");
    const body = { client_id: String(data.get("client_id") || "").trim(), client_secret: String(data.get("client_secret") || "").trim() };
    if ((await attempt(() => api(`/calendars/apps/${id}`, { method: "PUT", body }), "Saved. Now sign in.")) === undefined) return;
    closeModal();
    rerender();
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "cal-app": (id) => {
    const p = CAL_SIGN_IN[id];
    if (!p) return;
    formModal(
      `Make a ${p.name} sign-in for your office`,
      "cal-app",
      `<input type="hidden" name="provider" value="${esc(id)}">
       <ol class="cal-steps">${p.steps.map((step) => `<li>${esc(step)}</li>`).join("")}</ol>
       <label class="field" for="cal-redirect">The address to register</label>
       <div class="cal-copy"><input id="cal-redirect" readonly value="${esc(calRedirect())}">${btn("Copy", "cal-copy-redirect")}</div>
       ${field(id === "google" ? "Client ID" : "Application (client) ID", "client_id", { attrs: 'maxlength="512" autocomplete="off"' })}
       ${p.secret ? field("Client secret", "client_secret", { type: "password", attrs: 'maxlength="512" autocomplete="off"' }) : ""}
       <p class="muted">Kept sealed on this computer. It lets the office ask ${esc(p.name)} for your consent; it is not your password.</p>`,
      "Save"
    );
  },
  "cal-copy-redirect": async () => {
    try {
      await navigator.clipboard.writeText(calRedirect());
      toast("Copied.");
    } catch (error) {
      $("#cal-redirect")?.select();
      toast("Select it and copy.", true);
    }
  },
  "cal-sign-in": async (id) => {
    const started = await attempt(() => api(`/calendars/oauth/${id}/start`, { method: "POST", body: { origin: location.origin } }));
    if (started?.url) location.href = started.url;
  },
  "cal-mac": async () => {
    const done = await attempt(() => api("/calendars/mac", { method: "POST" }), "Look for the macOS dialog and press Allow.");
    if (done !== undefined) rerender();
  },
  "cal-add": (id) => {
    const provider = CAL_PROVIDERS[id] || CAL_PROVIDERS.other;
    formModal(`Connect ${provider.name}`, "cal-connect", calConnectForm(id), "Connect");
  },
  "cal-sync": async (id) => {
    const synced = await attempt(() => api(`/calendars/${id}/sync`, { method: "POST" }));
    if (synced === undefined) return;
    toast(synced.last_error ? explain(new Error(synced.last_error)) : "Synced.", Boolean(synced.last_error));
    rerender();
  },
  "cal-remove": async (id) => {
    if (!window.confirm("Disconnect this calendar? Its events leave the office and its link or password is deleted.")) return;
    if ((await attempt(() => api(`/calendars/${id}`, { method: "DELETE" }), "Disconnected.")) !== undefined) rerender();
  },
});

// Redrawn while it waits on macOS or the helper, so the answer shows without a reload.
Office.screens.calendars = {
  title: "Calendars",
  nav: "office",
  render: renderCalendars,
  refresh: 4000,
  quiet: () => !Cal.sources.some((source) => source.kind === "mac" && calMacState(source)),
};

// ---- Your day on the floor ----

function calClock(value) {
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// One way of writing times everywhere a calendar is drawn: the
// owner's own 12- or 24-hour clock, short enough to fit a small event.
const CAL_12H = (() => {
  try {
    return Boolean(new Intl.DateTimeFormat([], { hour: "numeric" }).resolvedOptions().hour12);
  } catch (error) {
    return false;
  }
})();

function calShort(value, suffix = true) {
  const at = new Date(value);
  const h = at.getHours();
  const m = at.getMinutes();
  if (!CAL_12H) return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
  const hour = h % 12 || 12;
  return `${hour}${m ? `:${String(m).padStart(2, "0")}` : ""}${suffix ? (h < 12 ? "am" : "pm") : ""}`;
}

// "9–10am", "11:15am–12:30pm", "09:00–10:00".
function calSpan(start, end) {
  const a = new Date(start);
  const b = new Date(end);
  const sameHalf = CAL_12H && a.getHours() < 12 === b.getHours() < 12;
  return `${calShort(a, !sameHalf)}–${calShort(b)}`;
}

function calHourLabel(hour) {
  if (!CAL_12H) return `${String(hour).padStart(2, "0")}:00`;
  return `${hour % 12 || 12}${hour < 12 ? "am" : "pm"}`;
}

function calColour(event) {
  return /^#[0-9a-f]{6}$/i.test(event?.color || "") ? event.color : "#13233a";
}

// Events that overlap share the width; events that do not keep all of it
// Items are {start, end} in any unit; each gets `lane`, `lanes`
// (how many its group needs) and `span` (lanes it may widen into, to the right).
function calLanes(items) {
  const sorted = [...items].sort((a, b) => a.start - b.start || b.end - a.end);
  let group = [];
  let groupEnd = -Infinity;
  const close = () => {
    const lanes = [];
    group.forEach((item) => {
      let lane = lanes.findIndex((end) => end <= item.start);
      if (lane === -1) {
        lane = lanes.length;
        lanes.push(0);
      }
      lanes[lane] = item.end;
      item.lane = lane;
    });
    group.forEach((item) => {
      item.lanes = lanes.length;
      // Widen over the lanes to its right that nothing overlapping uses.
      let span = 1;
      while (
        item.lane + span < lanes.length &&
        !group.some((other) => other.lane === item.lane + span && other.start < item.end && other.end > item.start)
      ) {
        span += 1;
      }
      item.span = span;
    });
  };
  sorted.forEach((item) => {
    if (group.length && item.start >= groupEnd) {
      close();
      group = [];
      groupEnd = -Infinity;
    }
    group.push(item);
    groupEnd = Math.max(groupEnd, item.end);
  });
  if (group.length) close();
  return sorted;
}

// The owner's own day, by their computer's clock: midnight to midnight.
function calToday() {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  return { start, end };
}

// The owner's time zone, so colleagues speak of 10:00 as the owner's 10:00.
let calZoneSent = "";
function calSendZone() {
  let zone = "";
  try {
    zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch (error) {
    return;
  }
  if (!zone || zone === calZoneSent) return;
  calZoneSent = zone;
  api("/calendars/preferences", { method: "PUT", body: { timezone: zone } }).catch(() => {
    calZoneSent = "";
  });
}

async function calDay() {
  calSendZone();
  const { start, end } = calToday();
  return api(`/calendars/day?start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}`).catch(() => null);
}

// Free, in a meeting until a time, or out: the owner's place on the floor.
function calStatusLine(status) {
  if (!status) return "";
  if (status.state === "busy") return `In a meeting until ${calShort(status.until)}`;
  if (status.state === "out") return `Out today: ${status.title || "busy"}`;
  return status.next ? `Free · next: ${status.next.title} at ${calShort(status.next.starts_at)}` : "Free for the rest of the day";
}

const FLOOR_LANE_PX = 40;

function floorDay(day) {
  if (!day || !day.connected) {
    return `<a class="floor-day-empty" href="#calendars"><strong>Connect your calendar</strong><span>Google, iCloud, Outlook and others, read only, so your colleagues work around your day.</span></a>`;
  }
  const { start: midnight } = calToday();
  const hour = (value) => (new Date(value) - midnight) / 3600000;
  const timed = (day.events || []).filter((e) => !e.all_day);
  const allDay = (day.events || []).filter((e) => e.all_day);
  const from = Math.max(0, Math.min(8, ...timed.map((e) => Math.floor(Math.max(0, hour(e.starts_at))))));
  const to = Math.min(24, Math.max(18, ...timed.map((e) => Math.ceil(Math.min(24, hour(e.ends_at))))));
  const span = to - from;
  const at = (h) => `${(((Math.min(Math.max(h, from), to) - from) / span) * 100).toFixed(2)}%`;
  const width = (a, b) => `${(((Math.min(b, to) - Math.max(a, from)) / span) * 100).toFixed(2)}%`;
  const now = hour(new Date());
  // Free time from now on, between busy events, worth naming at 45 minutes.
  const busy = timed.filter((e) => e.busy).map((e) => [hour(e.starts_at), hour(e.ends_at)]).sort((a, b) => a[0] - b[0]);
  const gaps = [];
  let cursor = Math.max(from, now);
  for (const [a, b] of busy) {
    if (a - cursor >= 0.75) gaps.push([cursor, a]);
    cursor = Math.max(cursor, b);
  }
  if (to - cursor >= 0.75) gaps.push([cursor, to]);
  const hours = Array.from({ length: span }, (_, i) => `<span>${esc(calHourLabel(from + i))}</span>`).join("");
  // Overlapping events take rows of their own, so none hides another.
  const placed = calLanes(
    timed
      .filter((e) => hour(e.ends_at) > from && hour(e.starts_at) < to)
      .map((e) => ({ e, start: hour(e.starts_at), end: Math.max(hour(e.ends_at), hour(e.starts_at) + 0.25) }))
  );
  const rows = Math.max(1, ...placed.map((p) => p.lane + 1));
  const blocks = placed
    .map(({ e, start, end, lane }) => {
      const short = end - start < 0.75 ? " is-short" : "";
      const passed = new Date(e.ends_at) < new Date() ? " is-past" : "";
      return `<div class="floor-day-event ${e.busy ? "is-busy" : "is-free"}${short}${passed}" style="--cal:${calColour(e)};left:${at(start)};width:${width(start, end)};top:${lane * FLOOR_LANE_PX}px" title="${esc(e.title)} · ${esc(calSpan(e.starts_at, e.ends_at))}${e.busy ? "" : " · shown as free"}"><strong>${esc(e.title)}</strong><small>${esc(calSpan(e.starts_at, e.ends_at))}</small></div>`;
    })
    .join("");
  const free = gaps
    .map(([a, b]) => {
      const length = b - a;
      const label = length >= 1 ? `${Math.floor(length)}${length % 1 >= 0.5 ? "½" : ""} h free` : `${Math.round(length * 60)} min free`;
      return `<div class="floor-day-gap" style="left:${at(a)};width:${width(a, b)}"><span>${esc(label)}</span></div>`;
    })
    .join("");
  const line = now > from && now < to ? `<div class="floor-day-now" style="left:${at(now)}" aria-label="Now, ${esc(calShort(new Date()))}"></div>` : "";
  const chips = allDay.length ? `<div class="floor-day-allday">${allDay.map((e) => `<span class="floor-day-chip" style="--cal:${calColour(e)}">${esc(e.title)}</span>`).join("")}</div>` : "";
  const today = new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  return `<section class="floor-day" aria-label="Your day">
    <div class="floor-day-head"><h2>Your day · ${esc(today)}</h2><span class="floor-day-status ${day.status?.state === "busy" ? "is-busy" : ""}">${esc(calStatusLine(day.status))}</span><a href="#calendar/day">Open calendar</a></div>
    ${chips}
    <div class="floor-day-hours" style="grid-template-columns: repeat(${span}, minmax(0, 1fr))">${hours}</div>
    <div class="floor-day-track" style="height:${rows * FLOOR_LANE_PX}px">${free}${blocks}${line}</div>
    ${timed.length ? "" : `<p class="muted floor-day-none">Nothing on your calendar today.</p>`}
    ${calPrepOffer(day.prep)}
  </section>`;
}

// The Chief's offer of a brief before a meeting: paid work, so it
// starts only when the owner presses Start, capped at what it says.
function calPrepOffer(prep) {
  if (!prep) return "";
  return `<div class="floor-day-prep" data-prep="${esc(prep.id)}"><div><span class="floor-room-label">The Chief suggests</span>
    <p><strong>A prep brief for “${esc(prep.title)}” at ${esc(calClock(prep.starts_at))}:</strong> where the related work stands, what is waiting on you, what to raise. From what the office already knows.</p></div>
    <div class="button-row tight">${btn("Not today", `cal-prep-dismiss:${prep.id}`)}${btn(`Start, up to ${money(prep.cost)}`, `cal-prep-start:${prep.id}:${calClock(prep.starts_at)}`, "primary")}</div></div>`;
}

Office.actions = Object.assign(Office.actions || {}, {
  "cal-prep-start": async (arg) => {
    const [id, ...rest] = String(arg).split(":");
    const started = await attempt(
      () => api(`/calendars/prep/${id}/start`, { method: "POST", body: { when: rest.join(":") } }),
      "Started. The Chief is writing your brief."
    );
    if (started?.run_id) location.hash = `task/${started.run_id}`;
  },
  "cal-prep-dismiss": async (id) => {
    if ((await attempt(() => api(`/calendars/prep/${id}/dismiss`, { method: "POST" }), "Not this one.")) !== undefined) rerender();
  },
});

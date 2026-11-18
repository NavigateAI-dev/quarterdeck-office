"use strict";

// Watching pages and topics: the office checks a page or a
// search on its own after the owner's press, for free unless it changed; then
// one small model call decides whether to tell the owner, within a daily cap.

const Watching = { channels: null };
const WATCH_EVERY = { 60: "Every hour", 240: "Every 4 hours", 1440: "Once a day" };

function watchWhen(iso) {
  if (!iso) return "";
  const at = new Date(iso);
  const today = new Date().toDateString() === at.toDateString();
  return today ? calShort(at) : at.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) + " " + calShort(at);
}

function watchRow(w) {
  const state =
    w.status === "paused" ? `<span class="tag">Paused</span>`
      : w.status === "ended" || w.status === "stopped" ? `<span class="tag">Stopped</span>`
      : "";
  const said = w.last_said
    ? `<p class="watch-said">“${esc(roomShort(w.last_said, 160))}” <span class="muted">${esc(watchWhen(w.last_said_at))}</span></p>`
    : `<p class="idea-meta">${w.last_checked_at ? `Nothing to tell yet · checked ${esc(watchWhen(w.last_checked_at))}` : "First check soon"}${w.status === "active" && w.next_check_at ? ` · next ${esc(watchWhen(w.next_check_at))}` : ""}</p>`;
  const why = w.stopped_reason ? `<p class="idea-meta">${esc(w.stopped_reason)}</p>` : w.last_error ? `<p class="idea-meta">Last check could not read it (${esc(w.last_error)})</p>` : "";
  return `<article class="watch-row" data-watch="${esc(w.id)}"><button type="button" class="watch-title" data-action="watch-open:${esc(w.id)}">${esc(w.title)}</button> ${state}${said}${why}</article>`;
}

function watchingTile(data) {
  const list = Array.isArray(data.desk?.watches) ? data.desk.watches : [];
  Watching.list = list;
  const rows = list.length
    ? list.map(watchRow).join("")
    : `<p class="idea-empty">Have the office check a page or a topic for you, and tell you when it changes: a price, a filing, job posts, rain before 15:00.</p>`;
  return `<section class="floor-sheet watch-sheet" aria-label="Watching"><div class="idea-head"><h2>Watching</h2>${btn("＋ Watch", "watch-new", "idea-sort-btn")}</div>${rows}</section>`;
}

async function watchChannels() {
  if (Array.isArray(Watching.channels)) return Watching.channels;
  const data = await api("/channels").catch((error) => {
    console.error("Could not read the chat channels", error);
    return null;
  });
  Watching.channels = (data?.destinations || []).filter((d) => d.enabled && d.name !== "Chat thread");
  return Watching.channels;
}

async function watchNew() {
  const channels = await watchChannels();
  const where = [["", "Only here, on the floor"], ...channels.map((d) => [d.id, d.name])];
  formModal(
    "Watch a page or a topic",
    "watch-form",
    `<fieldset class="watch-kind"><legend>What to watch</legend>
       <label><input type="radio" name="kind" value="page" checked> A page (its address)</label>
       <label><input type="radio" name="kind" value="search"> A topic (search words)</label></fieldset>
     ${field("Address or search words", "watch-target", { attrs: 'maxlength="2000" placeholder="https://… or: new AI regulation Netherlands"' })}
     ${field("Tell me when (optional)", "watch-condition", { required: false, attrs: 'maxlength="1000" placeholder="e.g. the price drops below €50; it will rain in Lyon before 15:00"', hint: "Leave empty to hear of any real change." })}
     ${field("Name (optional)", "watch-title", { required: false, attrs: 'maxlength="200"' })}
     <div class="watch-grid">
       ${field("How often", "watch-every", { type: "select", value: "240", choices: Object.entries(WATCH_EVERY) })}
       ${field("Until (optional)", "watch-ends", { type: "date", required: false })}
       ${field("Tell me in", "watch-where", { type: "select", value: "", required: false, choices: where })}
       ${field("At most a day", "watch-cap", { type: "number", value: "0.05", attrs: 'min="0.01" max="1" step="0.01"' })}
     </div>
     <p class="watch-consent">Each check reads it for free, with the office's own reader and the search it already uses. Only when it changed is one small model call made, within the amount above per day. After you press Start it runs on its own until you pause it.</p>`,
    "Start watching"
  );
}

function watchOpen(id) {
  const w = (Watching.list || []).find((x) => x.id === id);
  if (!w) return;
  const rows = [
    ["Watching", w.kind === "page" ? w.target : `search: ${w.target}`],
    ["Tell me when", w.condition || "any real change"],
    ["How often", WATCH_EVERY[w.every_minutes] || `${w.every_minutes} min`],
    ["Tell me in", w.post_to?.name || "Only here, on the floor"],
    ["Today", `${money(w.spent_today_usd || 0)} of ${money(w.daily_cap_usd)}`],
    w.ends_on ? ["Until", ideaDayName(w.ends_on)] : null,
  ]
    .filter(Boolean)
    .map(([label, value]) => `<div class="detail-line"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`)
    .join("");
  const said = w.last_said ? `<p class="idea-quote">${esc(w.last_said)}</p>` : "";
  const toggle = w.status === "active" ? btn("Pause", `watch-do:${id}:pause`) : btn("Start again", `watch-do:${id}:resume`);
  const now = w.status === "active" ? btn("Check now", `watch-do:${id}:check`) : "";
  modal(w.title, said + rows, `<div class="button-row">${btn("Delete", `watch-do:${id}:delete`, "danger")}${toggle}${now}${btn("Close", "close", "primary")}</div>`);
}

Office.forms = Object.assign(Office.forms || {}, {
  "watch-form": async (form) => {
    const data = new FormData(form);
    const body = {
      kind: String(data.get("kind") || "page"),
      target: String(data.get("watch-target") || "").trim(),
      condition: String(data.get("watch-condition") || "").trim(),
      title: String(data.get("watch-title") || "").trim(),
      every_minutes: Number(data.get("watch-every") || 240),
      ends_on: String(data.get("watch-ends") || "") || null,
      post_to_id: String(data.get("watch-where") || "") || null,
      daily_cap_usd: Number(data.get("watch-cap") || 0.05),
      confirm: true,
    };
    if (!body.target) return toast("Say what to watch first.", true);
    if ((await attempt(() => api("/watches", { method: "POST", body }), "Watching. The first check runs within a minute.")) !== undefined) {
      closeModal();
      rerender();
    }
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "watch-new": () => watchNew(),
  "watch-open": (id) => watchOpen(id),
  "watch-do": async (arg) => {
    const [id, action] = String(arg).split(":");
    if (action === "delete" && !window.confirm("Delete this watch? It stops checking; what it cost stays in the month.")) return;
    const said = { pause: "Paused.", resume: "Watching again.", check: "Checked.", delete: "Deleted." }[action];
    if ((await attempt(() => api(`/watches/${id}/${action}`, { method: "POST" }), said)) !== undefined) {
      closeModal();
      rerender();
    }
  },
});

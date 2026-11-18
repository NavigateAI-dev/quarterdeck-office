"use strict";

// The owner's wall on the floor: widgets added, arranged, folded
// and taken off. Taking one off keeps what it knew unless the owner cleans it
// out. Whether agents read a widget is switched here too.

const Wall = { list: null, arranging: false, drag: "" };

// What the floor looked like before the wall: used if the wall cannot be read.
const WALL_FALLBACK = [
  { kind: "calendar", title: "Your day", size: "wide", default_size: "wide", placed: true },
  { kind: "ideas", title: "Ideas", size: "wide", default_size: "wide", placed: true },
  { kind: "watching", title: "Watching", size: "small", default_size: "small", placed: true },
  { kind: "delivered", title: "Delivered", size: "small", default_size: "small", placed: true },
  { kind: "parked", title: "Parked for later", size: "small", default_size: "small", placed: true },
  { kind: "month", title: "This month", size: "small", default_size: "small", placed: true },
];

async function wallLoad() {
  const data = await api("/widgets").catch((error) => {
    console.error("Could not read the wall", error);
    return null;
  });
  Wall.list = Array.isArray(data?.widgets) ? data.widgets : WALL_FALLBACK;
  return Wall.list;
}

function wallPlaced() {
  return (Wall.list || WALL_FALLBACK).filter((w) => w.placed);
}

function wallHas(kind) {
  return wallPlaced().some((w) => w.kind === kind);
}

// Each widget's tile, from what the floor already fetched.
const WIDGET_TILES = {
  calendar: (data) => (typeof floorDay === "function" ? floorDay(data.day) : ""),
  delivered: (data) => {
    const delivered = data.desk?.delivered || [];
    return `<section class="floor-sheet" aria-label="Delivered"><h2>Delivered — have a look</h2><div class="desk-rows">${
      delivered.length ? delivered.map(deskDelivered).join("") : `<p class="muted">What your office finishes this week shows here.</p>`
    }</div></section>`;
  },
  ideas: (data, widget) => (typeof ideasTile === "function" ? ideasTile(data, widget) : ""),
  watching: (data) => (typeof watchingTile === "function" ? watchingTile(data) : ""),
  parked: (data) => {
    const returning = data.desk?.parked?.returning || [];
    const rows = returning.length
      ? returning.map(deskReturning).join("")
      : `<p class="muted">Say “next week” in a discussion and it waits here until then.</p>`;
    return `<section class="floor-sheet" aria-label="Parked for later"><h2>Parked for later</h2><div class="desk-rows">${rows}</div></section>`;
  },
  month: (data) => {
    const usage = data.overview?.usage_month || {};
    const counts = data.overview?.counts || {};
    return `<section class="floor-sheet floor-numbers" aria-label="This month"><h2>This month</h2>
      <div class="floor-figure"><strong>${money(usage.cost_usd || 0)}</strong><span>${usage.cost_complete ? "spent" : "spent (partly unpriced)"}</span></div>
      <p>${counts.in_motion || 0} working · ${counts.needs_owner || 0} waiting for you · ${counts.completed_week || 0} finished this week</p>
      <a href="#settings">Office status</a></section>`;
  },
};

function wallControls(widget) {
  const sizes = [
    ["wide", "Wide"],
    ["small", "Small"],
    ["folded", "Folded"],
  ]
    .map(([size, label]) => btn(label, `widget-size:${widget.kind}:${size}`, widget.size === size ? "is-on" : "", `aria-pressed="${widget.size === size}"`))
    .join("");
  const reads = widget.readable
    ? btn(widget.agents_read ? "Agents read this" : "Agents don't read this", `widget-read:${widget.kind}`, widget.agents_read ? "is-on" : "", `aria-pressed="${Boolean(widget.agents_read)}"`)
    : "";
  return `<div class="widget-controls" role="group" aria-label="Arrange ${esc(widget.title)}">
    <span class="widget-grip" aria-hidden="true">⠿</span>
    ${btn("↑", `widget-move:${widget.kind}:-1`, "", `aria-label="Move ${esc(widget.title)} up"`)}${btn("↓", `widget-move:${widget.kind}:1`, "", `aria-label="Move ${esc(widget.title)} down"`)}
    <span class="widget-sizes">${sizes}</span>${reads}
    ${btn("× Take off", `widget-off:${widget.kind}`, "widget-off")}
  </div>`;
}

function wallTile(widget, data) {
  const folded = widget.size === "folded";
  const body = folded
    ? `<button type="button" class="widget-folded" data-action="widget-size:${esc(widget.kind)}:${esc(widget.default_size || "small")}">${esc(widget.title)}<small>Open</small></button>`
    : WIDGET_TILES[widget.kind]?.(data, widget) || "";
  const drag = Wall.arranging ? ` draggable="true"` : "";
  return `<div class="widget widget-${esc(widget.size)}${Wall.arranging ? " is-arranging" : ""}" data-widget="${esc(widget.kind)}"${drag}>${Wall.arranging ? wallControls(widget) : ""}${body}</div>`;
}

// The wide tiles across the floor, and the small and folded ones beside it.
function wallRender(data) {
  const placed = wallPlaced();
  const wide = placed.filter((w) => w.size === "wide").map((w) => wallTile(w, data)).join("");
  const side = placed.filter((w) => w.size !== "wide").map((w) => wallTile(w, data)).join("");
  const add = `<div class="wall-tools">${btn("＋ Add a widget", "widget-shelf", "wall-add")}${btn(Wall.arranging ? "Done arranging" : "Arrange", "widget-arrange", Wall.arranging ? "primary" : "")}</div>`;
  return { wide: wide ? `<div class="wall-wide">${wide}</div>` : "", side: side + add };
}

function wallShelf() {
  const rows = (Wall.list || [])
    .map((w) => {
      const kept = !w.placed && w.held?.length ? `<small>Still knows: ${esc(w.held.join("; "))}</small>` : "";
      const action = w.placed ? `<span class="tag">On your wall</span>` : btn("Add", `widget-add:${w.kind}`, "primary");
      return `<li class="shelf-row"><span><strong>${esc(w.title)}</strong><small>${esc(w.blurb || "")}</small>${kept}</span>${action}</li>`;
    })
    .join("");
  modal("Add a widget", `<p class="muted">Put a widget on your wall, then arrange it or switch whether agents read it.</p><ul class="shelf">${rows}</ul>`, `<div class="button-row">${btn("Close", "close")}</div>`);
}

function wallOffDialog(widget) {
  const held = widget.held || [];
  const body = held.length
    ? `<p>It holds:</p><ul>${held.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>
       <p class="muted"><strong>Keep what it knew</strong>: put it back later and it is as it was; agents stop reading it meanwhile. <strong>Clean it out</strong>: all of the above is deleted and its sign-ins are disconnected.</p>`
    : `<p class="muted">It keeps nothing of its own; the office's records stay. Add it back any time.</p>`;
  const buttons = held.length && widget.cleanable
    ? `${btn("Cancel", "close")}${btn("Clean it out", `widget-clean:${widget.kind}`, "danger")}${btn("Keep what it knew", `widget-keep:${widget.kind}`, "primary")}`
    : `${btn("Cancel", "close")}${btn("Take it off", `widget-keep:${widget.kind}`, "primary")}`;
  modal(`Take “${widget.title}” off your wall?`, body, `<div class="button-row">${buttons}</div>`);
}

function wallFind(kind) {
  return (Wall.list || []).find((w) => w.kind === kind);
}

async function wallOrder(kinds) {
  const data = await attempt(() => api("/widgets/order", { method: "PUT", body: { kinds } }));
  if (Array.isArray(data?.widgets)) Wall.list = data.widgets;
  rerender();
}

Office.actions = Object.assign(Office.actions || {}, {
  "widget-arrange": () => {
    Wall.arranging = !Wall.arranging;
    rerender();
  },
  "widget-shelf": async () => {
    await wallLoad();
    wallShelf();
  },
  "widget-add": async (kind) => {
    const placed = await attempt(() => api(`/widgets/${kind}/place`, { method: "POST" }));
    if (!placed) return;
    closeModal();
    toast(`${placed.title} is on your wall.`);
    rerender();
  },
  "widget-off": (kind) => {
    const widget = wallFind(kind);
    if (widget) wallOffDialog(widget);
  },
  "widget-keep": async (kind) => {
    const off = await attempt(() => api(`/widgets/${kind}/remove`, { method: "POST", body: { clean: false } }));
    if (!off) return;
    closeModal();
    toast(`${off.title} is off your wall. ＋ Add a widget brings it back.`);
    rerender();
  },
  "widget-clean": async (kind) => {
    const widget = wallFind(kind);
    if (!window.confirm(`Delete everything “${widget?.title || kind}” knew? This cannot be undone.`)) return;
    const off = await attempt(() => api(`/widgets/${kind}/remove`, { method: "POST", body: { clean: true } }));
    if (!off) return;
    closeModal();
    toast(`${off.title} is off your wall and cleaned out.`);
    rerender();
  },
  "widget-size": async (arg) => {
    const [kind, size] = String(arg).split(":");
    if (await attempt(() => api(`/widgets/${kind}`, { method: "PATCH", body: { size } }))) rerender();
  },
  "widget-read": async (kind) => {
    const widget = wallFind(kind);
    const changed = await attempt(() => api(`/widgets/${kind}`, { method: "PATCH", body: { agents_read: !widget?.agents_read } }));
    if (!changed) return;
    toast(changed.agents_read ? `Agents now read ${changed.title} with their work.` : `Agents no longer read ${changed.title}.`);
    rerender();
  },
  "widget-move": (arg) => {
    const [kind, step] = String(arg).split(":");
    const kinds = wallPlaced().map((w) => w.kind);
    const at = kinds.indexOf(kind);
    const to = at + Number(step);
    if (at < 0 || to < 0 || to >= kinds.length) return;
    [kinds[at], kinds[to]] = [kinds[to], kinds[at]];
    return wallOrder(kinds);
  },
});

// Dragging a tile onto another puts it before that one (arranging only).
document.addEventListener("dragstart", (event) => {
  const tile = event.target?.closest?.(".widget.is-arranging");
  if (!tile) return;
  Wall.drag = tile.dataset.widget || "";
  event.dataTransfer?.setData("text/plain", Wall.drag);
});
document.addEventListener("dragover", (event) => {
  if (Wall.drag && event.target?.closest?.(".widget.is-arranging")) event.preventDefault();
});
document.addEventListener("dragend", () => {
  Wall.drag = "";
});
document.addEventListener("drop", (event) => {
  const target = event.target?.closest?.(".widget.is-arranging");
  const moving = Wall.drag;
  Wall.drag = "";
  if (!target || !moving || target.dataset.widget === moving) return;
  event.preventDefault();
  const kinds = wallPlaced().map((w) => w.kind).filter((kind) => kind !== moving);
  const at = kinds.indexOf(target.dataset.widget);
  kinds.splice(at < 0 ? kinds.length : at, 0, moving);
  wallOrder(kinds);
});

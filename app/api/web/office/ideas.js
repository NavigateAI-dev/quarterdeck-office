"use strict";

// Ideas: a thought kept in seconds on the floor, planned
// for a day, and back under Today when its day comes. Keeping one calls no
// model; Start is the only button here that spends, and only on a press.

const Ideas = { board: null, draft: "", pick: "", view: "kind" };
const IDEAS_SHOWN = 6;
const UNSORTED_SHOWN = 4;

// The owner's daily three: each kind with its question.
const IDEA_KINDS = [
  { key: "friction", name: "Friction", mark: "⚡", ask: "What felt tedious, repetitive or slow today?" },
  { key: "curiosity", name: "Curiosity", mark: "✦", ask: "What technology, product or business model made you genuinely curious?" },
  { key: "opportunity", name: "Opportunity", mark: "$", ask: "What problem did you see that someone would pay to solve?" },
];
const IDEA_KIND = Object.fromEntries(IDEA_KINDS.map((k) => [k.key, k]));
const IDEA_PICK_KEY = "office.idea.kind";
const IDEA_VIEW_KEY = "office.ideas.view";

(function ideaPrefsLoad() {
  try {
    const pick = localStorage.getItem(IDEA_PICK_KEY) || "";
    Ideas.pick = IDEA_KIND[pick] ? pick : "";
    Ideas.view = localStorage.getItem(IDEA_VIEW_KEY) === "day" ? "day" : "kind";
  } catch (error) {
    // Private windows may refuse storage; the defaults stand.
  }
})();

function ideaPrefSave(key, value) {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch (error) {
    // Kept for this page only.
  }
}

function ideaKindTag(kind) {
  const k = IDEA_KIND[kind];
  return k ? `<span class="idea-kind-tag is-${esc(k.key)}"><span aria-hidden="true">${esc(k.mark)}</span> ${esc(k.name)}</span>` : "";
}

// The three chips above the box: the chosen kind decides where a thought lands
// and the box asks its question. Pressing the chosen one again unsorts.
function ideaKindPicker() {
  const chips = IDEA_KINDS.map((k) => {
    const on = Ideas.pick === k.key;
    return `<button type="button" class="idea-kind-chip is-${esc(k.key)}${on ? " on" : ""}" role="radio" aria-checked="${on}" data-action="idea-kind-pick:${esc(k.key)}"><span aria-hidden="true">${esc(k.mark)}</span> ${esc(k.name)}</button>`;
  }).join("");
  const ask = IDEA_KIND[Ideas.pick]?.ask || "";
  return `<div class="idea-kinds" role="radiogroup" aria-label="What kind of thought">${chips}</div><p class="idea-ask" data-idea-ask ${ask ? "" : "hidden"}>${esc(ask)}</p>`;
}

// The question is said above the box; the box itself keeps a short hint.
function ideaPlaceholder() {
  return Ideas.pick ? "Write it in a line or two…" : "A thought for later…";
}

// Today's three: one of each kind kept today, filled as they are.
function ideaThree(board) {
  const three = board?.today_three || {};
  const done = IDEA_KINDS.filter((k) => (three[k.key] || 0) > 0).length;
  const marks = IDEA_KINDS.map((k) => {
    const n = three[k.key] || 0;
    return `<span class="idea-three-mark is-${esc(k.key)}${n ? " on" : ""}" title="${esc(k.name)}: ${n ? `${n} today` : "none yet today"}"><span aria-hidden="true">${n ? "✓" : esc(k.mark)}</span><span class="visually-hidden">${esc(k.name)} ${n ? "kept" : "not yet"}</span></span>`;
  }).join("");
  return `<div class="idea-three${done === 3 ? " is-full" : ""}" aria-label="Today's three: ${done} of 3"><span class="idea-three-label">Today's three</span>${marks}<strong>${done}/3</strong></div>`;
}

function ideaDay(iso) {
  // A plain day (YYYY-MM-DD) read as local, not as UTC midnight.
  const [y, m, d] = String(iso || "").split("-").map(Number);
  return y ? new Date(y, m - 1, d) : null;
}

function ideaIso(day) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
}

function ideaBlockedAt(iso) {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? "" : at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

// The projects an idea can belong to, read once when the Plan dialog opens.
async function ideaProjects() {
  if (Array.isArray(Ideas.projects)) return Ideas.projects;
  const projects = await api("/projects").catch((error) => {
    console.error("Could not read the projects", error);
    return [];
  });
  Ideas.projects = (Array.isArray(projects) ? projects : []).filter((p) => p.status === "ACTIVE");
  return Ideas.projects;
}

function ideaDayName(iso) {
  const day = ideaDay(iso);
  return day ? day.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "short" }) : "";
}

// The days offered under "Plan for": from the owner's today, as the office has it.
function ideaChoices(todayIso) {
  const today = ideaDay(todayIso) || new Date();
  const plus = (n) => {
    const day = new Date(today);
    day.setDate(day.getDate() + n);
    return ideaIso(day);
  };
  const toMonday = ((8 - today.getDay()) % 7) || 7;
  return [
    ["Today", plus(0)],
    ["Tomorrow", plus(1)],
    ["Monday", plus(toMonday)],
    ["Next week", plus(7)],
  ];
}

// What one idea says under its words: its day, its time, its project, its source.
function ideaMeta(idea) {
  const when = {
    overdue: `<span class="idea-late">planned ${esc(ideaDayName(idea.plan_for))}</span>`,
    today: "",
    later: "",
    someday: "someday",
    new: "",
  }[idea.bucket] || "";
  const bits = [
    Ideas.view === "day" || !idea.kind ? ideaKindTag(idea.kind) : "",
    when,
    idea.blocked_at ? `⏱ ${esc(ideaBlockedAt(idea.blocked_at))}` : "",
    idea.project ? `▣ ${esc(roomShort(idea.project.name, 32))}` : "",
    idea.topic ? `from “${esc(roomShort(idea.topic.title, 32))}”` : idea.source === "slack" ? "from Slack" : idea.source === "phone" ? "from your phone" : "",
  ].filter(Boolean);
  return bits.length ? `<p class="idea-meta">${bits.join(" · ")}</p>` : "";
}

// One tap to a day for a new idea; the rest is under More…
function ideaActions(idea) {
  const more = btn("More…", `idea-plan:${idea.id}`, "idea-more-btn");
  if (idea.bucket === "today" || idea.bucket === "overdue") {
    return btn("Start", `idea-start:${idea.id}`, "idea-go") + btn("Done ✓", `idea-done:${idea.id}`) + more;
  }
  // By kind, the wall is for noticing; planning is one press away.
  if (Ideas.view === "kind") return btn("Plan…", `idea-plan:${idea.id}`, "idea-more-btn");
  if (idea.bucket === "new") {
    const days = ideaChoices(Ideas.board?.today_is).slice(0, 3);
    return days.map(([label, iso]) => btn(esc(label), `idea-day:${idea.id}:${iso}`, "idea-chip")).join("") + more;
  }
  return more;
}

function ideaCard(idea) {
  const kind = idea.kind ? ` kind-${esc(idea.kind)}` : "";
  return `<article class="idea-card is-${esc(idea.bucket)}${kind}" data-idea="${esc(idea.id)}" data-bucket="${esc(idea.bucket)}" data-kind="${esc(idea.kind || "")}">
    <p class="idea-words">${esc(roomShort(idea.text, 220))}</p>${ideaMeta(idea)}
    <div class="idea-actions">${ideaActions(idea)}</div></article>`;
}

// An unsorted idea: one tap gives it a kind.
function ideaUnsortedCard(idea) {
  const chips = IDEA_KINDS.map((k) => btn(`<span aria-hidden="true">${esc(k.mark)}</span><span class="visually-hidden">${esc(k.name)}</span>`, `idea-kind:${idea.id}:${k.key}`, `idea-kind-chip idea-kind-mini is-${k.key}`, `title="${esc(k.name)}"`)).join("");
  return `<article class="idea-card is-unsorted" data-idea="${esc(idea.id)}" data-kind="">
    <p class="idea-words">${esc(roomShort(idea.text, 220))}</p>${ideaMeta(idea)}
    <div class="idea-actions"><span class="idea-sort-ask">Which kind?</span>${chips}${btn("More…", `idea-plan:${idea.id}`, "idea-more-btn")}</div></article>`;
}

// The wall by kind: a column each, newest first, then the unsorted.
function ideaKindWall(board) {
  const kinds = board.kinds || {};
  const counts = board.kind_counts || {};
  const columns = IDEA_KINDS.map((k) => {
    const list = kinds[k.key] || [];
    const shown = list.slice(0, IDEAS_SHOWN).map(ideaCard).join("");
    const more = (counts[k.key] || 0) > IDEAS_SHOWN ? `<p class="idea-tail">${counts[k.key] - IDEAS_SHOWN} more · ${btn("All ideas →", "ideas-all", "idea-link")}</p>` : "";
    return `<section class="idea-col idea-kind-col is-${esc(k.key)}" aria-label="${esc(k.name)}">
      <h3><span class="idea-kind-mark" aria-hidden="true">${esc(k.mark)}</span>${esc(k.name)}${counts[k.key] ? ` <span class="idea-num">${counts[k.key]}</span>` : ""}</h3>
      <p class="idea-kind-ask">${esc(k.ask)}</p>
      ${shown + more || `<p class="idea-empty">Nothing yet. ${btn("Note one", `idea-kind-pick:${k.key}`, "idea-link")}</p>`}</section>`;
  }).join("");
  const unsorted = kinds.unsorted || [];
  const lane = unsorted.length
    ? `<section class="idea-unsorted" aria-label="Unsorted"><h3>Unsorted <span class="idea-num">${counts.unsorted || unsorted.length}</span><small>Kept before kinds, or without one</small></h3>
        <div class="idea-unsorted-list">${unsorted.slice(0, UNSORTED_SHOWN).map(ideaUnsortedCard).join("")}</div>
        ${(counts.unsorted || 0) > UNSORTED_SHOWN ? `<p class="idea-tail">${counts.unsorted - UNSORTED_SHOWN} more · ${btn("All ideas →", "ideas-all", "idea-link")}</p>` : ""}</section>`
    : "";
  return `<div class="idea-cols idea-kind-cols">${columns}</div>${lane}`;
}

function ideaViewSwitch() {
  const option = (key, label) => `<button type="button" role="radio" aria-checked="${Ideas.view === key}" class="${Ideas.view === key ? "on" : ""}" data-action="ideas-view:${key}">${label}</button>`;
  return `<div class="idea-view" role="radiogroup" aria-label="Show ideas">${option("kind", "By kind")}${option("day", "By day")}</div>`;
}

// The All ideas dialog and the small tile use the same card.
const ideaRow = ideaCard;

function ideaColumn(key, title, hint, body, count) {
  return `<section class="idea-col idea-col-${key}" aria-label="${esc(title)}"><h3>${esc(title)}${count ? ` <span class="idea-num">${count}</span>` : ""}</h3>${body || `<p class="idea-empty">${esc(hint)}</p>`}</section>`;
}

// Coming up: the days ahead, each with its ideas, then someday.
function ideaComing(board) {
  const days = new Map();
  for (const idea of board.later || []) {
    if (!days.has(idea.plan_for)) days.set(idea.plan_for, []);
    days.get(idea.plan_for).push(idea);
  }
  const shown = [...days.entries()].slice(0, 5);
  const groups = shown.map(([iso, list]) => `<h4 class="idea-day-label">${esc(ideaDayName(iso))}</h4>${list.map(ideaCard).join("")}`).join("");
  const counts = board.counts || {};
  const hidden = (counts.later || 0) - shown.reduce((n, [, list]) => n + list.length, 0);
  const tail = [hidden > 0 ? `${hidden} more later` : "", counts.someday ? `${counts.someday} someday` : ""].filter(Boolean);
  return groups + (tail.length ? `<p class="idea-tail">${esc(tail.join(" · "))} · ${btn("All ideas →", "ideas-all", "idea-link")}</p>` : "");
}

// One text box for a thought, wherever it is caught: it grows as you
// write, the phone does not "correct" it, and Enter keeps it only when no
// character is still being composed (pinyin, kana), so a half-typed word is
// never kept. Shift+Enter is a new line.
const IDEA_DRAFT_KEY = "office.idea.draft";

function ideaField(id, placeholder, rows = 1) {
  return `<textarea id="${id}" name="text" class="idea-input" data-idea-input rows="${rows}" maxlength="2000"
    placeholder="${esc(placeholder || ideaPlaceholder())}" data-kind-hint="${placeholder ? "" : "1"}" autocomplete="off" autocorrect="off" autocapitalize="sentences"
    spellcheck="false" enterkeyhint="done">${esc(Ideas.draft)}</textarea>`;
}

function ideaDraftSave(text) {
  Ideas.draft = text;
  try {
    if (text) localStorage.setItem(IDEA_DRAFT_KEY, text);
    else localStorage.removeItem(IDEA_DRAFT_KEY);
  } catch (error) {
    // Private windows may refuse storage; the draft then lives in this page only.
  }
}

(function ideaDraftLoad() {
  try {
    Ideas.draft = localStorage.getItem(IDEA_DRAFT_KEY) || "";
  } catch (error) {
    Ideas.draft = "";
  }
})();

function ideaGrow(box) {
  if (!box) return;
  box.style.height = "auto";
  box.style.height = `${Math.min(box.scrollHeight, 220)}px`;
}

function ideaCapture() {
  return `${ideaKindPicker()}<form id="idea-keep" class="idea-capture" data-idea-box>
      <label class="visually-hidden" for="idea-text">A thought to keep for later</label>
      <span class="idea-bulb" aria-hidden="true">${esc(IDEA_KIND[Ideas.pick]?.mark || "💡")}</span>
      ${ideaField("idea-text", "")}
      <button class="primary">Keep</button>
    </form>
    <p class="idea-help">Kept as written. Nobody acts on it until you plan or start it. <span class="idea-keys">Press <kbd>i</kbd> anywhere · “friction: …”, “curiosity: …”, “opportunity: …” in Slack</span></p>`;
}

function ideasTile(data, widget) {
  const board = data.desk?.ideas;
  if (!board || !Array.isArray(board.today)) {
    console.error("Ideas: no board in the desk", data.desk);
    return `<section class="floor-sheet" aria-label="Ideas"><h2>Ideas</h2><p class="muted">Ideas could not be read just now.</p></section>`;
  }
  Ideas.board = board;
  const counts = board.counts || {};
  if (widget?.size === "small") {
    // Beside the floor: capture, and what is for today.
    const today = board.today.length ? board.today.map(ideaCard).join("") : `<p class="idea-empty">Nothing planned for today.</p>`;
    return `<section class="floor-sheet idea-sheet is-small" aria-label="Ideas"><h2>Ideas</h2>${ideaThree(board)}${ideaCapture()}${today}
      <p class="idea-tail">${counts.new || 0} new · ${counts.later || 0} later · ${btn("All ideas →", "ideas-all", "idea-link")}</p></section>`;
  }
  const fresh = board.new.slice(0, IDEAS_SHOWN).map(ideaCard).join("");
  const moreNew = (counts.new || 0) > IDEAS_SHOWN ? `<p class="idea-tail">${counts.new - IDEAS_SHOWN} more · ${btn("All ideas →", "ideas-all", "idea-link")}</p>` : "";
  const open = (counts.new || 0) + (counts.today || 0) + (counts.later || 0) + (counts.someday || 0);
  const sorter = open >= 2 ? btn("Sort my ideas…", "ideas-sort", "idea-sort-btn") : "";
  const waiting = (counts.new || 0) + board.today.filter((i) => i.bucket === "overdue").length;
  const friday = board.weekend && waiting
    ? `<div class="idea-friday" role="note"><strong>Before the weekend:</strong> ${waiting} idea${waiting === 1 ? " is" : "s are"} not planned yet. Give ${waiting === 1 ? "it" : "each"} a day with one tap${open >= 2 ? ", or let the lead suggest" : ""}.</div>`
    : "";
  const byDay = `<div class="idea-cols">
      ${ideaColumn("today", "Today", "Plan an idea for today and it waits here, beside your day.", board.today.map(ideaCard).join(""), counts.today)}
      ${ideaColumn("new", "New", "What you keep lands here. Give it a day with one tap.", fresh + moreNew, counts.new)}
      ${ideaColumn("coming", "Coming up", "Ideas planned for the days ahead.", ideaComing(board), (counts.later || 0) + (counts.someday || 0))}
    </div>`;
  return `<section class="floor-sheet idea-sheet idea-board" aria-label="Ideas" data-view="${esc(Ideas.view)}">
    <div class="idea-head"><h2>Ideas</h2><span class="idea-sub">Friction, curiosity, opportunity: three a day</span>${ideaThree(board)}${ideaViewSwitch()}${sorter}</div>
    ${friday}
    ${ideaCapture()}
    ${Ideas.view === "day" ? byDay : ideaKindWall(board)}</section>`;
}

async function ideaKeep(text) {
  const said = String(text || "").trim();
  if (!said) {
    toast("Write the thought first.", true);
    return false;
  }
  if (typeof calSendZone === "function") calSendZone();
  const source = window.matchMedia?.("(pointer: coarse)").matches ? "phone" : "floor";
  const kind = Ideas.pick || null;
  const under = kind ? `Kept as ${IDEA_KIND[kind].name}.` : "Kept.";
  const kept = await attempt(() => api("/ideas", { method: "POST", body: { text: said, source, kind } }), `${under} Plan it for a day when you like.`);
  return kept !== undefined;
}

function ideaFind(id) {
  const board = Ideas.board || {};
  return [...(board.today || []), ...(board.new || []), ...(board.later || []), ...(board.someday || [])].find((i) => i.id === id);
}

async function ideaPlanDialog(id) {
  const idea = ideaFind(id);
  const projects = await ideaProjects();
  const current = idea?.project?.id || "";
  const options = [`<option value="">No project</option>`, ...projects.map((p) => `<option value="${esc(p.id)}" ${p.id === current ? "selected" : ""}>${esc(p.name)}</option>`)].join("");
  const projectRow = `<div class="idea-project"><label for="idea-project-pick">Project</label><select id="idea-project-pick" data-idea="${esc(id)}">${options}</select></div>`;
  const kindRow = `<div class="idea-kind-row" role="group" aria-label="Kind"><span>Kind</span>${IDEA_KINDS.map((k) => btn(`<span aria-hidden="true">${esc(k.mark)}</span> ${esc(k.name)}`, `idea-kind:${id}:${k.key}`, `idea-kind-chip is-${k.key}${idea?.kind === k.key ? " on" : ""}`)).join("")}${idea?.kind ? btn("Unsorted", `idea-kind:${id}:`, "idea-kind-chip") : ""}</div>`;
  const todayIso = Ideas.board?.today_is || ideaIso(new Date());
  const days = ideaChoices(todayIso)
    .map(([label, iso]) => btn(`${esc(label)}<small>${esc(ideaDayName(iso))}</small>`, `idea-day:${id}:${iso}`, idea?.plan_for === iso ? "is-on idea-choice" : "idea-choice"))
    .join("");
  modal(
    "Plan this idea",
    `${idea ? `<p class="idea-quote">${esc(roomShort(idea.text, 200))}</p>` : ""}
     ${kindRow}
     <div class="idea-days">${days}</div>
     <form id="idea-date" class="idea-date" data-idea="${esc(id)}">
       <label for="idea-date-day">Another day</label>
       <input id="idea-date-day" name="day" type="date" min="${esc(todayIso)}" required>
       <button class="primary">Plan for that day</button>
     </form>
     ${projectRow}
     <form id="idea-words" class="idea-date" data-idea="${esc(id)}">
       <label for="idea-words-text">Its words</label>
       <input id="idea-words-text" name="text" maxlength="2000" required value="${esc(idea?.text || "")}">
       <button>Save words</button>
     </form>
     <p class="muted">On its day it is under Today on the floor, and agents read it with their work. Nothing starts until you press Start.</p>`,
    `<div class="button-row idea-dialog-actions">${btn("Drop", `idea-drop:${id}`, "danger")}${btn("Someday", `idea-someday:${id}`)}${idea?.plan_for || idea?.bucket === "someday" ? btn("Not planned", `idea-unplan:${id}`) : ""}${btn(idea?.blocked_at ? "Block more time…" : "Block time…", `idea-block:${id}`)}${btn("Start…", `idea-start:${id}`, "primary")}</div>`
  );
}

async function ideaPlan(id, body, said) {
  const planned = await attempt(() => api(`/ideas/${id}/plan`, { method: "PUT", body }), said);
  if (planned === undefined) return;
  closeModal();
  ideasRefresh();
}

function ideasRefresh() {
  // The floor shows the ideas; elsewhere the All dialog is refreshed, or nothing.
  if (Office.view === "home") rerender();
}

function ideasAll() {
  const board = Ideas.board || {};
  const group = (title, list) => (list?.length ? `<h3 class="idea-group">${esc(title)}</h3>${list.map(ideaCard).join("")}` : "");
  const kinds = board.kinds || {};
  const body = Ideas.view === "kind"
    ? [...IDEA_KINDS.map((k) => group(`${k.mark} ${k.name}`, kinds[k.key])), group("Unsorted", kinds.unsorted)].join("")
    : [group("Today", board.today), group("New", board.new), group("Coming up", board.later), group("Someday", board.someday)].join("");
  modal("All ideas", `<div class="idea-all">${body || `<p class="muted">No open ideas.</p>`}</div>`);
}

// The quick box from any page: `i`, when nothing is being typed.
function ideaQuickBox() {
  const box = $("#idea-text");
  if (box) {
    box.focus();
    box.scrollIntoView({ block: "center", behavior: "smooth" });
    return;
  }
  formModal(
    "Keep an idea",
    "idea-quick",
    `${ideaKindPicker()}<label class="visually-hidden" for="idea-quick-text">A thought to keep</label>
     ${ideaField("idea-quick-text", "", 3)}
     <p class="muted">Kept under Ideas on the floor. Nothing is sent to a model.</p>`,
    "Keep"
  );
  $("#idea-quick-text")?.focus();
}

document.addEventListener("keydown", (event) => {
  if (event.key !== "i" || event.metaKey || event.ctrlKey || event.altKey || event.isComposing) return;
  const target = event.target;
  const typing = target?.closest?.("input, textarea, select, [contenteditable=''], [contenteditable='true']");
  if (typing || $("#modal")?.open || !Office.csrf) return;
  event.preventDefault();
  ideaQuickBox();
});

// A half-written thought survives the floor's refresh, and leaving the page.
document.addEventListener("input", (event) => {
  if (!event.target?.matches?.("[data-idea-input]")) return;
  ideaDraftSave(event.target.value);
  ideaGrow(event.target);
});

// Enter keeps the thought, never while a character is being composed: Safari
// ends composition before the Enter that confirms it, so that Enter is ignored too.
let ideaComposedAt = 0;
document.addEventListener("compositionend", (event) => {
  if (event.target?.matches?.("[data-idea-input]")) ideaComposedAt = Date.now();
});
document.addEventListener("keydown", (event) => {
  const box = event.target;
  if (!box?.matches?.("[data-idea-input]") || event.key !== "Enter" || event.shiftKey) return;
  if (event.isComposing || event.keyCode === 229 || Date.now() - ideaComposedAt < 80) return;
  event.preventDefault();
  box.form?.requestSubmit();
});

Office.forms = Object.assign(Office.forms || {}, {
  "idea-keep": async (form) => {
    const input = form.querySelector("#idea-text");
    if (!input) {
      console.error("Element not found: #idea-text");
      return;
    }
    if (await ideaKeep(input.value)) {
      ideaDraftSave("");
      input.value = "";
      await rerender();
      $("#idea-text")?.focus();
    }
  },
  "idea-quick": async (form) => {
    const input = form.querySelector("#idea-quick-text");
    if (await ideaKeep(input?.value)) {
      ideaDraftSave("");
      closeModal();
      ideasRefresh();
    }
  },
  "idea-date": async (form) => {
    const day = String(new FormData(form).get("day") || "");
    if (!day) return toast("Pick a day first.", true);
    await ideaPlan(form.dataset.idea, { day }, `Planned for ${ideaDayName(day)}.`);
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "idea-plan": (id) => ideaPlanDialog(id),
  "idea-day": (arg) => {
    const [id, day] = arg.split(":");
    return ideaPlan(id, { day }, `Planned for ${ideaDayName(day)}.`);
  },
  "idea-someday": (id) => ideaPlan(id, { someday: true }, "Kept for someday."),
  "idea-unplan": (id) => ideaPlan(id, { day: null }, "Back with the new ideas."),
  "idea-done": async (id) => {
    if ((await attempt(() => api(`/ideas/${id}/finish`, { method: "POST", body: { status: "done" } }), "Done.")) !== undefined) {
      closeModal();
      ideasRefresh();
    }
  },
  "idea-drop": async (id) => {
    if ((await attempt(() => api(`/ideas/${id}/finish`, { method: "POST", body: { status: "dropped" } }), "Dropped.")) !== undefined) {
      closeModal();
      ideasRefresh();
    }
  },
  "idea-start": (id) => {
    // Start spends: say how much before it does (consent before spend).
    const idea = ideaFind(id);
    const cap = Number(Ideas.board?.start_cap_usd || 0.5);
    modal(
      "Start this as work?",
      `${idea ? `<p class="idea-quote">${esc(roomShort(idea.text, 200))}</p>` : ""}
       <p>The team takes it up in ${idea?.topic ? `“${esc(roomShort(idea.topic.title, 60))}”` : "a new discussion opened with these words"}, up to ${esc(money(cap))}. If it needs more it asks you first. The result comes back to that discussion.</p>`,
      `<div class="button-row">${btn("Not now", "close")}${btn("Start", `idea-go:${id}`, "primary")}</div>`
    );
  },
  "idea-go": async (id) => {
    const started = await attempt(() => api(`/ideas/${id}/start`, { method: "POST" }), "Started. The result comes back to its discussion.");
    if (started?.topic_id) {
      closeModal();
      go("topic", started.topic_id);
    }
  },
  "ideas-all": () => ideasAll(),
  // Choose the kind for the next thought kept; the chosen one again unsorts.
  "idea-kind-pick": (kind) => {
    Ideas.pick = Ideas.pick === kind ? "" : IDEA_KIND[kind] ? kind : "";
    ideaPrefSave(IDEA_PICK_KEY, Ideas.pick);
    document.querySelectorAll(".idea-kind-chip[role='radio']").forEach((chip) => {
      const on = chip.dataset.action === `idea-kind-pick:${Ideas.pick}`;
      chip.classList.toggle("on", on);
      chip.setAttribute("aria-checked", String(on));
    });
    document.querySelectorAll("[data-idea-input][data-kind-hint='1']").forEach((box) => {
      box.placeholder = ideaPlaceholder();
    });
    document.querySelectorAll("[data-idea-ask]").forEach((line) => {
      const ask = IDEA_KIND[Ideas.pick]?.ask || "";
      line.textContent = ask;
      line.hidden = !ask;
    });
    const bulb = document.querySelector("#idea-keep .idea-bulb");
    if (bulb) bulb.textContent = IDEA_KIND[Ideas.pick]?.mark || "💡";
    const box = document.querySelector("#modal [data-idea-input]") || document.getElementById("idea-text") || document.getElementById("idea-screen-text");
    box?.focus();
  },
  "idea-kind": async (arg) => {
    const [id, kind] = String(arg).split(":");
    const said = kind ? `Now ${IDEA_KIND[kind]?.name || kind}.` : "Back with the unsorted.";
    if ((await attempt(() => api(`/ideas/${id}/kind`, { method: "PUT", body: { kind: kind || null } }), said)) !== undefined) {
      closeModal();
      ideasRefresh();
    }
  },
  "ideas-view": (view) => {
    Ideas.view = view === "day" ? "day" : "kind";
    ideaPrefSave(IDEA_VIEW_KEY, Ideas.view === "day" ? "day" : "");
    ideasRefresh();
  },
  // Time on the calendar for it: the calendar's own form, filled in; the
  // office remembers the event once it is saved (never before).
  "idea-block": async (id) => {
    const idea = ideaFind(id);
    if (typeof calEditForm !== "function" || typeof CalView === "undefined") {
      console.error("Ideas: the calendar form is not loaded");
      return;
    }
    const writable = await attempt(() => api("/calendars/writable"));
    if (!Array.isArray(writable)) return;
    if (!writable.length) return toast("Connect a calendar the office can add to first (Calendar).", true);
    CalView.writable = writable;
    CalView.after = async (change) => {
      if (change?.id && (await attempt(() => api(`/ideas/${id}/block`, { method: "PUT", body: { change_id: change.id } }), "Time blocked for this idea.")) !== undefined) ideasRefresh();
    };
    const start = ideaDay(idea?.plan_for || Ideas.board?.today_is) || new Date();
    const now = new Date();
    if (ideaIso(start) === ideaIso(now)) start.setHours(Math.min(now.getHours() + 1, 23), 0, 0, 0);
    else start.setHours(9, 0, 0, 0);
    calEditForm(null, start, { title: roomShort(idea?.text || "Idea", 120), ends_at: new Date(start.getTime() + 3600000).toISOString() });
  },
});

// On a phone: a screen of its own at #idea, to bookmark or put on the
// home screen, and a 💡 button on every page of a narrow screen.
async function renderIdeaScreen(content) {
  const board = await api("/ideas").catch((error) => {
    console.error("Could not read the ideas", error);
    return null;
  });
  if (board) Ideas.board = board;
  const today = board?.today?.length ? board.today.map(ideaCard).join("") : `<p class="idea-empty">Nothing planned for today.</p>`;
  content.innerHTML = `${heading("Ideas", "Keep an idea", "Write it down now; plan it for a day later. Nothing is sent to a model.")}
    ${ideaThree(board)}
    <form id="idea-screen" class="idea-screen card" data-idea-box>
      ${ideaKindPicker()}
      <label class="visually-hidden" for="idea-screen-text">A thought to keep</label>
      ${ideaField("idea-screen-text", "", 4)}
      <div class="button-row"><button class="primary">Keep it</button></div>
    </form>
    <section class="floor-sheet idea-sheet" aria-label="Ideas for today"><h2>Today</h2><div class="idea-list">${today}</div>
      <p class="idea-more muted">${btn("All ideas →", "ideas-all", "room-tool")}</p></section>`;
}

Office.screens = Object.assign(Office.screens || {}, {
  idea: { title: "Keep an idea", render: renderIdeaScreen, quiet: () => Boolean(document.activeElement?.closest?.("[data-idea-box]")) },
});

Office.forms["idea-screen"] = async (form) => {
  const input = form.querySelector("#idea-screen-text");
  if (!input) {
    console.error("Element not found: #idea-screen-text");
    return;
  }
  if (await ideaKeep(input.value)) {
    ideaDraftSave("");
    await rerender();
    $("#idea-screen-text")?.focus();
  }
};


(function ideaButton() {
  if (document.querySelector(".idea-fab")) return;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "idea-fab";
  button.dataset.action = "idea-quick";
  button.setAttribute("aria-label", "Keep an idea");
  button.textContent = "💡";
  document.body.appendChild(button);
})();

Office.actions["idea-quick"] = () => {
  if (!Office.csrf) return;
  ideaQuickBox();
};

document.addEventListener("change", async (event) => {
  const pick = event.target;
  if (pick?.id !== "idea-project-pick") return;
  const id = pick.dataset.idea;
  const projectId = pick.value || null;
  const placed = await attempt(() => api(`/ideas/${id}/project`, { method: "PUT", body: { project_id: projectId } }), projectId ? "Placed in the project." : "Out of the project.");
  if (placed !== undefined) {
    const idea = ideaFind(id);
    if (idea) idea.project = placed.project;
    ideasRefresh();
  }
});

// Sort my ideas: paid, so what it reads and may cost comes first, and
// nothing changes until a suggestion is accepted.
async function ideasSortDialog() {
  const ahead = await attempt(() => api("/ideas/sort"));
  if (!ahead) return;
  if (ahead.latest?.open) return ideasSuggestions(ahead.latest, ahead);
  ideasSortOffer(ahead);
}

function ideasSortOffer(ahead) {
  const cost = ahead.estimate_usd != null ? `about ${money(Math.max(ahead.estimate_usd, 0.01))}` : "a few cents (not yet known: the office has no similar calls to go by)";
  modal(
    "Sort my ideas",
    `<p>The lead reads your ${esc(ahead.ideas)} open idea${ahead.ideas === 1 ? "" : "s"}, your projects' names, and your coming week if agents may read your calendar. It suggests:</p>
     <ul class="idea-sort-kinds"><li><strong>Merge</strong> ideas that say the same</li><li><strong>Reword</strong> one that is unclear</li><li><strong>A day</strong> to take one up</li><li><strong>A project</strong> it belongs to</li></ul>
     <p><strong>One model call, ${esc(cost)}.</strong> Nothing changes until you accept a suggestion.</p>`,
    `<div class="button-row">${btn("Not now", "close")}${btn("Sort them", "ideas-sort-go", "primary")}</div>`
  );
}

function ideasSuggestions(sort, ahead) {
  const KIND = { merge: "Merge", reword: "Reword", plan: "Plan", project: "Project" };
  const rows = (sort.suggestions || [])
    .map((s) => {
      const what =
        s.kind === "merge" ? `Into: “${esc(s.text)}”`
          : s.kind === "reword" ? `As: “${esc(s.text)}”`
          : s.kind === "plan" ? (s.someday ? "For someday" : `For ${esc(ideaDayName(s.day))}`)
          : `Into ${esc(s.project?.name || "")}`;
      const from = (s.texts || []).map((t) => `<li>${esc(roomShort(t, 120))}</li>`).join("");
      const done = { accepted: "Accepted ✓", ignored: "Ignored", passed: "Its ideas changed since" }[s.status];
      const actions = s.status === "open"
        ? `${btn("Ignore", `idea-sug:${sort.id}:${s.n}:ignore`)}${btn("Accept", `idea-sug:${sort.id}:${s.n}:accept`, "primary")}`
        : `<span class="tag">${esc(done || s.status)}</span>`;
      return `<article class="idea-sug is-${esc(s.status)}" data-sug="${esc(s.n)}"><div><span class="idea-sug-kind">${esc(KIND[s.kind] || s.kind)}</span><ul class="idea-sug-from">${from}</ul><p class="idea-sug-what">${what}</p>${s.why ? `<p class="idea-meta">${esc(s.why)}</p>` : ""}</div><div class="button-row tight">${actions}</div></article>`;
    })
    .join("");
  const spent = sort.cost_usd != null ? ` · cost ${money(sort.cost_usd)}` : "";
  const again = ahead ? btn("Sort again…", "ideas-sort-again") : "";
  modal(
    "Suggestions for your ideas",
    `<p class="muted">From ${esc(sort.ideas)} idea${sort.ideas === 1 ? "" : "s"}${esc(spent)}. Accept what helps; ignore the rest.</p>
     <div class="idea-sugs">${rows || `<p class="idea-empty">No suggestions: your ideas look sorted.</p>`}</div>`,
    `<div class="button-row">${again}${btn("Done", "close", "primary")}</div>`
  );
}

Office.forms["idea-words"] = async (form) => {
  const text = String(new FormData(form).get("text") || "").trim();
  if (!text) return toast("Write the idea first.", true);
  if ((await attempt(() => api(`/ideas/${form.dataset.idea}/text`, { method: "PUT", body: { text } }), "Saved.")) !== undefined) {
    closeModal();
    ideasRefresh();
  }
};

Office.actions = Object.assign(Office.actions || {}, {
  "ideas-sort": () => ideasSortDialog(),
  "ideas-sort-again": async () => {
    const ahead = await attempt(() => api("/ideas/sort"));
    if (ahead) ideasSortOffer(ahead);
  },
  "ideas-sort-go": async () => {
    const go = document.querySelector('[data-action="ideas-sort-go"]');
    if (go) {
      go.disabled = true;
      go.textContent = "Sorting…";
    }
    const sort = await attempt(() => api("/ideas/sort", { method: "POST", body: { confirm: true } }));
    if (!sort) {
      closeModal();
      return;
    }
    ideasSuggestions(sort, null);
    ideasRefresh();
  },
  "idea-sug": async (arg) => {
    const [sortId, n, answer] = String(arg).split(":");
    const sort = await attempt(() => api(`/ideas/sort/${sortId}/${n}/${answer}`, { method: "POST" }), answer === "accept" ? "Done." : "Ignored.");
    if (!sort) return;
    ideasSuggestions(sort, null);
    ideasRefresh();
  },
});

// A box drawn again (the floor refreshes) grows back to fit its draft.
new MutationObserver((changes) => {
  for (const change of changes) {
    for (const node of change.addedNodes) {
      if (node.nodeType !== 1) continue;
      const boxes = node.matches?.("[data-idea-input]") ? [node] : node.querySelectorAll?.("[data-idea-input]") || [];
      boxes.forEach((box) => ideaGrow(box));
    }
  }
}).observe(document.body, { childList: true, subtree: true });

// The answer in an editor beside the task.
//
// Once a task's answer is written it opens on the right of the task page, in
// a panel that closes to a strip. A document edits like a page, slides like a
// deck. Every edit is saved, after a pause, as the answer's next version; the
// office's own versions are kept. Selecting a passage offers to ask about it
// (in the task's room) or to have it rewritten (one paid call, only on a
// press, shown as a suggestion to accept or reject).
//
// The panel lives outside #content on purpose: the task page redraws itself
// every few seconds, and a redraw must never take the owner's typing with it.

const EDITOR_PREF = "office.editor.open";
const SAVE_AFTER_MS = 3000;
const AUTHOR = { owner: "you", chief: "the Chief", strategist: "the Strategist", application: "the office" };
// The task is no longer writing its answer: the owner's edit cannot race it.
const EDITABLE_STATUSES = ["COMPLETED", "FAILED", "CANCELLED", "REJECTED", "CLOSED", "NEEDS_HUMAN_REVIEW"];

const AnswerEditor = {
  run: null,
  answer: null, // GET /runs/{id}/answer
  doc: null, // the document as data
  artifactId: null,
  base: 0,
  mode: "doc",
  slides: [],
  slide: 0,
  view: "edit",
  open: false,
  dirty: false,
  saving: false,
  timer: null,
  conflict: null,
  status: "",
  picked: null, // { range, quote } of the selection asked about
  asking: null, // "ask" | "rewrite"
  loading: null,
};

// ---- preference ----

function editorPreferred() {
  try {
    const kept = localStorage.getItem(EDITOR_PREF);
    // The first time an answer is written it opens; after that, as left.
    return kept === null ? true : kept === "1";
  } catch {
    return true;
  }
}

function rememberEditor(open) {
  try {
    localStorage.setItem(EDITOR_PREF, open ? "1" : "0");
  } catch {
    // A private window keeps no preference; the panel still works.
  }
}

// ---- inline marks: the same rules as app/documents/inline.py ----

const MARK = /\[([^\[\]\n]+)\]\(((?:https?:\/\/|mailto:)[^\s()]+)\)|\*\*([^\s*](?:[^*]*?[^\s*])?)\*\*|\+\+([^\s+](?:[^+]*?[^\s+])?)\+\+|(?<![*\w])\*([^\s*](?:[^*]*?[^\s*])?)\*(?![*\w])/g;
// A link is kept only to a web or mail address, as the office writes it.
const SAFE_LINK = /^(https?:\/\/|mailto:)[^\s()]+$/;

function inlineHtml(text) {
  const source = String(text ?? "");
  let out = "";
  let at = 0;
  for (const found of source.matchAll(MARK)) {
    out += esc(source.slice(at, found.index));
    if (found[1] !== undefined) out += `<a href="${esc(found[2])}" rel="noopener noreferrer">${esc(found[1])}</a>`;
    else if (found[3] !== undefined) out += `<strong>${esc(found[3])}</strong>`;
    else if (found[4] !== undefined) out += `<u>${esc(found[4])}</u>`;
    else out += `<em>${esc(found[5])}</em>`;
    at = found.index + found[0].length;
  }
  return out + esc(source.slice(at));
}

// ---- document data → editable HTML ----

function blockHtml(block) {
  if (block.kind === "paragraph") return `<p>${inlineHtml(block.text) || "<br>"}</p>`;
  if (block.kind === "bullets") {
    const tagName = block.ordered ? "ol" : "ul";
    return `<${tagName}>${(block.items || []).map((item) => `<li>${inlineHtml(item) || "<br>"}</li>`).join("")}</${tagName}>`;
  }
  if (block.kind === "sources") {
    return `<ol class="sources">${(block.items || []).map((item) => `<li>${inlineHtml(item)}</li>`).join("")}</ol>`;
  }
  if (block.kind === "table") {
    const head = (block.columns || []).map((name) => `<th>${inlineHtml(name)}</th>`).join("");
    const rows = (block.rows || []).map((row) => `<tr>${row.map((cell) => `<td>${inlineHtml(cell) || "<br>"}</td>`).join("")}</tr>`).join("");
    const caption = block.caption ? `<caption>${esc(block.caption)}</caption>` : "";
    const key = `${(block.columns || []).join("|")}#${(block.rows || []).length}#${(block.rows || [])[0]?.[0] ?? ""}`;
    const drawn = (AnswerEditor.charts || {})[key];
    // The office's own SVG, its labels escaped there; skipped when the page is read back.
    const figure = drawn ? `<figure class="chart-figure" contenteditable="false" data-ed-skip>${drawn}</figure>` : "";
    return `<div class="table-frame"><table>${caption}<thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>${figure}`;
  }
  return "";
}

function sectionHtml(section) {
  return (section.heading ? `<h2>${esc(section.heading)}</h2>` : "") + (section.blocks || []).map(blockHtml).join("");
}

// ---- editable HTML → document data ----

// A pending suggestion is not yet the owner's text: the original stands.
function inlineText(node) {
  if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || "";
  if (node.nodeType !== Node.ELEMENT_NODE) return "";
  const element = node;
  if (element.dataset?.edUi !== undefined) return "";
  if (element.tagName === "INS" && element.dataset?.sugg) return "";
  if (element.tagName === "BR") return " ";
  const inner = [...element.childNodes].map(inlineText).join("");
  const name = element.tagName;
  const marked = (mark) => {
    const trimmed = inner.trim();
    if (!trimmed) return inner;
    const lead = inner.slice(0, inner.indexOf(trimmed));
    const tail = inner.slice(inner.indexOf(trimmed) + trimmed.length);
    return `${lead}${mark}${trimmed}${mark}${tail}`;
  };
  if ((name === "STRONG" || name === "B") && !inner.includes("**")) return marked("**");
  if ((name === "EM" || name === "I") && !/(^|[^*])\*(?!\*)/.test(inner)) return marked("*");
  if (name === "U" && !inner.includes("++")) return marked("++");
  if (name === "A") {
    const address = element.getAttribute("href") || "";
    const label = flat(inner).replace(/[\[\]]/g, "");
    return SAFE_LINK.test(address) && label ? `[${label}](${address})` : inner;
  }
  return inner;
}

const flat = (text) => String(text).replace(/\s+/g, " ").trim();
const BLOCK_TAGS = ["P", "DIV", "UL", "OL", "TABLE", "H1", "H2", "H3", "H4", "BLOCKQUOTE", "PRE"];

function tableData(table) {
  const head = table.querySelector("thead tr");
  const bodyRows = [...table.querySelectorAll("tbody tr")];
  const allRows = [...table.querySelectorAll("tr")];
  const headerRow = head || allRows[0];
  if (!headerRow) return null;
  const columns = [...headerRow.children].map((cell) => flat(inlineText(cell)));
  const rows = (head ? bodyRows : allRows.slice(1)).map((row) => {
    const cells = [...row.children].map((cell) => flat(inlineText(cell)));
    while (cells.length < columns.length) cells.push("");
    return cells.slice(0, columns.length);
  });
  const caption = table.querySelector("caption");
  return { kind: "table", columns, rows, caption: caption ? flat(caption.textContent) || null : null };
}

// Walks the page's top-level elements into a title and sections.
function documentFrom(root, fallbackTitle) {
  let title = "";
  const sections = [];
  let section = { heading: "", blocks: [] };
  const push = (block) => section.blocks.push(block);
  const walk = (nodes) => {
    for (const node of nodes) {
      if (node.nodeType === Node.TEXT_NODE) {
        const text = flat(node.nodeValue || "");
        if (text) push({ kind: "paragraph", text });
        continue;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) continue;
      const element = node;
      if (element.dataset?.edSkip !== undefined || element.dataset?.edUi !== undefined) continue;
      const name = element.tagName;
      if (name === "H1" && !title) {
        title = flat(element.textContent);
      } else if (["H1", "H2", "H3", "H4"].includes(name)) {
        if (section.heading || section.blocks.length) sections.push(section);
        section = { heading: flat(element.textContent).slice(0, 300), blocks: [] };
      } else if (name === "UL" || name === "OL") {
        const items = [...element.children].filter((li) => li.tagName === "LI").map((li) => flat(inlineText(li)));
        if (!items.some(Boolean)) continue;
        if (element.classList.contains("sources")) push({ kind: "sources", items: items.filter(Boolean) });
        else push({ kind: "bullets", items, ordered: name === "OL" });
      } else if (name === "TABLE") {
        const table = tableData(element);
        if (table) push(table);
      } else if (name === "DIV" && element.querySelector(":scope > table")) {
        const table = tableData(element.querySelector(":scope > table"));
        if (table) push(table);
      } else if (name === "DIV" && [...element.children].some((child) => BLOCK_TAGS.includes(child.tagName))) {
        walk(element.childNodes);
      } else if (name !== "BR") {
        const text = flat(inlineText(element));
        if (text) push({ kind: "paragraph", text });
      }
    }
  };
  walk(root.childNodes);
  if (section.heading || section.blocks.length) sections.push(section);
  return { title: title || fallbackTitle || "The answer", sections };
}

// ---- loading ----

// Where the open text is kept: a task's answer, or a document opened from a
// reply in a discussion (kind "document").
const isDocument = () => AnswerEditor.run?.kind === "document";
const sourceUrl = (id) => (isDocument() ? `/documents/${id}` : `/runs/${id}/answer`);

// A long reply, opened in the editor as a document of its own.
AnswerEditor.openDocument = async function openDocument(shown) {
  if (AnswerEditor.dirty) await AnswerEditor.save();
  AnswerEditor.reset();
  AnswerEditor.run = { id: shown.lineage, kind: "document", status: "COMPLETED", topic_id: shown.topic_id, deliverable: {} };
  AnswerEditor.openedOn = location.hash;
  AnswerEditor.open = true;
  await AnswerEditor.load(shown.lineage);
  AnswerEditor.place();
};

AnswerEditor.sync = async function sync(run, shown) {
  const panel = editorPanel();
  if (!panel) return;
  if (!run || !shown) {
    if (run && AnswerEditor.run?.id === run.id) return;
    AnswerEditor.hide();
    return;
  }
  const sameRun = AnswerEditor.run?.id === run.id;
  AnswerEditor.run = run;
  const newer = shown.artifact?.id && shown.artifact.id !== AnswerEditor.artifactId;
  if (!sameRun) {
    AnswerEditor.reset();
    AnswerEditor.open = editorPreferred();
  }
  AnswerEditor.openedOn = location.hash;
  if (!sameRun || (newer && !AnswerEditor.dirty && !AnswerEditor.saving)) await AnswerEditor.load(run.id);
  AnswerEditor.place();
};

AnswerEditor.reset = function reset() {
  clearTimeout(AnswerEditor.timer);
  Object.assign(AnswerEditor, {
    answer: null, doc: null, artifactId: null, base: 0, slides: [], slide: 0, view: "edit",
    dirty: false, saving: false, conflict: null, status: "", picked: null, asking: null, openedOn: null,
  });
};

AnswerEditor.load = async function load(runId) {
  if (AnswerEditor.loading === runId) return;
  AnswerEditor.loading = runId;
  try {
    const answer = await api(sourceUrl(runId)).catch(() => null);
    if (!answer?.current) return;
    const shown = await api(`/artifacts/${answer.current.artifact_id}/document`).catch(() => null);
    if (!shown?.document) return;
    if (AnswerEditor.run?.id !== runId) return;
    AnswerEditor.answer = answer;
    AnswerEditor.doc = shown.document;
    // Its tables of numbers, drawn by the office; shown, never edited or saved.
    AnswerEditor.charts = shown.charts || {};
    AnswerEditor.artifactId = answer.current.artifact_id;
    AnswerEditor.base = answer.current.version;
    AnswerEditor.mode = (AnswerEditor.run?.deliverable || {}).shape === "slides" ? "slides" : "doc";
    AnswerEditor.slides = (shown.document.sections || []).filter((section) => !section.supporting);
    if (!AnswerEditor.slides.length) AnswerEditor.slides = [{ heading: "", blocks: [{ kind: "paragraph", text: "" }] }];
    AnswerEditor.slide = Math.min(AnswerEditor.slide, AnswerEditor.slides.length - 1);
    AnswerEditor.conflict = null;
    AnswerEditor.status = "";
    AnswerEditor.draw();
    refreshAnswerCard();
  } finally {
    AnswerEditor.loading = null;
  }
};

// ---- the panel ----

function editorPanel() {
  let panel = document.getElementById("answer-editor");
  if (panel) return panel;
  if (!document.body) return null;
  panel = document.createElement("aside");
  panel.id = "answer-editor";
  panel.className = "answer-editor";
  panel.setAttribute("aria-label", "The answer, in the editor");
  panel.hidden = true;
  document.body.appendChild(panel);
  const rail = document.createElement("button");
  rail.type = "button";
  rail.id = "answer-editor-rail";
  rail.className = "answer-editor-rail";
  rail.hidden = true;
  rail.setAttribute("aria-label", "Open the answer");
  rail.innerHTML = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"></rect><path d="M14 4v16"></path></svg><span>The answer</span><i hidden></i>`;
  rail.addEventListener("click", () => AnswerEditor.show());
  document.body.appendChild(rail);
  wireEditor(panel);
  return panel;
}

AnswerEditor.available = () => Boolean(AnswerEditor.run && AnswerEditor.doc);
AnswerEditor.editable = () => EDITABLE_STATUSES.includes(AnswerEditor.run?.status);

AnswerEditor.place = function place() {
  const panel = editorPanel();
  const rail = document.getElementById("answer-editor-rail");
  if (!panel || !rail) return;
  const available = AnswerEditor.available();
  panel.hidden = !(available && AnswerEditor.open);
  rail.hidden = !(available && !AnswerEditor.open);
  const mark = rail.querySelector("i");
  if (mark) mark.hidden = !(AnswerEditor.answer?.current?.by === "owner");
  document.body.classList.toggle("editor-open", available && AnswerEditor.open);
  document.body.classList.toggle("editor-closed", available && !AnswerEditor.open);
};

AnswerEditor.show = function show() {
  AnswerEditor.open = true;
  rememberEditor(true);
  AnswerEditor.place();
  refreshAnswerCard();
  document.querySelector("#answer-editor .ed-close")?.focus();
};

AnswerEditor.close = function close() {
  if (AnswerEditor.dirty) AnswerEditor.save();
  AnswerEditor.open = false;
  rememberEditor(false);
  AnswerEditor.hideBubble();
  AnswerEditor.place();
  refreshAnswerCard();
  document.getElementById("answer-editor-rail")?.focus();
};

// Leaving the task: the panel goes, and anything typed is saved first.
AnswerEditor.hide = function hide() {
  if (AnswerEditor.dirty) AnswerEditor.save();
  AnswerEditor.run = null;
  AnswerEditor.reset();
  AnswerEditor.hideBubble();
  const panel = document.getElementById("answer-editor");
  const rail = document.getElementById("answer-editor-rail");
  if (panel) panel.hidden = true;
  if (rail) rail.hidden = true;
  document.body.classList.remove("editor-open", "editor-closed");
};

// The room's card says where the answer is; it is redrawn with the room, but
// opening or closing the panel should not wait for that.
function refreshAnswerCard() {
  const card = document.querySelector("[data-answer-place]");
  if (!card || !TaskRoom.answer || !TaskRoom.run) return;
  const holder = document.createElement("div");
  holder.innerHTML = answerCard(TaskRoom.answer, TaskRoom.run);
  if (!holder.querySelector("[data-answer-place]")) return;
  // The report sits inside the full card, or in a card of its own beside the
  // open editor: whichever was shown goes, and what answerCard drew replaces it.
  const beside = card.nextElementSibling;
  if (beside?.classList.contains("report-card")) beside.remove();
  card.replaceWith(...holder.children);
}

// Shown in the room while the editor is open; closed, the room shows the answer itself.
AnswerEditor.card = function card() {
  const label = AnswerEditor.mode === "slides" ? "The deck" : "The answer";
  const hint = AnswerEditor.editable()
    ? "Edit it like a page. Select any part to ask the team about it."
    : "The office is still working on it; you can edit it once it finishes.";
  return `<div class="card answer-place is-open" data-answer-place><strong>${esc(label)} is open on the right</strong><p>${esc(hint)}</p></div>`;
};

function versionsHtml() {
  const versions = [...(AnswerEditor.answer?.versions || [])].reverse();
  // Saving after every pause writes many versions; the owner's run of them reads as one.
  const groups = [];
  for (const version of versions) {
    const last = groups[groups.length - 1];
    if (last && last.by === "owner" && version.by === "owner") last.rows.push(version);
    else groups.push({ by: version.by, rows: [version] });
  }
  const rows = groups
    .map((group) => {
      const newest = group.rows[0];
      const oldest = group.rows[group.rows.length - 1];
      const span = group.rows.length > 1 ? `v${oldest.version}–v${newest.version}` : `v${newest.version}`;
      const who = group.by === "owner" ? "Your edits" : `By ${AUTHOR[group.by] || "the office"}`;
      const isCurrent = newest.artifact_id === AnswerEditor.artifactId;
      const restore = isCurrent || !AnswerEditor.editable() ? "" : `<button type="button" data-ed="restore:${esc(newest.artifact_id)}:${esc(newest.version)}">Restore</button>`;
      return `<li><div><strong>${esc(who)}</strong> <span class="muted">${esc(span)} · ${esc(when(newest.created_at))}${isCurrent ? " · the answer now" : ""}</span>${newest.note ? `<p>${esc(newest.note)}</p>` : ""}</div><div class="button-row tight">${btn("View", `artifact:${newest.artifact_id}`)}${restore}</div></li>`;
    })
    .join("");
  return `<div class="ed-versions"><p class="muted">Every version is kept. The newest is the answer the office and its agents read.</p><ol>${rows}</ol></div>`;
}

const ICON = {
  undo: '<path d="M9 14L4 9l5-5"></path><path d="M4 9h10a6 6 0 010 12h-3"></path>',
  redo: '<path d="M15 14l5-5-5-5"></path><path d="M20 9H10a6 6 0 000 12h3"></path>',
  bullets: '<path d="M9 6h11M9 12h11M9 18h11"></path><circle cx="4.5" cy="6" r="1"></circle><circle cx="4.5" cy="12" r="1"></circle><circle cx="4.5" cy="18" r="1"></circle>',
  numbers: '<path d="M10 6h10M10 12h10M10 18h10M4 4v4M3 18h3l-3 3h3"></path>',
  table: '<rect x="3" y="5" width="18" height="14" rx="1.5"></rect><path d="M3 10h18M9 10v9M15 10v9"></path>',
  ask: '<path d="M4 5h16v11H9l-5 4z"></path>',
  close: '<path d="M6 6l12 12M18 6L6 18"></path>',
  plus: '<path d="M12 5v14M5 12h14"></path>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"></path>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"></path>',
  down: '<path d="M12 5v14M6 13l6 6 6-6"></path>',
  link: '<path d="M10 14a4 4 0 006 0l3-3a4 4 0 00-6-6l-1 1"></path><path d="M14 10a4 4 0 00-6 0l-3 3a4 4 0 006 6l1-1"></path>',
};
const icon = (name) => `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">${ICON[name]}</svg>`;
const tool = (command, label, inner) => `<button type="button" class="ed-tool" data-ed="${command}" aria-label="${esc(label)}" title="${esc(label)}">${inner}</button>`;

function toolbarHtml() {
  const common = `${tool("undo", "Undo", icon("undo"))}${tool("redo", "Redo", icon("redo"))}<span class="ed-sep"></span>`;
  const marks = `${tool("bold", "Bold", "<b>B</b>")}${tool("italic", "Italic", "<i>I</i>")}${tool("underline", "Underline", "<u>U</u>")}${tool("link", "Link, or remove a link", icon("link"))}<span class="ed-sep"></span>`;
  const ask = `<span class="ed-grow"></span><button type="button" class="ed-ask-btn" data-ed="ask-selection">${icon("ask")}Ask about selection</button>`;
  if (AnswerEditor.mode === "slides") {
    return `<div class="ed-toolbar" role="toolbar" aria-label="Slide tools">${common}<button type="button" class="ed-tool wide" data-ed="slide-add">${icon("plus")}New slide</button>${tool("slide-up", "Move slide up", icon("up"))}${tool("slide-down", "Move slide down", icon("down"))}${tool("slide-delete", "Delete slide", icon("trash"))}<span class="ed-sep"></span>${marks}${tool("bullets", "Bulleted list", icon("bullets"))}${ask}</div>`;
  }
  return `<div class="ed-toolbar" role="toolbar" aria-label="Formatting">${common}<label class="ed-hidden-label" for="ed-style">Text style</label><select id="ed-style" class="ed-style" data-ed-style><option value="p">Body text</option><option value="h2">Heading</option></select><span class="ed-sep"></span>${marks}${tool("bullets", "Bulleted list", icon("bullets"))}${tool("numbers", "Numbered list", icon("numbers"))}${tool("table", "Insert a table", icon("table"))}${ask}</div>`;
}

function pageHtml() {
  const doc = AnswerEditor.doc;
  const editable = AnswerEditor.editable();
  const working = (doc.sections || []).filter((section) => section.supporting).length;
  if (AnswerEditor.mode === "slides") {
    const slides = AnswerEditor.slides;
    const index = AnswerEditor.slide;
    const thumbs = slides
      .map((slide, i) => {
        const lines = (slide.blocks || []).flatMap((b) => (b.kind === "paragraph" ? [b.text] : b.items || [])).slice(0, 3);
        return `<li><span>${i + 1}</span><button type="button" class="ed-thumb${i === index ? " is-current" : ""}" data-ed="slide-go:${i}" aria-label="Slide ${i + 1}${i === index ? ", shown" : ""}"${i === index ? ' aria-current="true"' : ""}><strong>${esc(slide.heading || doc.title)}</strong>${lines.map((line) => `<small>${esc(String(line).replace(/\*/g, ""))}</small>`).join("")}</button></li>`;
      })
      .join("");
    const current = slides[index] || { heading: "", blocks: [] };
    return `<div class="ed-deck"><ol class="ed-thumbs" aria-label="Slides">${thumbs}</ol><div class="ed-stage"><article class="ed-slide document" data-ed-page ${editable ? 'contenteditable="true"' : ""} spellcheck="true" aria-label="Slide ${index + 1}">${`<h2>${esc(current.heading || "")}</h2>`}${(current.blocks || []).map(blockHtml).join("")}</article><p class="muted ed-slide-count">Slide ${index + 1} of ${slides.length}</p><div class="ed-notes"><label for="ed-notes">Speaker notes</label><textarea id="ed-notes" data-ed-notes ${editable ? "" : "readonly"} placeholder="What you say over this slide">${esc(current.notes || "")}</textarea></div></div></div>`;
  }
  const sections = (doc.sections || []).filter((section) => !section.supporting);
  return `<div class="ed-desk"><article class="ed-page document" data-ed-page ${editable ? 'contenteditable="true"' : ""} spellcheck="true" aria-label="The answer"><h1>${esc(doc.title)}</h1>${sections.map(sectionHtml).join("")}</article>${working ? `<p class="muted ed-working">How it got there — ${working} more section${working === 1 ? "" : "s"}, kept with the answer and in every download.</p>` : ""}</div>`;
}

function statusHtml() {
  if (AnswerEditor.conflict) {
    return `<div class="ed-status is-warn" role="status"><span>The office wrote version ${esc(AnswerEditor.conflict)} while you were editing.</span><button type="button" data-ed="keep-mine">Keep my edits</button><button type="button" data-ed="show-theirs">Show theirs</button></div>`;
  }
  const text = !AnswerEditor.editable()
    ? "Read only while the office is still working on this task."
    : AnswerEditor.status || (AnswerEditor.dirty ? "Unsaved changes" : "Saved");
  const dot = AnswerEditor.status.startsWith("Couldn") ? "is-bad" : AnswerEditor.dirty || AnswerEditor.saving ? "is-busy" : "";
  return `<div class="ed-status" role="status"><span class="ed-dot ${dot}"></span><span>${esc(text)}</span><span class="ed-grow"></span><span class="muted">Your edits are a new version; the office's are kept.</span></div>`;
}

AnswerEditor.draw = function draw() {
  const panel = editorPanel();
  if (!panel || !AnswerEditor.doc) return;
  const label = isDocument() ? "The document" : AnswerEditor.mode === "slides" ? "The deck" : "The answer";
  const current = AnswerEditor.answer?.current;
  const meta = current ? `v${current.version} · by ${AUTHOR[current.by] || "the office"}` : "";
  const editing = AnswerEditor.view === "edit";
  panel.innerHTML = `
    <div class="ed-head">
      <div class="ed-title"><h2>${esc(label)}</h2><span class="muted">${esc(meta)}</span></div>
      <div class="ed-views" role="group" aria-label="Editor view">
        <button type="button" data-ed="view:edit" aria-pressed="${editing}">Edit</button>
        <button type="button" data-ed="view:versions" aria-pressed="${!editing}">Versions</button>
      </div>
      <details class="ed-download"><summary>Download</summary><div class="ed-download-menu">${downloadRow(AnswerEditor.artifactId)}</div></details>
      <button type="button" class="ed-tool ed-close" data-ed="close" aria-label="Close the editor" title="Close">${icon("close")}</button>
    </div>
    ${editing ? (AnswerEditor.editable() ? toolbarHtml() : "") : ""}
    <div class="ed-body">${editing ? pageHtml() : versionsHtml()}</div>
    <div class="ed-ask" data-ed-askbox hidden></div>
    <div data-ed-statusbar>${statusHtml()}</div>`;
  AnswerEditor.place();
};

AnswerEditor.drawStatus = function drawStatus() {
  const bar = document.querySelector("#answer-editor [data-ed-statusbar]");
  if (bar) bar.innerHTML = statusHtml();
};

// ---- editing and saving ----

AnswerEditor.page = () => document.querySelector("#answer-editor [data-ed-page]");

AnswerEditor.changed = function changed() {
  if (!AnswerEditor.editable()) return;
  AnswerEditor.dirty = true;
  AnswerEditor.status = "";
  AnswerEditor.drawStatus();
  clearTimeout(AnswerEditor.timer);
  AnswerEditor.timer = setTimeout(() => AnswerEditor.save(), SAVE_AFTER_MS);
};

// The slide on the stage, read back into the deck.
AnswerEditor.captureSlide = function captureSlide() {
  const page = AnswerEditor.page();
  if (AnswerEditor.mode !== "slides" || !page) return;
  const read = documentFrom(page, AnswerEditor.doc.title);
  const blocks = read.sections.flatMap((section, i) => (i === 0 ? section.blocks : [{ kind: "paragraph", text: section.heading }, ...section.blocks]));
  const heading = read.sections[0]?.heading || "";
  const notes = document.querySelector("#answer-editor [data-ed-notes]")?.value || "";
  AnswerEditor.slides[AnswerEditor.slide] = { heading, blocks, notes: notes.slice(0, 4000) };
};

AnswerEditor.current = function current() {
  if (AnswerEditor.mode === "slides") {
    AnswerEditor.captureSlide();
    return { title: AnswerEditor.doc.title, sections: AnswerEditor.slides };
  }
  const page = AnswerEditor.page();
  return page ? documentFrom(page, AnswerEditor.doc.title) : null;
};

AnswerEditor.save = async function save() {
  clearTimeout(AnswerEditor.timer);
  if (!AnswerEditor.dirty || AnswerEditor.saving || AnswerEditor.conflict || !AnswerEditor.run) return;
  const document_ = AnswerEditor.current();
  if (!document_) return;
  const runId = AnswerEditor.run.id;
  AnswerEditor.saving = true;
  AnswerEditor.status = "Saving…";
  AnswerEditor.drawStatus();
  AnswerEditor.dirty = false;
  try {
    const answer = await api(sourceUrl(runId), { method: "PUT", body: { base_version: AnswerEditor.base, document: document_ } });
    if (AnswerEditor.run?.id !== runId) return;
    AnswerEditor.answer = answer;
    AnswerEditor.base = answer.current.version;
    AnswerEditor.artifactId = answer.current.artifact_id;
    AnswerEditor.doc = { ...AnswerEditor.doc, title: document_.title };
    AnswerEditor.status = "";
    const meta = document.querySelector("#answer-editor .ed-title .muted");
    if (meta) meta.textContent = `v${answer.current.version} · by you`;
    const menu = document.querySelector("#answer-editor .ed-download-menu");
    if (menu) menu.innerHTML = downloadRow(AnswerEditor.artifactId);
  } catch (error) {
    AnswerEditor.dirty = true;
    const newer = /^newer_version:(\d+)$/.exec(String(error.message || ""));
    if (newer) AnswerEditor.conflict = Number(newer[1]);
    else {
      AnswerEditor.status = `Couldn't save: ${explain(error)}. Trying again shortly.`;
      AnswerEditor.timer = setTimeout(() => AnswerEditor.save(), SAVE_AFTER_MS * 3);
    }
  } finally {
    AnswerEditor.saving = false;
    AnswerEditor.drawStatus();
    if (AnswerEditor.dirty && !AnswerEditor.conflict && !AnswerEditor.timer) AnswerEditor.changed();
  }
};

// ---- restoring an earlier version ----

// The earlier version's own sections become the answer again, as a new
// version by the owner; every version stays where it was.
async function restoreVersion(artifactId, version) {
  const run_ = AnswerEditor.run;
  if (!run_ || !artifactId) return;
  if (AnswerEditor.dirty) await AnswerEditor.save();
  const earlier = await attempt(() => api(`/artifacts/${artifactId}/document`));
  if (!earlier?.document) return;
  const sections = (earlier.document.sections || []).filter((section) => !section.supporting);
  const saved = await attempt(() =>
    api(sourceUrl(run_.id), { method: "PUT", body: { base_version: AnswerEditor.base, document: { title: earlier.document.title, sections } } })
  );
  if (!saved) return;
  toast(`Version ${version} is the answer again, saved as version ${saved.current.version}.`);
  AnswerEditor.view = "edit";
  await AnswerEditor.load(run_.id);
}

// ---- commands ----

function edRun(command, value = null) {
  const page = AnswerEditor.page();
  if (!page) return;
  if (!page.contains(document.getSelection()?.anchorNode)) page.focus();
  document.execCommand(command, false, value);
  AnswerEditor.changed();
}

// A link on the selection, or off the one the cursor is in.
function editLink() {
  const selection = document.getSelection();
  const at = selection?.anchorNode;
  const inLink = (at?.nodeType === Node.ELEMENT_NODE ? at : at?.parentElement)?.closest?.("[data-ed-page] a");
  if (inLink) {
    edRun("unlink");
    return;
  }
  if (!selection || selection.isCollapsed) {
    toast("Select the words to link first.");
    return;
  }
  const range = selection.getRangeAt(0).cloneRange();
  const address = (window.prompt("Link to (https://… or mailto:…)", "https://") || "").trim();
  if (!address || address === "https://") return;
  if (!SAFE_LINK.test(address)) {
    toast("Links go to a web address (https://…) or an email (mailto:…).", true);
    return;
  }
  selection.removeAllRanges();
  selection.addRange(range);
  edRun("createLink", address);
}

const TABLE_HTML = `<div class="table-frame"><table><thead><tr><th>Column</th><th>Column</th><th>Column</th></tr></thead><tbody><tr><td><br></td><td><br></td><td><br></td></tr><tr><td><br></td><td><br></td><td><br></td></tr></tbody></table></div><p><br></p>`;

function slideOp(op) {
  AnswerEditor.captureSlide();
  const slides = AnswerEditor.slides;
  const at = AnswerEditor.slide;
  if (op === "add") {
    slides.splice(at + 1, 0, { heading: "New slide", blocks: [{ kind: "bullets", items: [""], ordered: false }] });
    AnswerEditor.slide = at + 1;
  } else if (op === "delete" && slides.length > 1) {
    slides.splice(at, 1);
    AnswerEditor.slide = Math.max(0, at - 1);
  } else if (op === "up" && at > 0) {
    [slides[at - 1], slides[at]] = [slides[at], slides[at - 1]];
    AnswerEditor.slide = at - 1;
  } else if (op === "down" && at < slides.length - 1) {
    [slides[at + 1], slides[at]] = [slides[at], slides[at + 1]];
    AnswerEditor.slide = at + 1;
  } else {
    return;
  }
  AnswerEditor.drawBody();
  AnswerEditor.changed();
}

AnswerEditor.drawBody = function drawBody() {
  const body = document.querySelector("#answer-editor .ed-body");
  if (body) body.innerHTML = pageHtml();
};

const EDITOR_COMMANDS = {
  undo: () => edRun("undo"),
  redo: () => edRun("redo"),
  bold: () => edRun("bold"),
  italic: () => edRun("italic"),
  underline: () => edRun("underline"),
  link: () => editLink(),
  bullets: () => edRun("insertUnorderedList"),
  numbers: () => edRun("insertOrderedList"),
  table: () => edRun("insertHTML", TABLE_HTML),
  "slide-add": () => slideOp("add"),
  "slide-delete": () => slideOp("delete"),
  "slide-up": () => slideOp("up"),
  "slide-down": () => slideOp("down"),
  close: () => AnswerEditor.close(),
  "keep-mine": () => {
    AnswerEditor.base = AnswerEditor.conflict;
    AnswerEditor.conflict = null;
    AnswerEditor.dirty = true;
    AnswerEditor.save();
  },
  "show-theirs": () => {
    AnswerEditor.dirty = false;
    AnswerEditor.conflict = null;
    if (AnswerEditor.run) AnswerEditor.load(AnswerEditor.run.id);
  },
  "ask-selection": () => AnswerEditor.askBox("ask"),
};

function wireEditor(panel) {
  // A toolbar press must not take the selection out of the page.
  panel.addEventListener("mousedown", (event) => {
    if (event.target.closest(".ed-toolbar button, .ed-bubble button")) event.preventDefault();
  });
  panel.addEventListener("click", (event) => {
    const target = event.target.closest("[data-ed]");
    if (!target || !panel.contains(target)) return;
    const [name, arg] = String(target.dataset.ed).split(":");
    if (name === "view") {
      if (AnswerEditor.mode === "slides") AnswerEditor.captureSlide();
      if (AnswerEditor.dirty) AnswerEditor.save();
      AnswerEditor.view = arg;
      AnswerEditor.draw();
    } else if (name === "slide-go") {
      AnswerEditor.captureSlide();
      AnswerEditor.slide = Number(arg) || 0;
      AnswerEditor.drawBody();
    } else if (name === "restore") {
      const [, , version] = String(target.dataset.ed).split(":");
      restoreVersion(arg, version);
    } else if (name === "accept" || name === "reject") {
      settleSuggestion(arg, name === "accept");
    } else if (EDITOR_COMMANDS[name]) {
      EDITOR_COMMANDS[name]();
    }
  });
  panel.addEventListener("change", (event) => {
    if (!event.target.matches("[data-ed-style]")) return;
    edRun("formatBlock", `<${event.target.value}>`);
  });
  panel.addEventListener("input", (event) => {
    if (event.target.closest("[data-ed-page], [data-ed-notes]")) AnswerEditor.changed();
  });
  // Pasted text arrives as text; a page copied from elsewhere brings its
  // styling with it, which the answer has no place for.
  panel.addEventListener("paste", (event) => {
    if (!event.target.closest("[data-ed-page]")) return;
    event.preventDefault();
    const text = event.clipboardData?.getData("text/plain") || "";
    document.execCommand("insertText", false, text);
  });
  panel.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      AnswerEditor.save();
    } else if (event.key === "Escape") {
      if (AnswerEditor.asking) AnswerEditor.askBox(null);
      else AnswerEditor.hideBubble();
    }
  });
  panel.addEventListener("submit", (event) => {
    if (!event.target.matches("[data-ed-askform]")) return;
    event.preventDefault();
    AnswerEditor.sendAsk(event.target);
  });
  document.addEventListener("selectionchange", () => AnswerEditor.noticeSelection());
}

// ---- asking about a selection ----

AnswerEditor.noticeSelection = function noticeSelection() {
  const page = AnswerEditor.page();
  const selection = document.getSelection();
  if (!page || !selection || selection.isCollapsed || !selection.rangeCount) {
    if (!AnswerEditor.asking) AnswerEditor.hideBubble();
    return;
  }
  const range = selection.getRangeAt(0);
  if (!page.contains(range.commonAncestorContainer)) return;
  const quote = flat(selection.toString());
  if (!quote) return;
  AnswerEditor.picked = { range: range.cloneRange(), quote: quote.slice(0, 4000) };
  if (!AnswerEditor.asking) AnswerEditor.showBubble(range);
};

AnswerEditor.showBubble = function showBubble(range) {
  let bubble = document.getElementById("ed-bubble");
  const panel = editorPanel();
  if (!panel) return;
  if (!bubble) {
    bubble = document.createElement("div");
    bubble.id = "ed-bubble";
    bubble.className = "ed-bubble";
    bubble.setAttribute("role", "toolbar");
    bubble.setAttribute("aria-label", "About the selection");
    bubble.innerHTML = `<button type="button" data-ed-bubble="ask">Ask about this</button>${AnswerEditor.editable() && !isDocument() ? `<button type="button" data-ed-bubble="rewrite">Ask for a rewrite…</button>` : ""}`;
    bubble.addEventListener("mousedown", (event) => event.preventDefault());
    bubble.addEventListener("click", (event) => {
      const choice = event.target.closest("[data-ed-bubble]")?.dataset.edBubble;
      if (choice) AnswerEditor.askBox(choice);
    });
    panel.appendChild(bubble);
  }
  const box = range.getBoundingClientRect();
  const frame = panel.getBoundingClientRect();
  bubble.hidden = false;
  const top = Math.max(frame.top + 8, box.top - 52);
  const left = Math.min(Math.max(frame.left + 8, box.left), frame.right - 300);
  bubble.style.top = `${top - frame.top}px`;
  bubble.style.left = `${left - frame.left}px`;
};

AnswerEditor.hideBubble = function hideBubble() {
  const bubble = document.getElementById("ed-bubble");
  if (bubble) bubble.hidden = true;
};

// One block holds the passage a rewrite replaces; across blocks it cannot say where.
function blockOf(node) {
  const page = AnswerEditor.page();
  let at = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  while (at && at.parentElement && at.parentElement !== page && !["LI", "TD", "TH", "P", "H2"].includes(at.tagName)) at = at.parentElement;
  return at;
}

AnswerEditor.askBox = function askBox(kind) {
  const box = document.querySelector("#answer-editor [data-ed-askbox]");
  if (!box) return;
  AnswerEditor.asking = kind;
  AnswerEditor.hideBubble();
  if (!kind) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  const picked = AnswerEditor.picked;
  if (!picked) {
    toast("Select the part of the answer to ask about first.");
    AnswerEditor.asking = null;
    return;
  }
  if (kind === "rewrite" && blockOf(picked.range.startContainer) !== blockOf(picked.range.endContainer)) {
    toast("Select within one paragraph or point to ask for a rewrite.", true);
    AnswerEditor.asking = null;
    return;
  }
  const canAsk = Boolean(AnswerEditor.run?.project_id || AnswerEditor.run?.topic_id);
  if (kind === "ask" && !canAsk) {
    toast("This task has no project, so there is no room to ask in.", true);
    AnswerEditor.asking = null;
    return;
  }
  const quote = picked.quote.length > 240 ? `${picked.quote.slice(0, 239)}…` : picked.quote;
  box.hidden = false;
  box.innerHTML = `<form data-ed-askform data-kind="${kind}">
      <blockquote>${esc(quote)}</blockquote>
      <label for="ed-ask-text">${kind === "ask" ? "Your question for the team" : "How should it change?"}</label>
      <input id="ed-ask-text" name="text" required maxlength="${kind === "ask" ? 700 : 600}" autocomplete="off" placeholder="${kind === "ask" ? "Is this backed by a source?" : "Shorter, and name the guidance"}">
      <div class="button-row tight">
        <button type="button" data-ed="ask-cancel">Cancel</button>
        <button class="primary">${kind === "ask" ? "Ask" : "Rewrite — one paid call"}</button>
      </div>
      <p class="muted">${kind === "ask" ? (isDocument() ? "The answer comes in this discussion." : "The answer comes in the task's Team room, under Your questions.") : "The Chief rewrites only this passage. It shows as a suggestion; nothing changes until you accept it."}</p>
    </form>`;
  box.querySelector("[data-ed='ask-cancel']")?.addEventListener("click", () => AnswerEditor.askBox(null));
  box.querySelector("input")?.focus();
};

AnswerEditor.sendAsk = async function sendAsk(form) {
  const kind = form.dataset.kind;
  const text = String(new FormData(form).get("text") || "").trim();
  const picked = AnswerEditor.picked;
  const run_ = AnswerEditor.run;
  if (!text || !picked || !run_) return;
  const send = form.querySelector("button.primary");
  if (send) send.disabled = true;
  if (kind === "ask") {
    const quote = picked.quote.length > 400 ? `${picked.quote.slice(0, 399)}…` : picked.quote;
    const sent = await attempt(() =>
      isDocument()
        ? api(`/topics/${run_.topic_id}/messages`, { method: "POST", body: { text: `About this part of the document — “${quote}”: ${text}` } })
        : api(`/projects/${run_.project_id}/room`, { method: "POST", body: { text: `About this part of the answer — “${quote}”: ${text}`, run_id: run_.id } })
    );
    if (sent === undefined) {
      if (send) send.disabled = false;
      return;
    }
    AnswerEditor.askBox(null);
    if (isDocument()) {
      toast("Asked. The answer comes here in the discussion.");
      rerender();
      return;
    }
    toast("Asked. The answer comes in the Team room, under Your questions.");
    TaskRoom.tab = "room";
    rerender();
    return;
  }
  AnswerEditor.status = "The Chief is rewriting the passage…";
  AnswerEditor.drawStatus();
  try {
    const rewritten = await api(`/runs/${run_.id}/answer/rewrite`, { method: "POST", body: { passage: picked.quote, instruction: text } });
    AnswerEditor.askBox(null);
    if (!rewritten.text) {
      toast(rewritten.note || "That passage could not be rewritten as asked.", true);
      return;
    }
    suggest(picked.range, rewritten.text, rewritten.note);
  } catch (error) {
    const reason = String(error.message || "");
    toast(
      reason === "chat_paused"
        ? "Chat is switched off in Settings, so no agent can rewrite."
        : reason === "monthly_limit"
          ? "This month's spending limit is reached. Raise it under Settings."
          : `The rewrite failed: ${explain(error)}`,
      true
    );
    if (send) send.disabled = false;
  } finally {
    AnswerEditor.status = "";
    AnswerEditor.drawStatus();
  }
};

let suggestionCount = 0;

// The rewrite sits beside the original until the owner chooses.
function suggest(range, text, note) {
  const page = AnswerEditor.page();
  if (!page || !page.contains(range.commonAncestorContainer)) {
    toast("The passage changed while it was being rewritten; nothing was replaced.", true);
    return;
  }
  const id = `s${++suggestionCount}`;
  const removed = document.createElement("del");
  removed.dataset.sugg = id;
  removed.appendChild(range.extractContents());
  const added = document.createElement("ins");
  added.dataset.sugg = id;
  added.innerHTML = inlineHtml(text);
  const bar = document.createElement("span");
  bar.className = "ed-sugg-bar";
  bar.dataset.edUi = "";
  bar.dataset.sugg = id;
  bar.contentEditable = "false";
  bar.innerHTML = `<span>${esc(note || "Suggested by the Chief")}</span><button type="button" data-ed="reject:${id}">Reject</button><button type="button" class="primary" data-ed="accept:${id}">Accept</button>`;
  range.insertNode(bar);
  range.insertNode(added);
  range.insertNode(removed);
  bar.querySelector("[data-ed^='accept']")?.focus();
}

function unwrap(element) {
  const parent = element.parentNode;
  if (!parent) return;
  while (element.firstChild) parent.insertBefore(element.firstChild, element);
  parent.removeChild(element);
}

function settleSuggestion(id, accepted) {
  const page = AnswerEditor.page();
  if (!page) return;
  const parts = [...page.querySelectorAll(`[data-sugg="${CSS.escape(id)}"]`)];
  for (const part of parts) {
    if (part.tagName === "DEL") accepted ? part.remove() : unwrap(part);
    else if (part.tagName === "INS") accepted ? unwrap(part) : part.remove();
    else part.remove();
  }
  if (accepted) AnswerEditor.changed();
}

// ---- leaving ----

// Leaving the page it was opened on closes it, after saving.
window.addEventListener("hashchange", () => {
  if (AnswerEditor.openedOn && location.hash !== AnswerEditor.openedOn) AnswerEditor.hide();
});
window.addEventListener("beforeunload", (event) => {
  if (!AnswerEditor.dirty) return;
  AnswerEditor.save();
  event.preventDefault();
  event.returnValue = "";
});

Office.actions = Object.assign(Office.actions || {}, {
  "editor-open": () => AnswerEditor.show(),
});

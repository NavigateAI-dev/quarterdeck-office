"use strict";

// AI history: exports from other AI tools, kept here and searched.
const ArchivePage = { overview: { sources: [], imports: [], review: [], memories: { proposed: [], kept: [] } }, hits: [], query: "", source: "" };
const ARCHIVE_ERRORS = {
  not_a_conversation_list: "That file is not a conversation export.",
  no_conversations_in_zip: "That zip has no conversations.json in it.",
  no_known_conversations: "No ChatGPT or Claude conversations were found in it.",
  broken_json: "The export is damaged or incomplete.",
  unreadable: "That file could not be read.",
  too_large: "That export is too large.",
  reader_unavailable: "The file reader did not answer. Try again.",
  interrupted: "The office restarted during the import. Import it again; nothing is doubled.",
};

function archiveImportRow(row) {
  const state = row.status === "running" ? "Reading…" : row.status === "failed" ? ARCHIVE_ERRORS[row.error] || "It failed." : `${row.conversations_new} new conversations, ${row.messages_new} new messages, from ${row.conversations_seen}`;
  const open = row.hidden_ready ? `<div class="button-row tight">${btn(`Let agents see ${row.hidden_ready}`, `archive-open-import:${row.id}`)}</div>` : "";
  const held = row.hidden_review ? `<p class="muted">${row.hidden_review} held back for sensitive data, listed under Review.</p>` : "";
  return `<div class="detail-line"><span>${esc(row.filename)} · ${esc(when(row.at))}</span><span>${esc(state)}</span></div>${held}${open}`;
}

function archiveReviewRow(item) {
  const found = (Array.isArray(item.found) ? item.found : []).map((f) => `${f.count} ${f.kind}`).join(", ");
  return `<div class="setting-row" data-review="${esc(item.conversation)}"><div><h3>${esc(item.title || "Untitled")}</h3><p class="muted">${esc(item.source_name)} · ${esc(found)}</p></div>
    <div class="button-row tight">${btn("Open", `archive-open:${item.conversation}|`)}</div></div>`;
}

const MEMORY_KINDS = { decision: "Decision", preference: "Preference", fact: "Fact", idea: "Idea", question: "Open question" };

function archiveMemoryRow(item) {
  const who = item.said_by === "owner" ? "You said" : "An AI said";
  const decide = item.status === "proposed" ? `${btn("Keep", `archive-memory:${item.id}:kept`, "primary")}${btn("Reject", `archive-memory:${item.id}:rejected`)}` : btn("Remove", `archive-memory:${item.id}:rejected`);
  return `<div class="setting-row" data-memory="${esc(item.id)}"><div><h3>${esc(item.content)}</h3>
      <p class="muted">${esc(MEMORY_KINDS[item.kind] || item.kind)} · ${who} · ${esc(item.source_name)} · ${esc(item.title || "Untitled")}${item.corrected ? " · Corrected by you" : ""}</p><p class="muted">“${esc(item.quote)}”</p></div>
    <div class="button-row tight">${decide}${btn("Correct", `archive-memory-edit:${item.id}`)}${btn("Source", `archive-open:${item.conversation}|${item.message_id ?? ""}`)}</div></div>`;
}

function archiveMemoriesHtml() {
  const { proposed, kept } = ArchivePage.overview.memories;
  const waiting = proposed.length ? `<h3>To review</h3><p class="muted">Each quotes the message it rests on. Nothing here reaches an agent until you keep it.</p>${proposed.map(archiveMemoryRow).join("")}` : "";
  const holding = kept.length ? `<h3>Kept</h3><div class="button-row tight">${btn("Download as Markdown", "archive-memory-export:")}</div>${kept.map(archiveMemoryRow).join("")}` : "";
  return waiting || holding ? `<div class="card spaced"><h2>Memories</h2>${waiting}${holding}</div>` : "";
}

function archiveHit(hit) {
  const who = hit.role === "user" ? "You" : "AI";
  const away = hit.excluded ? ` ${tag("Hidden from agents", "gray")}` : "";
  return `<div class="setting-row" data-hit="${esc(hit.message_id)}"><div><h3>${esc(hit.title || "Untitled")}${away}</h3>
      <p class="muted">${esc(hit.source_name)} · ${esc(hit.at ? when(hit.at) : "no date")} · ${who}</p><p>${esc(hit.excerpt)}</p></div>
    <div class="button-row tight">${btn("Open", `archive-open:${hit.conversation}|${hit.message_id}`)}</div></div>`;
}

function archiveHitsHtml() {
  return ArchivePage.hits.map(archiveHit).join("") || (ArchivePage.query ? `<p class="muted">Nothing found. Try fewer or shorter words.</p>` : "");
}

function drawArchiveHits() {
  const box = document.getElementById("archive-hits");
  if (!box) {
    console.error("Element not found: archive-hits");
    return;
  }
  box.innerHTML = archiveHitsHtml();
}

const ARCHIVE_REACH = { off: "No agent", discussions: "Agents in discussions", discussions_and_slack: "Agents in discussions and in your Slack chats" };
const ARCHIVE_ACTIONS = { search: "searched", read: "read", reading: "read for memories", export: "exported" };

// Compared with what is saved now, so narrowing never asks.
function archiveProtectionWidens(next) {
  const now = ArchivePage.protection;
  if (!now) return false;
  const rank = Object.keys(ARCHIVE_REACH);
  if (rank.indexOf(next.agents) > rank.indexOf(now.agents)) return true;
  if (next.backups === "encrypted" && now.backups === "left_out") return true;
  if (now.providers === null) return false;
  return next.providers === null || next.providers.some((p) => !now.providers.includes(p));
}

function archiveProtectionHtml() {
  const p = ArchivePage.protection;
  if (!p) return `<div class="card spaced"><h2>Protection</h2><p class="muted">The settings could not be read.</p></div>`;
  const choices = Array.isArray(p.connections) ? p.connections : [];
  const ticks = choices.map((c) => `<label class="check"><input type="checkbox" name="provider" value="${esc(c.id)}"${p.providers === null || p.providers.includes(c.id) ? " checked" : ""}> ${esc(c.name)}</label>`).join("");
  const seen = (Array.isArray(p.record) ? p.record : []).map((r) => `<div class="detail-line"><span>${esc(when(r.at))} · ${esc(r.who)} ${esc(ARCHIVE_ACTIONS[r.action] || r.action)}</span><span>${esc(r.detail)}</span></div>`).join("");
  return `<div class="card spaced"><h2>Protection</h2>
    <p class="muted">Always on: only you can open this page, imports stay hidden from agents until you let them in, anything with a secret or identity number opens one by one, and those are masked before any model.</p>
    <form data-form="archive-protection">
      <label>Who may search your history<select name="agents">${Object.entries(ARCHIVE_REACH).map(([v, n]) => `<option value="${v}"${v === p.agents ? " selected" : ""}>${n}</option>`).join("")}</select></label>
      <fieldset><legend>Model providers that may receive it</legend>${ticks || `<p class="muted">No providers set up yet.</p>`}<p class="muted">An agent on any other provider, or with a backup provider not ticked, does not get your history.</p></fieldset>
      <label>In backups<select name="backups"><option value="encrypted"${p.backups === "encrypted" ? " selected" : ""}>Encrypted under this office's root key</option><option value="left_out"${p.backups === "left_out" ? " selected" : ""}>Left out</option></select></label>
      <p class="muted">Encrypted backups can only bring your history back with the root key, so keep it safe. If left out, a restore loses what you kept and deleted here.</p>
      <div class="button-row"><button type="submit" class="primary">Save</button></div>
    </form>
    <p class="muted">On this computer the words are kept unencrypted in the database so they can be searched: keep FileVault on. Passages an agent used stay in that discussion and its kept model calls.</p>
    <h3>Who reached it</h3>${seen || `<p class="muted">Nothing yet.</p>`}</div>`;
}

function archiveExportHtml() {
  return `<div class="card spaced"><h2>Export</h2><p class="muted">All your imported conversations and memories as a zip: JSON and Markdown you can read anywhere. Conversations hidden from agents are included; ones you deleted are not.</p>
    <form data-form="archive-export">
      <label class="check"><input type="checkbox" name="original"> Keep secrets and identity numbers as written (otherwise masked)</label>
      <div class="button-row"><button type="submit">Download</button></div>
    </form></div>`;
}

async function renderArchive(content) {
  const [seen, guarded] = await Promise.all([api("/archive").catch(() => null), api("/archive/protection").catch(() => null)]);
  ArchivePage.protection = guarded;
  ArchivePage.overview = { sources: Array.isArray(seen?.sources) ? seen.sources : [], imports: Array.isArray(seen?.imports) ? seen.imports : [], review: Array.isArray(seen?.review) ? seen.review : [], memories: { proposed: Array.isArray(seen?.memories?.proposed) ? seen.memories.proposed : [], kept: Array.isArray(seen?.memories?.kept) ? seen.memories.kept : [] } };
  const kept = ArchivePage.overview.sources.map((s) => `<div class="detail-line"><span>${esc(s.name)}</span><span>${s.conversations} conversations, ${s.messages} messages</span></div>`).join("");
  const choices = [["", "Everywhere"], ["chatgpt", "ChatGPT"], ["claude", "Claude"], ["office", "This office"]];
  content.innerHTML = `${heading("AI HISTORY", "What you worked out with other AI tools, kept here.", "Import your ChatGPT and Claude exports, then search them. Importing and searching call no model; Find what to keep, inside a conversation, does. Imports stay hidden from agents until you let them in. When an agent searches, the passages it finds go to its model, with secrets and identity numbers masked.")}
    <div class="card"><h2>Search</h2>
      <form data-form="archive-search" class="button-row">
        <input name="q" aria-label="Search your history" placeholder="Words that were said" value="${esc(ArchivePage.query)}">
        <select name="source" aria-label="Where to search">${choices.map(([v, n]) => `<option value="${v}"${v === ArchivePage.source ? " selected" : ""}>${n}</option>`).join("")}</select>
        <button type="submit" class="primary">Search</button>
      </form><div id="archive-hits">${archiveHitsHtml()}</div></div>
    ${archiveMemoriesHtml()}
    <div class="card spaced"><h2>Import an export</h2>
      <p class="muted">The zip or conversations.json from ChatGPT (Settings → Data controls → Export) or Claude (Settings → Privacy → Export data). Importing one again adds only what is new.</p>
      <div class="button-row">${btn("Choose a file", "archive-pick:", "primary")}</div>
      ${kept}${ArchivePage.overview.imports.map(archiveImportRow).join("")}</div>
    ${ArchivePage.overview.review.length ? `<div class="card spaced"><h2>Review</h2><p class="muted">These hold a secret or an identity number, so they stay hidden until you look at each one.</p>${ArchivePage.overview.review.map(archiveReviewRow).join("")}</div>` : ""}
    ${archiveProtectionHtml()}${archiveExportHtml()}`;
  if (ArchivePage.overview.imports.some((row) => row.status === "running")) {
    setTimeout(() => {
      if (document.getElementById("archive-hits")) rerender();
    }, 3000);
  }
}

async function archiveSend(file) {
  const headers = { "Content-Type": "application/octet-stream" };
  if (Office.csrf) headers["X-CSRF-Token"] = Office.csrf;
  toast("Sending the file…");
  const response = await fetch(`/archive/imports?filename=${encodeURIComponent(file.name)}`, { method: "POST", headers, body: file, credentials: "same-origin" });
  if (!response.ok) {
    toast(response.status === 413 ? "That export is too large." : "The file could not be sent.", true);
    return;
  }
  toast("Received. Reading it now.");
  rerender();
}

Office.actions = Object.assign(Office.actions || {}, {
  "archive-pick": () => {
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = ".zip,.json";
    picker.addEventListener("change", () => {
      const file = picker.files?.[0];
      if (file) archiveSend(file).catch((error) => toast(explain(error), true));
    });
    picker.click();
  },
  "archive-open": async (value) => {
    const [ref, message] = String(value).split("|");
    const around = message ? `?around=${encodeURIComponent(message)}` : "";
    const seen = await attempt(() => api(`/archive/conversations/${encodeURIComponent(ref)}${around}`));
    if (!seen) return;
    const rows = Array.isArray(seen.messages) ? seen.messages : [];
    const body = rows.map((m) => `<div class="detail-line"><span>${m.role === "user" ? "You" : "AI"}${m.at ? ` · ${esc(when(m.at))}` : ""}</span></div><p>${esc(m.content)}</p>`).join("");
    const id = ref.startsWith("archive:") ? ref.slice("archive:".length) : "";
    const hidden = seen.state === "excluded";
    const read = id && !hidden ? btn("Find what to keep", `archive-read:${id}`, "primary") : "";
    const controls = id ? `<div class="button-row">${read}${btn(hidden ? "Let agents see it" : "Hide from agents", `archive-hide:${id}:${hidden ? "no" : "yes"}`)}${btn("Delete for good", `archive-delete:${id}`)}</div>` : "";
    modal(seen.title || "Untitled", `${controls}${body}`);
  },
  "archive-read": async (id) => {
    toast("Reading this conversation…");
    const done = await attempt(() => api(`/archive/conversations/${id}/memories`, { method: "POST", body: { confirm: true } }));
    if (!done) return;
    const part = done.messages_read < done.messages_total ? ` It read the latest ${done.messages_read} of ${done.messages_total} messages.` : "";
    const left = done.dropped ? ` ${done.dropped} left out: their quotes did not check out.` : "";
    toast(`${done.proposed} new to review.${part}${left}`);
    closeModal();
    rerender();
  },
  "archive-memory": async (value) => {
    const [id, status] = String(value).split(":");
    if ((await attempt(() => api(`/archive/memories/${id}`, { method: "PUT", body: { status } }), status === "kept" ? "Kept." : "Rejected.")) !== undefined) rerender();
  },
  "archive-memory-edit": (id) => {
    const { proposed, kept } = ArchivePage.overview.memories;
    const item = [...proposed, ...kept].find((m) => m.id === id);
    if (!item) {
      console.error("Memory not found:", id);
      return;
    }
    modal("Correct this memory", `<form data-form="archive-memory-correct" data-memory="${esc(id)}"><textarea name="content" aria-label="The memory" rows="4" maxlength="600">${esc(item.content)}</textarea><div class="button-row"><button type="submit" class="primary">Keep it like this</button></div></form>`);
  },
  "archive-memory-export": () => {
    const link = document.createElement("a");
    link.href = "/archive/memories/export.md";
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
  },
  "archive-open-import": async (id) => {
    if ((await attempt(() => api(`/archive/imports/${id}/open`, { method: "POST" }), "Agents can search them now.")) !== undefined) rerender();
  },
  "archive-hide": async (value) => {
    const [id, hide] = String(value).split(":");
    if ((await attempt(() => api(`/archive/conversations/${id}`, { method: "PUT", body: { excluded: hide === "yes" } }), hide === "yes" ? "Hidden from agents." : "Agents can see it again.")) === undefined) return;
    closeModal();
    await archiveSearch();
    rerender();
  },
  "archive-delete": async (id) => {
    if (!window.confirm("Delete this conversation for good? Importing the export again will not bring it back.")) return;
    if ((await attempt(() => api(`/archive/conversations/${id}`, { method: "DELETE" }), "Deleted.")) === undefined) return;
    closeModal();
    await archiveSearch();
    rerender();
  },
});

async function archiveSearch() {
  if (!ArchivePage.query) return;
  const params = new URLSearchParams({ q: ArchivePage.query });
  if (ArchivePage.source) params.set("source", ArchivePage.source);
  const seen = await attempt(() => api(`/archive/search?${params}`));
  ArchivePage.hits = Array.isArray(seen?.hits) ? seen.hits : [];
  drawArchiveHits();
}

Office.screens.archive = { title: "AI history", render: renderArchive };

Office.forms = Object.assign(Office.forms || {}, {
  "archive-protection": async (form) => {
    const data = new FormData(form);
    const all = form.querySelectorAll("input[name=provider]").length;
    const ticked = data.getAll("provider").map(String);
    const body = { agents: String(data.get("agents")), providers: ticked.length === all ? null : ticked, backups: String(data.get("backups")) };
    if ((await attempt(() => api("/archive/protection", { method: "PUT", body }), "Saved.")) !== undefined) rerender();
  },
  "archive-export": (form) => {
    const original = new FormData(form).get("original") === "on";
    if (original && !window.confirm("This file will hold your secrets and identity numbers as written. Keep it somewhere safe.")) return;
    const link = document.createElement("a");
    link.href = `/archive/export.zip?mask=${original ? "false" : "true"}`;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
    toast("Preparing your export. A large history takes a while.");
  },
  "archive-memory-correct": async (form) => {
    const content = String(new FormData(form).get("content") || "").trim();
    if (!content) return;
    if ((await attempt(() => api(`/archive/memories/${form.dataset.memory}`, { method: "PUT", body: { content } }), "Corrected and kept.")) === undefined) return;
    closeModal();
    rerender();
  },
  "archive-search": async (form) => {
    const data = new FormData(form);
    ArchivePage.query = String(data.get("q") || "").trim();
    ArchivePage.source = String(data.get("source") || "");
    ArchivePage.hits = [];
    await archiveSearch();
    drawArchiveHits();
  },
});

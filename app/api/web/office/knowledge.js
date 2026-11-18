"use strict";

// Knowledge: the owner's documents the office may read, and which projects may.

const Knowledge = { sources: [], projects: [], grants: {} };

async function renderKnowledge(content) {
  const [sources, projects] = await Promise.all([api("/sources"), api("/projects")]);
  Knowledge.sources = sources;
  Knowledge.projects = projects.filter((p) => p.status === "ACTIVE");
  const grants = await Promise.all(Knowledge.projects.map((p) => api(`/projects/${p.id}/sources`).catch(() => [])));
  Knowledge.grants = Object.fromEntries(Knowledge.projects.map((p, i) => [p.id, new Set(grants[i].map((s) => s.id))]));
  const card = (s) => {
    const where = s.kind === "folder" ? `Shared folder · ${s.root}` : "From this computer";
    const failed = s.status === "FAILED";
    const access = Knowledge.projects.filter((p) => Knowledge.grants[p.id]?.has(s.id)).map((p) => p.name);
    return `<div class="card"><div class="card-top"><span class="connector-icon">${s.kind === "folder" ? "▱" : "✎"}</span>${tag(failed ? "Could not read" : `${s.document_count} documents`, failed ? "clay" : "")}</div>
      <h2>${esc(s.name)}</h2><p>${esc(where)}${s.last_error ? ` · ${esc(s.last_error)}` : ""}</p>
      <div class="detail-line"><span>Access</span><strong>Read only</strong></div>
      <div class="detail-line"><span>Projects</span><strong>${esc(access.join(", ") || "None yet")}</strong></div>
      ${s.skipped?.length ? `<details><summary>${s.skipped.length} files skipped</summary><ul class="plain">${s.skipped.slice(0, 50).map((x) => `<li>${esc(x.path)}: ${esc(x.reason)}</li>`).join("")}</ul></details>` : ""}
      <div class="button-row">${btn("Manage access", `source-access:${s.id}`, "primary")}${
        s.kind === "folder" ? btn("Re-read", `source-reindex:${s.id}`) : btn("Update from folder", `source-refresh:${s.id}`) + btn("Paste text", `source-doc:${s.id}`)
      }${btn("Remove", `source-remove:${s.id}`, "danger")}</div></div>`;
  };
  content.innerHTML =
    heading("SHARED CONTEXT", "A library your office can work with.", "Add only what your agents need, and choose which projects may read it.", btn("＋ Add source", "source-add", "primary")) +
    (sources.length ? `<div class="grid3">${sources.map(card).join("")}</div>` : emptyCard("No knowledge yet.", "Choose a folder on this computer, or paste text. PDFs (including scanned), Word, Markdown and text are read.", btn("＋ Add source", "source-add", "primary"))) +
    `<div class="note spaced">A folder from this computer is copied in once and read in the isolated reader, which has no network; choose it again to update it. Google Drive, Dropbox and OneDrive work through their synced folder on this computer; a direct connector to those services is not built yet. The shared folder is read in place and never changed. A project's tasks see only the sources you grant it, and cite them as local:// passages.</div>`;
}

async function openAddSource() {
  const folders = await api("/sources/folders")
    .then((r) => r.folders || [])
    .catch(() => []);
  const choices = [
    ["device", "A folder on this computer"],
    ...(folders.length ? [["folder", "A folder in the shared folder"]] : []),
    ["upload", "Text I paste"],
  ];
  formModal(
    "Add knowledge",
    "source-form",
    `${field("Name", "name", { attrs: 'maxlength="255"' })}
     ${field("What to add", "kind", { type: "select", choices, value: "device" })}
     <div id="device-row">
       <label class="field" for="f-device">Choose the folder</label>
       <input id="f-device" type="file" webkitdirectory directory multiple>
       <p class="muted hint">The office copies and reads what it can (PDF, including scanned, Word, Markdown, text). Files stay on this computer. For a folder from Google Drive, Dropbox or OneDrive, choose its synced folder here.</p>
       <p id="device-count" class="muted"></p>
     </div>
     <div id="folder-row" hidden>${
       folders.length
         ? field("Folder", "root", { type: "select", required: false, choices: folders.map((f) => [f, f]) })
         : ""
     }<p class="muted hint">The shared folder is read in place, never copied. Change which folder that is with <code>python3 -m scripts.launch --pick-folder</code>.</p></div>`,
    "Add source"
  );
  const kind = $("#f-kind");
  const sync = () => {
    for (const [id, want] of [["device-row", "device"], ["folder-row", "folder"]]) {
      const row = $(`#${id}`);
      if (row && kind) row.hidden = kind.value !== want;
    }
  };
  kind?.addEventListener("change", sync);
  $("#f-device")?.addEventListener("change", (event) => {
    const count = $("#device-count");
    const files = [...(event.target.files || [])];
    const bytes = files.reduce((total, f) => total + f.size, 0);
    if (count) count.textContent = files.length ? `${files.length} files · ${(bytes / 1024 ** 2).toFixed(1)} MB` : "";
  });
  sync();
}

// A folder is sent in batches so one request stays small; the office reads
// each batch in the isolated reader and keeps what it understands.
const BATCH_FILES = 25;
const BATCH_BYTES = 20 * 1024 * 1024;
const READABLE = /\.(pdf|docx|md|markdown|txt|csv|json|ya?ml|html?|rst)$/i;

async function sendFolder(sourceId, files) {
  const usable = files.filter((file) => READABLE.test(file.name) && file.size <= 25 * 1024 ** 2);
  if (!usable.length) {
    toast("None of those files can be read. PDF, Word, Markdown and text are supported.", true);
    return null;
  }
  let batch = [];
  let bytes = 0;
  let first = true;
  let last = null;
  const flush = async () => {
    if (!batch.length) return;
    last = await api(`/sources/${sourceId}/files`, { method: "POST", body: { replace: first, files: batch } });
    first = false;
    batch = [];
    bytes = 0;
  };
  for (const file of usable) {
    const buffer = await file.arrayBuffer();
    let binary = "";
    const bytesView = new Uint8Array(buffer);
    for (let i = 0; i < bytesView.length; i += 8192) {
      binary += String.fromCharCode(...bytesView.subarray(i, i + 8192));
    }
    batch.push({ path: file.webkitRelativePath || file.name, content: btoa(binary) });
    bytes += file.size;
    toast(`Reading ${Math.min(usable.length, batch.length + (first ? 0 : 1))} of ${usable.length} files…`);
    if (batch.length >= BATCH_FILES || bytes >= BATCH_BYTES) await flush();
  }
  await flush();
  const ignored = files.length - usable.length;
  toast(`${last?.document_count ?? 0} documents read${ignored ? `; ${ignored} file(s) of other kinds ignored` : ""}.`);
  return last;
}

function openAccess(id) {
  const source = Knowledge.sources.find((s) => s.id === id);
  if (!source) return;
  modal(
    `Who may read ${source.name}`,
    Knowledge.projects.length
      ? `<p>Tasks in a ticked project can read and cite this source.</p>${Knowledge.projects
          .map((p) => `<label class="check"><input type="checkbox" data-grant="${esc(p.id)}" ${Knowledge.grants[p.id]?.has(id) ? "checked" : ""}> ${esc(p.name)}</label>`)
          .join("")}`
      : "<p>Create a project first (start a task), then grant it access here.</p>"
  );
  document.querySelectorAll("[data-grant]").forEach((box) => {
    box.addEventListener("change", async () => {
      const project = box.dataset.grant;
      const ok = await attempt(() => api(`/projects/${project}/sources/${id}`, { method: box.checked ? "PUT" : "DELETE" }), box.checked ? "Access granted." : "Access removed.");
      if (ok === undefined) box.checked = !box.checked;
      else if (box.checked) Knowledge.grants[project]?.add(id);
      else Knowledge.grants[project]?.delete(id);
    });
  });
  $("#modal")?.addEventListener("close", () => rerender(), { once: true });
}

function openAddDocument(id) {
  AgentState.docSource = id;
  formModal(
    "Add a document",
    "document-form",
    `${field("File name", "path", { attrs: 'maxlength="512" placeholder="notes/interview-3.md"' })}
     <label class="field" for="f-file">Load from a text or Markdown file (optional)</label><input id="f-file" type="file" accept=".txt,.md,.markdown,.csv,text/*">
     ${field("Text", "text", { type: "textarea", attrs: 'rows="8"' })}`,
    "Add document"
  );
  $("#f-file")?.addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const text = $("#f-text");
    const path = $("#f-path");
    if (text) text.value = await file.text();
    if (path && !path.value) path.value = file.name;
  });
}

Office.screens.knowledge = { title: "Knowledge", render: renderKnowledge };

Office.actions = Object.assign(Office.actions || {}, {
  "source-add": openAddSource,
  "source-access": openAccess,
  "source-doc": openAddDocument,
  "source-refresh": (id) => {
    // Browsers cannot re-read a folder without the owner choosing it again.
    const picker = document.createElement("input");
    picker.type = "file";
    picker.webkitdirectory = true;
    picker.multiple = true;
    picker.addEventListener("change", async () => {
      const files = [...(picker.files || [])];
      if (!files.length) return;
      await attempt(() => sendFolder(id, files));
      rerender();
    });
    picker.click();
  },
  "source-reindex": async (id) => {
    const source = await attempt(() => api(`/sources/${id}/reindex`, { method: "POST" }));
    if (source) toast(source.status === "FAILED" ? `Could not read it: ${source.last_error}` : `Re-read: ${source.document_count} documents.`, source.status === "FAILED");
    rerender();
  },
  "source-remove": async (id) => {
    const source = Knowledge.sources.find((s) => s.id === id);
    if (!window.confirm(`Remove "${source?.name}" from the office? Your files are not touched.`)) return;
    if ((await attempt(() => api(`/sources/${id}`, { method: "DELETE" }), "Source removed.")) !== undefined) rerender();
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "source-form": async (form) => {
    const data = new FormData(form);
    const name = String(data.get("name")).trim();
    const kind = data.get("kind");
    let source;
    if (kind === "device") {
      const files = [...($("#f-device")?.files || [])];
      if (!files.length) {
        toast("Choose a folder first.", true);
        return;
      }
      source = await attempt(() => api("/sources/upload", { method: "POST", body: { name } }));
      if (!source) return;
      closeModal();
      const done = await attempt(() => sendFolder(source.id, files));
      await rerender();
      if (done) openAccess(source.id);
      return;
    }
    if (kind === "folder") {
      const root = String(data.get("root") || "");
      if (!root) {
        toast("Choose a folder, or paste documents instead.", true);
        return;
      }
      source = await attempt(() => api("/sources", { method: "POST", body: { name, root } }));
      if (!source) return;
      toast(source.status === "FAILED" ? `Added, but it could not be read: ${source.last_error}` : `Added: ${source.document_count} documents read.`, source.status === "FAILED");
    } else {
      source = await attempt(() => api("/sources/upload", { method: "POST", body: { name } }), "Added. Now add documents to it.");
      if (!source) return;
    }
    closeModal();
    await rerender();
    openAccess(source.id);
  },
  "document-form": async (form) => {
    const data = new FormData(form);
    const body = { path: String(data.get("path")).trim(), text: String(data.get("text")) };
    if ((await attempt(() => api(`/sources/${AgentState.docSource}/documents`, { method: "POST", body }), "Document added.")) === undefined) return;
    closeModal();
    rerender();
  },
});

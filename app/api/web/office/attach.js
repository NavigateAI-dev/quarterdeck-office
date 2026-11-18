"use strict";

// Attaching files and folders where work starts: the new task
// form, the floor's box to the Chief, and a discussion's composer. What is
// picked is read like any document the office keeps (the isolated reader),
// granted to the work's project when it is sent, and named on the message.

const Attach = { by: {}, reading: {} };
const ATTACH_TYPES = ".pdf,.docx,.md,.markdown,.txt,.csv,.json,.yaml,.yml,.html,.htm,.rst";

function attachList(formId) {
  return Attach.by[formId] || [];
}

function attachedIds(formId) {
  return attachList(formId).map((item) => item.id);
}

function attachClear(formId) {
  Attach.by[formId] = [];
  attachDraw(formId);
}

function attachChips(formId) {
  const chips = attachList(formId)
    .map(
      (item) => `<span class="attach-chip"><span>${esc(item.name)}</span><small>${esc(item.documents)} document${item.documents === 1 ? "" : "s"}</small>${btn("×", `attach-remove:${formId}:${item.id}`, "attach-x", `aria-label="Remove ${esc(item.name)}"`)}</span>`
    )
    .join("");
  const reading = Attach.reading[formId] ? `<span class="attach-chip is-reading"><span>${esc(Attach.reading[formId])}</span><small>reading…</small></span>` : "";
  return chips + reading;
}

// The control: what is attached, and the ways to attach more.
function attachRow(formId) {
  return `<div class="attach-row" data-attach="${esc(formId)}">
    <div class="attach-chips" id="${esc(formId)}-chips">${attachChips(formId)}</div>
    <div class="attach-menu" role="group" aria-label="Attach">
      ${btn("＋ Files…", `attach-files:${formId}`, "attach-btn")}
      ${btn("＋ A folder…", `attach-folder:${formId}`, "attach-btn")}
      ${btn("From documents", `attach-existing:${formId}`, "attach-btn")}
    </div>
    <div class="attach-pick-holder" id="${esc(formId)}-pick" hidden></div>
  </div>`;
}

function attachDraw(formId) {
  const holder = document.getElementById(`${formId}-chips`);
  if (holder) holder.innerHTML = attachChips(formId);
}

// True while something is still being read: sending then would leave it out.
function attachBusy(formId) {
  if (!Attach.reading[formId]) return false;
  toast("Still reading what you attached. Send when it is done.", true);
  return true;
}

async function attachUpload(formId, files, label) {
  if (!files.length) return;
  const stamp = new Date().toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const name = `${label} · ${stamp}`.slice(0, 255);
  Attach.reading[formId] = label;
  attachDraw(formId);
  try {
    const source = await attempt(() => api("/sources/upload", { method: "POST", body: { name } }));
    if (!source?.id) return;
    const read = await sendFolder(source.id, files).catch((error) => {
      toast(explain(error), true);
      return null;
    });
    if (!read || !read.document_count) {
      // Nothing readable came of it: no empty source is left behind.
      await api(`/sources/${source.id}`, { method: "DELETE" }).catch(() => null);
      return;
    }
    Attach.by[formId] = [...attachList(formId), { id: source.id, name: label, documents: read.document_count }];
  } finally {
    Attach.reading[formId] = "";
    attachDraw(formId);
  }
}

function attachPick(formId, folder) {
  const picker = document.createElement("input");
  picker.type = "file";
  picker.multiple = true;
  if (folder) picker.webkitdirectory = true;
  else picker.accept = ATTACH_TYPES;
  picker.addEventListener("change", () => {
    const files = [...(picker.files || [])];
    if (!files.length) return;
    const top = folder ? String(files[0].webkitRelativePath || "").split("/")[0] : "";
    const label = folder ? top || "A folder" : files.length === 1 ? files[0].name : `${files[0].name} and ${files.length - 1} more`;
    attachUpload(formId, files, label);
  });
  picker.click();
}

Office.actions = Object.assign(Office.actions || {}, {
  "attach-files": (formId) => attachPick(formId, false),
  "attach-folder": (formId) => attachPick(formId, true),
  "attach-existing": async (formId) => {
    // Listed in place, not in a dialog: the new task form is a dialog itself.
    const holder = document.getElementById(`${formId}-pick`);
    if (!holder) return;
    if (!holder.hidden) {
      holder.hidden = true;
      return;
    }
    const sources = await attempt(() => api("/sources"));
    if (!Array.isArray(sources)) return;
    const already = new Set(attachedIds(formId));
    const usable = sources.filter((s) => s.document_count && !already.has(s.id));
    Attach.offered = usable;
    holder.innerHTML = usable.length
      ? `<ul class="attach-pick">${usable
          .map((s) => `<li><span><strong>${esc(s.name)}</strong><small>${esc(s.document_count)} documents</small></span>${btn("Attach", `attach-choose:${formId}:${s.id}`)}</li>`)
          .join("")}</ul>`
      : `<p class="muted">Nothing in the office's documents yet. Attach files or a folder instead.</p>`;
    holder.hidden = false;
  },
  "attach-choose": (arg) => {
    const [formId, id] = String(arg).split(":");
    const source = (Attach.offered || []).find((s) => s.id === id);
    if (!source) return;
    Attach.by[formId] = [...attachList(formId), { id: source.id, name: source.name, documents: source.document_count }];
    const holder = document.getElementById(`${formId}-pick`);
    if (holder) holder.hidden = true;
    attachDraw(formId);
  },
  "attach-remove": (arg) => {
    const [formId, id] = String(arg).split(":");
    // Taken off this message only; it stays among the office's documents.
    Attach.by[formId] = attachList(formId).filter((item) => item.id !== id);
    attachDraw(formId);
  },
});

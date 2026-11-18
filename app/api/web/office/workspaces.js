// Folders of the owner's, given to a business space:
// the Workspaces part of Settings (Reach). Each space, its folders and their
// access, and who gave or took back what. Folders come from inside the one
// approved root (~/dev); the server refuses anything outside it, hidden, or
// holding keys. Work access is offered only once the owner has turned on a
// writable mount.

const WS_REFUSED = {
  whole_root: "Choose a folder inside the root, not the whole of it.",
  absolute_path: "Choose a folder inside the root.",
  outside_root: "That folder is outside the root.",
  credential_folder: "That folder holds keys or sign-ins; the office never reads it.",
  hidden_folder: "Hidden folders are not given to a space.",
  already_attached: "That folder is already in this space.",
  work_access_off: "Work access needs a writable folder, which is off. Read access is on.",
  not_fast_forward: "Your default branch has moved on since this work started. Nothing was merged; ask for the work again on the new version.",
  checkout_has_changes: "Your checkout of the default branch has changes that are not committed. Commit or put them aside, then merge.",
  not_an_office_branch: "Only the office's own branches (office/…) can be merged here.",
  not_a_default_branch: "That is not a branch to merge into.",
  git_failed: "Git could not read that branch just now.",
};

function wsAccessTag(access) {
  return access === "work" ? tag("Work", "amber") : tag("Read", "blue");
}

async function renderWorkspaces(content) {
  const [spaces, all] = await Promise.all([api("/ventures"), api("/workspaces")]);
  const list = Array.isArray(spaces) ? spaces : [];
  const folders = Array.isArray(all?.workspaces) ? all.workspaces : [];
  const work = Boolean(all?.work_access);
  const rows = list
    .map((space) => {
      const mine = folders.filter((f) => f.space_id === space.id);
      const items = mine.length
        ? `<ul class="ws-list">${mine
            .map(
              (f) => `<li class="ws-folder"><div><strong>${esc(f.name)}</strong><span class="muted">~/dev/${esc(f.path)} · given by ${esc(f.granted_by)} ${esc(when(f.granted_at))}</span></div>${wsAccessTag(f.access)}${
                work ? btn(f.access === "work" ? "Read only" : "Allow work", `ws-access:${f.id}:${f.access === "work" ? "read" : "work"}`, "room-tool") : ""
              }${work && f.access === "work" ? btn("Branches", `ws-branches:${f.id}`, "room-tool") : ""}${btn("Detach", `ws-detach:${f.id}`, "room-tool")}</li>`
            )
            .join("")}</ul>`
        : `<p class="muted">No folders yet.</p>`;
      return `<section class="card ws-space" data-space="${esc(space.id)}"><div class="card-top"><h3>${esc(space.name)}</h3>${btn("＋ Attach a folder", `ws-attach:${space.id}`, "")}</div>${items}</section>`;
    })
    .join("");
  content.innerHTML =
    heading(
      "WORKSPACES",
      "Your folders, given to a space.",
      `Folders inside ~/dev, read live by the work in that space: searched, quoted, summarised, never changed. ${
        work ? "Work access is on: coding agents may change files on their own branch, and merging needs you." : "Changing files (work access) is off."
      }`
    ) + (rows || emptyCard("No business spaces yet.", "Make a space first; its folders are given here."));
}

async function wsAttachDialog(spaceId) {
  const [picked, all] = await Promise.all([
    api("/workspaces/folders").catch((error) => {
      console.warn("The folders could not be listed:", error);
      return null;
    }),
    api("/workspaces").catch(() => ({})),
  ]);
  const folders = Array.isArray(picked?.folders) ? picked.folders : [];
  if (!folders.length) {
    modal("Attach a folder", `<p>The folder reader did not list any folders inside ~/dev just now. It may be starting; try again in a moment.</p>`);
    return;
  }
  const options = folders.map((path) => `<option value="${esc(path)}">~/dev/${esc(path)}</option>`).join("");
  const work = Boolean(all?.work_access);
  formModal(
    "Attach a folder",
    "ws-attach-form",
    `<input type="hidden" name="space" value="${esc(spaceId)}">
     <label class="field" for="ws-path">Folder</label><select id="ws-path" name="path" required>${options}</select>
     <label class="field" for="ws-name">Called (optional)</label><input id="ws-name" name="name" maxlength="120" placeholder="Its folder name">
     <label class="field" for="ws-access">Access</label>
     <select id="ws-access" name="access"><option value="read">Read: search, quote, summarise</option>${
       work ? `<option value="work">Work: coding agents change files on their own branch</option>` : `<option value="work" disabled>Work: off until you turn on a writable folder</option>`
     }</select>
     <p class="muted">The work in this space reads it as it is, with no network, and cites the files it used.</p>`,
    "Attach"
  );
}

// The office's branches in a folder (6d): what each changes, and the owner's merge.
const WsBranches = { folder: "", rows: [] };

async function wsBranchesDialog(folderId) {
  let listed;
  try {
    listed = await api(`/workspaces/${folderId}/branches`);
  } catch (error) {
    toast(WS_REFUSED[error?.message] || explain(error), true);
    return;
  }
  WsBranches.folder = folderId;
  WsBranches.rows = Array.isArray(listed?.branches) ? listed.branches : [];
  const rows = WsBranches.rows
    .map((row, index) => `<li class="ws-branch"><div><strong>${esc(row.branch)}</strong><span class="muted">${esc(row.subject || "")} · ${esc(when(row.at))} · ${esc(String(row.commit || "").slice(0, 8))}</span></div>${btn("Changes", `ws-diff:${index}`, "room-tool")}${btn("Merge…", `ws-merge:${index}`, "room-tool primary")}</li>`)
    .join("");
  modal(
    "The office's branches",
    rows
      ? `<p class="muted">Work the office finished in this folder, each on a branch of its own. Your default branch changes only when you merge one.</p><ul class="ws-branches">${rows}</ul>`
      : `<p>No office branches here yet. When approved work in this space passes its checks, it appears here as office/….</p>`
  );
}

async function wsDiffDialog(index) {
  const row = WsBranches.rows[Number(index)];
  if (!row) {
    console.error("Branch not found:", index);
    return;
  }
  let changes;
  try {
    changes = await api(`/workspaces/${WsBranches.folder}/branches/diff?branch=${encodeURIComponent(row.branch)}`);
  } catch (error) {
    toast(WS_REFUSED[error?.message] || explain(error), true);
    return;
  }
  const files = (changes.files || []).map((f) => `<li><code>${esc(f.status)}</code> ${esc(f.path)}</li>`).join("");
  modal(
    `Changes on ${row.branch}`,
    `<p class="muted">Against ${esc(changes.into)}: ${esc((changes.files || []).length)} file${(changes.files || []).length === 1 ? "" : "s"}.</p>
     <ul class="ws-diff-files">${files}</ul>
     <pre class="ws-diff">${esc(changes.patch || "")}</pre>${changes.truncated ? `<p class="muted">Long; the rest is in the branch.</p>` : ""}`,
    `<div class="button-row">${btn("Back", `ws-branches:${WsBranches.folder}`)}${btn("Merge…", `ws-merge:${index}`, "primary")}</div>`
  );
}

function wsMergeDialog(index) {
  const row = WsBranches.rows[Number(index)];
  if (!row) {
    console.error("Branch not found:", index);
    return;
  }
  modal(
    "Merge this branch?",
    `<p>Your default branch moves forward to <strong>${esc(row.branch)}</strong> (${esc(String(row.commit || "").slice(0, 8))}). Fast-forward only: nothing of yours is overwritten, and if your default branch has moved on or your checkout has uncommitted changes, nothing happens.</p>
     <p class="muted">This is your merge, recorded as yours. The office never merges on its own, and nothing is pushed.</p>`,
    `<div class="button-row">${btn("Not now", `ws-branches:${WsBranches.folder}`)}${btn("Merge", `ws-merge-go:${index}`, "primary")}</div>`
  );
}

Office.forms = Object.assign(Office.forms || {}, {
  "ws-attach-form": async (form) => {
    const data = new FormData(form);
    const body = { path: String(data.get("path") || ""), access: String(data.get("access") || "read") };
    const name = String(data.get("name") || "").trim();
    if (name) body.name = name;
    try {
      await api(`/ventures/${data.get("space")}/workspaces`, { method: "POST", body });
    } catch (error) {
      toast(WS_REFUSED[error?.message] || explain(error), true);
      return;
    }
    closeModal();
    toast("Attached. The work in this space can read it now.");
    await rerender();
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "ws-branches": (folderId) => wsBranchesDialog(folderId),
  "ws-diff": (index) => wsDiffDialog(index),
  "ws-merge": (index) => wsMergeDialog(index),
  "ws-merge-go": async (index) => {
    const row = WsBranches.rows[Number(index)];
    if (!row) return;
    try {
      const merged = await api(`/workspaces/${WsBranches.folder}/merge`, { method: "POST", body: { branch: row.branch } });
      toast(`Merged into ${merged.into}.`);
      closeModal();
    } catch (error) {
      toast(WS_REFUSED[error?.message] || explain(error), true);
    }
  },
  "ws-attach": (spaceId) => wsAttachDialog(spaceId),
  "ws-detach": async (id) => {
    if (!window.confirm("Detach this folder? The work in this space stops reading it at once.")) return;
    // A detach answers with no content; only a failure is undefined.
    if ((await attempt(() => api(`/workspaces/${id}`, { method: "DELETE" }), "Detached.")) !== undefined) await rerender();
  },
  "ws-access": async (arg) => {
    const [id, access] = String(arg).split(":");
    try {
      await api(`/workspaces/${id}`, { method: "PATCH", body: { access } });
    } catch (error) {
      toast(WS_REFUSED[error?.message] || explain(error), true);
      return;
    }
    await rerender();
  },
});

Office.screens.workspaces = { title: "Workspaces", render: renderWorkspaces, nav: "office" };

// The review of a coding task done by the coder on the owner's Mac (Stage 36c):
// what the office's own checks found, what the reviewer read in the diff, and
// Merge, Send back or Discard. Merging and discarding happen on the Mac, by the
// helper, only when the owner presses; the office never writes to ~/dev itself.

const CODER_STATES = {
  queued: "waiting for your Mac",
  claimed: "taken on your Mac",
  running: "working on your Mac",
  done: "finished",
  failed: "failed",
  cancelled: "stopped",
  unknown: "stopped reporting",
};

// What a coder job's failure means, as the build card reads it.
Object.assign(BUILD_ERRORS, {
  "coder:unknown": "The coder on your Mac stopped reporting. Look in its worktree to see how far it got.",
  "coder:cli_not_on_this_mac": "The chosen coding CLI is not on your Mac. Choose one the helper found, under Settings → Models.",
  "coder:coder_not_started": "The coder on your Mac never took it: start Quarterdeck Coder, then try again.",
  "coder:files_too_large": "The branch's files were too large for the office to check (over 200 MB).",
  "coder:files_not_sent": "The coder could not send the branch's files to the office.",
  coder_files_missing: "The office never received the branch's files to check.",
  install_failed: "Installing the packages it listed failed.",
});

function coderReviewRows(review) {
  if (!review) return "";
  if (review.verdict === "not_reviewed") return `<p class="muted">${esc(review.plain || "Not reviewed.")}</p>`;
  const verdict = review.verdict === "looks_right" ? tag("Looks right") : tag("Concerns", "clay");
  const findings = (review.findings || [])
    .map((f) => `<li>${f.path ? `<code>${esc(f.path)}</code> ` : ""}${esc(f.issue)}</li>`)
    .join("");
  return `<p>${verdict} ${esc(review.plain || "")}</p>${findings ? `<ul class="plain coder-findings">${findings}</ul>` : ""}`;
}

async function loadCoderCard(run, content) {
  const card = content?.querySelector("#coder-card") || document.getElementById("coder-card");
  if (!card) return;
  let found = null;
  try {
    found = await api(`/runs/${run.id}/coder`);
  } catch (error) {
    card.hidden = true;
    return;
  }
  const jobs = found?.jobs || [];
  const code = jobs.find((j) => j.kind === "code");
  if (!code) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const report = run.state?.engineering || {};
  const tests = report.tests || {};
  const changed = report.changed_files || [];
  const settled = jobs.find((j) => j.kind !== "code" && jobs.indexOf(j) < jobs.indexOf(code));
  const where = `${esc(code.cli_label)} · <code>${esc(code.branch)}</code> in <code>${esc(code.folder)}</code>`;
  const checks = tests.status
    ? `<p>${tests.status === "passed" ? tag("Checks passed") : tag(tests.status === "failed" ? "Checks failed" : "Not checked", "clay")} ${changed.length} file${changed.length === 1 ? "" : "s"} changed, checked by the office itself.</p>`
    : "";
  const takeOver = code.outcome?.take_over
    ? `<details class="coder-take-over"><summary>Carry it on yourself</summary><pre class="command">${esc(code.outcome.take_over)}</pre></details>`
    : "";
  const lastWord = settled
    ? `<p class="muted">${esc(settled.kind === "merge" ? "Merge" : "Discard")}: ${esc(CODER_STATES[settled.state] || settled.state)}${settled.outcome?.error ? ` (${esc(settled.outcome.error.replace(/_/g, " "))})` : ""}.</p>`
    : "";
  const actions = [
    found.merge ? btn("Merge", "coder-merge", "primary") : "",
    found.send_back ? btn("Send back…", "coder-send-back") : "",
    found.discard ? btn("Discard", "coder-discard") : "",
  ].join("");
  card.innerHTML = `<h2>Review</h2>
    <p>${where} ${tag(CODER_STATES[code.state] || code.state, code.state === "done" ? "" : "gray")}</p>
    ${checks}${coderReviewRows(report.review)}${takeOver}${lastWord}
    ${actions ? `<div class="button-row">${actions}</div>` : ""}
    <p class="muted">Merge and Discard are done on your Mac by Quarterdeck Coder: Merge only into a clean checkout on ${esc(code.folder)}'s own branch, and only the commit reviewed here.</p>`;
}

Office.actions = Object.assign(Office.actions || {}, {
  "coder-merge": async () => {
    const run = TaskRoom.run;
    if (!run) return;
    if (!window.confirm("Merge this branch into your own branch, on your Mac? Only the commit reviewed here is merged.")) return;
    const done = await attempt(() => api(`/runs/${run.id}/coder/merge`, { method: "POST" }), "Asked your Mac to merge it.");
    if (done) rerender();
  },
  "coder-discard": async () => {
    const run = TaskRoom.run;
    if (!run) return;
    if (!window.confirm("Discard this work? Its worktree and its office/ branch are removed from your Mac.")) return;
    const done = await attempt(() => api(`/runs/${run.id}/coder/discard`, { method: "POST" }), "Asked your Mac to discard it.");
    if (done) rerender();
  },
  "coder-send-back": () => {
    formModal(
      "Send it back",
      "coder-send-back-form",
      `<p class="muted">The same coding session on your Mac carries on, on the same branch, with what you write here. The office checks it again when it finishes.</p>
       ${field("What should change", "notes", { type: "textarea", attrs: 'maxlength="4000" rows="5"' })}`,
      "Send back"
    );
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "coder-send-back-form": async (form) => {
    const run = TaskRoom.run;
    const notes = String(new FormData(form).get("notes") || "").trim();
    if (!run || !notes) return;
    const done = await attempt(() => api(`/runs/${run.id}/coder/send-back`, { method: "POST", body: { notes } }), "Sent back to your Mac.");
    if (!done) return;
    closeModal();
    rerender();
  },
});

// Provider cards and failover, on Settings → Models.
//
// One card per provider the office can use: who uses
// it, what it has left where the provider says, its last call and errors in
// the last hour, the backup the owner chose, and whether it is set aside now.

const PROVIDER_REASON = {
  no_credit: "out of credit",
  key_refused: "its key was refused",
  failing: "kept failing",
};
const PROVIDER_STATE = {
  out_of_credit: ["Out of credit", "warn"],
  failed_over: ["Failed over", "warn"],
  not_ready: ["Not ready", "warn"],
};

function providerInitial(name) {
  return esc(String(name || "?").trim().charAt(0).toUpperCase() || "?");
}

function providerBalance(card) {
  const reading = card.balance;
  if (!reading) return "Not published by this provider";
  // Said in words where the provider keeps it to itself (an admin key, say).
  if (reading.state === "unsupported") return esc(reading.reason || "Not published by this provider");
  if (reading.state !== "ok") return "Could not be read just now";
  if (reading.total != null) return `${Number(reading.total).toFixed(2)} ${esc(reading.currency || "")} left`;
  if (reading.remaining != null && reading.limit != null) return `$${Number(reading.remaining).toFixed(2)} of $${Number(reading.limit).toFixed(2)} left`;
  if (reading.remaining != null) return `$${Number(reading.remaining).toFixed(2)} left on the key`;
  return "No limit on this key";
}

function providerCard(card, providers) {
  const state = PROVIDER_STATE[card.needs_you];
  const chip = state
    ? `<span class="pv-chip is-warn">${esc(state[0])}</span>`
    : card.used_by.length
      ? `<span class="pv-chip is-on">In use</span>`
      : `<span class="pv-chip">Ready</span>`;
  const used = card.used_by.length
    ? `<div class="pv-roles" aria-label="Used by">${card.used_by.map((name) => `<span class="pv-chip">${esc(name)}</span>`).join("")}</div>`
    : `<p class="pv-note">Not used by a role.${providers.some((p) => p.backup.key === card.key) ? " A backup for " + esc(providers.filter((p) => p.backup.key === card.key).map((p) => p.name).join(", ")) + "." : ""}</p>`;
  const backup = card.backup.key
    ? `${esc(card.backup.name || "a provider")}${card.backup.come_back ? "" : " · stays there"}`
    : `<span class="pv-missing">No backup yet</span>`;
  const moved = card.failed_over
    ? `<div class="pv-moved" role="status">Set aside${card.failed_over.since ? ` since ${esc(when(card.failed_over.since))}` : ""}: ${esc(PROVIDER_REASON[card.failed_over.reason] || card.failed_over.reason)}. ${card.backup.key ? `Its work goes to <strong>${esc(card.backup.name)}</strong>; budgets do not change.` : "With no backup, its tasks stop and say so."}</div>`
    : card.needs_you === "out_of_credit" && card.backup.key
      ? `<div class="pv-moved" role="status">Out of credit. Its work goes to <strong>${esc(card.backup.name)}</strong>, your backup; budgets do not change.</div>`
      : "";
  const actions = [
    btn("Use for roles…", `provider-roles:${card.key}`, card.used_by.length ? "" : "primary"),
    btn(card.backup.key ? "Change backup" : "Choose a backup", `provider-backup:${card.key}`, card.needs_you && !card.backup.key ? "primary" : ""),
    btn("Check it now", `provider-check:${card.key}`),
  ].join("");
  return `<article class="pv-card${state ? " is-warn" : card.used_by.length ? " is-on" : ""}" data-provider="${esc(card.key)}">
    <div class="pv-top"><span class="pv-mark" aria-hidden="true">${providerInitial(card.name)}</span>
      <div class="pv-name"><h3>${esc(card.name)}</h3><small>${esc(`${card.provider} · ${card.model}`)}</small></div>${chip}</div>
    ${used}
    <dl class="pv-facts">
      <div><dt>Balance</dt><dd>${providerBalance(card)}</dd></div>
      <div><dt>Last call</dt><dd>${card.last_call_at ? esc(when(card.last_call_at)) : "Not yet"}</dd></div>
      <div><dt>Errors, last hour</dt><dd>${esc(card.last_hour.errors)} of ${esc(card.last_hour.calls)}</dd></div>
      <div><dt>If it fails</dt><dd>${backup}</dd></div>
    </dl>
    ${moved}
    <div class="button-row tight pv-actions">${actions}</div>
  </article>`;
}

function providersView(data, models, office) {
  if (!data) return `<div class="card"><p class="muted">The providers could not be read just now.</p></div>`;
  const providers = data.providers || [];
  const summary = data.summary || {};
  const limit = summary.monthly_limit_usd;
  const usable = models.filter((m) => ["MODEL_AVAILABLE", "VERIFIED"].includes(m.status));
  const chosen = office.settings.role_models || {};
  const choices = [["", "No model yet"], ...usable.map((m) => [m.id, `${m.display_name} · ${m.model_id}`])];
  const without = data.without_model || [];
  const missing = without.length
    ? `<div class="pv-moved" role="status">No model yet for <strong>${esc(without.join(", "))}</strong>: their work stops until you choose one below.</div>`
    : "";
  return `${missing}<section class="pv-summary" aria-label="This month">
      <div><span>Spent this month</span><strong>$${Number(summary.spent_this_month_usd || 0).toFixed(2)}</strong><small>${limit ? `of your $${Number(limit).toFixed(2)} limit` : "no monthly limit set"}</small></div>
      <div><span>Model calls today</span><strong>${esc(summary.calls_today || 0)}</strong><small>across ${esc(providers.length)} provider${providers.length === 1 ? "" : "s"}</small></div>
      <div><span>Failed over this week</span><strong>${esc(summary.failovers_week || 0)}</strong><small>tasks moved to a backup</small></div>
      <div><span>Needs you</span><strong class="${summary.needs_you ? "pv-warn" : ""}">${esc(summary.needs_you || 0)}</strong><small>${summary.needs_you ? "see the marked cards" : "nothing to do"}</small></div>
    </section>
    <section class="pv-grid" aria-label="Providers">
      ${providers.map((card) => providerCard(card, providers)).join("")}
      <a class="pv-add" href="#connections"><strong>Add a provider</strong><span>Pick one, paste its key, and the office checks it.</span></a>
    </section>
    <section class="card pv-who" aria-label="Who uses what">
      <div class="card-top"><h2>Who uses what</h2><small>New tasks use these. An agent that names its own model keeps it.</small></div>
      <form id="role-models-form" class="pv-roles-form">
        ${BUILT_IN.map(([role, name]) => `<div class="pv-role"><label for="f-role_${role}"><strong>${esc(name)}</strong></label>
          <select id="f-role_${role}" name="role_${role}">${choices.map(([value, label]) => `<option value="${esc(value)}" ${(chosen[role] || "") === value ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></div>`).join("")}
        <div class="button-row"><button class="primary">Save</button></div>
      </form>
    </section>
    ${providerUsageView(Office.providerUsage)}`;
}

// Calls, tokens, cost, errors and failovers per provider, over a period.
const PROVIDER_PERIODS = [[1, "Today"], [7, "7 days"], [30, "30 days"]];
const PROVIDER_COLORS = ["#284e3d", "#c98a4b", "#5f8fa6", "#8a6fb0", "#9baf86"];

function providerUsageView(usage) {
  if (!usage) return "";
  const days = Settings.providerDays || 7;
  const rows = usage.providers || [];
  const keys = rows.map((row) => row.key);
  const color = (key) => PROVIDER_COLORS[Math.max(0, keys.indexOf(key)) % PROVIDER_COLORS.length];
  const peak = Math.max(0.0001, ...(usage.days || []).map((day) => Object.values(day.by_provider).reduce((a, b) => a + b, 0)));
  const money = (value) => `$${Number(value || 0).toFixed(2)}`;
  const table = rows.length
    ? `<div class="pv-table-frame"><table class="pv-table"><caption>By provider</caption>
        <thead><tr><th scope="col">Provider · model</th><th scope="col" class="num">Calls</th><th scope="col" class="num">Tokens</th><th scope="col" class="num">Cost</th><th scope="col" class="num">Errors</th><th scope="col" class="num">Failed over</th></tr></thead>
        <tbody>${rows.map((row) => `<tr><td><span class="pv-dot" style="background:${color(row.key)}"></span><strong>${esc(row.name)}</strong><br><small>${esc(row.model)}</small></td><td class="num">${esc(row.calls)}</td><td class="num">${Number(row.tokens).toLocaleString()}</td><td class="num">${money(row.cost_usd)}</td><td class="num">${esc(row.errors)}</td><td class="num">${row.failovers_out ? `${esc(row.failovers_out)} out` : ""}${row.failovers_in ? ` ${esc(row.failovers_in)} in` : ""}${row.failovers_out || row.failovers_in ? "" : "0"}</td></tr>`).join("")}</tbody></table></div>`
    : `<p class="muted">No model calls in this period.</p>`;
  const bars = (usage.days || []).length
    ? `<div class="pv-bars" role="img" aria-label="Cost per day, by provider">${usage.days
        .map((day) => {
          const total = Object.values(day.by_provider).reduce((a, b) => a + b, 0);
          const parts = Object.entries(day.by_provider).map(([key, cost]) => `<span style="flex:${cost || 0.0001};background:${color(key)}" title="${esc(usage.names[key] || key)}: ${money(cost)}"></span>`).join("");
          return `<div class="pv-bar" title="${esc(day.day)}: ${money(total)}"><div style="height:${Math.max(2, Math.round((total / peak) * 100))}%">${parts}</div><small>${esc(day.day.slice(5))}</small></div>`;
        })
        .join("")}</div>`
    : `<p class="muted">Nothing spent in this period.</p>`;
  const happened = (usage.happened || []).length
    ? `<ul class="pv-happened">${usage.happened.map((line) => `<li><span>${esc(when(line.at))}</span>${esc(line.text)}</li>`).join("")}</ul>`
    : `<p class="muted">No provider ran short or failed over in this period.</p>`;
  return `<section class="card pv-usage" id="provider-usage" aria-label="Usage by provider">
      <div class="card-top"><h2>Usage by provider</h2>
        <div class="ed-views" role="group" aria-label="Period">${PROVIDER_PERIODS.map(([value, label]) => `<button type="button" data-action="provider-days:${value}" aria-pressed="${value === days}">${esc(label)}</button>`).join("")}</div></div>
      ${table}
      <div class="grid2 pv-usage-grid">
        <div><h3>Cost by day</h3>${bars}</div>
        <div><h3>What happened</h3>${happened}</div>
      </div>
    </section>`;
}

function backupDialog(card, providers) {
  const others = providers.filter((p) => p.key !== card.key);
  const current = card.backup.key || "";
  const option = (value, title, detail) =>
    `<label class="pv-option${current === value ? " is-chosen" : ""}"><input type="radio" name="backup" value="${esc(value)}" ${current === value ? "checked" : ""}><span><strong>${esc(title)}</strong><small>${esc(detail)}</small></span></label>`;
  return `<form id="provider-backup-form" data-key="${esc(card.key)}">
    <p>A backup you choose now. The office never picks one for you.</p>
    <fieldset class="pv-fieldset"><legend>Go to</legend>
      ${others.map((p) => option(p.key, p.name, `${p.provider} · ${p.model}${p.usable ? "" : " · not ready"}`)).join("")}
      ${option("", "No backup", "Its tasks stop and say why, as they do today")}
    </fieldset>
    <p class="pv-when">It moves when ${esc(card.name)} runs out of credit, refuses its key, or fails three calls in a row (an outage, rate limits, timeouts). A task's own budget, a refusal or a malformed answer never move it.</p>
    <label class="approval-check"><input type="checkbox" name="come_back" ${card.backup.come_back !== false ? "checked" : ""}><span>Come back to ${esc(card.name)} when it answers again<br><small>Tried again every five minutes</small></span></label>
    <div class="note">A task that moves keeps its own budget and limits, and says on its page which model it moved to and why. A backup never raises what a task may spend.</div>
    <div class="button-row"><button type="button" data-action="close">Cancel</button><button class="primary">Save backup</button></div>
  </form>`;
}

Office.actions = Object.assign(Office.actions || {}, {
  "provider-backup": (key) => {
    const providers = Office.providers?.providers || [];
    const card = providers.find((p) => p.key === key);
    if (!card) {
      console.error("Provider not found:", key);
      return;
    }
    modal(`When ${card.name} fails`, backupDialog(card, providers), "");
  },
  "provider-days": (days) => {
    Settings.providerDays = Number(days) || 7;
    rerender();
  },
  // One press: the research runs again, once, as the owner's own revision.
  "providers-retry": async () => {
    const run = TaskRoom.run;
    if (!run) return;
    const body = { action: "revise", revision_target: "research", comment: "Try the research again; the model providers were unavailable." };
    const sent = await attempt(() => api(`/runs/${run.id}/review`, { method: "POST", body }), "Trying the research again.");
    if (sent !== undefined) rerender();
  },
  "go-providers": () => {
    Settings.tab = "models";
    location.hash = "settings";
  },
  "provider-roles": () => {
    document.getElementById("role-models-form")?.scrollIntoView({ behavior: "smooth", block: "center" });
    document.getElementById("f-role_chief")?.focus();
  },
  "provider-check": async (id) => {
    const checked = await attempt(() => api(`/connections/${id}/test`, { method: "POST", body: { billable: false } }), "Checked with the provider.");
    if (checked !== undefined) rerender();
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "provider-backup-form": async (form) => {
    const key = form.dataset.key;
    const data = new FormData(form);
    const backup = String(data.get("backup") || "") || null;
    const body = { backup, come_back: data.get("come_back") === "on" };
    const saved = await attempt(() => api(`/providers/${encodeURIComponent(key)}/backup`, { method: "PUT", body }), backup ? "Backup saved." : "No backup: its tasks stop and say why.");
    if (saved === undefined) return;
    closeModal();
    rerender();
  },
});

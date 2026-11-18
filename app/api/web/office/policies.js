"use strict";

// The office's rules. Each one narrows what the office may do; none widens it.
// Approval before anything leaves the office, path confinement and skill
// permissions apply whatever is set here.

const Policies = { available: [], configured: [], projects: [] };

function policyParams(kind, params = {}) {
  const fields = Object.entries(kind.parameters || {});
  if (!fields.length) return `<p class="muted hint">Nothing to set.</p>`;
  return fields
    .map(([name, spec]) => {
      const value = params[name];
      if (spec.type === "number") {
        return field(name.replace(/_/g, " "), `param_${name}`, {
          type: "number",
          required: false,
          value: value ?? spec.default ?? "",
          attrs: 'min="1"',
        });
      }
      return field(name.replace(/_/g, " "), `param_${name}`, {
        required: false,
        value: Array.isArray(value) ? value.join(", ") : (value ?? ""),
        attrs: `placeholder="${esc(spec.example || "")}"`,
        hint: spec.of === "host" ? "One host per line or comma separated." : "",
      });
    })
    .join("");
}

function readParams(form, kind) {
  const data = new FormData(form);
  const params = {};
  for (const [name, spec] of Object.entries(kind.parameters || {})) {
    const raw = String(data.get(`param_${name}`) || "").trim();
    if (!raw) continue;
    if (spec.type === "number") params[name] = Number(raw);
    else if (spec.type === "list") params[name] = raw.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
    else params[name] = raw;
  }
  return params;
}

async function renderPolicies(content) {
  const [state, projects, records] = await Promise.all([
    api("/policies"),
    api("/projects"),
    api("/policies/records?limit=20").catch(() => []),
  ]);
  Policies.available = state.available;
  Policies.configured = state.policies;
  Policies.projects = projects.filter((p) => p.status === "ACTIVE");
  const byKind = Object.fromEntries(state.available.map((kind) => [kind.id, kind]));
  const inForce = state.policies
    .map((row) => {
      const kind = byKind[row.kind] || { title: row.kind, about: "", parameters: {} };
      const where = row.scope === "project" ? `in ${esc(row.project || "one project")}` : "everywhere";
      const settings = Object.entries(row.params || {})
        .map(([name, value]) => `${name.replace(/_/g, " ")}: ${Array.isArray(value) ? value.join(", ") : value}`)
        .join(" · ");
      return `<div class="setting-row"><div>
          <h3>${esc(kind.title)} ${row.enabled ? "" : tag("off", "gray")}</h3>
          <p>${esc(kind.about)}</p>
          <p class="muted">Applies ${where}${settings ? ` · ${esc(settings)}` : ""}</p>
        </div>
        <div class="button-row tight">${btn(row.enabled ? "Turn off" : "Turn on", `policy-toggle:${row.id}:${row.enabled ? "off" : "on"}`)}${btn("Remove", `policy-remove:${row.id}`, "danger")}</div>
      </div>`;
    })
    .join("");
  const offer = state.available
    .map(
      (kind) => `<div class="card"><div class="card-top"><span class="connector-icon">⚖</span>${tag(kind.hooks.includes("model_call") ? "before a model call" : "before a tool call", "gray")}</div>
        <h3>${esc(kind.title)}</h3><p>${esc(kind.about)}</p>
        <div class="button-row">${btn("Turn on", `policy-add:${kind.id}`, "primary")}</div></div>`
    )
    .join("");
  const decisions = records.length
    ? records
        .map(
          (row) => `<div class="detail-line"><span>${esc(row.policy || "policy")} · ${esc(row.target)}<br><small class="muted">${esc(row.reason || "")}</small></span><strong>${esc(row.verdict)}<br><small class="muted">${esc(when(row.created_at))}</small></strong></div>`
        )
        .join("")
    : `<p class="muted">No decisions yet. Refusals and pauses are recorded here with their reason.</p>`;
  content.innerHTML =
    heading("RULES", "What the office may do.", "Each rule narrows what agents may do. Nothing here widens it: approval before anything leaves the office, folder confinement and skill permissions always apply.") +
    `<div class="grid2">
      <div class="card"><h2>In force</h2>${inForce || `<p class="muted">No rules yet. The office's built-in limits still apply.</p>`}</div>
      <div class="card"><h2>Recent decisions</h2>${decisions}</div>
    </div>
    <h2 class="spaced">Rules you can turn on</h2>
    <div class="grid3">${offer}</div>`;
}

Office.screens.policies = { title: "Rules", render: renderPolicies };

Office.actions = Object.assign(Office.actions || {}, {
  "policy-add": (kindId) => {
    const kind = Policies.available.find((item) => item.id === kindId);
    if (!kind) return;
    Policies.adding = kind;
    formModal(
      kind.title,
      "policy-form",
      `<p>${esc(kind.about)}</p>
       ${field("Where it applies", "scope", {
         type: "select",
         choices: [["office", "Everywhere in this office"], ...Policies.projects.map((p) => [p.id, `Only in ${p.name}`])],
       })}
       ${policyParams(kind)}`,
      "Turn it on"
    );
  },
  "policy-toggle": async (arg) => {
    const [id, state] = arg.split(":");
    if ((await attempt(() => api(`/policies/${id}`, { method: "PUT", body: { enabled: state === "on" } }), state === "on" ? "Rule on." : "Rule off.")) !== undefined) rerender();
  },
  "policy-remove": async (id) => {
    if (!window.confirm("Remove this rule? What it refused before will be allowed again.")) return;
    if ((await attempt(() => api(`/policies/${id}`, { method: "DELETE" }), "Removed.")) !== undefined) rerender();
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "policy-form": async (form) => {
    const kind = Policies.adding;
    if (!kind) return;
    const scope = String(new FormData(form).get("scope") || "office");
    const body = {
      kind: kind.id,
      scope: scope === "office" ? "office" : "project",
      params: readParams(form, kind),
    };
    if (scope !== "office") body.project_id = scope;
    if ((await attempt(() => api("/policies", { method: "POST", body }), "Rule is in force.")) === undefined) return;
    closeModal();
    rerender();
  },
});

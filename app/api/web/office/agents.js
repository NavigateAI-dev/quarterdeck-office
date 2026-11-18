"use strict";

// Agents (profiles, versions, models, skills, Slack bots) and Skills.

// The roles the office's workflow uses. An agent in one of these roles takes
// part in every task; any other role is the owner's own and works through its
// own chat bot only.
const BUILT_IN = [
  ["chief", "Chief", "Turns your objective into a plan, then writes the final summary."],
  ["researcher", "Researcher", "Finds evidence, reads the sources, and cites them."],
  ["strategist", "Strategist", "Turns the evidence into the deliverable you asked for."],
  ["critic", "Critic", "Checks the work against the plan before it reaches you."],
  ["advisor", "Advisor", "On call when you turn it on: reads the whole task before the plan, when a failure repeats and before it is done, and advises the Chief."],
];
const ROLES = BUILT_IN.map(([id, name, about]) => [id, `${name} — ${about.replace(/\.$/, "").toLowerCase()}`]);
const AgentState = { agents: [], connections: [], skills: [], skillTab: "discover" };

async function loadAgentData() {
  const [agents, connections, skills, office] = await Promise.all([
    api("/agents"),
    api("/connections"),
    api("/skills"),
    api("/settings").catch(() => null),
  ]);
  AgentState.agents = agents;
  AgentState.connections = connections;
  AgentState.skills = skills;
  // What each role falls back to, so "office default" names a real model.
  const chosen = office?.settings?.role_models || {};
  AgentState.roleModels = Object.fromEntries(
    Object.entries(chosen).map(([role, id]) => {
      const connection = connections.find((c) => c.id === id);
      return [role, connection ? `${connection.display_name} · ${connection.model_id}` : "a chosen model"];
    })
  );
}

function enabledVersion(agent) {
  return agent.versions.find((v) => v.version === agent.enabled_version) || agent.versions[agent.versions.length - 1];
}
function connectionLabel(id, role) {
  const c = AgentState.connections.find((row) => row.id === id);
  if (c) return `${c.display_name} · ${c.model_id}`;
  const fallback = AgentState.roleModels?.[role];
  return fallback ? `Office's model for this role · ${fallback}` : "No model chosen for this role yet";
}
function skillNames(pins) {
  return (pins || []).map((p) => AgentState.skills.find((s) => s.skill_id === p.skill_id && s.version === p.version)?.name || p.skill_id).join(", ") || "None";
}

async function renderAgents(content) {
  await loadAgentData();
  const active = AgentState.agents.filter((a) => a.status === "ACTIVE");
  const archived = AgentState.agents.filter((a) => a.status !== "ACTIVE");
  const card = (agent, i) => {
    const version = enabledVersion(agent) || {};
    const tested = AgentState.connections.find((c) => c.id === version.connection_id);
    return `<div class="card"><div class="card-top">${avatar(agent.name, i)}${agent.status === "ACTIVE" ? tag("Available") : tag("Archived", "gray")}</div>
      <h2>${esc(agent.name)}</h2><small>${esc(ROLE_LABEL[agent.role] || agent.role)}</small>
      <p class="clamp">${esc(version.instructions || "Uses the built-in instructions for its role.")}</p>
      <div class="detail-line"><span>Model</span><strong>${esc(connectionLabel(version.connection_id, agent.role))}</strong></div>
      <div class="detail-line"><span>Skills</span><strong>${esc(skillNames(version.skills))}</strong></div>
      <div class="detail-line"><span>Version</span><strong>v${esc(agent.enabled_version)} of ${agent.versions.length}</strong></div>
      <div class="detail-line"><span>External writes</span><strong>Your approval</strong></div>
      ${
        agent.status === "ACTIVE"
          ? `<div class="button-row">${btn("Open console →", `agent-open:${agent.id}`, "primary")}${btn("Slack bot", `bot:${agent.id}`)}${tested ? btn("Test", `test-agent:${agent.id}`) : ""}<a class="button" href="/agents/${esc(agent.id)}/export" download>Export</a></div>`
          : ""
      }</div>`;
  };
  content.innerHTML =
    heading(
      "YOUR WORKING GROUP",
      "Good work starts with good agents.",
      "Give each agent a role, the right model and skills, and clear boundaries.",
      `<div class="button-row">${btn("Import from a file", "agent-import")}${btn("＋ Create agent", "new-agent", "primary")}</div>`
    ) +
    (active.length
      ? `<div class="grid3">${active.map(card).join("")}</div>`
      : emptyCard("No agents yet.", "The built-in roles already do the work. Create an agent to give a role your own instructions, a model you chose, and skills.", btn("＋ Create agent", "new-agent", "primary"))) +
    `<h2 class="spaced">Roles in every task</h2><div class="grid3">${BUILT_IN.map(([role, name, about], i) => {
      const mine = active.filter((a) => a.role === role);
      return `<div class="card"><div class="card-top">${avatar(name, i)}${mine.length ? tag(`${mine.length} of your agents`) : tag("Built-in", "gray")}</div>
        <h3>${esc(name)}</h3><p>${esc(about)}</p>
        <p class="muted">${mine.length ? esc(`Your instructions apply: ${mine.map((a) => a.name).join(", ")}.`) : "Runs on the office's own instructions and model."}</p>
        <div class="button-row">${btn(mine.length ? "Add another" : "Make it mine", `new-agent:${role}`)}</div></div>`;
    }).join("")}</div>
    <div class="note spaced">Each task snapshots the enabled version of every agent, so changing an agent never alters work already in progress. An agent in a role above joins every task in that role; an agent in a role you invent works through its own Slack bot only.</div>` +
    (archived.length ? `<h2 class="spaced">Archived</h2><div class="grid3">${archived.map(card).join("")}</div>` : "");
}

const CONNECTION_REASON = {
  CREDENTIAL_MISSING: "no key stored yet",
  PROVIDER_TEST_PENDING: "not checked with the provider yet",
  MODEL_UNAVAILABLE: "the provider does not offer this model to your key",
  CREDENTIAL_STORE_UNAVAILABLE: "the key store is unavailable",
};

function agentFields(agent) {
  const version = agent ? enabledVersion(agent) : {};
  const ready = (c) => ["MODEL_AVAILABLE", "VERIFIED"].includes(c.status);
  const usable = AgentState.connections.filter(ready);
  const waiting = AgentState.connections.filter((c) => !ready(c));
  const roleDefault = AgentState.roleModels?.[agent ? agent.role : "researcher"];
  const defaultLabel = roleDefault
    ? `The office's model for this role · ${roleDefault}`
    : "The office's model for this role (none chosen yet)";
  const chosen = new Set((version.skills || []).map((p) => `${p.skill_id}@${p.version}`));
  const enabled = AgentState.skills.filter((s) => s.status === "ENABLED");
  return `${
    agent
      ? ""
      : `${field("Agent name", "name", { attrs: 'maxlength="255"' })}${field("Role", "role", { type: "select", choices: [...ROLES, ["__custom", "A role I define…"]], value: AgentState.newRole || "researcher" })}
         <div id="custom-role-row" hidden>${field("Role name", "custom_role", { required: false, attrs: 'maxlength="64" placeholder="Slack communication assistant"', hint: "Write it in words. A role of your own does not join office tasks; give it a Slack bot to work with it." })}<p class="muted" id="custom-role-saved" aria-live="polite"></p></div>`
  }
    ${field("Instructions", "instructions", { type: "textarea", required: false, value: version.instructions || "", attrs: 'maxlength="32768" rows="5"', hint: "Added to the role's built-in instructions. The role's output format and checks still apply." })}
    ${field("Model", "connection", {
      type: "select",
      required: false,
      value: version.connection_id || "",
      choices: [
        ["", defaultLabel],
        ...usable.map((c) => [c.id, `${c.display_name} · ${c.model_id}`]),
        ...waiting.map((c) => [c.id, `${c.display_name} · ${c.model_id} — ${CONNECTION_REASON[c.status] || c.status.toLowerCase()}`, true]),
      ],
      hint: AgentState.connections.length
        ? waiting.length
          ? `${waiting.length === 1 ? "One model is" : `${waiting.length} models are`} not ready yet: store the key in Connections and run the free check.`
          : "Models come from Connections."
        : "No model connections yet. Add one under Connections, or leave this on the office's model.",
    })}
    <fieldset class="skill-picks"><legend class="field">Skills</legend>${
      enabled.length
        ? enabled
            .map((s) => `<label class="check"><input type="checkbox" name="skill" value="${esc(`${s.skill_id}@${s.version}`)}" ${chosen.has(`${s.skill_id}@${s.version}`) ? "checked" : ""}> ${esc(s.name)} <span class="muted">v${esc(s.version)}</span></label>`)
            .join("")
        : `<p class="muted">No enabled skills yet. Install one under Skills.</p>`
    }</fieldset>`;
}

function versionBody(form) {
  const data = new FormData(form);
  return {
    instructions: String(data.get("instructions") || ""),
    connection_id: data.get("connection") || null,
    skills: data.getAll("skill").map((value) => {
      const [skill_id, version] = String(value).split("@");
      return { skill_id, version };
    }),
  };
}

function openConfigure(id) {
  const agent = AgentState.agents.find((a) => a.id === id);
  if (!agent) return;
  AgentState.editing = agent;
  const history = agent.versions
    .slice()
    .reverse()
    .map(
      (v) => `<div class="detail-line"><span>v${v.version} · ${esc(when(v.created_at))}${v.version === agent.enabled_version ? " · in use" : ""}</span>${v.version === agent.enabled_version ? "" : btn("Use this version", `enable-version:${agent.id}:${v.version}`)}</div>`
    )
    .join("");
  formModal(
    `Configure ${agent.name}`,
    "agent-version-form",
    `${agentFields(agent)}<p class="muted">Saving creates version ${agent.versions.length + 1} and uses it for new tasks.</p><details><summary>Version history</summary>${history}</details>`,
    "Save new version"
  );
}

async function openBot(id) {
  const agent = AgentState.agents.find((a) => a.id === id);
  const bot = await attempt(() => api(`/agents/${id}/chat-bot/slack`));
  if (!agent || !bot) return;
  AgentState.botAgent = agent;
  const install = bot.installation;
  const state = install
    ? `Connected as ${install.bot_user_id} in ${install.workspace_name}.${bot.app_token_configured ? "" : " Add the app-level token so it can receive messages."}${install.owner_external_id ? " You are paired in this workspace." : " Pair from Connections → Slack."}`
    : "Not connected yet.";
  modal(
    `${agent.name}'s Slack bot`,
    `<div class="approval-preview"><h3>${install ? "Connected" : "Set up in two steps"}</h3><p>${esc(state)}</p></div>
     <p><strong>1.</strong> <a href="${esc(bot.create_app_url)}" target="_blank" rel="noopener">Create this agent's Slack app ↗</a> (pre-filled), then <em>Install to Workspace</em> and copy the Bot User OAuth Token (xoxb-). Under Basic Information → App-Level Tokens, generate one with <code>connections:write</code> (xapp-).</p>
     <form id="agent-bot-form"><p><strong>2.</strong> Paste the tokens here. They are stored encrypted and never shown again.</p>
       ${field("Bot token (xoxb-…)", "bot_token", { type: "password", required: false, attrs: 'autocomplete="off"' })}
       ${field("App-level token (xapp-…)", "app_token", { type: "password", required: false, attrs: 'autocomplete="off"' })}
       <div class="button-row">${install || bot.app_token_configured ? btn("Disconnect bot", `bot-disconnect:${id}`, "danger") : ""}${btn("Close", "close")}<button class="primary">Save tokens</button></div>
     </form>
     <p class="muted">A researcher's bot researches requests itself; other agents' bots propose a full office task. Work starts only after you reply "yes".</p>`,
    ""
  );
}

async function renderSkills(content) {
  const [catalog, installed, updates] = await Promise.all([
    api("/skills/catalog").catch(() => []),
    api("/skills"),
    api("/skills/updates").catch(() => []),
  ]);
  AgentState.catalog = catalog;
  AgentState.skills = installed;
  const updateOf = new Map((updates || []).map((u) => [u.skill_id, u]));
  const key = new Map(installed.map((s) => [`${s.skill_id}@${s.version}`, s]));
  const discover = catalog
    .map((skill) => {
      const latest = skill.versions[skill.versions.length - 1];
      const have = key.get(`${skill.skill_id}@${latest.version}`);
      return `<div class="card"><div class="card-top"><span class="connector-icon">✧</span>${tag(latest.publisher || "Curated", "gray")}</div>
        <h2>${esc(skill.name)}</h2><p>${esc(latest.description)}</p>
        <small>REQUESTED CAPABILITIES</small><p>${esc(latest.permissions.length ? latest.permissions.join(", ") : "Instructions only · no tools")}</p>
        <div class="button-row">${have && have.status !== "UNINSTALLED" ? tag(`Installed · ${have.status.toLowerCase().replace("_", " ")}`) : btn("Review & install", `skill-review:${skill.skill_id}`, "primary")}</div></div>`;
    })
    .join("");
  const mine = installed
    .filter((s) => s.status !== "UNINSTALLED")
    .map((s) => {
      const path = `${s.skill_id}:${s.version}`;
      const actions =
        s.status === "NEEDS_REVIEW"
          ? btn("Review permissions", `skill-approve:${path}`, "primary")
          : s.status === "ENABLED"
            ? btn("Disable", `skill-toggle:${path}:disable`)
            : btn("Enable", `skill-toggle:${path}:enable`, "primary");
      const from = s.source ? `<p class="muted">From <a href="${esc(s.source.skills_sh)}" target="_blank" rel="noopener noreferrer">${esc(s.source.repo)}</a> at ${esc(String(s.source.commit).slice(0, 7))}</p>${auditTags(s.audits)}` : "";
      // Where it is offered, and whether its author has improved it.
      const where = `<p class="muted" data-skill-lines>${s.lines?.length ? `Offered in ${esc(s.lines.join(", "))} only` : "Offered to every task"}</p>`;
      const update = updateOf.get(s.skill_id);
      const current = update && s.source && update.installed_commit === s.source.commit;
      const news =
        current && update.state === "available"
          ? `<div class="skill-update" data-skill-update>${tag("Update available", "amber")}<p>${esc(update.message || "Changed")}${update.latest_date ? ` · ${esc(when(update.latest_date))}` : ""}</p>${btn("See the update", `skill-update:${s.skill_id}`, "primary")}</div>`
          : current && update.state === "unreachable"
            ? `<p class="muted">Could not check GitHub for updates (${esc(update.reason || "unreachable")}).</p>`
            : "";
      return `<div class="card"><div class="card-top"><span class="connector-icon">✧</span>${tag(s.status === "ENABLED" ? "Enabled" : s.status === "NEEDS_REVIEW" ? "Needs your review" : "Disabled", s.status === "ENABLED" ? "" : s.status === "NEEDS_REVIEW" ? "amber" : "gray")}</div>
        <h2>${esc(s.name)}</h2><small>v${esc(s.version)} · for ${esc((s.roles || []).map((r) => ROLE_LABEL[r] || r).join(", ") || "any role")}</small>${from}
        <p>${esc(s.permissions.length ? `Uses: ${s.permissions.join(", ")}` : "Instructions only · no tools")}</p>
        ${where}${news}
        <div class="button-row">${actions}${btn("Uninstall", `skill-uninstall:${path}`, "danger")}</div></div>`;
    })
    .join("");
  const count = installed.filter((s) => s.status !== "UNINSTALLED").length;
  const tab = AgentState.skillTab;
  content.innerHTML =
    heading(
      "CAPABILITIES",
      "A little expertise goes a long way.",
      "Reusable instructions for your agents. Reviewed permissions, before installation.",
      `<div class="button-row">${btn("Check for updates", "skill-check")}${btn("Add from skills.sh", "skill-source", "primary")}</div>`
    ) +
    `<div class="subnav" role="tablist">${btn("Discover", "skill-tab:discover", tab === "discover" ? "active" : "", 'role="tab"')}${btn(`Installed (${count})`, "skill-tab:installed", tab === "installed" ? "active" : "", 'role="tab"')}</div>
     <div class="grid3">${tab === "discover" ? discover || emptyCard("The catalog is empty.", "Curated skills appear here.") : mine || emptyCard("No skills installed.", "Install one from Discover, then add it to an agent.")}</div>
     <div class="note spaced">Skills are instructions, never code. A skill that asks for a capability runs only in tasks where that capability is available; otherwise it is skipped and the task room says so.</div>`;
}

// A role written in words, as the office keeps it: "Slack assistant" is
// slack-assistant. The same rule as the office's own (AgentCreate).
function roleName(text) {
  return String(text || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "-")
    .replace(/^[-_]+|[-_]+$/g, "")
    .replace(/^[^a-z]+/, "")
    .slice(0, 64)
    .replace(/[-_]+$/, "");
}

// ---- skills from skills.sh or GitHub ----

// skills.sh's audits, as it publishes them: information, not a verdict.
function auditTags(audits) {
  const names = { gen: "Gen", socket: "Socket", snyk: "Snyk" };
  const shown = Object.entries(audits || {}).map(([key, value]) => {
    const good = /pass|safe|^0 alerts|low risk/i.test(value);
    const bad = /fail|high risk/i.test(value);
    return tag(`${names[key] || key}: ${value}`, good ? "" : bad ? "clay" : "amber");
  });
  return shown.length ? `<p class="labels">${shown.join(" ")}</p>` : "";
}

const SKILL_ROLES = [
  ["researcher", "Researcher: while researching a step"],
  ["summarist", "Writer: while writing a report or document"],
  ["strategist", "Strategist: while writing a proposal"],
  ["critic", "Critic: while reviewing the work"],
];

function openSkillSource(prefill = "") {
  modal(
    "Add a skill from skills.sh",
    `<p>Paste a skills.sh link, a GitHub link, or <code>owner/repo skill-name</code>. The office reads the skill and shows you what it is before anything is installed.</p>
     ${field("Skill", "skill-source", { value: prefill, attrs: 'maxlength="400" placeholder="https://www.skills.sh/mattpocock/skills/to-questionnaire" autocomplete="off"' })}
     <div id="skill-preview"></div>`,
    `<div class="button-row">${btn("Read it", "skill-source-read", "primary")}</div>`
  );
}

async function readSkillSource() {
  const source = $("#f-skill-source")?.value.trim() || "";
  const holder = $("#skill-preview");
  if (!source || !holder) {
    toast("Paste a skills.sh link or owner/repo first.", true);
    return;
  }
  holder.innerHTML = `<p class="muted loading">Reading the skill…</p>`;
  let found;
  try {
    found = await api("/skills/ecosystem/preview", { method: "POST", body: { source } });
  } catch (error) {
    const detail = error?.detail;
    if (detail && detail.code && Array.isArray(detail.choices) && detail.choices.length) {
      holder.innerHTML = `<p>${detail.code === "choose_skill" ? "This repository holds several skills. Which one?" : "That skill is not there. These are:"}</p>
        <div class="button-row wrap">${detail.choices.map((name) => btn(name, `skill-source-pick:${name}`)).join("")}</div>`;
      return;
    }
    holder.innerHTML = `<p class="error-text">${esc(explain(error))}</p>`;
    return;
  }
  SkillIntake.found = { ...found, source };
  const list = (title, items, render) => (items?.length ? `<h3>${esc(title)}</h3><ul class="plain">${items.map(render).join("")}</ul>` : "");
  holder.innerHTML = `<div class="skill-preview">
      <h2>${esc(found.name)}</h2>
      <p>${esc(found.description || "No description.")}</p>
      <p class="muted">From <a href="${esc(found.skills_sh)}" target="_blank" rel="noopener noreferrer">${esc(found.source)}</a> · ${esc(found.path)} · pinned at ${esc(found.commit.slice(0, 7))} · ${esc(found.characters.toLocaleString())} characters</p>
      ${auditTags(found.audits) || `<p class="muted">skills.sh publishes no audit for it.</p>`}
      ${list("Not installed: code the office never runs", found.refused, (f) => `<li><code>${esc(f)}</code></li>`)}
      ${list("What it assumes the office cannot do", found.assumptions, (a) => `<li>${esc(a.text)}</li>`)}
      ${list("Left out for length", found.omitted, (f) => `<li>${esc(f)}</li>`)}
      <p class="muted">${found.permissions.length ? `It needs: ${esc(found.permissions.join(", "))}. It runs only where that is available.` : "It needs no tools: instructions only."}</p>
      ${field("Used by", "skill-role", { type: "select", value: found.suggested_role, choices: SKILL_ROLES })}
      <details><summary>Read its instructions</summary><pre class="skill-text">${esc(found.instructions)}</pre></details>
      <div class="button-row">${btn("Install this version", "skill-source-install", "primary")}</div>
    </div>`;
}

const SkillIntake = { found: null };

async function installSkillSource() {
  const found = SkillIntake.found;
  const role = $("#f-skill-role")?.value || "";
  if (!found || !role) {
    console.error("Nothing previewed to install", { found, role });
    return;
  }
  const done = await attempt(
    () => api("/skills/ecosystem/install", { method: "POST", body: { source: found.source, commit: found.commit, role } }),
    `Installed ${found.name}. The Chief can now use it in a step.`
  );
  if (!done) return;
  closeModal();
  AgentState.skillTab = "installed";
  rerender();
}

function openSkillReview(skillId) {
  const skill = AgentState.catalog?.find((s) => s.skill_id === skillId);
  if (!skill) return;
  const v = skill.versions[skill.versions.length - 1];
  const review = v.review || {};
  modal(
    `Install ${skill.name}`,
    `<p class="muted">${esc(v.publisher || "Curated")} · version ${esc(v.version)}</p>
     <div class="approval-preview"><h3>Requested capabilities</h3>${
       v.permissions.length ? `<ul class="plain">${v.permissions.map((p) => `<li>${esc(p)}</li>`).join("")}</ul><p>You approve these after installing, before any agent can use it.</p>` : "<p>None. Instructions only, no tools, no connector access.</p>"
     }</div>
     <div class="approval-preview"><h3>Suited to</h3><p>${esc((v.roles || []).map((r) => ROLE_LABEL[r] || r).join(", ") || "Any role")}</p>${
       Object.keys(review).length ? `<p class="muted">Review: ${esc(Object.entries(review).map(([k, x]) => `${k.replace(/_/g, " ")} ${x}`).join(" · "))}</p>` : ""
     }</div>
     <details><summary>Read the instructions</summary><pre class="artifact-text">${esc(v.instructions)}</pre></details>`,
    `<div class="button-row">${btn("Cancel", "close")}${btn("Install skill", `skill-install:${skill.skill_id}:${v.version}`, "primary")}</div>`
  );
}

function skillPath(arg) {
  const [skillId, version, action] = arg.split(":");
  return [`/skills/${skillId}/versions/${version}`, action];
}

Office.screens.agents = { title: "Agents", render: renderAgents };
// An update to a skill from GitHub: what changes, then the owner decides.
async function openSkillUpdate(skillId) {
  const preview = await attempt(() => api(`/skills/${encodeURIComponent(skillId)}/update/preview`, { method: "POST" }));
  if (!preview) return;
  AgentState.update = preview;
  const lines = preview.changes.length
    ? preview.changes.map((c) => `<div class="diff-line ${c.op === "+" ? "added" : "removed"}"><span>${c.op === "+" ? "+" : "−"}</span>${esc(c.text)}</div>`).join("")
    : `<p class="muted">The instructions are the same; only other files in its folder changed.</p>`;
  const asks = preview.permissions_added.length
    ? `<p class="answer-note">It now asks for: ${esc(preview.permissions_added.join(", "))}. It installs waiting for your review of that.</p>`
    : "";
  modal(
    `Update: ${preview.found?.name || skillId}`,
    `<p class="muted">From ${esc(preview.found?.source || "")} at ${esc(String(preview.commit).slice(0, 7))}. Tasks keep the version they started with.</p>${asks}
     <div class="diff" data-skill-diff>${lines}</div>`,
    `<div class="button-row">${btn("Not now", "close")}${btn("Install the update", "skill-update-install", "primary")}</div>`
  );
}

Office.screens.skills = { title: "Skills", render: renderSkills };

Office.actions = Object.assign(Office.actions || {}, {
  "skill-check": async () => {
    const checked = await attempt(() => api("/skills/updates/check", { method: "POST" }));
    if (checked === undefined) return;
    const available = checked.filter((u) => u.state === "available").length;
    toast(available ? `${available} ${available === 1 ? "skill has" : "skills have"} an update.` : "Every skill is up to date.");
    AgentState.skillTab = "installed";
    rerender();
  },
  "skill-update": (skillId) => openSkillUpdate(skillId),
  "skill-update-install": async () => {
    const preview = AgentState.update;
    if (!preview) return;
    const installed = await attempt(
      () => api("/skills/ecosystem/install", { method: "POST", body: { source: preview.source_text, commit: preview.commit, role: preview.role } }),
      preview.permissions_added.length ? "Installed. Review its new permissions to use it." : "Updated. New tasks use it."
    );
    if (installed === undefined) return;
    closeModal();
    rerender();
  },
  "new-agent": async (role) => {
    await attempt(loadAgentData);
    AgentState.editing = null;
    AgentState.newRole = role || "researcher";
    formModal("Create an agent", "agent-create-form", agentFields(null), "Create agent");
    const select = $("#f-role");
    const sync = () => {
      const row = $("#custom-role-row");
      const input = $("#f-custom_role");
      if (!row || !input || !select) return;
      row.hidden = select.value !== "__custom";
      input.required = select.value === "__custom";
    };
    select?.addEventListener("change", sync);
    sync();
    // Say what the role will be saved as, while it is typed.
    const custom = $("#f-custom_role");
    const saved = $("#custom-role-saved");
    custom?.addEventListener("input", () => {
      if (!saved) return;
      const name = roleName(custom.value);
      saved.textContent = custom.value.trim() ? (name ? `Saved as: ${name}` : "Use at least one letter.") : "";
    });
  },
  "agent-open": (id) => go("agent", id),
  "agent-import": () => {
    formModal(
      "Import an agent",
      "agent-import-form",
      `<p>Paste an agent file, or choose one. The office says what it would do before anything is created.</p>
       <label class="field" for="agent-file">Agent file</label>
       <input id="agent-file" type="file" accept=".yaml,.yml,text/yaml">
       ${field("Or paste it here", "document", { type: "textarea", required: false, attrs: 'rows="10" spellcheck="false"' })}
       <div id="import-plan"></div>`,
      "Check this file"
    );
    $("#agent-file")?.addEventListener("change", async (event) => {
      const file = event.target.files?.[0];
      const box = $("#f-document");
      if (file && box) box.value = await file.text();
    });
  },
  "agent-import-confirm": async () => {
    const document_text = AgentState.importing;
    if (!document_text) return;
    const done = await attempt(
      () => api("/agents/import", { method: "POST", body: { document: document_text, confirm: true } })
    );
    if (!done) return;
    closeModal();
    toast(`${done.agent.name} imported.`);
    go("agent", done.agent.id);
  },
  configure: openConfigure,
  "enable-version": async (arg) => {
    const [id, version] = arg.split(":");
    if ((await attempt(() => api(`/agents/${id}/versions/${version}/enable`, { method: "POST" }), `Now using version ${version}.`)) === undefined) return;
    closeModal();
    rerender();
  },
  archive: async (id) => {
    if (!window.confirm("Archive this agent? New tasks stop using it; its history is kept. Its Slack bot goes silent.")) return;
    if ((await attempt(() => api(`/agents/${id}/archive`, { method: "POST" }), "Archived.")) === undefined) return;
    if (Office.view === "agent") go("agents");
    else rerender();
  },
  "test-agent": async (id) => {
    if (!window.confirm("Send one short sample request to this agent's model? Your provider bills it.")) return;
    const result = await attempt(() => api(`/agents/${id}/test`, { method: "POST", body: { billable: true } }));
    if (result) modal("Test reply", `<p>v${esc(result.version)} on ${esc(result.model)} · ${esc(result.latency_seconds)}s</p><div class="approval-preview"><p>${esc(result.reply)}</p></div>`);
  },
  bot: openBot,
  "bot-disconnect": async (id) => {
    if (!window.confirm("Disconnect this Slack bot? Its stored tokens are deleted.")) return;
    if ((await attempt(() => api(`/agents/${id}/chat-bot/slack`, { method: "DELETE" }), "Disconnected.")) === undefined) return;
    openBot(id);
  },
  "skill-tab": (tab) => {
    AgentState.skillTab = tab;
    rerender();
  },
  "skill-review": openSkillReview,
  "skill-source": () => openSkillSource(),
  "skill-source-read": readSkillSource,
  "skill-source-install": installSkillSource,
  "skill-source-pick": (name) => {
    const input = $("#f-skill-source");
    if (!input) return;
    const base = input.value.trim().split(/\s+--skill|\s+/)[0].replace(/\/$/, "");
    input.value = /skills\.sh\//.test(base) ? `${base.split("/").slice(0, 5).join("/")}/${name}` : `${base} ${name}`;
    readSkillSource();
  },
  "skill-install": async (arg) => {
    const [skill_id, version] = arg.split(":");
    const result = await attempt(() => api("/skills/install", { method: "POST", body: { skill_id, version } }));
    if (!result) return;
    closeModal();
    toast(result.status === "NEEDS_REVIEW" ? "Installed. Review its permissions under Installed before use." : "Installed and enabled. Add it to an agent.");
    AgentState.skillTab = "installed";
    rerender();
  },
  "skill-approve": async (arg) => {
    const skill = AgentState.skills.find((s) => `${s.skill_id}:${s.version}` === arg);
    if (!window.confirm(`Allow ${skill?.name || "this skill"} to use: ${(skill?.permissions || []).join(", ")}?`)) return;
    const [path] = skillPath(arg);
    if ((await attempt(() => api(`${path}/approve`, { method: "POST" }), "Approved and enabled.")) !== undefined) rerender();
  },
  "skill-toggle": async (arg) => {
    const [path, action] = skillPath(arg);
    if ((await attempt(() => api(`${path}/${action}`, { method: "POST" }), action === "enable" ? "Enabled." : "Disabled.")) !== undefined) rerender();
  },
  "skill-uninstall": async (arg) => {
    if (!window.confirm("Uninstall this skill? Agents stop using it in new tasks; past tasks keep their copy.")) return;
    const [path] = skillPath(arg);
    const result = await attempt(() => api(path, { method: "DELETE" }));
    if (!result) return;
    toast(result.affected_agents?.length ? `Uninstalled. It was used by: ${result.affected_agents.join(", ")}.` : "Uninstalled.");
    rerender();
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "agent-import-form": async (form) => {
    const text = String(new FormData(form).get("document") || "").trim();
    const holder = $("#import-plan");
    if (!text || !holder) return;
    AgentState.importing = text;
    const checked = await attempt(() => api("/agents/import", { method: "POST", body: { document: text } }));
    if (!checked) return;
    const plan = checked.plan;
    holder.innerHTML = `<div class="approval-preview"><h3>${esc(plan.name)} · ${esc(plan.role)}</h3>
        <p>${esc((plan.instructions || "No extra instructions.").slice(0, 300))}</p>
        <p class="muted">${esc(plan.connection_id ? "Uses a model this office has." : "Will use the office's model.")} ${esc(plan.skills.length ? `${plan.skills.length} skill(s) kept.` : "No skills.")}</p>
        ${plan.notes.map((note) => `<p class="muted">• ${esc(note)}</p>`).join("")}
      </div>
      <div class="button-row">${plan.can_import ? btn("Import it", "agent-import-confirm", "primary") : tag("Cannot import under that name", "amber")}</div>`;
  },
  "agent-create-form": async (form) => {
    const data = new FormData(form);
    const role = String(data.get("role")) === "__custom" ? roleName(data.get("custom_role")) : String(data.get("role"));
    if (!role) {
      toast("Give the role a name, with at least one letter.", true);
      return;
    }
    const body = { ...versionBody(form), name: String(data.get("name")).trim(), role };
    const created = await attempt(() => api("/agents", { method: "POST", body }), `${body.name} is ready.`);
    if (!created) return;
    closeModal();
    go("agent", created.id);
  },
  "agent-version-form": async (form) => {
    const agent = AgentState.editing;
    if (!agent) return;
    const saved = await attempt(() => api(`/agents/${agent.id}/versions`, { method: "POST", body: versionBody(form) }));
    if (!saved) return;
    const latest = Math.max(...saved.versions.map((v) => v.version));
    if ((await attempt(() => api(`/agents/${agent.id}/versions/${latest}/enable`, { method: "POST" }), `Saved as version ${latest}; new tasks use it.`)) === undefined) return;
    closeModal();
    rerender();
  },
  "agent-bot-form": async (form) => {
    const agent = AgentState.botAgent;
    const data = new FormData(form);
    const body = {};
    const bot = String(data.get("bot_token") || "").trim();
    const app = String(data.get("app_token") || "").trim();
    if (bot) body.bot_token = bot;
    if (app) body.app_token = app;
    form.reset();
    if (!agent || !Object.keys(body).length) {
      toast("Paste at least one token first.", true);
      return;
    }
    const known = { bot_already_in_use: "That token belongs to another bot. Each agent needs its own Slack app." };
    try {
      await api(`/agents/${agent.id}/chat-bot/slack`, { method: "PUT", body });
      toast(`${agent.name}'s bot is saved. It connects within 30 seconds.`);
      openBot(agent.id);
    } catch (error) {
      if (!(error instanceof SignedOut)) toast(known[error.message] || explain(error), true);
    }
  },
});

// ---- one agent's console ----

async function renderAgentConsole(content, id) {
  await loadAgentData();
  const agent = AgentState.agents.find((a) => a.id === id);
  if (!agent) {
    content.innerHTML = emptyCard("That agent is gone.", "It may have been deleted.", `<a class="button" href="#agents">Back to agents</a>`);
    return;
  }
  AgentState.editing = agent;
  // The trail with this agent's own name as its last step.
  renderTrail({ title: agent.name });
  const version = enabledVersion(agent) || {};
  const builtIn = BUILT_IN.find(([role]) => role === agent.role);
  const bot = await api(`/agents/${id}/chat-bot/slack`).catch(() => null);
  const tested = AgentState.connections.find((c) => c.id === version.connection_id);
  const history = agent.versions
    .slice()
    .reverse()
    .map(
      (v) => `<div class="detail-line"><span>v${v.version} · ${esc(when(v.created_at))}${v.version === agent.enabled_version ? " · in use" : ""}<br><small class="muted">${esc((v.instructions || "No extra instructions").slice(0, 90))}</small></span>${
        v.version === agent.enabled_version ? tag("In use") : btn("Use this", `enable-version:${agent.id}:${v.version}`)
      }</div>`
    )
    .join("");
  content.innerHTML =
    heading(
      "AGENT",
      agent.name,
      `${builtIn ? `${builtIn[1]} in every task` : `Your own role “${agent.role}” · works through its Slack bot`} · v${agent.enabled_version} of ${agent.versions.length}`,
      `<div class="button-row">${tested ? btn("Test (billable)", `test-agent:${agent.id}`) : ""}${agent.status === "ACTIVE" ? btn("Archive", `archive:${agent.id}`, "danger") : tag("Archived", "gray")}</div>`
    ) +
    `<div class="task-layout"><section>
      <form id="agent-version-form" class="card"><h2>How this agent works</h2>
        <p class="muted">${esc(builtIn ? `${builtIn[2]} Your instructions are added to that; the role's output format and checks still apply.` : "This role is your own. It answers you in its Slack bot and does not join office tasks.")}</p>
        ${agentFields(agent)}
        <div class="button-row"><button class="primary">Save as version ${agent.versions.length + 1}</button><span class="muted">New tasks use it at once; running tasks keep theirs.</span></div>
      </form>
      <div class="card"><h2>Identity</h2>
        <form id="agent-identity-form">
          ${field("Name", "name", { value: agent.name, attrs: 'maxlength="255"' })}
          ${field("Role", "role", { value: agent.role, attrs: 'maxlength="64" pattern="[a-z][a-z0-9_-]*"', hint: `Built-in roles: ${BUILT_IN.map(([r]) => r).join(", ")}. Any other name is your own role.` })}
          <div class="button-row"><button>Save identity</button></div>
        </form>
      </div>
    </section>
    <aside class="task-aside">
      <div class="card"><h3>In Slack</h3>
        <p class="muted">Two ways, and you can use both.</p>
        <div class="detail-line"><span><strong>Speak in the office's channels</strong><br><small class="muted">Name this agent in a message to the office bot — “${esc(agent.name)}, what did you find?” — and it answers here, under its own name. Nothing to install.</small></span></div>
        <div class="detail-line"><span><strong>Its own bot</strong><br><small class="muted">A separate Slack app so people can @mention ${esc(agent.name)}, DM it, and invite it to channels.</small></span></div>
      </div>
      <div class="card"><h3>Slack bot</h3>
        <p class="muted">${esc(bot?.installation ? `Connected as ${bot.installation.bot_user_id}${bot.app_token_configured ? "" : " · needs the app-level token"}` : "Not connected. Give this agent its own bot to message it directly.")}</p>
        <div class="button-row">${btn(bot?.installation ? "Manage bot" : "Set up bot", `bot:${agent.id}`, bot?.installation ? "" : "primary")}</div></div>
      <div class="card"><h3>Model</h3>
        <div class="detail-line"><span>In use</span><strong>${esc(connectionLabel(version.connection_id, agent.role))}</strong></div>
        <div class="detail-line"><span>Skills</span><strong>${esc(skillNames(version.skills))}</strong></div>
        <div class="detail-line"><span>External writes</span><strong>Your approval</strong></div>
        <p class="muted">Models are added under Connections; skills under Skills.</p></div>
      <div class="card"><h3>Versions</h3>${history}</div>
    </aside></div>`;
}

Office.screens.agent = { title: "Agent", render: renderAgentConsole, nav: "agents" };

Office.forms["agent-identity-form"] = async (form) => {
  const agent = AgentState.editing;
  const data = new FormData(form);
  const body = { name: String(data.get("name")).trim(), role: String(data.get("role")).trim() };
  if (!agent) return;
  const known = { agent_name_already_exists: "Another agent already has that name." };
  try {
    await api(`/agents/${agent.id}`, { method: "PATCH", body });
    toast("Saved.");
    rerender();
  } catch (error) {
    if (!(error instanceof SignedOut)) toast(known[error.message] || explain(error), true);
  }
};

ERRORS.connection_not_tested =
  "That model has not passed its check yet. Store its key in Connections and run the free check.";

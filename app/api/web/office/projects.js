"use strict";

// Projects as work areas: their own tasks, limits, models and channel.
// Anything a project leaves unset follows the office, and the screen says so.

const Projects = { list: [], current: null, ventures: [], editingVenture: null, objectives: {}, editingObjective: null, entities: [], editingEntity: null, deliverables: null };

function limitLine(project, office, field, money = true) {
  const value = project[field];
  const fallback = office[field];
  const show = (v) => (v === null || v === undefined ? "none" : money ? `$${Number(v).toFixed(2)}` : Number(v).toLocaleString());
  return value === null || value === undefined
    ? `Office's: ${show(fallback)}`
    : `This project: ${show(value)}`;
}

// A business line is what the work is for. Its doctrine is how work is judged
// inside it, and it reaches every agent on every task in that line.
function ventureSection(ventures) {
  const cards = ventures
    .map(
      (v) => `<div class="card"><div class="card-top"><h3>${esc(v.name)}</h3>${tag(`${v.projects} project${v.projects === 1 ? "" : "s"}`, v.projects ? "" : "gray")}</div>
        <p class="muted">${esc(v.description || "No description.")}</p>
        ${v.doctrine ? `<h4>How work is judged here</h4><p class="muted">${esc(v.doctrine)}</p>` : `<p class="muted">No doctrine yet. Without it the agents judge this work like any other.</p>`}
        ${Object.keys(v.role_doctrine || {}).length ? `<p class="labels">${Object.keys(v.role_doctrine).map((role) => tag(`${ROLE_LABEL[role] || role} has its own standard`)).join(" ")}</p>` : ""}
        ${objectiveLines(Projects.objectives[v.id] || [])}
        <div class="button-row"><a class="button primary" href="#space/${esc(v.id)}">Open the space</a>${btn("Edit", `venture-edit:${v.id}`)}${btn("＋ Objective", `objective-new:${v.id}`)}</div></div>`
    )
    .join("");
  return `<h2>Business lines</h2>
    <p class="muted">What the work is for. Define one for each business you run: what it is, how its work is judged, and what it is working towards. Every task in a project belonging to it is told all three before it starts.</p>
    ${ventures.length ? `<div class="grid3">${cards}</div>` : emptyCard("No business lines yet.", "Add one for each business you run, so the same agents can judge their work differently.")}
    <div class="button-row">${btn("＋ New business line", "venture-new", "primary")}${ventures.length ? btn("Sort now", "lines-sort") : ""}</div>`;
}

// What a line is working towards, and what the work came to. A task that
// finished without answering what it was asked is not progress: counting it
// would make a quarter look worked rather than advanced.
// The subjects the work keeps returning to. The owner names them, because they
// know their domain; the office attaches what it learns, and nothing enters a
// dossier until it is kept.
function entitySection(entities, ventures) {
  const line = (id) => ventures.find((v) => v.id === id)?.name;
  const cards = entities
    .map(
      (e) => `<div class="card"><div class="card-top"><h3>${esc(e.name)}</h3>${e.waiting ? tag(`${e.waiting} to review`, "amber") : tag(`${e.kept} known`, e.kept ? "" : "gray")}</div>
        <p class="muted">${esc(e.kind)}${e.venture_id ? ` · ${esc(line(e.venture_id) || "a business line")}` : " · the whole office"}</p>
        ${e.aliases.length ? `<p class="labels">${e.aliases.map((a) => tag(a, "gray")).join(" ")}</p>` : ""}
        <div class="button-row">${btn("Dossier", `entity-open:${e.id}`, e.waiting ? "primary" : "")}</div></div>`
    )
    .join("");
  return `<h2 class="spaced">Subjects</h2>
    <p class="muted">What your work keeps coming back to: a regulator, an instrument, a vendor. Name one and the office attaches what it learns, so the next task starts knowing it. A task is told only the subjects it is about.</p>
    ${entities.length ? `<div class="grid3">${cards}</div>` : emptyCard("No subjects yet.", "Name the things your work returns to, such as a regulation or a regulator.")}
    <div class="button-row">${btn("＋ New subject", "entity-new", "primary")}</div>`;
}

function entityForm(entity) {
  return (
    field("Name", "name", { value: entity?.name || "", hint: "What you call it." }) +
    field("What kind of thing is it?", "kind", {
      type: "select",
      value: entity?.kind || "subject",
      choices: [
        ["instrument", "A law, regulation or standard"],
        ["regulator", "A regulator or authority"],
        ["vendor", "A vendor or product"],
        ["brand", "A brand or client"],
        ["subject", "Something else"],
      ],
    }) +
    field("Also called", "aliases", {
      required: false,
      value: (entity?.aliases || []).join(", "),
      hint: "Other names the work uses, separated by commas. The Wwft is also BWBR0024282.",
    }) +
    field("Whose subject is it?", "venture_id", {
      type: "select",
      required: false,
      value: entity?.venture_id || "",
      choices: [["", "The whole office"], ...(Projects.ventures || []).map((v) => [v.id, v.name])],
    })
  );
}

async function openDossier(id) {
  const entity = (Projects.entities || []).find((e) => e.id === id);
  if (!entity) return;
  const [waiting, kept] = await Promise.all([
    api(`/entities/${id}/facts`).catch(() => []),
    api(`/entities/${id}/facts?status=APPROVED`).catch(() => []),
  ]);
  const chance = (row) =>
    row.durable_probability === null || row.durable_probability === undefined
      ? tag("not ranked", "gray")
      : tag(`${Math.round(row.durable_probability * 100)}% durable`);
  modal(
    entity.name,
    `<p class="muted">What finished work said about it. Only what you keep is told to later tasks about ${esc(entity.name)}.</p>` +
      (kept.length
        ? `<h3>Known</h3>${kept.map((f) => `<div class="detail-line"><span>${esc(f.fact)}</span><strong>kept</strong></div>`).join("")}`
        : "") +
      (waiting.length
        ? `<h3>Waiting for you</h3>${waiting
            .map(
              (f) => `<div class="detail-line"><span>${esc(f.fact)}</span>
                <span class="button-row tight">${chance(f)}${btn("Keep", `fact-keep:${f.id}`, "primary")}${btn("Discard", `fact-drop:${f.id}`)}</span></div>`
            )
            .join("")}`
        : `<p class="muted">Nothing waiting. Open a finished task and ask the office what it learned about its subjects.</p>`),
    `<div class="button-row">${btn("Edit subject", `entity-edit:${id}`)}${btn("Close", "close", "primary")}</div>`
  );
}

async function decideFact(id, approved) {
  const said = await attempt(
    () => api(`/facts/${id}/decision`, { method: "POST", body: { approved } }),
    approved ? "Kept. Later tasks about this subject are told." : "Discarded."
  );
  if (said !== undefined) {
    closeModal();
    rerender();
  }
}

function objectiveLines(objectives) {
  if (!objectives.length) return "";
  return (
    `<h4>Working towards</h4>` +
    objectives
      .map((o) => {
        const moved = o.delivered ? `${o.delivered} delivered` : "";
        const part = o.partial ? `${o.partial} partial` : "";
        const open = o.unfinished ? `${o.unfinished} not yet` : "";
        const counts = [moved, part, open].filter(Boolean).join(" · ") || "no work yet";
        return `<div class="detail-line"><span>${esc(o.title)}${o.measure ? `<br><small class="muted">${esc(o.measure)}</small>` : ""}</span>
          <span>${o.status === "OPEN" ? tag(counts, o.delivered ? "" : "gray") : tag(o.status.toLowerCase())} ${btn("Edit", `objective-edit:${o.id}`)}</span></div>`;
      })
      .join("")
  );
}

function objectiveForm(objective) {
  return (
    field("What are you trying to achieve?", "title", {
      value: objective?.title || "",
      hint: "One outcome, in your own words.",
    }) +
    field("How would you know it happened?", "measure", {
      type: "textarea",
      required: false,
      value: objective?.measure || "",
      hint: "Leave it empty and this is a direction rather than a target. That is honest, and it says so.",
    }) +
    field("By when", "ends_on", {
      type: "date",
      required: false,
      value: objective?.ends_on || "",
      hint: "After this date it stops being told to new work.",
    }) +
    (objective
      ? field("Status", "status", {
          type: "select",
          value: objective.status,
          choices: [
            ["OPEN", "Still working towards it"],
            ["MET", "Met"],
            ["MISSED", "Missed"],
            ["DROPPED", "No longer pursuing"],
          ],
          hint: "Yours to close. Finished work is evidence for it, never a verdict on it.",
        })
      : "")
  );
}

// This line's pack: the installed skills offered only to its tasks.
function packPicker(venture) {
  const skills = (Projects.skills || []).filter((s) => s.status === "ENABLED");
  const latest = new Map();
  skills.forEach((s) => latest.set(s.skill_id, s));
  const kept = new Set(venture?.skills || []);
  const boxes = [...latest.values()]
    .map((s) => `<label class="check"><input type="checkbox" name="skills" value="${esc(s.skill_id)}" ${kept.has(s.skill_id) ? "checked" : ""}> ${esc(s.name || s.skill_id)}</label>`)
    .join("");
  return `<h3>Skills for this line</h3>
    <p class="muted">A skill ticked here is offered only to this line's tasks. A skill no line has ticked is offered to every task.</p>
    ${boxes ? `<fieldset class="skill-picks">${boxes}</fieldset>` : `<p class="muted">No skills installed yet. Add them under Skills.</p>`}`;
}

function ventureForm(venture) {
  const perRole = venture?.role_doctrine || {};
  return (
    field("Name", "name", { value: venture?.name || "", hint: "What you call this business line." }) +
    field("What it is", "description", {
      type: "textarea",
      required: false,
      value: venture?.description || "",
      hint: "Its operating model, and who it serves.",
    }) +
    field("How work is judged here", "doctrine", {
      type: "textarea",
      required: false,
      value: venture?.doctrine || "",
      hint: "The standard of proof and the non-negotiables. Every task in this line is told this before it starts.",
    }) +
    `<h3>What each role should hold to</h3>
     <p class="muted">Said to that role alone, after your own instructions for it. Leave one empty and that role hears only the line above.</p>` +
    BUILT_IN.map(([role, name, about]) =>
      field(`${name}`, `role_${role}`, {
        type: "textarea",
        required: false,
        value: perRole[role] || "",
        hint: about,
      })
    ).join("") +
    packPicker(venture)
  );
}

async function renderProjects(content) {
  const [projects, progress, ventures] = await Promise.all([
    api("/projects"),
    api("/office/progress").catch(() => ({ projects: [] })),
    api("/ventures").catch(() => []),
  ]);
  Projects.ventures = ventures;
  Projects.deliverables = await api("/deliverables").catch(() => null);
  Projects.objectives = {};
  Projects.entities = await api("/entities").catch(() => []);
  await Promise.all(
    ventures.map(async (v) => {
      Projects.objectives[v.id] = await api(`/ventures/${v.id}/objectives`).catch(() => []);
    })
  );
  const active = projects.filter((p) => p.status === "ACTIVE");
  Projects.list = active;
  const byId = new Map(active.map((p) => [p.id, p]));
  // Busiest first: what needs the owner, then what is working, then the rest.
  const order = { needs_you: 0, working: 1, quiet: 2 };
  const cards = (progress.projects || [])
    .filter((card) => byId.has(card.id))
    .sort((a, b) => (order[a.state] ?? 3) - (order[b.state] ?? 3) || String(b.last_activity || "").localeCompare(String(a.last_activity || "")))
    .map((card) => projectProgressCard(card, byId.get(card.id)))
    .join("");
  content.innerHTML =
    heading(
      "BUSINESS SPACES",
      "Your business lines, and the projects in them.",
      "The long-running context of your work: each project's progress, spend and what waits on you, and each line's goals and standards. You don't need one to start talking.",
      btn("＋ New task", "new-task", "primary")
    ) +
    (active.length ? `<div class="pc-grid" data-project-cards>${cards}</div>` : emptyCard("No projects yet.", "A project is created the first time you start a task.")) +
    ventureSection(ventures) +
    entitySection(Projects.entities, ventures);
}

// What this project has learned. A lesson is written by a finished task and
// only reaches later tasks once the owner approves it: a ranking never decides.
function memoryCard(waiting, learned) {
  if (!waiting.length && !learned.length) return "";
  const chance = (row) =>
    row.reusable_probability === null || row.reusable_probability === undefined
      ? tag("not ranked", "gray")
      : tag(`${Math.round(row.reusable_probability * 100)}% likely useful`);
  const review = waiting
    .map(
      (row) => `<div class="detail-line"><span>${esc(row.lesson)}<br><small class="muted">${esc(row.kind.replace(/_/g, " "))} · ${esc(row.evidence || "")}</small></span>
        <span class="button-row tight">${chance(row)}${btn("Keep", `memory-keep:${row.id}`, "primary")}${btn("Discard", `memory-drop:${row.id}`)}</span></div>`
    )
    .join("");
  return `<div class="card spaced"><h2>What this project has learned</h2>
    <p class="muted">A finished task writes these from what it observed. Only what you keep is told to later tasks.</p>
    ${waiting.length ? `<h3>Waiting for you</h3>${review}` : ""}
    ${learned.length ? `<h3>In use</h3>${learned.map((row) => `<div class="detail-line"><span>${esc(row.lesson)}</span><strong>kept</strong></div>`).join("")}` : ""}
  </div>`;
}

async function renderProject(content, id) {
  const [summary, settings, connections, channels, waiting, learned, ventures] = await Promise.all([
    api(`/projects/${id}/summary`),
    api(`/projects/${id}/settings`),
    api("/connections"),
    api("/channels").catch(() => ({ destinations: [] })),
    api(`/projects/${id}/memory`).catch(() => []),
    api(`/projects/${id}/memory?status=APPROVED`).catch(() => []),
    api("/ventures").catch(() => []),
  ]);
  Projects.ventures = ventures;
  Projects.deliverables = await api("/deliverables").catch(() => null);
  Projects.current = { id, summary, settings };
  // The trail with this project's own name as its last step.
  renderTrail({ title: summary.name });
  const project = settings.project;
  const office = settings.office;
  const usable = connections.filter((c) => ["MODEL_AVAILABLE", "VERIFIED"].includes(c.status));
  const destinations = (channels.destinations || []).filter((d) => d.enabled);
  const tasks = Object.entries(summary.tasks)
    .map(([status, count]) => `<div class="detail-line"><span>${esc((STATUS[status] || [status])[0])}</span><strong>${count}</strong></div>`)
    .join("") || `<p class="muted">No tasks yet.</p>`;
  const limit = project.monthly_cost_limit_usd;
  const share = limit ? Math.min(100, Math.round((summary.spent_this_month_usd / limit) * 100)) : 0;
  content.innerHTML =
    heading("PROJECT", summary.name, summary.description || "Its tasks, knowledge, limits and models.", `<div class="button-row">${btn("Rename", "project-edit")}<a class="button" href="#room/${esc(id)}">Its discussion</a>${btn("＋ New task here", `project-task:${id}`, "primary")}</div>`) +
    lineLabel({ project_id: id, name: summary.venture_name, set_by: summary.venture_set_by, suggestion: summary.venture_suggestion }) +
    `<div class="grid2">
      <div class="card"><h2>Work</h2>${tasks}
        <div class="detail-line"><span>Spent this month</span><strong>$${summary.spent_this_month_usd.toFixed(2)}${limit ? ` of $${limit.toFixed(2)}` : ""}</strong></div>
        <div class="detail-line"><span>Tokens</span><strong>${summary.tokens.toLocaleString()}</strong></div>
        ${limit ? `<div class="meter"><span style="width:${share}%"></span></div>` : ""}
        <div class="button-row"><a class="button" href="#board">See them on the board ↗</a></div>
      </div>
      <div class="card"><h2>Knowledge it may read</h2>
        ${summary.sources.map((s) => `<div class="detail-line"><span>${esc(s.name)}</span><strong>${s.documents} documents</strong></div>`).join("") || `<p class="muted">Nothing granted yet. Grant a source under Knowledge.</p>`}
        <div class="button-row"><a class="button" href="#knowledge">Manage knowledge ↗</a></div>
      </div>
    </div>
    ${memoryCard(waiting, learned)}
    <div class="grid2 spaced">
      <div class="card"><h2>Limits for this project</h2>
        <p class="muted">Empty means the office's setting applies. Limits are checked together: neither the office's nor this project's can be passed.</p>
        <form id="project-limits-form">
          ${field("Monthly limit (USD)", "monthly_cost_limit_usd", { type: "number", required: false, value: project.monthly_cost_limit_usd ?? "", attrs: 'min="0.01" step="0.01"', hint: limitLine(project, office, "monthly_cost_limit_usd") })}
          ${field("Cost per task (USD)", "run_max_cost_usd", { type: "number", required: false, value: project.run_max_cost_usd ?? "", attrs: 'min="0.01" step="0.01"', hint: limitLine(project, office, "run_max_cost_usd") })}
          ${field("Tokens per task", "run_max_tokens", { type: "number", required: false, value: project.run_max_tokens ?? "", attrs: 'min="1"', hint: limitLine(project, office, "run_max_tokens", false) })}
          ${field("Minutes per task", "run_max_minutes", { type: "number", required: false, value: project.run_max_minutes ?? "", attrs: 'min="1"', hint: limitLine(project, office, "run_max_minutes", false) })}
          <div class="button-row"><button class="primary">Save limits</button></div>
        </form>
      </div>
      <div class="card"><h2>What its tasks should come to</h2>
        <p class="muted">The shape a task here takes unless it says otherwise. Said before the work starts, so the work aims at it.</p>
        <form id="project-deliverable-form">
          ${field("Shape", "shape", {
            type: "select",
            value: (summary.deliverable || {}).shape || "report",
            choices: (Projects.deliverables?.shapes || []).map((d) => [d.id, d.label]),
          })}
          ${field("Hand it over as", "format", {
            type: "select",
            value: (summary.deliverable || {}).format || "onscreen",
            choices: (Projects.deliverables?.formats || []).map((f) => [f.id, f.label]),
          })}
          <div class="button-row"><button class="primary">Save</button></div>
        </form>
      </div>
      <div class="card"><h2>Models and notifications</h2>
        <form id="project-models-form">
          ${field("Agents as a team", "team_on", {
            type: "select",
            required: false,
            value: project.team_on === true ? "on" : project.team_on === false ? "off" : "",
            choices: [["", `The office's choice · ${office.team_on ? "on" : "off"}`], ["on", "On for this project"], ["off", "Off for this project"]],
            hint: "Steps read each other's posts before writing, and the team reacts after each wave.",
          })}
          ${field("Advisor on call", "advisor_on", {
            type: "select",
            required: false,
            value: project.advisor_on === true ? "on" : project.advisor_on === false ? "off" : "",
            choices: [["", `The office's choice · ${office.advisor_on ? "on" : "off"}`], ["on", "On for this project"], ["off", "Off for this project"]],
            hint: "Reads the whole task before the plan, when a failure repeats and before it is done, and advises the Chief.",
          })}
          ${BUILT_IN.map(([role, name]) => {
            const chosen = project.role_models[role] || "";
            const officeChoice = office.role_models[role];
            const officeName = officeChoice ? connections.find((c) => c.id === officeChoice)?.display_name : null;
            return field(name, `role_${role}`, {
              type: "select",
              required: false,
              value: chosen,
              choices: [["", officeName ? `The office's choice · ${officeName}` : "The office's choice"], ...usable.map((c) => [c.id, `${c.display_name} · ${c.model_id}`])],
            });
          }).join("")}
          ${field("Finish on a cheaper model past (USD)", "downgrade_at_usd", {
            type: "number",
            required: false,
            value: project.downgrade_at_usd ?? "",
            attrs: 'min="0.01" step="0.01"',
            hint: "Empty means a task stops at its limit instead of continuing.",
          })}
          ${field("The cheaper model", "cheap_connection_id", {
            type: "select",
            required: false,
            value: project.cheap_connection_id || "",
            choices: [["", "None chosen"], ...usable.map((c) => [c.id, `${c.display_name} · ${c.model_id}`])],
          })}
          ${field("Tell this project's news to", "notify_destination_id", {
            type: "select",
            required: false,
            value: project.notify_destination_id || "",
            choices: [["", "Every channel the office notifies"], ...destinations.map((d) => [d.id, d.name])],
            hint: "Choose a channel and this project's notifications go only there.",
          })}
          <div class="button-row"><button class="primary">Save</button></div>
        </form>
      </div>
    </div>`;
}

Office.screens.projects = { title: "Business spaces", render: renderProjects };
Office.screens.project = { title: "Project", render: renderProject, nav: "projects" };

function projectBody(form, fields) {
  const data = new FormData(form);
  const body = {};
  for (const field of fields) {
    const text = String(data.get(field) || "").trim();
    body[field] = text === "" ? null : Number(text);
  }
  return body;
}

async function decideMemory(id, approved, scope = "project") {
  const said = await attempt(
    () => api(`/memory/${id}/decision`, { method: "POST", body: { approved, scope } }),
    approved ? "Kept. Later tasks in this project are told." : "Discarded."
  );
  if (said !== undefined) rerender();
}

// Which business line a project's work is judged by, who put it there, and
// the office's guess when it has none. Shown wherever the work is.
function lineLabel(line) {
  if (!line || !line.project_id) return "";
  const id = line.project_id;
  const change = btn("Change", `line-change:${id}`);
  if (line.name) {
    const who = line.set_by === "office" ? "sorted by the office" : "your choice";
    return `<p class="line-label" data-line-label>${tag(line.name)}<span class="muted">Business line · ${esc(who)}</span>${change}</p>`;
  }
  const hint = line.suggestion || {};
  if (line.set_by !== "owner" && hint.venture_id) {
    const sure = Math.round(Number(hint.confidence || 0) * 100);
    return `<p class="line-label" data-line-label>${tag("No business line", "gray")}<span class="muted">The office thinks <strong>${esc(hint.line)}</strong> (${sure}% sure)</span>${btn("Put it there", `line-accept:${id}:${hint.venture_id}`, "primary")}${change}</p>`;
  }
  const why = line.set_by === "owner" ? "your choice" : hint.line === "none" ? "none of your lines fits it" : "the office sorts it shortly";
  return `<p class="line-label" data-line-label>${tag("No business line", "gray")}<span class="muted">${esc(why)}</span>${change}</p>`;
}

// The office's guess for a project in the list, if it names a line still open.
function suggestionFor(project, ventures) {
  const hint = project.venture_suggestion || {};
  const line = (ventures || []).find((v) => v.id === hint.venture_id);
  return line ? { ...hint, line: line.name } : { line: hint.line, confidence: hint.confidence };
}

async function setLine(projectId, ventureId) {
  const saved = await attempt(
    () => api(`/projects/${projectId}/venture`, { method: "PUT", body: { venture_id: ventureId || null } }),
    ventureId ? "Saved. Tasks here are judged by that line." : "Saved. No business line for this project."
  );
  if (saved === undefined) return;
  closeModal();
  rerender();
}

Office.actions = Object.assign(Office.actions || {}, {
  "line-change": async (projectId) => {
    const ventures = await attempt(() => api("/ventures"));
    if (!ventures) return;
    const summary = await api(`/projects/${projectId}/summary`).catch(() => null);
    Projects.changingLine = projectId;
    formModal(
      "Business line",
      "line-change-form",
      `<p class="muted">Every task in this project is told how work is judged in its line. The office will not move a line you choose.</p>` +
        field("Business line", "venture_id", {
          type: "select",
          required: false,
          value: summary?.venture_id || "",
          choices: [["", "None · judged like any other work"], ...ventures.map((v) => [v.id, v.name])],
        }),
      "Save"
    );
  },
  "line-accept": (arg) => {
    const [projectId, ventureId] = String(arg).split(":");
    if (projectId && ventureId) return setLine(projectId, ventureId);
  },
  "memory-keep": (id) => {
    const venture = Projects.current?.summary?.venture_name;
    formModal(
      "Keep this lesson",
      "memory-scope-form",
      field("Where does it apply?", "scope", {
        type: "select",
        value: "project",
        choices: [
          ["project", "This project only"],
          ...(venture ? [["venture", `Everything in ${venture}`]] : []),
          ["office", "Everywhere in the office"],
        ],
        hint: venture
          ? "A fact about the world, such as a source that refuses us, belongs to the business line or the office."
          : "This project belongs to no business line, so a lesson can be kept here or everywhere.",
      }) + `<input type="hidden" name="memory_id" value="${esc(id)}">`,
      "Keep"
    );
  },
  "memory-drop": (id) => decideMemory(id, false),
  "entity-new": () => {
    Projects.editingEntity = null;
    formModal("New subject", "entity-form", entityForm(null), "Add");
  },
  "entity-edit": (id) => {
    const entity = (Projects.entities || []).find((e) => e.id === id);
    if (!entity) return;
    Projects.editingEntity = id;
    formModal("Subject", "entity-form", entityForm(entity), "Save");
  },
  "entity-open": openDossier,
  "fact-keep": (id) => decideFact(id, true),
  "fact-drop": (id) => decideFact(id, false),
  "objective-new": (ventureId) => {
    Projects.editingObjective = { venture: ventureId, id: null };
    formModal("New objective", "objective-form", objectiveForm(null), "Add");
  },
  "objective-edit": (id) => {
    const all = Object.entries(Projects.objectives || {});
    for (const [ventureId, rows] of all) {
      const found = (rows || []).find((o) => o.id === id);
      if (found) {
        Projects.editingObjective = { venture: ventureId, id };
        formModal("Objective", "objective-form", objectiveForm(found), "Save");
        return;
      }
    }
  },
  "lines-sort": async () => {
    const sorted = await attempt(() => api("/projects/sort-into-lines", { method: "POST" }));
    if (sorted === undefined) return;
    const done = sorted.assigned || [];
    const asking = sorted.unsure || [];
    modal(
      "Sorted into business lines",
      (done.length
        ? `<p>Put under a line:</p>${done.map((r) => `<div class="detail-line"><span>${esc(r.project)}</span><strong>${esc(r.line)} <span class="muted">${Math.round(r.confidence * 100)}%</span></strong></div>`).join("")}`
        : `<p class="muted">Nothing was confident enough to place on its own.</p>`) +
        (asking.length
          ? `<h3>Left to you</h3><p class="muted">Too unsure to place, or belonging to no line. The wrong line would tell its tasks the wrong standard, so the office did not guess.</p>${asking.map((r) => `<div class="detail-line"><span>${esc(r.project)}</span><strong class="muted">${esc(r.line)} · ${Math.round(r.confidence * 100)}%</strong></div>`).join("")}`
          : "")
    );
    rerender();
  },
  // A project's name and what it is about, changed in place.
  "project-edit": () => {
    const summary = Projects.current?.summary;
    if (!summary) return;
    formModal(
      "Rename this project",
      "project-edit-form",
      field("Name", "name", { value: summary.name || "", attrs: 'maxlength="255"' }) +
        field("What it is about", "description", {
          type: "textarea",
          required: false,
          value: summary.description || "",
          attrs: 'maxlength="8192" rows="3"',
          hint: "Its tasks and the office's sorting read this, with what its tasks asked.",
        }),
      "Save"
    );
  },
  "venture-new": async () => {
    Projects.skills = await api("/skills").catch(() => []);
    Projects.editingVenture = null;
    formModal("New business line", "venture-form", ventureForm(null), "Add");
  },
  "venture-edit": async (id) => {
    Projects.skills = await api("/skills").catch(() => []);
    const venture = (Projects.ventures || []).find((v) => v.id === id);
    if (!venture) return;
    Projects.editingVenture = id;
    formModal("Business line", "venture-form", ventureForm(venture), "Save");
  },
  "project-open": (id) => go("project", id),
  "project-task": async (id) => {
    Projects.startIn = id;
    await openNewTask();
    const select = $("#f-project");
    if (select) select.value = id;
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "project-edit-form": async (form) => {
    const id = Projects.current?.id;
    if (!id) return;
    const data = new FormData(form);
    const name = String(data.get("name") || "").trim();
    if (!name) {
      toast("A project needs a name.", true);
      return;
    }
    const saved = await attempt(
      () => api(`/projects/${id}`, { method: "PUT", body: { name, description: String(data.get("description") || "") } }),
      "Saved."
    );
    if (saved === undefined) return;
    closeModal();
    rerender();
  },
  "line-change-form": (form) => {
    const projectId = Projects.changingLine;
    if (!projectId) return;
    return setLine(projectId, String(new FormData(form).get("venture_id") || ""));
  },
  "project-deliverable-form": async (form) => {
    const id = Projects.current?.id;
    if (!id) return;
    const data = new FormData(form);
    const saved = await attempt(
      () =>
        api(`/projects/${id}/deliverable`, {
          method: "PUT",
          body: { shape: String(data.get("shape")), format: String(data.get("format")) },
        }),
      "Saved. New tasks here aim at that."
    );
    if (saved !== undefined) rerender();
  },
  "entity-form": async (form) => {
    const data = new FormData(form);
    const body = {
      name: String(data.get("name") || "").trim(),
      kind: String(data.get("kind") || "subject"),
      aliases: String(data.get("aliases") || "")
        .split(",")
        .map((a) => a.trim())
        .filter(Boolean),
      venture_id: String(data.get("venture_id") || "") || null,
    };
    const id = Projects.editingEntity;
    const saved = await attempt(
      () => (id ? api(`/entities/${id}`, { method: "PUT", body }) : api("/entities", { method: "POST", body })),
      id ? "Saved." : "Added. Finished work can now tell the office about it."
    );
    if (saved === undefined) return;
    closeModal();
    rerender();
  },
  "objective-form": async (form) => {
    const data = new FormData(form);
    const editing = Projects.editingObjective;
    if (!editing) return;
    const body = {
      title: String(data.get("title") || "").trim(),
      measure: String(data.get("measure") || ""),
      ends_on: String(data.get("ends_on") || "") || null,
    };
    if (editing.id) body.status = String(data.get("status") || "OPEN");
    const saved = await attempt(
      () =>
        editing.id
          ? api(`/objectives/${editing.id}`, { method: "PUT", body })
          : api(`/ventures/${editing.venture}/objectives`, { method: "POST", body }),
      editing.id ? "Saved." : "Added. New tasks in this line are told it."
    );
    if (saved === undefined) return;
    closeModal();
    rerender();
  },
  "memory-scope-form": async (form) => {
    const data = new FormData(form);
    await decideMemory(String(data.get("memory_id")), true, String(data.get("scope") || "project"));
    closeModal();
  },
  "venture-form": async (form) => {
    const data = new FormData(form);
    const role_doctrine = {};
    BUILT_IN.forEach(([role]) => {
      const text = String(data.get(`role_${role}`) || "").trim();
      if (text) role_doctrine[role] = text;
    });
    const body = {
      name: String(data.get("name") || "").trim(),
      description: String(data.get("description") || ""),
      doctrine: String(data.get("doctrine") || ""),
      role_doctrine,
      skills: data.getAll("skills").map(String),
    };
    const id = Projects.editingVenture;
    const saved = await attempt(
      () =>
        id
          ? api(`/ventures/${id}`, { method: "PUT", body })
          : api("/ventures", { method: "POST", body }),
      id ? "Saved. New tasks in this line are told it." : "Added."
    );
    if (saved === undefined) return;
    closeModal();
    rerender();
  },
  "project-limits-form": async (form) => {
    const body = projectBody(form, ["monthly_cost_limit_usd", "run_max_cost_usd", "run_max_tokens", "run_max_minutes"]);
    const id = Projects.current?.id;
    if (!id) return;
    if ((await attempt(() => api(`/projects/${id}/settings`, { method: "PUT", body }), "Saved. New tasks in this project use it.")) === undefined) return;
    rerender();
  },
  "project-models-form": async (form) => {
    const data = new FormData(form);
    const role_models = {};
    for (const [role] of BUILT_IN) role_models[role] = data.get(`role_${role}`) || null;
    const destination = String(data.get("notify_destination_id") || "");
    const cheap = String(data.get("cheap_connection_id") || "");
    const past = String(data.get("downgrade_at_usd") || "").trim();
    const advisor = String(data.get("advisor_on") || "");
    const together = String(data.get("team_on") || "");
    const body = {
      role_models,
      advisor_on: advisor === "on" ? true : advisor === "off" ? false : null,
      team_on: together === "on" ? true : together === "off" ? false : null,
      notify_destination_id: destination || null,
      cheap_connection_id: cheap || null,
      downgrade_at_usd: past ? Number(past) : null,
    };
    const id = Projects.current?.id;
    if (!id) return;
    if ((await attempt(() => api(`/projects/${id}/settings`, { method: "PUT", body }), "Saved.")) === undefined) return;
    rerender();
  },
});


// ---- A business space: one place for a line's decisions, goals,
// discussions, projects and materials ----

function spaceDecision(d) {
  const from = d.source ? ` · from <a href="#topic/${esc(d.source.topic_id)}">${esc(d.source.title)}</a>` : "";
  const replaced = (d.replaced || [])
    .map((r) => `<li><span class="replaced">${esc(r.text)}</span> <span class="muted">replaced ${esc(when(r.replaced_at))}</span></li>`)
    .join("");
  return `<li class="space-decision"><strong>${esc(d.text)}</strong><span class="muted">Decided by ${esc(d.by || "you")} · ${esc(when(d.at))}${from}</span>${replaced ? `<ul class="space-replaced">${replaced}</ul>` : ""}</li>`;
}

async function renderSpace(content, ventureId) {
  if (!ventureId) {
    go("projects");
    return;
  }
  const [space, kept] = await Promise.all([
    api(`/ventures/${ventureId}/space`),
    api(`/ventures/${ventureId}/workspaces`).catch(() => null),
  ]);
  // Its folders, read live by its work; changed in Settings.
  const folders = Array.isArray(kept?.workspaces) ? kept.workspaces : [];
  const folderList = folders.length
    ? `<ul class="plain">${folders.map((f) => `<li><strong>${esc(f.name)}</strong> <span class="muted">~/dev/${esc(f.path)} · ${esc(f.access)}</span></li>`).join("")}</ul>`
    : `<p class="muted">No folders yet.</p>`;
  // The trail with this business space's own name as its last step.
  renderTrail({ title: space.name });
  const goals = space.goals.length
    ? `<ul class="plain">${space.goals.map((g) => `<li><strong>${esc(g.title)}</strong>${g.ends_on ? ` <span class="muted">by ${esc(g.ends_on)}</span>` : ""}${g.measure ? `<p class="muted">${esc(g.measure)}</p>` : ""}</li>`).join("")}</ul>`
    : `<p class="muted">No goals yet. Add one from Business spaces with ＋ Objective.</p>`;
  const talks = space.discussions.length ? `<div class="topic-grid">${space.discussions.slice(0, 6).map(topicCard).join("")}</div>` : `<p class="muted">No discussions placed here yet.</p>`;
  const projects = space.projects.map((p) => `<li><a href="#project/${esc(p.id)}">${esc(p.name)}</a></li>`).join("");
  const materials = space.materials.map((m) => `<li>${esc(m.name)} <span class="muted">${esc(m.documents)} documents</span></li>`).join("");
  content.innerHTML =
    heading("BUSINESS SPACE", space.name, space.description || "The long-running context of this line of work.") +
    (space.doctrine ? `<div class="card space-doctrine"><h3>How work is judged here</h3><p>${esc(space.doctrine)}</p></div>` : "") +
    `<div class="grid2 space-grid">
      <div class="card"><h2>Decisions</h2><p class="muted">What agents in this space follow. An idea said in passing stays in its discussion until you record it.</p>${
        space.decisions.length ? `<ul class="plain space-decisions">${space.decisions.map(spaceDecision).join("")}</ul>` : `<p class="muted">None recorded yet. On your own message in a discussion, press Record as decision.</p>`
      }</div>
      <div class="card"><h2>Working towards</h2>${goals}</div>
    </div>
    <h2 class="spaced">Discussions here</h2>${talks}
    <div class="grid2 space-grid">
      <div class="card"><h2>Projects</h2>${projects ? `<ul class="plain">${projects}</ul>` : `<p class="muted">No projects in this line yet.</p>`}</div>
      <div class="card"><h2>Materials</h2>${materials ? `<ul class="plain">${materials}</ul>` : `<p class="muted">No knowledge granted to this line's projects yet.</p>`}<p class="muted">Only this line's work reads these.</p></div>
    </div>
    <div class="card spaced"><h2>Folders</h2>${folderList}<p class="muted">Your folders inside ~/dev, read as they are by the work in this space.</p><div class="button-row">${btn("Manage folders", "settings-open:workspaces")}</div></div>`;
}

Office.screens.space = { title: "Business space", nav: "projects", render: renderSpace };

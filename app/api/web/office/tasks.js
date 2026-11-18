"use strict";

// Tasks: the list, the task room for one run, and the owner's actions on it.

const Tasks = { deliverables: null };
const TaskRoom = { run: null, context: null, answer: null, workingOpen: false, reportOpen: false, documents: {}, tab: "workflow", filter: "all", approval: null };
// Kept across refreshes, so a refresh asks only for what is new.
const taskEvents = new EventReader((runId, after) => api(`/runs/${runId}/events?after=${after}`));

const FILTERS = [
  ["all", "All tasks", () => true],
  ["review", "Needs you", (t) => NEEDS_OWNER.includes(t.status)],
  ["active", "In progress", (t) => ACTIVE.includes(t.status)],
  ["done", "Finished", (t) => !NEEDS_OWNER.includes(t.status) && !ACTIVE.includes(t.status)],
];

function updateTaskCount(count) {
  Office.taskCount = count;
  const badge = document.querySelector('nav a[href="#tasks"] .count');
  if (badge) {
    badge.textContent = String(count);
    badge.hidden = !count;
  }
}

async function renderTasks(content) {
  const [overview, progress] = await Promise.all([
    api("/office/overview"),
    api("/office/progress").catch(() => ({ tasks: [] })),
  ]);
  // Work in motion, as cards: what is running and what waits on the owner.
  const moving = (progress.tasks || []).map(taskProgressCard).join("");
  const [, , keep] = FILTERS.find(([id]) => id === TaskRoom.filter) || FILTERS[0];
  const rows = overview.tasks.filter(keep);
  content.innerHTML =
    heading("LIBRARY", "What has been handed off.", "Each piece of work reports back to where it was asked. Come here to plan, compare or check progress.", `<div class="button-row"><a class="button" href="#board">Board</a><a class="button" href="#knowledge">Folders &amp; documents</a>${btn("＋ New task", "new-task")}</div>`) +
    (moving ? `<h2 class="spaced">In motion</h2><div class="pc-grid" data-task-cards>${moving}</div><h2 class="spaced">Every task</h2>` : "") +
    `<div class="subnav" role="tablist">${FILTERS.map(
      ([id, label, test]) => `<button type="button" role="tab" aria-selected="${id === TaskRoom.filter}" class="${id === TaskRoom.filter ? "active" : ""}" data-action="filter:${id}">${label} <span class="muted">${overview.tasks.filter(test).length}</span></button>`
    ).join("")}</div>
    <div id="task-list">${
      rows.length
        ? rows
            .map(
              // The row is the way in: a button on every row only repeated it.
              (t) => `<button type="button" class="card compact-task task-row" data-action="open:${esc(t.run_id)}">${avatar(t.project || t.objective)}<div><strong>${esc(t.title || t.objective)}</strong><p>${esc(t.project)}${t.business_line ? ` · ${esc(t.business_line)}` : ""} · ${esc(when(t.started_at) || "not started")}${t.requested_from === "chat" ? " · asked from chat" : ""}</p></div><div class="push">${taskProgress(t.run_id)}${statusTag(t.status)}</div><span class="row-go" aria-hidden="true">›</span></button>`
            )
            .join("")
        : emptyCard("Nothing here yet.", "Tasks appear here when they reach this stage.")
    }</div>`;
  updateTaskCount(overview.counts.needs_owner + overview.counts.in_motion);
}

// ---- the task room ----

function roleMessages(run) {
  const state = run.state || {};
  const items = [];
  const plan = state.plan;
  if (plan)
    items.push({
      node: "plan",
      who: "Chief",
      role: "Planner",
      text: `I set the question: ${plan.research_question}`,
      listTitle: "Done when", list: plan.acceptance_criteria || [],
      chips: (plan.subquestions || []).length > 1 ? [`${plan.subquestions.length} parallel research questions`] : [],
    });
  const research = state.research;
  if (research)
    items.push({
      node: "research",
      who: "Researcher",
      role: "Research",
      text: (research.key_findings || []).slice(0, 3).join(" "),
      chips: [`${(research.sources || []).length} sources`, `confidence: ${research.confidence}`],
    });
  const strategy = state.strategy;
  if (strategy)
    items.push({
      node: "strategy",
      who: "Strategist",
      role: "Deliverable",
      text: strategy.recommendation || strategy.problem || "Draft ready.",
      chips: strategy.smallest_useful_test ? [`Test: ${strategy.smallest_useful_test}`] : [],
    });
  const critique = state.critique || state.last_critique;
  if (critique)
    items.push({
      node: "critique",
      who: "Critic",
      role: "Review",
      // The critic's plain word for the owner first; the old verdict line otherwise.
      text:
        critique.for_owner ||
        (critique.verdict === "PASS"
          ? "I checked the work against the plan. It passes."
          : `Verdict: ${critique.verdict}${critique.revision_target ? ` — revise the ${critique.revision_target}` : ""}.`),
      list: (critique.issues || []).length
        ? critique.issues.map((i) => `${i.issue} — ${i.why_it_matters}`)
        : [...(critique.critical_issues || []), ...(critique.required_corrections || [])].slice(0, 4),
    });
  const synthesis = state.synthesis;
  if (synthesis)
    items.push({
      node: "synthesis",
      who: "Chief",
      role: "Summary",
      text: synthesis.recommendation,
      listTitle: "Claims the evidence may not support", list: state.unverified_claims || [],
    });
  return items;
}

function lastCompleted(events) {
  const times = {};
  events.forEach((e) => {
    if (e.kind === "step_completed" && e.payload?.node) times[e.payload.node] = e.created_at;
  });
  return times;
}

function renderActivity(run, events, controls) {
  const times = lastCompleted(events);
  const agentItems = roleMessages(run).map((m) => ({ ...m, at: times[m.node] || run.completed_at || run.started_at }));
  const ownerItems = controls
    .filter((c) => c.kind === "instruction")
    .map((c) => ({
      owner: true,
      at: c.created_at,
      text: c.text,
      chips: [
        c.target_node ? `for the ${ROLE_LABEL[NODE_ROLE[c.target_node]] || c.target_node}` : "for the whole team",
        c.status === "QUEUED"
          ? c.target_node
            ? "waiting for that agent"
            : "waiting for the next step"
          : `picked up at ${NODE_LABEL[c.received_node] || c.received_node || "the task"}`,
      ],
      // Only a note no agent has taken can still be changed.
      editable: c.status === "QUEUED" ? c.id : null,
    }));
  const items = [...agentItems, ...ownerItems].sort((a, b) => new Date(b.at || 0) - new Date(a.at || 0));
  const running = ACTIVE.includes(run.status) && run.status !== "PAUSED";
  const note = running
    ? `◎ &nbsp; ${esc(ROLE_LABEL[NODE_ROLE[run.current_node]] || "The office")} is working on ${esc(NODE_LABEL[run.current_node] || "the next step")}…`
    : run.status === "PAUSED"
      ? "◎ &nbsp; Agents are paused. Resume when you are ready."
      : NEEDS_OWNER.includes(run.status)
        ? "◎ &nbsp; Work has stopped for you. Nothing has left the office."
        : `◎ &nbsp; ${esc((STATUS[run.status] || [run.status])[0])}. Everything is recorded in this room.`;
  if (!items.length) return `<div class="note">${note}</div>${emptyCard("No messages yet.", "Agents post here as each step finishes.")}`;
  return (
    `<div class="note">${note}</div>` +
    items
      .map((m, index) =>
        m.owner
          ? `<div class="message">${`<span class="avatar owner">${esc((Office.owner || "O").charAt(0).toUpperCase())}</span>`}<div class="message-body"><strong>You <span class="muted">· Direction</span></strong><time>${esc(when(m.at))}</time><p>${esc(m.text)}</p>${m.chips.map((c) => tag(c, "gray")).join(" ")}${
              m.editable
                ? `<span class="button-row tight">${btn("Reword", `direction-edit:${m.editable}`)}${btn("Take it back", `direction-withdraw:${m.editable}`, "danger")}</span>`
                : ""
            }</div></div>`
          : `<div class="message">${avatar(m.who)}<div class="message-body"><strong>${esc(m.who)} <span class="muted">· ${esc(m.role)}</span></strong><time>${esc(when(m.at))}</time>${foldable(m.text, `fold-${index}`)}${
              m.list?.length ? `${m.listTitle ? `<p class="list-title">${esc(m.listTitle)}</p>` : ""}<ul class="plain">${m.list.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""
            }${(m.chips || []).map((c) => tag(c, "gray")).join(" ")}</div></div>`
      )
      .join("")
  );
}

const WORKFLOW_STEPS = ["plan", "research", "strategy", "critique", "synthesis"];

// Where a planned step stands, in words.
const STEP_STATE = { pending: ["Waiting", "gray"], done: ["Done", ""], empty: ["Found nothing", "amber"], failed: ["Failed", "clay"] };

// A task the Chief planned as steps: the steps, what each waits for, and
// where each stands; then the review and the answer, which every task gets.
// The owner's line to a working task: a hint, more context, a change of
// direction. Read by the next agent to look, even in the middle of a step.
function messageBar(run, controls) {
  const notes = (controls || []).filter((c) => c.kind === "instruction");
  const last = notes[notes.length - 1];
  let heard = "";
  if (last) {
    heard =
      last.status === "QUEUED"
        ? `<p class="message-bar-state">${tag("On its way", "amber")} “${esc(roomShortText(last.text))}”: the next agent to look reads it, even mid-step.</p>`
        : `<p class="message-bar-state">${tag("Read")} “${esc(roomShortText(last.text))}” was read at ${esc(NODE_LABEL[last.received_node] || last.received_node || "the task")}${last.received_at ? ` · ${esc(when(last.received_at))}` : ""}.</p>`;
  }
  return `<section class="message-bar" aria-label="Message the team">
    <form class="compose direction" id="direction-form">
      <select name="target_node" aria-label="Who this is for">
        <option value="">The whole team</option>
        ${WORKFLOW_STEPS.filter((node) => node !== "approval")
          .map((node) => `<option value="${esc(node)}">${esc(ROLE_LABEL[NODE_ROLE[node]] || node)} · ${esc(NODE_LABEL[node])}</option>`)
          .join("")}
      </select>
      <input aria-label="Message for your agents" name="text" maxlength="4000" placeholder="Message the team: a hint, more context, a change of direction…" required>
      <button class="primary">Send ↑</button>
    </form>
    ${heard || `<p class="muted message-bar-state">It reaches them within a move or two, even in the middle of a step. To leave a step out, use Skip on the Plan tab.</p>`}
  </section>`;
}

function roomShortText(text) {
  const words = String(text || "").trim();
  return words.length > 90 ? `${words.slice(0, 89)}…` : words;
}

function renderSteps(run, plan) {
  const status = run.state?.step_status || {};
  // Steps the owner chose to skip, and whether more can be skipped now.
  const skipping = new Set((TaskRoom.controls || []).filter((c) => c.kind === "skip").map((c) => c.text));
  const canSkip = ["PENDING", "RUNNING", "RECOVERING", "PAUSED"].includes(run.status);
  const byId = Object.fromEntries(plan.steps.map((s) => [s.id, s]));
  const rows = plan.steps
    .map((s) => {
      const [label, cls] = STEP_STATE[status[s.id] || "pending"] || [status[s.id], "gray"];
      const after = (s.depends_on || []).map((d) => byId[d]?.id || d);
      const skillState = (run.state?.step_skills || {})[s.id];
      const skill = s.skill
        ? tag(skillState && skillState !== "applied" ? `Skill ${s.skill} not available` : `Skill · ${s.skill}`, skillState && skillState !== "applied" ? "gray" : "")
        : "";
      const waiting = (status[s.id] || "pending") === "pending";
      const skip = !waiting
        ? ""
        : skipping.has(s.id)
          ? tag("Skipping", "gray")
          : canSkip
            ? btn("Skip", `step-skip:${s.id}`, "room-tool", `aria-label="Skip: ${esc(s.brief)}"`)
            : "";
      return `<li class="plan-step"><div class="plan-step-head">${tag(s.kind === "write" ? "Write" : s.kind === "browse" ? "Browse" : "Research", s.kind === "write" ? "amber" : "")}<strong>${esc(s.brief)}</strong>${skill}${tag(label, cls)}${skip}</div>
        <p class="muted">${esc(s.id)}${after.length ? ` · after ${esc(after.join(", "))}` : " · starts at once"} · ${esc(s.effort || "standard")} effort${s.added_because ? ` · added because ${esc(s.added_because)}` : ""}</p></li>`;
    })
    .join("");
  const replans = Number(run.state?.replans || 0) + Number(run.state?.revision_count || 0);
  return `<div class="card"><h2>The Chief's plan</h2>
    <p class="muted">${esc(plan.research_question)}</p>
    <ol class="plan-steps">${rows}</ol>
    <p class="muted">Then the critic reviews the work and the Chief writes the answer${
      run.workflow_name === "agent_research" ? "" : "; you are asked to approve only if it proposes something to be done"
    }.${replans ? ` The plan was revised ${replans} time${replans > 1 ? "s" : ""} as the work went.` : ""}</p>
    ${plan.acceptance_criteria?.length ? `<h3>Done when</h3><ul class="plain">${plan.acceptance_criteria.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
  </div>`;
}

function renderPlan(run, events) {
  if (run.state?.plan?.steps?.length) return renderSteps(run, run.state.plan);
  const workflow = run.workflow_name === "agent_research" ? ["plan", "research", "critique", "synthesis"] : ["plan", "research", "strategy", "critique", "synthesis", "approval"];
  const done = new Set(events.filter((e) => e.kind === "step_completed").map((e) => e.payload?.node));
  if (run.approval_status && run.approval_status !== "PENDING") done.add("approval");
  const plan = run.state?.plan;
  return `<div class="card"><h2>One outcome. A visible plan.</h2>
    ${plan ? `<p class="muted">${esc(plan.research_question)}</p>` : ""}
    <ul class="checklist">${workflow
      .map((node) => {
        const current = run.current_node === node && ACTIVE.includes(run.status);
        const who = ROLE_LABEL[NODE_ROLE[node]] || "You";
        return `<li class="${done.has(node) ? "" : "pending"}">${esc(who)} — ${esc(NODE_LABEL[node])}${current ? " · working now" : ""}</li>`;
      })
      .join("")}</ul>
    ${plan?.acceptance_criteria?.length ? `<h3>Done when</h3><ul class="plain">${plan.acceptance_criteria.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
    ${plan?.official_sources?.length ? `<h3>Official pages to check</h3><ul class="plain">${plan.official_sources.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>` : ""}
  </div>`;
}

// What each artifact is, in the owner's terms. "Summary" and "Final report"
// said nothing about which one held the answer, and the answer was in the one
// that sounded like a footnote.
const ARTIFACT_LABEL = {
  task_plan: "Task plan",
  research_report: "Research notes",
  strategy_proposal: "The full report",
  critique: "Critic review",
  synthesis: "The answer",
  final_report: "Task record",
  engineering_report: "Engineering report",
};
// Where the answer lives, best first. The Chief's final answer is written last,
// after the critic's review, so it leads; the report the owner asked for sits
// beneath it. It used to lead with the report, and a revised task showed its
// report while the new final answer sat unread under Artifacts.
const ANSWER_TYPES = ["synthesis", "strategy_proposal", "final_report"];

function renderArtifacts(artifacts) {
  if (!artifacts.length) return emptyCard("No artifacts yet.", "Each finished step saves its output here.");
  return artifacts
    .slice()
    .reverse()
    .map(
      (a) => `<div class="file-card"><div><strong>${esc(ARTIFACT_LABEL[a.type] || a.type)}</strong>${a.type === "synthesis" ? " " + tag("the answer") : a.type === "strategy_proposal" ? " " + tag("the report") : ""}<small>v${a.version} · by ${esc(a.created_by)} · ${esc(when(a.created_at))}</small></div><div class="button-row tight">${btn("Read ↗", `artifact:${a.id}`, "primary")}${downloadRow(a.id)}</div></div>`
    )
    .join("");
}

function policyQuestion(records) {
  return (records || []).find((row) => row.verdict === "ask" && !row.decided_at);
}

// What a task is told before it starts. The office assembled this invisibly:
// an office that decides what its agents know should show its working.
function contextCard(told) {
  if (!told || !told.parts.length) return "";
  const chips = told.parts
    .map(
      (part, index) =>
        `<button type="button" class="chip" data-action="context-open:${index}" title="${esc(part.note)}">${esc(part.name)} <strong>${part.count}</strong></button>`
    )
    .join("");
  TaskRoom.context = told;
  return `<div class="card"><h3>What this task is told</h3>
    <p class="muted">Its own objective, plus everything below. Open one to read it.</p>
    <p class="labels">${chips}</p></div>`;
}

// A task that was picked up again used to show the bare word "Resuming".
// The answer to the task, where the owner is already looking. Two artifacts
// used to be called "Summary" and "Final report"; neither name said which held
// the answer, and the answer sat in the middle of the one that read like an
// engineering log.
// One version of one artifact never changes, so its document is read once.
async function documentOf(artifact) {
  if (TaskRoom.documents[artifact.id]) return TaskRoom.documents[artifact.id];
  const document_ = await api(`/artifacts/${artifact.id}/document`).catch(() => null);
  if (document_ && document_.html) TaskRoom.documents[artifact.id] = document_;
  return document_;
}

// The newest version of each kind: a revision or a rework writes a new one.
async function answerFor(artifacts) {
  const newest = (type) => artifacts.filter((a) => a.type === type).pop();
  const versions = (type) => artifacts.filter((a) => a.type === type).length;
  let answer = null;
  for (const type of ANSWER_TYPES) {
    const found = newest(type);
    if (!found) continue;
    const document_ = await documentOf(found);
    if (document_ && document_.html) {
      answer = { ...document_, artifact: found, versions: versions(type) };
      break;
    }
  }
  if (!answer) return null;
  const report = answer.artifact.type === "synthesis" ? newest("strategy_proposal") : null;
  const reportDocument = report ? await documentOf(report) : null;
  return {
    ...answer,
    report: reportDocument?.html ? { ...reportDocument, artifact: report, versions: versions("strategy_proposal") } : null,
  };
}

function revisedTag(found) {
  // A revised answer says so, and when: the page must show that it changed.
  return found.versions > 1 ? tag(`revised · v${found.artifact.version} · ${when(found.artifact.created_at)}`, "amber") : "";
}

function answerCard(answer, run) {
  if (!answer) return "";
  // The card is already called "The answer"; the document need not say it again.
  let body = answer.html.replace(/^\s*<h2>The answer<\/h2>\s*/i, "");
  if (TaskRoom.workingOpen) body = body.replace('<details class="working"', '<details class="working" open');
  // A task sent back to work still shows its last answer until the new one is written.
  // A finished answer on a task still working means it was sent back to work;
  // without one, what shows is the writer's draft before the Chief's answer.
  const working = run && ["PENDING", "RUNNING", "RECOVERING"].includes(run.status);
  const final = answer.artifact.type === "synthesis";
  const note = !working
    ? ""
    : final
      ? `<p class="answer-note" data-answer-previous>This is the previous answer. The office is working on this task again, and the new answer replaces this one when it finishes.</p>`
      : `<p class="answer-note" data-answer-draft>A draft from the writer. The Chief writes the final answer after the critic's review.</p>`;
  const report = answer.report;
  let reportBody = report ? report.html.replace(/^\s*<h2>The answer<\/h2>\s*/i, "") : "";
  if (report && TaskRoom.workingOpen) reportBody = reportBody.replace('<details class="working"', '<details class="working" open');
  const reportFold = report
    ? `<details class="report-fold"${TaskRoom.reportOpen ? " open" : ""} data-report>
        <summary>The full report ${revisedTag(report) || tag(`v${report.artifact.version}`, "gray")}</summary>
        <div class="artifact-body document">${reportBody}</div>
        <div class="button-row">${btn("Open the report in full", `artifact:${report.artifact.id}`)}${downloadRow(report.artifact.id)}</div>
      </details>`
    : "";
  const reportCard = reportFold ? `<div class="card report-card">${reportFold}</div>` : "";
  // With the editor open beside the task, the room only says where the answer is.
  const editor = typeof AnswerEditor !== "undefined" && AnswerEditor.available() && AnswerEditor.run?.id === run.id;
  if (editor && AnswerEditor.open) return AnswerEditor.card() + reportCard;
  const opener = editor
    ? btn(AnswerEditor.editable() ? "Edit in the editor" : "Open in the editor", "editor-open", "primary")
    : btn("Open in full", `artifact:${answer.artifact.id}`, "primary");
  return `<div class="card answer" data-answer-place data-answer-id="${esc(answer.artifact.id)}"><div class="card-top"><h2>The answer</h2><span class="labels">${revisedTag(answer)}${tag(`by the ${esc(answer.artifact.created_by || "office")}`)}</span></div>
    ${note}
    <div class="artifact-body document">${body}</div>
    <div class="button-row">${opener}${isBuild(run) ? "" : btn("Open as a page ↗", `answer-page:${answer.artifact.id}`) + downloadRow(answer.artifact.id)}</div>
    ${reportFold}</div>`;
}

// What the office's own browser did for this task: its latest page as a
// picture, and each step it took, each with the screenshot it took then.
function browserCard(run, events) {
  const steps = (events || []).filter((event) => event.kind === "browser_step").map((event) => event.payload || {});
  if (!steps.length) return "";
  const shot = (name) => `/runs/${esc(run.id)}/browser/${esc(name)}`;
  const last = [...steps].reverse().find((step) => step.shot);
  const live = ["RUNNING", "PENDING", "RECOVERING"].includes(run.status);
  const rows = steps
    .slice(-15)
    .map(
      (step) => `<li><span class="browser-n">${esc(step.step)}</span><span><strong>${esc(step.said || step.action)}</strong><small>${esc(step.title || step.url || "")}</small></span>${
        step.shot ? `<button type="button" class="browser-thumb" data-action="browser-shot:${esc(step.shot)}" aria-label="See step ${esc(step.step)}"><img src="${shot(step.shot)}" alt="" loading="lazy"></button>` : ""
      }</li>`
    )
    .join("");
  return `<section class="card browser-card" aria-label="The office browser"><div class="card-top"><h2>The office browser</h2>${live ? tag("working", "amber") : tag(`${steps.length} steps`, "gray")}</div>
    ${last ? `<figure class="browser-now"><img src="${shot(last.shot)}" alt="What the office browser showed at step ${esc(last.step)}"><figcaption>${esc(last.title || "")} · <span class="mono">${esc(last.url || "")}</span></figcaption></figure>` : ""}
    <ol class="browser-steps">${rows}</ol>
    <p class="muted">A sandboxed browser with none of your logins. Anything that signs in, submits, sends or pays asks you first.</p></section>`;
}

// A role moved to the backup the owner chose for its provider.
function failoverNote(events) {
  const moved = (events || []).filter((event) => event.kind === "model_failed_over");
  if (!moved.length) return "";
  const why = { no_credit: "ran out of credit", key_refused: "refused its key", failing: "kept failing" };
  const lines = moved.map((event) => {
    const p = event.payload || {};
    return `${esc(ROLE_LABEL[p.role] || p.role || "A role")} moved to <strong>${esc(p.to)}</strong> at ${esc(when(event.created_at || event.at))}: ${esc(p.from)} ${esc(why[p.reason] || "kept failing")}.`;
  });
  return `<div class="attention" data-failover><span class="attention-icon">⇄</span><div><strong>Moved to your backup</strong>
    <p>${lines.join("<br>")} The task's budget and limits are unchanged.</p></div>${btn("See providers", "go-providers")}</div>`;
}

function recoveryNote(run, events) {
  const recovered = (events || []).filter((event) => event.kind === "run_recovered");
  if (!recovered.length) return "";
  const last = recovered[recovered.length - 1];
  const node = (last.payload || {}).from_node;
  const where = node ? `It continues from ${esc(NODE_LABEL[node] || node)}.` : "It continues from its last completed step.";
  const again = recovered.length > 1 ? ` This has happened ${recovered.length} times.` : "";
  return `<div class="attention calm"><span class="attention-icon">↻</span><div><strong>This task was picked up again</strong>
    <p>The worker holding it stopped before it finished, so the office reclaimed it. Nothing completed was lost. ${where}${esc(again)}</p></div></div>`;
}

// How the task moved across the floor: the rooms it passed through,
// where it stopped at the door and what the owner chose, from its events.
const FLOOR_OF_NODE = {
  plan: "Chief's office", replan: "Chief's office", synthesis: "Chief's office",
  research: "Research bay", work: "Research bay", strategy: "Writing room",
  critique: "Review desk", approval: "Your in-tray", engineering: "Other desks",
};
function floorPath(events) {
  const stops = [];
  for (const event of events || []) {
    const p = event.payload || {};
    let place = "";
    let said = "";
    if (event.kind === "step_started" && FLOOR_OF_NODE[p.node]) {
      place = FLOOR_OF_NODE[p.node];
      said = NODE_LABEL[p.node] ? `${NODE_LABEL[p.node].replace(/^the /, "")}.` : "";
      said = said.charAt(0).toUpperCase() + said.slice(1);
    } else if (event.kind === "budget_ask") {
      place = "Stopped at the door";
      said = `Reached its ${p.limit === "cost" ? "spending" : p.limit || "token"} limit.`;
    } else if (event.kind === "budget_raised") {
      place = "You";
      said = "Raised its limit.";
    } else if (event.kind === "finish_requested") {
      place = "You";
      said = "Chose to finish with what it has.";
    } else if (event.kind === "run_finished") {
      place = "Done";
      said = (STATUS[p.status] || [p.status || "Finished"])[0] + ".";
    } else continue;
    const last = stops[stops.length - 1];
    if (last && last.place === place && last.said === said) continue;
    stops.push({ place, said, at: event.created_at || event.at });
  }
  return stops;
}

function floorPathCard(events) {
  const stops = floorPath(events);
  if (stops.length < 2) return "";
  const shown = stops.slice(-8);
  const items = shown
    .map((stop) => `<li class="${stop.place === "Stopped at the door" ? "is-stop" : stop.place === "You" ? "is-you" : ""}"><time>${esc(clock(stop.at))}</time><span><strong>${esc(stop.place)}.</strong> ${esc(stop.said)}</span></li>`)
    .join("");
  const earlier = stops.length > shown.length ? `<p class="muted">${stops.length - shown.length} earlier moves are in Activity.</p>` : "";
  return `<section class="card floor-path" aria-label="How it moved across the floor"><h2>How it moved across the floor</h2>${earlier}<ol>${items}</ol></section>`;
}

function clock(value) {
  const at = value ? new Date(value) : null;
  return at && !Number.isNaN(at.getTime()) ? at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
}

function attentionFor(run, question, ceiling, events = []) {
  if (question)
    return `<div class="attention"><span class="attention-icon">⚖</span><div><strong>A rule is asking you</strong><p>${esc(question.reason || "This task wants to do something a rule holds back.")}${question.detail?.url ? ` (${esc(question.detail.url)})` : ""}</p></div>
      <div class="button-row tight">${btn("Refuse", `policy-answer:${question.id}:no`, "danger")}${btn("Allow for this task", `policy-answer:${question.id}:yes`, "primary")}</div></div>`;
  if (run.status === "WAITING_FOR_APPROVAL" && run.state?.plan?.task_kind === "build")
    return `<div class="attention build-ready"><span class="attention-icon">⚒</span><div><strong>Ready to build</strong><p>Read what will be made, then press Start building. The coder on your Mac writes it in its own worktree; nothing runs until you do.</p></div>${btn("Review and start building →", "approval", "primary")}</div>`;
  if (run.status === "WAITING_FOR_APPROVAL")
    return `<div class="attention"><span class="attention-icon">!</span><div><strong>Your approval is needed</strong><p>Read the proposal and decide. Nothing is executed or published without you.</p></div>${btn("Review proposal →", "approval")}</div>`;
  const asked = ownerQuestions(run);
  if (run.status === "NEEDS_HUMAN_REVIEW" && run.state?.stopped_reason === "needs_owner_answers" && asked.length)
    return `<div class="attention"><span class="attention-icon">?</span><div><strong>The Chief has ${asked.length === 1 ? "a question" : "questions"} before starting</strong>
      <ul class="owner-questions">${asked.map((q) => `<li>${esc(q)}</li>`).join("")}</ul></div>${btn("Answer and continue →", "answer-owner", "primary")}</div>`;
  if (run.status === "NEEDS_HUMAN_REVIEW" && run.state?.stopped_reason === "providers_unavailable") return providersDownCard(run);
  if (reviewable(run))
    return `<div class="attention"><span class="attention-icon">!</span><div><strong>This task needs your review</strong><p>${esc(stopText(run.state?.stopped_reason))} The critic's concerns are below: read them, then accept, revise or close.</p></div>${btn("Read the concerns ↓", "review-jump")}</div>`;
  if (["NEEDS_HUMAN_REVIEW", "BLOCKED", "FAILED"].includes(run.status))
    return `<div class="attention"><span class="attention-icon">!</span><div><strong>${run.status === "FAILED" ? "This task stopped with an error" : "This task needs your review"}</strong><p>${esc(stopText(run.state?.stopped_reason || run.error) || "The office could not finish on its own.")}</p></div>${btn("Decide what happens →", "review")}</div>`;
  // A task held at a ceiling has its own panel below, with the choices that
  // work. Offering Resume here would stop it again the moment it started.
  if (ceiling) return "";
  if (run.status === "PAUSED" && typeof Ceiling === "object" && Ceiling.stuck && Ceiling.runId === run.id) return stuckCard(run.id, Ceiling.stuck);
  const lastPause = [...(events || [])].reverse().find((e) => e.kind === "run_paused");
  if (run.status === "PAUSED" && lastPause?.payload?.reason === "monthly_limit_reached")
    return `<div class="attention"><span class="attention-icon">!</span><div><strong>Paused: this month's limit is reached</strong><p>It waited its turn and the month's spending limit was reached meanwhile, so it did not start. Raise the limit under Settings → Budget & limits, then resume.</p></div>${btn("Resume", "resume")}</div>`;
  if (run.status === "PAUSED") return `<div class="attention calm"><span class="attention-icon">॥</span><div><strong>Task is paused</strong><p>Agents wait until you resume. You can still read everything.</p></div>${btn("Resume", "resume")}</div>`;
  if (run.team_proposal?.objective) return teamProposalCard(run.team_proposal);
  return "";
}

// The lead cannot build or run the whole pipeline itself: it proposes a team
// run, and this is where the owner starts it. A build then comes back to the
// owner for Start building, once the team has planned it.
function teamProposalCard(proposal) {
  const what = `<p class="idea-quote">${esc(roomShort(proposal.objective, 240))}</p>`;
  if (proposal.started_run_id)
    return `<div class="attention calm"><span class="attention-icon">✓</span><div><strong>The full team is on it</strong>${what}</div><a class="button" href="#task/${esc(proposal.started_run_id)}">Open the team's task →</a></div>`;
  return `<div class="attention"><span class="attention-icon">⇢</span><div><strong>The lead proposes a full team run</strong>${what}<p>${esc(proposal.why || "It needs more than the lead can do on its own.")} The team plans it first; if it builds something, you read the plan and press Start building there.</p></div>${btn("Start the team run…", "team-run", "primary")}</div>`;
}

// No research could run: the model providers were down or refusing.
// Reviewing would not help; trying again once they answer will.
const PROVIDER_CODE_TEXT = {
  rate_limited: "rate-limited",
  provider_unavailable: "unavailable",
  provider_unreachable: "unreachable",
  gateway_unreachable: "the office gateway was unreachable",
  role_has_no_model: "without a model for this role (choose one under Settings → Models)",
  provider_timeout: "timing out",
  insufficient_balance: "out of credit",
  quota_exhausted: "out of quota",
  credential_invalid: "refusing the key",
  permission_denied: "refusing the key",
  credential_unavailable: "missing its key",
};
function providersDownCard(run) {
  const failure = run.state?.provider_failure || {};
  const said = [...new Set((failure.codes || []).map((code) => PROVIDER_CODE_TEXT[code] || code.replace(/_/g, " ")))];
  const steps = failure.steps ? `${failure.steps} research step${failure.steps === 1 ? "" : "s"}` : "The research";
  return `<div class="attention" data-providers-down><span class="attention-icon">⇄</span><div><strong>The research could not run</strong>
    <p>${esc(steps)} failed because the model providers were ${esc(said.join(", ") || "not answering")}. Nothing is wrong with the task itself, and nothing more was spent on it. Try again when they answer; Settings → Models shows each provider and its backup.</p></div>
    <div class="button-row tight">${btn("See providers", "go-providers")}${btn("Try the research again", "providers-retry", "primary")}</div></div>`;
}

const SETTLED_FOR_FOLLOW_UP = ["COMPLETED", "FAILED", "CANCELLED"];

// The ask, in full, under the short title. Nothing a title drops is lost.
function askedCard(run) {
  const asked = String(run.objective || "").trim();
  if (!asked || asked === run.title) return "";
  return `<div class="card asked"><h2>What was asked</h2><p>${esc(asked).replace(/\n/g, "<br>")}</p></div>`;
}

// A thread reads as a thread: what this continues, and what continued it.
// This task's line of work: what it built on and what built on it,
// at any depth, what it was told and who was told it, what it taught.
function lineageCard(seen) {
  if (!seen) return "";
  const task = (item) => {
    const where = item.project ? ` <span class="muted">in ${esc(item.project)}</span>` : "";
    return `${btn(esc(item.title), `open:${item.run_id}`)} ${statusTag(item.status)}${where}`;
  };
  const depth = (item, near, far) => (item.hops > 1 ? `${far} (${item.hops} back)` : near);
  const rows = [];
  [...(seen.built_on || [])].forEach((item) => rows.push(`<p data-lineage="built-on">${esc(depth(item, "Continues", "Earlier"))} ${task(item)}</p>`));
  (seen.built_upon || []).forEach((item) => rows.push(`<p data-lineage="built-upon">${esc(item.hops > 1 ? `Then (${item.hops} on)` : "Followed by")} ${task(item)}</p>`));
  (seen.told_about || []).forEach((item) => rows.push(`<p data-lineage="told-about">Was told about ${task(item)}</p>`));
  (seen.told_to || []).forEach((item) => rows.push(`<p data-lineage="told-to">Was told to ${task(item)}</p>`));
  const notes = [];
  if (seen.serves) notes.push(`Serves <strong>${esc(seen.serves)}</strong>`);
  if (seen.taught?.length) notes.push(`Taught ${esc(seen.taught.length)} lesson${seen.taught.length === 1 ? "" : "s"}`);
  if (seen.noted?.length) {
    const subjects = [...new Set(seen.noted.map((n) => n.subject))];
    notes.push(`Noted ${esc(seen.noted.length)} fact${seen.noted.length === 1 ? "" : "s"} about ${esc(subjects.join(", "))}`);
  }
  if (seen.asked_about) notes.push(`Asked about ${esc(seen.asked_about)} time${seen.asked_about === 1 ? "" : "s"} in its room`);
  if (!rows.length && !notes.length) return "";
  return `<div class="card thread" data-lineage-card><h2>This line of work</h2>${rows.join("")}${notes.length ? `<p class="muted">${notes.join(" · ")}</p>` : ""}</div>`;
}

// A task that builds software: its next step is a new version, not a report.
function isBuild(run) {
  return run?.state?.plan?.task_kind === "build" || Boolean(run?.state?.build_folder);
}

function roomActions(run, ceiling) {
  const buttons = [];
  // The task's project has a room where its work is talked over.
  if (run.project_id) buttons.push(`<a class="button" href="#room/${esc(run.project_id)}">Open the room</a>`);
  if (SETTLED_FOR_FOLLOW_UP.includes(run.status) && isBuild(run)) buttons.push(btn("Make the next version…", "follow-up:next-version", "primary"));
  else if (SETTLED_FOR_FOLLOW_UP.includes(run.status)) buttons.push(btn("Rework", "rework"), btn("Ask a follow-up", "follow-up", "primary"));
  // Not finished, so not a follow-up: a new task that is told what this one has found so far.
  else buttons.push(btn("＋ New task from this", "new-task"));
  if (["PENDING", "RUNNING", "RECOVERING"].includes(run.status)) buttons.push(btn("Pause", "pause"));
  if (run.status === "PAUSED" && !ceiling) buttons.push(btn("Resume", "resume"));
  if ([...ACTIVE, ...NEEDS_OWNER].includes(run.status)) buttons.push(btn("Cancel task", "cancel", "danger"));
  return `<div class="button-row">${buttons.join("")}</div>`;
}

async function renderRoom(content, runId) {
  const [run, events, controls, artifacts, usage, policyRecords] = await Promise.all([
    api(`/runs/${runId}`),
    taskEvents.read(runId),
    api(`/runs/${runId}/controls`),
    api(`/runs/${runId}/artifacts`),
    api(`/runs/${runId}/usage`),
    api(`/policies/records?run_id=${runId}`).catch(() => []),
  ]);
  const thread = await api(`/runs/${runId}/thread`).catch(() => null);
  const seen = await api(`/runs/${runId}/lineage`).catch(() => null);
  // The workflow, drawn live, only when it is the tab being read.
  // The room is read from the same flow, so both tabs (and the tab's count) have it.
  const flow = await api(`/runs/${runId}/flow`).catch(() => null);
  // The follow-up dialog offers the same shapes as a new task.
  if (!Tasks.deliverables) Tasks.deliverables = await api("/deliverables").catch(() => null);
  TaskRoom.thread = thread;
  // What this task is actually told. Assembled invisibly until now.
  // What this task was told, stored with it; not what its project holds now.
  const told = await api(`/runs/${runId}/context`).catch(() => null);
  // The answer, read straight into the room. It used to be three clicks away,
  // inside whichever artifact happened to hold it.
  TaskRoom.answer = await answerFor(artifacts);
  // What a paused task is waiting on, if it is waiting on the owner at all.
  const ceiling = await ceilingQuestion(runId, run.status);
  const question = policyQuestion(policyRecords);
  TaskRoom.run = run;
  TaskRoom.controls = controls;
  // The answer beside the task, in its editor; not awaited, it loads on its own.
  if (typeof AnswerEditor !== "undefined") AnswerEditor.sync(run, TaskRoom.answer);
  // The trail with this task's own name as its last step.
  renderTrail({ title: run.title || run.objective.slice(0, 60) });
  const spent = usage.spent || {};
  const budget = usage.budget || {};
  const cost = spent.cost_reported ? `$${Number(spent.cost_usd || 0).toFixed(2)}` : `${(spent.tokens || 0).toLocaleString()} tokens`;
  // Paid tool calls are spending, not a member of the working group.
  // One chip per name: the planner and the summariser are both the Chief.
  const byName = new Map();
  for (const row of usage.by_role || []) {
    if (row.role === "tools") continue;
    const name = ROLE_LABEL[row.role] || row.role;
    const seen = byName.get(name) || { role: row.role, calls: 0 };
    seen.calls += row.calls || 0;
    byName.set(name, seen);
  }
  const roles = [...byName.values()].map((entry) => entry.role);
  const paid = (usage.by_role || []).find((r) => r.role === "tools");
  const group = (roles.length ? roles : ["chief", "researcher", "strategist", "critic"]).map(
    (role, i) => `<div class="agent-chip">${avatar(ROLE_LABEL[role] || role, i)}<span>${esc(ROLE_LABEL[role] || role)}<small>${esc(byName.get(ROLE_LABEL[role] || role)?.calls || 0)} calls</small></span></div>`
  );
  const canDirect = ["PENDING", "RUNNING", "RECOVERING", "PAUSED"].includes(run.status);
  const tabs = [
    ["workflow", "Workflow"],
    ["room", flow?.room?.count ? `Team room (${flow.room.count})` : "Team room"],
    ["activity", "Activity"],
    ["plan", "Plan"],
    ["artifacts", `Artifacts (${artifacts.length})`],
    ["calls", "Model calls"],
  ];
  const body =
    TaskRoom.tab === "workflow" ? renderFlow(flow) : TaskRoom.tab === "room" ? renderTeamRoom(flow) : TaskRoom.tab === "plan" ? renderPlan(run, events) : TaskRoom.tab === "artifacts" ? renderArtifacts(artifacts) : TaskRoom.tab === "calls" ? modelCallsTab() : renderActivity(run, events, controls);
  content.innerHTML =
    heading(
      run.workflow_name === "agent_research" ? "RESEARCH TASK" : "TASK",
      run.title || run.objective,
      `${(STATUS[run.status] || [run.status])[0]} · started ${when(run.started_at) || "soon"} · ${cost} so far`,
      btn("✎ Rename", `title-rename:task:${run.id}`, "rename-btn", 'aria-label="Rename this task"')
    ) +
    // Where this work sits: its project, and the line it is judged by.
    (run.project_id ? `<p class="task-where">In <a href="#project/${esc(run.project_id)}">${esc(run.project_name || "its project")}</a></p>${lineLabel(run.business_line)}` : "") +
    // Actions in one row under the title, where the eye goes next; stacked
    // beside a long title they read as part of it.
    `<div class="task-actions">${roomActions(run, ceiling)}</div>` +
    // Talk to the team while it works: near the top, never below the tabs.
    (canDirect ? messageBar(run, controls) : "") +
    // The work in a reading column; the details beside it, not under the answer.
    `<div class="task-columns"><div class="task-main">` +
    askedCard(run) +
    lineageCard(seen) +
    attentionFor(run, question, ceiling, events) +
    critiqueCard(run) +
    ceilingCard(ceiling) +
    recoveryNote(run, events) +
    failoverNote(events) +
    browserCard(run, events) +
    buildingNow(run, events) +
    answerCard(TaskRoom.answer, run) +
    (typeof taskVerdictSlot === "function" ? taskVerdictSlot(run) : "") +
    (run.state?.build_folder ? `<div id="build-card" class="card build-card"><h2>What was built</h2><p class="muted">Reading the project folder…</p></div>` : "") +
    (run.state?.approval_id ? `<div id="coder-card" class="card coder-card" hidden></div>` : "") +
    floorPathCard(events) +
    `<section class="task-work">
      <div class="subnav" role="tablist">${tabs.map(([id, label]) => `<button type="button" role="tab" aria-selected="${TaskRoom.tab === id}" class="${TaskRoom.tab === id ? "active" : ""}" data-action="tab:${id}">${esc(label)}</button>`).join("")}</div>
      ${body}
    </section></div>
    <aside class="task-aside">
      <div class="card"><h3>Your working group</h3><div class="agent-chain wrap">${group.join("")}</div><p class="muted">Each role checks the work of the one before it. You control spending and every external action.</p></div>
      ${policyRecords.length ? `<div class="card"><h3>Rules</h3>${policyRecords.slice(0, 5).map((row) => `<div class="detail-line"><span>${esc(row.policy || "rule")} · ${esc(row.target)}<br><small class="muted">${esc(row.reason || "")}</small></span><strong>${esc(row.decision || row.verdict)}</strong></div>`).join("")}</div>` : ""}
      ${contextCard(told)}
      <div class="card"><h3>Outcome & boundaries</h3>
        <div class="detail-line"><span>Tokens</span><strong>${(spent.tokens || 0).toLocaleString()} / ${budget.max_tokens ? Number(budget.max_tokens).toLocaleString() : "∞"}</strong></div>
        <div class="detail-line"><span>Cost</span><strong>${spent.cost_reported ? `$${Number(spent.cost_usd || 0).toFixed(2)}` : "not reported"}${budget.max_cost_usd ? ` / $${budget.max_cost_usd}` : ""}</strong></div>
        ${paid ? `<div class="detail-line"><span>Paid tools</span><strong>$${Number(paid.cost_usd || 0).toFixed(2)} over ${esc(paid.calls)} calls</strong></div>` : ""}
        <div class="detail-line"><span>Time limit</span><strong>${budget.max_minutes ? `${budget.max_minutes} min` : "none"}</strong></div>
        <div class="detail-line"><span>Sharing</span><strong>Owner approval</strong></div>
        <p class="muted">This task's own limits. They start from the project's and only apply here.</p>
        <div class="button-row">
          ${LIMIT_EDITABLE_UI.includes(run.status) ? btn("Change limits", "limits-edit") : ""}
          ${run.status === "COMPLETED" ? btn("Save as template", "template") : ""}
          ${["COMPLETED", "NEEDS_HUMAN_REVIEW"].includes(run.status) ? btn("What did it learn?", "learned") : ""}
        </div>
      </div>
    </aside></div>`;
  // Filled into this render's own card: the screen may still be off the page.
  if (run.state?.build_folder) loadBuildCard(run, content, buildSteps(events));
  if (run.state?.approval_id && typeof loadCoderCard === "function") loadCoderCard(run, content);
  if (TaskRoom.tab === "calls") loadModelCalls(run.id, content);
  if (content.querySelector("[data-live-build]")) LiveBuild.start(run.id);
  else if (LiveBuild.run === run.id) LiveBuild.stop();
}

// What a build made: whether its checks passed, its files, a preview and a download.
const BUILD_ERRORS = {
  tests_failed: "Its own checks failed, so the office did not keep it.",
  no_changes: "The coding agent made no files.",
  out_of_scope: "It changed files outside its folder, so the office did not keep it.",
  code_execution_disabled: "Code execution is off for this office, so nothing was built.",
  "runtime:runtime_failed": "The coding agent stopped with an error.",
  landing_failed: "It was built and checked, but could not be saved into its folder; the change is kept as a patch.",
  no_test_command: "It had no check to run.",
  build_unfinished: "The coding agent stopped before finishing, and again after the office resumed it once.",
};

// The latest build's steps: from where the coding agent last started.
// Steps the record keeps; the live view's own (where, session, now, plan) are not steps.
const RECORDED_STEPS = ["phase", "file", "command", "note", "check", "thinking", "owner", "paused"];
function buildSteps(events) {
  const steps = (events || []).filter((e) => e.kind === "build_step" && RECORDED_STEPS.includes(e.payload?.kind));
  let start = 0;
  steps.forEach((e, i) => {
    if (e.payload?.kind === "phase" && /is writing it/.test(e.payload?.text || "")) start = i;
  });
  return steps.slice(start);
}

const BUILD_MARK = { file: "✎", command: "›", note: "·", check: "✓", phase: "◆", thinking: "…", owner: "✉", paused: "‖" };

function buildStepRows(steps) {
  return `<ol class="build-steps">${steps
    .map((e) => {
      const p = e.payload || {};
      const failed = p.ok === false;
      // An edit opens to its change; the owner's own words read as theirs.
      const text = p.kind === "owner" ? `You: ${p.text || ""}` : p.text || "";
      const body = p.diff
        ? `<details class="build-diff"><summary>${esc(text)}</summary><pre>${diffLines(p.diff)}</pre></details>`
        : esc(text);
      return `<li class="build-step ${esc(p.kind || "")}${failed ? " failed" : ""}"><span class="build-mark" aria-hidden="true">${failed ? "✗" : BUILD_MARK[p.kind] || "·"}</span><span>${body}</span><time>${esc(when(e.created_at))}</time></li>`;
    })
    .join("")}</ol>`;
}

// A diff as lines, added and removed marked. The text came from the build: escaped.
function diffLines(diff) {
  return String(diff)
    .split("\n")
    .filter((line) => !/^(diff --git|index |--- |\+\+\+ )/.test(line))
    .map((line) => {
      const kind = line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "";
      return `<span class="${kind}">${esc(line)}</span>`;
    })
    .join("\n");
}

// While the coding agent works: what you see is what it is making.
// The card fills itself from /build/live every two seconds; the room around it
// stays still, so the preview and a half-typed comment are never redrawn away.
function buildingNow(run, events) {
  const steps = buildSteps(events);
  const running = ["RUNNING", "RECOVERING"].includes(run.status);
  if (!running || !steps.length) return "";
  const began = new Date(steps[0].created_at || Date.now());
  const minutes = Math.max(0, Math.floor((Date.now() - began.getTime()) / 60000));
  const stopping = (TaskRoom.controls || []).some((c) => c.kind === "cancel" && c.status === "QUEUED");
  const recent = steps.slice(-40);
  return `<div class="card build-live" data-building-now data-live-build="${esc(run.id)}">
    <div class="card-top"><h2>Building now</h2><span class="labels" data-live-clock>${tag(`${minutes} min of up to 15`, "gray")}</span></div>
    <div class="live-grid">
      <div class="live-work">
        <div data-live-plan></div>
        <p class="live-now" data-live-now hidden></p>
        <div data-live-steps>${steps.length > recent.length ? `<p class="muted">${steps.length - recent.length} earlier steps are kept below once it finishes.</p>` : ""}${buildStepRows(recent)}</div>
      </div>
      <div class="live-view" data-live-view><p class="muted">The page shows here as soon as it has one.</p></div>
    </div>
    <form id="live-note-form" class="live-note">
      <p class="live-pointed" data-live-pointed hidden></p>
      <label class="field" for="live-note-text">Say something to it while it builds</label>
      <textarea id="live-note-text" name="text" rows="2" maxlength="4000" placeholder="For example: make the header blue, and drop the second page."></textarea>
      <div class="button-row tight">
        <select name="mode" aria-label="When it hears it"><option value="next">At its next step</option><option value="now">Now (interrupt it)</option></select>
        <button class="primary">Send</button>
        ${btn("Point at something", "live-point")}
        <span data-live-hold>${btn("Pause after this step", "live-pause")}</span>
      </div>
      <div data-live-notes></div>
    </form>
    <div class="button-row">${stopping ? tag("Stopping…", "amber") : btn("Stop the build", "build-stop", "danger")}</div>
  </div>`;
}

const LiveBuild = {
  run: null,
  timer: null,
  changed: 0,
  pointed: null,
  active: false,
  start(runId) {
    if (this.run === runId && this.timer) return;
    this.stop();
    this.run = runId;
    this.active = true;
    this.changed = 0;
    this.timer = setInterval(() => this.tick(), 2000);
    setTimeout(() => this.tick(), 50);
  },
  stop() {
    clearInterval(this.timer);
    this.timer = null;
    this.active = false;
    this.run = null;
    this.pointed = null;
  },
  card() {
    return this.run ? document.querySelector(`[data-live-build="${CSS.escape(this.run)}"]`) : null;
  },
  async tick() {
    const runId = this.run;
    if (!runId || document.hidden) return;
    if (!this.card()) {
      // The owner went elsewhere.
      if (location.hash.indexOf(runId) < 0) this.stop();
      return;
    }
    let live;
    try {
      live = await api(`/runs/${runId}/build/live`);
    } catch (error) {
      console.error("Live build view unavailable", error);
      return;
    }
    if (runId !== this.run) return;
    if (!live.building) {
      // Finished or stopped: the room shows its record now.
      this.stop();
      await rerender();
      return;
    }
    this.draw(live);
  },
  draw(live) {
    const card = this.card();
    if (!card) return;
    const minutes = live.began ? Math.max(0, Math.floor((Date.now() - new Date(live.began).getTime()) / 60000)) : 0;
    const clock = card.querySelector("[data-live-clock]");
    if (clock) clock.innerHTML = `${tag(live.agent || "Coder on your Mac", "gray")} ${tag(`${minutes} min of up to 15`, "gray")}${live.paused ? " " + tag("Paused", "amber") : ""}`;
    const plan = card.querySelector("[data-live-plan]");
    if (plan)
      plan.innerHTML = (live.plan || []).length
        ? `<ul class="live-plan">${live.plan.map((item) => `<li class="${item.done ? "done" : ""}"><span aria-hidden="true">${item.done ? "✓" : "○"}</span> ${esc(item.text)}</li>`).join("")}</ul>`
        : "";
    const now = card.querySelector("[data-live-now]");
    if (now) {
      now.hidden = !live.now && !live.paused;
      now.textContent = live.paused ? "Paused between steps. Continue, or say what to change." : live.now ? `Now: ${live.now}` : "";
    }
    const steps = card.querySelector("[data-live-steps]");
    if (steps) {
      const list = steps.querySelector(".build-steps");
      const atBottom = !list || list.scrollTop + list.clientHeight >= list.scrollHeight - 8;
      const open = [...steps.querySelectorAll("details[open] summary")].map((s) => s.textContent);
      steps.innerHTML =
        (live.earlier ? `<p class="muted">${live.earlier} earlier steps are kept below once it finishes.</p>` : "") +
        buildStepRows((live.steps || []).map((step) => ({ payload: step, created_at: step.at })));
      steps.querySelectorAll("details").forEach((d) => {
        if (open.includes(d.querySelector("summary")?.textContent)) d.open = true;
      });
      const fresh = steps.querySelector(".build-steps");
      if (fresh && atBottom) fresh.scrollTop = fresh.scrollHeight;
    }
    const view = card.querySelector("[data-live-view]");
    if (view && live.preview) {
      let frame = view.querySelector("iframe");
      if (!frame) {
        view.innerHTML = `<iframe title="The page as it is being built" sandbox="allow-scripts allow-modals allow-forms" referrerpolicy="no-referrer"></iframe><p class="muted">Updates as files change. ${`<a href="${esc(live.preview)}" target="_blank" rel="noopener noreferrer">Open it full size ↗</a>`}</p>`;
        frame = view.querySelector("iframe");
      }
      if (live.changed !== this.changed) {
        this.changed = live.changed;
        frame.src = `${live.preview}?t=${encodeURIComponent(live.changed)}`;
      }
    }
    const hold = card.querySelector("[data-live-hold]");
    if (hold) {
      const asked = (live.notes || []).some((n) => n.kind === "build_pause" && n.status === "QUEUED");
      hold.innerHTML = live.paused ? btn("Continue", "live-continue", "primary") : asked ? tag("Pausing after this step…", "amber") : btn("Pause after this step", "live-pause");
    }
    const notes = card.querySelector("[data-live-notes]");
    if (notes) {
      const said = (live.notes || []).filter((n) => n.text);
      notes.innerHTML = said.length
        ? `<ul class="live-notes">${said.map((n) => `<li>${esc(n.text)} ${n.status === "QUEUED" ? tag(n.kind === "build_now" ? "Interrupting…" : "Waiting for its next step", "amber") : tag("Given to it", "gray")}</li>`).join("")}</ul>`
        : "";
    }
  },
  pick(picked) {
    // From the build's own page: text only, shown escaped, sent as the owner's context.
    this.pointed = {
      tag: String(picked?.tag || "").slice(0, 20),
      text: String(picked?.text || "").slice(0, 160),
      selector: String(picked?.selector || "").slice(0, 200),
    };
    const card = this.card();
    const shown = card?.querySelector("[data-live-pointed]");
    if (shown) {
      shown.hidden = false;
      shown.innerHTML = `Pointing at ${esc(`<${this.pointed.tag}>`)}${this.pointed.text ? ` “${esc(this.pointed.text)}”` : ""} ${btn("×", "live-unpoint", "room-tool")}`;
    }
    card?.querySelector("#live-note-text")?.focus();
  },
};

window.addEventListener("message", (event) => {
  const frame = LiveBuild.card()?.querySelector("[data-live-view] iframe");
  if (!frame || event.source !== frame.contentWindow) return;
  if (event.data && typeof event.data === "object" && event.data.officePicked) LiveBuild.pick(event.data.officePicked);
});

async function loadBuildCard(run, content, steps = []) {
  const card = content?.querySelector("#build-card") || document.getElementById("build-card");
  if (!card) return;
  let built = null;
  try {
    built = await api(`/runs/${run.id}/build`);
  } catch (error) {
    if (error instanceof SignedOut) return;
    card.innerHTML = `<h2>What was built</h2><p class="muted">Not built yet.</p>`;
    return;
  }
  const files = built.files || [];
  const ok = built.status === "completed" && built.landed;
  const errors = (built.errors || []).map((code) => BUILD_ERRORS[code] || String(code).replace(/_/g, " "));
  const state = built.status
    ? ok
      ? `<p>${tag("Built")} Its checks passed and the files are kept in the office's project folder.</p>`
      : `<p>${tag("Not finished", "clay")} ${esc(errors.join(" ") || "The build did not finish.")}</p>`
    : files.length
      ? `<p>${tag("From the build this one follows", "gray")} The new build has not run yet.</p>`
      : `<p class="muted">Nothing built yet. Press Start building to begin.</p>`;
  const list = files.length
    ? `<details class="build-files"${files.length <= 12 ? " open" : ""}><summary>${files.length} file${files.length === 1 ? "" : "s"}</summary><ul class="plain">${files
        .map((f) => `<li>${btn(esc(f.path), `build-file:${encodeURIComponent(f.path)}`, "room-tool")} <small class="muted">${Math.max(1, Math.round(f.bytes / 1024))} KB</small></li>`)
        .join("")}</ul></details>`
    : "";
  TaskRoom.built = built;
  const versions = built.versions || [];
  const latest = versions[versions.length - 1];
  const zipOf = (commit) => `/runs/${esc(run.id)}/build.zip${commit ? `?v=${esc(commit)}` : ""}`;
  const mineLine = built.version
    ? `<p class="build-version">${tag(`Version ${built.version}`)} ${built.version === versions.length ? "The latest version of this project." : `This task made version ${built.version}; the project is now at version ${versions.length}.`}</p>`
    : "";
  const versionRows = versions
    .slice()
    .reverse()
    .map((v) => {
      const counts = ["added", "edited", "removed"]
        .map((kind) => [kind, v.changed.filter((c) => c.change === kind).length])
        .filter(([, n]) => n)
        .map(([kind, n]) => `${n} ${kind}`)
        .join(", ");
      const who = v.run_id === run.id ? "this task" : v.run_id ? `<a href="#task/${esc(v.run_id)}">another task</a>` : "an earlier build";
      const restored = /^Restore version/.test(v.subject) ? ` · ${esc(v.subject.toLowerCase())}` : "";
      return `<li class="build-version-row${v.latest ? " latest" : ""}"><div><strong>Version ${esc(v.number)}</strong>${v.latest ? ` ${tag("latest")}` : ""}<small>${esc(when(v.at))} · by ${who}${restored}${counts ? ` · ${esc(counts)}` : ""}</small></div>
        <div class="button-row tight">${v.preview ? `<a class="button" href="${esc(v.preview)}" target="_blank" rel="noopener">Preview</a>` : ""}<a class="button" href="${zipOf(v.commit)}" download>Download</a>${v.latest ? "" : btn("Restore as a new version", `build-restore:${v.commit}:${v.number}`)}</div></li>`;
    })
    .join("");
  const history = versions.length > 1 ? `<details class="build-versions"><summary>All versions (${versions.length})</summary><ul class="plain">${versionRows}</ul><p class="muted">Every version is kept. Restoring an older one makes it the newest version; nothing is overwritten.</p></details>` : "";
  const online = (built.published || [])
    .map((p) => `<p class="build-online">${tag(p.target === "github" ? "GitHub Pages" : "Netlify")} Online at <a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.url)}</a></p>`)
    .join("");
  const actions = [
    built.preview ? `<a class="button primary" href="${esc(built.preview)}" target="_blank" rel="noopener">Preview the website ↗</a>` : "",
    built.preview && ok ? btn((built.published || []).length ? "Publish again…" : "Put it online…", "build-publish") : "",
    files.length ? `<a class="button" href="${zipOf(built.commit)}" download>Download as zip</a>` : "",
    ["COMPLETED", "FAILED"].includes(run.status) && files.length ? btn(latest ? `Make version ${latest.number + 1}…` : "Change something", "follow-up:next-version") : "",
    ["FAILED", "CANCELLED"].includes(run.status) && !ok ? btn("Try the build again", "build-again", "primary") : "",
  ].join("");
  const said = built.summary ? `<details class="build-said"><summary>What the coding agent said</summary><p>${esc(built.summary)}</p></details>` : "";
  const how = steps.length && !["RUNNING", "RECOVERING"].includes(run.status) ? `<details class="build-how"><summary>How it was built (${steps.length} steps)</summary>${buildStepRows(steps)}</details>` : "";
  card.innerHTML = `<h2>What was built</h2>${mineLine}${state}${online}${list}${said}${how}${history}<div class="button-row">${actions}</div>`;
}

Office.actions = Object.assign(Office.actions || {}, {
  "build-publish": () => {
    const built = TaskRoom.built || {};
    const tokens = built.tokens || {};
    const help = {
      netlify: "A Netlify personal access token: app.netlify.com → User settings → Applications → New access token.",
      github: "A GitHub fine-grained token for your account with Administration, Contents and Pages set to Read and write: github.com → Settings → Developer settings → Fine-grained tokens.",
    };
    formModal(
      "Put it online",
      "publish-form",
      `<p class="muted">The website goes on the public internet, at an address anyone with the link can open. Publishing again later updates the same place.</p>
       ${field("Where", "target", { type: "select", value: "netlify", choices: [["netlify", "Netlify (a netlify.app address at once)"], ["github", "GitHub Pages (a public repository in your account)"]] })}
       ${field("Name (GitHub Pages only)", "name", { required: false, attrs: 'maxlength="60" placeholder="colouring-pages"' })}
       ${(built.versions || []).length > 1 ? field("Which version", "version", { type: "select", value: "", choices: [["", `The latest (version ${built.versions.length})`], ...built.versions.slice(0, -1).reverse().map((v) => [v.commit, `Version ${v.number}`])] }) : ""}
       <div class="publish-token" data-token-for="netlify" ${tokens.netlify ? "hidden" : ""}>${field("Netlify token", "token_netlify", { type: "password", required: false, attrs: 'autocomplete="off"', hint: help.netlify })}</div>
       <div class="publish-token" data-token-for="github" hidden>${tokens.github ? "" : field("GitHub token", "token_github", { type: "password", required: false, attrs: 'autocomplete="off"', hint: help.github })}</div>
       <p class="muted">A token is sealed on this computer and sent only to that company, to publish.</p>
       <label class="check"><input type="checkbox" name="public" required> I understand this makes the website public.</label>`,
      "Publish"
    );
    const select = document.querySelector("#publish-form [name=target]");
    select?.addEventListener("change", () => {
      document.querySelectorAll("#publish-form [data-token-for]").forEach((box) => {
        box.hidden = box.dataset.tokenFor !== select.value || Boolean(tokens[select.value]);
      });
    });
  },
  "build-restore": async (arg) => {
    const [commit, number] = String(arg).split(":");
    const run = TaskRoom.run;
    if (!run || !commit) return;
    if (!window.confirm(`Bring version ${number} back as the newest version? Every version is kept; nothing is overwritten.`)) return;
    const done = await attempt(() => api(`/runs/${run.id}/build/restore`, { method: "POST", body: { commit } }));
    if (!done) return;
    toast(`Version ${done.restored} is back, as version ${done.versions}.`);
    rerender();
  },
  "build-again": async () => {
    const run = TaskRoom.run;
    if (!run) return;
    // A follow-up in the same folder: its spec is shown and nothing runs until Start building.
    const objective = `Build this again, as specified before; the last attempt did not finish. ${run.objective}`.slice(0, 8000);
    const started = await attempt(
      () => api(`/runs/${run.id}/follow-up`, { method: "POST", body: { objective } }),
      "Started. Its spec comes back for you to press Start building."
    );
    if (started?.id) go("task", started.id);
  },
  // The answer as a page of its own, at a private link to keep or pass on.
  "answer-page": async (id) => {
    const page = await attempt(() => api(`/artifacts/${id}/page`));
    if (!page?.path) return;
    const link = `${location.origin}${page.path}`;
    modal(
      "This answer as a page",
      `<p>A clean page of this answer, its charts drawn, for reading or passing on. It is read-only and runs nothing.</p>
       <div class="field"><input id="answer-page-link" readonly value="${esc(link)}" aria-label="The page's link"></div>
       <p class="muted">Anyone with this link who can reach your office can read it. It shows this version of the answer.</p>`,
      `<div class="button-row">${btn("Copy link", "answer-page-copy")}<a class="button primary" href="${esc(page.path)}" target="_blank" rel="noopener noreferrer">Open it ↗</a></div>`
    );
  },
  "answer-page-copy": async () => {
    const input = document.getElementById("answer-page-link");
    if (!input) {
      console.error("Element not found: answer-page-link");
      return;
    }
    try {
      await navigator.clipboard.writeText(input.value);
      toast("Link copied.");
    } catch {
      input.select();
      toast("Select the link and copy it.");
    }
  },
  // Point at the live page, pause and continue the agent.
  "live-point": () => {
    const frame = LiveBuild.card()?.querySelector("[data-live-view] iframe");
    if (!frame) {
      toast("There is no page to point at yet.", true);
      return;
    }
    frame.contentWindow?.postMessage({ officePick: true }, "*");
    toast("Click the thing on the page you want to talk about.");
  },
  "live-unpoint": () => {
    LiveBuild.pointed = null;
    const shown = LiveBuild.card()?.querySelector("[data-live-pointed]");
    if (shown) shown.hidden = true;
  },
  "live-pause": async () => {
    const run = TaskRoom.run;
    if (!run) return;
    const done = await attempt(
      () => api(`/runs/${run.id}/build/notes`, { method: "POST", body: { mode: "pause" } }),
      "It pauses after the step it is on."
    );
    if (done !== undefined) LiveBuild.tick();
  },
  "live-continue": async () => {
    const run = TaskRoom.run;
    if (!run) return;
    const done = await attempt(
      () => api(`/runs/${run.id}/build/notes`, { method: "POST", body: { mode: "continue" } }),
      "Continuing."
    );
    if (done !== undefined) LiveBuild.tick();
  },
  "build-stop": async () => {
    const run = TaskRoom.run;
    if (!run) return;
    if (!window.confirm("Stop the build? The coder on your Mac stops at its next report, within a few seconds, and nothing it made is kept by the office.")) return;
    if ((await attempt(() => api(`/runs/${run.id}/controls`, { method: "POST", body: { kind: "cancel" } }), "Stopping the build.")) !== undefined) rerender();
  },
  "build-file": async (encoded) => {
    const path = decodeURIComponent(encoded || "");
    const run = TaskRoom.run;
    if (!run || !path) return;
    const version = TaskRoom.built?.commit ? `&v=${encodeURIComponent(TaskRoom.built.commit)}` : "";
    const file = await attempt(() => api(`/runs/${run.id}/build/file?path=${encodeURIComponent(path)}${version}`));
    if (!file) return;
    modal(path, file.text === null ? `<p class="muted">This file is not text (${esc(file.bytes)} bytes). Download the zip to open it.</p>` : `<pre class="build-code">${esc(file.text)}</pre>${file.cut ? `<p class="muted">Shown in part; the zip has all of it.</p>` : ""}`);
  },
});

// A long entry shows a few lines and a way to read the rest.
const FOLD_AT = 360;
function foldable(text, id) {
  const value = String(text ?? "");
  if (value.length <= FOLD_AT) return `<p>${esc(value)}</p>`;
  return `<p class="folded" id="${esc(id)}">${esc(value)}</p><button type="button" class="text-btn unfold" data-action="unfold:${esc(id)}">Show all</button>`;
}

// How far a running task has come, from the office pulse.
function taskProgress(runId) {
  const running = (Office.pulse?.running || []).find((task) => task.run_id === runId);
  if (!running?.progress) return "";
  const { done, total } = running.progress;
  return `<span class="row-progress" title="${esc(`${done} of ${total} done`)}">${Island.ring(`row:${runId}`, done / Math.max(1, total))}<small>${esc(done)}/${esc(total)}</small></span>`;
}

// ---- dialogs ----

// Where the owner is when they ask for a new task: its project, and the task on
// screen if there is one. A new task starts from there, not from the top of a list.
function newTaskOrigin() {
  if (Office.view === "task" && TaskRoom.run) return { projectId: TaskRoom.run.project_id, run: TaskRoom.run };
  if (Office.view === "project") return { projectId: Projects.current?.id || Office.param || "", run: null };
  if (Office.view === "room") return { projectId: Office.param || "", run: null };
  return { projectId: "", run: null };
}

async function openNewTask() {
  const origin = newTaskOrigin();
  Tasks.origin = origin;
  const projects = await attempt(() => api("/projects"));
  if (!projects) return;
  const [options, office] = await Promise.all([api("/deliverables").catch(() => null), api("/settings").catch(() => null)]);
  Tasks.deliverables = options;
  // Offered only when the owner turned the browser on and it runs.
  Tasks.browserReady = Boolean(office?.settings?.browser_on && office?.browser_running);
  const choices = projects.filter((p) => p.status === "ACTIVE").map((p) => [p.id, p.name]);
  formModal(
    "What should your office work on?",
    "new-task-form",
    `<p>Describe the outcome. The office plans the steps, checks its own work, and asks you before anything leaves it.</p>
     ${field("Title", "title", {
       required: false,
       attrs: 'maxlength="120" placeholder="Written for you from what you ask below" autocomplete="off"',
       hint: "Leave it and a short title is written from what you ask. Type your own and it is kept.",
     })}
     ${field("Desired outcome & context", "objective", { type: "textarea", attrs: 'maxlength="8192" rows="5"' })}
     ${
       origin.run
         ? `<label class="check build-on"><input type="checkbox" id="f-build-on" name="build_on" checked> Build on this task: <strong>${esc(origin.run.title || origin.run.objective.slice(0, 80))}</strong><small class="muted"> it is told what that task has found</small></label>`
         : ""
     }
     ${
       choices.length
         ? field("Project", "project", {
             type: "select",
             value: choices.some(([id]) => id === origin.projectId) ? origin.projectId : choices[0][0],
             choices: [...choices, ["__new", "＋ New project…"]],
           })
         : ""
     }
     <div id="new-project-row" ${choices.length ? "hidden" : ""}>${field("New project name", "project_name", { required: !choices.length, attrs: 'maxlength="255"' })}</div>
     <div id="serves-row"></div>
     ${
       options
         ? field("What should it come to?", "shape", {
             type: "select",
             value: options.default.shape,
             choices: options.shapes.map((s) => [s.id, s.label]),
             hint: "Said before the work starts, so the work aims at it.",
           }) +
           field("Hand it over as", "format", {
             type: "select",
             value: options.default.format,
             choices: options.formats.map((f) => [f.id, f.label]),
           })
         : ""
     }
     ${
       Tasks.browserReady
         ? `<label class="check"><input type="checkbox" name="use_browser"> Use the office's browser for this</label>
            <p class="muted hint">For a site's own search, going through pages, or a form that shows a result. Signing in, paying or sending still asks you first.</p>`
         : ""
     }
     <label class="check"><input type="checkbox" name="full_team"> Full team: a plan, research in waves and a critique</label>
     <p class="muted hint">For large work. Without it, the office's lead answers and decides itself how much research the request needs; it can propose the full team.</p>
     <label class="field">Files or a folder for this task (optional)</label>
     ${attachRow("new-task-form")}
     <div class="note">Uses paid model calls within the default budget. External sharing always needs your approval.</div>`,
    "Start task"
  );
  suggestTitles();
  const select = $("#f-project");
  const showObjectives = async () => {
    const row = $("#serves-row");
    if (!row) return;
    row.innerHTML = "";
    const project = projects.find((p) => p.id === select?.value);
    if (!project || !project.venture_id) return;
    const open = await api(`/ventures/${project.venture_id}/objectives?open_only=true`).catch(() => []);
    if (!open.length) return;
    row.innerHTML = field("What is this for?", "serves_objective_id", {
      type: "select",
      required: false,
      choices: [["", "Nothing in particular"], ...open.map((o) => [o.id, o.title])],
      hint: "What this project's business line is working towards. Naming one lets finished work count towards it.",
    });
  };
  const shape = $("#f-shape");
  const format = $("#f-format");
  shape?.addEventListener("change", () => {
    const chosen = (Tasks.deliverables?.shapes || []).find((s) => s.id === shape.value);
    if (format && chosen?.suits && !format.dataset.chosen) format.value = chosen.suits;
  });
  format?.addEventListener("change", () => {
    if (format) format.dataset.chosen = "1";
  });
  select?.addEventListener("change", () => {
    const row = $("#new-project-row");
    const name = $("#f-project_name");
    if (row && name) {
      row.hidden = select.value !== "__new";
      name.required = select.value === "__new";
    }
    showObjectives();
  });
  showObjectives();
}

// The title follows the ask until the owner writes one of their own: a title
// typed by hand is never overwritten, and clearing it hands it back.
function suggestTitles() {
  const objective = $("#f-objective");
  const title = $("#f-title");
  if (!objective || !title) {
    console.error("New task form not found: #f-objective / #f-title");
    return;
  }
  title.dataset.auto = "true";
  let timer = null;
  const suggest = async () => {
    if (title.dataset.auto !== "true") return;
    const asked = objective.value.trim();
    if (!asked) {
      title.value = "";
      return;
    }
    const got = await api("/titles", { method: "POST", body: { objective: asked } }).catch((error) => {
      console.error("Title suggestion failed:", error);
      return null;
    });
    // Only if nothing changed meanwhile: neither the ask, nor who owns the title.
    if (got?.title && title.dataset.auto === "true" && objective.value.trim() === asked) title.value = got.title;
  };
  objective.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(suggest, 400);
  });
  objective.addEventListener("blur", suggest);
  title.addEventListener("input", () => {
    title.dataset.auto = title.value.trim() ? "false" : "true";
    if (title.dataset.auto === "true") suggest();
  });
}

async function submitNewTask(form) {
  const data = new FormData(form);
  let projectId = data.get("project");
  if (!projectId || projectId === "__new") {
    const project = await attempt(() => api("/projects", { method: "POST", body: { name: String(data.get("project_name") || "").trim() } }));
    if (!project) return;
    projectId = project.id;
  }
  const serves = String(data.get("serves_objective_id") || "");
  const shape = String(data.get("shape") || "");
  const format = String(data.get("format") || "");
  const title = String(data.get("title") || "").trim();
  const body = {
    project_id: projectId,
    objective: String(data.get("objective")).trim(),
    ...(title ? { title } : {}),
    ...(serves ? { serves_objective_id: serves } : {}),
    ...(shape ? { deliverable: { shape, format: format || "onscreen" } } : {}),
    ...(data.get("build_on") && Tasks.origin?.run ? { continues_run_id: Tasks.origin.run.id } : {}),
    ...(data.get("use_browser") ? { use_browser: true } : {}),
    ...(data.get("full_team") ? { full_team: true } : {}),
    ...(attachedIds("new-task-form").length ? { attachments: attachedIds("new-task-form") } : {}),
  };
  if (attachBusy("new-task-form")) return;
  const run = await attempt(() => api("/runs", { method: "POST", body }), body.full_team ? "Task started. The team is planning it." : "Task started.");
  if (!run) return;
  attachClear("new-task-form");
  closeModal();
  TaskRoom.tab = "workflow";
  go("task", run.id);
}

async function openApproval() {
  const pending = await attempt(() => api("/approvals/pending"));
  const match = pending?.find((row) => row.approval.run_id === TaskRoom.run?.id);
  if (!match) {
    toast("This proposal was already decided. Reloading.");
    rerender();
    return;
  }
  TaskRoom.approval = match.approval;
  const b = match.brief;
  const list = (title, items) => (items?.length ? `<h3>${esc(title)}</h3><ul class="plain">${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "");
  if (b.builds) return openBuildApproval(b);
  modal(
    "Review before deciding",
    `<p class="muted">${esc(b.objective)}</p>
     <div class="approval-preview"><h3>Recommendation</h3><p>${esc(b.recommendation)}</p></div>
     ${list("What the research found", b.research_summary)}
     ${list("What the critic raised", b.critical_review)}
     ${list("Known uncertainties", b.known_uncertainties)}
     ${list("Claims the evidence may not support", b.unverified_claims)}
     <div class="approval-preview"><h3>What approving does</h3><p>${
       b.executes_code
         ? `Runs an engineering change in <strong>${esc(b.repository)}</strong> (${esc(b.base_branch)}), limited to: ${esc((b.permitted_paths || []).join(", "))}. Tests: <code>${esc(b.test_command || "none")}</code>.${b.publishes ? " A merge request is opened for your review." : " Nothing is published."}`
         : "Records your decision. No code runs and nothing is published."
     }</p></div>
     ${field("Comment (optional; required to revise)", "decision-comment", { type: "textarea", required: false, attrs: 'maxlength="4096" rows="2"' })}
     ${field("If revising, what should change?", "decision-target", { type: "select", choices: [["research", "The research"], ["strategy", "The strategy"]] })}
     <label class="approval-check"><input type="checkbox" id="approve-check"> I have read this proposal. My approval applies to exactly this scope.</label>`,
    `<div class="button-row">${btn("Reject", "decide:reject", "danger")}${btn("Send back to revise", "decide:revise")}${btn("Approve", "decide:approve", "primary", 'id="approve-button" disabled title="Tick the box above to confirm you have read the proposal."')}</div>`
  );
  armApproveButton();
}

// A build: the spec, what happens, and Start building.
function openBuildApproval(b) {
  const agent = { claude_code: "Claude Code", codex: "Codex", opencode: "opencode", dsh: "DeepSeek Harness" }[b.coding_agent] || "Claude Code";
  modal(
    "Start building?",
    `<p class="muted">${esc(b.objective)}</p>
     <div class="approval-preview"><h3>What will be made</h3><p class="build-spec">${esc(b.proposed_engineering_scope)}</p></div>
     <div class="approval-preview"><h3>What happens when you press Start</h3><p>${esc(b.expected_output)}</p><p class="muted">Built by ${esc(agent)} on your Mac, then checked by the office. Change the CLI under Settings → Limits and models.</p></div>
     ${field("Want something different? Say what (needed for Change the plan)", "decision-comment", { type: "textarea", required: false, attrs: 'maxlength="4096" rows="2"' })}
     <input type="hidden" id="f-decision-target" value="strategy">
     <label class="approval-check"><input type="checkbox" id="approve-check"> I have read what will be made.</label>`,
    `<div class="button-row">${btn("Not now", "decide:reject", "danger")}${btn("Change the plan", "decide:revise")}${btn("Start building", "decide:approve", "primary", 'id="approve-button" disabled title="Tick the box above first."')}</div>`
  );
  armApproveButton();
}

function armApproveButton() {
  const box = $("#approve-check");
  const button = $("#approve-button");
  if (!box || !button) return;
  const sync = () => {
    button.disabled = !box.checked;
    button.title = box.checked ? "" : "Tick the box above to confirm you have read the proposal.";
  };
  box.addEventListener("change", sync);
  sync();
}

async function decide(action) {
  const approval = TaskRoom.approval;
  if (!approval) return;
  const comment = $("#f-decision-comment")?.value.trim() || null;
  if (action === "approve" && !$("#approve-check")?.checked) {
    toast("Tick the box to confirm you have read the proposal.", true);
    return;
  }
  if (action === "revise" && !comment) {
    toast("Say what should change before sending it back.", true);
    return;
  }
  const body = { scope_digest: approval.scope_digest, comment };
  if (action === "revise") body.revision_target = $("#f-decision-target")?.value || "research";
  const done = await attempt(() => api(`/approvals/${approval.id}/${action}`, { method: "POST", body }), action === "approve" ? "Approved." : action === "reject" ? "Rejected." : "Sent back for revision.");
  if (done === undefined) return;
  closeModal();
  rerender();
}

// What the Chief asked the owner in its plan, if anything.
function ownerQuestions(run) {
  const asked = run?.state?.plan?.owner_questions;
  return Array.isArray(asked) ? asked.filter((q) => typeof q === "string" && q.trim()) : [];
}

function openOwnerAnswers() {
  const run = TaskRoom.run;
  const asked = ownerQuestions(run);
  modal(
    "Answer the Chief",
    `<ol class="owner-questions">${asked.map((q) => `<li>${esc(q)}</li>`).join("")}</ol>
     ${field("Your answers", "owner-answers", { type: "textarea", attrs: 'maxlength="8192" rows="5"' })}
     <p class="muted">Your answers go to the research as your direction. Leave out anything you would rather not say; the answer will note it.</p>`,
    `<div class="button-row">${btn("Close the task", "review-act:close", "danger")}${btn("Continue with my answers", "owner-answers-send", "primary")}</div>`
  );
}

async function sendOwnerAnswers() {
  const answers = $("#f-owner-answers")?.value.trim() || "";
  if (!answers) {
    toast("Write your answers first, or close the task.", true);
    return;
  }
  const asked = ownerQuestions(TaskRoom.run);
  const comment = `The owner answered the Chief's questions.\n${asked.map((q) => `- ${q}`).join("\n")}\nAnswers:\n${answers}`;
  const done = await attempt(() => api(`/runs/${TaskRoom.run.id}/review`, { method: "POST", body: { action: "accept", comment } }), "The office is researching with your answers.");
  if (done === undefined) return;
  closeModal();
  rerender();
}

// Redo part of a finished task, in the same task. The earlier answer is kept.
function openRework() {
  const run = TaskRoom.run;
  if (!run) {
    console.error("No task on screen to rework");
    return;
  }
  const researchOnly = run.workflow_name === "agent_research";
  const parts = researchOnly
    ? [["research", "The research"]]
    : [["", "Let the office decide from my note"], ["research", "The research, and everything after it"], ["strategy", "The strategy, and everything after it"]];
  modal(
    "Rework this task",
    `<p>The same task reopens, does it again better and writes a new answer. The current answer is kept as an earlier version.</p>
     ${field("Anything in particular? (optional)", "rework-comment", { type: "textarea", required: false, attrs: 'maxlength="8192" rows="3" placeholder="Leave empty and the office improves what its review raised and the gaps the answer listed. Or say, e.g.: add the EU vendors and compare them on price."' })}
     ${field("Redo", "rework-target", { type: "select", choices: parts, value: researchOnly ? "research" : "" })}
     <p class="muted">It is new work: it spends within this task's limits and the monthly limits${run.workflow_name === "agent_research" ? "" : ", and a proposal asks for your approval again"}.</p>`,
    `<div class="button-row">${btn("Rework", "rework-send", "primary")}</div>`
  );
}

async function sendRework() {
  // Optional: with no note the office decides what to improve.
  const comment = $("#f-rework-comment")?.value.trim() || "";
  const target = $("#f-rework-target")?.value || "";
  const body = { comment, ...(target ? { revision_target: target } : {}) };
  const done = await attempt(() => api(`/runs/${TaskRoom.run.id}/rework`, { method: "POST", body }), comment ? "Reworking. The office is redoing it with your note." : "Reworking. The office is improving what its review raised.");
  if (done === undefined) return;
  closeModal();
  TaskRoom.tab = "workflow";
  rerender();
}

// Stops that come from the critic's review: the owner decides with what the
// critic said in front of them. It used to say only that the critic "kept
// raising the same concerns", without saying which.
const REVIEW_STOPS = ["revision_not_converging", "revision_limit_reached", "revision_made_no_change", "critic_blocked"];
const CONCERNS = [
  ["required_corrections", "What it asks to be corrected"],
  ["critical_issues", "Critical issues"],
  ["missing_evidence", "Evidence it found missing"],
];
const MORE_FROM_CRITIC = [
  ["weak_assumptions", "Weak assumptions"],
  ["alternative_explanations", "Other explanations"],
  ["what_would_change_conclusion", "What would change the conclusion"],
];

function reviewable(run) {
  const critique = run?.state?.last_critique || run?.state?.critique;
  const reason = String(run?.state?.stopped_reason || "").split(":")[0];
  return ["NEEDS_HUMAN_REVIEW", "BLOCKED"].includes(run?.status) && critique && REVIEW_STOPS.includes(reason);
}

function critiqueCard(run) {
  if (!reviewable(run)) return "";
  const critique = run.state.last_critique || run.state.critique;
  const seen = run.state.convergence || {};
  const repeated =
    seen.method === "typesafe" && seen.total
      ? `It raised ${seen.still_raised} of its ${seen.total} earlier concerns again, so another round on its own was unlikely to help.`
      : "";
  const list = (key, title, pickable) => {
    // Ranked issues carry their critical_issues twice; shown once, as issues.
    if (key === "critical_issues" && (critique.issues || []).length) return "";
    const items = (critique[key] || []).filter(Boolean).map(concernText);
    if (!items.length) return "";
    return `<h3>${esc(title)} <span class="muted">${items.length}</span></h3><ul class="concerns">${items
      .map((item, i) =>
        pickable
          ? `<li><label><input type="checkbox" data-concern="${esc(key)}:${i}"> <span>${esc(item)}</span></label></li>`
          : `<li><span>${esc(item)}</span></li>`
      )
      .join("")}</ul>`;
  };
  const target = critique.revision_target === "strategy" ? "strategy" : critique.revision_target === "research" ? "research" : "";
  const worked = hasWork(TaskRoom.run);
  return `<div class="card review" id="critic-review" data-critic-review>
    <div class="card-top"><h2>What the critic said</h2><span class="labels">${tag(String(critique.verdict || ""), critique.verdict === "BLOCK" ? "clay" : "amber")}</span></div>
    ${critique.for_owner ? `<p class="review-plain">${esc(critique.for_owner)}</p>` : ""}
    ${repeated ? `<p class="review-repeat">${esc(repeated)}</p>` : ""}
    <h3>How to decide</h3>
    <ol class="review-how">
      <li><strong>${worked ? "Finish with what it has" : "Accept as is"}:</strong> ${worked ? "the Chief writes the final answer from the work done so far, stating these concerns as its limits. No more research or replanning." : "nothing was researched yet, so the office researches the plan as it stands."}</li>
      <li><strong>Revise:</strong> tick the concerns that matter to you, add your own direction if you like, and the office does one more round aimed at them.</li>
      <li><strong>Stop without an answer:</strong> nothing more is spent and no answer is written; what exists stays readable.</li>
    </ol>
    ${list("issues", "What matters most", true)}
    ${CONCERNS.map(([key, title]) => list(key, title, true)).join("")}
    <details class="review-more"><summary>More from the critic</summary>
      ${MORE_FROM_CRITIC.map(([key, title]) => list(key, title, false)).join("")}
      ${critique.confidence ? `<h3>How sure it is</h3><p>${esc(critique.confidence)}</p>` : ""}
    </details>
    ${field("Your own direction (optional)", "review-note", { type: "textarea", required: false, attrs: 'maxlength="4000" rows="3"' })}
    ${field("Redo", "review-redo", { type: "select", required: false, value: target, choices: [["", "Let the office decide"], ["research", "The research"], ["strategy", "The writing"]] })}
    <div class="button-row">${btn("Stop without an answer", "review-card:close", "danger")}${worked ? btn("Finish with what it has", "review-finish") : btn("Accept as is", "review-card:accept")}${btn("Revise with what I ticked", "review-card:revise", "primary")}</div>
  </div>`;
}

// A ranked issue reads as what is wrong, why it matters, and the fix.
function concernText(item) {
  if (item && typeof item === "object") return `${item.issue} Why it matters: ${item.why_it_matters} Fix: ${item.fix}`;
  return String(item || "");
}

// Something was researched or written, so an answer can be made from it.
function hasWork(run) {
  return Boolean(run?.state?.research || run?.state?.strategy);
}

// The instruction a revision carries: the concerns the owner ticked, then their own words.
function reviewComment() {
  const critique = TaskRoom.run?.state?.last_critique || TaskRoom.run?.state?.critique || {};
  const picked = Array.from(document.querySelectorAll("[data-critic-review] [data-concern]:checked"))
    .map((box) => {
      const [key, index] = String(box.dataset.concern || "").split(":");
      const item = (critique[key] || [])[Number(index)];
      return item ? concernText(item) : item;
    })
    .filter(Boolean);
  const note = $("#f-review-note")?.value.trim() || "";
  const parts = [];
  if (picked.length) parts.push(`Fix these concerns from the critic:\n${picked.map((item) => `- ${item}`).join("\n")}`);
  if (note) parts.push(note);
  // The office takes up to 8,192 characters of instruction.
  return parts.join("\n\n").slice(0, 8000);
}

async function reviewFromCard(action) {
  const comment = reviewComment();
  if (action === "revise" && !comment) {
    toast("Tick the concerns to fix, or say what should change.", true);
    return;
  }
  const body = { action, comment };
  const target = $("#f-review-redo")?.value;
  if (action === "revise" && target) body.revision_target = target;
  const done = await attempt(
    () => api(`/runs/${TaskRoom.run.id}/review`, { method: "POST", body }),
    action === "close" ? "Stopped without an answer." : action === "accept" ? "Accepted. The Chief is writing the final answer." : "The office is revising with your direction."
  );
  if (done === undefined) return;
  rerender();
}

function openReview() {
  const run = TaskRoom.run;
  modal(
    "What should happen next?",
    `<p>${esc(stopText(run?.state?.stopped_reason || run?.error) || "The office stopped and needs your decision.")}</p>
     ${field("Your instruction (needed to revise)", "review-comment", { type: "textarea", required: false, attrs: 'maxlength="8192" rows="3"' })}
     <p class="muted">${hasWork(run) ? "Finishing writes the answer from the work done so far, with no more research or replanning; it does not authorise any engineering." : "Accepting moves the work on to the summary and your approval; it does not authorise any engineering."}</p>
     ${field("Redo", "review-target", { type: "select", choices: [["", "Let the office decide from my note"], ["research", "The research"], ["strategy", "The strategy"]] })}`,
    `<div class="button-row">${btn("Stop without an answer", "review-act:close", "danger")}${hasWork(run) ? btn("Finish with what it has", "review-finish") : btn("Accept as is", "review-act:accept")}${btn("Revise", "review-act:revise", "primary")}</div>`
  );
}

async function reviewAct(action) {
  const comment = $("#f-review-comment")?.value.trim() || "";
  if (action === "revise" && !comment) {
    toast("Say what should change.", true);
    return;
  }
  const body = { action, comment };
  const target = $("#f-review-target")?.value;
  if (action === "revise" && target) body.revision_target = target;
  const done = await attempt(() => api(`/runs/${TaskRoom.run.id}/review`, { method: "POST", body }), action === "close" ? "Stopped without an answer." : "The office is working on it again.");
  if (done === undefined) return;
  closeModal();
  rerender();
}

// What a finished task can be taken away as. Markdown and HTML need nothing of
// the office; Word and Excel are written from the same document.
const DOWNLOADS = [
  ["docx", "Word"],
  ["xlsx", "Excel"],
  ["pptx", "Slides"],
  ["html", "Web page"],
  ["md", "Markdown"],
];

function downloadRow(id) {
  const asked = (TaskRoom.run?.deliverable || {}).format || "";
  const ordered = [...DOWNLOADS].sort(([a], [b]) => (b === asked) - (a === asked));
  return ordered
    .map(
      ([format, label]) =>
        `<a class="button ${format === asked ? "primary" : ""}" href="/artifacts/${esc(id)}/download?format=${esc(format)}" download>${esc(label)}</a>`
    )
    .join("");
}

async function showArtifact(id) {
  // The office renders the document: the stored payload is structured data,
  // and showing it raw is what made a finished task look like a JSON dump.
  const [artifact, document_] = await Promise.all([
    attempt(() => api(`/artifacts/${id}`)),
    attempt(() => api(`/artifacts/${id}/document`)),
  ]);
  if (!artifact || !document_) return;
  modal(
    document_.title || ARTIFACT_LABEL[artifact.type] || artifact.type,
    `<p class="muted">${esc(document_.subtitle || ARTIFACT_LABEL[artifact.type] || artifact.type)} · v${artifact.version} · by ${esc(artifact.created_by)} · ${esc(when(artifact.created_at))}</p>
     <div class="artifact-body document">${document_.html}</div>`,
    `<div class="button-row">${downloadRow(id)}${btn("Close", "close", "primary")}</div>`
  );
}

async function control(kind, text, target) {
  const run = TaskRoom.run;
  if (!run) return undefined;
  if (kind === "cancel" && !window.confirm("Cancel this task? Work done so far is kept, but it will not continue.")) return undefined;
  const body = { kind };
  if (text) body.text = text;
  if (target) body.target_node = target;
  const messages = {
    pause: "Pausing at the next step boundary.",
    cancel: "Task cancelled.",
    instruction: target
      ? `Direction sent to the ${ROLE_LABEL[NODE_ROLE[target]] || target}. It waits until that agent works again.`
      : "Direction sent. The next step to start picks it up.",
  };
  return attempt(() => api(`/runs/${run.id}/controls`, { method: "POST", body }), messages[kind]);
}

Office.screens.tasks = { title: "Library", render: renderTasks, refresh: 15000 };
// A finished task cannot change, so the room stops redrawing itself. It used to
// poll every four seconds and close the working the reader had just opened.
const SETTLED = ["COMPLETED", "FAILED", "CANCELLED", "REJECTED", "CLOSED"];
Office.screens.task = {
  title: "Task",
  render: (content, param) => renderRoom(content, param),
  refresh: 4000,
  // A task waiting on the owner changes only when they act; redrawing it
  // under them would lose what they ticked or typed.
  // A build being watched redraws only its own live card.
  quiet: () => SETTLED.includes(TaskRoom.run?.status) || reviewable(TaskRoom.run) || LiveBuild.active,
  nav: "tasks",
};

function limitsForm(run, budget) {
  return (
    field("Tokens for this task", "max_tokens", {
      type: "number",
      required: false,
      value: budget.max_tokens ?? "",
      attrs: 'min="1" step="1"',
      hint: "The amount of text the team may read and write. Empty leaves it as it is.",
    }) +
    field("Cost for this task (USD)", "max_cost_usd", {
      type: "number",
      required: false,
      value: budget.max_cost_usd ?? "",
      attrs: 'min="0.01" step="0.01"',
      hint: "Empty means no money limit of its own; the office's monthly limit still applies.",
    }) +
    field("Minutes of work", "max_minutes", {
      type: "number",
      required: false,
      value: budget.max_minutes ?? "",
      attrs: 'min="1" step="1"',
      hint: "Counted while agents are working, not while the task waits for you.",
    }) +
    `<p class="muted">A limit below what the task has already used is refused: it would stop again at once.</p>`
  );
}

Office.actions = Object.assign(Office.actions || {}, {
  "step-skip": async (stepId) => {
    const run = TaskRoom.run;
    if (!run) return;
    if (!window.confirm("Skip this step? If it is running it stops at its next move with what it has; otherwise it is left out. The answer says what was not covered.")) return;
    if ((await attempt(() => api(`/runs/${run.id}/controls`, { method: "POST", body: { kind: "skip", text: stepId } }), "Skipping it.")) !== undefined) rerender();
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "browser-shot": (name) => {
    const run = TaskRoom.run;
    if (!run || !/^\d{1,4}\.jpg$/.test(String(name))) return;
    modal("The office browser", `<img class="browser-full" src="/runs/${esc(run.id)}/browser/${esc(name)}" alt="A step of the office browser">`);
  },
  "context-open": (index) => {
    const part = TaskRoom.context?.parts?.[Number(index)];
    if (!part) return;
    modal(
      part.name,
      `<p class="muted">${esc(part.note)}</p>` +
        part.items.map((item) => `<div class="drawer-message"><p>${esc(item)}</p></div>`).join("")
    );
  },
  learned: async () => {
    const run = TaskRoom.run;
    if (!run) return;
    const preview = await attempt(() => api(`/runs/${run.id}/entities/extract?dry_run=true`, { method: "POST" }));
    if (preview === undefined) return;
    modal(
      "What this task learned",
      `<p class="muted">Taken from what the task itself said about the subjects you named. Nothing is kept until you keep it, and a dry run sends nothing.</p>` +
        (preview.would_send || [])
          .map((w) => `<div class="detail-line"><span>${esc(w.fact)}</span><strong>${esc(w.entity)}</strong></div>`)
          .join(""),
      `<div class="button-row">${btn("Cancel", "close")}${btn(`Propose these ${preview.calls}`, `learned-store:${run.id}`, "primary")}</div>`
    );
  },
  "learned-store": async (runId) => {
    const done = await attempt(
      () => api(`/runs/${runId}/entities/extract`, { method: "POST" }),
      "Proposed. Review them under Projects → Subjects."
    );
    if (done !== undefined) closeModal();
  },
  "limits-edit": async () => {
    const run = TaskRoom.run;
    if (!run) return;
    const usage = await attempt(() => api(`/runs/${run.id}/usage`), null);
    if (usage === undefined) return;
    formModal("Limits for this task", "limits-form", limitsForm(run, usage.budget || {}), "Save limits");
  },
  "policy-answer": async (arg) => {
    const [recordId, answer] = arg.split(":");
    const allow = answer === "yes";
    const done = await attempt(
      () => api(`/policies/records/${recordId}/decide`, { method: "POST", body: { allow } }),
      allow ? "Allowed for this task; it continues." : "Refused. The task stays paused."
    );
    if (done !== undefined) rerender();
  },
  "new-task": openNewTask,
  "follow-up": (kind) => {
    const run = TaskRoom.run;
    if (!run) return;
    const options = Tasks.deliverables;
    // From a build: the next version, in the same project folder.
    const next = kind === "next-version" || isBuild(run);
    const number = ((TaskRoom.built?.versions || []).length || 0) + 1;
    formModal(
      next ? `Make version ${number}` : "Ask a follow-up",
      "follow-up-form",
      `<p class="muted">${
        next
          ? `A new task that changes this build into version ${number}, in the same project folder. You will see what it will do and press Start building; the earlier versions stay as they are.`
          : "A task of its own, told what this one concluded — its answer, what it rested on and what it left unknown — and free to contradict it. It plans, it costs money, and it can be refused by a limit like any other task."
      }</p>
       ${next ? `<input type="hidden" name="next_version" value="${number}">` : ""}
       ${field(next ? "What should change?" : "What do you want to know now?", "objective", { type: "textarea", attrs: 'maxlength="8192" rows="5"', hint: next ? "For example: add a dinosaur theme, and a button to print two pictures on one page." : "You need not repeat the earlier question or its findings." })}
       ${
         options && !next
           ? field("What should it come to?", "shape", {
               type: "select",
               value: (run.deliverable || {}).shape || options.default.shape,
               choices: options.shapes.map((d) => [d.id, d.label]),
             }) +
             field("Hand it over as", "format", {
               type: "select",
               value: (run.deliverable || {}).format || options.default.format,
               choices: options.formats.map((f) => [f.id, f.label]),
             })
           : ""
       }`,
      next ? "Plan the next version" : "Start the follow-up"
    );
  },
  open: (id) => {
    TaskRoom.tab = "workflow";
    go("task", id);
  },
  filter: (id) => {
    TaskRoom.filter = id;
    rerender();
  },
  tab: (id) => {
    TaskRoom.tab = id;
    rerender();
  },
  approval: openApproval,
  decide,
  review: openReview,
  "review-act": reviewAct,
  "review-card": reviewFromCard,
  "review-finish": async () => {
    const run = TaskRoom.run;
    if (!run) return;
    const done = await attempt(
      () => api(`/runs/${run.id}/finish`, { method: "POST" }),
      "Finishing. The Chief is writing the answer from the work done so far."
    );
    if (done === undefined) return;
    closeModal();
    rerender();
  },
  "review-jump": () => document.querySelector("[data-critic-review]")?.scrollIntoView({ behavior: "smooth", block: "start" }),
  "answer-owner": openOwnerAnswers,
  rework: openRework,
  unfold: (id) => {
    const text = document.getElementById(id);
    if (!text) {
      console.error("Nothing to unfold:", id);
      return;
    }
    text.classList.remove("folded");
    text.nextElementSibling?.classList.contains("unfold") && text.nextElementSibling.remove();
  },
  "rework-send": sendRework,
  "owner-answers-send": sendOwnerAnswers,
  "direction-edit": (id) => {
    const run = TaskRoom.run;
    if (!run) return;
    TaskRoom.editing = id;
    const current = TaskRoom.controls?.find((c) => c.id === id);
    formModal(
      "Reword your direction",
      "direction-edit-form",
      field("Direction", "text", { type: "textarea", value: current?.text || "", attrs: 'rows="3" maxlength="4000"' }),
      "Save"
    );
  },
  "direction-withdraw": async (id) => {
    const run = TaskRoom.run;
    if (!run) return;
    const known = { already_received: "An agent already has this note, so it cannot be taken back." };
    try {
      await api(`/runs/${run.id}/controls/${id}`, { method: "DELETE" });
      toast("Taken back; no agent saw it.");
      rerender();
    } catch (error) {
      if (!(error instanceof SignedOut)) toast(known[error.message] || explain(error), true);
    }
  },
  artifact: showArtifact,
  pause: async () => (await control("pause")) !== undefined && rerender(),
  cancel: async () => (await control("cancel")) !== undefined && rerender(),
  resume: async () => (await attempt(() => api(`/runs/${TaskRoom.run.id}/resume`, { method: "POST" }), "Resumed.")) !== undefined && rerender(),
  "team-run": () => {
    const proposal = TaskRoom.run?.team_proposal;
    if (!proposal?.objective) return;
    modal(
      "Start the full team run?",
      `<p>The full team plans, researches, critiques and answers this, building on what this task found. It is a new task with its own limits; a build waits for your Start building.</p><p class="idea-quote">${esc(roomShort(proposal.objective, 300))}</p>`,
      `<div class="button-row">${btn("Not now", "close")}${btn("Start the team run", "team-run-go", "primary")}</div>`
    );
  },
  "team-run-go": async () => {
    const runId = TaskRoom.run?.id;
    if (!runId) return;
    const team = await attempt(() => api(`/runs/${runId}/team-run`, { method: "POST" }), "Started with the full team.");
    closeModal();
    if (team?.id) location.hash = `#task/${team.id}`;
    else rerender();
  },
  template: async () => {
    const name = window.prompt("Name this template", TaskRoom.run?.objective.slice(0, 80) || "");
    if (!name) return;
    await attempt(() => api(`/runs/${TaskRoom.run.id}/template`, { method: "POST", body: { name } }), "Saved as a template.");
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "publish-form": async (form) => {
    const run = TaskRoom.run;
    if (!run) return;
    const data = new FormData(form);
    const target = String(data.get("target") || "netlify");
    if (!data.get("public")) return toast("Tick that it will be public first.", true);
    const body = { target, public: true };
    const name = String(data.get("name") || "").trim();
    if (target === "github" && name) body.name = name;
    const version = String(data.get("version") || "");
    if (version) body.version = version;
    const token = String(data.get(`token_${target}`) || "").trim();
    if (token) body.token = token;
    const done = await attempt(() => api(`/runs/${run.id}/publish`, { method: "POST", body }));
    if (!done) return;
    closeModal();
    toast(`Online at ${done.url}${target === "github" ? " (GitHub may take a minute to show it)" : ""}.`);
    rerender();
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  // The owner's word to the agent while it builds.
  "live-note-form": async (form) => {
    const run = TaskRoom.run;
    if (!run) return;
    const data = new FormData(form);
    const text = String(data.get("text") || "").trim();
    if (!text) {
      toast("Say what to change first.", true);
      return;
    }
    const mode = String(data.get("mode") || "next");
    const sent = await attempt(
      () =>
        api(`/runs/${run.id}/build/notes`, {
          method: "POST",
          body: { text, mode, ...(LiveBuild.pointed ? { pointed: LiveBuild.pointed } : {}) },
        }),
      mode === "now" ? "Sent. It stops what it is doing and takes it now." : "Sent. It takes it at its next step."
    );
    if (sent === undefined) return;
    form.reset();
    Office.actions["live-unpoint"]();
    LiveBuild.tick();
  },
  "follow-up-form": async (form) => {
    const run = TaskRoom.run;
    if (!run) return;
    const data = new FormData(form);
    const shape = String(data.get("shape") || "");
    const started = await attempt(
      () =>
        api(`/runs/${run.id}/follow-up`, {
          method: "POST",
          body: {
            // A next version is a build that changes this one.
            objective: data.get("next_version")
              ? `Build version ${data.get("next_version")} of this, changing it: ${String(data.get("objective")).trim()}`
              : String(data.get("objective")).trim(),
            ...(data.get("next_version")
              ? { deliverable: { shape: "software", format: "onscreen" } }
              : shape
                ? { deliverable: { shape, format: String(data.get("format") || "onscreen") } }
                : {}),
          },
        }),
      "Started. It carries what this task found."
    );
    if (started?.id) location.hash = `task/${started.id}`;
  },
  "limits-form": async (form) => {
    const run = TaskRoom.run;
    if (!run) return;
    const data = new FormData(form);
    const body = {};
    for (const name of ["max_tokens", "max_cost_usd", "max_minutes"]) {
      const text = String(data.get(name) || "").trim();
      if (text === "") continue;
      body[name] = name === "max_cost_usd" ? Number(text) : Math.round(Number(text));
    }
    if (!Object.keys(body).length) {
      toast("Give at least one limit to change.", true);
      return;
    }
    if ((await attempt(() => api(`/runs/${run.id}/budget`, { method: "POST", body }), "Limits saved.")) === undefined) return;
    closeModal();
    rerender();
  },
  "new-task-form": submitNewTask,
  "direction-edit-form": async (form) => {
    const run = TaskRoom.run;
    const text = String(new FormData(form).get("text") || "").trim();
    if (!run || !text || !TaskRoom.editing) return;
    const known = { already_received: "An agent already has this note, so it cannot be changed." };
    try {
      await api(`/runs/${run.id}/controls/${TaskRoom.editing}`, { method: "PUT", body: { text } });
      closeModal();
      toast("Reworded.");
      rerender();
    } catch (error) {
      if (!(error instanceof SignedOut)) toast(known[error.message] || explain(error), true);
    }
  },
  "direction-form": async (form) => {
    // Sent, the bar says whether it was read; the box empties at once.
    const data = new FormData(form);
    const text = String(data.get("text") || "").trim();
    if (!text) return;
    const target = String(data.get("target_node") || "");
    if ((await control("instruction", text, target || null)) !== undefined) rerender();
  },
});

// Renaming a task or a discussion: the owner found a task's title
// could not be changed at all, least of all on a phone. One dialog for both,
// with a field a phone does not zoom into; a blank task name is made from the
// ask again.
function titleRenameDialog(kind, id) {
  const current = kind === "task" ? TaskRoom.run?.title || "" : RoomState?.topic?.title || "";
  formModal(
    kind === "task" ? "Rename this task" : "Rename this discussion",
    "title-rename-form",
    `<input type="hidden" name="kind" value="${esc(kind)}"><input type="hidden" name="target_id" value="${esc(id)}">
     ${field("Name", "rename-title", { value: current, required: kind !== "task", attrs: `maxlength="${kind === "task" ? 120 : 200}" autocomplete="off" enterkeyhint="done" class="rename-field"`, hint: kind === "task" ? "Leave it empty to name it from what was asked again." : "" })}`,
    "Save"
  );
  const input = $("#f-rename-title");
  input?.focus();
  input?.select();
}

Office.forms = Object.assign(Office.forms || {}, {
  "title-rename-form": async (form) => {
    const data = new FormData(form);
    const kind = String(data.get("kind") || "");
    // Not named "id": a field by that name hides the form's own id.
    const id = String(data.get("target_id") || "");
    const title = String(data.get("rename-title") || "").trim();
    if (kind !== "task" && !title) return toast("Give it a name.", true);
    const path = kind === "task" ? `/runs/${id}/title` : `/topics/${id}`;
    if ((await attempt(() => api(path, { method: "PUT", body: { title } }), "Renamed.")) !== undefined) {
      closeModal();
      rerender();
    }
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "title-rename": (arg) => {
    const [kind, id] = String(arg).split(":");
    titleRenameDialog(kind, id);
  },
});

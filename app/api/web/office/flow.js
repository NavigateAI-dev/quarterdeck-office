"use strict";

// The workflow of one task, drawn live: the Chief plans and decides, Jev's
// quick judgments route the work, helpers run the steps side by side, the
// critic reviews, and the Chief writes the answer. Drawn from GET /runs/{id}/flow,
// which reads only what the task recorded, and redrawn with the task page.

const FLOW_EFFORT = { light: 1, standard: 2, deep: 4 };
const FLOW_STATUS = {
  running: ["●", "working"],
  done: ["✓", "done"],
  empty: ["○", "found nothing"],
  failed: ["✕", "failed"],
  pending: ["·", "waiting"],
};
const FLOW_ACTOR = {
  chief: "chief",
  researcher: "researcher",
  writer: "writer",
  critic: "critic",
  advisor: "advisor",
  jev: "jev",
  owner: "you",
  engineer: "engineer",
  office: "office",
};

function flowModel(name) {
  // "openai/gpt-5.6-sol" reads as "gpt-5.6-sol"; a connection alias says so.
  const text = String(name || "");
  if (!text) return "not run yet";
  if (text.startsWith("connection:")) return "a connection";
  return text.split("/").pop();
}

function flowEffort(effort) {
  const filled = FLOW_EFFORT[effort] || 0;
  if (!filled) return "";
  return `<span class="wf-effort" title="${esc(effort)} effort">${"▮".repeat(filled)}${"▯".repeat(4 - filled)} ${esc(effort)}</span>`;
}

function flowLane(kind, title, lines, active) {
  return `<div class="wf-lane wf-${kind}${active ? " is-active" : ""}">
    <div class="wf-lane-title">${active ? '<span class="wf-live" aria-hidden="true"></span>' : ""}${title}</div>
    ${lines.filter(Boolean).map((line) => `<div class="wf-line">${line}</div>`).join("")}
  </div>`;
}

function flowLink(label, live) {
  return `<div class="wf-link${live ? " is-live" : ""}" aria-hidden="true"><span></span>${label ? `<em>${esc(label)}</em>` : ""}</div>`;
}

function flowJudgment(row) {
  const sure = typeof row.confidence === "number" ? row.confidence : null;
  const bar = sure === null
    ? `<span class="wf-bar is-empty"></span>`
    : `<span class="wf-bar ${esc(row.grade || "")}"><i style="width:${Math.round(Math.max(0, Math.min(1, sure)) * 100)}%"></i></span>`;
  const verdict = sure === null ? "" : `<b class="${esc(row.grade || "")}">${sure.toFixed(2)} ${esc(row.grade || "")}</b>`;
  return `<div class="wf-judged${row.by === "rule" ? " by-rule" : ""}"><span class="wf-judged-label">${esc(row.label)}</span>${bar}<span class="wf-judged-answer">${esc(row.answer)}${verdict}</span></div>`;
}

function flowStep(card) {
  const [glyph, word] = FLOW_STATUS[card.status] || FLOW_STATUS.pending;
  const role = card.role === "writer" ? "Writer" : "Researcher";
  return `<div class="wf-step is-${esc(card.status)}" data-flow-step="${esc(card.id)}">
    <div class="wf-step-top"><strong>${esc(role)}</strong><span class="wf-step-state">${glyph} ${esc(word)}</span></div>
    <div class="wf-step-model">${esc(flowModel(card.model))}</div>
    ${flowEffort(card.effort)}
    ${card.brief ? `<p class="wf-step-brief" title="${esc(card.brief)}">${esc(card.brief)}</p>` : ""}
    ${card.skill ? `<span class="wf-skill">skill · ${esc(card.skill)}</span>` : ""}
  </div>`;
}

const FLOW_VERDICT = {
  continue: ["✓", "continue"],
  change_course: ["!", "change course"],
  skipped: ["–", "skipped · budget"],
  unavailable: ["–", "unavailable"],
};

// The advisor on call: beside the map, as in the reference. It reads the
// whole task at three moments and never writes the work.
function flowAdvisor(lane) {
  if (!lane || !lane.on) {
    return `<div class="wf-advisor is-off" data-advisor-lane>
      <div class="wf-lane-title">Advisor <span class="wf-sub">off</span></div>
      <p class="wf-quiet">Not on call for this task. Turn it on under Settings → Models, or for this project on its page.</p>
    </div>`;
  }
  const moments = (lane.moments || [])
    .map((m) => {
      const [glyph, word] = FLOW_VERDICT[m.verdict] || ["◇", "not reached"];
      return `<div class="wf-moment${m.verdict ? " is-reached" : ""} verdict-${esc(m.verdict || "none")}"><span class="wf-moment-mark">${glyph}</span><span>${esc(m.label)}</span><em>${esc(word)}</em></div>`;
    })
    .join("");
  const latest = lane.latest || null;
  const said = latest ? [...(latest.advice || []), ...(latest.watch_for || []).map((w) => `watch for: ${w}`)] : [];
  const tokens = Number(lane.tokens || 0);
  return `<div class="wf-advisor" data-advisor-lane>
    <div class="wf-lane-title">Advisor <span class="wf-sub">on call</span></div>
    <div class="wf-model">${esc(flowModel(lane.model))}</div>
    <div class="wf-moments">${moments}</div>
    ${said.length ? `<div class="wf-advice"><div class="muted-mono">last advice</div>${said.slice(0, 4).map((line) => `<div class="wf-advice-line">» ${esc(line)}</div>`).join("")}</div>` : ""}
    <div class="wf-advisor-numbers"><span>calls <b>${esc(lane.calls || 0)}</b></span><span>tokens read <b>${tokens >= 1000 ? `${Math.round(tokens / 1000)}k` : esc(tokens)}</b></span></div>
    <p class="wf-quiet">Silent on every routine turn. Never writes the work: the Chief applies the advice.</p>
  </div>`;
}

// The task's room (T): what each agent posted while it worked, newest last.
const ROOM_KIND = {
  plan: "plan",
  claim: "taking",
  finding: "found",
  doubt: "doubt",
  draft: "draft",
  challenge: "challenge",
  need: "needs",
  offer: "offers",
  advice: "advice",
  note: "note",
  owner: "you",
};

// Stages of the room the owner folded away, kept across the live refresh.
const ROOM_FOLDED = new Set();
document.addEventListener(
  "toggle",
  (event) => {
    const stage = event.target?.closest?.("[data-room-stage]");
    if (!stage || event.target !== stage) return;
    const label = stage.dataset.roomStage || "";
    if (stage.open) ROOM_FOLDED.delete(label);
    else ROOM_FOLDED.add(label);
  },
  true,
);

function roomPost(p) {
  const who = `${FLOW_ACTOR[p.author_role] || p.author_role}${p.step_id ? ` · ${p.step_id}` : ""}`;
  const towards = p.to_step ? ` → ${p.to_step}` : p.to_role ? ` → ${p.to_role}` : "";
  const replies = (p.replies || []).map(roomPost).join("");
  return `<div class="wf-thread"><div class="wf-post actor-${esc(p.author_role)} kind-${esc(p.kind)}"><span class="wf-post-who">${esc(who)}</span><span class="wf-post-kind">${esc(ROOM_KIND[p.kind] || p.kind)}${esc(towards)}</span><span class="wf-post-text">${esc(p.text)}</span></div>${replies ? `<div class="wf-replies">${replies}</div>` : ""}</div>`;
}

// The room by stage (planning, each wave, the huddle after it, review, the
// answer), each post with the replies it drew beneath it.
function roomStages(sections) {
  return sections
    .map((s) => {
      const label = s.label || "Room";
      const open = ROOM_FOLDED.has(label) ? "" : " open";
      return `<details class="wf-stage phase-${esc(s.phase || "room")}" data-room-stage="${esc(label)}"${open}><summary>${esc(label)} <span class="muted-mono">· ${esc(s.count)} post${s.count === 1 ? "" : "s"}</span></summary>${(s.posts || []).map(roomPost).join("")}</details>`;
    })
    .join("");
}

function flowRoom(room, flow) {
  if (!room) return "";
  const posts = room.posts || [];
  const sections = Array.isArray(room.sections) ? room.sections : [];
  const team = room.team
    ? `team mode on${room.huddles ? ` · ${room.huddles} huddle${room.huddles === 1 ? "" : "s"}` : ""}`
    : "team mode off: agents post here as they work, but do not read it or huddle";
  const rows = sections.length ? roomStages(sections) : posts.map(roomPost).join("");
  return `<div class="wf-room" data-team-room><div class="wf-log-title">Team room <span class="muted-mono">· ${esc(room.count || 0)} post${room.count === 1 ? "" : "s"} · ${esc(team)}</span></div>
    ${rows || `<p class="wf-quiet">Nothing posted yet.</p>`}${roomTalk(room.talk)}${roomSay(flow)}</div>`;
}

// What the owner asked about this task, and the answers. The same
// messages are in the project's room, named with this task.
function roomTalk(talk) {
  const said = Array.isArray(talk) ? talk : [];
  if (!said.length) return "";
  const rows = said
    .map((m) => {
      const author = m.author || {};
      const asking = author.kind === "owner";
      const role = author.kind === "agent" ? ` · ${ROLE_LABEL[author.role] || author.role || ""}` : "";
      return `<div class="wf-post wf-talk ${asking ? "is-question" : "is-answer"} actor-${esc(author.role || author.kind || "")}"><span class="wf-post-who">${esc(author.name || "")}${esc(role)}</span><span class="wf-post-kind">${asking ? "asked" : "answered"}</span><span class="wf-post-text">${esc(m.text)}</span></div>`;
    })
    .join("");
  return `<details class="wf-stage phase-talk" data-room-stage="Your questions" open><summary>Your questions <span class="muted-mono">· ${esc(said.length)}</span></summary><div class="wf-talk-list" data-room-talk>${rows}</div></details>`;
}

// One box for both: ask the agents about the task, at any time; or, while it
// works, a word to the team at its next huddle or to one step.
function roomSay(flow) {
  const canAsk = Boolean(TaskRoom.run?.project_id);
  const steps = flow?.active ? (flow?.steps || []).filter((s) => s.id && s.brief !== "") : [];
  const choices = [
    ...(canAsk ? [["ask", "Ask the agents"]] : []),
    ...(flow?.active ? [["team", "Tell the team at work"]] : []),
    ...steps.map((s) => [`step:${s.id}`, `Tell ${s.id}`]),
  ];
  if (!choices.length) return "";
  return `<form class="wf-say" id="room-post-form">
      <select name="to" aria-label="Who this is for">${choices.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("")}</select>
      <input name="text" maxlength="1000" required placeholder="${canAsk ? "Ask about this task — @name an agent, or the Chief answers" : "Say something to the team…"}" aria-label="Your message in the team room">
      <button class="primary">Send</button>
    </form>
    <p class="wf-quiet" id="room-say-status" role="status">${canAsk ? "A question is answered from this task's discussion and its answer, and shows in the project's room too. " : ""}${flow?.active ? "Telling the team reaches its next huddle; telling a step is acted on as a challenge." : ""}</p>`;
}

// Replies are written after the question is stored: look again until they are in.
async function awaitReplies(runId, questionId, expected) {
  for (let tries = 0; tries < 30; tries++) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    if (TaskRoom.run?.id !== runId || TaskRoom.tab !== "room") return;
    const flow = await api(`/runs/${runId}/flow`).catch(() => null);
    const answered = (flow?.room?.talk || []).filter((m) => m.reply_to === questionId).length;
    if (answered >= expected) {
      rerender();
      return;
    }
  }
}

Office.forms = Object.assign(Office.forms || {}, {
  "room-post-form": async (form) => {
    const runId = TaskRoom.run?.id;
    if (!runId) return;
    const data = new FormData(form);
    const text = String(data.get("text") || "").trim();
    if (!text) return;
    const to = String(data.get("to") || "ask");
    if (to === "ask") {
      const projectId = TaskRoom.run?.project_id;
      if (!projectId) return;
      const sent = await attempt(() => api(`/projects/${projectId}/room`, { method: "POST", body: { text, run_id: runId } }));
      if (sent === undefined) return;
      form.reset();
      const names = (sent.replying || []).map((r) => r.name).filter(Boolean);
      rerender();
      const status = document.getElementById("room-say-status");
      if (status && names.length) status.textContent = `${names.join(", ")} ${names.length === 1 ? "is" : "are"} replying…`;
      awaitReplies(runId, sent.id, Math.max(1, names.length));
      return;
    }
    const toStep = to.startsWith("step:") ? to.slice(5) : "";
    const posted = await attempt(
      () => api(`/runs/${runId}/room`, { method: "POST", body: { text, to_step: toStep || null } }),
      toStep ? `Posted to ${toStep}.` : "Posted to the team."
    );
    if (posted === undefined) return;
    form.reset();
    rerender();
  },
});

function flowLevels(steps) {
  const levels = new Map();
  (steps || []).forEach((card) => {
    const row = levels.get(card.level) || [];
    row.push(card);
    levels.set(card.level, row);
  });
  return [...levels.entries()].sort(([a], [b]) => a - b);
}

// The room has its own tab; the map only says it is there.
function flowRoomPointer(room) {
  const count = room?.count || 0;
  return `<p class="wf-quiet wf-room-pointer">Team room · ${esc(count)} post${count === 1 ? "" : "s"} — <button type="button" class="wf-room-open" data-action="tab:room">read how the agents talked</button></p>`;
}

// The task's room on its own tab (T): every agent's post, by stage, threaded.
function renderTeamRoom(flow) {
  if (!flow) return emptyCard("The team room is not available", "The office could not read this task's room. Try again in a moment.");
  return `<section class="wf${flow.active ? " is-running" : ""}" data-room-tab aria-label="How the agents talked">${flowRoom(flow.room, flow)}</section>`;
}

function renderFlow(flow) {
  if (!flow) return emptyCard("The workflow is not available", "The office could not read this task's workflow. Try again in a moment.");
  const phase = flow.phase;
  const chief = flow.chief || {};
  const critic = flow.critic || {};
  const answer = flow.answer || {};
  const line = flow.status_line || {};
  const judged = (flow.jev?.judgments || []);
  const decisions = (flow.decisions || []).slice(-3);
  const levels = flowLevels(flow.steps);
  const efforts = Object.entries(line.efforts || {}).map(([name, count]) => `${name} ${count}`).join(" · ") || "—";

  const chiefLane = flowLane(
    "chief",
    `Chief <span class="wf-sub">plans and decides</span>`,
    [
      `<span class="wf-model">${esc(flowModel(chief.model))}</span>`,
      chief.doing ? `<span class="wf-now">${esc(chief.doing)}…</span>` : "",
      `<span class="muted-mono">replans ${esc(chief.replans ?? 0)}/${esc(chief.max_replans ?? 2)} · ${esc(chief.calls || 0)} calls</span>`,
    ],
    phase === "chief"
  );
  const jevLane = `<div class="wf-lane wf-jev">
    <div class="wf-lane-title">Jev <span class="wf-sub">routing layer</span><span class="wf-count">${esc(judged.length)} judged</span></div>
    ${judged.length ? judged.map(flowJudgment).join("") : `<p class="wf-quiet">No judgments on this task yet.</p>`}
    ${decisions.length ? decisions.map(flowJudgment).join("") : ""}
    <div class="wf-key"><span class="sharp">sharp → acted on</span><span class="split">split → held back</span><span class="rule">rule → decided in code</span></div>
  </div>`;
  const workers = levels.length
    ? levels
        .map(
          ([level, cards]) =>
            `<div class="wf-level"><div class="wf-level-label">${level === 1 ? "side by side" : `then · level ${esc(level)}`}</div><div class="wf-steps">${cards.map(flowStep).join("")}</div></div>`
        )
        .join("")
    : `<p class="wf-quiet">No steps yet: the Chief is still planning.</p>`;
  const criticLane = flowLane(
    "critic",
    `Critic <span class="wf-sub">reviews the work</span>`,
    [
      `<span class="wf-model">${esc(flowModel(critic.model))}</span>`,
      critic.active ? `<span class="wf-now">reviewing…</span>` : critic.verdict ? `<span>verdict · <b>${esc(critic.verdict)}</b></span>` : "",
      `<span class="muted-mono">${esc(critic.rounds || 0)} review${critic.rounds === 1 ? "" : "s"}</span>`,
    ],
    phase === "critic"
  );
  const answerLane = flowLane(
    "answer",
    `Back to the Chief <span class="wf-sub">review and write the answer</span>`,
    [answer.state === "writing" ? `<span class="wf-now">writing the answer…</span>` : answer.state === "done" ? `<span>✓ answer written</span>` : `<span class="muted-mono">waiting for the work</span>`],
    phase === "answer"
  );
  const log = (flow.log || [])
    .slice()
    .reverse()
    .map(
      (row) =>
        `<div class="wf-log-row"><time>${esc(new Date(row.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }))}</time><span class="wf-actor actor-${esc(row.actor)}">${esc(FLOW_ACTOR[row.actor] || row.actor)}</span><span>${esc(row.text)}</span></div>`
    )
    .join("");
  const status = [
    ["effort", efforts],
    ["helpers", `${line.running || 0}/${line.max_parallel || 4}`],
    ["steps", `${line.done || 0}/${line.steps || 0}`],
    ["replans", line.replans || "0/2"],
    ["waves", line.waves || 0],
    ["jev", line.jev || 0],
    ["advisor", flow.advisor?.on ? "on call" : "off"],
  ]
    .map(([name, value]) => `<span>${esc(name)} [<b>${esc(value)}</b>]</span>`)
    .join("");

  return `<section class="wf${flow.active ? " is-running" : ""}" data-flow aria-label="How this task is working">
    <div class="wf-legend">
      <span class="chief">chief</span><span class="workers">helpers</span><span class="jev">jev</span><span class="critic">critic</span><span class="advisor">advisor</span>
    </div>
    <div class="wf-body">
    ${flowAdvisor(flow.advisor)}
    <div class="wf-map">
      ${chiefLane}
      ${flowLink("", phase === "chief" || phase === "workers")}
      ${jevLane}
      ${flowLink(`handed to helpers · up to ${line.max_parallel || 4} at a time`, phase === "workers")}
      <div class="wf-workers${phase === "workers" ? " is-active" : ""}">${workers}</div>
      ${flowLink("", phase === "critic")}
      ${criticLane}
      ${flowLink("", phase === "answer")}
      ${answerLane}
    </div>
    </div>
    ${flowRoomPointer(flow.room)}
    <div class="wf-log"><div class="wf-log-title">Log</div>${log || `<p class="wf-quiet">Nothing recorded yet.</p>`}</div>
    <div class="wf-status">${status}</div>
  </section>`;
}

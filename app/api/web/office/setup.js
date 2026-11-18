"use strict";

// Setting up the office, in the browser. Every step shows what is actually
// true — a model the provider confirmed, a folder that was read — and each
// action is the same one used elsewhere in the office, so setup grants no
// shortcut around the office's own rules.

const Setup = { state: null };

const STEP_HELP = {
  name: "What you call this office, and what the agents call you.",
  model: "Agents think with a model you own. Your key is stored encrypted on this computer and is never given to an agent or put in a prompt.",
  budget: "What one task may spend, and a monthly limit. At the limit, new tasks are refused; work already running finishes.",
  knowledge: "Folders and documents the office may read. It reads a copy in an isolated reader with no network, and never changes your files.",
  chat: "Get told in Slack when something needs you, and give the office work from there. Chat can never approve anything.",
  sample: "One finished task, written when it is installed. Nothing is called and nothing is spent: it is marked as not real work everywhere it appears, so it can never be read as a live result.",
  task: "Describe an outcome. The office plans it, checks its own work, and stops for you before anything leaves it.",
};
const STEP_ACTION = {
  name: ["Name the office", "setup-name"],
  model: ["Connect a model", "conn-add"],
  budget: ["Set limits", "setup-budget"],
  knowledge: ["Add a folder", "source-add"],
  chat: ["Connect Slack", "slack-connect"],
  sample: ["Install the sample", "sample-install"],
  task: ["Start a task", "new-task"],
};

function setupProgress(state) {
  if (!state) return "";
  const done = state.required_done;
  const total = state.required_total;
  return `${done}/${total}`;
}

async function renderSetup(content) {
  const state = await api("/setup");
  Setup.state = state;
  const steps = state.steps
    .map((step) => {
      const [label, action] = STEP_ACTION[step.key] || ["Open", ""];
      return `<div class="setting-row setup-step ${step.done ? "done" : ""}">
        <div>
          <h3>${step.done ? "✓" : "○"} ${esc(step.title)}${step.optional ? tag("optional", "gray") : ""}</h3>
          <p>${esc(STEP_HELP[step.key] || "")}</p>
          <p class="muted">${esc(step.state)}</p>
        </div>
        <div class="button-row tight">${
          step.key === "sample" && step.done
            ? btn("Remove the sample", "sample-remove")
            : action
              ? btn(step.done ? "Change" : label, action, step.done ? "" : "primary")
              : ""
        }</div>
      </div>`;
    })
    .join("");
  content.innerHTML =
    heading(
      "SET UP YOUR OFFICE",
      state.ready ? "Your office is ready." : "A few things to set up.",
      state.ready
        ? "Everything the office needs is in place. You can change any of it later in Settings."
        : "Each step below says what is true right now, not what has been ticked.",
      state.ready && !state.completed_at ? btn("Finish setup", "setup-finish", "primary") : btn("Guided setup", "onboarding-open")
    ) +
    `<div class="card">${steps}</div>
     <div class="note spaced">The office runs on this computer and only you can open it. Keys are stored encrypted here, agents never see them, and nothing is published or written outside without your approval. Code execution and publishing stay switched off unless an operator turns them on in configuration.</div>`;
}

Office.screens.setup = { title: "Set up", render: renderSetup };

Office.actions = Object.assign(Office.actions || {}, {
  "sample-install": async () => {
    const made = await attempt(
      () => api("/sample-office", { method: "POST" }),
      "Installed. It is marked as not real work wherever it appears."
    );
    if (made) rerender();
  },
  "sample-remove": async () => {
    const gone = await attempt(() => api("/sample-office", { method: "DELETE" }), "Removed.");
    if (gone) rerender();
  },
  "setup-name": () => {
    const state = Setup.state || {};
    formModal(
      "Name your office",
      "office-name-form",
      `${field("Office name", "office_name", { value: state.office_name || "", attrs: 'maxlength="128"' })}
       ${field("Your name", "owner_name", { value: state.owner_name || "", attrs: 'maxlength="255"', hint: "How the office greets you and signs its messages." })}`,
      "Save"
    );
  },
  "setup-budget": async () => {
    const office = await attempt(() => api("/settings"));
    if (!office) return;
    const s = office.settings;
    formModal(
      "Set your limits",
      "setup-budget-form",
      `${field("Monthly limit (USD)", "monthly_cost_limit_usd", { type: "number", required: false, value: s.monthly_cost_limit_usd ?? "", attrs: 'min="0.01" step="0.01"', hint: "Empty means no limit. At the limit, new tasks are refused and scheduled tasks skip." })}
       ${field("Cost per task (USD)", "run_max_cost_usd", { type: "number", required: false, value: s.run_max_cost_usd ?? "", attrs: 'min="0.01" step="0.01"' })}
       ${field("Tokens per task", "run_max_tokens", { type: "number", required: false, value: s.run_max_tokens ?? "", attrs: 'min="1"' })}`,
      "Save limits"
    );
  },
  "setup-finish": async () => {
    if ((await attempt(() => api("/setup/complete", { method: "POST" }), "Your office is set up.")) === undefined) return;
    go("home");
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "office-name-form": async (form) => {
    const data = new FormData(form);
    const body = {
      office_name: String(data.get("office_name")).trim(),
      owner_name: String(data.get("owner_name")).trim(),
    };
    if ((await attempt(() => api("/settings", { method: "PUT", body }), "Saved.")) === undefined) return;
    closeModal();
    Office.officeName = body.office_name;
    Office.owner = body.owner_name || Office.owner;
    const name = $("#office-name");
    if (name) name.textContent = body.office_name;
    const initial = $("#office-initial");
    if (initial) initial.textContent = body.office_name.charAt(0).toUpperCase();
    rerender();
  },
  "setup-budget-form": async (form) => {
    const data = new FormData(form);
    const body = {};
    for (const [key, value] of data.entries()) {
      const text = String(value).trim();
      body[key] = text === "" ? null : Number(text);
    }
    if ((await attempt(() => api("/settings", { method: "PUT", body }), "Limits saved.")) === undefined) return;
    closeModal();
    rerender();
  },
});

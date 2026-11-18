"use strict";

// First-run onboarding (Settings design, §10: "a first-run onboarding in place
// of Setup"): on a new office, one step at a time over the page, each step the
// same action the Setup screen uses, so it grants nothing Setup would not.
// What each step says is read from the office (/setup), never ticked by hand.
// "Later" closes it for good in this browser; Setup keeps the full list and
// opens this again from its "Guided setup" button.

const Onboarding = { state: null, at: 0, connections: null };
const ONBOARDING_LATER_KEY = "office.onboarding.later";
// The order a new owner meets them: who they are, a model, what it may
// spend, then work; the sample is offered before the first task.
const ONBOARDING_ORDER = ["name", "model", "budget", "sample", "task"];

function onboardingSteps(state) {
  const byKey = new Map((state?.steps || []).map((step) => [step.key, step]));
  return ONBOARDING_ORDER.map((key) => byKey.get(key)).filter(Boolean);
}

function onboardingDialog() {
  let dialog = document.getElementById("onboarding");
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = "onboarding";
  dialog.className = "onboarding";
  dialog.setAttribute("aria-labelledby", "onboarding-title");
  document.body.appendChild(dialog);
  return dialog;
}

async function onboardingRead() {
  const [state, connections] = await Promise.all([
    api("/setup").catch((error) => {
      console.error("Could not read the setup steps", error);
      return null;
    }),
    api("/connections").catch(() => []),
  ]);
  Onboarding.state = state;
  Onboarding.connections = Array.isArray(connections) ? connections : [];
  return state;
}

// The model step needs a role for each model, not only a connection.
function onboardingModelExtra(step) {
  const ready = (Onboarding.connections || []).filter((c) => ["MODEL_AVAILABLE", "VERIFIED"].includes(c.status));
  const missing = step.detail?.roles_missing || [];
  if (!ready.length || !missing.length) return "";
  const first = ready[0];
  return `<div class="onboarding-extra"><p>${esc(first.display_name)} (${esc(first.model_id)}) is checked and ready, but ${esc(missing.join(", "))} ${missing.length === 1 ? "has" : "have"} no model yet.</p>${btn(`Use it for every role`, `onboarding-roles:${first.id}`, "primary")}<p class="muted">You can give each role its own model later, in Settings → Models.</p></div>`;
}

function onboardingDraw() {
  const dialog = onboardingDialog();
  const steps = onboardingSteps(Onboarding.state);
  if (!steps.length) {
    dialog.innerHTML = `<div class="onboarding-body"><h2 id="onboarding-title">Setting up</h2><p>The setup steps could not be read just now.</p><div class="button-row">${btn("Close", "onboarding-close", "primary")}</div></div>`;
    return;
  }
  Onboarding.at = Math.max(0, Math.min(Onboarding.at, steps.length - 1));
  const step = steps[Onboarding.at];
  const [label, action] = STEP_ACTION[step.key] || ["Open", ""];
  const dots = steps
    .map((s, i) => `<button type="button" class="onboarding-dot${i === Onboarding.at ? " on" : ""}${s.done ? " done" : ""}" data-action="onboarding-go:${i}" aria-label="Step ${i + 1}: ${esc(s.title)}${s.done ? ", done" : ""}" aria-current="${i === Onboarding.at ? "step" : "false"}"></button>`)
    .join("");
  const last = Onboarding.at === steps.length - 1;
  const ready = Boolean(Onboarding.state?.ready);
  dialog.innerHTML = `<div class="onboarding-body">
      <p class="onboarding-eyebrow">Step ${Onboarding.at + 1} of ${steps.length}${step.optional ? " · optional" : ""}</p>
      <h2 id="onboarding-title">${step.done ? "✓ " : ""}${esc(step.title)}</h2>
      <p class="onboarding-help">${esc(STEP_HELP[step.key] || "")}</p>
      <p class="onboarding-state ${step.done ? "is-done" : ""}">${esc(step.state)}</p>
      ${step.key === "model" ? onboardingModelExtra(step) : ""}
      <div class="button-row onboarding-act">${action && !(step.key === "sample" && step.done) ? btn(step.done ? "Change" : label, `onboarding-do:${action}`, step.done ? "" : "primary") : ""}</div>
      <div class="onboarding-foot">
        <div class="onboarding-dots" role="group" aria-label="Steps">${dots}</div>
        <div class="button-row">
          ${btn("Later", "onboarding-later")}
          ${Onboarding.at > 0 ? btn("Back", "onboarding-go:" + (Onboarding.at - 1)) : ""}
          ${last ? btn(ready ? "Finish" : "Done for now", "onboarding-finish", "primary") : btn(step.done || step.optional ? "Next" : "Skip for now", "onboarding-go:" + (Onboarding.at + 1), step.done ? "primary" : "")}
        </div>
      </div>
    </div>`;
}

async function onboardingOpen(at) {
  const state = await onboardingRead();
  if (!state) return;
  const steps = onboardingSteps(state);
  // Start at the first step not done yet, unless a step was asked for.
  Onboarding.at = typeof at === "number" ? at : Math.max(0, steps.findIndex((s) => !s.done && !s.optional));
  onboardingDraw();
  const dialog = onboardingDialog();
  if (!dialog.open) dialog.showModal();
}

function onboardingClose() {
  const dialog = document.getElementById("onboarding");
  if (dialog?.open) dialog.close();
}

// A step's own form (a modal over this one) closed: read what is true now.
document.getElementById("modal")?.addEventListener("close", async () => {
  if (!document.getElementById("onboarding")?.open) return;
  await onboardingRead();
  onboardingDraw();
});

Office.actions = Object.assign(Office.actions || {}, {
  "onboarding-open": () => onboardingOpen(),
  "onboarding-go": (index) => {
    Onboarding.at = Number(index) || 0;
    onboardingDraw();
  },
  "onboarding-do": (action) => {
    const [name, ...rest] = String(action).split(":");
    const run = Office.actions[name];
    if (typeof run !== "function") {
      console.error("Onboarding: no action", action);
      return;
    }
    // A step that leaves the page (a new task) ends onboarding for now.
    if (name === "new-task") onboardingClose();
    run(rest.join(":"));
  },
  "onboarding-roles": async (connectionId) => {
    const roles = Object.fromEntries(["chief", "researcher", "strategist", "critic"].map((role) => [role, connectionId]));
    if ((await attempt(() => api("/settings", { method: "PUT", body: { role_models: roles } }), "Every role uses it now.")) === undefined) return;
    await onboardingRead();
    onboardingDraw();
  },
  "onboarding-later": () => {
    try {
      localStorage.setItem(ONBOARDING_LATER_KEY, "1");
    } catch (error) {
      // Asked again next time.
    }
    onboardingClose();
    toast("Setup waits under Set up your office, whenever you like.");
  },
  "onboarding-finish": async () => {
    if (Onboarding.state?.ready && !Onboarding.state?.completed_at) {
      if ((await attempt(() => api("/setup/complete", { method: "POST" }), "Your office is set up.")) === undefined) return;
    }
    onboardingClose();
    go("home");
  },
  "onboarding-close": () => onboardingClose(),
});

// On a new office, once signed in: open by itself, unless set aside here.
async function onboardingMaybe() {
  let later = false;
  try {
    later = localStorage.getItem(ONBOARDING_LATER_KEY) === "1";
  } catch (error) {
    later = false;
  }
  if (later || !Office.csrf || document.getElementById("onboarding")?.open) return;
  const state = await onboardingRead();
  if (!state || state.completed_at || state.ready) return;
  // Only a new office: no task ever asked, and little else done yet.
  const task = (state.steps || []).find((step) => step.key === "task");
  if (task?.done || (state.required_done || 0) > 1) return;
  onboardingOpen();
}

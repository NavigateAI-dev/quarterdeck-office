"use strict";

// Shared building blocks for the office: API access, the session, markup
// helpers, dialogs and routing. Every screen renders from live API data.

const Office = {
  csrf: "",
  owner: "",
  screens: {},
  view: "home",
  param: "",
  timers: [],
};

const $ = (selector) => document.querySelector(selector);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

class SignedOut extends Error {}

// A page kept open across an update (a phone's home-screen app especially)
// keeps running the old code: it says so, and reloads at the next safe moment.
function noteBuild(build) {
  if (!build) return;
  if (!Office.build) {
    Office.build = build;
    return;
  }
  if (build === Office.build || Office.stale) return;
  Office.stale = true;
  if (document.getElementById("update-bar")) return;
  const bar = document.createElement("div");
  bar.id = "update-bar";
  bar.className = "update-bar";
  bar.setAttribute("role", "status");
  bar.innerHTML = `<span>The office has been updated.</span><button type="button" class="primary" data-action="reload-office">Reload</button>`;
  document.body.appendChild(bar);
}

Office.actions = Object.assign(Office.actions || {}, {
  "reload-office": () => location.reload(),
});

// Reloads into the new version when nothing is open that a reload would lose.
function reloadIfStale() {
  if (!Office.stale) return false;
  if (document.getElementById("modal")?.open) return false;
  location.reload();
  return true;
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers);
  const method = options.method || "GET";
  // Anything that widens what the office may do waits for the owner's Confirm (widen.js).
  if (method !== "GET" && !options.confirmed && typeof widenConfirm === "function") {
    if (!(await widenConfirm(path, method, options.body))) throw new NotConfirmed();
  }
  let body = options.body;
  if (body !== undefined && typeof body !== "string") body = JSON.stringify(body);
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && Office.csrf) headers["X-CSRF-Token"] = Office.csrf;
  const response = await fetch(path, { method, headers, body, credentials: "same-origin" });
  noteBuild(response.headers.get("X-Office-Build"));
  if (!response.ok) {
    let detail = String(response.status);
    let structured = null;
    try {
      const data = await response.json();
      // Some refusals carry more than a code, such as the skills to choose from.
      if (data && data.detail && typeof data.detail === "object" && !Array.isArray(data.detail)) {
        structured = data.detail;
        detail = String(data.detail.code || detail);
      }
      if (data && typeof data.detail === "string") detail = data.detail;
      else if (Array.isArray(data?.detail) && data.detail[0]?.msg) detail = data.detail[0].msg;
    } catch {
      /* a non-JSON error body carries nothing worth showing */
    }
    if (response.status === 401 && !path.startsWith("/session")) {
      Office.csrf = "";
      showSignedOut("Your session ended. Sign in again.");
      throw new SignedOut(detail);
    }
    const failure = new Error(detail);
    failure.detail = structured;
    throw failure;
  }
  return response.status === 204 ? null : response.json();
}

// ---- markup helpers (all text goes through esc) ----

const COLORS = ["", "clay", "blue", "gray"];
function avatar(name, index) {
  const letter = esc(String(name || "?").trim().charAt(0).toUpperCase() || "?");
  const color = COLORS[(index ?? hash(name)) % COLORS.length];
  return `<span class="avatar ${color}">${letter}</span>`;
}
function hash(text) {
  let total = 0;
  for (const c of String(text || "")) total = (total + c.charCodeAt(0)) % 997;
  return total;
}
const tag = (text, cls = "") => `<span class="tag ${cls}">${esc(text)}</span>`;
const btn = (label, action, cls = "", extra = "") =>
  `<button type="button" class="${cls}" data-action="${esc(action)}" ${extra}>${label}</button>`;
// A title long enough to fill the page is set smaller and clamped to three
// lines, with the whole of it on hover: a question used as a title is still a title.
const LONG_TITLE = 34;
function heading(kicker, title, description, actions = "") {
  const text = String(title ?? "");
  const long = text.length > LONG_TITLE ? ' class="long"' : "";
  return `<div class="heading"><div><div class="eyebrow">${esc(kicker)}</div><h1${long} title="${esc(text)}">${esc(text)}</h1><p>${esc(description)}</p></div>${actions}</div>`;
}
function emptyCard(title, text, action = "") {
  return `<div class="card empty"><h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;
}
function field(label, name, options = {}) {
  const id = `f-${name}`;
  const required = options.required === false ? "" : "required";
  const attrs = `id="${id}" name="${name}" ${required} ${options.attrs || ""}`;
  let control;
  if (options.type === "textarea") control = `<textarea ${attrs}>${esc(options.value || "")}</textarea>`;
  else if (options.type === "select")
    control = `<select ${attrs}>${(options.choices || [])
      .map(
        ([value, text, disabled]) =>
          `<option value="${esc(value)}" ${value === options.value ? "selected" : ""} ${disabled ? "disabled" : ""}>${esc(text)}</option>`
      )
      .join("")}</select>`;
  else control = `<input ${attrs} type="${options.type || "text"}" value="${esc(options.value || "")}">`;
  return `<label class="field" for="${id}">${esc(label)}</label>${control}${options.hint ? `<p class="muted hint">${esc(options.hint)}</p>` : ""}`;
}

// ---- time and status ----

function money(value) {
  return `$${Number(value || 0).toFixed(2)}`;
}

function when(value) {
  if (!value) return "";
  const date = new Date(value);
  const today = new Date();
  const time = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (date.toDateString() === today.toDateString()) return time;
  return `${date.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
}

const STATUS = {
  PENDING: ["Starting", ""],
  RUNNING: ["In progress", ""],
  RECOVERING: ["Resuming", ""],
  PAUSED: ["Paused", "gray"],
  WAITING_FOR_APPROVAL: ["Needs your approval", "amber"],
  NEEDS_HUMAN_REVIEW: ["Needs your review", "amber"],
  BLOCKED: ["Blocked · needs you", "amber"],
  COMPLETED: ["Completed", ""],
  FAILED: ["Failed", "clay"],
  REJECTED: ["Rejected", "gray"],
  CLOSED: ["Closed", "gray"],
  CANCELLED: ["Cancelled", "gray"],
};
function statusTag(status) {
  const [text, cls] = STATUS[status] || [status, "gray"];
  return tag(text, cls);
}
const NEEDS_OWNER = ["WAITING_FOR_APPROVAL", "NEEDS_HUMAN_REVIEW", "BLOCKED"];
const ACTIVE = ["PENDING", "RUNNING", "RECOVERING", "PAUSED"];
// A task whose own limits the owner can still change; it has work left.
const LIMIT_EDITABLE_UI = ACTIVE;

const ROLE_LABEL = {
  chief: "Chief",
  planner: "Chief",
  researcher: "Researcher",
  strategist: "Strategist",
  critic: "Critic",
  advisor: "Advisor",
  writer: "Writer",
  summarist: "Writer",
  // Not an agent: what paid tool calls cost, recorded beside the models.
  tools: "Paid tools",
};
// Why a task stopped, in words. Codes stay in the records; the owner reads these.
const STOP_TEXT = {
  // A fault in the office itself, not in the work; it stops rather than retrying.
  // The office stopped during approved engineering work and cannot tell whether it finished.
  engineering_outcome_uncertain: "The office stopped while carrying out the approved work and cannot tell whether it finished. Check the repository (and any merge request) before deciding; it will not run the work a second time on its own.",
  // 6 Oct: no research could run; the providers were down or refusing.
  providers_unavailable: "The research could not run: the model providers were unavailable or refusing. Try again when they answer.",
  unexpected_error: "The office hit a fault of its own while working, not a problem with the work. It stopped instead of retrying; Rework tries again.",
  needs_owner_answers: "The Chief needs your answers before the research can start.",
  evidence_not_reachable: "The Chief thought the evidence this needs is out of the office's reach. Continue and it researches what it can reach, saying what it could not see.",
  revision_not_converging: "The critic kept raising the same concerns, so the office stopped revising. Accept the work as it is, or say what to change.",
  revision_made_no_change: "A revision changed nothing, so the office stopped. Say what should change, or accept the work as it is.",
  revision_limit_reached: "The office used all its revision rounds. Accept the work as it is, or say what to change.",
  critic_blocked: "The critic found a problem it would not pass. Read its critique, then decide.",
  engineering_failed: "The follow-up work failed.",
};

function stopText(code) {
  const text = String(code || "");
  const known = STOP_TEXT[text] || STOP_TEXT[text.split(":")[0]];
  if (known) return known;
  if (text.startsWith("closed_by_")) return `Closed: ${text.replace(/^closed_by_[^:]*:\s*/, "")}`;
  return text ? text.replace(/_/g, " ") : "";
}

const NODE_ROLE = { plan: "chief", research: "researcher", strategy: "strategist", critique: "critic", synthesis: "chief", approval: "owner", engineering: "engineer", work: "chief", replan: "chief" };
const NODE_LABEL = { plan: "planning", research: "research", strategy: "strategy", critique: "review", synthesis: "the summary", approval: "the approval step", engineering: "the follow-up", work: "the planned steps", replan: "replanning" };

// ---- dialog and toast ----

function modal(title, body, buttons) {
  const dialog = $("#modal");
  const content = $("#modal-content");
  if (!dialog || !content) {
    console.error("Element not found: #modal");
    return;
  }
  content.innerHTML = `<div class="modal-top"><h2>${esc(title)}</h2><button type="button" data-action="close" aria-label="Close dialog">×</button></div>${body}${
    buttons === undefined ? `<div class="button-row">${btn("Done", "close", "primary")}</div>` : buttons
  }`;
  if (!dialog.open) dialog.showModal();
}
function formModal(title, id, body, submitLabel) {
  modal(title, `<form id="${id}">${body}<div class="button-row">${btn("Cancel", "close")}<button class="primary">${esc(submitLabel)}</button></div></form>`, "");
}
function closeModal() {
  const dialog = $("#modal");
  if (dialog?.open) dialog.close();
}
function toast(text, isError = false) {
  // The Island says it where the eye is; the status region still announces it.
  if (typeof Island !== "undefined") Island.say(text, isError);
  const node = $("#toast");
  if (!node) return;
  node.textContent = text;
  node.classList.toggle("error", isError);
  node.classList.add("show");
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => node.classList.remove("show"), isError ? 6000 : 3500);
}

// Friendly text for the API's error codes; unknown codes are shown as they are.
const ERRORS = {
  not_confirmed: "Nothing changed.",
  publish_token_missing: "Add a token for that place first.",
  token_refused: "The token was refused. Check it is current and has the permissions listed.",
  repository_name_taken: "You already have a GitHub repository with that name. Choose another name.",
  no_website: "There is no index.html to publish.",
  host_unreachable: "The hosting service did not answer. Try again in a minute.",
  pages_refused: "GitHub did not switch Pages on; the repository was made. Check the token's Pages permission.",
  login_name_invalid: "Give a sign-in name of up to 120 characters.",
  password_too_short: "Use a password of at least 10 characters.",
  current_password_wrong: "The current password was not right.",
  insufficient_balance: "The provider account is out of balance. Top it up, or choose another model for this role.",
  skill_not_found: "That skill could not be found at that address.",
  skill_github_rate_limited: "GitHub is limiting how often the office may read it. Try again in a few minutes.",
  skill_source_unreachable: "The skill's repository did not answer. Try again shortly.",
  skill_no_skill_here: "That repository holds no SKILL.md.",
  skill_file_too_large: "The skill is larger than the office accepts.",
  skill_id_taken: "A curated skill already has that name.",
  skill_unusable: "The skill could not be read as instructions.",
  not_finished: "This task is still going. Give it direction, or wait until it finishes to rework it.",
  run_not_pausable: "This task is not running, so it cannot be paused.",
  run_not_cancellable: "This task has already finished.",
  run_not_paused: "This task is not paused.",
  nothing_learned: "This task did not record anything a later task could use.",
  already_decided: "You have already decided about this lesson.",
  run_finished: "This task has finished, so its limits can no longer change.",
  raise_the_limit_first: "It is waiting at its limit, so resuming would stop it again at once. Open the task and raise the limit to continue it.",
  run_not_accepting_instructions: "This task is not accepting direction right now.",
  revision_target_ambiguous: "Say whether the research or the strategy should change.",
  stale_scope: "The proposal changed since you opened it. Reload and review it again.",
  intake_requires_typesafe: "Describing work in plain text needs the TypeSafe key.",
  below_spent_tokens: "That is lower than the tokens it has already used, so it would stop again at once. Choose a higher number.",
  calendar_refused: "The calendar refused the office: check the user name and app password, or that the link is the private one.",
  calendar_not_found: "Nothing is at that address. Copy the calendar link again.",
  calendar_unreachable: "The calendar's server could not be reached. Check the address and your connection.",
  calendar_timed_out: "The calendar's server took too long to answer. Try again in a minute.",
  calendar_unavailable: "The calendar's server answered with an error. Try again later.",
  calendar_too_large: "That calendar is larger than the office reads (10 MB).",
  calendar_address_invalid: "That is not a calendar link. It starts with https:// or webcal://.",
  calendar_address_missing: "Paste the calendar's link.",
  calendar_account_incomplete: "Give the user name and the app-specific password (and the server address, where asked).",
  calendar_none_found: "That account has no calendars with events.",
  calendar_server_unreadable: "The calendar's server answered in a way the office cannot read. Check the CalDAV address.",
  not_a_calendar: "That address does not give a calendar. Copy the link that ends in .ics.",
  calendar_repeats_unreadable: "A repeating event in that calendar could not be read.",
  calendar_secret_unavailable: "The office's key store is not available, so the calendar could not be kept sealed.",
  calendar_read_only: "That calendar is read only: it came in as a link, or macOS keeps it unchangeable (subscribed or shared).",
  event_not_found: "That event is no longer in your calendar. The view will refresh.",
  calendar_change_refused: "macOS refused the change. Check the calendar in the Calendar app.",
  calendar_change_failed: "The change could not be made. Try again.",
  sign_in_app_missing: "Set up the sign-in first: it takes about five minutes, once.",
  sign_in_app_incomplete: "Paste the ID (and, for Google, the secret) from your sign-in app.",
  sign_in_refused: "The sign-in was refused. Check the app's ID and secret, then sign in again.",
  sign_in_failed: "The sign-in did not complete. Try again.",
  sign_in_unreachable: "The sign-in service could not be reached. Check your connection.",
  sign_in_no_refresh: "The company did not give lasting access. Remove the office in your account's connected apps, then sign in again.",
  mac_access_denied: "macOS said no. Allow “Quarterdeck Calendar” in System Settings, Privacy & Security, Calendars.",
  mac_access_restricted: "Calendar access is restricted on this Mac by Screen Time or a device profile.",
  calendar_kind_unknown: "That kind of calendar is not one the office reads.",
  nothing_to_finish: "Nothing was researched or written yet, so there is no answer to finish with. Raise the limit, or stop it.",
  run_not_finishable: "This task is no longer waiting, so it cannot be finished from here. Reopen it to see where it is.",
  below_spent_cost: "That is lower than what it has already spent, so it would stop again at once. Choose a higher amount.",
  nothing_raised: "Nothing to raise: give a new limit first.",
};
function explain(error) {
  const message = error?.message || String(error);
  // A form value the office cannot use: say so in words, not as its rule.
  if (/^String should match pattern/.test(message)) return "A value in the form uses characters the office cannot use there. Use letters, numbers and dashes.";
  return ERRORS[message] || message;
}
async function attempt(work, success) {
  try {
    const result = await work();
    if (success) toast(success);
    return result;
  } catch (error) {
    // Cancelled at the confirm step: nothing was sent, nothing to say.
    const quiet = error instanceof SignedOut || (typeof NotConfirmed !== "undefined" && error instanceof NotConfirmed);
    if (!quiet) toast(explain(error), true);
    return undefined;
  }
}

// ---- routing ----

function every(ms, work) {
  const id = setInterval(() => work().catch((error) => console.error(error)), ms);
  Office.timers.push(id);
}
function stopTimers() {
  Office.timers.forEach(clearInterval);
  Office.timers = [];
}
function go(view, param = "") {
  location.hash = param ? `${view}/${param}` : view;
}
function showSignedOut(message) {
  stopTimers();
  if (typeof liveStop === "function") liveStop();
  const shell = $("#shell");
  const out = $("#signed-out");
  if (shell) shell.hidden = true;
  if (out) out.hidden = false;
  const error = $("#signed-out-error");
  if (error) error.textContent = message || "";
  showPasswordForm();
}

// The name and password form shows only once the owner has set one.
async function showPasswordForm() {
  const form = $("#password-form");
  if (!form) return;
  try {
    const response = await fetch("/session/methods", { credentials: "same-origin" });
    const methods = response.ok ? await response.json() : {};
    const on = Boolean(methods?.password);
    form.hidden = !on;
    const or = document.querySelector(".password-or");
    if (or) or.hidden = !on;
    const intro = document.querySelector(".launch-intro");
    if (intro) intro.hidden = on;
    if (on) $("#login-name")?.focus();
  } catch (error) {
    console.error("Could not read sign-in methods", error);
  }
}


// A request id for what the owner says. Sending the same words again
// after a dropped connection reuses it, so the office finds the message it
// already has instead of saying it twice. getRandomValues works over plain
// HTTP on the home network, where randomUUID does not.
const SENDING = new Map();
function requestKey(scope, text) {
  const held = SENDING.get(scope);
  if (held && held.text === text) return held.key;
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  const key = "k-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  SENDING.set(scope, { text, key });
  return key;
}
function requestSent(scope) {
  SENDING.delete(scope);
}

"use strict";

// A confirm step for anything that widens what the office may do
// (decision record 0008). Narrowing never
// asks; widening says what changes and waits for the owner's Confirm. One
// place, consulted by api() before it sends, so every screen gets it.
// Cancel sends nothing and throws NotConfirmed, which attempt() keeps quiet.

class NotConfirmed extends Error {
  constructor() {
    super("not_confirmed");
    this.name = "NotConfirmed";
  }
}

// Limits the owner can raise: a higher number (or none) lets work spend more.
const WIDEN_LIMITS = {
  run_max_cost_usd: ["What one task may spend", (v) => money(v)],
  run_max_tokens: ["The tokens one task may use", (v) => Number(v).toLocaleString()],
  run_max_minutes: ["How long one task may run", (v) => `${v} min`],
  monthly_cost_limit_usd: ["What the office may spend in a month", (v) => money(v)],
  chat_daily_max_tokens: ["What chat may use in a day", (v) => `${Number(v).toLocaleString()} tokens`],
  runs_at_once: ["Tasks running at the same time", (v) => String(v)],
};

const WIDEN_SWITCHES = {
  browser_on: "New tasks may use the office's own browser: it visits websites for you, signed in where you signed it in.",
  related_across_lines: "Work may draw on related work from any business line, not only its own.",
  advisor_on: "The advisor joins new tasks: one more model on each, so a little more spend.",
};

async function widenCurrent(path) {
  try {
    return await api(path, { confirmed: true });
  } catch (error) {
    console.warn("Could not read the current values to compare:", path, error);
    return null;
  }
}

function widenLimitLines(body, current) {
  const lines = [];
  for (const [key, [label, shown]] of Object.entries(WIDEN_LIMITS)) {
    if (!(key in body)) continue;
    const next = body[key];
    const now = current?.[key];
    if (next === null || next === undefined) {
      if (now !== null && now !== undefined) lines.push(`${label}: back to the configured default (from ${shown(now)})`);
    } else if (now === null || now === undefined || Number(next) > Number(now)) {
      lines.push(`${label}: ${now === null || now === undefined ? "" : `${shown(now)} → `}${shown(next)}`);
    }
  }
  return lines;
}

// What a request widens, in the owner's words; none means it narrows or is neutral.
async function widenLines(path, method, body) {
  const data = body && typeof body === "object" ? body : {};
  if (method === "PUT" && path === "/settings") {
    const lines = Object.entries(WIDEN_SWITCHES)
      .filter(([key]) => data[key] === true)
      .map(([, said]) => said);
    if (Object.keys(WIDEN_LIMITS).some((key) => key in data)) {
      const current = await widenCurrent("/settings");
      lines.push(...widenLimitLines(data, current?.settings || current));
    }
    return lines;
  }
  if (method === "PUT" && /^\/projects\/[^/]+\/settings$/.test(path)) {
    if (!Object.keys(WIDEN_LIMITS).some((key) => key in data)) return [];
    const current = await widenCurrent(path);
    return widenLimitLines(data, current?.project || current);
  }
  if (method === "PUT" && /^\/topics\/[^/]+\/settings$/.test(path)) {
    const lines = [];
    if (data.access === "work") lines.push("Coding work started in this thread may change files in the space's folder, on a branch of its own. Merging stays yours.");
    if (typeof data.goal_cap_usd === "number" && data.goal_cap_usd > 0.5) lines.push(`Work started in this thread may spend up to ${money(data.goal_cap_usd)} before it asks.`);
    return lines;
  }
  if (method === "PATCH" && /^\/workspaces\/[^/]+$/.test(path) && data.access === "work") {
    return ["Coding work in this space may change files in this folder, on office/ branches. Your checkout and default branch are never written; merging stays yours."];
  }
  if (method === "POST" && /\/tools\/[^/]+\/decision$/.test(path) && data.stance === "allowed") {
    return ["The agents may call this tool without asking you first."];
  }
  if (method === "POST" && /\/decide-undecided$/.test(path) && (data.read_only === "allowed" || data.others === "allowed")) {
    return [`The agents may call ${data.others === "allowed" ? "every undecided tool on this server" : "this server's read-only tools"} without asking you first.`];
  }
  if ((method === "POST" && path === "/routines") || (method === "PUT" && /^\/routines\/[^/]+$/.test(path) && data.enabled === true)) {
    return ["The office does this on its own at the times you set, on your keys and inside your monthly limit, without asking first."];
  }
  if (method === "PUT" && path === "/archive/protection" && archiveProtectionWidens(data)) {
    return ["Your AI history can reach more: more places or more model providers than before, or backups that hold it."];
  }
  if (method === "POST" && /^\/archive\/conversations\/[^/]+\/memories$/.test(path)) {
    return ["This conversation goes to your lead's model, and is paid for on your key. Secrets and identity numbers are masked first, but the rest of the text is not."];
  }
  if (method === "POST" && /^\/archive\/imports\/[^/]+\/open$/.test(path)) {
    return ["Agents may search these conversations, and the passages they find go to their models. Anything flagged as sensitive stays hidden."];
  }
  if (method === "PUT" && /^\/archive\/conversations\/[^/]+$/.test(path) && data.excluded === false) {
    return ["Agents may search this conversation, and the passages they find go to their models. Secrets and identity numbers are masked first, but the rest of the text is not."];
  }
  if (method === "PUT" && /^\/mcp\/servers\/[^/]+\/no-project$/.test(path)) {
    return ["Any discussion with no project may use this server's reviewed tools. A tool that asks you first still does."];
  }
  if (method === "PUT" && /^\/policies\/[^/]+$/.test(path) && data.enabled === false) {
    return ["This rule stops applying: what it held back or asked about goes ahead."];
  }
  if (method === "PUT" && path === "/settings/coding-agent" && data.packages === true) {
    return ["Builds may install packages from npm and PyPI (install scripts stay off)."];
  }
  if (method === "POST" && path === "/access/home/enable") {
    return ["The office opens on your home network: other devices there can reach its sign-in page."];
  }
  if (method === "POST" && (path === "/access/remote/enable" || path === "/access/remote/connect")) {
    return ["The office can be reached from outside this computer, through your private tailnet."];
  }
  if (method === "POST" && /^\/channels\/installations\/[^/]+\/members$/.test(path)) {
    return ["This person may talk to the office's chat bots and give it work, on your keys and inside your limits. Approvals stay yours."];
  }
  return [];
}

function widenDialog() {
  let dialog = document.getElementById("widen-confirm");
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = "widen-confirm";
  dialog.className = "widen-confirm";
  dialog.setAttribute("aria-labelledby", "widen-title");
  document.body.appendChild(dialog);
  return dialog;
}

// Resolves true on Confirm, false on Cancel, Esc or the backdrop.
function widenAsk(lines) {
  const dialog = widenDialog();
  dialog.innerHTML = `<form method="dialog" class="widen-body">
      <h2 id="widen-title">This lets the office do more</h2>
      <ul class="widen-lines">${lines.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>
      <p class="muted">You can narrow it again at any time.</p>
      <div class="button-row"><button value="cancel" type="submit">Cancel</button><button value="confirm" type="submit" class="primary" autofocus>Confirm</button></div>
    </form>`;
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
    dialog.addEventListener(
      "click",
      (event) => {
        if (event.target === dialog) dialog.close("cancel");
      },
      { once: true }
    );
    dialog.returnValue = "";
    dialog.showModal();
  });
}

// Called by api() before it sends anything that is not a read.
async function widenConfirm(path, method, body) {
  let lines = [];
  try {
    lines = await widenLines(String(path).split("?")[0], method, body);
  } catch (error) {
    console.error("Could not tell what this change widens; asking anyway", error);
    lines = ["This change may let the office do more than it does now."];
  }
  if (!lines.length) return true;
  return widenAsk(lines);
}

// ---- Text size (Settings → General): this device's choice, kept in this browser.
// Every font size is in rem, so the root size scales all text and
// leaves the layout's spacing as it is.
const TEXT_SIZES = [
  ["0.9", "Smaller"],
  ["1", "Standard"],
  ["1.15", "Larger"],
  ["1.3", "Largest"],
];
const TEXT_SIZE_KEY = "office.text-size";

function textSizeNow() {
  try {
    const kept = localStorage.getItem(TEXT_SIZE_KEY) || "1";
    return TEXT_SIZES.some(([value]) => value === kept) ? kept : "1";
  } catch (error) {
    return "1";
  }
}

function textSizeApply(value) {
  document.documentElement.style.fontSize = value === "1" ? "" : `${16 * Number(value)}px`;
}

function textSizeCard() {
  const now = textSizeNow();
  const options = TEXT_SIZES.map(
    ([value, label]) =>
      `<button type="button" role="radio" aria-checked="${now === value}" class="${now === value ? "on" : ""}" data-action="text-size:${value}" style="font-size:${Number(value)}rem">${esc(label)}</button>`
  ).join("");
  return `<div class="card text-size-card"><h2>Text size</h2><div class="text-size-options" role="radiogroup" aria-label="Text size">${options}</div><p class="muted">For this device only. Your browser's own zoom still works as well.</p></div>`;
}

textSizeApply(textSizeNow());

Office.actions = Object.assign(Office.actions || {}, {
  "text-size": (value) => {
    if (!TEXT_SIZES.some(([size]) => size === value)) return;
    try {
      localStorage.setItem(TEXT_SIZE_KEY, value);
    } catch (error) {
      // Kept for this page only.
    }
    textSizeApply(value);
    rerender();
  },
});

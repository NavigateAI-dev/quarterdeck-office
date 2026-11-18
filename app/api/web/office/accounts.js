"use strict";

// Accounts: every outside account the office can use, in one
// list, each connected by the owner's own consent on the company's page.
// Google and Microsoft share the calendar's sign-in app; the rest sign in as
// tool servers. Nothing here is a password the office keeps.

const Accounts = { tab: "all", data: null, models: [] };

const ACCOUNT_TABS = [
  ["all", "All"],
  ["connections", "Connections"],
  ["logins", "Logins"],
  ["keys", "API keys"],
];

const ACCOUNT_RETURN = {
  connected: ["Connected. Its tools are ready; choose the projects that may use them.", false],
  sign_in_declined: ["You did not allow access, so nothing was connected.", true],
  sign_in_expired: ["That sign-in took longer than ten minutes. Press Connect again.", true],
  "sign-in-failed": ["Sign-in did not finish. Press Connect to try again.", true],
};

function accountsReturned(param) {
  if (!param) return;
  const said = param.startsWith("signed-in/")
    ? ["Signed in. Review its tools, then choose the projects that may use them.", false]
    : ACCOUNT_RETURN[param] || [explain(new Error(param)), true];
  toast(said[0], said[1]);
  history.replaceState(null, "", "#accounts");
  Office.param = "";
}

function accountMark(name) {
  const letter = String(name || "?").trim().charAt(0).toUpperCase();
  let hue = 0;
  for (const ch of String(name || "")) hue = (hue * 31 + ch.charCodeAt(0)) % 360;
  return `<span class="account-mark" style="--mark-hue:${hue}" aria-hidden="true">${esc(letter)}</span>`;
}

function accountRow(name, covers, state, actions, extra = "") {
  return `<li class="account-row">${accountMark(name)}<div class="account-what"><strong>${esc(name)}</strong><small>${esc(covers)}</small>${extra}</div><div class="account-state">${state}</div><div class="account-actions">${actions}</div></li>`;
}

function usedBy(server) {
  const count = server?.projects?.length || 0;
  return count ? `used by ${count} project${count === 1 ? "" : "s"}` : "no project uses it yet";
}

// Google or Microsoft: mail and files through the calendar's sign-in app.
function accountOwnRow(row) {
  const server = row.server;
  const calendar = row.calendar ? `<span class="tag">Calendar connected</span>` : `<a href="#calendars">Calendar →</a>`;
  let state;
  let actions;
  let note = "";
  if (server) {
    state = `<span class="account-on">Connected${row.email ? ` · ${esc(row.email)}` : ""}</span><small>${esc(server.callable)} read-only tools · ${esc(usedBy(server))}</small>`;
    actions = btn("Projects…", `account-projects:${server.id}`) + btn("Disconnect", `account-disconnect:${server.id}`, "danger");
  } else if (row.app_set) {
    state = `<small>Asks ${esc(row.name)} for your consent</small>`;
    actions = btn("Connect", `account-connect:${row.id}`, "primary");
    if (row.id === "google") note = `<p class="muted account-note">If you set Google up for the calendar only: in your Google project, enable the Gmail API and the Google Drive API first.</p>`;
  } else {
    state = `<small>Set up once (about five minutes, guided), then one press</small>`;
    actions = btn("Set it up", `cal-app:${row.id}`, "primary");
  }
  return accountRow(row.name, row.covers, state, actions, `<span class="account-extra">${calendar}</span>${note}`);
}

// A service's own tool server, signed in to with the owner's account.
function accountServiceRow(row) {
  const server = row.server;
  let state;
  let actions;
  if (!server) {
    state = row.addable ? `<small>Asks ${esc(row.vendor)} for your consent</small>` : `<small>${esc(row.vendor)} does not let the office connect yet</small>`;
    actions = row.addable ? btn("Connect", `account-service:${row.id}`, "primary") : "";
  } else if (!server.has_credential || server.status === "NEEDS_SIGN_IN") {
    state = `<small>Added, waiting for you to sign in</small>`;
    actions = btn("Sign in", `account-sign-in:${server.id}`, "primary") + btn("Remove", `account-disconnect:${server.id}`, "danger");
  } else {
    const review = server.awaiting_review ? ` · <a href="#tools">review ${esc(server.awaiting_review)} tool${server.awaiting_review === 1 ? "" : "s"}</a>` : "";
    state = `<span class="account-on">Connected</span><small>${esc(server.callable)} tools allowed${review} · ${esc(usedBy(server))}</small>`;
    actions = btn("Projects…", `account-projects:${server.id}`) + btn("Disconnect", `account-disconnect:${server.id}`, "danger");
  }
  return accountRow(row.name, row.covers, state, actions);
}

function accountKeyRow(row) {
  const server = row.server;
  const state = server ? `<span class="account-on">Key saved</span><small>${esc(usedBy(server))}</small>` : `<small>Takes a key from ${esc(row.vendor)}</small>`;
  const actions = server ? btn("Projects…", `account-projects:${server.id}`) + btn("Remove", `account-disconnect:${server.id}`, "danger") : btn("Add key", `account-key:${row.id}`, "primary");
  return accountRow(row.name, row.covers, state, actions);
}

function accountModelRows() {
  const byProvider = new Map();
  for (const model of Accounts.models) {
    const key = model.provider_name || model.provider;
    const seen = byProvider.get(key) || { name: key, keyed: false, count: 0 };
    seen.keyed = seen.keyed || model.credential_configured;
    seen.count += 1;
    byProvider.set(key, seen);
  }
  return [...byProvider.values()]
    .map((p) =>
      accountRow(
        p.name,
        `Models for your agents · ${p.count} model${p.count === 1 ? "" : "s"}`,
        p.keyed ? `<span class="account-on">Key saved</span>` : `<small>No key</small>`,
        `<a class="button" href="#connections">Manage</a>`
      )
    )
    .join("");
}

function accountLogins() {
  return `<div class="card account-logins"><h2>Logins</h2>
    <p>The office's own browser keeps no logins. Every task opens it fresh, with no cookies from anywhere, and anything that would sign in, submit, send, pay or book stops and asks you first.</p>
    <p class="muted">The browser is ${Accounts.data?.browser_on ? "on" : "off"}. <a href="#settings">Change it in Limits and models →</a></p></div>`;
}

function accountSection(title, rows) {
  return rows ? `<section class="account-section"><h2>${esc(title)}</h2><ul class="account-list">${rows}</ul></section>` : "";
}

async function renderAccounts(content, param) {
  accountsReturned(param);
  const [data, models] = await Promise.all([api("/accounts"), api("/connections").catch(() => [])]);
  if (!data || !Array.isArray(data.connections)) {
    console.error("Invalid accounts response:", data);
    content.innerHTML = emptyCard("Accounts could not be read.", "Try again in a moment.", btn("Try again", "retry", "primary"));
    return;
  }
  Accounts.data = data;
  Accounts.models = Array.isArray(models) ? models : [];
  const tab = Accounts.tab;
  const own = data.connections.filter((row) => row.kind === "account").map(accountOwnRow).join("");
  const services = data.connections.filter((row) => row.kind === "mcp").map(accountServiceRow).join("");
  const keys = accountModelRows() + data.keys.map(accountKeyRow).join("");
  const tabs = `<div class="account-tabs" role="tablist">${ACCOUNT_TABS.map(
    ([id, label]) => `<button type="button" role="tab" aria-selected="${tab === id}" class="${tab === id ? "is-on" : ""}" data-action="account-tab:${id}">${esc(label)}</button>`
  ).join("")}</div>`;
  let body = "";
  if (tab === "all" || tab === "connections") body += accountSection("Your accounts", own) + accountSection("Services", services);
  if (tab === "all" || tab === "logins") body += accountLogins();
  if (tab === "all" || tab === "keys") body += accountSection("API keys", keys);
  content.innerHTML =
    heading("ACCOUNTS", "Connect the places you work.", "Each one asks the company for your consent on its own page. Agents use what you connect only in the projects you choose, and every tool call is checked and recorded.") +
    tabs +
    body;
}

function accountProjects(serverId) {
  const projects = Accounts.data?.projects || [];
  const server = [...(Accounts.data?.connections || []), ...(Accounts.data?.keys || [])].map((row) => row.server).find((s) => s?.id === serverId);
  const on = new Set(server?.projects || []);
  modal(
    "Which projects may use it?",
    projects.length
      ? `<p class="muted">Tasks and discussions in these projects can use its tools.</p>${projects
          .map((p) => `<label class="check"><input type="checkbox" data-account-grant="${esc(p.id)}" ${on.has(p.id) ? "checked" : ""}> ${esc(p.name)}</label>`)
          .join("")}`
      : "<p>Create a project first, then choose it here.</p>",
    `<div class="button-row">${btn("Done", "account-projects-done", "primary")}</div>`
  );
  document.querySelectorAll("[data-account-grant]").forEach((box) => {
    box.addEventListener("change", async () => {
      const project = box.dataset.accountGrant;
      const ok = await attempt(() => api(`/projects/${project}/mcp/${serverId}`, { method: box.checked ? "PUT" : "DELETE" }), box.checked ? "It may use it now." : "Taken away.");
      if (ok === undefined) box.checked = !box.checked;
    });
  });
}

async function accountSignIn(serverId) {
  const started = await attempt(() => api(`/mcp/servers/${serverId}/sign-in?back=accounts`, { method: "POST" }));
  if (started?.authorize_url) location.href = started.authorize_url;
}

function accountEntry(id) {
  return [...(Accounts.data?.connections || []), ...(Accounts.data?.keys || [])].find((row) => row.id === id);
}

Office.forms = Object.assign(Office.forms || {}, {
  "account-key-form": async (form) => {
    const id = form.querySelector("[name=entry]")?.value || "";
    const entry = accountEntry(id);
    const input = form.querySelector("[name=secret]");
    const secret = input?.value?.trim() || "";
    if (input) input.value = "";
    if (!entry || !secret) return;
    const added = await attempt(() => api("/mcp/servers", { method: "POST", body: { name: entry.name, url: entry.url, secret } }));
    if (!added) return;
    closeModal();
    toast(`${entry.name} is added. Review its tools, then choose the projects that may use them.`);
    rerender();
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "account-tab": (id) => {
    Accounts.tab = id;
    rerender();
  },
  "account-connect": async (key) => {
    const started = await attempt(() => api(`/accounts/${key}/connect`, { method: "POST", body: { origin: location.origin } }));
    if (started?.url) location.href = started.url;
  },
  "account-service": async (id) => {
    const entry = accountEntry(id);
    if (!entry) {
      console.error("Account not found:", id);
      return;
    }
    const added = await attempt(() => api("/mcp/servers", { method: "POST", body: { name: entry.name, url: entry.url } }));
    if (!added?.id) return;
    if (added.status === "NEEDS_SIGN_IN" || !added.has_credential) return accountSignIn(added.id);
    rerender();
  },
  "account-sign-in": (serverId) => accountSignIn(serverId),
  "account-key": (id) => {
    const entry = accountEntry(id);
    if (!entry) return;
    formModal(
      `Add ${entry.name}`,
      "account-key-form",
      `<input type="hidden" name="entry" value="${esc(entry.id)}">
       <p>${esc(entry.key_help)}</p>
       ${field("Key", "secret", { type: "password", attrs: 'maxlength="4096" autocomplete="off"' })}
       <p class="muted">Kept sealed on this computer and sent only to ${esc(entry.vendor)}.</p>`,
      "Add"
    );
  },
  "account-projects": (serverId) => accountProjects(serverId),
  "account-projects-done": () => {
    closeModal();
    rerender();
  },
  "account-disconnect": async (serverId) => {
    if (!window.confirm("Disconnect it? Its sign-in or key is deleted from this computer, and no project can use it until you connect it again.")) return;
    if ((await attempt(() => api(`/mcp/servers/${serverId}`, { method: "DELETE" }), "Disconnected.")) !== undefined) rerender();
  },
});

Office.screens.accounts = { title: "Accounts", render: renderAccounts, nav: "office" };

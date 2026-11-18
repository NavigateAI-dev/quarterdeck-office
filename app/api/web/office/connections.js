"use strict";

// What the office's Slack app should have beyond the first install: files
// and 💡 ideas.
const SLACK_IDEA_SCOPES = ["reactions:read", "channels:history", "groups:history"];
const SLACK_WANTED = ["files:write", "files:read", ...SLACK_IDEA_SCOPES];

// Connections: model providers, and the office's chat workspace (Slack).

const Conn = { tab: "models", connections: [], channels: null, providers: [], people: [], installation: null };
const EVENT_LABELS = {
  needs_approval: "A task needs my approval",
  needs_input: "A task needs my input",
  completed: "A task finished",
  failed: "A task failed",
  limit_warning: "A limit reaches 80% or 100%",
  ideas_friday: "Fridays at 16:00: ideas not planned yet",
  daily_digest: "Every day at 18:00: what finished, failed and waits, as counts",
};
const CONNECTION_STATUS = {
  MODEL_AVAILABLE: ["Ready", ""],
  CREDENTIAL_MISSING: ["Needs a key", "amber"],
  PROVIDER_TEST_PENDING: ["Not checked yet", "gray"],
  VERIFIED: ["Ready", ""],
  MODEL_UNAVAILABLE: ["Model not available", "clay"],
  CREDENTIAL_STORE_UNAVAILABLE: ["Key store unavailable", "clay"],
};

function describeTest(connection) {
  const test = connection.last_test;
  if (!test) return "Not checked yet";
  const usage = test.input_tokens === null || test.input_tokens === undefined ? "usage not reported" : `${test.input_tokens} in / ${test.output_tokens} out tokens`;
  return test.billable ? `Billable test · ${usage}` : "Free check";
}

function connectionsView() {
  const cards = Conn.connections
    .map((c) => {
      const [label, cls] = CONNECTION_STATUS[c.status] || [c.status.replace(/_/g, " ").toLowerCase(), "clay"];
      return `<div class="card"><div class="card-top"><span class="connector-icon">${esc(c.provider.charAt(0).toUpperCase())}</span>${tag(label, cls)}</div>
        <h3>${esc(c.display_name)}</h3><small>${esc(c.provider_name || c.provider)} · ${esc(c.model_id)}</small>
        <p>${esc(c.last_error || describeTest(c))}${c.last_checked_at ? ` · ${esc(when(c.last_checked_at))}` : ""}</p>
        <div class="detail-line"><span>Key</span><strong>${c.credential_configured ? "Stored encrypted on this computer" : needsKey(c) ? "Not stored" : "Not needed"}</strong></div>
        ${c.base_url ? `<div class="detail-line"><span>Address</span><strong class="wrap">${esc(c.base_url)}</strong></div>` : ""}
        <div class="button-row">${btn(c.credential_configured ? "Replace key" : "Add key", `conn-key:${c.id}`, c.credential_configured || !needsKey(c) ? "" : "primary")}${btn("Change model", `conn-models:${c.id}`)}${
          c.credential_configured || !needsKey(c) ? `${btn("Check (free)", `conn-check:${c.id}`)}${btn("Test (billable)", `conn-test:${c.id}`)}` : ""
        }${c.credential_configured ? btn("Remove key", `conn-unkey:${c.id}`, "danger") : ""}</div></div>`;
    })
    .join("");
  return `<div class="grid3">${cards || emptyCard("No model connections yet.", "Add an OpenAI or Anthropic model with your own API key, then assign it to agents.")}</div>
    <div class="note spaced">API keys are billed by your provider, stored encrypted on this computer, and never shown again or given to agents.</div>`;
}

// A message the platform never confirmed is neither sent again nor forgotten:
// the office asks the platform, and failing that, asks the owner.
function unknownDeliveries(deliveries) {
  const unknown = deliveries.filter((d) => d.status === "UNKNOWN" && !d.reconciled_at);
  if (!unknown.length) return "";
  return `<div class="attention"><span class="attention-icon">?</span><div>
      <strong>${unknown.length} message(s) with an unknown outcome</strong>
      <p>The platform never confirmed these. They will not be sent again until you or the platform say what happened.</p>
    </div>${btn("Ask the platform", "delivery-reconcile")}</div>
    <div class="card">${unknown
      .map(
        (d) => `<div class="setting-row"><div><h3>${esc(EVENT_LABELS[d.event] || d.event)}</h3><p class="muted">${esc((d.text || "").slice(0, 140))}</p></div>
          <div class="button-row tight">${btn("It arrived", `delivery-arrived:${d.id}`)}${btn("Send it again", `delivery-retry:${d.id}`, "primary")}</div></div>`
      )
      .join("")}</div>`;
}

function slackView() {
  const data = Conn.channels;
  const office = data.installations[0];
  const app = data.apps.slack || {};
  const people = Conn.people || [];
  const destinations = data.destinations.filter((d) => d.enabled && office && d.installation_id === office.id);
  const workspace = office
    ? `<div class="card"><div class="card-top"><span class="connector-icon">S</span>${tag("Connected")}</div><h3>Slack · ${esc(office.workspace_name)}</h3>
        <div class="detail-line"><span>Receives your messages</span><strong>${app.app_token_configured ? "Yes (Socket Mode)" : "No · add the app-level token"}</strong></div>
        <div class="detail-line"><span>You are paired</span><strong>${office.owner_external_id ? "Yes" : "Not yet"}</strong></div>
        <div class="detail-line" data-slack-files><span>Answers, files and buttons in threads</span><strong>${(office.scopes || []).includes("files:write") ? "Yes" : "Needs a Slack app update"}</strong></div>
        <div class="detail-line" data-slack-reads><span>Reads pictures and files you share</span><strong>${(office.scopes || []).includes("files:read") ? "Yes" : "Needs a Slack app update"}</strong></div>
        <div class="detail-line" data-slack-ideas><span>Keeps a message you mark 💡 as an idea</span><strong>${SLACK_IDEA_SCOPES.every((scope) => (office.scopes || []).includes(scope)) ? "Yes" : "Needs a Slack app update"}</strong></div>
        ${SLACK_WANTED.every((scope) => (office.scopes || []).includes(scope)) ? "" : `<p class="muted">Work you ask for in Slack is answered in its thread. To get documents as files, buttons (Start, Stop, Rework), to have it read the screenshots and files you share, and to keep a message you mark 💡 as an idea, your Slack app needs a few more permissions. “idea: …” works without them. ${btn("How to update it", `slack-update:${office.id}`)}</p>`}
        <div class="button-row">${btn(office.owner_external_id ? "Re-pair" : "Pair Slack", `slack-pair:${office.id}`, office.owner_external_id ? "" : "primary")}${btn("App credentials", "slack-app")}${btn("Disconnect", `slack-disconnect:${office.id}`, "danger")}</div></div>`
    : `<div class="card"><div class="card-top"><span class="connector-icon">S</span>${tag("Not connected", "gray")}</div><h3>Slack</h3><p>Get notified in a channel, and message the office bot from Slack.</p>
        <div class="button-row">${btn("Connect workspace", "slack-connect", "primary")}${btn("App credentials", "slack-app")}</div></div>`;
  const peopleCard = office
    ? `<div class="card"><h3>Who may use the bots</h3>
        <p class="muted">A member can ask and can give work, on your keys and inside your budgets. Approvals stay yours. Anyone not listed is answered nothing at all.</p>
        ${people
          .map(
            (row) =>
              `<div class="detail-line"><span><code>${esc(row.user_id)}</code></span><span>${
                row.role === "owner"
                  ? tag("you")
                  : `${tag("member", "gray")} ${btn("Remove", `member-remove:${esc(row.user_id)}`, "danger")}`
              }</span></div>`
          )
          .join("")}
        <form class="compose" id="member-form">
          <input name="user_id" maxlength="64" placeholder="Slack member ID, such as U01ABCDE" aria-label="Slack member ID" required>
          <button class="primary">Add</button>
        </form>
        <p class="muted">Find it in Slack: open their profile, then More, then Copy member ID.</p>
      </div>`
    : "";
  const channelCard = office
    ? `<div class="card"><h3>Notifications</h3>${
        destinations.length
          ? destinations.map((d) => `<div class="detail-line"><span>${esc(d.name)}<br><small>${esc(d.events.map((e) => EVENT_LABELS[e] || e).join(", "))}</small></span><span>${btn("Test", `dest-test:${d.id}`)} ${btn("Remove", `dest-remove:${d.id}`, "danger")}</span></div>`).join("")
          : `<p class="muted">No channel chosen yet.</p>`
      }<div class="button-row">${btn("＋ Add channel", "dest-add", "primary")}</div></div>`
    : "";
  const planned = ["Microsoft Teams", "Discord"]
    .map((name) => `<div class="card"><div class="card-top"><span class="connector-icon">${name.charAt(0)}</span>${tag("Planned", "gray")}</div><h3>${name}</h3><p>Uses the same channel layer as Slack when added.</p></div>`)
    .join("");
  return `<div class="grid3">${workspace}${peopleCard}${channelCard}${planned}</div>
    <div class="note spaced">Each agent can also be its own Slack bot: Agents → Slack bot. Chat never approves anything, and work starts only after you reply "yes".</div>
    ${unknownDeliveries(data.deliveries || [])}
    ${data.deliveries?.length ? `<h2 class="spaced">Recent messages</h2><div class="card">${data.deliveries.slice(0, 8).map((d) => `<div class="detail-line"><span>${esc(EVENT_LABELS[d.event] || d.event)}${d.agent_profile_id ? " · agent bot" : ""}</span><strong>${esc(d.status.toLowerCase())}${d.error ? ` (${esc(d.error)})` : ""}</strong></div>`).join("")}</div>` : ""}`;
}

function priceText(row) {
  if (row.local) return "runs on this computer";
  if (!row.known) return "price not published";
  const inPrice = row.input_per_million;
  const outPrice = row.output_per_million;
  if (inPrice === null && outPrice === null) return "price not published";
  return `$${inPrice ?? "?"} in · $${outPrice ?? "?"} out per million tokens`;
}

function windowText(row) {
  const parts = [];
  if (row.context_window) parts.push(`${Math.round(row.context_window / 1000)}k context`);
  if (row.max_output_tokens) parts.push(`${Math.round(row.max_output_tokens / 1000)}k output`);
  if (row.capabilities?.length) parts.push(row.capabilities.join(", "));
  return parts.join(" · ");
}

function modelRows(details, chatOnly, search) {
  const wanted = details
    .filter((row) => (chatOnly ? row.chat : true))
    .filter((row) => !search || row.model.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => (a.known === b.known ? a.model.localeCompare(b.model) : a.known ? -1 : 1));
  if (!wanted.length) return { html: `<p class="muted">No model matches.</p>`, count: 0 };
  return {
    count: wanted.length,
    html: wanted
    .map(
      (row) => `<label class="model-row"><input type="radio" name="model_id" value="${esc(row.model)}" ${row.model === Conn.currentModel ? "checked" : ""}>
        <span><strong>${esc(row.model)}</strong><br><small class="muted">${esc(priceText(row))}${windowText(row) ? ` · ${esc(windowText(row))}` : ""}</small></span></label>`
      )
      .join(""),
  };
}

function renderModelList() {
  const list = $("#model-list");
  if (!list) return;
  const chatOnly = $("#chat-only")?.checked ?? true;
  const search = $("#model-search")?.value || "";
  const shown = modelRows(Conn.details || [], chatOnly, search);
  list.innerHTML = shown.html;
  showChosen(shown.count);
  const note = $("#model-note");
  if (note) {
    note.textContent =
      Conn.source === "catalogue"
        ? "These are the models this provider publishes, with prices from the public model price list (refreshed daily). Add your key and check to see exactly what your account may use."
        : "Listed by the provider for your key. Prices come from the public model price list, refreshed daily.";
  }
}

function modelBrowser(listing) {
  Conn.details = listing.details || [];
  Conn.pricesKnown = listing.prices_known;
  Conn.source = listing.source;
  return `<div class="model-browser">
      <label class="field" for="model-search">Model</label>
      <input id="model-search" type="search" autocomplete="off" value="${esc(Conn.currentModel || "")}"
             placeholder="Search, or type a model name" aria-label="Model">
      <label class="check"><input type="checkbox" id="chat-only" checked> Chat models only (${listing.chat_models.length} of ${listing.models.length})</label>
      <div id="model-list" class="model-list"></div>
      <p id="model-chosen" class="muted hint"></p>
      <p id="model-note" class="muted hint"></p>
    </div>`;
}

function chosenModel() {
  const picked = document.querySelector('#model-list input[name="model_id"]:checked');
  return picked ? picked.value : $("#model-search")?.value.trim() || "";
}

function showChosen(matches) {
  const line = $("#model-chosen");
  if (!line) return;
  const picked = document.querySelector('#model-list input[name="model_id"]:checked');
  const typed = $("#model-search")?.value.trim() || "";
  const exact = (Conn.details || []).some((row) => row.model === typed);
  if (picked) {
    line.textContent = `Using ${picked.value}.`;
  } else if (!typed) {
    line.textContent = "Pick one below, or type a name.";
  } else if (exact) {
    line.textContent = `Using ${typed}.`;
  } else if (matches) {
    line.textContent = `Pick one of the ${matches} below, or type the exact name.`;
  } else {
    line.textContent = `No model matches “${typed}”. It will be used exactly as typed.`;
  }
}

function wireModelBrowser(onPick) {
  renderModelList();
  $("#model-search")?.addEventListener("input", () => {
    // Typing is a new intention: an earlier pick no longer applies.
    const picked = document.querySelector('#model-list input[name="model_id"]:checked');
    if (picked) picked.checked = false;
    renderModelList();
  });
  $("#chat-only")?.addEventListener("change", renderModelList);
  $("#model-list")?.addEventListener("change", (event) => {
    if (event.target.name !== "model_id") return;
    showChosen();
    if (onPick) onPick(event.target.value);
  });
}

// The model list needs no key: the office knows what each provider
// publishes. A key only narrows it to what that account may actually use.
async function browseModels(withKey = false) {
  const provider = $("#f-provider")?.value;
  const holder = $("#model-browser");
  if (!provider || !holder) return;
  const body = {};
  const key = withKey ? $("#f-secret")?.value.trim() || "" : "";
  const address = $("#f-base_url")?.value.trim() || "";
  if (key) body.api_key = key;
  if (address) body.base_url = address;
  // Keep what the owner already typed or picked when the list is replaced.
  Conn.currentModel = ($("#model-search") ? chosenModel() : Conn.currentModel) || "";
  holder.innerHTML = `<p class="muted">${key ? "Asking the provider…" : "Loading models…"}</p>`;
  let listing;
  try {
    listing = await api(`/providers/${provider}/models`, { method: "POST", body });
  } catch (error) {
    if (error instanceof SignedOut) return;
    holder.innerHTML = `<p class="muted">${esc(
      error.message === "credential_missing"
        ? "This provider only lists its models to a key. Add the key, then check."
        : explain(error)
    )}</p>`;
    return;
  }
  holder.innerHTML = modelBrowser(listing);
  wireModelBrowser((model) => {
    const name = $("#f-display_name");
    if (name && !name.value) name.value = model;
  });
}

function needsKey(connection) {
  const preset = Conn.providers.find((p) => p.id === connection.provider);
  return preset ? preset.needs_key : true;
}

async function renderConnections(content) {
  const [connections, channels, deliveries, providers] = await Promise.all([
    api("/connections"),
    api("/channels"),
    api("/channels/deliveries").catch(() => []),
    api("/providers").catch(() => []),
  ]);
  Conn.providers = providers;
  Conn.connections = connections;
  Conn.channels = { ...channels, deliveries };
  // Who may use the bots is fetched here, where the screen loads its data: the
  // view that renders it is synchronous.
  const installation = (channels.installations || [])[0];
  Conn.installation = installation ? installation.id : null;
  Conn.people = Conn.installation
    ? await api(`/channels/installations/${Conn.installation}/members`).catch(() => [])
    : [];
  const tabs = [
    ["models", `Models (${connections.length})`],
    ["chat", "Chat channels"],
  ];
  content.innerHTML =
    heading("CONNECTIONS", "Bring your office into your world.", "Model providers and chat channels, with clear boundaries.", Conn.tab === "models" ? btn("＋ Add model", "conn-add", "primary") : "") +
    `<div class="subnav" role="tablist">${tabs.map(([id, label]) => btn(esc(label), `conn-tab:${id}`, Conn.tab === id ? "active" : "", 'role="tab"')).join("")}</div>` +
    (Conn.tab === "models" ? connectionsView() : slackView());
}

async function openAddDestination() {
  const office = Conn.channels?.installations[0];
  if (!office) return;
  const channels = await attempt(() => api(`/channels/installations/${office.id}/available`));
  if (!channels) return;
  formModal(
    "Notify a channel",
    "destination-form",
    `${field("Channel", "channel", { type: "select", choices: channels.map((c) => [c.can_post ? `${c.external_id}|${c.name}` : "", c.can_post ? c.name : `${c.name} (invite the bot first)`]) })}
     <fieldset class="skill-picks"><legend class="field">When</legend>${Object.entries(EVENT_LABELS)
       .map(([id, label]) => `<label class="check"><input type="checkbox" name="events" value="${id}" ${id !== "completed" ? "checked" : ""}> ${esc(label)}</label>`)
       .join("")}</fieldset>
     <p class="muted">Messages carry the task's status and where to act, never its content.</p>`,
    "Add channel"
  );
}

Office.screens.connections = { title: "Connections", render: renderConnections };

Office.actions = Object.assign(Office.actions || {}, {
  "member-remove": async (userId) => {
    if (!Conn.installation) return;
    if (!window.confirm(`Remove ${userId}? They will be answered nothing at all.`)) return;
    const gone = await attempt(
      () => api(`/channels/installations/${Conn.installation}/members/${encodeURIComponent(userId)}`, { method: "DELETE" }),
      "Removed."
    );
    if (gone !== undefined) rerender();
  },
  "conn-tab": (tab) => {
    Conn.tab = tab;
    rerender();
  },
  // Opened from Connections or from Setup, so it loads what it needs itself
  // rather than relying on a screen having been visited first.
  "conn-add": async () => {
    if (!Conn.providers.length) {
      const listing = await attempt(() => api("/providers"));
      if (!listing) return;
      Conn.providers = listing;
    }
    const providers = Conn.providers;
    formModal(
      "Add a model",
      "connection-form",
      `${field("Provider", "provider", { type: "select", choices: providers.map((p) => [p.id, p.name]) })}
       <p id="provider-about" class="muted hint"></p>
       ${field("Name", "display_name", { attrs: 'maxlength="255" placeholder="Work models"' })}
       <div id="base-url-row">${field("Service address", "base_url", { required: false, attrs: 'maxlength="1000" placeholder="https://…/v1"', hint: "Only for a service of your own; the catalogue fills this in otherwise." })}</div>
       <div id="key-row">${field("API key", "secret", { type: "password", attrs: 'autocomplete="off"', required: false, hint: "Stored encrypted on this computer. You can add it later." })}</div>
       <div class="button-row tight">${btn("Check with my key", "conn-browse")}</div>
       <div id="model-browser"></div>`,
      "Add model"
    );
    const select = $("#f-provider");
    const sync = () => {
      const preset = providers.find((p) => p.id === select?.value);
      if (!preset) return;
      browseModels();
      const about = $("#provider-about");
      if (about) about.textContent = `${preset.about}${preset.base_url ? ` Address: ${preset.base_url}` : ""}`;
      const keyRow = $("#key-row");
      if (keyRow) keyRow.hidden = !preset.needs_key;
      const urlRow = $("#base-url-row");
      if (urlRow) urlRow.hidden = !preset.needs_base_url;
      const url = $("#f-base_url");
      if (url) url.required = preset.needs_base_url;
    };
    select?.addEventListener("change", sync);
    sync();
  },
  "conn-browse": () => browseModels(true),
  "conn-models": async (id) => {
    if (!Conn.connections.length) Conn.connections = (await attempt(() => api("/connections"))) || [];
    const connection = Conn.connections.find((c) => c.id === id);
    const listing = await attempt(() => api(`/connections/${id}/models`));
    if (!listing) return;
    Conn.keyFor = id;
    Conn.currentModel = connection?.model_id || "";
    formModal(
      `Model for ${connection?.display_name || "this connection"}`,
      "model-form",
      modelBrowser(listing),
      "Use this model"
    );
    wireModelBrowser();
  },
  "conn-key": (id) => {
    Conn.keyFor = id;
    formModal("Store API key", "key-form", field("API key", "secret", { type: "password", attrs: 'autocomplete="off"', hint: "Replaces any stored key. Never shown again." }), "Store key");
  },
  "conn-check": async (id) => {
    const result = await attempt(() => api(`/connections/${id}/test`, { method: "POST", body: { billable: false } }));
    if (result) toast(result.status === "MODEL_AVAILABLE" ? "The provider confirmed the key and model." : `Check result: ${result.last_error || result.status}`, result.status !== "MODEL_AVAILABLE");
    rerender();
  },
  "conn-test": async (id) => {
    if (!window.confirm("This sends one small request that your provider bills. Continue?")) return;
    await attempt(() => api(`/connections/${id}/test`, { method: "POST", body: { billable: true } }), "Test request done.");
    rerender();
  },
  "conn-unkey": async (id) => {
    if (!window.confirm("Remove the stored key? Agents using this model stop working until a key is added.")) return;
    if ((await attempt(() => api(`/connections/${id}/credential`, { method: "DELETE" }), "Key removed.")) !== undefined) rerender();
  },
  "slack-connect": () =>
    formModal(
      "Connect your Slack workspace",
      "slack-token-form",
      `<p>Create the app from <code>docs/slack-app-manifest.yaml</code> at api.slack.com/apps, install it to your workspace, and paste its Bot User OAuth Token.</p>${field("Bot token (xoxb-…)", "bot_token", { type: "password", attrs: 'autocomplete="off"' })}`,
      "Connect"
    ),
  "slack-app": async () => {
    if (!Conn.channels) Conn.channels = (await attempt(() => api("/channels"))) || { apps: {} };
    formModal(
      "Slack app credentials",
      "slack-app-form",
      `<p class="muted">Leave a field empty to keep what is stored. Values are stored encrypted and never shown again.</p>
       ${field("App-level token (xapp-…, scope connections:write)", "app_token", { type: "password", required: false, attrs: 'autocomplete="off"', hint: "Lets you message the office from Slack." })}
       ${field("Signing secret", "signing_secret", { type: "password", required: false, attrs: 'autocomplete="off"' })}
       <details><summary>OAuth install (only with an HTTPS address)</summary>
       ${field("Client ID", "client_id", { required: false, value: Conn.channels?.apps.slack?.client_id || "" })}
       ${field("Client secret", "client_secret", { type: "password", required: false, attrs: 'autocomplete="off"' })}
       ${field("Redirect URL", "redirect_uri", { required: false, value: Conn.channels?.apps.slack?.redirect_uri || "" })}
       </details>`,
      "Save"
    );
  },
  "slack-pair": async (id) => {
    const pairing = await attempt(() => api(`/channels/installations/${id}/pairing`, { method: "POST" }));
    if (!pairing) return;
    modal("Pair Slack", `<p>Send this code to the bot as a direct message in Slack within ${Math.round(pairing.expires_in_seconds / 60)} minutes:</p><div class="approval-preview"><h2 class="code">${esc(pairing.code)}</h2></div><p class="muted">Pairing proves which Slack user you are. Every office bot in this workspace then listens to you only.</p>`);
  },
  // The permission and the switch an older Slack app is missing.
  "slack-update": (id) => {
    modal(
      "Update your Slack app",
      `<ol class="steps">
        <li>Open <a href="https://api.slack.com/apps" target="_blank" rel="noopener noreferrer">api.slack.com/apps</a> and choose the office's app.</li>
        <li><strong>OAuth &amp; Permissions</strong> → Bot Token Scopes → <strong>Add an OAuth Scope</strong> for each one missing: ${SLACK_WANTED.map((scope) => `<code>${scope}</code>`).join(", ")}. The last three let a 💡 keep a message as an idea; the office reads only the message you marked.</li>
        <li><strong>Event Subscriptions</strong> → Subscribe to bot events → add <code>reaction_added</code> (for 💡).</li>
        <li><strong>Interactivity &amp; Shortcuts</strong> → switch it <strong>On</strong>. With Socket Mode no request URL is needed; save.</li>
        <li>Slack then asks you to <strong>reinstall the app</strong> to your workspace. Do so; the bot token stays the same.</li>
        <li>Come back here and press <strong>Check again</strong>.</li>
      </ol>
      <p class="muted">The office cannot see the Interactivity switch. If a button in Slack shows a warning sign, that switch is still off.</p>`,
      `<div class="button-row">${btn("Close", "close")}${btn("Check again", `slack-check:${id}`, "primary")}</div>`
    );
  },
  "slack-check": async (id) => {
    const checked = await attempt(() => api(`/channels/installations/${id}/check`, { method: "POST" }));
    if (!checked) return;
    closeModal();
    const missing = SLACK_WANTED.filter((scope) => !(checked.scopes || []).includes(scope));
    toast(missing.length ? `Still without ${missing.join(", ")}. Was the app reinstalled?` : "Updated. It sends files, reads what you share, and keeps 💡 messages as ideas.", Boolean(missing.length));
    rerender();
  },
  "slack-disconnect": async (id) => {
    if (!window.confirm("Disconnect this workspace? Its stored token is deleted and notifications stop.")) return;
    if ((await attempt(() => api(`/channels/installations/${id}`, { method: "DELETE" }), "Disconnected.")) !== undefined) rerender();
  },
  "dest-add": openAddDestination,
  "delivery-reconcile": async () => {
    const settled = await attempt(() => api("/channels/deliveries/reconcile", { method: "POST" }));
    if (!settled) return;
    toast(
      settled.delivered || settled.not_delivered
        ? `${settled.delivered} had arrived, ${settled.not_delivered} had not and will be sent.`
        : "The platform could not say. Decide for each message below."
    );
    rerender();
  },
  "delivery-arrived": async (id) => {
    if ((await attempt(() => api(`/channels/deliveries/${id}/resolve`, { method: "POST", body: { arrived: true } }), "Recorded as delivered.")) !== undefined) rerender();
  },
  "delivery-retry": async (id) => {
    if ((await attempt(() => api(`/channels/deliveries/${id}/resolve`, { method: "POST", body: { arrived: false } }), "It will be sent once.")) !== undefined) rerender();
  },
  "dest-test": async (id) => {
    if (!window.confirm("Post a test message to this channel?")) return;
    await attempt(() => api(`/channels/destinations/${id}/test`, { method: "POST", body: { confirm: true } }), "Test message sent.");
  },
  "dest-remove": async (id) => {
    if ((await attempt(() => api(`/channels/destinations/${id}`, { method: "DELETE" }), "Channel removed.")) !== undefined) rerender();
  },
});

const PAIRING_ERRORS = { app_token_missing: "Add the app-level token under App credentials first, so the office can receive your Slack messages." };

Office.forms = Object.assign(Office.forms || {}, {
  "member-form": async (form) => {
    const userId = String(new FormData(form).get("user_id") || "").trim();
    if (!userId || !Conn.installation) return;
    const added = await attempt(
      () => api(`/channels/installations/${Conn.installation}/members`, { method: "POST", body: { user_id: userId } }),
      "Added. They can talk to the office in Slack now."
    );
    if (added !== undefined) rerender();
  },
  "model-form": async () => {
    const model_id = chosenModel();
    if (!model_id) {
      toast("Choose a model first.", true);
      return;
    }
    if ((await attempt(() => api(`/connections/${Conn.keyFor}`, { method: "PATCH", body: { model_id } }), `Now using ${model_id}.`)) === undefined) return;
    closeModal();
    await attempt(() => api(`/connections/${Conn.keyFor}/test`, { method: "POST", body: { billable: false } }));
    rerender();
  },
  "connection-form": async (form) => {
    const data = new FormData(form);
    const model_id = chosenModel();
    if (!model_id) {
      toast("Pick a model, or type its name.", true);
      return;
    }
    const body = {
      provider: data.get("provider"),
      display_name: String(data.get("display_name")).trim(),
      model_id,
    };
    const address = String(data.get("base_url") || "").trim();
    if (address) body.base_url = address;
    const created = await attempt(() => api("/connections", { method: "POST", body }));
    if (!created) return;
    const secret = String(data.get("secret") || "");
    form.reset();
    if (secret) {
      await attempt(() => api(`/connections/${created.id}/credential`, { method: "PUT", body: { secret } }));
    }
    if (secret || !needsKey(created)) {
      await attempt(() => api(`/connections/${created.id}/test`, { method: "POST", body: { billable: false } }));
      toast("Model added and checked with the provider.");
    } else {
      toast("Model added. Add its key next.");
    }
    closeModal();
    rerender();
  },
  "key-form": async (form) => {
    const secret = String(new FormData(form).get("secret") || "");
    form.reset();
    if ((await attempt(() => api(`/connections/${Conn.keyFor}/credential`, { method: "PUT", body: { secret } }), "Key stored. Checking it with the provider…")) === undefined) return;
    closeModal();
    await attempt(() => api(`/connections/${Conn.keyFor}/test`, { method: "POST", body: { billable: false } }));
    rerender();
  },
  "slack-token-form": async (form) => {
    const token = String(new FormData(form).get("bot_token") || "").trim();
    form.reset();
    if ((await attempt(() => api("/channels/slack/installations", { method: "POST", body: { bot_token: token } }), "Workspace connected. Add a channel next.")) === undefined) return;
    closeModal();
    Conn.tab = "chat";
    rerender();
  },
  "slack-app-form": async (form) => {
    const data = new FormData(form);
    const body = {};
    for (const key of ["app_token", "signing_secret", "client_id", "client_secret", "redirect_uri"]) {
      const value = String(data.get(key) || "").trim();
      if (value) body[key] = value;
    }
    form.reset();
    if (!Object.keys(body).length) return closeModal();
    if ((await attempt(() => api("/channels/slack/app", { method: "PUT", body }), "Saved. The office connects within 30 seconds.")) === undefined) return;
    closeModal();
    rerender();
  },
  "destination-form": async (form) => {
    const data = new FormData(form);
    const [external_id, name] = String(data.get("channel") || "|").split("|");
    const events = data.getAll("events");
    const office = Conn.channels?.installations[0];
    if (!external_id || !events.length || !office) {
      toast("Choose a channel the bot is in, and at least one event.", true);
      return;
    }
    if ((await attempt(() => api(`/channels/installations/${office.id}/destinations`, { method: "POST", body: { external_id, name, events } }), `Notifications will go to ${name}.`)) === undefined) return;
    closeModal();
    rerender();
  },
});

ERRORS.app_token_missing = PAIRING_ERRORS.app_token_missing;
ERRORS.base_url_required = "That service needs its address (a URL ending in /v1).";
ERRORS.provider_unsupported = "The office does not know that provider.";
ERRORS.credential_missing = "Add the API key first.";
ERRORS.redirect_must_be_https = "OAuth needs an HTTPS redirect URL. For your own workspace, paste the bot token instead.";

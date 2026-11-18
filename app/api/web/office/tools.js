"use strict";

// Tools: servers that offer the office capabilities it does not hard-code, what
// the owner decided about each tool, and which projects may use them.

const Tools = { servers: [], projects: [], grants: {}, reviewing: null, catalog: null };

// What SPEC section 18 says of each action a tool can be tagged with. Only the
// actions it holds back are offered: tagging a tool "searches the web" changes
// nothing, so asking would only add noise. Must match app/policy.py.
const SECTION_18 = {
  send_external_email: ["Sends email outside the office", "deny"],
  delete_external_data: ["Deletes data outside the office", "deny"],
  spend_money: ["Spends money", "deny"],
  deploy_production: ["Deploys to production", "deny"],
  merge_pull_request: ["Merges a pull request", "deny"],
  modify_code: ["Changes code", "approval"],
  create_pull_request: ["Opens a pull request", "approval"],
  create_decision_record: ["Records a decision", "approval"],
  modify_strategy_record: ["Changes a strategy record", "approval"],
  merge_workspace_branch: ["Merges an office branch in one of your folders", "approval"],
};

const STANCE_LABEL = {
  allowed: ["Allowed", ""],
  needs_approval: ["Asks you first", "amber"],
  refused: ["Refused", "gray"],
  unreviewed: ["Not reviewed", "amber"],
  changed_since_review: ["Changed since you reviewed it", "amber"],
  denied_by_policy: ["Never allowed (section 18)", "clay"],
};

Object.assign(ERRORS, {
  mcp_name_taken: "A server already has that name. Choose another.",
  mcp_non_public_address: "That address is on this computer or a private network. The office only talks to public HTTPS servers.",
  mcp_host_unresolvable: "That server's name could not be found. Check the address.",
  mcp_missing_host: "That address has no server name in it.",
  mcp_scheme_not_allowed: "Only HTTPS addresses are accepted.",
  mcp_host_not_allowed: "That server's address is not on the office's host allow-list (FETCH_ALLOW_HOSTS).",
  mcp_unreachable: "The server did not answer. Check the address and try again.",
  mcp_unauthorised: "The server refused the key. Check it, remove the server and add it again with the right one.",
  mcp_server_error: "The server answered with an error.",
  mcp_not_json: "That address answered, but not as an MCP server.",
  mcp_malformed_tools: "The server's list of tools could not be read.",
  mcp_response_too_large: "The server's answer was too large to accept.",
  mcp_empty_stream: "The server answered with nothing.",
  secret_not_found: "The key for this server is missing. Remove the server and add it again.",
  mcp_sign_in_required: "The server asks you to sign in with your account.",
  mcp_sign_in_unsupported: "The server asks for sign-in but does not say how. It cannot be signed in to from the office.",
  mcp_registration_unsupported: "The vendor does not let the office register itself for sign-in. It cannot be signed in to from here.",
  mcp_registration_refused: "The vendor refused to register the office for sign-in.",
  mcp_pkce_unsupported: "The vendor's sign-in lacks a protection the office requires (PKCE S256).",
  mcp_sign_in_unreachable: "The vendor's sign-in service did not answer, or answered with something unreadable. Try again later.",
});

function stanceTag(stance) {
  const [text, cls] = STANCE_LABEL[stance] || [stance, "gray"];
  return tag(text, cls);
}

// The vendor's page sends the owner back to #tools/signed-in/<id> or
// #tools/sign-in-failed. Say what happened once, then drop it from the address.
function sayHowSignInWent(param) {
  if (!param) return;
  if (param.startsWith("signed-in/")) toast("Signed in. The server's tools are listed for your review.");
  else if (param.startsWith("sign-in-failed")) toast("Sign-in did not finish. Try again from the server's card.", true);
  else return;
  history.replaceState(null, "", "#tools");
  Office.param = "";
}

async function renderTools(content, param) {
  sayHowSignInWent(param);
  const [servers, projects] = await Promise.all([api("/mcp/servers"), api("/projects")]);
  if (!Array.isArray(servers) || !Array.isArray(projects)) {
    console.error("Invalid tools response:", { servers, projects });
    throw new Error("could not load tools");
  }
  Tools.servers = servers;
  Tools.projects = projects.filter((p) => p.status === "ACTIVE");
  const granted = await Promise.all(Tools.projects.map((p) => api(`/projects/${p.id}/mcp`).catch(() => ({ servers: [] }))));
  Tools.grants = Object.fromEntries(Tools.projects.map((p, i) => [p.id, new Set((granted[i]?.servers || []).map((s) => s.id))]));
  const card = (s) => {
    const reached = s.status === "REACHABLE";
    const waiting = s.status === "NEEDS_SIGN_IN";
    const paidCount = s.tools.filter((t) => t.price_usd).length;
    const status = reached
      ? tag(`${s.tools.length} tools`)
      : waiting
        ? tag("Needs sign-in", "amber")
        : s.status === "UNREACHABLE"
          ? tag("Did not answer", "clay")
          : tag("Not asked yet", "gray");
    const key = s.signed_in ? "Signed in with your account" : s.has_credential ? "Stored, never shown" : "None";
    const signIn = waiting || s.signed_in ? btn(waiting ? "Sign in" : "Sign in again", `mcp-sign-in:${s.id}`, waiting ? "primary" : "") : "";
    const access = Tools.projects.filter((p) => Tools.grants[p.id]?.has(s.id)).map((p) => p.name);
    return `<div class="card"><div class="card-top"><span class="connector-icon">⚒</span>${status}</div>
      <h2>${esc(s.name)}</h2><p>${esc(s.url)}${s.detail ? ` · ${esc(explain(new Error(`mcp_${s.detail}`)))}` : ""}</p>
      <div class="detail-line"><span>Usable by agents</span><strong>${esc(s.callable)} of ${esc(s.tools.length)}</strong></div>
      <div class="detail-line"><span>Waiting for your review</span><strong>${esc(s.awaiting_review)}</strong></div>
      ${paidCount ? `<div class="detail-line"><span>Paid tools</span><strong>${esc(paidCount)} of ${esc(s.tools.length)}</strong></div>` : ""}
      <div class="detail-line"><span>Key</span><strong>${esc(key)}</strong></div>
      <div class="detail-line"><span>Projects</span><strong>${esc(access.join(", ") || "None yet")}</strong></div>
      ${s.no_project_discussions ? `<div class="detail-line"><span>Discussions with no project</span><strong>Allowed</strong></div>` : ""}
      <div class="button-row">${signIn}${btn(s.awaiting_review ? `Review ${s.awaiting_review} tools` : "Review tools", `mcp-review:${s.id}`, s.awaiting_review ? "primary" : "")}${btn(
        "Manage access",
        `mcp-access:${s.id}`,
        s.awaiting_review || waiting ? "" : "primary"
      )}${btn("Ask again", `mcp-refresh:${s.id}`)}${btn("Remove", `mcp-remove:${s.id}`, "danger")}</div></div>`;
  };
  content.innerHTML =
    heading(
      "TOOLS",
      "Tools from servers you trust.",
      "Give your agents a capability the office does not have, one reviewed tool at a time.",
      `<div class="button-row">${btn("Browse the catalog", "mcp-catalog")}${btn("＋ Add server", "mcp-add", "primary")}</div>`
    ) +
    (servers.length
      ? `<div class="grid3">${servers.map(card).join("")}</div>`
      : emptyCard(
          "No tool servers yet.",
          "Pick one from the catalog, or add any MCP server by its HTTPS address. Nothing it offers can be used until you have reviewed it.",
          `<div class="button-row">${btn("Browse the catalog", "mcp-catalog", "primary")}${btn("＋ Add server", "mcp-add")}</div>`
        )) +
    `<div class="note spaced">The office talks to a server only over HTTPS, and only at an address on its host allow-list. A tool is usable once you allow it and grant its server to a project. The researcher may use it to find evidence; the critic only uses tools you allowed outright, to check a claim. A tool that asks you first pauses the task until you say yes. A tool's own claims, such as "read-only", are shown but decide nothing. Section 18 of the office's rules outranks your review: a tool you say sends email, deletes outside data, spends money, deploys or merges is never used.</div>`;
}

// `preset` fills the form from a catalog entry; the owner can still change it.
function openAddServer(preset) {
  const entry = preset && typeof preset === "object" ? preset : null;
  const keyHint = entry?.sign_in === "key" ? `${entry.key_help} ` : "";
  const account = entry?.sign_in === "account";
  formModal(
    entry ? `Add ${entry.name}` : "Add a tool server",
    "mcp-server-form",
    `${entry?.note ? `<p class="note">${esc(entry.note)}</p>` : ""}
     ${field("Name", "name", { value: entry?.name || "", attrs: 'maxlength="128" placeholder="Filings"' })}
     ${field("Address", "url", { type: "url", value: entry?.url || "", attrs: 'maxlength="2048" pattern="https://.*" placeholder="https://mcp.example.com/mcp"', hint: "The server's Streamable HTTP address. HTTPS only." })}
     ${
       account
         ? `<p class="muted">This server signs in with your ${esc(entry.vendor)} account. Once it is added, its card asks you to sign in on ${esc(entry.vendor)}'s own page; the office never sees your password.</p>`
         : field(entry?.sign_in === "key" ? "Key" : "Key (optional)", "secret", {
             type: "password",
             required: entry?.sign_in === "key",
             attrs: 'maxlength="4096" autocomplete="off"',
             hint: `${keyHint}Stored encrypted in this office and sent only to this server. It is never shown again. A server that signs in with an account needs none: sign in from its card.`,
           })
     }`,
    "Add and ask what it offers"
  );
}

// ---- the catalog ----

function catalogEntry(entry) {
  const badges = [tag(entry.sign_in_label, entry.sign_in === "account" ? "gray" : entry.sign_in === "key" ? "amber" : "")];
  if (entry.relay) badges.push(tag("Relay", "clay"));
  const action = entry.added
    ? tag("Added")
    : entry.addable
      ? btn("Add", `mcp-catalog-add:${entry.id}`, "primary")
      : `<span class="muted">${esc(entry.vendor)} does not let the office register itself for sign-in, so it cannot be added yet.</span>`;
  return `<li class="catalog-entry">
    <div class="catalog-head"><strong>${esc(entry.name)}</strong><span class="muted">${esc(entry.vendor)}</span>${badges.join("")}</div>
    <p>${esc(entry.what)}</p>
    ${entry.note ? `<p class="muted">${esc(entry.note)}</p>` : ""}
    <div class="catalog-foot"><a href="${esc(entry.docs)}" target="_blank" rel="noopener noreferrer">Vendor's documentation ↗</a>${action}</div>
  </li>`;
}

async function openCatalog() {
  const catalog = await attempt(() => api("/mcp/catalog"));
  if (!catalog || !Array.isArray(catalog.categories)) {
    console.error("Invalid catalog response:", catalog);
    return;
  }
  Tools.catalog = catalog;
  modal(
    "Tool servers catalog",
    `<p>Servers suited to office work, each run by the vendor itself and checked by hand on ${esc(catalog.checked_on)}. Adding one fills in the form; you still review every tool it offers before an agent can use it.</p>
     <input type="search" id="catalog-filter" aria-label="Find a service" placeholder="Find a service: Slack, Canva, analytics…" autocomplete="off">
     ${catalog.categories
       .map((group) => `<section class="catalog-section"><h3 class="catalog-group">${esc(group.label)}</h3><ul class="catalog">${group.entries.map(catalogEntry).join("")}</ul></section>`)
       .join("")}
     <p class="muted catalog-empty" hidden>Nothing here by that name. Any server that speaks MCP over HTTPS can still be added by its address.</p>`,
    `<div class="button-row">${btn("Not listed? Add one by its address", "mcp-add")}${btn("Done", "close", "primary")}</div>`
  );
  // Find a service by name, vendor, what it does or its group.
  const filter = $("#catalog-filter");
  filter?.addEventListener("input", () => {
    const wanted = filter.value.trim().toLowerCase();
    let shown = 0;
    document.querySelectorAll(".catalog-section").forEach((section) => {
      const group = section.querySelector(".catalog-group")?.textContent.toLowerCase() || "";
      let inSection = 0;
      section.querySelectorAll(".catalog > li").forEach((item) => {
        const match = !wanted || group.includes(wanted) || item.textContent.toLowerCase().includes(wanted);
        item.hidden = !match;
        if (match) inSection += 1;
      });
      section.hidden = inSection === 0;
      shown += inSection;
    });
    const empty = document.querySelector(".catalog-empty");
    if (empty) empty.hidden = shown > 0;
  });
}

function schemaText(schema) {
  const properties = Object.keys(schema?.properties || {});
  return properties.length ? `Takes: ${properties.join(", ")}` : "Takes no arguments";
}

function claimsText(claims) {
  const said = Object.entries(claims || {})
    .filter(([key, value]) => key.endsWith("Hint") && typeof value === "boolean")
    .map(([key, value]) => `${key.replace(/Hint$/, "").replace(/([A-Z])/g, " $1").toLowerCase()}: ${value ? "yes" : "no"}`);
  return said.length ? `The server says — ${said.join(", ")}. A claim, not a promise.` : "";
}

function toolForm(tool, index) {
  const chosen = tool.chosen || (["allowed", "needs_approval", "refused"].includes(tool.stance) ? tool.stance : "");
  const tags = new Set(tool.acts_as || []);
  const held = tool.chosen && tool.stance !== tool.chosen;
  const choices = [
    ["", "Choose…", true],
    ["allowed", "Allowed — agents may call it"],
    ["needs_approval", "Asks me first — the task pauses for my yes"],
    ["refused", "Refused — never offered to an agent"],
  ];
  return `<div class="card tool-review">
    <div class="card-top"><h3>${esc(tool.name)}</h3>${tool.price_usd ? tag(`Paid · up to ${money(tool.price_usd)}`, "amber") : ""}${stanceTag(tool.stance)}</div>
    <p>${esc(tool.description || "No description.")}</p>
    <p class="muted">${esc(schemaText(tool.schema))}${claimsText(tool.claims) ? ` · ${esc(claimsText(tool.claims))}` : ""}</p>
    <details><summary>Arguments schema</summary><pre>${esc(JSON.stringify(tool.schema || {}, null, 2))}</pre></details>
    ${held ? `<p class="muted">You chose "${esc(STANCE_LABEL[tool.chosen]?.[0] || tool.chosen)}"; section 18 holds it at "${esc(STANCE_LABEL[tool.stance]?.[0] || tool.stance)}" because of what you said it does.</p>` : ""}
    ${field("Your decision", `stance-${index}`, { type: "select", choices, value: chosen })}
    ${field("Price per call (USD, optional)", `price-${index}`, {
      type: "number",
      required: false,
      value: tool.price_usd ?? "",
      attrs: 'min="0.01" max="1000" step="0.01" placeholder="Free"',
      hint: "If the vendor bills you for each call, the most one call costs. It counts against the task's cost limit and the monthly limits. Being billed per call is not \"Spends money\" below: that is for a tool that pays or moves money.",
    })}
    <fieldset class="acts-as"><legend>What it does (section 18)</legend>${Object.entries(SECTION_18)
      .map(
        ([action, [label, level]]) =>
          `<label class="check"><input type="checkbox" data-acts="${index}" value="${esc(action)}" ${tags.has(action) ? "checked" : ""}> ${esc(label)} <small class="muted">${level === "deny" ? "never allowed" : "always asks you"}</small></label>`
      )
      .join("")}</fieldset>
    <div class="button-row">${btn("Save", `mcp-decide:${index}`, "primary")}</div></div>`;
}

async function openToolReview(id) {
  const server = Tools.servers.find((s) => s.id === id);
  if (!server) {
    console.error("Server not found:", id);
    return;
  }
  Tools.reviewing = server;
  modal(
    `Review ${server.name}'s tools`,
    server.tools.length
      ? `${bulkReview(server)}<p>Decide each tool on what it says it does. If its description or arguments change later, it is reviewed again.</p>${server.tools.map(toolForm).join("")}`
      : "<p>This server offers no tools. Ask it again once it has some.</p>"
  );
  $("#modal")?.addEventListener("close", () => rerender(), { once: true });
}

// One press for every tool not decided yet: the server's read-only
// claim sorts them, and the owner picks what each group gets.
function bulkReview(server) {
  const open = server.tools.filter((t) => t.stance === "unreviewed" || t.stance === "changed_since_review");
  if (!open.length) return "";
  const reading = open.filter((t) => t.claims?.readOnlyHint).length;
  const choice = (name, chosen) =>
    `<select name="${name}">${["allowed", "needs_approval", "refused"].map((s) => `<option value="${s}" ${s === chosen ? "selected" : ""}>${esc(STANCE_LABEL[s][0])}</option>`).join("")}</select>`;
  return `<form id="mcp-bulk-form" class="card bulk-review">
    <strong>Decide the ${open.length} undecided at once</strong>
    <label class="bulk-line">${reading} that ${esc(server.name)} says only read ${choice("read_only", "allowed")}</label>
    <label class="bulk-line">${open.length - reading} that may change things ${choice("others", "needs_approval")}</label>
    <p class="muted">“Only read” is the server's own claim. “Asks you first” stops the task and asks you before each call. You can change any tool below afterwards.</p>
    <div class="button-row"><button class="primary">Apply to ${open.length} tools</button></div>
  </form>`;
}

async function decideTool(index) {
  const server = Tools.reviewing;
  const tool = server?.tools?.[Number(index)];
  if (!tool) {
    console.error("Tool not found at index:", index);
    return;
  }
  const select = $(`#f-stance-${index}`);
  const stance = select?.value || "";
  if (!stance) {
    toast("Choose a decision first.", true);
    return;
  }
  const actsAs = [...document.querySelectorAll(`[data-acts="${index}"]:checked`)].map((box) => box.value);
  const priceText = String($(`#f-price-${index}`)?.value || "").trim();
  const price = priceText ? Number(priceText) : null;
  if (price !== null && !(price > 0 && price <= 1000)) {
    toast("A price is more than $0 and at most $1,000 per call, or left empty for a free tool.", true);
    return;
  }
  const updated = await attempt(() =>
    api(`/mcp/servers/${server.id}/tools/${encodeURIComponent(tool.name)}/decision`, { method: "POST", body: { stance, acts_as: actsAs, price_usd: price } })
  );
  if (!updated) return;
  const now = updated.tools?.find((t) => t.name === tool.name);
  toast(now && now.stance !== stance ? `Saved. Section 18 holds ${tool.name} at "${STANCE_LABEL[now.stance]?.[0] || now.stance}".` : `Saved ${tool.name}.`);
  Tools.servers = Tools.servers.map((s) => (s.id === updated.id ? updated : s));
  Tools.reviewing = updated;
  openToolReview(updated.id);
}

function openServerAccess(id) {
  const server = Tools.servers.find((s) => s.id === id);
  if (!server) {
    console.error("Server not found:", id);
    return;
  }
  modal(
    `Who may use ${server.name}`,
    `${Tools.projects.length
      ? `<p>Tasks and discussions in a ticked project may use this server's reviewed tools.${server.callable ? "" : " None are usable yet: review them first."}</p>${Tools.projects
          .map((p) => `<label class="check"><input type="checkbox" data-mcp-grant="${esc(p.id)}" ${Tools.grants[p.id]?.has(id) ? "checked" : ""}> ${esc(p.name)}</label>`)
          .join("")}`
      : "<p>Create a project first (start a task), then grant it this server here.</p>"}
    <p>Discussions outside any project reach further than one project's work: any of them could use this server.</p>
    <label class="check"><input type="checkbox" data-mcp-no-project ${server.no_project_discussions ? "checked" : ""}> Discussions with no project</label>`
  );
  const noProject = document.querySelector("[data-mcp-no-project]");
  noProject?.addEventListener("change", async () => {
    const ok = await attempt(() => api(`/mcp/servers/${id}/no-project`, { method: noProject.checked ? "PUT" : "DELETE" }), noProject.checked ? "Access granted." : "Access removed.");
    if (ok === undefined) noProject.checked = !noProject.checked;
    else server.no_project_discussions = noProject.checked;
  });
  document.querySelectorAll("[data-mcp-grant]").forEach((box) => {
    box.addEventListener("change", async () => {
      const project = box.dataset.mcpGrant;
      const ok = await attempt(() => api(`/projects/${project}/mcp/${id}`, { method: box.checked ? "PUT" : "DELETE" }), box.checked ? "Access granted." : "Access removed.");
      if (ok === undefined) box.checked = !box.checked;
      else if (box.checked) Tools.grants[project]?.add(id);
      else Tools.grants[project]?.delete(id);
    });
  });
  $("#modal")?.addEventListener("close", () => rerender(), { once: true });
}

Office.screens.tools = { title: "Tools", render: renderTools };

Office.forms = Object.assign(Office.forms || {}, {
  "mcp-bulk-form": async (form) => {
    const server = Tools.reviewing;
    if (!server) return;
    const body = { read_only: form.querySelector("[name=read_only]")?.value || "allowed", others: form.querySelector("[name=others]")?.value || "needs_approval" };
    const updated = await attempt(() => api(`/mcp/servers/${server.id}/decide-undecided`, { method: "POST", body }));
    if (!updated) return;
    toast(`Decided ${updated.decided} tools. ${updated.callable} can be used now.`);
    Tools.servers = Tools.servers.map((s) => (s.id === updated.id ? updated : s));
    openToolReview(updated.id);
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "mcp-add": () => openAddServer(),
  "mcp-catalog": openCatalog,
  "mcp-catalog-add": (id) => {
    const entry = (Tools.catalog?.categories || []).flatMap((group) => group.entries).find((e) => e.id === id);
    if (!entry) {
      console.error("Catalog entry not found:", id);
      return;
    }
    openAddServer(entry);
  },
  "mcp-review": openToolReview,
  "mcp-decide": decideTool,
  "mcp-access": openServerAccess,
  "mcp-refresh": async (id) => {
    const server = await attempt(() => api(`/mcp/servers/${id}/list`, { method: "POST" }));
    if (server) toast(server.changed ? "It now offers something different: review the changed tools." : `Asked again: ${server.tools.length} tools.`);
    rerender();
  },
  "mcp-sign-in": async (id) => {
    const started = await attempt(() => api(`/mcp/servers/${id}/sign-in`, { method: "POST" }));
    if (!started?.authorize_url) {
      console.error("Sign-in did not start:", started);
      return;
    }
    // The vendor's own page, in this tab; it sends the owner back to Tools.
    window.location.assign(started.authorize_url);
  },
  "mcp-remove": async (id) => {
    const server = Tools.servers.find((s) => s.id === id);
    if (!window.confirm(`Remove "${server?.name}"? Its key is deleted and no project can use it.`)) return;
    if ((await attempt(() => api(`/mcp/servers/${id}`, { method: "DELETE" }), "Server removed.")) !== undefined) rerender();
  },
});

Office.forms = Object.assign(Office.forms || {}, {
  "mcp-server-form": async (form) => {
    const data = new FormData(form);
    const secret = String(data.get("secret") || "");
    const body = { name: String(data.get("name")).trim(), url: String(data.get("url")).trim(), ...(secret ? { secret } : {}) };
    const server = await attempt(() => api("/mcp/servers", { method: "POST", body }));
    closeModal();
    // A server that did not answer is still recorded, so show it either way.
    await rerender();
    if (server?.status === "NEEDS_SIGN_IN") toast(`Added. Sign in to ${server.name} from its card to see its tools.`);
    else if (server) openToolReview(server.id);
  },
});

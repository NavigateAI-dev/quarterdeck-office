// Mobile and remote access: open the office from a phone, on the home
// network or through the owner's private tailnet. Nothing here makes the office
// public: each address works only once the office has checked it, and a phone
// still signs in with a one-time code. A phone counts as connected only once
// it has signed in, never because a code was shown.

const Access = { points: null, pairing: null, pairTimer: null, busy: "" };

const REMOTE_SAYS = {
  setup_required: "Not connected to Tailscale yet.",
  awaiting_authorization: "Waiting for you to approve this office in Tailscale.",
  off: "Connected to your tailnet. The route to the office is off.",
  checking: "Checking the route…",
  ready_for_phone_test: "On. Connect your phone to confirm it works.",
  confirmed_from_phone: "On, and confirmed from a phone.",
  connection_problem: "The route is not doing what the office expects.",
};

function accessTag(point) {
  if (!point?.enabled) return tag("Off", "gray");
  return point.confirmed_at ? tag("Confirmed from phone") : tag("Ready for phone test", "clay");
}

function lastCheck(point) {
  if (!point?.last_check_at) return "";
  return `<p class="muted access-check">Last checked ${esc(when(point.last_check_at))}.</p>`;
}

function confirmedLine(point) {
  if (!point?.confirmed_at) return `<p class="muted">No phone has signed in through this address yet.</p>`;
  return `<p>Confirmed from ${esc(point.confirmed_device || "a phone")}, ${esc(when(point.confirmed_at))}.</p>`;
}

function copyable(text) {
  return `<div class="access-copy"><code>${esc(text)}</code>${btn("Copy", `access-copy:${encodeURIComponent(text)}`, "small")}</div>`;
}

function homeCard(home) {
  const setup = home?.setup || {};
  let body;
  if (setup.required) {
    body = `<p>Once, on this Mac, publish the office on your home network. Open Terminal in the office folder and run:</p>
      ${copyable(setup.command || "python3 -m scripts.access home")}
      <p class="muted">It finds the Mac's own network address and restarts the office. Nothing is reachable until you turn it on here. To undo it: <code>${esc(setup.undo || "python3 -m scripts.access home --off")}</code></p>`;
  } else if (!home.enabled) {
    const choices = (setup.suggested || []).map((a) => [a, a]);
    const failed = home.check && home.check.answered === false
      ? `<p class="error-text">The office did not answer at ${esc(home.check.via || home.address || "that address")}${home.check.reason ? ` (${esc(home.check.reason)})` : ""}. Check the Mac is on that network, then try again.</p>`
      : "";
    body = `<p>Your phone must be on the same Wi-Fi as this Mac.</p>
      ${failed}
      <form id="access-home-form">${
        choices.length
          ? field("Address for the phone", "address", { type: "select", choices, value: home.address || choices[0][0] })
          : field("Address for the phone", "address", { placeholder: "http://192.168.1.20:8000", value: home.address || "" })
      }<div class="button-row"><button class="primary" ${Access.busy === "home" ? "disabled" : ""}>${Access.busy === "home" ? "Checking…" : "Check and turn on"}</button></div></form>
      <p class="muted">A <code>.local</code> name is easier to remember but some phones do not find it; the number always works.</p>`;
  } else {
    body = `<p>At ${copyable(home.address)}</p>
      ${confirmedLine(home)}
      <div class="button-row">${btn("Connect your phone", "access-pair:home", "primary")}${btn("Turn off", "access-home-off")}</div>`;
  }
  return `<div class="card access-card" data-access="home"><h2>Home network ${accessTag(home)}</h2>
    <p class="muted">Fast and simple, only while the phone is on your Wi-Fi.</p>
    ${body}${lastCheck(home)}</div>`;
}

function remoteCard(point) {
  const remote = point?.remote || {};
  const stage = remote.stage || "setup_required";
  const check = point?.check || {};
  let body = `<p>${esc(REMOTE_SAYS[stage] || "")}</p>`;
  if (stage === "setup_required" && !remote.installed) {
    body = `<p>The remote container is not running. Start it from the office folder:</p>${copyable("docker compose up -d remote")}`;
  } else if (stage === "setup_required") {
    body += `<p>You need a free Tailscale account, and the Tailscale app on your phone signed in to the same account.</p>
      <div class="button-row">${btn(Access.busy === "connect" ? "Asking Tailscale…" : "Connect to Tailscale", "access-remote-connect", "primary", Access.busy ? "disabled" : "")}</div>
      <details class="access-advanced"><summary>Use an auth key instead</summary>
        <form id="access-key-form">${field("One-time auth key", "auth_key", { type: "password", attrs: 'autocomplete="off"' })}
        <p class="muted">Create a one-off key in the Tailscale admin console. It is used once to join and not kept.</p>
        <div class="button-row"><button>Join with this key</button></div></form></details>`;
  } else if (stage === "awaiting_authorization") {
    body += `<div class="button-row"><a class="button primary" href="${esc(remote.auth_url)}" target="_blank" rel="noopener noreferrer">Approve “quarterdeck” in Tailscale</a>${btn("I've approved it", "access-refresh")}</div>`;
  } else if (stage === "off") {
    const https = check.needs_https
      ? `<p class="error-text">Your tailnet needs HTTPS certificates turned on once.${check.url ? ` <a href="${esc(check.url)}" target="_blank" rel="noopener noreferrer">Turn them on</a>, then try again.` : " Turn them on under DNS in the Tailscale admin console, then try again."}</p>`
      : "";
    body += `${https}<p class="muted">Only devices signed in to your tailnet can reach it. It is never put on the public internet.</p>
      <div class="button-row">${btn(Access.busy === "remote" ? "Turning on…" : "Turn on remote access", "access-remote-on", "primary", Access.busy ? "disabled" : "")}</div>`;
  } else if (stage === "ready_for_phone_test" || stage === "confirmed_from_phone" || stage === "checking") {
    body += `<p>At ${copyable(point.address || "")}</p>${confirmedLine(point)}
      <div class="button-row">${btn("Connect your phone", "access-pair:remote", "primary")}${btn(Access.busy === "remote" ? "Turning off…" : "Turn off", "access-remote-off", "", Access.busy ? "disabled" : "")}</div>`;
  } else if (stage === "connection_problem") {
    const still = check.still_routed ? "<p class=\"error-text\">The route did not stop when asked. The office refuses that address meanwhile; try turning it off again.</p>" : "";
    body += `${still}<div class="button-row">${btn("Turn off", "access-remote-off", "primary")}${btn("Check again", "access-refresh")}</div>`;
  }
  const forget = remote.installed && stage !== "setup_required"
    ? `<details class="access-advanced"><summary>Advanced</summary><p class="muted">Device name: ${esc(remote.dns_name || remote.hostname || "quarterdeck")}. Forgetting it turns the route off and signs this office out of your tailnet.</p>
       <div class="button-row">${btn("Forget this device", "access-remote-forget", "danger")}</div></details>`
    : "";
  const label = remote.label || "Setup required";
  const marked = stage === "confirmed_from_phone" ? tag(label) : stage === "ready_for_phone_test" ? tag(label, "clay") : tag(label, "gray");
  return `<div class="card access-card" data-access="remote"><h2>Remote access ${marked}</h2>
    <p class="muted">From anywhere, through your private Tailscale network.</p>
    ${body}${lastCheck(point)}${forget}</div>`;
}

// Signing in with a name and password, from any device.
function loginCard(login) {
  const on = Boolean(login?.set);
  const status = on ? tag("On") : tag("Off", "gray");
  const current = on
    ? `<label class="field" for="login-current">Current password</label>
       <input id="login-current" name="current_password" type="password" autocomplete="current-password" required>`
    : "";
  const remove = on
    ? `<details class="login-remove"><summary>Turn password sign-in off</summary>
        <form id="login-remove-form">
          <label class="field" for="login-remove-current">Current password</label>
          <input id="login-remove-current" name="current_password" type="password" autocomplete="current-password" required>
          <div class="button-row"><button>Turn off</button></div>
        </form></details>`
    : "";
  return `<div class="card access-card" data-access="login"><h2>Sign in with a password ${status}</h2>
    <p class="muted">${on ? `Signed in as <strong>${esc(login.name)}</strong> from any device that reaches the office. The office keeps only a scrambled copy of the password.` : "Sign in on your phone or another computer with a name and password instead of a one-time code."}</p>
    <form id="login-form">
      <label class="field" for="login-set-name">Sign-in name</label>
      <input id="login-set-name" name="name" autocomplete="username" autocapitalize="none" spellcheck="false" value="${esc(login?.name || Office.owner || "")}" required>
      ${current}
      <label class="field" for="login-new">${on ? "New password" : "Password"} (10 characters or more)</label>
      <input id="login-new" name="password" type="password" autocomplete="new-password" minlength="10" required>
      <div class="button-row"><button class="primary">${on ? "Change password" : "Turn on"}</button></div>
    </form>
    ${remove}
    <p class="muted">Five wrong tries stop password sign-in for 15 minutes; the launcher on this computer still works. Away from home, use Remote access so the password travels encrypted.</p></div>`;
}

function awakeCard() {
  return `<div class="card access-card" data-access="awake"><h2>Keep the Mac reachable</h2>
    <p>Your phone reaches the office on this Mac. For that the Mac must stay powered, online and awake, with Docker running.</p>
    <p class="muted">On a Mac mini: System Settings → Energy → turn on “Prevent automatic sleeping when the display is off”. The office does not change this for you.</p></div>`;
}

async function renderAccess(content) {
  const [data, login] = await Promise.all([api("/access"), api("/settings/login").catch(() => ({ set: false }))]);
  Access.points = Array.isArray(data?.points) ? data.points : [];
  const home = Access.points.find((p) => p.kind === "home") || { kind: "home", setup: { required: true } };
  const remote = Access.points.find((p) => p.kind === "remote") || { kind: "remote" };
  content.innerHTML =
    heading("MOBILE & REMOTE ACCESS", "Open the office from your phone.", "Two ways in. Either one works only after the office has checked it. Sign in there with a one-time code, or with a password you set here.") +
    `<div class="access-grid">${homeCard(home)}${remoteCard(remote)}${loginCard(login)}${awakeCard()}</div>`;
}

function pairBody(pairing, point) {
  const confirmed = point?.confirmed_at && Access.pairing?.since && point.confirmed_at > Access.pairing.since;
  const state = confirmed
    ? `<p class="access-confirmed">Signed in from ${esc(point.confirmed_device || "a phone")}.</p>`
    : `<p class="muted" data-pair-wait>Waiting for the phone to sign in. Showing the code does not count.</p>`;
  return `<div class="access-pair">
      <div class="access-qr">${pairing.qr || ""}</div>
      <ol class="access-steps">
        ${point?.kind === "remote" ? "<li>Open the Tailscale app on the phone and make sure it is connected.</li>" : "<li>Join the phone to the same Wi-Fi as this Mac.</li>"}
        <li>Point the phone's camera at the code and open the link.</li>
        <li>To keep it, use Share → Add to Home Screen.</li>
      </ol>
    </div>
    <p class="muted">The code works once and for ${Math.round((pairing.expires_in || 120) / 60)} minutes. Or send the link to the phone yourself:</p>
    ${copyable(pairing.url)}
    ${state}`;
}

async function startPairing(kind) {
  const pairing = await attempt(() => api("/access/pair", { method: "POST", body: { kind } }));
  if (!pairing) return;
  Access.pairing = { kind, since: new Date().toISOString(), data: pairing };
  const point = (Access.points || []).find((p) => p.kind === kind);
  modal("Connect your phone", pairBody(pairing, point), `<div class="button-row">${btn("New code", `access-pair:${kind}`)}${btn("Done", "access-pair-done", "primary")}</div>`);
  clearInterval(Access.pairTimer);
  Access.pairTimer = setInterval(watchPairing, 3000);
}

async function watchPairing() {
  const dialog = document.getElementById("modal");
  if (!dialog?.open || !Access.pairing) {
    clearInterval(Access.pairTimer);
    return;
  }
  try {
    const data = await api("/access");
    const point = (data?.points || []).find((p) => p.kind === Access.pairing.kind);
    if (point?.confirmed_at && point.confirmed_at > Access.pairing.since) {
      clearInterval(Access.pairTimer);
      const content = document.getElementById("modal-content");
      const wait = content?.querySelector("[data-pair-wait]");
      if (wait) wait.outerHTML = `<p class="access-confirmed">Signed in from ${esc(point.confirmed_device || "a phone")}.</p>`;
    }
  } catch (error) {
    console.error("Pairing check failed", error);
  }
}

async function accessStep(which, work, success) {
  Access.busy = which;
  rerender();
  try {
    return await attempt(work, success);
  } finally {
    Access.busy = "";
    rerender();
  }
}

Office.screens.access = { title: "Mobile & remote access", render: renderAccess };

Office.forms = Object.assign(Office.forms || {}, {
  "access-home-form": async (form) => {
    const address = form.querySelector("[name=address]")?.value?.trim() || "";
    if (!address) return;
    const point = await accessStep("home", () => api("/access/home/enable", { method: "POST", body: { address } }));
    if (point && point.enabled) toast("The office answered there. Connect your phone.");
    else if (point) toast("The office did not answer at that address.", true);
  },
  "login-form": async (form) => {
    const name = form.querySelector("[name=name]")?.value?.trim() || "";
    const passwordInput = form.querySelector("[name=password]");
    const currentInput = form.querySelector("[name=current_password]");
    const body = { name, password: passwordInput?.value || "" };
    if (currentInput) body.current_password = currentInput.value;
    const saved = await attempt(() => api("/settings/login", { method: "PUT", body }));
    if (passwordInput) passwordInput.value = "";
    if (currentInput) currentInput.value = "";
    if (saved?.set) {
      toast(`Password sign-in is on for ${saved.name}.`);
      rerender();
    }
  },
  "login-remove-form": async (form) => {
    const input = form.querySelector("[name=current_password]");
    const saved = await attempt(() => api("/settings/login/remove", { method: "POST", body: { current_password: input?.value || "" } }));
    if (input) input.value = "";
    if (saved && !saved.set) {
      toast("Password sign-in is off.");
      rerender();
    }
  },
  "access-key-form": async (form) => {
    const input = form.querySelector("[name=auth_key]");
    const key = input?.value?.trim() || "";
    if (input) input.value = "";
    if (!key) return;
    await accessStep("connect", () => api("/access/remote/connect", { method: "POST", body: { auth_key: key } }));
  },
});

Office.actions = Object.assign(Office.actions || {}, {
  "access-copy": async (encoded) => {
    const text = decodeURIComponent(encoded || "");
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied.");
    } catch (error) {
      console.error("Copy failed", error);
      toast("Copy did not work here; select the text instead.", true);
    }
  },
  "access-refresh": () => rerender(),
  "access-pair": (kind) => startPairing(kind),
  "access-pair-done": () => {
    clearInterval(Access.pairTimer);
    Access.pairing = null;
    closeModal();
    rerender();
  },
  "access-home-off": async () => {
    await accessStep("home", () => api("/access/home/disable", { method: "POST" }), "The home network address is off. This computer still works.");
  },
  "access-remote-connect": async () => {
    await accessStep("connect", () => api("/access/remote/connect", { method: "POST", body: {} }));
  },
  "access-remote-on": async () => {
    const point = await accessStep("remote", () => api("/access/remote/enable", { method: "POST" }));
    if (point?.enabled) toast("Remote access is on. Connect your phone to confirm it.");
  },
  "access-remote-off": async () => {
    const point = await accessStep("remote", () => api("/access/remote/disable", { method: "POST" }));
    if (point && !point.check?.still_routed) toast("Remote access is off. This computer still works.");
  },
  "access-remote-forget": async () => {
    if (!window.confirm("Forget this device? Remote access stops and the office leaves your tailnet.")) return;
    await accessStep("remote", () => api("/access/remote/forget", { method: "POST" }), "The office has left your tailnet.");
  },
});

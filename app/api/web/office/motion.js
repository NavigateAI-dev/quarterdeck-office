"use strict";

// Motion: springs, the morphing selection pill and the Island.
//
// One small spring integrator drives everything that moves. JS motion (the
// pill, the Island) calls it directly; CSS gets spring curves sampled from the
// same solver as `linear()` easings, so a hovered card and a sliding pill
// settle the same way. Motion is for orientation, not decoration: the
// system's "reduce motion" setting makes every spring land at once.

const Motion = (() => {
  const query = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  const reduced = () => Boolean(query && query.matches);

  // Stiffness, damping and mass, in the terms a spring is usually tuned in.
  const PRESETS = {
    snappy: { stiffness: 520, damping: 40, mass: 1 },
    smooth: { stiffness: 280, damping: 30, mass: 1 },
    bouncy: { stiffness: 420, damping: 22, mass: 1 },
  };
  // A spring still running on a key is replaced by the next one on that key,
  // from where it is and as fast as it is going: nothing snaps.
  const running = new Map();
  const STEP = 1 / 240;

  function spring(from, to, apply, options = {}) {
    const { stiffness, damping, mass } = { ...PRESETS.smooth, ...(PRESETS[options.preset] || {}), ...options };
    const keys = Object.keys(to);
    if (reduced()) {
      apply({ ...to });
      return Promise.resolve();
    }
    const previous = options.key ? running.get(options.key) : null;
    if (previous) previous.cancelled = true;
    const position = { ...from };
    const velocity = {};
    for (const key of keys) {
      position[key] = Number.isFinite(position[key]) ? position[key] : to[key];
      velocity[key] = previous?.velocity?.[key] || 0;
    }
    const scale = Math.max(1, ...keys.map((key) => Math.abs(to[key] - position[key])));
    const token = { cancelled: false, velocity };
    if (options.key) running.set(options.key, token);
    return new Promise((resolve) => {
      let last = performance.now();
      const frame = (now) => {
        if (token.cancelled) return resolve();
        const elapsed = Math.min(0.064, Math.max(0, (now - last) / 1000));
        last = now;
        const steps = Math.max(1, Math.ceil(elapsed / STEP));
        const h = elapsed / steps;
        for (let i = 0; i < steps; i += 1) {
          for (const key of keys) {
            const force = -stiffness * (position[key] - to[key]) - damping * velocity[key];
            velocity[key] += (force / mass) * h;
            position[key] += velocity[key] * h;
          }
        }
        const settled = keys.every(
          (key) => Math.abs(position[key] - to[key]) < 0.002 * scale && Math.abs(velocity[key]) < 0.02 * scale
        );
        if (settled) {
          if (options.key && running.get(options.key) === token) running.delete(options.key);
          apply({ ...to });
          return resolve();
        }
        apply({ ...position });
        requestAnimationFrame(frame);
      };
      requestAnimationFrame(frame);
    });
  }

  // The same spring as a CSS easing: sampled from 0 to 1 until it settles.
  function curve(name) {
    const { stiffness, damping, mass } = PRESETS[name];
    let position = 0;
    let velocity = 0;
    const samples = [0];
    let time = 0;
    const dt = 1 / 120;
    while (time < 2) {
      const force = -stiffness * (position - 1) - damping * velocity;
      velocity += (force / mass) * dt;
      position += velocity * dt;
      time += dt;
      samples.push(position);
      if (Math.abs(position - 1) < 0.001 && Math.abs(velocity) < 0.01) break;
    }
    const every = Math.max(1, Math.floor(samples.length / 48));
    const points = samples.filter((_, index) => index % every === 0);
    points[points.length - 1] = 1;
    return { easing: `linear(${points.map((p) => +p.toFixed(4)).join(", ")})`, ms: Math.round(time * 1000) };
  }

  function install() {
    const root = document.documentElement;
    if (!root || !CSS.supports?.("transition-timing-function", "linear(0, 1)")) return;
    for (const name of Object.keys(PRESETS)) {
      const { easing, ms } = curve(name);
      root.style.setProperty(`--spring-${name}`, easing);
      root.style.setProperty(`--spring-${name}-ms`, `${ms}ms`);
    }
  }

  // ---- the morphing selection pill ----
  //
  // One pill per list, behind the items. Choosing another item moves it there
  // on two springs: the edge in the direction of travel is snappy, the other
  // softer, so the pill stretches towards the choice and then settles, like a
  // drop of liquid, instead of sliding as a block. Lists are redrawn freely;
  // the pill remembers where it was by key and starts from there.
  const pills = new Map();

  function edgesOf(target, container) {
    const box = container.getBoundingClientRect();
    const r = target.getBoundingClientRect();
    return {
      left: r.left - box.left + container.scrollLeft,
      top: r.top - box.top + container.scrollTop,
      right: r.right - box.left + container.scrollLeft,
      bottom: r.bottom - box.top + container.scrollTop,
    };
  }

  function place(pill, edges) {
    pill.style.transform = `translate(${edges.left}px, ${edges.top}px)`;
    pill.style.width = `${Math.max(0, edges.right - edges.left)}px`;
    pill.style.height = `${Math.max(0, edges.bottom - edges.top)}px`;
  }

  function follow(container, activeSelector, key, variant = "") {
    if (!container) return;
    let pill = container.querySelector(":scope > .motion-pill");
    if (!pill) {
      pill = document.createElement("span");
      pill.className = `motion-pill ${variant}`.trim();
      pill.setAttribute("aria-hidden", "true");
      container.prepend(pill);
    }
    container.classList.add("has-pill");
    const target = container.querySelector(activeSelector);
    if (!target) {
      pill.style.opacity = "0";
      return;
    }
    const to = edgesOf(target, container);
    if (variant === "line") to.top = to.bottom - 2;
    const state = pills.get(key);
    pill.style.opacity = "1";
    if (!state) {
      place(pill, to);
      pills.set(key, { edges: { ...to }, pill });
      return;
    }
    state.pill = pill;
    const from = { ...state.edges };
    place(pill, from);
    const down = to.top + to.bottom > from.top + from.bottom;
    const across = to.left + to.right > from.left + from.right;
    const lead = { [down ? "bottom" : "top"]: 1, [across ? "right" : "left"]: 1 };
    const apply = (group) => (values) => {
      Object.assign(state.edges, values);
      place(state.pill, state.edges);
    };
    const pick = (names) => Object.fromEntries(names.map((name) => [name, to[name]]));
    const leading = Object.keys(to).filter((name) => lead[name]);
    const trailing = Object.keys(to).filter((name) => !lead[name]);
    spring(pickFrom(from, leading), pick(leading), apply("lead"), { preset: "snappy", key: `${key}:lead` });
    spring(pickFrom(from, trailing), pick(trailing), apply("trail"), { preset: "smooth", key: `${key}:trail` });
  }

  function pickFrom(source, names) {
    return Object.fromEntries(names.map((name) => [name, source[name]]));
  }

  // A resized window moves the items; the pill goes with them at once.
  window.addEventListener("resize", () => {
    for (const state of pills.values()) {
      const container = state.pill?.parentElement;
      const target = container?.querySelector("a.active, button.active, [aria-selected='true']");
      if (!container || !target) continue;
      state.edges = edgesOf(target, container);
      if (state.pill.classList.contains("line")) state.edges.top = state.edges.bottom - 2;
      place(state.pill, state.edges);
    }
  });

  // A screen that opens rises in, a few of its parts at a time.
  const STAGGER = 12;
  let entering = null;
  function enter(container) {
    if (!container || reduced()) return;
    [...container.children].slice(0, STAGGER).forEach((child, index) => child.style.setProperty("--i", index));
    container.classList.remove("entering");
    void container.offsetWidth; // Restart the entrance if one was playing.
    container.classList.add("entering");
    clearTimeout(entering);
    entering = setTimeout(() => container.classList.remove("entering"), 1100);
  }

  install();
  return { spring, reduced, curve, follow, enter, PRESETS };
})();

// ---- the Island ----
//
// One pill at the top of the page that says what the office is doing, after
// Codenotch's notch. Idle, it shows the month's spend against its limit; while
// tasks work, a ring for how far each has come and one for how much of its
// budget it has used; amber while anything waits on you. It springs open to
// say what just happened: a task finished or started waiting (only a change
// seen since the page opened, never the first reading) or a limit crossed 80%
// or 100% (once, however often it is read). Clicked, it opens into all of it.
// Its size is sprung from what it holds, so each state grows out of the last.
const Island = (() => {
  let tasks = [];
  let waiting = [];
  let month = null;
  let expanded = false;
  let message = null;
  let timer = null;
  let queue = [];
  let previous = null;
  let history = [];
  const rings = new Map();
  const MESSAGE_MS = 3500;
  const ERROR_MS = 6000;
  const SEEN = "office.island.seen";
  const SOUND = "office.island.sound";

  const root = () => document.getElementById("island");
  // What a task is doing, as a live status says it: a verb, not the step's name.
  const DOING = {
    plan: "Planning",
    work: "Working through the plan",
    replan: "Replanning",
    research: "Researching",
    strategy: "Writing",
    critique: "Reviewing",
    synthesis: "Writing the answer",
    approval: "Waiting for you",
    engineering: "Following up",
  };
  const stepLabel = (step) => DOING[step] || (step ? String(step) : "Starting");
  const ENDED = {
    COMPLETED: "finished.",
    FAILED: "stopped with an error.",
    CANCELLED: "was cancelled.",
    REJECTED: "was rejected.",
    CLOSED: "was closed.",
  };
  const money2 = (value) => `$${Number(value || 0).toFixed(2)}`;
  const percent = (share) => `${Math.round(Number(share || 0) * 100)}%`;

  function stored(key, fallback) {
    try {
      const value = localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch {
      return fallback;
    }
  }
  function store(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* private window or blocked storage: remembered for this page only */
    }
  }

  // A ring: how much of something is done or used. Drawn from where it was.
  function ring(key, fraction, tone = "", size = 18) {
    const to = Math.max(0, Math.min(1, Number(fraction) || 0));
    const from = rings.has(key) ? rings.get(key) : to;
    rings.set(key, to);
    return `<svg class="ring ${tone}" viewBox="0 0 20 20" width="${size}" height="${size}" aria-hidden="true"><circle class="ring-track" cx="10" cy="10" r="8"></circle><circle class="ring-fill" cx="10" cy="10" r="8" pathLength="100" data-to="${(100 - to * 100).toFixed(1)}" style="stroke-dashoffset:${(100 - from * 100).toFixed(1)}"></circle></svg>`;
  }
  function settleRings(inner) {
    const fills = inner.querySelectorAll(".ring-fill[data-to]");
    if (!fills.length) return;
    requestAnimationFrame(() => fills.forEach((fill) => (fill.style.strokeDashoffset = fill.dataset.to)));
  }
  const tone = (share) => (share >= 1 ? "over" : share >= 0.8 ? "near" : "");

  function state() {
    if (message) return "message";
    // It opens at any time: the month, the providers and the sound switch are
    // worth reaching when nothing is running too.
    if (expanded) return "expanded";
    if (tasks.length) return "live";
    if (waiting.length) return "waiting";
    return "idle";
  }

  function taskRow(task) {
    const progress = task.progress || { done: 0, total: 1 };
    const budget = task.budget || {};
    const spend = budget.max_cost
      ? `${money2(budget.spent_cost)} of ${money2(budget.max_cost)}`
      : budget.max_tokens
        ? `${Number(budget.spent_tokens || 0).toLocaleString()} of ${Number(budget.max_tokens).toLocaleString()} tokens`
        : money2(budget.spent_cost);
    return `<li><a href="#task/${esc(task.run_id)}">${ring(`p:${task.run_id}`, progress.done / Math.max(1, progress.total))}<span class="island-task">${esc(task.title)}<small>${esc(stepLabel(task.step))} · ${esc(progress.done)} of ${esc(progress.total)}</small></span><span class="island-spend">${ring(`b:${task.run_id}`, budget.share || 0, `small ${tone(budget.share || 0)}`, 14)}${esc(spend)}</span></a></li>`;
  }

  function monthLine() {
    if (!month) return "";
    const share = month.share;
    const text = month.limit ? `${money2(month.spent)} of ${money2(month.limit)} this month` : `${money2(month.spent)} this month`;
    return `<p class="island-month">${ring("month", share || 0, tone(share || 0), 14)}<span>${esc(text)}</span></p>`;
  }

  let providers = [];
  function providerLines() {
    const shown = providers.filter((entry) => entry.state === "ok" && (entry.balance || entry.limit?.limit));
    return shown
      .map((entry) => {
        const text = entry.balance
          ? `${entry.name}: ${Number(entry.balance.total).toFixed(2)} ${entry.balance.currency} left`
          : `${entry.name}: ${money2(entry.limit.remaining)} of ${money2(entry.limit.limit)} left`;
        // A ring only where there is a limit to measure against; a balance is a dot.
        const mark = entry.balance
          ? `<span class="island-dot${entry.usable === false ? " out" : ""}" aria-hidden="true"></span>`
          : ring(`prov:${entry.connection_id}`, entry.limit.share || 0, tone(entry.limit.share || 0), 14);
        return `<p class="island-month">${mark}<span>${esc(text)}</span></p>`;
      })
      .join("");
  }

  function balances(list) {
    providers = Array.isArray(list) ? list : [];
    if (expanded) render();
  }

  function body(now) {
    if (now === "message") {
      const mark = message.error ? "!" : message.kind === "alert" ? "%" : "✓";
      return `<span class="island-mark ${message.error ? "error" : message.kind || ""}" aria-hidden="true">${mark}</span><span class="island-text">${esc(message.text)}</span>`;
    }
    if (now === "expanded") {
      const working = tasks.length
        ? `<p class="island-title">Working now</p><ul>${tasks.map(taskRow).join("")}</ul>`
        : waiting.length
          ? ""
          : `<p class="island-title">Nothing running</p>`;
      const needs = waiting.length
        ? `<p class="island-title amber">Needs you</p><ul>${waiting
            .map(
              (task) =>
                `<li><a href="#task/${esc(task.run_id)}"><span class="island-pulse amber" aria-hidden="true"></span><span class="island-task">${esc(task.title)}<small>${esc(task.reason || "")}</small></span></a></li>`
            )
            .join("")}</ul>`
        : "";
      return `<div class="island-panel">${working}${needs}${monthLine()}${providerLines()}<div class="island-foot"><a class="island-more" href="#board">Open the board →</a><button type="button" class="island-sound" data-island-sound>${sound() ? "Sound on" : "Sound off"}</button></div></div>`;
    }
    if (now === "live") {
      const first = tasks[0];
      const text = tasks.length === 1 ? `${stepLabel(first.step)} · ${first.title}` : `${tasks.length} tasks working`;
      const done = tasks.reduce((sum, task) => sum + (task.progress?.done || 0), 0);
      const total = tasks.reduce((sum, task) => sum + (task.progress?.total || 0), 0) || 1;
      const budget = tasks.length === 1 && first.budget?.share != null ? `<span class="island-share">${esc(percent(first.budget.share))} of budget</span>` : "";
      return `${ring("live", done / total)}<span class="island-text">${esc(text)}</span>${budget}`;
    }
    if (now === "waiting") {
      const text = waiting.length === 1 ? `${waiting[0].title} needs you` : `${waiting.length} tasks need you`;
      return `<span class="island-pulse amber" aria-hidden="true"></span><span class="island-text">${esc(text)}</span>`;
    }
    const idle = month && month.limit ? `Ready · ${percent(month.share)} of the month` : "Ready";
    return `${month && month.limit ? ring("month", month.share || 0, tone(month.share || 0), 16) : `<span class="island-dot" aria-hidden="true"></span>`}<span class="island-text">${esc(idle)}</span>`;
  }

  function render() {
    const island = root();
    if (!island) return;
    let inner = island.querySelector(".island-body");
    if (!inner) {
      inner = document.createElement("div");
      inner.className = "island-body";
      island.append(inner);
    }
    const before = island.getBoundingClientRect();
    const now = state();
    island.dataset.state = now;
    island.dataset.waiting = String(waiting.length > 0);
    island.setAttribute("aria-expanded", String(now === "expanded"));
    island.setAttribute(
      "aria-label",
      tasks.length || waiting.length
        ? `${tasks.length} working, ${waiting.length} waiting for you. Show them.`
        : "Office status"
    );
    inner.innerHTML = body(now);
    settleRings(inner);
    const width = Math.ceil(inner.offsetWidth);
    const height = Math.ceil(inner.offsetHeight);
    const size = (values) => {
      island.style.width = `${values.w}px`;
      island.style.height = `${values.h}px`;
    };
    if (!before.width) size({ w: width, h: height });
    else Motion.spring({ w: before.width, h: before.height }, { w: width, h: height }, size, { preset: "bouncy", key: "island" });
    if (!Motion.reduced() && inner.animate) {
      inner.animate([{ opacity: 0, filter: "blur(4px)" }, { opacity: 1, filter: "blur(0)" }], { duration: 240, easing: "ease-out" });
    }
  }

  function show(next) {
    message = next;
    expanded = false;
    history = [...history, next].slice(-20);
    clearTimeout(timer);
    render();
    timer = setTimeout(() => {
      message = null;
      if (queue.length) show(queue.shift());
      else render();
    }, next.duration ?? (next.error ? ERROR_MS : MESSAGE_MS));
  }

  // Said now, whatever else is waiting to be said: what the owner just did.
  function say(text, isError = false, duration) {
    queue = [];
    show({ text: String(text ?? ""), error: Boolean(isError), duration });
  }

  // Said in turn: what the office noticed.
  function announce(next) {
    if (sound()) chime(next.kind);
    if (message) queue.push(next);
    else show(next);
  }

  function live(list) {
    pulse({ running: (Array.isArray(list) ? list : []).map((card) => ({ ...card, title: card.title || card.objective })) }, { quiet: true });
  }

  function seenAlerts() {
    try {
      return new Set(JSON.parse(stored(SEEN, "[]")));
    } catch {
      return new Set();
    }
  }

  function alertText(alert) {
    const share = percent(alert.share);
    if (alert.scope === "provider") return alert.threshold >= 100 ? `${alert.name} has run out.` : `${alert.name} is running low.`;
    if (alert.scope === "office") return `The office has used ${share} of this month's limit.`;
    if (alert.scope === "task") return `${alert.name} has used ${share} of its budget.`;
    return `${alert.name} has used ${share} of its monthly limit.`;
  }

  function pulse(data, options = {}) {
    const running = Array.isArray(data?.running) ? data.running : [];
    const waits = Array.isArray(data?.waiting) ? data.waiting : [];
    const finished = Array.isArray(data?.finished) ? data.finished : [];
    tasks = running.map((task) => ({ ...task, title: task.title || "A task" }));
    waiting = waits.map((task) => ({ ...task, title: task.title || "A task" }));
    if (data?.month) month = data.month;
    const said = [];
    if (!options.quiet) {
      // A change, not a state: nothing is announced from the first reading.
      if (previous) {
        const before = new Set([...previous.running, ...previous.waiting]);
        for (const task of finished) {
          if (before.has(task.run_id) && !waiting.some((w) => w.run_id === task.run_id)) {
            said.push({ text: `${task.title} ${ENDED[task.status] || "finished."}`, kind: "done", href: `#task/${task.run_id}` });
          }
        }
        for (const task of waiting) {
          if (!previous.waiting.has(task.run_id)) {
            const reason = String(task.reason || "needs you");
            said.push({ text: `${task.title}: ${reason.charAt(0).toLowerCase()}${reason.slice(1)}.`, kind: "waiting", href: `#task/${task.run_id}` });
          }
        }
      }
      // A limit crossed is an event: said once, even on the first reading.
      const seen = seenAlerts();
      const fresh = (Array.isArray(data?.alerts) ? data.alerts : []).filter((alert) => !seen.has(alert.id));
      fresh.slice(0, 2).forEach((alert) => said.push({ text: alertText(alert), kind: "alert", href: alert.scope === "task" ? `#task/${alert.subject}` : "#settings" }));
      fresh.forEach((alert) => seen.add(alert.id));
      if (fresh.length) store(SEEN, JSON.stringify([...seen].slice(-200)));
      previous = { running: new Set(tasks.map((t) => t.run_id)), waiting: new Set(waiting.map((t) => t.run_id)) };
    }
    if (!message) render();
    said.forEach(announce);
  }

  function sound(on) {
    if (on === undefined) return stored(SOUND, "off") === "on";
    store(SOUND, on ? "on" : "off");
    if (expanded) render();
    return Boolean(on);
  }

  // A short chime, made here rather than loaded: two soft notes, lower for
  // something that needs you.
  let audio = null;
  function chime(kind) {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      const notes = kind === "waiting" || kind === "alert" ? [659, 523] : [784, 1047];
      notes.forEach((frequency, index) => {
        const tone = audio.createOscillator();
        const gain = audio.createGain();
        const at = audio.currentTime + index * 0.12;
        tone.frequency.value = frequency;
        tone.type = "sine";
        gain.gain.setValueAtTime(0.0001, at);
        gain.gain.exponentialRampToValueAtTime(0.12, at + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.32);
        tone.connect(gain).connect(audio.destination);
        tone.start(at);
        tone.stop(at + 0.34);
      });
    } catch (error) {
      console.warn("The chime could not play:", error);
    }
  }

  function toggle() {
    if (message) return;
    expanded = !expanded;
    render();
  }

  function collapse() {
    if (!expanded) return;
    expanded = false;
    render();
  }

  // For a fresh start (and the tests): forget what was seen on this page.
  function reset() {
    previous = null;
    history = [];
    queue = [];
    message = null;
    clearTimeout(timer);
    render();
  }

  document.addEventListener("click", (event) => {
    const island = root();
    if (!island) return;
    if (island.contains(event.target)) {
      if (event.target.closest("[data-island-sound]")) {
        sound(!sound());
        return;
      }
      if (event.target.closest("a")) {
        setTimeout(collapse, 0);
        return;
      }
      if (message?.href) {
        location.hash = message.href;
        return;
      }
      toggle();
      return;
    }
    collapse();
  });
  document.addEventListener("keydown", (event) => {
    const island = root();
    if (event.key === "Escape") collapse();
    else if ((event.key === "Enter" || event.key === " ") && island && document.activeElement === island) {
      event.preventDefault();
      toggle();
    }
  });
  document.addEventListener("DOMContentLoaded", render);
  if (document.readyState !== "loading") setTimeout(render, 0);

  return { say, live, pulse, balances, toggle, collapse, reset, sound, history: () => history.slice(), ring };
})();

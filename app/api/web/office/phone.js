// Two places the phone's tab bar reaches:
// Threads, each space's threads with their state, as the sidebar lists them on a
// computer; and More, what does not fit the bar. Both work on a computer too.

async function renderThreads(content) {
  const data = await api("/office/sidebar");
  Office.sidebar = data;
  const spaces = Array.isArray(data?.spaces) ? data.spaces : [];
  const body = spaces.length
    ? spaces
        .map(
          (space) =>
            `<section class="threads-space" aria-label="${esc(space.name)}"><p class="side-label">${esc(space.name)}</p>${(space.threads || [])
              .map((thread) => threadRow(thread, ""))
              .join("")}${space.more ? `<a class="side-more" href="#discussions">${esc(space.more)} more</a>` : ""}</section>`
        )
        .join("")
    : emptyCard("No threads yet.", "Start one on the Desk: say anything.");
  content.innerHTML =
    heading("THREADS", "Your threads.", "What needs you or is working first, then the rest, by space.", `<div class="button-row">${btn("New thread", "thread-new", "primary")}</div>`) +
    `<div class="threads-list">${body}</div>`;
}

// What the bar has no room for, one tap away.
const MORE_PLACES = [
  ["#calendar", "Calendar", "Your day, and what the office plans around it."],
  ["#projects", "Spaces", "Your business spaces and their projects."],
  ["#idea", "Ideas", "Keep a thought for later, and see what is planned."],
  ["#handbook", "Help", "How the office works, and its health now."],
];

function renderMore(content) {
  const places = MORE_PLACES.map(
    ([href, name, about]) => `<a class="card more-place" href="${href}"><strong>${esc(name)}</strong><span class="muted">${esc(about)}</span></a>`
  ).join("");
  content.innerHTML =
    heading("MORE", "Everything else.", "") +
    `<div class="more-places">${places}<button type="button" class="card more-place" data-action="settings-open"><strong>Settings</strong><span class="muted">Team, models and spending, rules, folders, connections.</span></button></div>
     <div class="button-row spaced">${btn("Sign out", "sign-out")}</div>`;
}

Office.screens.threads = { title: "Threads", render: renderThreads, refresh: 30000 };
Office.screens.more = { title: "More", render: renderMore };

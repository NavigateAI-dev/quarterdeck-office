"use strict";

// A task's events, read by cursor. The API answers at most EVENT_PAGE events
// per request, so reading "all events" on every refresh showed a long task's
// first page and nothing after it. This keeps what it has read and asks only
// for what came after the last cursor it holds.

// Must match the limit of GET /runs/{id}/events.
const EVENT_PAGE = 500;

class EventReader {
  constructor(fetchPage, pageSize = EVENT_PAGE) {
    this.fetchPage = fetchPage;
    this.pageSize = pageSize;
    this.runId = null;
    this.events = [];
    this.cursor = 0;
  }

  async read(runId) {
    if (runId !== this.runId) {
      // Another task: nothing read for the last one applies to this one.
      this.runId = runId;
      this.events = [];
      this.cursor = 0;
    }
    for (;;) {
      const page = await this.fetchPage(runId, this.cursor);
      if (!Array.isArray(page)) {
        console.error("Invalid events response, keeping what was read:", page);
        break;
      }
      // A task left while this was in flight: its events are not this one's.
      if (runId !== this.runId) break;
      this.events.push(...page);
      if (page.length) this.cursor = page[page.length - 1].cursor;
      if (page.length < this.pageSize) break;
    }
    return this.events;
  }
}

// A project room, read by its two cursors: one for what was said and one for
// what its tasks did. Each refresh asks only for what came after both, and
// keeps reading while the room says there is more.
class RoomReader {
  constructor(fetchPage) {
    this.fetchPage = fetchPage;
    this.reset(null);
  }

  reset(projectId) {
    this.projectId = projectId;
    this.items = [];
    this.cursor = { after_event: 0, after_message: 0 };
    this.replying = [];
    // What the project's tasks wait on. Not paged: each read gives it whole.
    this.waiting = [];
    // The work handed off in a discussion, as it stands now. Whole.
    this.work = [];
  }

  async read(projectId) {
    if (projectId !== this.projectId) this.reset(projectId);
    for (;;) {
      const page = await this.fetchPage(projectId, this.cursor);
      if (!page || !Array.isArray(page.items) || !page.cursor) {
        console.error("Invalid room response, keeping what was read:", page);
        break;
      }
      // A room left while this was in flight: its items are not this one's.
      if (projectId !== this.projectId) break;
      this.items.push(...page.items);
      this.cursor = page.cursor;
      this.replying = Array.isArray(page.replying) ? page.replying : [];
      this.waiting = Array.isArray(page.waiting) ? page.waiting : [];
      this.work = Array.isArray(page.work) ? page.work : [];
      if (!page.more) break;
    }
    return this;
  }
}

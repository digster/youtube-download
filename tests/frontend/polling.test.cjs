const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const staticDir = path.resolve(__dirname, "../../src/youtube_download/static");

// Run the shipped scripts with a deterministic clock and local API fixtures.
// Browser QA covers the real DOM; these tests cover request timing and races
// without slow wall-clock sleeps or calls to a user's server/account.
async function harness({ detail = false, hidden = false } = {}) {
  let now = 0,
    timerId = 0;
  const timers = new Map(),
    nodes = new Map(),
    listeners = new Map(),
    windowListeners = new Map();
  const requests = [];
  const playlist = {
    id: "custom_fixture",
    source: "custom",
    title: "Fixture",
    state: "idle",
    metadata: { state: "idle" },
    total: 0,
    downloaded: 0,
    local_count: 0,
    bytes: 0,
  };
  const fixture = {
    playlist,
    login: { phase: "idle" },
    fail: false,
    hold: null,
  };
  const document = {
    hidden,
    activeElement: null,
    querySelector(selector) {
      if (!nodes.has(selector))
        nodes.set(selector, {
          innerHTML: "",
          textContent: "",
          open: false,
          hidden: false,
          classList: { add() {}, toggle() {} },
          addEventListener() {},
          setAttribute() {},
          querySelector() {
            return null;
          },
        });
      return nodes.get(selector);
    },
    querySelectorAll() {
      return [];
    },
    addEventListener(name, callback) {
      const callbacks = listeners.get(name) || [];
      listeners.set(name, [...callbacks, callback]);
    },
  };
  const setTimer = (callback, delay, repeat = false) => {
    const id = ++timerId;
    timers.set(id, { callback, time: now + delay, delay, repeat });
    return id;
  };
  const context = vm.createContext({
    document,
    console,
    URL,
    URLSearchParams,
    CSS: { escape: (v) => v },
    window: {
      addEventListener(name, fn) {
        windowListeners.set(name, fn);
      },
    },
    location: {
      pathname: detail ? "/playlists/custom_fixture" : "/",
      search: "",
    },
    setTimeout: (fn, delay) => setTimer(fn, delay),
    clearTimeout: (id) => timers.delete(id),
    setInterval: (fn, delay) => setTimer(fn, delay, true),
    clearInterval: (id) => timers.delete(id),
    fetch: async (url) => {
      requests.push({ url, time: now });
      if (url === "/api/auth/start")
        fixture.login = { phase: "waiting", configured: true };
      if (url === "/api/auth/cancel")
        fixture.login = { phase: "cancelled", configured: true };
      // Capture the response before blocking, as a real GET may return stale
      // state when a later mutation finishes before that GET does.
      const data = JSON.parse(
        JSON.stringify(
          url === "/api/status"
            ? { account: { authenticated: false }, login: fixture.login }
            : url.startsWith("/api/auth/")
              ? fixture.login
              : detail
                ? { playlist: fixture.playlist, videos: [] }
                : { playlists: [fixture.playlist] },
        ),
      );
      if (fixture.hold && url.startsWith("/api/playlists")) await fixture.hold;
      if (fixture.holdAuth && url === "/api/auth/status")
        await fixture.holdAuth;
      if (fixture.fail) throw new Error("offline");
      return { ok: true, json: async () => data };
    },
  });
  const polling = path.join(staticDir, "polling.js");
  if (fs.existsSync(polling))
    vm.runInContext(fs.readFileSync(polling, "utf8"), context);
  vm.runInContext(
    fs.readFileSync(path.join(staticDir, "app.js"), "utf8"),
    context,
  );
  // Rendering needs native DOM nodes. The browser regression exercises it
  // through these same poll ticks while this harness isolates the scheduler.
  vm.runInContext("render = () => {}", context);
  const flush = async () => {
    for (let i = 0; i < 30; i++) await Promise.resolve();
  };
  await flush();
  return {
    fixture,
    requests,
    document,
    timers,
    run: (code) => vm.runInContext(code, context),
    async visibility(hidden) {
      document.hidden = hidden;
      for (const callback of listeners.get("visibilitychange") || [])
        callback();
      await flush();
    },
    async pageEvent(name, event = {}) {
      windowListeners.get(name)?.(event);
      await flush();
    },
    async advance(ms) {
      const end = now + ms;
      while (true) {
        const next = [...timers].sort((a, b) => a[1].time - b[1].time)[0];
        if (!next || next[1].time > end) break;
        now = next[1].time;
        timers.delete(next[0]);
        if (next[1].repeat)
          timers.set(next[0], { ...next[1], time: now + next[1].delay });
        next[1].callback();
        await flush();
      }
      now = end;
      await flush();
    },
    flush,
    libraryRequests() {
      return requests.filter((r) => r.url.startsWith("/api/playlists"));
    },
  };
}

test("idle library stops fetching every two seconds", async () => {
  const h = await harness();
  await h.advance(29999);
  assert.equal(h.libraryRequests().length, 1);
  await h.advance(1);
  assert.deepEqual(
    h.libraryRequests().map((r) => r.time),
    [0, 30000],
  );
});

for (const detail of [false, true]) {
  for (const kind of ["download", "metadata"]) {
    test(`${detail ? "detail" : "overview"}: ${kind} queue runs fast, completion returns to idle`, async () => {
      const h = await harness({ detail });
      const job =
        kind === "download" ? h.fixture.playlist : h.fixture.playlist.metadata;
      job.state = "queued";
      await h.run("reload(true)");
      const before = h.libraryRequests().length;
      await h.advance(1999);
      assert.equal(h.libraryRequests().length, before);
      job.state = "running";
      await h.advance(1);
      assert.equal(h.libraryRequests().length, before + 1);
      await h.advance(2000);
      assert.equal(h.libraryRequests().length, before + 2);
      job.state = "completed";
      await h.advance(2000);
      const finished = h.libraryRequests().length;
      await h.advance(29999);
      assert.equal(h.libraryRequests().length, finished);
      await h.advance(1);
      assert.equal(h.libraryRequests().length, finished + 1);
    });
  }
}

test("paused and failed jobs use the idle interval", async () => {
  const h = await harness({ detail: true });
  for (const state of ["paused", "error", "completed_with_errors"]) {
    h.fixture.playlist.state = state;
    h.fixture.playlist.metadata.state = state;
    await h.run("reload(true)");
    const before = h.libraryRequests().length;
    await h.advance(29999);
    assert.equal(h.libraryRequests().length, before);
    await h.advance(1);
    assert.equal(h.libraryRequests().length, before + 1);
  }
});

test("hidden tabs stop timers and returning refreshes immediately", async () => {
  const h = await harness({ detail: true });
  await h.visibility(true);
  assert.equal(h.timers.size, 0);
  h.fixture.playlist.metadata.state = "running";
  await h.advance(120000);
  assert.equal(h.libraryRequests().length, 1);
  await h.visibility(false);
  assert.equal(h.libraryRequests().length, 2);
  await h.advance(2000);
  assert.equal(h.libraryRequests().length, 3);
});

test("a page first loaded in the background refreshes on its first visit", async () => {
  const h = await harness({ hidden: true });
  assert.equal(h.timers.size, 0);
  await h.advance(60000);
  assert.equal(h.libraryRequests().length, 1);
  await h.visibility(false);
  assert.equal(h.libraryRequests().length, 2);
  await h.advance(30000);
  assert.equal(h.libraryRequests().length, 3);
});

test("pending sign-in also stops while hidden and resumes immediately", async () => {
  const h = await harness();
  h.fixture.login.phase = "waiting";
  h.run('state.login = { phase: "waiting" }; loginPoller.schedule()');
  await h.visibility(true);
  await h.advance(60000);
  assert.equal(
    h.requests.filter((r) => r.url === "/api/auth/status").length,
    0,
  );
  await h.visibility(false);
  assert.equal(
    h.requests.filter((r) => r.url === "/api/auth/status").length,
    1,
  );
  await h.advance(2000);
  assert.equal(
    h.requests.filter((r) => r.url === "/api/auth/status").length,
    2,
  );
});

test("in-flight reads finish while hidden without scheduling another request", async () => {
  const h = await harness();
  let release;
  h.fixture.hold = new Promise((resolve) => {
    release = resolve;
  });
  const read = h.run("reload()");
  await h.flush();
  await h.visibility(true);
  h.fixture.hold = null;
  release();
  await read;
  assert.equal(h.timers.size, 0);
  await h.advance(60000);
  assert.equal(h.libraryRequests().length, 2);
  await h.visibility(false);
  assert.equal(h.libraryRequests().length, 3);
});

test("slow reads do not overlap and the delay starts after completion", async () => {
  const h = await harness();
  let release;
  h.fixture.hold = new Promise((resolve) => {
    release = resolve;
  });
  await h.advance(30000);
  await h.advance(60000);
  assert.equal(h.libraryRequests().length, 2);
  h.fixture.hold = null;
  release();
  await h.flush();
  await h.advance(29999);
  assert.equal(h.libraryRequests().length, 2);
  await h.advance(1);
  assert.equal(h.libraryRequests().length, 3);
});

test("mutations during an old read coalesce into a fresh trailing read", async () => {
  const h = await harness({ detail: true });
  let release;
  h.fixture.hold = new Promise((resolve) => {
    release = resolve;
  });
  const read = h.run("reload()");
  await h.flush();
  h.fixture.playlist.metadata.state = "running";
  const first = h.run("reload(true)");
  const second = h.run("reload(true)");
  assert.equal(first, read);
  assert.equal(second, read);
  assert.equal(h.libraryRequests().length, 2);
  h.fixture.hold = null;
  release();
  await first;
  assert.equal(h.libraryRequests().length, 3);
  assert.equal(h.run("state.playlist.metadata.state"), "running");
  await h.advance(2000);
  assert.equal(h.libraryRequests().length, 4);
});

test("server failures back off even when the last snapshot is active", async () => {
  const h = await harness();
  h.fixture.playlist.state = "running";
  await h.run("reload(true)");
  h.fixture.fail = true;
  await h.advance(2000);
  const failed = h.libraryRequests().length;
  assert.equal(
    h.document.querySelector("#connection").innerHTML,
    "Connection interrupted",
  );
  await h.advance(29999);
  assert.equal(h.libraryRequests().length, failed);
  h.fixture.fail = false;
  await h.advance(1);
  assert.equal(h.libraryRequests().length, failed + 1);
  await h.advance(2000);
  assert.equal(h.libraryRequests().length, failed + 2);
});

test("pending sign-in polls quickly without polling the idle library", async () => {
  const h = await harness();
  for (const phase of ["waiting", "exchanging"]) {
    h.fixture.login.phase = phase;
    h.run(`state.login = { phase: "${phase}" }; loginPoller.schedule()`);
    const before = h.requests.filter(
      (r) => r.url === "/api/auth/status",
    ).length;
    await h.advance(6000);
    assert.equal(
      h.requests.filter((r) => r.url === "/api/auth/status").length,
      before + 3,
    );
    assert.equal(h.libraryRequests().length, 1);
  }
  h.fixture.login.phase = "cancelled";
  await h.advance(2000);
  const finished = h.requests.filter(
    (r) => r.url === "/api/auth/status",
  ).length;
  await h.advance(60000);
  assert.equal(
    h.requests.filter((r) => r.url === "/api/auth/status").length,
    finished,
  );
});

test("idle setup polls slowly, closing setup removes its timer", async () => {
  const h = await harness();
  h.document.querySelector("#setup-dialog").open = true;
  await h.run("refreshLogin()");
  await h.advance(29999);
  assert.equal(
    h.requests.filter((r) => r.url === "/api/auth/status").length,
    1,
  );
  await h.advance(1);
  assert.equal(
    h.requests.filter((r) => r.url === "/api/auth/status").length,
    2,
  );
  h.document.querySelector("#setup-dialog").open = false;
  h.run("loginPoller.schedule()");
  await h.advance(60000);
  assert.equal(
    h.requests.filter((r) => r.url === "/api/auth/status").length,
    2,
  );
});

test("page-cache restoration resumes polling with fresh data", async () => {
  const h = await harness();
  await h.pageEvent("pagehide");
  await h.advance(60000);
  assert.equal(h.libraryRequests().length, 1);
  await h.pageEvent("pageshow", { persisted: true });
  assert.equal(h.libraryRequests().length, 2);
  // Some browsers dispatch both events when restoring a cached page.
  await h.visibility(false);
  assert.equal(h.libraryRequests().length, 2);
  await h.advance(30000);
  assert.equal(h.libraryRequests().length, 3);
});

for (const action of ["start", "cancel"]) {
  test(`a stale sign-in status cannot overwrite ${action}`, async () => {
    const h = await harness();
    if (action === "cancel") {
      h.fixture.login.phase = "waiting";
      h.run('state.login = { phase: "waiting" }');
    }
    let release;
    h.fixture.holdAuth = new Promise((resolve) => {
      release = resolve;
    });
    const read = h.run("refreshLogin()");
    await h.flush();
    await h.run(`loginAction("auth-${action}")`);
    h.fixture.holdAuth = null;
    release();
    await read;
    assert.equal(
      h.run("state.login.phase"),
      action === "start" ? "waiting" : "cancelled",
    );
    await h.advance(2000);
    assert.equal(
      h.requests.filter((r) => r.url === "/api/auth/status").length,
      action === "start" ? 2 : 1,
    );
  });
}

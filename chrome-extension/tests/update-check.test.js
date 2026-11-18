const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const Updates = require("../shared/update-check.js");

// The same tables are mirrored in tests/test_update_check.py.
const NEWER = [["0.4.2", "0.4.1"], ["0.5.0", "0.4.9"], ["1.0.0", "0.99.99"], ["v0.10.0", "0.9.0"],
  ["0.4.1.1", "0.4.1"], ["1.0.0", "1.0.0-rc.1"], ["1.0.0-rc.2", "1.0.0-rc.1"],
  ["1.0.0-rc.10", "1.0.0-rc.2"], ["1.0.0-rc.1", "1.0.0-beta"], ["1.0.0-alpha.1", "1.0.0-alpha"]];
const NOT_NEWER = [["0.4.1", "0.4.1"], ["0.4.0", "0.4.1"], ["v0.4.1", "0.4.1"], ["1.0.0-rc.1", "1.0.0"],
  ["1.0.0+build.5", "1.0.0"], ["0.4.1.0", "0.4.1"], ["0.4.9", "0.5.0"]];
const INVALID = ["", null, undefined, "latest", "1.2", "v", "1.2.3.4.5", "1.2.x", "1.2.3-", "1..3", 5];

test("newer versions are newer, in both directions", () => {
  for (const [candidate, current] of NEWER) {
    assert.equal(Updates.isNewer(candidate, current), true, `${candidate} > ${current}`);
    assert.equal(Updates.isNewer(current, candidate), false, `${current} < ${candidate}`);
  }
});

test("equal or older versions are not newer", () => {
  for (const [candidate, current] of NOT_NEWER) assert.equal(Updates.isNewer(candidate, current), false, `${candidate} vs ${current}`);
});

test("anything that is not a version never counts as newer", () => {
  for (const bad of INVALID) {
    assert.equal(Updates.parseVersion(bad), null, String(bad));
    assert.equal(Updates.isNewer(bad, "0.4.1"), false);
    assert.equal(Updates.isNewer("9.9.9", bad), false);
    assert.equal(Updates.compareVersions(bad, "0.4.1"), null);
  }
});

const payload = (overrides = {}) => ({
  tag_name: "v0.5.0", name: "Live Subtitle v0.5.0", draft: false, prerelease: false,
  html_url: "https://github.com/xu-0306/live-subtitle/releases/tag/v0.5.0",
  published_at: "2026-12-01T00:00:00Z", body: "# What's new", ...overrides,
});

test("parseRelease reads the GitHub shape", () => {
  assert.deepEqual(Updates.parseRelease(payload()), {
    version: "0.5.0", tag: "v0.5.0",
    url: "https://github.com/xu-0306/live-subtitle/releases/tag/v0.5.0", published: "2026-12-01T00:00:00Z",
  });
});

test("parseRelease rejects drafts, pre-releases and bad data", () => {
  for (const bad of [null, [], "x", {}, payload({ draft: true }), payload({ prerelease: true }),
    payload({ tag_name: "latest" }), payload({ tag_name: null }), payload({ tag_name: 5 })]) {
    assert.equal(Updates.parseRelease(bad), null, JSON.stringify(bad));
  }
});

test("the download link must be an https github.com page", () => {
  for (const url of [null, 5, "", "http://github.com/x/y", "https://evil.example/x",
    "https://github.com.evil.example/x", "javascript:alert(1)", "file:///C:/x"]) {
    assert.equal(Updates.parseRelease(payload({ html_url: url })).url, Updates.RELEASES_PAGE, String(url));
  }
});

function fakeFetch(reply, calls = []) {
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return typeof reply === "function" ? reply(url, init) : reply;
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}
const respond = (body, status = 200) => ({
  ok: status >= 200 && status < 300, status,
  text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
});

test("the request is a plain GET of the release JSON: no cookies, no referrer, no body", async () => {
  const fetchImpl = fakeFetch(respond(payload()));
  const release = await Updates.fetchLatestRelease(fetchImpl);
  assert.equal(release.version, "0.5.0");
  const [{ url, init }] = fetchImpl.calls;
  assert.equal(url, "https://api.github.com/repos/xu-0306/live-subtitle/releases/latest");
  assert.deepEqual(Object.keys(init.headers), ["Accept"]);
  assert.equal(init.credentials, "omit");
  assert.equal(init.referrerPolicy, "no-referrer");
  assert.equal(init.method, undefined);
  assert.equal(init.body, undefined);
});

test("HTTP errors become short messages", async () => {
  for (const [status, message] of [[404, /No release/], [403, /limiting/], [429, /limiting/], [500, /HTTP 500/]]) {
    await assert.rejects(Updates.fetchLatestRelease(fakeFetch(respond("", status))), message, String(status));
  }
});

test("offline, a refused answer and a hung server all end in a quiet error", async () => {
  await assert.rejects(Updates.fetchLatestRelease(fakeFetch(() => { throw new TypeError("Failed to fetch"); })), /Could not reach GitHub/);
  for (const body of ["not json", "[]", '{"tag_name":"nightly"}', "x".repeat(1000001)]) {
    await assert.rejects(Updates.fetchLatestRelease(fakeFetch(respond(body))), /does not understand/, body.slice(0, 20));
  }
  const hung = fakeFetch((_url, init) => new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(new Error("aborted")))));
  await assert.rejects(Updates.fetchLatestRelease(hung, 20), /Could not reach GitHub/);
});

function storageArea(initial = {}) {
  const data = { ...initial };
  return {
    data,
    get(keys, callback) {
      const result = {};
      for (const key of Array.isArray(keys) ? keys : [keys]) if (key in data) result[key] = data[key];
      callback(result);
    },
    set(values, callback) { Object.assign(data, values); callback?.(); },
  };
}
const DAY = 24 * 3600 * 1000;
const HOUR = 3600 * 1000;

test("isDue: daily after a success, hourly after a failure, never when switched off", () => {
  const now = 10 * DAY;
  assert.equal(Updates.isDue({}, undefined, now), true);
  assert.equal(Updates.isDue({ checkedAt: now - HOUR, ok: true }, undefined, now), false);
  assert.equal(Updates.isDue({ checkedAt: now - DAY, ok: true }, undefined, now), true);
  assert.equal(Updates.isDue({ checkedAt: now - 600000, ok: false }, undefined, now), false);
  assert.equal(Updates.isDue({ checkedAt: now - HOUR, ok: false }, undefined, now), true);
  assert.equal(Updates.isDue({ checkedAt: now + 5000, ok: true }, undefined, now), true);
  assert.equal(Updates.isDue({ checkedAt: "yesterday" }, undefined, now), true);
  assert.equal(Updates.isDue({}, false, now), false);
});

test("runCheck records a new release, then stays quiet until the next check is due", async () => {
  const storage = storageArea();
  const fetchImpl = fakeFetch(respond(payload()));
  const first = await Updates.runCheck({ storage, fetchImpl, now: 1000 });
  assert.equal(first.ok, true);
  assert.equal(first.latest.version, "0.5.0");
  assert.deepEqual(storage.data.updateCheck, first);
  await Updates.runCheck({ storage, fetchImpl, now: 1000 + HOUR });
  assert.equal(fetchImpl.calls.length, 1, "an hour later nothing is asked");
  await Updates.runCheck({ storage, fetchImpl, now: 1000 + DAY });
  assert.equal(fetchImpl.calls.length, 2, "a day later it asks again");
  await Updates.runCheck({ storage, fetchImpl, now: 1000 + DAY + 10, force: true });
  assert.equal(fetchImpl.calls.length, 3, "force always asks");
});

test("a failed check keeps the last good answer and retries after an hour", async () => {
  const storage = storageArea({ updateCheck: { checkedAt: 1000, ok: true, error: "", latest: { version: "0.5.0", tag: "v0.5.0", url: Updates.RELEASES_PAGE, published: "" } } });
  const offline = fakeFetch(() => { throw new TypeError("Failed to fetch"); });
  const failed = await Updates.runCheck({ storage, fetchImpl: offline, now: 1000 + DAY });
  assert.equal(failed.ok, false);
  assert.equal(failed.error, "Could not reach GitHub.");
  assert.equal(failed.latest.version, "0.5.0", "the banner already showing must not vanish");
  await Updates.runCheck({ storage, fetchImpl: offline, now: 1000 + DAY + 600000 });
  assert.equal(offline.calls.length, 1, "ten minutes after a failure it waits");
  await Updates.runCheck({ storage, fetchImpl: offline, now: 1000 + DAY + HOUR });
  assert.equal(offline.calls.length, 2, "an hour after a failure it retries");
});

test("switched off: no automatic check, but Check now still works", async () => {
  const storage = storageArea({ updateCheckEnabled: false });
  const fetchImpl = fakeFetch(respond(payload()));
  await Updates.runCheck({ storage, fetchImpl, now: 1000 });
  assert.equal(fetchImpl.calls.length, 0);
  assert.equal(storage.data.updateCheck, undefined);
  await Updates.runCheck({ storage, fetchImpl, now: 1000, force: true });
  assert.equal(fetchImpl.calls.length, 1);
});

test("runCheck never rejects, even when storage throws", async () => {
  const broken = { get() { throw new Error("storage gone"); }, set() { throw new Error("storage gone"); } };
  const state = await Updates.runCheck({ storage: broken, fetchImpl: fakeFetch(respond(payload())), now: 5 });
  assert.equal(state.ok, true);
});

test("describe and summary: news only while the release is newer than this build", () => {
  const state = { checkedAt: 1000, ok: true, error: "", latest: { version: "0.5.0", url: "https://github.com/xu-0306/live-subtitle/releases/tag/v0.5.0" } };
  const news = Updates.describe(state, "", "0.4.1");
  assert.equal(news.available, true);
  assert.equal(news.version, "0.5.0");
  assert.equal(news.dismissed, false);
  assert.equal(Updates.describe(state, "0.5.0", "0.4.1").dismissed, true);
  assert.equal(Updates.describe(state, "0.4.9", "0.4.1").dismissed, false, "an older dismissal does not hide a newer release");
  const updated = Updates.describe(state, "", "0.5.0");
  assert.equal(updated.available, false, "after updating, the banner goes away without another check");
  assert.equal(updated.url, Updates.RELEASES_PAGE);
  assert.equal(Updates.describe(undefined, "", "0.4.1").available, false);
  assert.match(Updates.summary(news), /^Last check \d{4}-\d\d-\d\d \d\d:\d\d: version 0\.5\.0 is available\.$/);
  assert.match(Updates.summary(updated), /this is the latest version\.$/);
  assert.match(Updates.summary({ ...news, ok: false, error: "Could not reach GitHub." }), /Could not reach GitHub\.$/);
  assert.equal(Updates.summary(Updates.describe(undefined, "", "0.4.1")), "Not checked yet.");
});

test("manifest: only alarms was added; no new host permission; the version is the single source", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "../manifest.json"), "utf8"));
  // "alarms" wakes the sleeping service worker for the daily update check and shows no install warning.
  assert.deepEqual(manifest.permissions, ["tabCapture", "activeTab", "offscreen", "storage", "scripting", "alarms"]);
  assert.deepEqual(manifest.host_permissions, ["<all_urls>"]);
  assert.ok(Updates.parseVersion(manifest.version), manifest.version);
});

function loadBackground({ alarms = [], fetchImpl, withUpdates = true, withAlarms = true, storage = {} } = {}) {
  const listeners = { message: [], alarm: [], startup: [], installed: [] };
  const created = [];
  const store = storageArea(storage);
  const listen = (list) => ({ addListener: (fn) => list.push(fn) });
  const chrome = {
    runtime: {
      getURL: (file) => `chrome-extension://test/${file}`,
      getManifest: () => ({ version: "0.4.1" }),
      onMessage: listen(listeners.message),
      onStartup: listen(listeners.startup),
      onInstalled: listen(listeners.installed),
      sendMessage() {},
    },
    storage: { local: store },
    tabs: { onUpdated: listen([]) },
    ...(withAlarms ? { alarms: {
      get: (name, callback) => callback(alarms.find((alarm) => alarm.name === name)),
      create: (name, info) => created.push({ name, info }),
      onAlarm: listen(listeners.alarm),
    } } : {}),
  };
  const context = vm.createContext({
    chrome, console, URL, AbortController, setTimeout, clearTimeout, fetch: fetchImpl,
    importScripts: (...files) => {
      for (const file of files) {
        if (withUpdates && file.endsWith("update-check.js")) {
          vm.runInContext(fs.readFileSync(path.join(__dirname, "../shared/update-check.js"), "utf8"), context);
        }
      }
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../background/background.js"), "utf8"), context, { filename: "background.js" });
  return { listeners, created, store, context };
}
// Objects built inside the vm context have another Object.prototype, which deepStrictEqual rejects.
const plain = (value) => JSON.parse(JSON.stringify(value));
const settle = async (until) => { for (let i = 0; i < 100 && !until(); i += 1) await new Promise(setImmediate); };
const askWorker = (listeners, message) => new Promise((resolve) => {
  assert.equal(listeners.message.at(-1)(message, {}, resolve), true);
});

test("service worker: creates the six-hourly alarm only when it is missing", () => {
  const fresh = loadBackground({ fetchImpl: fakeFetch(respond(payload())) });
  assert.deepEqual(plain(fresh.created), [{ name: "update-check", info: { delayInMinutes: 1, periodInMinutes: 360 } }]);
  const existing = loadBackground({ alarms: [{ name: "update-check" }], fetchImpl: fakeFetch(respond(payload())) });
  assert.deepEqual(plain(existing.created), []);
});

test("service worker: the alarm, start-up and install each trigger a throttled check", async () => {
  const fetchImpl = fakeFetch(respond(payload()));
  const { listeners, store } = loadBackground({ fetchImpl });
  listeners.alarm[0]({ name: "something-else" });
  await new Promise(setImmediate);
  assert.equal(fetchImpl.calls.length, 0, "other alarms are ignored");
  listeners.alarm[0]({ name: "update-check" });
  await settle(() => store.data.updateCheck);
  assert.equal(store.data.updateCheck.latest.version, "0.5.0");
  listeners.startup[0]();
  listeners.installed[0]();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(fetchImpl.calls.length, 1, "start-up and install right after a check do not ask again");
});

test("service worker: update-check message answers from storage, forces on request, shares one request", async () => {
  const fetchImpl = fakeFetch(respond(payload()));
  const { listeners } = loadBackground({ fetchImpl });
  const [a, b] = await Promise.all([askWorker(listeners, { type: "update-check" }), askWorker(listeners, { type: "update-check" })]);
  assert.equal(fetchImpl.calls.length, 1, "two simultaneous askers share one request");
  assert.equal(a.ok && b.ok, true);
  assert.equal(a.state.latest.version, "0.5.0");
  await askWorker(listeners, { type: "update-check" });
  assert.equal(fetchImpl.calls.length, 1, "the popup asking again is answered from storage");
  await askWorker(listeners, { type: "update-check", force: true });
  assert.equal(fetchImpl.calls.length, 2, "Check now forces a request");
});

test("service worker: offline is a quiet answer, not an error", async () => {
  const offline = fakeFetch(() => { throw new TypeError("Failed to fetch"); });
  const { listeners, store } = loadBackground({ fetchImpl: offline });
  const reply = await askWorker(listeners, { type: "update-check", force: true });
  assert.equal(reply.ok, true);
  assert.equal(reply.state.ok, false);
  assert.equal(store.data.updateCheck.error, "Could not reach GitHub.");
});

test("service worker: loads fine without the update script or the alarms API", async () => {
  const bare = loadBackground({ withUpdates: false, withAlarms: false, fetchImpl: fakeFetch(respond(payload())) });
  assert.deepEqual(bare.created, []);
  const reply = await askWorker(bare.listeners, { type: "update-check", force: true });
  assert.deepEqual(plain(reply), { ok: false, state: null });
});

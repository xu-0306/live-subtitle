const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const FilesUpdate = require("../shared/files-update.js");
const Updates = require("../shared/update-check.js");

const respond = (body, status = 200) => ({
  ok: status >= 200 && status < 300, status,
  text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
});
const URL_ = "chrome-extension://test/manifest.json";

function recordingFetch(reply) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (typeof reply === "function") return reply(url, init);
    return reply;
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

test("readDiskVersion reads the manifest on disk without the browser cache", async () => {
  const fetchImpl = recordingFetch(respond({ name: "x", version: "0.6.0" }));
  assert.equal(await FilesUpdate.readDiskVersion(fetchImpl, URL_), "0.6.0");
  const [{ url, init }] = fetchImpl.calls;
  assert.match(url, /^chrome-extension:\/\/test\/manifest\.json\?t=\d+$/);
  assert.equal(init.cache, "no-store");
});

test("readDiskVersion is empty for anything it cannot trust", async () => {
  for (const reply of [respond("", 404), respond("not json"), respond("[]"), respond({}), respond({ version: 5 }),
    () => { throw new TypeError("Failed to fetch"); }]) {
    assert.equal(await FilesUpdate.readDiskVersion(recordingFetch(reply), URL_), "");
  }
});

const decide = (disk, running, busy) => {
  const isBusy = async () => { isBusy.asked += 1; return busy; };
  isBusy.asked = 0;
  return FilesUpdate.decide({
    fetchImpl: recordingFetch(respond({ version: disk })), manifestUrl: URL_, runningVersion: running,
    isNewer: Updates.isNewer, isBusy,
  }).then((result) => ({ ...result, asked: isBusy.asked }));
};

test("decide: reload when the files on disk are newer and nothing is capturing", async () => {
  assert.deepEqual(await decide("0.6.0", "0.5.1", false), { action: "reload", disk: "0.6.0", asked: 1 });
});

test("decide: wait while a capture is running", async () => {
  assert.deepEqual(await decide("0.6.0", "0.5.1", true), { action: "wait", disk: "0.6.0", asked: 1 });
});

test("decide: same, older or unreadable versions do nothing and never ask about captures", async () => {
  for (const [disk, running] of [["0.5.1", "0.5.1"], ["0.5.0", "0.5.1"], ["garbage", "0.5.1"], ["0.6.0", "garbage"]]) {
    const result = await decide(disk, running, false);
    assert.equal(result.action, "", `${disk} vs ${running}`);
    assert.equal(result.asked, 0);
  }
});

function loadBackground({ diskVersion, running = "0.5.1", captured = [], alarms = [], withReload = true } = {}) {
  const listeners = { alarm: [] };
  const created = [];
  const reloads = [];
  const fetchCalls = [];
  const listen = (list) => ({ addListener: (fn) => list.push(fn) });
  const chrome = {
    runtime: {
      getURL: (file) => `chrome-extension://test/${file}`,
      getManifest: () => ({ version: running }),
      onMessage: listen([]), onStartup: listen([]), onInstalled: listen([]), sendMessage() {},
      ...(withReload ? { reload: () => reloads.push(Date.now()) } : {}),
    },
    storage: { local: { get: (_keys, callback) => callback({}), set: (_values, callback) => callback?.() } },
    tabs: { onUpdated: listen([]) },
    tabCapture: { getCapturedTabs: (callback) => callback(captured.slice()) },
    alarms: {
      get: (name, callback) => callback(alarms.find((alarm) => alarm.name === name)),
      create: (name, info) => created.push({ name, info }),
      onAlarm: listen(listeners.alarm),
    },
  };
  const fetchImpl = async (url, init) => {
    fetchCalls.push({ url, init });
    if (url.startsWith(URL_)) return respond({ version: diskVersion });
    throw new TypeError("Failed to fetch"); // the GitHub update check: offline, which is fine here
  };
  const context = vm.createContext({
    chrome, console, URL, AbortController, setTimeout, clearTimeout, fetch: fetchImpl,
    importScripts: (...files) => {
      for (const file of files) {
        for (const name of ["update-check.js", "files-update.js"]) {
          if (file.endsWith(name)) vm.runInContext(fs.readFileSync(path.join(__dirname, "../shared", name), "utf8"), context);
        }
      }
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../background/background.js"), "utf8"), context, { filename: "background.js" });
  return { listeners, created, reloads, fetchCalls, chrome };
}
const plain = (value) => JSON.parse(JSON.stringify(value));
const settle = async (until) => { for (let i = 0; i < 200 && !until(); i += 1) await new Promise(setImmediate); };
const quiet = () => new Promise((resolve) => setTimeout(resolve, 30));

test("service worker: waking up with newer files on disk reloads the extension once", async () => {
  const { reloads, fetchCalls } = loadBackground({ diskVersion: "0.6.0" });
  await settle(() => reloads.length);
  assert.equal(reloads.length, 1);
  assert.ok(fetchCalls.some((call) => /^chrome-extension:\/\/test\/manifest\.json\?t=\d+$/.test(call.url)));
});

test("service worker: the same version on disk does not reload", async () => {
  const { reloads, fetchCalls } = loadBackground({ diskVersion: "0.5.1" });
  await settle(() => fetchCalls.some((call) => call.url.startsWith(URL_)));
  await quiet();
  assert.equal(reloads.length, 0);
});

test("service worker: a running capture is never interrupted; the next alarm reloads once it has ended", async () => {
  const captured = [{ tabId: 7 }];
  const { reloads, listeners, fetchCalls } = loadBackground({ diskVersion: "0.6.0", captured });
  await settle(() => fetchCalls.some((call) => call.url.startsWith(URL_)));
  await quiet();
  assert.equal(reloads.length, 0, "capturing: wait");
  captured.length = 0;
  for (const listener of listeners.alarm) listener({ name: "extension-files" }); // the update-check one ignores it
  await settle(() => reloads.length);
  assert.equal(reloads.length, 1, "capture over: reload");
});

test("service worker: only its own alarm triggers the files check", async () => {
  const { reloads, listeners, fetchCalls } = loadBackground({ diskVersion: "0.5.1" });
  await settle(() => fetchCalls.some((call) => call.url.startsWith(URL_)));
  const before = fetchCalls.filter((call) => call.url.startsWith(URL_)).length;
  for (const listener of listeners.alarm) listener({ name: "something-else" });
  await quiet();
  assert.equal(fetchCalls.filter((call) => call.url.startsWith(URL_)).length, before);
  assert.equal(reloads.length, 0);
});

test("service worker: the five-minute alarm is created only when missing", () => {
  const fresh = loadBackground({ diskVersion: "0.5.1" });
  assert.deepEqual(plain(fresh.created.filter((alarm) => alarm.name === "extension-files")),
    [{ name: "extension-files", info: { delayInMinutes: 1, periodInMinutes: 5 } }]);
  const existing = loadBackground({ diskVersion: "0.5.1", alarms: [{ name: "extension-files" }] });
  assert.deepEqual(existing.created.filter((alarm) => alarm.name === "extension-files"), []);
});

test("service worker: without runtime.reload nothing is scheduled or checked", async () => {
  const { created, reloads, fetchCalls } = loadBackground({ diskVersion: "0.6.0", withReload: false });
  await quiet();
  assert.deepEqual(created.filter((alarm) => alarm.name === "extension-files"), []);
  assert.equal(reloads.length, 0);
  assert.equal(fetchCalls.filter((call) => call.url.startsWith(URL_)).length, 0);
});

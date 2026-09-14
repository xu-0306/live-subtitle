const assert = require("node:assert/strict");
const test = require("node:test");
const catalog = require("../shared/catalog.js");

function storageArea(initial = {}) {
  const data = { ...initial };
  return {
    data,
    get(keys, callback) {
      const result = {};
      for (const key of Array.isArray(keys) ? keys : [keys]) result[key] = data[key];
      callback(result);
    },
    set(values, callback) {
      Object.assign(data, values);
      callback?.();
    },
  };
}

class FakeSocket {
  static replies = [];
  constructor(url) {
    this.url = url;
    this.listeners = new Map();
    this.sent = [];
    FakeSocket.replies.push(this);
    queueMicrotask(() => this.emit("open", {}));
  }
  addEventListener(type, callback) {
    const list = this.listeners.get(type) || [];
    list.push(callback);
    this.listeners.set(type, list);
  }
  emit(type, event) { for (const callback of this.listeners.get(type) || []) callback(event); }
  send(value) { this.sent.push(JSON.parse(value)); }
  close() { this.closed = true; }
}

function v2(overrides = {}) {
  return {
    type: "desktop_catalog",
    version: 2,
    server_id: "desktop-a",
    instance_id: "instance-1",
    revision: 4,
    models: [{ id: "profile:future", name: "Future", engine: "desktop", selection: { kind: "profile", id: "future" }, available: true, status: "configured", apiKey: "must-not-leak", path: "C:\\private" }],
    stt_models: [{ id: "unseen-stt", name: "Unseen STT", selection: { model: "unseen-stt" }, available: false, status: "missing" }],
    defaults: { stt: { model: "unseen-stt" }, translation: { target_language: "Kiswahili", partial: true } },
    active: { captures: 0, selection: { kind: "profile", id: "future", secret: "drop" } },
    capabilities: ["profile_import"],
    ...overrides,
  };
}

test("v2 normalization strips secrets and requires persistent server identity", () => {
  const normalized = catalog.normalizeCatalog(v2());
  assert.equal(normalized.kind, "v2");
  assert.equal(normalized.models[0].apiKey, undefined);
  assert.equal(normalized.models[0].path, undefined);
  assert.deepEqual(normalized.active.selection, { kind: "profile", id: "future" });
  assert.equal(catalog.normalizeCatalog({ type: "desktop_catalog", version: 2, instance_id: "i" }), null);
  assert.equal(catalog.normalizeCatalog({ type: "desktop_catalog", version: 2, server_id: "s" }), null);
  assert.equal(catalog.normalizeCatalog(v2({ server_id: "unknown-server" })), null);
});

test("cache is partitioned by URL and server instance", async () => {
  const area = storageArea();
  await catalog.writeCache(area, "ws://one.test/asr", catalog.normalizeCatalog(v2()), 100);
  await catalog.writeCache(area, "ws://two.test/asr", catalog.normalizeCatalog(v2({ server_id: "desktop-b", instance_id: "instance-9" })), 200);
  const one = await catalog.readCache(area, "ws://one.test/asr", "desktop-a", "instance-1");
  const two = await catalog.readCache(area, "ws://two.test/asr", "desktop-b", "instance-9");
  assert.equal(one.server_id, "desktop-a");
  assert.equal(two.server_id, "desktop-b");
  assert.equal(await catalog.readCache(area, "ws://one.test/asr", "desktop-b", "instance-9"), null);
  assert.doesNotMatch(JSON.stringify(area.data), /must-not-leak|private/);
});

test("legacy response is upgrade-needed, while a stale cache remains available", () => {
  const legacy = catalog.normalizeCatalog({ type: "translation_catalog", version: 1, models: [{ id: "local:old", name: "Old", engine: "desktop", selection: { kind: "local", id: "old" } }] });
  assert.equal(legacy.upgradeNeeded, true);
  assert.equal(legacy.kind, "legacy");
  assert.equal(catalog.isUsableCatalog(legacy), false);
});

test("catalog client ignores an older refresh result", async () => {
  const area = storageArea();
  let resolveFirst;
  const client = new catalog.CatalogClient({ storageArea: area, timeoutMs: 20, WebSocketClass: class extends FakeSocket {
    constructor(url) {
      super(url);
      const socket = this;
      queueMicrotask(() => {
        const reply = url.includes("first") ? new Promise((resolve) => { resolveFirst = resolve; }) : Promise.resolve(v2({ revision: 9 }));
        reply.then((payload) => socket.emit("message", { data: JSON.stringify(payload) }));
      });
    }
  } });
  const first = client.refresh("ws://first.test/asr");
  const second = await client.refresh("ws://second.test/asr");
  assert.equal(second.ok, true);
  resolveFirst(v2({ revision: 1 }));
  const ignored = await first;
  assert.equal(ignored.ignored, true);
  assert.equal(client.catalog.revision, 9);
});

const assert = require("node:assert/strict");
const test = require("node:test");
const migration = require("../shared/migration.js");

function area(initial = {}) {
  const data = { ...initial };
  return {
    data,
    get(keys, callback) { callback(Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map((key) => [key, data[key]]))); },
    set(values, callback) { Object.assign(data, values); callback?.(); },
  };
}

function legacy(id = "old-cloud") {
  return {
    id,
    name: "Old cloud",
    engine: "openai",
    model: "future-model",
    connection: { apiUrl: "https://provider.test/v1", apiMode: "responses", autoCompleteApiUrl: false },
    apiKey: "secret-in-memory-only",
  };
}

test("successful import switches selection only after backend acknowledgement and keeps backup secret-free", async () => {
  const storage = area({ selectedTranslationModel: "old-cloud" });
  let request;
  const result = await migration.importLegacyProfiles({
    models: [legacy()],
    storageArea: storage,
    backendKey: "desktop-a|instance-1",
    getApiKey: async () => "secret-in-memory-only",
    send: async (payload) => { request = payload; return { ok: true, mapping: { "old-cloud": "profile:new-cloud" }, revision: 7 }; },
  });
  assert.equal(request.profiles[0].apiKey, "secret-in-memory-only");
  assert.equal(result.selectedId, "profile:new-cloud");
  assert.equal(storage.data.selectedTranslationModel, "profile:new-cloud");
  assert.doesNotMatch(JSON.stringify(storage.data[migration.BACKUP_KEY]), /secret-in-memory-only/);
  assert.equal(storage.data[migration.STATE_KEY].status, "complete");
  assert.equal(storage.data[migration.STATE_KEY].backendKey, "desktop-a|instance-1");
});

test("secure-store read failure preserves the profile for retry and sends no empty credential", async () => {
  const storage = area({ selectedTranslationModel: "old-cloud" });
  let called = false;
  const result = await migration.importLegacyProfiles({
    models: [legacy()], storageArea: storage,
    getApiKey: async () => { throw new Error("locked"); },
    send: async () => { called = true; return { ok: true, mapping: {} }; },
  });
  assert.equal(result.ok, false);
  assert.equal(called, false);
  assert.equal(storage.data[migration.STATE_KEY].status, "failed");
  assert.equal(storage.data[migration.BACKUP_KEY].profiles[0].apiKey, undefined);
});

test("partial acknowledgement keeps failed profiles pending and maps only acknowledged ids", async () => {
  const storage = area({ selectedTranslationModel: "good" });
  const result = await migration.importLegacyProfiles({
    models: [legacy("good"), legacy("bad")],
    storageArea: storage,
    getApiKey: async () => "key",
    send: async () => ({ ok: false, mapping: { good: "profile:good" }, errors: [{ id: "bad", message: "duplicate settings" }], revision: 8 }),
  });
  assert.equal(result.ok, true);
  assert.equal(result.partial, true);
  assert.deepEqual(result.pendingIds, ["bad"]);
  assert.equal(storage.data.selectedTranslationModel, "profile:good");
  assert.equal(storage.data[migration.STATE_KEY].status, "partial");
});

test("retries only pending profiles and keeps completion stable across backend restarts", async () => {
  const storage = area({ selectedTranslationModel: "good" });
  let calls = 0;
  const first = await migration.importLegacyProfiles({
    models: [legacy("good"), legacy("bad")],
    storageArea: storage,
    backendKey: "desktop-a|ws://desktop.test/asr",
    getApiKey: async () => "key",
    send: async (payload) => {
      calls += 1;
      assert.deepEqual(payload.profiles.map((item) => item.id), ["good", "bad"]);
      return { ok: false, mapping: { good: "profile:good" }, errors: [{ id: "bad", message: "retry" }], revision: 1 };
    },
  });
  assert.equal(first.partial, true);
  const second = await migration.importLegacyProfiles({
    models: [legacy("good"), legacy("bad")],
    storageArea: storage,
    backendKey: "desktop-a|ws://desktop.test/asr",
    getApiKey: async () => "key",
    send: async (payload) => {
      calls += 1;
      assert.deepEqual(payload.profiles.map((item) => item.id), ["bad"]);
      return { ok: true, mapping: { bad: "profile:bad" }, revision: 2 };
    },
  });
  assert.equal(second.partial, false);
  assert.equal(storage.data[migration.STATE_KEY].status, "complete");
  const skipped = await migration.importLegacyProfiles({
    models: [legacy("good"), legacy("bad")],
    storageArea: storage,
    backendKey: "desktop-a|ws://desktop.test/asr",
    send: async () => { calls += 1; throw new Error("must not reimport"); },
  });
  assert.equal(skipped.skipped, true);
  assert.equal(calls, 2);
});

test("sanitizes nested legacy connection fields before backup", () => {
  const profile = migration.sanitizeLegacyProfile({
    id: "legacy",
    engine: "openai",
    connection: { apiUrl: "https://provider.test/v1", apiMode: "responses", apiKey: "secret", token: "secret" },
    ollama: { model: "local", password: "secret", host: "http://localhost:11434" },
  });
  assert.deepEqual(profile.connection, { apiUrl: "https://provider.test/v1", apiMode: "responses" });
  assert.deepEqual(profile.ollama, { model: "local", host: "http://localhost:11434" });
  assert.doesNotMatch(JSON.stringify(profile), /secret/);
});

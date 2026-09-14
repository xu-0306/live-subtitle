const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const repoRoot = path.resolve(__dirname, "..", "..");

function source(relativePath) {
  return fs.readFileSync(path.join(repoRoot, relativePath), "utf8");
}

class Element {
  constructor(id) {
    this.id = id;
    this.value = "";
    this.textContent = "";
    this.innerHTML = "";
    this.dataset = {};
    this.style = {};
    this.disabled = false;
    this.checked = true;
    this.children = [];
    this.listeners = new Map();
  }

  addEventListener(type, callback) {
    const callbacks = this.listeners.get(type) || [];
    callbacks.push(callback);
    this.listeners.set(type, callbacks);
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  dispatch(type, event = {}) {
    for (const callback of this.listeners.get(type) || []) {
      callback(event);
    }
  }
}

const elementIds = [
  "status",
  "wsUrl",
  "translationModelSelect",
  "sttModelSizeSelect",
  "startBtn",
  "stopBtn",
  "openSettingsBtn",
  "subtitleOriginalSize",
  "subtitleOriginalSizeValue",
  "subtitleTranslatedSize",
  "subtitleTranslatedSizeValue",
  "overlayOpacity",
  "overlayOpacityValue",
  "subtitleHistoryLines",
  "subtitleHistoryLinesValue",
  "subtitleShowPartial",
  "subtitleShowPartialValue",
  "newTranslationName",
  "newTranslationEngine",
  "newTranslationModel",
  "newTranslationHost",
  "newTranslationApiKey",
  "newTranslationBaseUrl",
  "newTranslationApiMode",
  "newTranslationAutoCompleteUrl",
  "resolvedTranslationApiUrl",
  "translationApiUrlHint",
  "addTranslationModel",
  "translationModelList",
  "translationLanguageSelect",
  "translationLanguageValue",
  "translationBehaviorSelect",
  "refreshDesktopServices",
  "desktopServicesStatus",
  "translationLanguages",
  "activeCaptureTitle",
  "activeCapturePhase",
  "activeCaptureSummary",
  "occupancySummary",
];
const elements = new Map(elementIds.map((id) => [id, new Element(id)]));

const storage = {
  wsUrl: "ws://127.0.0.1:8765/asr",
  translationModels: [
    { id: "private-api", name: "Future cloud", engine: "openai", model: "future/model", connection: {apiUrl: "https://example.test/v1", apiMode: "chat_completions"} },
  ],
  selectedTranslationModel: "local:future-custom-model",
  sttModelSize: "medium",
  translationTargetLanguage: "ja",
  translationIncludePartials: true,
};

const runtimeListeners = [];
const runtimeMessages = [];
const tabUpdatedListeners = [];
const tabMessages = [];
const sockets = [];
let offscreenDocument = false;

function respondLater(callback, value) {
  if (typeof callback === "function") {
    setImmediate(() => callback(value));
  }
}

function dispatchRuntimeMessage(message, callback) {
  runtimeMessages.push(message);
  let responded = false;
  const sendResponse = (value) => {
    if (responded) return;
    responded = true;
    respondLater(callback, value);
  };
  for (const listener of runtimeListeners) {
    const keepChannel = listener(message, { tab: { id: 42 } }, sendResponse);
    if (keepChannel === true) return;
  }
  if (typeof callback === "function" && !responded) {
    respondLater(callback, undefined);
  }
}

class FakeWebSocket {
  static OPEN = 1;

  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.bufferedAmount = 0;
    this.listeners = new Map();
    this.sent = [];
    sockets.push(this);
    setTimeout(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.emit("open", {});
    }, 0);
  }

  addEventListener(type, callback) {
    const callbacks = this.listeners.get(type) || [];
    callbacks.push(callback);
    this.listeners.set(type, callbacks);
  }

  emit(type, event) {
    for (const callback of this.listeners.get(type) || []) {
      callback(event);
    }
  }

  send(value) {
    this.sent.push(value);
    if (typeof value === 'string' && JSON.parse(value).type === 'translation_catalog') {
      setImmediate(() => this.emit('message', {data: JSON.stringify({type: 'translation_catalog', version: 1,
        models: [{id: 'local:future-custom-model', name: 'Unlisted model', engine: 'desktop', selection: {kind: 'local', id: 'future-custom-model'}}]})}));
    }
    if (typeof value === 'string' && JSON.parse(value).type === 'config') {
      setImmediate(() => this.emit('message', {data: JSON.stringify({
        type: 'status',
        phase: 'loading',
        message: 'Preparing selected service',
        snapshot: { stt: { model: 'medium' }, translation: { selection: { kind: 'local', id: 'future-custom-model' }, target_language: 'Te Reo Māori', partial: false } },
      })}));
    }
  }

  close() {
    this.readyState = 3;
    this.emit("close", { code: 1000 });
  }
}

class FakeMediaRecorder {
  static isTypeSupported() {
    return false;
  }

  constructor() {
    this.listeners = new Map();
  }

  addEventListener(type, callback) {
    const callbacks = this.listeners.get(type) || [];
    callbacks.push(callback);
    this.listeners.set(type, callbacks);
  }

  start() {}

  requestData() {}

  stop() {
    for (const callback of this.listeners.get("stop") || []) {
      callback();
    }
  }
}

async function waitForCondition(readValue, expected, label, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  const matches = (value) => typeof expected === 'function' ? expected(value) : value === expected;
  let actual = await readValue();
  while (!matches(actual) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
    actual = await readValue();
  }
  if (!matches(actual)) {
    const expectedText = typeof expected === 'function' ? 'the expected condition' : JSON.stringify(expected);
    throw new Error(`${label} did not reach ${expectedText}; got ${JSON.stringify(actual)}`);
  }
  return actual;
}

const chrome = {
  runtime: {
    lastError: null,
    onMessage: {
      addListener(callback) {
        runtimeListeners.push(callback);
      },
    },
    getURL(relativePath) {
      return `chrome-extension://evaluation/${relativePath}`;
    },
    sendMessage: dispatchRuntimeMessage,
  },
  storage: {
    local: {
      get(_keys, callback) {
        respondLater(callback, { ...storage });
      },
      set(values, callback) {
        Object.assign(storage, values);
        respondLater(callback, undefined);
      },
    },
  },
  tabs: {
    onUpdated: {
      addListener(callback) {
        tabUpdatedListeners.push(callback);
      },
    },
    query(_query, callback) {
      const tabs = [{ id: 42 }];
      respondLater(callback, tabs);
      return Promise.resolve(tabs);
    },
    get(_tabId, callback) {
      respondLater(callback, { id: 42 });
    },
    sendMessage(tabId, message) {
      tabMessages.push({ tabId, message });
      return Promise.resolve();
    },
  },
  scripting: {
    insertCSS() {
      return Promise.resolve();
    },
    executeScript() {
      return Promise.resolve();
    },
  },
  offscreen: {
    hasDocument() {
      return Promise.resolve(offscreenDocument);
    },
    createDocument() {
      offscreenDocument = true;
      return Promise.resolve();
    },
  },
  tabCapture: {
    getCapturedTabs(callback) {
      respondLater(callback, []);
    },
    getMediaStreamId() {
      return Promise.resolve("stream-id");
    },
  },
};

const context = vm.createContext({
  chrome,
  console,
  document: {
    getElementById(id) {
      return elements.get(id) || null;
    },
    createElement(tagName) {
      return new Element(tagName);
    },
  },
  navigator: {
    mediaDevices: {
      getUserMedia: async () => ({
        getTracks: () => [{ stop() {} }],
      }),
    },
  },
  WebSocket: FakeWebSocket,
  MediaRecorder: FakeMediaRecorder,
  window: {
    setTimeout,
    clearTimeout,
  },
  setTimeout,
  clearTimeout,
  setInterval: () => ({ timer: true }),
  clearInterval: () => {},
  AudioContext: undefined,
  importScripts: () => {},
  URL,
});

for (const relativePath of [
  "chrome-extension/shared/connection-settings.js",
  "chrome-extension/shared/translation-client.js",
  "chrome-extension/shared/capture-settings.js",
  "chrome-extension/popup/popup.js",
  "chrome-extension/background/background.js",
  "chrome-extension/offscreen/offscreen.js",
]) {
  // Each extension page/background/offscreen script has its own global scope
  // in Chrome. Keep that separation so same-named functions do not collide in
  // this one-process harness.
  vm.runInContext(`(function () {\n${source(relativePath)}\n})();`, context, { filename: relativePath });
}

async function main() {
  // Credential storage is covered by connection-settings.test.js; this harness
  // exercises inference routing through the real page/background scripts.
  context.STTConnectionSettings = {...context.STTConnectionSettings, getApiKey: async () => ''};
  await waitForCondition(
    () => elements.get('desktopServicesStatus').textContent,
    (value) => /1 desktop choices/.test(value),
    'desktop catalog',
  );
  elements.get('subtitleOriginalSize').value = '1.4';
  elements.get('subtitleOriginalSize').dispatch('input');
  elements.get('subtitleTranslatedSize').value = '0.8';
  elements.get('subtitleTranslatedSize').dispatch('input');
  await waitForCondition(() => storage.subtitleOriginalScale, 1.4, 'original subtitle size persistence');
  await waitForCondition(() => storage.subtitleTranslatedScale, 0.8, 'translated subtitle size persistence');
  assert.equal(elements.get('subtitleOriginalSizeValue').textContent, '140%');
  assert.equal(elements.get('subtitleTranslatedSizeValue').textContent, '80%');
  for (const [modelId, language, expected] of [
    ['local:future-custom-model', 'Te Reo Māori', {selection: {kind: 'local', id: 'future-custom-model'}}],
    ['private-api', 'zh-CN', {engine: 'openai'}],
  ]) {
    const configCountBefore = sockets.flatMap((s) => s.sent).filter((value) => {
      if (typeof value !== 'string') return false;
      try { return JSON.parse(value).type === 'config'; } catch { return false; }
    }).length;
    elements.get('translationModelSelect').value = modelId;
    elements.get('translationModelSelect').dispatch('change');
    elements.get('translationLanguageSelect').value = language;
    elements.get('translationLanguageSelect').dispatch('change');
    await waitForCondition(
      () => storage.translationTargetLanguage,
      language,
      'language persistence',
    );
    elements.get('startBtn').dispatch('click');
    await waitForCondition(
      () => sockets.flatMap((s) => s.sent).filter((value) => {
        if (typeof value !== 'string') return false;
        try { return JSON.parse(value).type === 'config'; } catch { return false; }
      }).length,
      configCountBefore + 1,
      'backend config send',
    );
    const popup = runtimeMessages.filter((m) => m.type === 'popup-start').at(-1);
    const offscreen = runtimeMessages.filter((m) => m.type === 'offscreen-start').at(-1);
    assert.deepEqual(JSON.parse(JSON.stringify(popup.settings)), JSON.parse(JSON.stringify(offscreen.settings)));
    assert.equal(offscreen.settings.browserTranslation, undefined);
    const config = sockets.flatMap((s) => s.sent).filter((v) => typeof v === 'string')
      .map(JSON.parse).filter((p) => p.type === 'config').at(-1);
    assert.equal(config.translation.target_language, language, JSON.stringify({status: elements.get('status').textContent, messages: runtimeMessages.map(m => m.type)}));
    for (const [key, value] of Object.entries(expected)) assert.deepEqual(config.translation[key], value);
    assert.equal(config.stt.model, 'medium');
    if (modelId === 'local:future-custom-model') {
      await waitForCondition(
        async () => {
          // The harness disables the real one-second status timer. Refresh the
          // same status endpoint the popup uses while waiting for the backend
          // status event, so this observes the product state rather than a
          // scheduler-dependent delay.
          await context.STTPopup.refreshCaptureStatus();
          return elements.get('activeCapturePhase').textContent;
        },
        'loading',
        'active capture phase',
      );
      assert.equal(elements.get('activeCaptureTitle').textContent, 'Capturing this tab');
      assert.match(elements.get('activeCaptureSummary').textContent, /future-custom-model/);
      assert.match(elements.get('activeCaptureSummary').textContent, /Te Reo Māori/);
      const removalsBefore = tabMessages.filter((item) => item.message?.type === 'subtitle-remove').length;
      dispatchRuntimeMessage({
        type: 'offscreen-state',
        tabId: 42,
        running: false,
        status: 'Idle',
        phase: 'idle',
        retryable: false,
      });
      await waitForCondition(
        () => tabMessages.filter((item) => item.message?.type === 'subtitle-remove').length,
        removalsBefore + 1,
        'terminal phase subtitle cleanup',
      );
    }
    if (modelId === 'private-api') assert.equal(config.translation.openai.model, 'future/model');
    elements.get('stopBtn').dispatch('click');
    await waitForCondition(
      async () => {
        await context.STTPopup.refreshCaptureStatus();
        return elements.get('activeCaptureTitle').textContent;
      },
      'Not capturing',
      'capture stop',
    );
  }
  const configCountBeforeNavigation = sockets.flatMap((s) => s.sent).filter((value) => {
    if (typeof value !== 'string') return false;
    try { return JSON.parse(value).type === 'config'; } catch { return false; }
  }).length;
  elements.get('startBtn').dispatch('click');
  await waitForCondition(
    () => sockets.flatMap((s) => s.sent).filter((value) => {
      if (typeof value !== 'string') return false;
      try { return JSON.parse(value).type === 'config'; } catch { return false; }
    }).length,
    configCountBeforeNavigation + 1,
    'navigation capture start',
  );
  const navigationRemovalsBefore = tabMessages.filter((item) => item.message?.type === 'subtitle-remove').length;
  assert.equal(tabUpdatedListeners.length, 1, 'background must register one tab navigation listener');
  tabUpdatedListeners[0](42, { url: 'https://www.youtube.com/@NVIDIA' }, { id: 42 });
  await waitForCondition(
    () => tabMessages.filter((item) => item.message?.type === 'subtitle-remove').length,
    (value) => value > navigationRemovalsBefore,
    'tab navigation subtitle cleanup',
  );
  assert.equal(runtimeMessages.filter((m) => m.type === 'popup-start').length, 3);
  assert.equal(storage.translationTargetLanguage, 'zh-CN', 'Simplified Chinese must not be rewritten');
  console.log('Actual popup -> background -> offscreen -> WebSocket routing passed for custom local and cloud models, arbitrary language, and zh-CN preservation.');
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exitCode = 1;
});

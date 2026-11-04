const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");

const vm = require('node:vm');
class HintElement {
  constructor() { this.value = ''; this.textContent = ''; this.dataset = {}; this.style = {}; this.listeners = {}; }
  addEventListener(type, cb) { (this.listeners[type] ||= []).push(cb); }
  appendChild() {}
  dispatch(type) { for (const cb of this.listeners[type] || []) cb({target: this}); }
}
async function hintPopup() {
  const ids = ['status', 'wsUrl', 'translationModelSelect', 'sttModelSizeSelect', 'sttModelModeSelect',
    'translationLanguageModeSelect', 'translationLanguageSelect', 'translationBehaviorSelect', 'themeSelect',
    'subtitleOriginalSize', 'subtitleOriginalSizeValue', 'subtitleTranslatedSize', 'subtitleTranslatedSizeValue'];
  const elements = Object.fromEntries(ids.map(id => [id, new HintElement()]));
  const writes = [];
  const context = {
    document: {body: {dataset: {}}, documentElement: {dataset: {}},
      getElementById: id => elements[id], createElement: () => new HintElement()},
    chrome: {
      storage: {local: {get: (_keys, cb) => cb({}), set: (values, cb) => writes.push({values, cb})}},
      tabs: {query: (_query, cb) => cb([{id: 1}])},
      runtime: {sendMessage: (_message, cb) => cb({ok: true, phase: 'Capturing', capturing: true})},
    },
    setInterval: () => 1, setTimeout, clearTimeout,
  };
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../popup/popup.js'), 'utf8'), context);
  await new Promise(setImmediate);
  return {context, elements, writes};
}
test('popup confirms each setting after persistence and keeps hints through polling', async () => {
  const {context, elements, writes} = await hintPopup();
  const cases = [
    ['translationModelSelect', 'change', 'none', 'next capture'],
    ['sttModelSizeSelect', 'change', 'small', 'next capture'],
    ['sttModelModeSelect', 'change', 'inherit', 'next capture'],
    ['translationLanguageModeSelect', 'change', 'override', 'next capture'],
    ['translationLanguageSelect', 'input', 'ja', 'next capture'],
    ['translationBehaviorSelect', 'change', 'partial', 'next capture'],
    ['wsUrl', 'change', 'ws://localhost:9999/asr', 'now'],
    ['themeSelect', 'change', 'dark', 'now'],
    ['subtitleOriginalSize', 'input', '1.2', 'now'],
    ['subtitleTranslatedSize', 'input', '1.4', 'now'],
  ];
  for (const [id, event, value, timing] of cases) {
    context.STTPopup.setStatus('Before save');
    elements[id].value = value;
    elements[id].dispatch(event);
    assert.equal(elements.status.textContent, 'Before save', id);
    assert.equal(writes.length, 1, id);
    writes.shift().cb();
    await new Promise(setImmediate);
    const hint = timing === 'now' ? 'Saved · applies now' : 'Saved · applies to next capture';
    assert.equal(elements.status.textContent, hint, id);
    await context.STTPopup.refreshCaptureStatus();
    assert.equal(elements.status.textContent, hint, 'polling should leave the hint readable');
    context.STTPopupState.savedHintUntil = 0;
    await context.STTPopup.refreshCaptureStatus();
    assert.equal(elements.status.textContent, 'Capturing');
  }
});
test('popup reports storage errors without claiming Saved', async () => {
  const {context, elements, writes} = await hintPopup();
  elements.translationLanguageSelect.value = 'ja';
  elements.translationLanguageSelect.dispatch('change');
  context.chrome.runtime.lastError = {message: 'Storage quota exceeded'};
  writes.shift().cb();
  await new Promise(setImmediate);
  assert.equal(elements.status.textContent, 'Could not save: Storage quota exceeded');
  assert.equal(elements.status.dataset.state, 'error');
});

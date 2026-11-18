const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");

const vm = require('node:vm');
class HintElement {
  constructor() { this.value = ''; this.textContent = ''; this.dataset = {}; this.style = {}; this.listeners = {}; this.children = []; }
  get innerHTML() { return ''; }
  set innerHTML(_value) { this.children = []; }
  addEventListener(type, cb) { (this.listeners[type] ||= []).push(cb); }
  appendChild(child) { this.children.push(child); return child; }
  dispatch(type) { for (const cb of this.listeners[type] || []) cb({target: this}); }
}
const capturing = () => ({ok: true, phase: 'Capturing', capturing: true});
async function hintPopup(reply = capturing) {
  const ids = ['status', 'translationModelSelect', 'sttModelSizeSelect',
    'translationLanguageSelect', 'translationLanguageCustom', 'translationBehaviorSelect', 'themeSelect',
    'subtitleOriginalSize', 'subtitleOriginalSizeValue', 'subtitleTranslatedSize', 'subtitleTranslatedSizeValue',
    'startBtn', 'stopBtn'];
  const elements = Object.fromEntries(ids.map(id => [id, new HintElement()]));
  const writes = [];
  const context = {
    document: {body: {dataset: {}}, documentElement: {dataset: {}},
      getElementById: id => elements[id], createElement: () => new HintElement()},
    chrome: {
      storage: {local: {get: (_keys, cb) => cb({}), set: (values, cb) => writes.push({values, cb})}},
      tabs: {query: (_query, cb) => cb([{id: 1}])},
      runtime: {sendMessage: (message, cb) => cb(reply(message))},
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
    ['translationLanguageSelect', 'change', 'ja', 'next capture'],
    ['translationLanguageSelect', 'change', '__desktop-default__', 'next capture'],
    ['translationBehaviorSelect', 'change', 'partial', 'next capture'],
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
test('a failed start stays on screen through status polling until the next attempt', async () => {
  let backendUp = false;
  const {context, elements} = await hintPopup((message) => message.type === 'popup-start'
    ? (backendUp ? {ok: true} : {ok: false, error: 'Backend is unavailable'})
    : {ok: true, phase: backendUp ? 'Capturing' : 'Idle', capturing: backendUp});
  const click = async () => {
    elements.startBtn.dispatch('click');
    for (let i = 0; i < 4; i += 1) await new Promise(setImmediate);
  };
  await click();
  const failure = 'Could not start capture: Backend is unavailable';
  assert.equal(elements.status.textContent, failure);
  await context.STTPopup.refreshCaptureStatus();
  assert.equal(elements.status.textContent, failure, 'the poll must not replace the failure with Idle');
  assert.equal(elements.status.dataset.state, 'error');
  backendUp = true;
  await click();
  await context.STTPopup.refreshCaptureStatus();
  assert.equal(elements.status.textContent, 'Capturing');
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
test('every "same as desktop" choice names the real desktop value and no mode toggle is needed', async () => {
  const {context, elements, writes} = await hintPopup();
  context.STTPopupState.catalog = {kind: 'v2',
    models: [{id: 'local:qwen', name: 'Qwen 2B', available: true, selection: {kind: 'local', id: 'qwen'}}],
    stt_models: [{id: 'small', name: 'Whisper Small', available: true, selection: {model: 'small'}}],
    defaults: {stt: {model: 'small'}, translation: {selection: {kind: 'local', id: 'qwen'}, target_language: 'zh-TW', partial: true}}};
  context.STTPopup.renderChoices();
  const firstLabel = (id) => elements[id].children[0].textContent;
  assert.equal(firstLabel('translationModelSelect'), 'Same as desktop · Qwen 2B');
  assert.equal(firstLabel('sttModelSizeSelect'), 'Same as desktop · Whisper Small');
  assert.equal(firstLabel('translationLanguageSelect'), 'Same as desktop · Chinese (Traditional)');
  const select = elements.translationLanguageSelect;
  assert.equal(select.value, '__desktop-default__');

  select.value = 'ja';
  select.dispatch('change');
  assert.equal(writes.length, 1, 'one write carries both the mode and the language');
  assert.deepEqual(JSON.parse(JSON.stringify(writes[0].values)), {translationTargetLanguageMode: 'override', translationTargetLanguage: 'ja'});
  assert.equal(select.value, 'ja');

  select.value = 'Te Reo Māori'; // not in the list: it must land in the "Other language" box instead of vanishing
  select.dispatch('change');
  assert.equal(select.value, '__other-language__');
  assert.equal(elements.translationLanguageCustom.hidden, false);
  assert.equal(elements.translationLanguageCustom.value, 'Te Reo Māori');

  select.value = '__desktop-default__';
  select.dispatch('change');
  assert.equal(context.STTPopupState.targetLanguageMode, 'inherit');
  assert.equal(elements.translationLanguageCustom.hidden, true);
});
test('a desktop default speech model that is not installed is flagged like any other missing model', async () => {
  const {context, elements} = await hintPopup();
  elements.sttSelectionHint = new HintElement();
  context.STTPopupState.catalog = {kind: 'v2', models: [],
    stt_models: [{id: 'base', name: 'Whisper Base', available: false, selection: {model: 'base'}}],
    defaults: {stt: {model: 'base'}, translation: {selection: {kind: 'none', id: 'none'}, target_language: 'zh-TW'}}};
  context.STTPopup.renderChoices();
  assert.equal(elements.sttModelSizeSelect.children[0].textContent, 'Same as desktop · Whisper Base · unavailable');
  assert.equal(elements.sttSelectionHint.textContent, 'This speech recognition model is not installed. Download it in the desktop app.');
  context.STTPopupState.catalog.stt_models[0].available = true;
  context.STTPopup.renderChoices();
  assert.equal(elements.sttModelSizeSelect.children[0].textContent, 'Same as desktop · Whisper Base');
  assert.equal(elements.sttSelectionHint.textContent, '');
});
test('choosing Other language without typing one stops the start with a plain message', async () => {
  const {elements} = await hintPopup((message) => message.type === 'popup-start' ? {ok: true} : {ok: true, phase: 'Idle', capturing: false});
  elements.translationLanguageSelect.value = '__other-language__';
  elements.translationLanguageSelect.dispatch('change');
  elements.startBtn.dispatch('click');
  for (let i = 0; i < 4; i += 1) await new Promise(setImmediate);
  assert.equal(elements.status.textContent, 'Type a target language, or pick Same as desktop.');
  assert.equal(elements.status.dataset.state, 'error');
});

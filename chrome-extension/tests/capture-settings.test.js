const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const { buildCaptureSettings } = require('../shared/capture-settings.js');
test('desktop default survives JSON transport without a translation override', () => {
  const received = JSON.parse(JSON.stringify(buildCaptureSettings({ engine: 'backend' }, undefined, { model: 'medium' })));
  assert.deepEqual(received, { stt: { model: 'medium' } });
});
test('API profiles share the backend lease with local models', () => {
  const cfg = { engine: 'openai', target_language: 'ja', partial: false, openai: { api_key: 'private' } };
  const settings = buildCaptureSettings({ engine: 'openai' }, cfg, {});
  assert.equal(settings.browserTranslation, undefined);
  assert.equal(settings.translation, cfg);
  assert.equal(buildCaptureSettings({ engine: 'ollama' }, { engine: 'ollama' }, {}).translation.engine, 'ollama');
});
test('v2 keeps inherited defaults sparse and carries unseen explicit model/language choices', () => {
  const { buildCaptureSettings, buildTranslationConfig, buildSttConfig, sanitizeSnapshot } = require('../shared/capture-settings.js');
  const model = { id: 'local:unseen', engine: 'desktop', selection: { kind: 'local', id: 'unseen' } };
  const translation = buildTranslationConfig(model, { targetLanguageMode: 'override', targetLanguage: 'Kiswahili', partialMode: 'inherit' });
  const stt = buildSttConfig({ sttMode: 'inherit', sttModel: 'medium' });
  const settings = buildCaptureSettings(model, translation, stt, { snapshot: { selection: model.selection } });
  assert.equal(settings.version, 2);
  assert.deepEqual(settings.translation.selection, model.selection);
  assert.equal(settings.translation.target_language, 'Kiswahili');
  assert.equal(settings.translation.partial, undefined);
  assert.equal(settings.stt, undefined);
  assert.deepEqual(sanitizeSnapshot({ selection: { kind: 'profile', id: 'id', apiKey: 'secret' }, partial: true, stt: { model: 'small', path: 'private' } }), {
    selection: { kind: 'profile', id: 'id' }, partial: true, stt: { model: 'small' },
  });
});
test('popup and options load the shared producer exactly once before its consumer', () => {
  for (const page of ['../popup/popup.html', '../options/options.html']) {
    const html = fs.readFileSync(path.join(__dirname, page), 'utf8');
    assert.equal(html.split('capture-settings.js').length, 2);
    assert.ok(html.indexOf('capture-settings.js') < html.lastIndexOf('popup.js'));
  }
  const js = fs.readFileSync(path.join(__dirname, '../popup/popup.js'), 'utf8');
  assert.match(js, /globalThis\.STTCaptureSettings\.buildCaptureSettings/);
});

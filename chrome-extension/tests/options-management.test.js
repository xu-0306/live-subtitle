const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

let playwright;
try { playwright = require(process.env.PLAYWRIGHT_MODULE || path.resolve(__dirname, '../../.work/ui-validation/node_modules/playwright')); }
catch { playwright = null; }

test('options manages a backend service and keeps desktop defaults visible', async (t) => {
  const executablePath = process.env.UI_SYNC_BROWSER || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
  if (!playwright || !fs.existsSync(executablePath)) {
    t.skip('Chromium or Playwright is unavailable');
    return;
  }
  const browser = await playwright.chromium.launch({ headless: true, executablePath });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.addInitScript(() => {
      const values = {};
      globalThis.chrome = { storage: { local: {
        get(keys, callback) {
          const found = {};
          for (const key of keys) found[key] = values[key];
          callback(found);
        },
        set(update, callback) { Object.assign(values, update); callback?.(); },
      } } };
      const data = {
        type: 'desktop_management_result', version: 1, ok: true, revision: 12,
        server_id: 'test-server', instance_id: 'test-instance',
        stt_models: [{ id: 'small', name: 'Whisper Small', available: true, status: 'installed' },
          { id: 'medium', name: 'Whisper Medium', available: false, status: 'missing' }],
        models: [{ id: 'local:qwen-2b', name: 'Qwen 2B', available: true, selection: { kind: 'local', id: 'qwen-2b' } }],
        profiles: [], defaults: { stt: { model: 'small' }, translation: { selection: { kind: 'none', id: 'none' },
          target_language: 'zh-TW', partial: false } },
      };
      globalThis.__managementRequests = [];
      globalThis.WebSocket = class {
        constructor() { this.events = {}; queueMicrotask(() => this.emit('open', {})); }
        addEventListener(type, listener) { (this.events[type] ||= []).push(listener); }
        emit(type, value) { for (const listener of this.events[type] || []) listener(value); }
        send(value) {
          const request = JSON.parse(value);
          globalThis.__managementRequests.push(request);
          if (request.action === 'save_profile') {
            data.profiles.push({ ...request.data.profile, id: request.data.profile.id || 'service-test-1',
              has_api_key: false, available: true, status: 'configured' });
            data.saved_profile_id = request.data.profile.id || 'service-test-1';
            data.models.push({ id: `profile:${data.saved_profile_id}`, name: request.data.profile.name, available: true,
              selection: { kind: 'profile', id: data.saved_profile_id } });
            data.revision += 1;
          }
          if (request.action === 'set_defaults') {
            const change = request.data;
            if (change.target_language !== undefined) data.defaults.translation.target_language = change.target_language;
            if (change.translation_selection) data.defaults.translation.selection = change.translation_selection;
            if (change.stt_model) data.defaults.stt.model = change.stt_model;
            data.revision += 1;
          }
          queueMicrotask(() => this.emit('message', { data: JSON.stringify(data) }));
        }
        close() { this.emit('close', {}); }
      };
    });
    await page.goto(pathToFileURL(path.resolve(__dirname, '../options/options.html')).href);
    await page.getByText('Connected. Settings are in sync with the desktop app.').waitFor();
    assert.equal(await page.locator('#defaultStt').inputValue(), 'small');
    assert.equal(await page.locator('#defaultStt option').count(), 1, 'only installed speech models are offered');
    assert.equal(await page.locator('#defaultTranslation option[value="local:qwen-2b"]').count(), 1);
    await page.locator('#serviceEditor summary').click();
    assert.equal(await page.getByText('Service ID').count(), 0);
    assert.equal(await page.getByText('API type').count(), 0);
    await page.locator('#serviceName').fill('Office translation');
    await page.locator('#serviceModel').fill('translator');
    await page.locator('#serviceEndpoint').fill('http://127.0.0.1:11434');
    await page.locator('#serviceForm button[type=submit]').click();
    await page.getByText('Saved to the shared desktop settings.').waitFor();
    assert.equal(await page.locator('#serviceList .management-row').count(), 1);
    assert.equal(await page.locator('#defaultTranslation option[value="profile:service-test-1"]').count(), 1);
    assert.equal(await page.locator('#serviceId').inputValue(), 'service-test-1');

    // Accessibility: 24px hit areas, service-specific button names, and a nav that shows where you are.
    const sizes = await page.locator('input[type=checkbox], input[type=range], .editor-panel summary').evaluateAll((elements) => elements.map((element) => {
      const rect = element.getBoundingClientRect();
      return [element.id || element.tagName, rect.width, rect.height];
    }));
    assert.ok(sizes.length > 0 && sizes.every(([, width, height]) => width >= 24 && height >= 24), `controls need 24px hit areas: ${JSON.stringify(sizes)}`);
    for (const label of ['Edit', 'Use as default', 'Remove']) {
      assert.equal(await page.getByRole('button', { name: `${label} Office translation`, exact: true }).count(), 1, label);
    }
    await page.locator('.settings-nav a[href="#services"]').click();
    assert.equal(await page.locator('.settings-nav a[aria-current="location"]').getAttribute('href'), '#services');
    await page.setViewportSize({ width: 700, height: 600 });
    await page.locator('.settings-nav a[href="#appearance"]').click();
    await page.waitForTimeout(100);
    assert.equal(await page.locator('.settings-nav a[aria-current="location"]').getAttribute('href'), '#appearance');
    await page.setViewportSize({ width: 1280, height: 860 });
    const request = await page.evaluate(() => globalThis.__managementRequests.find((item) => item.action === 'save_profile'));
    assert.equal(request.expected_revision, 12);
    assert.equal(request.data.profile.id, '');
    assert.equal(Object.hasOwn(request.data.profile, 'api_type'), false);
    const layout = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth }));
    assert.ok(layout.width <= layout.viewport, 'desktop settings must not overflow horizontally');

    // Unsaved edits in the defaults form survive other actions; only Save defaults clears them.
    const defaultsRequests = () => page.evaluate(() => globalThis.__managementRequests.filter((item) => item.action === 'set_defaults'));
    const idle = (count) => page.waitForFunction((n) => globalThis.__managementRequests.filter((item) => item.action === 'set_defaults').length === n
      && !document.getElementById('saveDefaults').disabled, count);
    await page.locator('#defaultLanguage').fill('ja');
    await page.locator('#serviceList .management-row button', { hasText: 'Use as default' }).click();
    await idle(1);
    assert.equal(await page.locator('#defaultLanguage').inputValue(), 'ja', 'another action must not overwrite the unsaved language');
    await page.locator('#saveDefaults').click();
    await idle(2);
    assert.equal((await defaultsRequests())[1].data.target_language, 'ja');
    assert.equal(await page.locator('#defaultLanguage').inputValue(), 'ja');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('options disables management actions and says so while the backend is offline', async (t) => {
  const executablePath = process.env.UI_SYNC_BROWSER || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
  if (!playwright || !fs.existsSync(executablePath)) {
    t.skip('Chromium or Playwright is unavailable');
    return;
  }
  const browser = await playwright.chromium.launch({ headless: true, executablePath });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
    await page.addInitScript(() => {
      globalThis.chrome = { storage: { local: { get(_keys, callback) { callback({}); }, set(_update, callback) { callback?.(); } } } };
      globalThis.WebSocket = class {
        constructor() { this.events = {}; queueMicrotask(() => (this.events.error || []).forEach((listener) => listener({}))); }
        addEventListener(type, listener) { (this.events[type] ||= []).push(listener); }
        send() {}
        close() {}
      };
    });
    await page.goto(pathToFileURL(path.resolve(__dirname, '../options/options.html')).href);
    await page.locator('#connectionBadge', { hasText: 'Offline' }).waitFor();
    assert.match(await page.locator('#revisionLabel').innerText(), /Offline/);
    assert.equal(await page.locator('#saveDefaults').isDisabled(), true);
    assert.equal(await page.locator('#refreshSettings').isDisabled(), false);
  } finally { await browser.close(); }
});

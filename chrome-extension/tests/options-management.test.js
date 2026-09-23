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
        local_models: [{ id: 'qwen-2b', name: 'Qwen 2B', repo: 'owner/repo', size: 1000000000,
          license: 'Apache-2.0', custom: false, installed: true, published: true, status: 'installed' }],
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
            data.revision += 1;
          }
          queueMicrotask(() => this.emit('message', { data: JSON.stringify(data) }));
        }
        close() { this.emit('close', {}); }
      };
    });
    await page.goto(pathToFileURL(path.resolve(__dirname, '../options/options.html')).href);
    await page.getByText('Connected. Models and services are in sync with the desktop app.').waitFor();
    assert.equal(await page.locator('#defaultStt').inputValue(), 'small');
    assert.equal(await page.locator('#speechList .management-row').count(), 2);
    assert.equal(await page.locator('#localList .management-row').count(), 1);
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
    const request = await page.evaluate(() => globalThis.__managementRequests.find((item) => item.action === 'save_profile'));
    assert.equal(request.expected_revision, 12);
    assert.equal(request.data.profile.id, '');
    assert.equal(Object.hasOwn(request.data.profile, 'api_type'), false);
    const layout = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth }));
    assert.ok(layout.width <= layout.viewport, 'desktop settings must not overflow horizontally');
    assert.deepEqual(errors, []);
    const output = path.resolve(__dirname, '../../.work/options-management-preview.png');
    await page.screenshot({ path: output, fullPage: true });
  } finally { await browser.close(); }
});

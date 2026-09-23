/* Real browser tab audio -> extension tabCapture -> backend STT.
 * Public audio fixture: https://raw.githubusercontent.com/openai/whisper/main/tests/jfk.flac
 * No generated/mocked subtitle messages are used.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const root = path.resolve(__dirname, '../../..');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || path.join(root, '.work/ui-validation/node_modules/playwright'));

async function main() {
  const output = path.resolve(process.env.UI_SYNC_OUTPUT);
  const fixture = process.env.UI_SYNC_AUDIO || path.join(root, '.work/ui-validation/jfk.flac');
  const fixtureBytes = fs.readFileSync(fixture);
  const server = http.createServer((request, response) => {
    if (request.url === '/speech.flac') {
      response.writeHead(200, { 'Content-Type': 'audio/flac', 'Content-Length': fixtureBytes.length });
      response.end(fixtureBytes);
    } else {
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      response.end('<!doctype html><title>Public speech capture verification</title><style>body{font:20px system-ui;padding:48px;background:#f4f7fc;color:#18304d}</style><h1>Public speech capture verification</h1><p>OpenAI Whisper test fixture · JFK speech</p><audio id="speech" controls src="/speech.flac"></audio><button id="play" onclick="speech.play()">Play speech</button>');
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const fixtureUrl = `http://127.0.0.1:${server.address().port}`;
  const extension = path.join(root, 'chrome-extension');
  const context = await chromium.launchPersistentContext(fs.mkdtempSync(path.join(output, 'live-profile-')), {
    executablePath: process.env.UI_SYNC_BROWSER,
    headless: process.env.UI_SYNC_HEADED !== '1',
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, '--autoplay-policy=no-user-gesture-required'],
  });
  try {
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    const id = new URL(worker.url()).host;
    const control = await context.newPage();
    await control.goto(`chrome-extension://${id}/options/options.html`);
    await control.evaluate(async (wsUrl) => {
      await chrome.storage.local.set({ wsUrl, sttModelMode: 'inherit', translationTargetLanguageMode: 'inherit', selectedTranslationModel: 'noop' });
      globalThis.liveEvidence = [];
      chrome.runtime.onMessage.addListener(message => {
        if (['offscreen-subtitle', 'offscreen-state'].includes(message.type)) globalThis.liveEvidence.push(message);
      });
    }, process.env.UI_SYNC_WS_URL);
    const audio = await context.newPage();
    await audio.goto(fixtureUrl);
    await audio.bringToFront();
    const tabId = await control.evaluate(async (url) => (await chrome.tabs.query({})).find(tab => tab.url?.startsWith(url)).id, fixtureUrl);
    // Calling the real action opens the extension on the active audio tab and
    // does not grant toolbar user activation; manual mode requires a real click.
    if (process.env.UI_SYNC_MANUAL !== '1') await control.evaluate(() => chrome.action.openPopup());
    await audio.waitForTimeout(500);
    const popup = context.pages().find(page => page.url().includes('/popup/popup.html'));
    console.log('Popup surface:', Boolean(popup));
    if (process.env.UI_SYNC_MANUAL === '1') {
      console.log('WAITING for native toolbar invocation and Start on isolated public speech tab');
    } else if (popup) {
      await popup.locator('#startBtn').click();
    } else {
      const started = await control.evaluate(async ({ tabId, wsUrl }) => chrome.runtime.sendMessage({
        type: 'popup-start', tabId, wsUrl,
        settings: { translation: { engine: 'noop' }, stt: {} },
      }), { tabId, wsUrl: process.env.UI_SYNC_WS_URL });
      assert.ok(started?.ok, started?.error || 'Capture did not start');
    }
    const deadline = Date.now() + 240000;
    let status;
    while (Date.now() < deadline) {
      status = await control.evaluate(tabId => chrome.runtime.sendMessage({type:'popup-status',tabId}), tabId);
      console.log('Status:', JSON.stringify(status));
      if (status?.phase === 'capturing' || /ready/i.test(status?.status || '')) break;
      if (status?.phase === 'error') throw new Error(status.status);
      await audio.waitForTimeout(1500);
    }
    assert.ok(status?.capturing, 'Capture must be running');
    await audio.locator('#play').click();
    await control.waitForFunction(() => globalThis.liveEvidence.some(message => message.type === 'offscreen-subtitle' && message.payload?.type === 'subtitle' && /country/i.test(message.payload.original || '')), null, { timeout: 90000 });
    await audio.screenshot({path: path.join(output, 'real-tab-subtitles.png'), fullPage:true});
    const stopped = await control.evaluate(tabId => chrome.runtime.sendMessage({type:'popup-stop',tabId}), tabId);
    assert.ok(stopped?.ok, 'Stop succeeded');
    await audio.waitForTimeout(500);
    const final = await control.evaluate(tabId => chrome.runtime.sendMessage({type:'popup-status',tabId}), tabId);
    assert.equal(final.capturing, false, 'Capture stopped');
    const events = await control.evaluate(() => globalThis.liveEvidence);
    fs.writeFileSync(path.join(output, 'live-result.json'), JSON.stringify({ browser:context.browser()?.version(), realTabCapture:true, inference:true, audioFixture:'openai/whisper tests/jfk.flac', status, final, events },null,2));
    console.log('Live evidence:', output);
  } finally { await context.close(); await new Promise(resolve => server.close(resolve)); }
}
main().catch(error => {console.error(error);process.exitCode=1;});

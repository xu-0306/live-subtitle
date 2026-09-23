/* Real Chromium extension rendering against an isolated running backend.
 * Dependencies and browser path are supplied by the validation environment.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '../../..');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || path.join(root, '.work/ui-validation/node_modules/playwright'));

async function main() {
  const wsUrl = process.env.UI_SYNC_WS_URL;
  if (!wsUrl) throw new Error('Set UI_SYNC_WS_URL to an isolated verification backend.');
  const output = path.resolve(process.env.UI_SYNC_OUTPUT || path.join(root, '.work/ui-sync-browser'));
  fs.mkdirSync(output, { recursive: true });
  const profile = fs.mkdtempSync(path.join(output, 'profile-'));
  const extension = path.join(root, 'chrome-extension');
  const context = await chromium.launchPersistentContext(profile, {
    ...(process.env.UI_SYNC_BROWSER ? { executablePath: process.env.UI_SYNC_BROWSER } : { channel: 'chromium' }),
    headless: process.env.UI_SYNC_HEADED !== '1',
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
  });
  const errors = [];
  const checks = [];
  try {
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker', { timeout: 20000 });
    const extensionId = new URL(worker.url()).host;
    const options = await context.newPage();
    options.on('pageerror', error => errors.push(String(error)));
    await options.setViewportSize({ width: 1200, height: 1000 });
    await options.goto(`chrome-extension://${extensionId}/options/options.html`);
    await options.evaluate(async (url) => { await chrome.storage.local.set({ wsUrl: url }); }, wsUrl);
    await options.reload();
    await options.locator('#themeSelect').waitFor();
    await options.waitForFunction(() => document.querySelector('#translationModelSelect')?.options.length >= 2, null, { timeout: 15000 });
    const popup = await context.newPage();
    popup.on('pageerror', error => errors.push(String(error)));
    await popup.setViewportSize({ width: 380, height: 560 });
    await popup.goto(`chrome-extension://${extensionId}/popup/popup.html`);
    await popup.waitForFunction(() => document.querySelector('#translationModelSelect')?.options.length >= 2, null, { timeout: 15000 });

    for (const theme of ['light', 'dark']) {
      await options.locator('#themeSelect').selectOption(theme);
      await popup.waitForTimeout(350);
      await options.screenshot({ path: path.join(output, `options-${theme}.png`), fullPage: true });
      await popup.screenshot({ path: path.join(output, `popup-${theme}.png`) });
      const geometry = await popup.evaluate(() => {
        const stop = document.querySelector('#stopBtn').getBoundingClientRect();
        return { viewport: innerWidth, width: document.documentElement.scrollWidth, stopTop: stop.top, stopBottom: stop.bottom, height: innerHeight };
      });
      assert.ok(geometry.width <= geometry.viewport + 1, `${theme}: horizontal overflow ${JSON.stringify(geometry)}`);
      assert.ok(geometry.stopTop >= 0 && geometry.stopBottom <= geometry.height, `${theme}: Stop is clipped ${JSON.stringify(geometry)}`);
      checks.push(`${theme}: real extension renders at 380x560 with visible Stop and no horizontal overflow`);
      await popup.locator('.active-card').scrollIntoViewIfNeeded();
      const scrollGeometry = await popup.evaluate(() => {
        const panel = document.querySelector('.active-card').getBoundingClientRect();
        const dock = document.querySelector('.control-dock').getBoundingClientRect();
        const choices = document.querySelector('.choices-card').getBoundingClientRect();
        return {panelTop:panel.top,panelBottom:panel.bottom,dockTop:dock.top,choicesBottom:choices.bottom};
      });
      assert.ok(scrollGeometry.choicesBottom <= scrollGeometry.panelTop + 1, 'Choice fields must not overlap capture status');
      assert.ok(scrollGeometry.panelBottom <= scrollGeometry.dockTop + 1, 'Current capture must be reachable above pinned controls');
      await popup.screenshot({path:path.join(output,`popup-${theme}-scrolled.png`)});
      await popup.locator('.popup-scroll').evaluate(element => {element.scrollTop=0;});
    }

    await options.locator('#themeSelect').selectOption('system');
    await popup.emulateMedia({ colorScheme: 'light' });
    await popup.waitForTimeout(150);
    const light = await popup.evaluate(() => getComputedStyle(document.body).backgroundColor);
    await popup.emulateMedia({ colorScheme: 'dark' });
    await popup.waitForTimeout(150);
    const dark = await popup.evaluate(() => getComputedStyle(document.body).backgroundColor);
    assert.notEqual(light, dark, 'System theme must react to color scheme');
    checks.push('System theme responds to browser color-scheme changes');

    const labels = await popup.locator('#translationModelSelect option').allTextContents();
    const modelLabels = await popup.locator('#sttModelSizeSelect option').allTextContents();
    assert.ok(labels.length >= 2 && modelLabels.length >= 1, 'catalog choices exist');
    assert.equal(await options.locator('#newTranslationApiKey').count(), 0, 'old independent profile editor removed');
    assert.equal(await options.locator('#translationModelList button').count(), 0, 'system choices are not deletable profiles');
    checks.push('backend catalog consumed; independent profile editor removed');

    await options.locator('#translationModelSelect').selectOption('profile:research');
    await popup.waitForFunction(() => document.querySelector('#translationModelSelect')?.value === 'profile:research');
    const renameStarted = Date.now();
    execFileSync(process.env.UI_SYNC_PYTHON, ['-X', 'utf8', '-c',
      "import os; from backend.config_store import load_snapshot,save_config; p=os.environ['UI_SYNC_CONFIG_PATH']; d,r=load_snapshot(p); d['translation']['service_profiles'][0]['name']='Renamed from desktop'; save_config(p,d,expected_revision=r)"],
      {cwd:root,env:process.env});
    await popup.waitForFunction(() => Array.from(document.querySelector('#translationModelSelect')?.options || []).some(option => option.value === 'profile:research' && option.textContent.includes('Renamed from desktop')), null, {timeout:5000});
    assert.equal(await popup.locator('#translationModelSelect').inputValue(), 'profile:research');
    checks.push(`desktop rename automatically syncs, preserving selection (${Date.now()-renameStarted} ms including config write)`);

    await popup.locator('#sttModelSizeSelect').selectOption('__desktop-default__');
    const sttStarted = Date.now();
    execFileSync(process.env.UI_SYNC_PYTHON, ['-X', 'utf8', '-c',
      "import os; os.environ['QT_QPA_PLATFORM']='offscreen'; from PySide6.QtWidgets import QApplication; from gui.app import MainWindow; a=QApplication([]); w=MainWindow(os.environ['UI_SYNC_CONFIG_PATH'],auto_start=False,setup_tray=False); w.model_select_combo.setCurrentIndex(w.model_select_combo.findData('base')); a.processEvents(); w.close()"],
      {cwd:root,env:process.env});
    await popup.waitForFunction(() => document.querySelector('#sttModelSizeSelect option[value="__desktop-default__"]')?.textContent.includes('base'), null, {timeout:5000});
    assert.equal(await popup.locator('#sttModelSizeSelect').inputValue(), '__desktop-default__');
    assert.equal(await popup.locator('label[for="sttModelSizeSelect"]').textContent(), 'STT model');
    checks.push(`actual Qt STT selection automatically syncs to inherited browser choice (${Date.now()-sttStarted} ms including Qt startup)`);

    await options.evaluate(async () => {
      await STTConnectionSettings.setApiKey('legacy-browser-fixture', 'FAKE-BROWSER-MIGRATION-KEY');
      await chrome.storage.local.set({translationModels:[{
        id:'legacy-browser-fixture',name:'Imported browser service',engine:'openai',model:'arbitrary-future-model',
        connection:{apiUrl:'https://example.test/custom/responses',apiMode:'responses',autoCompleteApiUrl:false},
      }],selectedTranslationModel:'legacy-browser-fixture'});
    });
    await options.reload();
    await options.waitForFunction(() => STTPopupState.catalog?.capabilities?.includes('profile_import'));
    await options.locator('#migrateProfilesBtn').click();
    await options.waitForFunction(() => STTPopupState.selectedTranslation?.startsWith('profile:') && Array.from(document.querySelector('#translationModelSelect').options).some(option => option.textContent.includes('Imported browser service')), null, {timeout:10000});
    await options.locator('#migrationCard').waitFor({state:'hidden',timeout:5000});
    const migrated = await options.evaluate(async () => {
      const data = await chrome.storage.local.get(['desktopProfileMigration','legacyTranslationProfilesBackup','desktopCatalogCache','selectedTranslationModel']);
      return {data,visible:!document.querySelector('#migrationCard').hidden};
    });
    assert.equal(migrated.data.desktopProfileMigration.status,'complete');
    assert.equal(migrated.visible,false);
    assert.ok(!JSON.stringify(migrated).includes('FAKE-BROWSER-MIGRATION-KEY'));
    const credentialCheck = execFileSync(process.env.UI_SYNC_PYTHON,['-X','utf8','-c',
      "import os; from backend.config_store import load_snapshot; d,r=load_snapshot(os.environ['UI_SYNC_CONFIG_PATH']); p=next(p for p in d['translation']['service_profiles'] if p['name']=='Imported browser service'); assert p['api_key']=='FAKE-BROWSER-MIGRATION-KEY'; assert p['api_type']=='responses'; assert p['auto_complete_endpoint'] is False; print('credential-and-endpoint-preserved')"],{cwd:root,env:process.env,encoding:'utf8'});
    assert.ok(credentialCheck.includes('credential-and-endpoint-preserved'));
    checks.push('real browser migrates saved profile and credential; endpoint semantics retained and secret absent from cache');
    await options.locator('#translationModelSelect').selectOption('profile:research');
    await popup.waitForFunction(() => document.querySelector('#translationModelSelect')?.value === 'profile:research');

    // Only the isolated backend created by the driver is stopped here.
    process.kill(Number(process.env.UI_SYNC_BACKEND_PID));
    await popup.waitForFunction(() => /offline/i.test(document.querySelector('#desktopServicesStatus')?.textContent || ''), null, {timeout:8000});
    assert.equal(await popup.locator('#translationModelSelect').inputValue(), 'profile:research');
    assert.ok((await popup.locator('#translationModelSelect option').allTextContents()).some(label => label.includes('Renamed from desktop')));
    await popup.screenshot({path:path.join(output,'popup-offline.png')});
    checks.push('backend disconnect keeps last known choice and shows offline status');
    assert.deepEqual(errors, [], 'No uncaught page errors');
    const result = { browser: context.browser()?.version(), checks, errors, translationLabels: labels, sttLabels: modelLabels, inference: false };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
    console.log('Evidence:', output);
  } finally { await context.close(); }
}

main().catch(error => { console.error(error); process.exitCode = 1; });

// Re-record the README demo clip (docs/media/demo.gif).
//
// What it does: loads the real extension from chrome-extension/ into Playwright's
// Chromium, plays a CC BY YouTube talk in demo-player.html, clicks the real popup's
// "Start capture" with local translation to Traditional Chinese, and saves a
// page recording (WebM) next to this file. Convert it with the ffmpeg lines in
// SHOT_LIST.md.
//
// Requirements:
//   - The backend running on ws://127.0.0.1:8765 with the speech and local
//     translation models already downloaded (start it from the desktop app).
//   - Playwright for Node. Point PLAYWRIGHT_MODULE at it if it is not installed
//     in this repo, and CHROMIUM at a Chromium build (branded Chrome ignores
//     --load-extension).
//
// Run from the repository root:
//   node docs/media/record_demo.js [youtubeId] [startSeconds] [recordSeconds]
//
// Two harness-only shortcuts, both limited to this script:
//   - --allowlisted-extension-id lets the extension capture the tab without a
//     toolbar click, which automation cannot perform.
//   - The popup's "current tab" lookup is pointed at the video tab, because the
//     popup is opened in its own window instead of from the toolbar.
const path = require("path");
const os = require("os");
const fs = require("fs");
const http = require("http");

const ROOT = path.resolve(__dirname, "../..");
const EXT = path.join(ROOT, "chrome-extension");
const pw = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const EXE = process.env.CHROMIUM || pw.chromium.executablePath();
const [VIDEO, START, SECONDS] = [process.argv[2] || "c8_BIamLESg", process.argv[3] || "15", Number(process.argv[4] || 40)];
const PORT = 8800;
const PAGE = `http://127.0.0.1:${PORT}/demo-player.html`;

const launch = (extra) => pw.chromium.launchPersistentContext(fs.mkdtempSync(path.join(os.tmpdir(), "ls-demo-")), {
  headless: false,
  executablePath: EXE,
  ignoreDefaultArgs: ["--enable-automation"],
  viewport: { width: 1280, height: 720 },
  ...extra.options,
  args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`, "--autoplay-policy=no-user-gesture-required", ...extra.args],
});
const worker = async (ctx) => ctx.serviceWorkers()[0] || ctx.waitForEvent("serviceworker");
const overlayText = (page) => page.evaluate(() => [...document.querySelectorAll("[class*=subtitle],[class*=overlay],[id*=subtitle]")].map((e) => e.innerText).join(" "));

(async () => {
  // YouTube embeds need an http origin, so serve this folder locally.
  const server = http.createServer((req, res) => {
    const file = path.join(__dirname, path.basename(new URL(req.url, PAGE).pathname));
    fs.readFile(file, (err, data) => (err ? (res.writeHead(404), res.end()) : (res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" }), res.end(data))));
  }).listen(PORT, "127.0.0.1");

  // An unpacked extension's id depends on its folder, so read it first.
  let ctx = await launch({ args: [] });
  const id = new URL((await worker(ctx)).url()).host;
  await ctx.close();

  ctx = await launch({
    args: [`--allowlisted-extension-id=${id}`, `--app=${PAGE}`, "--window-size=1296,860"],
    options: { recordVideo: { dir: __dirname, size: { width: 1280, height: 720 } } },
  });
  const sw = await worker(ctx);
  // Same preferences a user sets by resizing and dragging the subtitle box.
  await sw.evaluate(() => chrome.storage.local.set({
    sttSubtitleOverlaySize: { width: 1040, height: 190 },
    sttSubtitleOverlayPosition: { x: 120, y: 500 },
    subtitleHistoryLines: 1,
    subtitleShowPartial: true,
    subtitleOriginalScale: 1.25,
    subtitleTranslatedScale: 1.5,
  }));
  const page = ctx.pages().find((p) => p.url().startsWith(PAGE)) || await ctx.waitForEvent("page", { predicate: (p) => p.url().startsWith(PAGE) });
  await page.waitForLoadState();
  const tabId = await sw.evaluate(async (u) => (await chrome.tabs.query({ url: u + "*" }))[0]?.id, PAGE);

  const popupOpened = ctx.waitForEvent("page", { predicate: (p) => p.url().includes("popup.html") });
  await sw.evaluate(() => chrome.windows.create({ url: chrome.runtime.getURL("popup/popup.html"), type: "popup", width: 400, height: 640, left: 1320, top: 0 }));
  const popup = await popupOpened;
  await popup.waitForLoadState();
  await popup.waitForTimeout(1500);
  await popup.evaluate((target) => {
    const query = chrome.tabs.query.bind(chrome.tabs);
    chrome.tabs.query = (info, cb) => {
      if (info && info.active) { const r = [{ id: target }]; return cb ? cb(r) : Promise.resolve(r); }
      return query(info, cb);
    };
  }, tabId);
  await popup.selectOption("#translationModelSelect", process.env.DEMO_TRANSLATION || "local:qwen35-4b-q4");
  await popup.selectOption("#translationLanguageModeSelect", "override");
  await popup.fill("#translationLanguageSelect", process.env.DEMO_TARGET || "Traditional Chinese");
  await popup.press("#translationLanguageSelect", "Tab");
  await popup.click("#startBtn");

  // Wait until the models are ready, so the clip does not open on a loading message.
  for (let i = 0; i < 90; i++) {
    const t = await overlayText(page);
    if (t && !/Preparing|Connecting|Loading/i.test(t)) break;
    await page.waitForTimeout(1000);
  }
  await page.bringToFront();
  await page.evaluate(([v, s]) => window.startVideo(v, s), [VIDEO, START]);
  console.log("video started; recording", SECONDS, "s");
  for (let i = 0; i < SECONDS; i += 10) {
    await page.waitForTimeout(10000);
    console.log(`${i + 10}s:`, (await overlayText(page)).replace(/\s+/g, " ").slice(-120));
  }
  const video = await page.video().path();
  await ctx.close();
  server.close();
  console.log("saved", video);
})().catch((e) => { console.error(e); process.exit(1); });

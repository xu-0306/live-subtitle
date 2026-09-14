const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

let playwright = null;
let playwrightSource = "";
const playwrightCandidates = [
  process.env.PLAYWRIGHT_MODULE,
  "playwright",
  path.resolve(__dirname, "../../.work/ui-validation/node_modules/playwright"),
].filter(Boolean);
for (const candidate of playwrightCandidates) {
  try {
    playwright = require(candidate);
    playwrightSource = candidate;
    break;
  } catch {
    // Try the next configured or local fallback.
  }
}

test("popup keeps the action dock pinned while the content scrolls", async (t) => {
  if (!playwright) {
    t.skip("Playwright is unavailable; set PLAYWRIGHT_MODULE or install playwright for browser layout validation");
    return;
  }
  let executablePath = process.env.UI_SYNC_BROWSER || "";
  if (!executablePath) {
    try { executablePath = playwright.chromium.executablePath(); } catch { executablePath = ""; }
  }
  if (!executablePath || !fs.existsSync(executablePath)) {
    t.skip(`Chromium is unavailable for ${playwrightSource}; set UI_SYNC_BROWSER to a browser executable`);
    return;
  }
  const browser = await playwright.chromium.launch({ headless: true, executablePath });
  try {
    const page = await browser.newPage({ viewport: { width: 380, height: 560 } });
    await page.goto(pathToFileURL(path.resolve(__dirname, "../popup/popup.html")).href);
    await page.locator(".popup-scroll").waitFor({ state: "visible" });
    await page.locator("#stopBtn").waitFor({ state: "visible" });
    await page.waitForFunction(() => {
      const scroll = document.querySelector(".popup-scroll");
      return document.readyState === "complete" && scroll && scroll.scrollHeight > scroll.clientHeight;
    });
    const initial = await page.evaluate(() => {
      const scroll = document.querySelector(".popup-scroll");
      const dock = document.querySelector(".control-dock");
      const stop = document.querySelector("#stopBtn");
      const cards = [".connection-card", ".choices-card", ".font-card", ".active-card"].map((selector) => {
        const rect = document.querySelector(selector).getBoundingClientRect();
        return { selector, top: rect.top, bottom: rect.bottom };
      });
      const dockRect = dock.getBoundingClientRect();
      const stopRect = stop.getBoundingClientRect();
      return {
        scrollHeight: scroll.scrollHeight,
        clientHeight: scroll.clientHeight,
        cards,
        dockBottom: dockRect.bottom,
        stopBottom: stopRect.bottom,
        stopVisible: stopRect.width > 0 && stopRect.height > 0,
      };
    });
    assert.ok(initial.scrollHeight > initial.clientHeight, "form content should be scrollable");
    assert.ok(initial.stopVisible, "Stop must remain rendered while idle");
    assert.ok(initial.stopBottom <= 560, "Stop must fit inside the popup");
    assert.ok(initial.dockBottom <= 560, "action dock must fit inside the popup");
    assert.ok(initial.cards[0].bottom <= initial.cards[1].top);
    assert.ok(initial.cards[1].bottom <= initial.cards[2].top);
    assert.ok(initial.cards[2].bottom <= initial.cards[3].top);

    const dockBefore = await page.locator(".control-dock").boundingBox();
    await page.locator(".popup-scroll").evaluate((element) => { element.scrollTop = element.scrollHeight; });
    const dockAfter = await page.locator(".control-dock").boundingBox();
    assert.equal(dockAfter.y, dockBefore.y, "action dock should stay pinned while content scrolls");
    assert.ok(await page.locator(".active-card").isVisible(), "current tab card must remain reachable");
    await page.locator(".font-card").scrollIntoViewIfNeeded();
    assert.ok(await page.locator(".font-card").isVisible(), "subtitle text-size controls must remain reachable");
    const typography = await page.evaluate(() => {
      const fontCard = document.querySelector(".font-card");
      const activeCard = document.querySelector(".active-card");
      const sizeLabel = document.querySelector(".font-card .field-label");
      const choiceLabel = document.querySelector(".choices-card .field-label");
      const sizeStyle = getComputedStyle(sizeLabel);
      const choiceStyle = getComputedStyle(choiceLabel);
      return {
        fontLeft: fontCard.getBoundingClientRect().left,
        activeLeft: activeCard.getBoundingClientRect().left,
        fontPadding: getComputedStyle(fontCard).paddingLeft,
        sizeFont: sizeStyle.font,
        sizeLetterSpacing: sizeStyle.letterSpacing,
        sizeTransform: sizeStyle.textTransform,
        choiceFont: choiceStyle.font,
        choiceLetterSpacing: choiceStyle.letterSpacing,
        choiceTransform: choiceStyle.textTransform,
      };
    });
    assert.equal(typography.fontLeft, typography.activeLeft, "subtitle appearance card must align with neighboring cards");
    assert.equal(typography.fontPadding, "12px", "subtitle appearance card must use the shared card padding");
    assert.equal(typography.sizeFont, typography.choiceFont, "subtitle labels must use the shared field typography");
    assert.equal(typography.sizeLetterSpacing, typography.choiceLetterSpacing);
    assert.equal(typography.sizeTransform, typography.choiceTransform);
    await page.locator("#subtitleOriginalSize").fill("1.4");
    assert.equal(await page.locator("#subtitleOriginalSizeValue").textContent(), "140%");
  } finally {
    await browser.close();
  }
});

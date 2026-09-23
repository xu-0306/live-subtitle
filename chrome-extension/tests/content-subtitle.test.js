const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "content", "content.js"),
  "utf8",
);

class Element {
  constructor() {
    this.children = [];
    this.style = { setProperty() {} };
    this.dataset = {};
    this.classList = {
      values: new Set(),
      add: (...values) => values.forEach((value) => this.classList.values.add(value)),
      remove: (...values) => values.forEach((value) => this.classList.values.delete(value)),
      contains: (value) => this.classList.values.has(value),
    };
    this.textContent = "";
    this._innerHTML = "";
    Object.defineProperty(this, "innerHTML", {
      get: () => this._innerHTML,
      set: (value) => {
        this._innerHTML = value;
        if (value === "") {
          this.children.forEach((child) => { child.parentNode = null; });
          this.children = [];
        }
      },
    });
  }

  appendChild(child) {
    if (child.parentNode && child.parentNode !== this) {
      child.parentNode.removeChild(child);
    }
    if (this.children.includes(child)) return child;
    this.children.push(child);
    child.parentNode = this;
    return child;
  }

  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    child.parentNode = null;
    return child;
  }

  addEventListener() {}
  contains(target) {
    return target === this || this.children.some((child) => child.contains(target));
  }
  getBoundingClientRect() {
    return { left: 0, top: 0, right: 640, bottom: 160, width: 640, height: 160 };
  }
}

const listeners = [];
const body = new Element();
const document = {
  body,
  fullscreenElement: null,
  createElement: () => new Element(),
  addEventListener() {},
};
const chrome = {
  runtime: {
    lastError: null,
    onMessage: { addListener(callback) { listeners.push(callback); } },
  },
  storage: {
    local: {
      get(_keys, callback) { callback({}); },
      set() {},
    },
  },
};
const window = {
  innerWidth: 1280,
  innerHeight: 720,
  addEventListener() {},
};

vm.runInNewContext(`(function () {\n${source}\n})();`, {
  chrome,
  console: { log() {} },
  document,
  window,
  setTimeout,
  clearTimeout,
});

function subtitle(original, translated, seq, final = true) {
  sendMessage({ type: "subtitle", original, translated, seq, final });
}

function sendMessage(message) {
  const listener = listeners[0];
  assert.ok(listener, "content script must register its message listener");
  listener(message, null, () => {});
}

test("content upserts by seq, rejects stale finals, and applies CJK compaction", () => {
  const sentence = (char) => char.repeat(100) + "。";
  const longCjk = sentence("甲") + sentence("乙") + sentence("丙");
  subtitle(longCjk, "", 1, true);
  const card = body.children[0].children[0];
  const originalLines = card.children[0];
  const translatedLines = card.children[1];
  const firstLine = originalLines.children[0];
  assert.equal(firstLine.textContent, sentence("丙"), "CJK should keep one sentence");

  subtitle("newer", "", 2, true);
  subtitle(longCjk, "延遲抵達的翻譯", 1, true);
  assert.equal(originalLines.children.length, 2, "known older seq must update its row");
  assert.equal(translatedLines.children[0].textContent, "延遲抵達的翻譯");

  subtitle("unknown stale", "old translation", 0, true);
  assert.equal(originalLines.children.length, 2, "unknown stale final must not append a row");
  assert.equal(originalLines.children[1].textContent, "newer");

  subtitle("newer", "最新", 2, true);
  assert.equal(originalLines.children.length, 2, "same seq must upsert one row");
  assert.equal(originalLines.children[1].textContent, "newer");
  assert.equal(translatedLines.children[1].textContent, "最新");
});

test("live partial is emphasized while every committed line becomes history", () => {
  sendMessage({ type: "subtitle-start" });
  subtitle("settled", "已定稿", 10, true);
  subtitle("still changing", "仍在更新", 11, false);

  const card = body.children[0].children[0];
  const originalLines = card.children[0];
  const committed = originalLines.children[originalLines.children.length - 2];
  const live = originalLines.children[originalLines.children.length - 1];
  assert.match(committed.className, /stt-line-history/);
  assert.match(live.className, /stt-line-partial/);
  assert.doesNotMatch(live.className, /stt-line-history/);
});

test("capture stop fades the overlay and late status cannot revive it", async () => {
  sendMessage({ type: "subtitle-start" });
  sendMessage({ type: "subtitle-status", message: "Capture ready" });
  const overlay = body.children[0];
  assert.equal(overlay.classList.contains("visible"), true);

  sendMessage({ type: "subtitle-remove" });
  assert.equal(overlay.classList.contains("visible"), false, "stop must begin fading immediately");
  assert.equal(body.children.includes(overlay), true, "overlay remains mounted during its CSS fade");

  sendMessage({ type: "subtitle-status", message: "late Capture ready" });
  assert.equal(overlay.classList.contains("visible"), false, "late status must not reopen a stopped overlay");

  await new Promise((resolve) => setTimeout(resolve, 250));
  assert.equal(body.children.includes(overlay), false, "overlay is removed after the fade finishes");
});

test("a new capture cancels an in-flight overlay removal", async () => {
  sendMessage({ type: "subtitle-start" });
  sendMessage({ type: "subtitle-status", message: "Capture ready" });
  const overlay = body.children[0];
  sendMessage({ type: "subtitle-remove" });
  sendMessage({ type: "subtitle-start" });
  sendMessage({ type: "subtitle-status", message: "Starting capture" });

  await new Promise((resolve) => setTimeout(resolve, 250));
  assert.equal(body.children.includes(overlay), true, "old fade timer must not remove the new capture overlay");
  assert.equal(overlay.classList.contains("visible"), true);
});

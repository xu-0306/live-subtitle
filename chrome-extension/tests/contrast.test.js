const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");

// Every text colour the popup and Options use must reach WCAG AA (4.5:1) on the surface it sits on, in both themes.
test("shared text colours meet WCAG AA in both themes", () => {
  const css = fs.readFileSync(path.join(__dirname, "../popup/popup.css"), "utf8");
  const blocks = [...css.matchAll(/:root(?:\[data-theme="dark"\])?\s*\{([^}]+)\}/g)];
  const tokens = (body) => Object.fromEntries([...body.matchAll(/--([\w-]+):\s*(#[\da-f]{6})/gi)].map((m) => [m[1], m[2]]));
  const light = tokens(blocks[0][1]);
  const dark = { ...light, ...tokens(blocks[1][1]) };
  const rgb = (hex) => hex.match(/[\da-f]{2}/gi).map((part) => parseInt(part, 16));
  const mix = (a, b, weight) => "#" + rgb(a).map((v, i) => Math.round(v * weight + rgb(b)[i] * (1 - weight)).toString(16).padStart(2, "0")).join("");
  const luminance = (hex) => rgb(hex).map((v) => v / 255)
    .map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
    .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
  const ratio = (a, b) => {
    const [lo, hi] = [luminance(a), luminance(b)].sort((x, y) => x - y);
    return (hi + 0.05) / (lo + 0.05);
  };
  for (const [theme, t] of Object.entries({ light, dark })) {
    const pairs = [
      ["faint", t["surface-muted"]], ["faint", t["surface-strong"]], ["muted", t["surface-muted"]],
      ["blue", t.surface], ["blue", t["blue-soft"]], ["primary-text", t.blue],
      ["success", mix(t.success, t.surface, 0.13)], ["warning", mix(t.warning, t.surface, 0.05)], ["danger", mix(t.danger, t.surface, 0.12)],
    ];
    for (const [name, background] of pairs) {
      const value = ratio(t[name], background);
      assert.ok(value >= 4.5, `${theme}: ${name} ${t[name]} on ${background} is ${value.toFixed(2)}:1`);
    }
  }
});

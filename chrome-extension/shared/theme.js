(function (global) {
  "use strict";

  const THEME_KEY = "extensionTheme";
  const THEMES = Object.freeze(["system", "light", "dark"]);

  function normalizeTheme(value) {
    const normalized = String(value || "system").trim().toLowerCase();
    return THEMES.includes(normalized) ? normalized : "system";
  }

  function mediaMatcher(source) {
    const matcher = source || global.matchMedia;
    if (typeof matcher !== "function") return null;
    // Window.matchMedia is a Web IDL method. Passing the function around
    // without its receiver throws in Chromium, which made System look like
    // Light even while the browser was in dark mode.
    return matcher === global.matchMedia ? matcher.bind(global) : matcher;
  }

  function resolvedTheme(preference, matchMedia = global.matchMedia) {
    const normalized = normalizeTheme(preference);
    if (normalized !== "system") return normalized;
    try { return mediaMatcher(matchMedia)?.("(prefers-color-scheme: dark)")?.matches ? "dark" : "light"; }
    catch { return "light"; }
  }

  function applyTheme(root, preference, matchMedia = global.matchMedia) {
    const normalized = normalizeTheme(preference);
    if (!root) return normalized;
    root.dataset.themePreference = normalized;
    root.dataset.theme = resolvedTheme(normalized, matchMedia);
    return normalized;
  }

  function watchSystemTheme(root, preference, callback, matchMedia = global.matchMedia) {
    let media;
    try { media = mediaMatcher(matchMedia)?.("(prefers-color-scheme: dark)"); } catch { media = null; }
    if (!media || typeof media.addEventListener !== "function") return () => {};
    const handler = () => {
      if (normalizeTheme(preference) === "system") applyTheme(root, preference, matchMedia);
      if (typeof callback === "function") callback(resolvedTheme(preference, matchMedia));
    };
    media.addEventListener("change", handler);
    return () => media.removeEventListener?.("change", handler);
  }

  function storageGet(storageArea, key) {
    if (!storageArea?.get) return Promise.resolve({});
    return new Promise((resolve) => storageArea.get([key], (data) => resolve(data || {})));
  }

  function storageSet(storageArea, values) {
    if (!storageArea?.set) return Promise.resolve();
    return new Promise((resolve) => storageArea.set(values, () => resolve()));
  }

  global.STTTheme = Object.freeze({
    THEME_KEY,
    THEMES,
    normalizeTheme,
    resolvedTheme,
    applyTheme,
    watchSystemTheme,
    storageGet,
    storageSet,
  });
  if (typeof module !== "undefined") module.exports = global.STTTheme;
})(globalThis);

(function (global) {
  "use strict";

  if (global.document?.body?.dataset?.page !== "options") return;

  const popup = global.STTPopup;
  const state = popup?.state || {};
  const KEYS = {
    original: "subtitleOriginalScale",
    translated: "subtitleTranslatedScale",
    legacy: "subtitleFontScale",
    opacity: "subtitleOverlayOpacity",
    history: "subtitleHistoryLines",
    showPartial: "subtitleShowPartial",
  };

  function $(id) { return global.document?.getElementById?.(id) || null; }
  function storageGet(keys) {
    if (!global.chrome?.storage?.local?.get) return Promise.resolve({});
    return new Promise((resolve) => {
      try { global.chrome.storage.local.get(keys, (data) => resolve(data || {})); } catch { resolve({}); }
    });
  }
  function storageSet(values) {
    if (!global.chrome?.storage?.local?.set) return Promise.resolve();
    return new Promise((resolve) => {
      try { global.chrome.storage.local.set(values, () => resolve()); } catch { resolve(); }
    });
  }
  function setStatus(message, stateName = "idle") {
    popup?.setStatus?.(message, stateName);
  }
  function clamp(value, min, max, fallback) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? Math.min(max, Math.max(min, numeric)) : fallback;
  }

  function backendKey(catalog, wsUrl) {
    if (!catalog?.server_id) return "";
    const canonicalUrl = global.STTDesktopCatalog?.canonicalUrl?.(wsUrl) || String(wsUrl || "").trim().replace(/\/$/, "");
    return `${catalog.server_id}|${canonicalUrl}`;
  }

  function renderCatalogDetails() {
    const result = state.catalogResult;
    const details = $("catalogDetails");
    const revision = $("catalogRevision");
    if (result?.catalog?.kind === "v2") {
      const catalog = result.catalog;
      if (revision) revision.textContent = "Synced";
      if (details) {
        details.textContent = result.offline ? "Offline snapshot · reconnect to verify choices." : "Managed by desktop app.";
      }
      return;
    }
    if (result?.upgradeNeeded) {
      if (revision) revision.textContent = "Upgrade needed";
      if (details) details.textContent = "Update the desktop app to use its shared catalog.";
      return;
    }
    if (result?.offline) {
      if (revision) revision.textContent = state.catalog ? "Stale" : "Offline";
      if (details) details.textContent = state.catalog ? "Showing the last known choices until the desktop app reconnects." : "Connect the desktop app to load managed services.";
      return;
    }
    if (revision) revision.textContent = "Not synced";
  }

  function wireAppearance() {
    const original = $("subtitleOriginalSize");
    const translated = $("subtitleTranslatedSize");
    const opacity = $("overlayOpacity");
    const history = $("subtitleHistoryLines");
    const showPartial = $("subtitleShowPartial");
    const update = async (key, value, output, formatter) => {
      if (output) output.textContent = formatter(value);
      await storageSet({ [key]: value });
    };
    original?.addEventListener("input", () => update(KEYS.original, clamp(original.value, 0.7, 1.6, 1), $("subtitleOriginalSizeValue"), (value) => `${Math.round(value * 100)}%`));
    translated?.addEventListener("input", () => update(KEYS.translated, clamp(translated.value, 0.7, 1.6, 1), $("subtitleTranslatedSizeValue"), (value) => `${Math.round(value * 100)}%`));
    opacity?.addEventListener("input", () => update(KEYS.opacity, clamp(opacity.value, 0.35, 1, 0.85), $("overlayOpacityValue"), (value) => `${Math.round(value * 100)}%`));
    history?.addEventListener("input", () => update(KEYS.history, Math.round(clamp(history.value, 1, 4, 2)), $("subtitleHistoryLinesValue"), (value) => String(value)));
    showPartial?.addEventListener("change", () => storageSet({ [KEYS.showPartial]: Boolean(showPartial.checked) }));
  }

  async function loadAppearance() {
    const data = await storageGet(Object.values(KEYS));
    const legacyScale = typeof data[KEYS.legacy] === "number" ? data[KEYS.legacy] : 1;
    const original = clamp(data[KEYS.original] ?? legacyScale, 0.7, 1.6, 1);
    const translated = clamp(data[KEYS.translated] ?? legacyScale, 0.7, 1.6, 1);
    const opacity = clamp(data[KEYS.opacity] ?? 0.85, 0.35, 1, 0.85);
    const history = Math.round(clamp(data[KEYS.history] ?? 2, 1, 4, 2));
    const showPartial = data[KEYS.showPartial] !== false;
    for (const [id, value] of [["subtitleOriginalSize", original], ["subtitleTranslatedSize", translated], ["overlayOpacity", opacity], ["subtitleHistoryLines", history]]) {
      const input = $(id); if (input) input.value = String(value);
    }
    for (const [id, value] of [["subtitleOriginalSizeValue", `${Math.round(original * 100)}%`], ["subtitleTranslatedSizeValue", `${Math.round(translated * 100)}%`], ["overlayOpacityValue", `${Math.round(opacity * 100)}%`], ["subtitleHistoryLinesValue", String(history)]]) {
      const output = $(id); if (output) output.textContent = value;
    }
    const checkbox = $("subtitleShowPartial"); if (checkbox) checkbox.checked = showPartial;
  }

  function sendImportMessage(wsUrl, payload) {
    const WebSocketClass = global.WebSocket;
    if (!WebSocketClass) return Promise.reject(new Error("WebSocket is unavailable"));
    return new Promise((resolve, reject) => {
      let socket; let finished = false;
      const timer = global.setTimeout(() => finish(new Error("Profile import timed out")), 2600);
      function finish(error, value) {
        if (finished) return;
        finished = true; global.clearTimeout(timer);
        try { socket?.close(); } catch { /* best effort */ }
        if (error) reject(error); else resolve(value);
      }
      try {
        socket = new WebSocketClass(wsUrl);
        socket.addEventListener("open", () => socket.send(JSON.stringify(payload)));
        socket.addEventListener("message", (event) => {
          let response; try { response = JSON.parse(event.data); } catch { return; }
          if (response.type === "desktop_import_result") finish(null, response);
          else if (response.type === "error") finish(null, { ok: false, error: String(response.message || "Profile import failed") });
        });
        socket.addEventListener("error", () => finish(new Error("Desktop server is unavailable")));
        socket.addEventListener("close", () => { if (!finished) finish(new Error("Desktop server closed the import connection")); });
      } catch (error) { finish(error); }
    });
  }

  async function maybeShowMigration() {
    const card = $("migrationCard");
    if (!card || !Array.isArray(state.legacyModels) || !state.legacyModels.length) return;
    const migration = await storageGet([global.STTProfileMigration?.STATE_KEY || "desktopProfileMigration"]);
    const migrationState = migration[global.STTProfileMigration?.STATE_KEY || "desktopProfileMigration"];
    const migrationBackendKey = backendKey(state.catalog, state.wsUrl);
    if (migrationState?.status === "complete" && !(migrationState.pendingIds || []).length && (!migrationBackendKey || migrationState.backendKey === migrationBackendKey)) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const summary = $("migrationSummary");
    if (summary) summary.textContent = migrationState?.status === "partial"
      ? `${migrationState.pendingIds?.length || 0} saved service(s) still need attention.`
      : "Move older saved services into the desktop app.";
    const status = $("migrationStatus");
    if (status && migrationState?.errors?.length) status.textContent = migrationState.errors.map((item) => `${item.id}: ${item.message}`).join(" · ");
  }

  async function importProfiles() {
    const button = $("migrateProfilesBtn");
    if (button) button.disabled = true;
    const resultState = state.catalogResult;
    if (resultState?.upgradeNeeded || !state.catalog?.capabilities?.includes("profile_import")) {
      const status = $("migrationStatus"); if (status) status.textContent = "Update the desktop app before importing saved services.";
      if (button) button.disabled = false;
      return;
    }
    const migrationApi = global.STTProfileMigration;
    if (!migrationApi?.importLegacyProfiles) {
      if (button) button.disabled = false;
      return;
    }
    const status = $("migrationStatus"); if (status) status.textContent = "Preparing saved services…";
    const ws = state.wsUrl;
    const migrationBackendKey = backendKey(state.catalog, state.wsUrl) || "unknown-backend";
    let result = await migrationApi.importLegacyProfiles({
      models: state.legacyModels,
      storageArea: global.chrome?.storage?.local,
      expectedRevision: state.catalog?.revision,
      backendKey: migrationBackendKey,
      getApiKey: (id) => global.STTConnectionSettings?.getApiKey?.(id) || Promise.resolve(""),
      send: (payload) => sendImportMessage(ws, payload),
    });
    const conflictText = [
      result.response?.error,
      result.error,
      ...(result.response?.errors || []),
      ...(result.errors || []),
    ].map((item) => typeof item === "object" ? `${item.id || ""} ${item.message || item.error || ""}` : String(item || "")).join(" ");
    if (!result.ok && /revision|conflict/i.test(conflictText)) {
      await popup?.refreshDesktopServices?.();
      result = await migrationApi.importLegacyProfiles({
        models: state.legacyModels,
        storageArea: global.chrome?.storage?.local,
        expectedRevision: state.catalog?.revision,
        backendKey: migrationBackendKey,
        getApiKey: (id) => global.STTConnectionSettings?.getApiKey?.(id) || Promise.resolve(""),
        send: (payload) => sendImportMessage(ws, payload),
      });
    }
    if (result.ok) {
      if (status) status.textContent = result.partial ? `Imported ${Object.keys(result.mapping || {}).length}; ${result.pendingIds.length} still pending.` : "Saved services imported. Refreshing catalog…";
      setStatus(result.partial ? "Some services need attention" : "Services imported", result.partial ? "warning" : "active");
      await popup?.refreshDesktopServices?.();
      await maybeShowMigration();
    } else if (status) {
      const errors = result.errors || result.response?.errors || [];
      status.textContent = errors.length ? errors.map((item) => `${item.id}: ${item.message}`).join(" · ") : String(result.error || result.response?.error || "Import failed. Your saved services are still available to retry.");
      setStatus("Service import needs attention", "error");
    }
    if (button) button.disabled = false;
  }

  function wireOptions() {
    popup?.wireEvents?.();
    wireAppearance();
    $("migrateProfilesBtn")?.addEventListener("click", importProfiles);
  }

  async function init() {
    wireOptions();
    await popup?.loadState?.();
    await loadAppearance();
    await maybeShowMigration();
    await popup?.refreshCaptureStatus?.();
    popup?.startStatusPolling?.();
    await popup?.refreshDesktopServices?.();
    popup?.startCatalogPolling?.();
    renderCatalogDetails();
    await maybeShowMigration();
    global.setInterval?.(renderCatalogDetails, 300);
  }

  init().catch((error) => setStatus(`Options error: ${error?.message || error}`, "error"));
})(globalThis);

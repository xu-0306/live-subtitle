(function (global) {
  "use strict";

  const DEFAULT_WS_URL = "ws://127.0.0.1:8765/asr";
  const KEYS = Object.freeze({
    wsUrl: "wsUrl",
    selectedTranslation: "selectedTranslationModel",
    selectedStt: "selectedSttModel",
    sttLegacy: "sttModelSize",
    sttMode: "sttModelMode",
    language: "translationTargetLanguage",
    languageMode: "translationTargetLanguageMode",
    partial: "translationIncludePartials",
    partialMode: "translationPartialMode",
    theme: "extensionTheme",
    originalScale: "subtitleOriginalScale",
    translatedScale: "subtitleTranslatedScale",
    legacyScale: "subtitleFontScale",
    opacity: "subtitleOverlayOpacity",
    history: "subtitleHistoryLines",
    showPartial: "subtitleShowPartial",
    legacyModels: "translationModels",
  });
  const DEFAULT_STT_MODEL = "medium";
  const FALLBACK_STT_MODELS = [
    { id: "tiny", name: "Whisper Tiny", available: true, status: "compatibility" },
    { id: "base", name: "Whisper Base", available: true, status: "compatibility" },
    { id: "small", name: "Whisper Small", available: true, status: "compatibility" },
    { id: "medium", name: "Whisper Medium", available: true, status: "compatibility" },
    { id: "large-v3", name: "Whisper Large v3", available: true, status: "compatibility" },
  ];
  const LANGUAGE_OPTIONS = [
    ["en", "English"], ["ja", "Japanese"], ["zh-TW", "Chinese (Traditional)"],
    ["zh-CN", "Chinese (Simplified)"], ["ko", "Korean"], ["fr", "French"],
    ["de", "German"], ["es", "Spanish"], ["pt", "Portuguese"], ["it", "Italian"],
    ["ru", "Russian"], ["id", "Indonesian"], ["vi", "Vietnamese"], ["th", "Thai"],
  ];
  const DESKTOP_DEFAULT = "__desktop-default__";
  const OTHER_LANGUAGE = "__other-language__";

  function $(id) { return global.document?.getElementById?.(id) || null; }
  function storageGet(keys) {
    if (!global.chrome?.storage?.local?.get) return Promise.resolve({});
    return new Promise((resolve) => {
      try { global.chrome.storage.local.get(keys, (data) => resolve(data || {})); }
      catch { resolve({}); }
    });
  }
  function storageSet(values) {
    if (!global.chrome?.storage?.local?.set) return Promise.reject(new Error("Settings storage is unavailable"));
    return new Promise((resolve, reject) => {
      try { global.chrome.storage.local.set(values, () => {
        const error = global.chrome.runtime?.lastError;
        if (error) reject(new Error(error.message)); else resolve();
      }); } catch (error) { reject(error); }
    });
  }
  async function persistSettings(values, appliesNow = false) {
    try {
      await storageSet(values);
      setStatus(appliesNow ? "Saved · applies now" : "Saved · applies to next capture");
      state.savedHintUntil = Date.now() + 2500;
    } catch (error) {
      setStatus(`Could not save: ${error.message || error}`, "error");
    }
  }
  function sendRuntime(message) {
    if (!global.chrome?.runtime?.sendMessage) return Promise.resolve(null);
    return new Promise((resolve) => {
      try {
        global.chrome.runtime.sendMessage(message, (response) => resolve(response || null));
      } catch { resolve(null); }
    });
  }
  function text(value) { return String(value ?? ""); }
  function hasOwn(object, key) { return Object.prototype.hasOwnProperty.call(object || {}, key); }
  function normalizeLanguage(value) { return text(value).trim(); }
  function clampFontScale(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? Math.min(1.6, Math.max(0.7, numeric)) : 1;
  }
  function renderFontScale(id, outputId, value) {
    const scale = clampFontScale(value);
    const input = $(id); if (input) input.value = String(scale);
    const output = $(outputId); if (output) output.textContent = `${Math.round(scale * 100)}%`;
    return scale;
  }

  function fallbackNormalizeCatalog(payload) {
    if (!payload || typeof payload !== "object") return null;
    const selection = (item) => item?.selection && typeof item.selection === "object"
      ? ["local", "profile", "none"].includes(text(item.selection.kind))
        ? { kind: text(item.selection.kind), ...(item.selection.id ? { id: text(item.selection.id) } : {}) }
        : undefined
      : undefined;
    if (payload.type === "desktop_catalog" && Number(payload.version) === 2) {
      const validIdentity = (value) => {
        const normalized = text(value).trim().toLowerCase();
        return Boolean(normalized) && !["unknown", "unknown-server", "unknown-instance", "undefined", "null"].includes(normalized);
      };
      if (!validIdentity(payload.server_id) || !validIdentity(payload.instance_id)) return null;
      return {
        kind: "v2", version: 2, upgradeNeeded: false,
        server_id: text(payload.server_id), instance_id: text(payload.instance_id),
        revision: Number(payload.revision || 0),
        models: (Array.isArray(payload.models) ? payload.models : []).map((item) => ({
          id: text(item.id), name: text(item.name || item.id), engine: text(item.engine || "desktop"),
          available: item.available !== false, status: text(item.status || "ready"),
          ...(selection(item) ? { selection: selection(item) } : {}),
          ...(item.reason ? { reason: text(item.reason) } : {}),
        })).filter((item) => item.id),
        stt_models: (Array.isArray(payload.stt_models) ? payload.stt_models : []).map((item) => ({
          id: text(item.id || item.model), name: text(item.name || item.id || item.model),
          available: item.available !== false, status: text(item.status || "ready"),
          ...(item.selection ? { selection: { model: text(item.selection.model || item.model), ...(item.selection.backend ? { backend: text(item.selection.backend) } : {}) } } : item.model ? { selection: { model: text(item.model), ...(item.backend ? { backend: text(item.backend) } : {}) } } : {}),
        })).filter((item) => item.id),
        defaults: payload.defaults || {}, active: payload.active || {}, capabilities: payload.capabilities || [],
      };
    }
    if (payload.type === "translation_catalog" && Number(payload.version) === 1) {
      return { kind: "legacy", version: 1, upgradeNeeded: true, models: Array.isArray(payload.models) ? payload.models : [], active: payload.active || {} };
    }
    return null;
  }

  // Test harnesses and an old backend may not load catalog.js. This adapter
  // still asks for v2 first, then detects v1 as an upgrade state.
  class CompatibilityCatalogClient {
    constructor() { this.generation = 0; this.catalog = null; }
    refresh(url) {
      const generation = ++this.generation;
      const client = this;
      const WebSocketClass = global.WebSocket;
      if (!WebSocketClass) return Promise.resolve({ ok: false, offline: true, error: new Error("WebSocket is unavailable") });
      return new Promise((resolve) => {
        let socket = null; let finished = false; let legacySent = false;
        const timeout = global.setTimeout(() => finish({ ok: false, offline: true, error: new Error("Desktop catalog timed out") }), 1800);
        const fallback = global.setTimeout(() => {
          if (finished || legacySent) return;
          legacySent = true;
          try { socket?.close(); } catch { /* best effort */ }
          try {
            socket = new WebSocketClass(url);
            socket.addEventListener("open", () => socket.send(JSON.stringify({ type: "translation_catalog" })));
            socket.addEventListener("message", (event) => {
              let payload; try { payload = JSON.parse(event.data); } catch { return; }
              const normalized = fallbackNormalizeCatalog(payload);
              if (normalized) finish(normalized.kind === "legacy" ? { ok: false, upgradeNeeded: true, legacy: normalized } : { ok: true, catalog: normalized });
            });
            socket.addEventListener("error", () => finish({ ok: false, offline: true, error: new Error("Desktop server is unavailable") }));
          } catch (error) { finish({ ok: false, offline: true, error }); }
        }, 300);
        function finish(result) {
          if (finished) return;
          finished = true;
          global.clearTimeout(timeout); global.clearTimeout(fallback);
          try { socket?.close(); } catch { /* best effort */ }
          if (generation !== client.generation) { resolve({ ignored: true }); return; }
          if (result.catalog) { client.catalog = result.catalog; }
          resolve(result);
        }
        try {
          socket = new WebSocketClass(url);
          socket.addEventListener("open", () => socket.send(JSON.stringify({ type: "desktop_catalog", version: 2 })));
          socket.addEventListener("message", (event) => {
            let payload; try { payload = JSON.parse(event.data); } catch { return; }
            const normalized = fallbackNormalizeCatalog(payload);
            if (normalized) finish(normalized.kind === "legacy" ? { ok: false, upgradeNeeded: true, legacy: normalized } : { ok: true, catalog: normalized });
          });
          socket.addEventListener("error", () => { /* give old servers a short compatibility window */ });
        } catch (error) { finish({ ok: false, offline: true, error }); }
      });
    }
  }

  function createCatalogClient() {
    if (global.STTDesktopCatalog?.CatalogClient) {
      return new global.STTDesktopCatalog.CatalogClient({ timeoutMs: 1200 });
    }
    return new CompatibilityCatalogClient();
  }

  const state = {
    wsUrl: DEFAULT_WS_URL,
    selectedTranslation: "",
    selectedStt: "",
    sttMode: "inherit",
    targetLanguage: "",
    targetLanguageMode: "inherit",
    languageOther: false,
    partial: false,
    partialMode: "inherit",
    theme: "system",
    legacyModels: [],
    catalog: null,
    catalogResult: null,
    catalogClient: createCatalogClient(),
    statusSnapshot: null,
    statusTimer: null,
    catalogTimer: null,
    catalogRefreshPromise: null,
    catalogRefreshUrl: "",
    catalogRetryAfter: 0,
    themeStop: null,
    focusRefreshBound: false,
    initialized: false,
    savedHintUntil: 0,
    failed: false,
  };

  function updateStatusOverflow(element) {
    const overflow = element.scrollHeight > element.clientHeight;
    // The cue means "more text below", so it goes away once the end is in view.
    if (element.dataset) element.dataset.overflow = String(overflow && element.scrollTop + element.clientHeight < element.scrollHeight - 1);
    if (overflow) element.setAttribute?.("tabindex", "0");
    else element.removeAttribute?.("tabindex");
  }
  function setStatus(message, stateName = "idle") {
    state.savedHintUntil = 0;
    const element = $("status");
    if (!element) return;
    element.textContent = text(message);
    if (element.dataset) element.dataset.state = stateName;
    updateStatusOverflow(element);
  }
  // A failed start/stop stays on screen until the next attempt; the 1 s status poll would otherwise replace it with "Idle".
  function fail(message) {
    setStatus(message, "error");
    state.failed = true;
  }
  function setCatalogStatus(message, stateName = "idle") {
    const element = $("desktopServicesStatus");
    if (element && element.textContent !== text(message)) element.textContent = text(message);
    const dot = $("catalogStateDot");
    if (dot?.dataset) dot.dataset.state = stateName;
  }
  function setButtons(capturing, starting = false) {
    const start = $("startBtn"); const stop = $("stopBtn");
    if (start) start.disabled = Boolean(capturing || starting);
    if (stop) stop.disabled = !Boolean(capturing || starting);
  }
  function applyThemePreference(preference) {
    const normalized = global.STTTheme?.normalizeTheme ? global.STTTheme.normalizeTheme(preference) : (["system", "light", "dark"].includes(preference) ? preference : "system");
    state.theme = normalized;
    if (global.STTTheme?.applyTheme) global.STTTheme.applyTheme(global.document?.documentElement, normalized);
    state.themeStop?.();
    state.themeStop = global.STTTheme?.watchSystemTheme?.(
      global.document?.documentElement,
      normalized,
      () => {},
    ) || null;
    const select = $("themeSelect"); if (select) select.value = normalized;
  }
  function sameAsDesktop(name) { return name ? `Same as desktop · ${name}` : "Same as desktop"; }
  function languageName(code) {
    const value = normalizeLanguage(code);
    return LANGUAGE_OPTIONS.find(([id]) => id.toLowerCase() === value.toLowerCase())?.[1] || value;
  }
  function serviceName(selection) {
    if (selection?.kind === "none") return "No translation";
    if (!selection?.id) return "";
    const model = (state.catalog?.models || []).find((item) => item.selection?.kind === selection.kind && item.selection?.id === selection.id);
    return model?.name || text(selection.id);
  }
  function speechItem(model) {
    return model ? sttOptions().find((item) => (item.selection?.model || item.id) === model) : undefined;
  }
  function speechName(model) {
    if (!model) return "";
    return speechItem(model)?.name || text(model);
  }
  function desktopLanguageName() { return languageName(state.catalog?.defaults?.translation?.target_language); }
  function desktopServiceName() { return serviceName(state.catalog?.defaults?.translation?.selection); }
  function desktopSpeechModel() { return state.catalog?.defaults?.stt?.model || state.catalog?.defaults?.stt?.selection?.model; }
  function desktopSpeechName() { return speechName(desktopSpeechModel()); }
  function desktopPartialName() {
    const partial = state.catalog?.defaults?.translation?.partial;
    return typeof partial === "boolean" ? (partial ? "While speaking" : "After each sentence") : "";
  }
  // A saved choice the catalog no longer lists is "removed" only when the desktop app actually answered; otherwise it is just unreachable.
  function staleName(what) { return state.catalogResult?.ok ? `Removed ${what}` : "Saved choice · desktop not reachable"; }
  function builtInSelections() {
    return global.STTDesktopCatalog?.builtInSelections?.() || [
      { id: "desktop-default", name: "Desktop default", engine: "desktop", system: true, available: true, status: "default" },
      { id: "none", name: "None · subtitles only", engine: "noop", system: true, available: true, status: "ready", selection: { kind: "none", id: "none" } },
    ];
  }
  function modelOptions() {
    const options = builtInSelections();
    const catalogModels = state.catalog?.models || (state.catalogResult?.legacy?.models || []);
    for (const model of catalogModels) {
      if (!model?.id || options.some((entry) => entry.id === model.id)) continue;
      options.push({ ...model, engine: model.engine || "desktop", name: model.name || model.id });
    }
    for (const model of state.legacyModels) {
      if (!model?.id || options.some((entry) => entry.id === model.id)) continue;
      options.push({ ...model, legacy: true, name: model.name || model.id, available: model.available !== false });
    }
    if (state.selectedTranslation && !options.some((entry) => entry.id === state.selectedTranslation)) {
      const raw = state.selectedTranslation;
      const colon = raw.indexOf(":");
      const kind = colon > 0 && ["local", "profile"].includes(raw.slice(0, colon)) ? raw.slice(0, colon) : "unknown";
      const legacyCompatibility = !global.STTDesktopCatalog && !state.catalogResult;
      options.push({ id: raw, name: staleName("service"), engine: "desktop", stale: true, available: legacyCompatibility, status: "removed", selection: kind === "unknown" ? undefined : { kind, id: raw.slice(colon + 1) } });
    }
    return options;
  }
  function sttOptions() {
    const options = (state.catalog?.stt_models || []).slice();
    // These names only exist for a v1 compatibility response. A v2 catalog
    // with no entries is authoritative and must remain empty instead of
    // inventing models the desktop service did not advertise.
    const legacyOnly = state.catalogResult?.upgradeNeeded || state.catalogResult?.legacy?.kind === "legacy";
    if (!options.length && legacyOnly) options.push(...FALLBACK_STT_MODELS);
    if (state.selectedStt && !options.some((entry) => entry.id === state.selectedStt)) {
      options.push({ id: state.selectedStt, name: staleName("speech recognition model"), available: false, stale: true, status: "removed", selection: { model: state.selectedStt } });
    }
    return options;
  }
  function optionLabel(item) {
    if (item.id === "desktop-default") return sameAsDesktop(desktopServiceName());
    if (item.id === "none") return "None · subtitles only";
    const label = text(item.name || item.id);
    if (item.stale) return label;
    return item.available === false ? `${label} · unavailable` : label;
  }
  function renderSelect(id, options, selectedId) {
    const select = $(id); if (!select || !global.document?.createElement) return;
    select.innerHTML = "";
    for (const item of options) {
      const option = global.document.createElement("option");
      option.value = item.id; option.textContent = optionLabel(item);
      if (item.available === false && item.id !== selectedId) option.disabled = true;
      select.appendChild(option);
    }
    if (selectedId) select.value = selectedId;
  }
  function showHint(id, message, isError = false) {
    const hint = $(id); if (!hint) return;
    hint.textContent = message;
    hint.hidden = !message;
    if (hint.dataset) hint.dataset.state = isError ? "error" : "ready";
  }
  function renderApplyHint(running) {
    const hint = $("applyHint");
    if (hint) hint.textContent = running
      ? "A capture is running. Stop and start it again to use these choices."
      : "Changes apply when you start capture.";
  }
  function renderLanguage() {
    const override = state.targetLanguageMode === "override";
    const known = LANGUAGE_OPTIONS.find(([id]) => id.toLowerCase() === state.targetLanguage.toLowerCase())?.[0];
    const other = override && (state.languageOther || !known);
    renderSelect("translationLanguageSelect", [
      { id: DESKTOP_DEFAULT, name: sameAsDesktop(desktopLanguageName()), available: true },
      ...LANGUAGE_OPTIONS.map(([id, name]) => ({ id, name, available: true })),
      { id: OTHER_LANGUAGE, name: "Other language…", available: true },
    ], !override ? DESKTOP_DEFAULT : other ? OTHER_LANGUAGE : known);
    const custom = $("translationLanguageCustom");
    if (custom) {
      custom.hidden = !other;
      if (other && normalizeLanguage(custom.value) !== state.targetLanguage) custom.value = state.targetLanguage;
    }
    const hint = $("translationLanguageHint");
    if (hint) hint.textContent = override
      ? "Used for every tab in this browser until you choose Same as desktop."
      : "Follows the language set in the desktop app.";
  }
  function renderChoices() {
    renderSelect("translationModelSelect", modelOptions(), state.selectedTranslation);
    const sttItems = [
      // The desktop app may default to a model it has not downloaded yet; flag it like any other missing model.
      { id: DESKTOP_DEFAULT, name: sameAsDesktop(desktopSpeechName()), available: speechItem(desktopSpeechModel())?.available !== false },
      ...sttOptions(),
    ];
    const sttSelection = state.sttMode === "override" && state.selectedStt ? state.selectedStt : DESKTOP_DEFAULT;
    renderSelect("sttModelSizeSelect", sttItems, sttSelection);
    renderLanguage();
    const speechMissing = sttItems.find((item) => item.id === sttSelection)?.available === false;
    showHint("sttSelectionHint", speechMissing ? "This speech recognition model is not installed. Download it in the desktop app." : "", speechMissing);
    const behavior = $("translationBehaviorSelect");
    if (behavior) {
      if (behavior.options?.[0]) behavior.options[0].textContent = sameAsDesktop(desktopPartialName());
      behavior.value = state.partialMode === "inherit" ? "inherit" : (state.partial ? "partial" : "final");
    }
    const selected = modelOptions().find((item) => item.id === state.selectedTranslation);
    const removed = Boolean(selected?.stale && state.catalogResult?.ok);
    const unavailable = selected?.available === false;
    showHint(
      "translationSelectionHint",
      removed ? "This service was removed in the desktop app. Pick another."
        : unavailable ? "This service is unavailable. Check the desktop app, then press ↻."
          : selected?.id === "none" ? "Subtitles only, no translation." : "",
      removed || unavailable,
    );
    renderApplyHint(Boolean(state.statusSnapshot?.running));
  }
  function renderActiveSnapshot(snapshot, fallbackStatus) {
    const title = $("activeCaptureTitle"); const phase = $("activeCapturePhase"); const summary = $("activeCaptureSummary"); const occupancy = $("occupancySummary");
    const active = snapshot && typeof snapshot === "object" ? snapshot : null;
    const running = Boolean(active?.running || active?.capturing);
    renderApplyHint(running);
    if (title) title.textContent = running ? (active.name || "Capturing this tab") : "Not capturing";
    if (phase) phase.textContent = text(active?.phase || fallbackStatus || (running ? "Capturing" : "Idle"));
    if (summary) {
      const names = running && (active.selection || active.target_language || active.stt)
        ? [
          active.selection ? serviceName(active.selection) : desktopServiceName(),
          active.stt?.model ? speechName(active.stt.model) : desktopSpeechName(),
          active.target_language ? languageName(active.target_language) : desktopLanguageName(),
        ].filter(Boolean).join(" · ")
        : "";
      summary.textContent = names || (running ? "This tab is using its own capture settings." : "Start capture to see what this tab is using.");
    }
    if (occupancy) {
      const count = Number(active?.captures ?? active?.capture_count ?? 0);
      occupancy.textContent = count > 1 ? `${count} tabs share the active desktop service.` : active?.occupancy || "";
    }
  }
  function renderCatalogResult(result) {
    state.catalogResult = result || null;
    if (result?.catalog?.kind === "v2") state.catalog = result.catalog;
    if (result?.ok && result.catalog?.kind === "v2") {
      setCatalogStatus("Connected to desktop app", "ready");
    } else if (result?.upgradeNeeded) {
      setCatalogStatus("Desktop app is too old · please update it", "warning");
    } else if (result?.offline) {
      const stale = Boolean(state.catalog || result.cached);
      if (result.cached?.kind === "v2" && !state.catalog) state.catalog = result.cached;
      setCatalogStatus(stale ? "Desktop app not reachable · showing saved choices" : "Desktop app not found · start it, then press ↻", "offline");
    }
    renderChoices();
  }
  function resetCatalogForEndpoint() {
    state.catalog = null;
    state.catalogResult = null;
    state.catalogRetryAfter = 0;
    if (state.catalogClient) {
      state.catalogClient.catalog = null;
      state.catalogClient.lastResult = null;
    }
    renderChoices();
  }
  async function refreshDesktopServices(options = {}) {
    const ws = (state.wsUrl || DEFAULT_WS_URL).trim() || DEFAULT_WS_URL;
    state.wsUrl = ws;
    if (!options.manual && Date.now() < state.catalogRetryAfter) return state.catalogResult;
    if (state.catalogRefreshPromise && state.catalogRefreshUrl === ws) return state.catalogRefreshPromise;
    state.catalogRefreshUrl = ws;
    const shouldAnnounce = Boolean(options.manual || !state.catalogResult || state.catalogResult.offline || modelOptions().some((item) => item.engine === "desktop" && item.stale));
    if (shouldAnnounce) setCatalogStatus("Connecting to desktop app…", "syncing");
    const request = (async () => {
      const result = await state.catalogClient.refresh(ws);
      if (result?.ignored) return result;
      state.catalogRetryAfter = result?.offline ? Date.now() + 5000 : 0;
      renderCatalogResult(result);
      return result;
    })();
    state.catalogRefreshPromise = request;
    try { return await request; }
    finally {
      if (state.catalogRefreshPromise === request) {
        state.catalogRefreshPromise = null;
        state.catalogRefreshUrl = "";
      }
    }
  }
  function startCatalogPolling() {
    if (state.catalogTimer || typeof global.setInterval !== "function") return state.catalogTimer;
    // Keep visible choices current while the desktop app adds/removes a
    // service. The client generation guard prevents an older reply winning.
    state.catalogTimer = global.setInterval(() => refreshDesktopServices(), 2500);
    return state.catalogTimer;
  }
  function startStatusPolling() {
    if (state.statusTimer || typeof global.setInterval !== "function") return state.statusTimer;
    state.statusTimer = global.setInterval(() => refreshCaptureStatus(), 1000);
    return state.statusTimer;
  }
  function getActiveTabId() {
    if (!global.chrome?.tabs?.query) return Promise.resolve(null);
    return new Promise((resolve) => {
      try { global.chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => resolve(tabs?.[0]?.id ?? null)); }
      catch { resolve(null); }
    });
  }
  async function refreshCaptureStatus() {
    const tabId = await getActiveTabId();
    const response = await sendRuntime({ type: "popup-status", tabId });
    if (!response?.ok) return;
    const capturing = Boolean(response.capturing);
    const phase = text(response.phase || response.status || (capturing ? "Capturing" : "Idle"));
    const normalizedPhase = phase.toLowerCase();
    const starting = ["starting", "loading", "connecting", "initializing", "stopping"].includes(normalizedPhase);
    setButtons(capturing, starting);
    const statusState = starting ? "starting" : capturing ? "active" : /error|offline|disconnect/i.test(phase) ? "error" : "idle";
    if (capturing || starting) state.failed = false;
    if (!state.failed && (Date.now() >= state.savedHintUntil || statusState === "error")) setStatus(phase, statusState);
    const snapshot = response.snapshot || response.captureSnapshot || null;
    const localOccupancy = Array.isArray(response.occupancy) ? response.occupancy.length : 0;
    const catalogOccupancy = Number(state.catalog?.active?.captures || 0);
    const occupancyCount = Math.max(localOccupancy, Number.isFinite(catalogOccupancy) ? catalogOccupancy : 0);
    state.statusSnapshot = snapshot
      ? { ...snapshot, running: capturing, phase, captures: Number(snapshot.captures ?? occupancyCount) }
      : capturing
        ? { running: true, phase, captures: occupancyCount }
        : null;
    renderActiveSnapshot(state.statusSnapshot, phase);
  }
  function legacyTranslationConfig(model) {
    const config = { engine: model?.engine || "noop" };
    if (model?.engine === "nllb") config.nllb = { model: model.model };
    if (model?.engine === "ollama") config.ollama = { model: model.model, host: model.host || "http://localhost:11434" };
    if (model?.engine === "openai") {
      const connection = global.STTConnectionSettings?.connectionFromModel?.(model) || { apiUrl: model.baseUrl || model.apiUrl || "", apiMode: model.apiMode || "chat_completions", autoCompleteApiUrl: model.autoCompleteApiUrl !== false };
      config.openai = { api_key: "", model: model.model || "gpt-4o-mini", base_url: connection.apiUrl, api_mode: connection.apiMode, auto_complete_api_url: connection.autoCompleteApiUrl };
    }
    return config;
  }
  function buildNextSettings(model, apiKey = "") {
    let translation;
    if (model?.legacy) {
      translation = legacyTranslationConfig(model);
      if (translation.openai) translation.openai.api_key = apiKey;
    } else if (model?.id === "none") {
      translation = { selection: { kind: "none", id: "none" } };
    } else {
      translation = {};
      if (model?.selection) translation.selection = { ...model.selection };
    }
    if (state.targetLanguageMode === "override" && state.targetLanguage) translation.target_language = state.targetLanguage;
    if (state.partialMode === "override") translation.partial = Boolean(state.partial);
    let stt;
    if (state.sttMode === "override") {
      const item = sttOptions().find((entry) => entry.id === state.selectedStt);
      const selection = item?.selection || { model: state.selectedStt };
      stt = { model: selection.model || state.selectedStt, ...(selection.backend ? { backend: selection.backend } : {}) };
    }
    const snapshot = {
      ...(model?.selection ? { selection: model.selection } : model?.id === "none" ? { selection: { kind: "none", id: "none" } } : {}),
      ...(state.targetLanguageMode === "override" && state.targetLanguage ? { target_language: state.targetLanguage } : {}),
      ...(stt ? { stt } : {}),
    };
    if (globalThis.STTCaptureSettings?.buildCaptureSettings) {
      return globalThis.STTCaptureSettings.buildCaptureSettings(model, translation, stt, { snapshot });
    }
    return { version: 2, ...(Object.keys(translation).length ? { translation } : {}), ...(stt ? { stt } : {}), snapshot };
  }
  async function startCapture() {
    state.failed = false;
    const tabId = await getActiveTabId();
    if (tabId === null || tabId === undefined) { fail("No active tab"); return; }
    const model = modelOptions().find((item) => item.id === state.selectedTranslation) || modelOptions()[0];
    if (!model) { fail("Choose a translation service first"); return; }
    if (model.available === false) { fail("This service is unavailable. Check the desktop app, then press ↻."); return; }
    if (state.targetLanguageMode === "override" && !state.targetLanguage) { fail("Type a target language, or pick Same as desktop."); return; }
    if (state.sttMode === "override") {
      if (!state.selectedStt) { fail("Choose a speech recognition model, or pick Same as desktop."); return; }
      const selectedSpeech = sttOptions().find((entry) => entry.id === state.selectedStt);
      if (selectedSpeech?.available === false && (state.catalogResult?.catalog?.kind === "v2" || global.STTDesktopCatalog)) { fail("This speech recognition model is unavailable. Check the desktop app, then press ↻."); return; }
    }
    let apiKey = "";
    if (model.legacy && model.engine === "openai" && global.STTConnectionSettings?.getApiKey) {
      try { apiKey = await global.STTConnectionSettings.getApiKey(model.id); }
      catch (error) { fail(`Could not read saved service key: ${error?.message || error}`); return; }
    }
    const wsUrl = (state.wsUrl || DEFAULT_WS_URL).trim() || DEFAULT_WS_URL;
    const settings = buildNextSettings(model, apiKey);
    setStatus("Starting…", "starting"); setButtons(false, true);
    const response = await sendRuntime({ type: "popup-start", tabId, wsUrl, settings });
    if (!response?.ok) { fail(`Could not start capture: ${response?.error || "extension unavailable"}`); setButtons(false); return; }
    setStatus("Connecting…", "starting");
    await refreshCaptureStatus();
  }
  async function stopCapture() {
    state.failed = false;
    const tabId = await getActiveTabId();
    setStatus("Stopping…", "starting"); setButtons(true, true);
    const response = await sendRuntime({ type: "popup-stop", tabId });
    if (!response?.ok) { fail(`Could not stop capture: ${response?.error || "extension unavailable"}`); return; }
    setStatus("Stopped", "idle"); setButtons(false); await refreshCaptureStatus();
  }
  function persistTranslationSelection() {
    const translation = $("translationModelSelect");
    if (translation) {
      state.selectedTranslation = translation.value;
      persistSettings({ [KEYS.selectedTranslation]: state.selectedTranslation });
    }
  }
  function persistSttSelection() {
    const stt = $("sttModelSizeSelect");
    if (!stt) return;
    if (stt.value === DESKTOP_DEFAULT) {
      state.sttMode = "inherit";
      persistSettings({ [KEYS.sttMode]: "inherit" });
    } else {
      state.sttMode = "override";
      state.selectedStt = stt.value;
      persistSettings({ [KEYS.sttMode]: "override", [KEYS.selectedStt]: state.selectedStt });
    }
    renderChoices();
  }
  function persistLanguageSelection() {
    const value = $("translationLanguageSelect")?.value || DESKTOP_DEFAULT;
    state.languageOther = value === OTHER_LANGUAGE;
    if (value === DESKTOP_DEFAULT) {
      state.targetLanguageMode = "inherit";
    } else {
      state.targetLanguageMode = "override";
      state.targetLanguage = state.languageOther ? "" : normalizeLanguage(value);
    }
    persistSettings({ [KEYS.languageMode]: state.targetLanguageMode, [KEYS.language]: state.targetLanguage });
    renderChoices();
    if (state.languageOther) $("translationLanguageCustom")?.focus?.();
  }
  function persistSelection() {
    persistTranslationSelection();
    persistSttSelection();
  }
  // Update notice: the service worker records the latest GitHub release (shared/update-check.js); this only shows it.
  async function renderUpdate() {
    const Updates = global.STTUpdateCheck;
    const version = global.chrome?.runtime?.getManifest?.()?.version || "";
    const label = $("appVersion");
    if (label && version) label.textContent = ` · v${version}`;
    const banner = $("updateBanner");
    if (!Updates || !banner) return;
    const data = await storageGet([Updates.STATE_KEY, Updates.ENABLED_KEY, Updates.DISMISSED_KEY]);
    const info = Updates.describe(data[Updates.STATE_KEY], data[Updates.DISMISSED_KEY], version);
    state.update = info;
    banner.hidden = !(info.available && !info.dismissed && data[Updates.ENABLED_KEY] !== false);
    if (banner.hidden) return;
    $("updateBannerText").textContent = `Version ${info.version} is available (this is ${version}).`;
    const link = $("updateBannerLink");
    if (link) link.href = info.url;
  }
  function dismissUpdate() {
    const Updates = global.STTUpdateCheck;
    if (Updates && state.update?.version) storageSet({ [Updates.DISMISSED_KEY]: state.update.version }).catch(() => {});
  }
  function wireEvents() {
    $("updateBannerDismiss")?.addEventListener("click", dismissUpdate);
    $("translationModelSelect")?.addEventListener("change", persistTranslationSelection);
    $("sttModelSizeSelect")?.addEventListener("change", persistSttSelection);
    $("translationLanguageSelect")?.addEventListener("change", persistLanguageSelection);
    $("translationLanguageCustom")?.addEventListener("input", (event) => {
      state.targetLanguage = normalizeLanguage(event.target.value);
      persistSettings({ [KEYS.language]: state.targetLanguage });
    });
    $("translationBehaviorSelect")?.addEventListener("change", (event) => { const value = event.target.value; state.partialMode = value === "inherit" ? "inherit" : "override"; state.partial = value === "partial"; persistSettings({ [KEYS.partialMode]: state.partialMode, [KEYS.partial]: state.partial }); renderChoices(); });
    $("refreshDesktopServices")?.addEventListener("click", () => refreshDesktopServices({ manual: true }));
    $("startBtn")?.addEventListener("click", startCapture);
    $("stopBtn")?.addEventListener("click", stopCapture);
    $("openSettingsBtn")?.addEventListener("click", () => global.chrome?.runtime?.openOptionsPage?.());
    $("themeSelect")?.addEventListener("change", (event) => { applyThemePreference(event.target.value); persistSettings({ [KEYS.theme]: state.theme }, true); });
    const bindFontScale = (id, outputId, key) => {
      const input = $(id);
      input?.addEventListener("input", () => {
        const scale = renderFontScale(id, outputId, input.value);
        persistSettings({ [key]: scale }, true);
      });
    };
    bindFontScale("subtitleOriginalSize", "subtitleOriginalSizeValue", KEYS.originalScale);
    bindFontScale("subtitleTranslatedSize", "subtitleTranslatedSizeValue", KEYS.translatedScale);
    if (global.chrome?.storage?.onChanged?.addListener) {
      global.chrome.storage.onChanged.addListener((changes, areaName) => {
        if (areaName && areaName !== "local") return;
        let rerender = false;
        let updateChanged = false;
        for (const [key, change] of Object.entries(changes || {})) {
          const value = change?.newValue;
          if (global.STTUpdateCheck && [global.STTUpdateCheck.STATE_KEY, global.STTUpdateCheck.ENABLED_KEY, global.STTUpdateCheck.DISMISSED_KEY].includes(key)) updateChanged = true;
          if (key === KEYS.wsUrl) {
            const nextUrl = text(value || DEFAULT_WS_URL);
            const changed = nextUrl !== state.wsUrl;
            state.wsUrl = nextUrl;
            if (changed) {
              // A catalog is scoped to its URL and server identity. Clear the
              // old endpoint immediately so its choices cannot be selected
              // during the refresh window.
              resetCatalogForEndpoint();
              refreshDesktopServices({ manual: true });
            }
          }
          if (key === KEYS.selectedTranslation) { state.selectedTranslation = text(value); rerender = true; }
          if (key === KEYS.selectedStt) { state.selectedStt = text(value); rerender = true; }
          if (key === KEYS.sttMode) { state.sttMode = value === "override" ? "override" : "inherit"; rerender = true; }
          if (key === KEYS.language) { state.targetLanguage = normalizeLanguage(value); rerender = true; }
          if (key === KEYS.languageMode) { state.targetLanguageMode = value === "override" ? "override" : "inherit"; rerender = true; }
          if (key === KEYS.partial || key === KEYS.partialMode) { state.partial = Boolean(key === KEYS.partial ? value : state.partial); if (key === KEYS.partialMode) state.partialMode = value === "override" ? "override" : "inherit"; rerender = true; }
          if (key === KEYS.theme) applyThemePreference(value);
          if (key === KEYS.originalScale) renderFontScale("subtitleOriginalSize", "subtitleOriginalSizeValue", value);
          if (key === KEYS.translatedScale) renderFontScale("subtitleTranslatedSize", "subtitleTranslatedSizeValue", value);
        }
        if (rerender) renderChoices();
        if (updateChanged) renderUpdate();
      });
    }
    if (!state.focusRefreshBound && typeof global.addEventListener === "function") {
      global.addEventListener("focus", () => refreshDesktopServices({ manual: true }));
      state.focusRefreshBound = true;
    }
  }
  async function loadState() {
    const data = await storageGet(Object.values(KEYS));
    state.wsUrl = text(data[KEYS.wsUrl] || DEFAULT_WS_URL);
    state.selectedTranslation = text(data[KEYS.selectedTranslation] || "desktop-default");
    state.legacyModels = Array.isArray(data[KEYS.legacyModels]) ? data[KEYS.legacyModels].map((model) => ({ ...model, legacy: true })) : [];
    state.selectedStt = text(data[KEYS.selectedStt] || data[KEYS.sttLegacy] || DEFAULT_STT_MODEL);
    state.sttMode = data[KEYS.sttMode] === "override" || (hasOwn(data, KEYS.sttLegacy) && !hasOwn(data, KEYS.sttMode)) ? "override" : "inherit";
    state.targetLanguage = normalizeLanguage(data[KEYS.language]);
    state.targetLanguageMode = data[KEYS.languageMode] === "override" || (hasOwn(data, KEYS.language) && !hasOwn(data, KEYS.languageMode)) ? "override" : "inherit";
    state.partial = Boolean(data[KEYS.partial]);
    state.partialMode = data[KEYS.partialMode] === "override" || (hasOwn(data, KEYS.partial) && !hasOwn(data, KEYS.partialMode)) ? "override" : "inherit";
    applyThemePreference(data[KEYS.theme] || "system");
    renderChoices();
    const appearance = {
      original: data[KEYS.originalScale] ?? data[KEYS.legacyScale] ?? 1,
      translated: data[KEYS.translatedScale] ?? data[KEYS.legacyScale] ?? 1,
      opacity: data[KEYS.opacity] ?? 0.85,
      history: data[KEYS.history] ?? 2,
      showPartial: data[KEYS.showPartial] !== false,
    };
    renderFontScale("subtitleOriginalSize", "subtitleOriginalSizeValue", appearance.original);
    renderFontScale("subtitleTranslatedSize", "subtitleTranslatedSizeValue", appearance.translated);
    for (const [id, value] of [["overlayOpacity", appearance.opacity], ["subtitleHistoryLines", appearance.history]]) { const element = $(id); if (element) element.value = String(value); }
    const show = $("subtitleShowPartial"); if (show) show.checked = appearance.showPartial;
    state.initialized = true;
  }
  function init() {
    const banner = $("status");
    if (banner && global.ResizeObserver) new global.ResizeObserver(() => updateStatusOverflow(banner)).observe(banner);
    banner?.addEventListener("scroll", () => updateStatusOverflow(banner), { passive: true });
    wireEvents();
    renderUpdate();
    // Asks the service worker, which answers from storage unless a daily check is due; never awaited by the UI.
    if (global.STTUpdateCheck) sendRuntime({ type: "update-check" });
    loadState().then(() => {
      // Catalog and status are independent. A slow/failed catalog must never
      // delay Stop or hide the current capture snapshot.
      refreshCaptureStatus();
      refreshDesktopServices();
      startStatusPolling();
      startCatalogPolling();
    }).catch((error) => setStatus(`Settings error: ${error?.message || error}`, "error"));
  }

  init();
  global.STTPopupState = state;
  global.STTPopup = Object.freeze({
    state,
    loadState,
    wireEvents,
    applyThemePreference,
    renderChoices,
    renderUpdate,
    persistSelection,
    setStatus,
    refreshDesktopServices,
    startCatalogPolling,
    startStatusPolling,
    refreshCaptureStatus,
    buildNextSettings,
    modelOptions,
    sttOptions,
  });
})(globalThis);

(function (global) {
  "use strict";

  const CATALOG_VERSION = 2;
  const CACHE_STORAGE_KEY = "desktopCatalogCache";
  const CACHE_SCHEMA_VERSION = 1;
  const DEFAULT_TIMEOUT_MS = 2600;

  // A catalog is deliberately a small capability document.  Keep this allowlist
  // here so credentials, local paths, and provider-specific secrets never enter
  // extension cache or UI state even if a future backend adds extra fields.
  function sanitizeModel(model) {
    if (!model || typeof model !== "object") return null;
    const selection = model.selection && typeof model.selection === "object"
      ? {
          kind: String(model.selection.kind || ""),
          id: String(model.selection.id || ""),
        }
      : undefined;
    const result = {
      id: String(model.id || ""),
      name: String(model.name || model.id || ""),
      engine: String(model.engine || "desktop"),
      available: model.available !== false,
      status: String(model.status || (model.available === false ? "unavailable" : "ready")),
    };
    if (["local", "profile", "none"].includes(selection?.kind) && selection.id) result.selection = selection;
    if (model.reason) result.reason = String(model.reason);
    return result.id ? result : null;
  }

  function sanitizeSttModel(model) {
    if (!model || typeof model !== "object") return null;
    const selection = model.selection && typeof model.selection === "object"
      ? {
          model: String(model.selection.model || model.model || ""),
          ...(model.selection.backend ? { backend: String(model.selection.backend) } : {}),
        }
      : model.model
        ? { model: String(model.model), ...(model.backend ? { backend: String(model.backend) } : {}) }
        : undefined;
    const result = {
      id: String(model.id || selection?.model || ""),
      name: String(model.name || model.id || selection?.model || ""),
      available: model.available !== false,
      status: String(model.status || (model.available === false ? "unavailable" : "ready")),
    };
    if (selection?.model) result.selection = selection;
    if (model.reason) result.reason = String(model.reason);
    return result.id ? result : null;
  }

  function sanitizeDefaults(defaults) {
    const source = defaults && typeof defaults === "object" ? defaults : {};
    const stt = source.stt && typeof source.stt === "object" ? source.stt : {};
    const translation = source.translation && typeof source.translation === "object"
      ? source.translation
      : {};
    const output = {
      stt: {},
      translation: {},
    };
    if (stt.model) output.stt.model = String(stt.model);
    if (stt.backend) output.stt.backend = String(stt.backend);
    if (translation.target_language) {
      output.translation.target_language = String(translation.target_language);
    }
    if (typeof translation.partial === "boolean") {
      output.translation.partial = translation.partial;
    }
    if (translation.selection && typeof translation.selection === "object") {
      const kind = String(translation.selection.kind || "");
      if (["local", "profile", "none"].includes(kind)) {
        output.translation.selection = {
          kind,
          ...(translation.selection.id ? { id: String(translation.selection.id) } : {}),
        };
      }
    }
    return output;
  }

  function sanitizeActive(active) {
    if (!active || typeof active !== "object") return { captures: 0, sessions: [] };
    const captureCount = Number.isFinite(Number(active.captures))
      ? Number(active.captures)
      : Number.isFinite(Number(active.capture_count))
        ? Number(active.capture_count)
        : undefined;
    const captures = Array.isArray(active.captures)
      ? active.captures.map((capture) => {
          if (!capture || typeof capture !== "object") return null;
          const selection = capture.selection && typeof capture.selection === "object"
            ? {
                kind: String(capture.selection.kind || ""),
                ...(capture.selection.id ? { id: String(capture.selection.id) } : {}),
              }
            : undefined;
          return {
            tab_id: capture.tab_id ?? capture.tabId ?? undefined,
            name: String(capture.name || ""),
            ...(selection ? { selection } : {}),
            ...(capture.target_language ? { target_language: String(capture.target_language) } : {}),
          };
        }).filter(Boolean)
      : [];
    const activeSelection = active.selection && typeof active.selection === "object"
      ? {
          kind: String(active.selection.kind || ""),
          ...(active.selection.id ? { id: String(active.selection.id) } : {}),
        }
      : undefined;
    return {
      ...(active.name ? { name: String(active.name) } : {}),
      ...(["local", "profile", "none"].includes(activeSelection?.kind) ? { selection: activeSelection } : {}),
      ...(captureCount !== undefined ? { captures: captureCount } : { captures: captures.length }),
      sessions: captures,
    };
  }

  function normalizeCatalog(payload) {
    if (!payload || typeof payload !== "object") return null;
    if (payload.type === "desktop_catalog" && Number(payload.version) === CATALOG_VERSION) {
      if (!validIdentity(payload.server_id) || !validIdentity(payload.instance_id)) return null;
      return {
        kind: "v2",
        upgradeNeeded: false,
        type: "desktop_catalog",
        version: CATALOG_VERSION,
        server_id: String(payload.server_id || ""),
        instance_id: String(payload.instance_id || ""),
        revision: Number.isFinite(Number(payload.revision)) ? Number(payload.revision) : 0,
        models: (Array.isArray(payload.models) ? payload.models : []).map(sanitizeModel).filter(Boolean),
        stt_models: (Array.isArray(payload.stt_models) ? payload.stt_models : [])
          .map(sanitizeSttModel).filter(Boolean),
        defaults: sanitizeDefaults(payload.defaults),
        active: sanitizeActive(payload.active),
        capabilities: Array.isArray(payload.capabilities)
          ? payload.capabilities.map((item) => String(item)).filter(Boolean)
          : [],
      };
    }
    if (payload.type === "translation_catalog" && Number(payload.version) === 1) {
      // Keep legacy entries only as a migration/upgrade hint.  The caller must
      // never treat this as a synchronized catalog.
      return {
        kind: "legacy",
        upgradeNeeded: true,
        type: "translation_catalog",
        version: 1,
        models: (Array.isArray(payload.models) ? payload.models : []).map(sanitizeModel).filter(Boolean),
        active: sanitizeActive(payload),
      };
    }
    return null;
  }

  function canonicalUrl(rawUrl) {
    const raw = String(rawUrl || "").trim();
    if (!raw) return "";
    try {
      const parsed = new URL(raw);
      parsed.hash = "";
      return parsed.toString().replace(/\/$/, "");
    } catch {
      return raw.replace(/\/$/, "");
    }
  }

  function validIdentity(value) {
    const normalized = String(value || "").trim();
    return Boolean(normalized) && !["unknown", "unknown-server", "unknown-instance", "undefined", "null"].includes(normalized.toLowerCase());
  }

  function cacheKey(wsUrl, serverId, instanceId) {
    const server = String(serverId || "").trim();
    const instance = String(instanceId || "").trim();
    return server && instance ? [canonicalUrl(wsUrl), server, instance].join("|") : "";
  }

  function isUsableCatalog(catalog) {
    return Boolean(catalog && catalog.kind === "v2" && catalog.version === CATALOG_VERSION);
  }

  function builtInSelections() {
    return [
      {
        id: "desktop-default",
        name: "Desktop default",
        engine: "desktop",
        system: true,
        available: true,
        status: "default",
      },
      {
        id: "none",
        name: "None · subtitles only",
        engine: "noop",
        system: true,
        available: true,
        status: "ready",
        selection: { kind: "none", id: "none" },
      },
    ];
  }

  function toSelectionOptions(catalog) {
    const entries = builtInSelections();
    if (!catalog) return entries;
    const models = Array.isArray(catalog.models) ? catalog.models : [];
    for (const model of models) {
      if (!model || !model.id || !model.selection) continue;
      entries.push({ ...model, id: String(model.id), name: String(model.name || model.id) });
    }
    return entries;
  }

  function toSttOptions(catalog) {
    return (Array.isArray(catalog?.stt_models) ? catalog.stt_models : [])
      .filter((item) => item?.id)
      .map((item) => ({ ...item, id: String(item.id), name: String(item.name || item.id) }));
  }

  function readStorage(storageArea, keys) {
    if (!storageArea?.get) return Promise.resolve({});
    return new Promise((resolve) => {
      try {
        storageArea.get(keys, (result) => resolve(result || {}));
      } catch {
        resolve({});
      }
    });
  }

  function writeStorage(storageArea, values) {
    if (!storageArea?.set) return Promise.resolve();
    return new Promise((resolve) => {
      try { storageArea.set(values, () => resolve()); } catch { resolve(); }
    });
  }

  async function readCache(storageArea, wsUrl, serverId, instanceId) {
    const data = await readStorage(storageArea, [CACHE_STORAGE_KEY]);
    const source = data[CACHE_STORAGE_KEY];
    if (!source || source.schema !== CACHE_SCHEMA_VERSION || !source.entries) return null;
    const key = cacheKey(wsUrl, serverId, instanceId);
    if (!key) return null;
    const entry = source.entries[key];
    return entry?.catalog ? { ...entry.catalog, cachedAt: entry.cachedAt, stale: true } : null;
  }

  async function writeCache(storageArea, wsUrl, catalog, now = Date.now()) {
    if (!isUsableCatalog(catalog)) return;
    const data = await readStorage(storageArea, [CACHE_STORAGE_KEY]);
    const source = data[CACHE_STORAGE_KEY] && data[CACHE_STORAGE_KEY].entries
      ? data[CACHE_STORAGE_KEY]
      : { schema: CACHE_SCHEMA_VERSION, entries: {}, active: {} };
    const key = cacheKey(wsUrl, catalog.server_id, catalog.instance_id);
    if (!key) return;
    const copy = { ...catalog };
    delete copy.cachedAt;
    delete copy.stale;
    delete copy._wsUrl;
    source.schema = CACHE_SCHEMA_VERSION;
    source.active = source.active && typeof source.active === "object" ? source.active : {};
    source.active[canonicalUrl(wsUrl)] = key;
    source.entries[key] = { cachedAt: now, catalog: copy };
    const keys = Object.keys(source.entries);
    if (keys.length > 12) {
      keys.sort((left, right) => Number(source.entries[left]?.cachedAt || 0) - Number(source.entries[right]?.cachedAt || 0));
      for (const oldKey of keys.slice(0, keys.length - 12)) delete source.entries[oldKey];
    }
    await writeStorage(storageArea, { [CACHE_STORAGE_KEY]: source });
  }

  function requestCatalog(wsUrl, options = {}) {
    const WebSocketClass = options.WebSocketClass || global.WebSocket;
    const timeoutMs = Number(options.timeoutMs || DEFAULT_TIMEOUT_MS);
    if (!WebSocketClass) return Promise.reject(new Error("WebSocket is unavailable"));
    const url = String(wsUrl || "").trim();
    if (!url) return Promise.reject(new Error("WebSocket URL is required"));
    return new Promise((resolve, reject) => {
      let socket;
      let finished = false;
      const timer = (options.setTimeout || global.setTimeout)(() => finish(new Error("Desktop catalog timed out")), timeoutMs);
      function cleanup() {
        (options.clearTimeout || global.clearTimeout)(timer);
        try { socket?.close(); } catch { /* best effort */ }
      }
      function finish(error, value) {
        if (finished) return;
        finished = true;
        cleanup();
        if (error) reject(error); else resolve(value);
      }
      try {
        socket = new WebSocketClass(url);
        socket.addEventListener("open", () => {
          try { socket.send(JSON.stringify({ type: "desktop_catalog", version: CATALOG_VERSION })); }
          catch (error) { finish(error); }
        });
        socket.addEventListener("message", (event) => {
          let payload;
          try { payload = JSON.parse(event.data); } catch { return; }
          const normalized = normalizeCatalog(payload);
          if (normalized) finish(null, normalized);
          else if (payload?.type === "error") finish(new Error(String(payload.message || "Desktop catalog request failed")));
        });
        socket.addEventListener("error", () => finish(new Error("Desktop server is unavailable")));
        socket.addEventListener("close", () => {
          if (!finished) finish(new Error("Desktop server closed the catalog connection"));
        });
      } catch (error) {
        finish(error);
      }
    });
  }

  function requestLegacyCatalog(wsUrl, options = {}) {
    const WebSocketClass = options.WebSocketClass || global.WebSocket;
    const timeoutMs = Number(options.timeoutMs || DEFAULT_TIMEOUT_MS);
    if (!WebSocketClass) return Promise.reject(new Error("WebSocket is unavailable"));
    return new Promise((resolve, reject) => {
      let socket;
      let finished = false;
      const timer = (options.setTimeout || global.setTimeout)(() => finish(new Error("Legacy desktop catalog timed out")), timeoutMs);
      function cleanup() {
        (options.clearTimeout || global.clearTimeout)(timer);
        try { socket?.close(); } catch { /* best effort */ }
      }
      function finish(error, value) {
        if (finished) return;
        finished = true;
        cleanup();
        if (error) reject(error); else resolve(value);
      }
      try {
        socket = new WebSocketClass(wsUrl);
        socket.addEventListener("open", () => {
          try { socket.send(JSON.stringify({ type: "translation_catalog" })); }
          catch (error) { finish(error); }
        });
        socket.addEventListener("message", (event) => {
          let payload;
          try { payload = JSON.parse(event.data); } catch { return; }
          const normalized = normalizeCatalog(payload);
          if (normalized?.kind === "legacy") finish(null, normalized);
          else if (payload?.type === "error") finish(new Error(String(payload.message || "Legacy catalog request failed")));
        });
        socket.addEventListener("error", () => finish(new Error("Desktop server is unavailable")));
        socket.addEventListener("close", () => { if (!finished) finish(new Error("Desktop server closed the catalog connection")); });
      } catch (error) { finish(error); }
    });
  }

  class CatalogClient {
    constructor(options = {}) {
      this.storageArea = options.storageArea || global.chrome?.storage?.local;
      this.WebSocketClass = options.WebSocketClass || global.WebSocket;
      this.timeoutMs = Number(options.timeoutMs || DEFAULT_TIMEOUT_MS);
      this.generation = 0;
      this.catalog = null;
      this.lastResult = null;
    }

    async cached(wsUrl) {
      const current = this.catalog;
      if (current && current._wsUrl === canonicalUrl(wsUrl)) return { ...current, stale: true };
      const source = await readStorage(this.storageArea, [CACHE_STORAGE_KEY]);
      const entries = source[CACHE_STORAGE_KEY]?.entries;
      if (!entries) return null;
      const activeKey = source[CACHE_STORAGE_KEY]?.active?.[canonicalUrl(wsUrl)];
      const entry = activeKey ? entries[activeKey] : null;
      if (!entry?.catalog) return null;
      return { ...entry.catalog, cachedAt: entry.cachedAt, stale: true, _wsUrl: canonicalUrl(wsUrl) };
    }

    async refresh(wsUrl, options = {}) {
      const generation = ++this.generation;
      const normalizedUrl = canonicalUrl(wsUrl);
      const cached = await this.cached(normalizedUrl);
      let result;
      try {
        result = await requestCatalog(normalizedUrl, {
          WebSocketClass: this.WebSocketClass,
          timeoutMs: options.timeoutMs || this.timeoutMs,
        });
      } catch (error) {
        try {
          const legacy = await requestLegacyCatalog(normalizedUrl, {
            WebSocketClass: this.WebSocketClass,
            timeoutMs: Math.min(this.timeoutMs, 1200),
          });
          if (generation !== this.generation) return { ignored: true, legacy };
          this.lastResult = { ok: false, upgradeNeeded: true, legacy, catalog: cached };
          return this.lastResult;
        } catch {
          // Continue with the cached/offline result below.
        }
        if (generation !== this.generation) return { ignored: true, cached };
        this.catalog = cached;
        this.lastResult = { ok: false, offline: true, error, catalog: cached };
        return this.lastResult;
      }
      if (generation !== this.generation) return { ignored: true, catalog: result };
      if (result.kind === "legacy") {
        this.lastResult = { ok: false, upgradeNeeded: true, legacy: result, catalog: cached };
        return this.lastResult;
      }
      result._wsUrl = normalizedUrl;
      this.catalog = result;
      await writeCache(this.storageArea, normalizedUrl, result);
      this.lastResult = { ok: true, offline: false, catalog: result };
      return this.lastResult;
    }
  }

  global.STTDesktopCatalog = Object.freeze({
    CATALOG_VERSION,
    CACHE_STORAGE_KEY,
    CACHE_SCHEMA_VERSION,
    canonicalUrl,
    validIdentity,
    cacheKey,
    sanitizeModel,
    sanitizeSttModel,
    normalizeCatalog,
    isUsableCatalog,
    builtInSelections,
    toSelectionOptions,
    toSttOptions,
    readCache,
    writeCache,
    requestCatalog,
    requestLegacyCatalog,
    CatalogClient,
  });
  if (typeof module !== "undefined") module.exports = global.STTDesktopCatalog;
})(globalThis);

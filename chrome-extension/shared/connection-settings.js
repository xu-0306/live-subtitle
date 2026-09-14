(function (global) {
  "use strict";

  const API_MODES = Object.freeze({
    CHAT_COMPLETIONS: "chat_completions",
    RESPONSES: "responses",
    MESSAGES: "messages",
  });
  const DEFAULT_API_BASE_URL = "https://api.openai.com";
  const API_PATHS = Object.freeze({
    [API_MODES.CHAT_COMPLETIONS]: "/v1/chat/completions",
    [API_MODES.RESPONSES]: "/v1/responses",
    [API_MODES.MESSAGES]: "/v1/messages",
  });
  const KNOWN_ENDPOINT_PATTERN = /\/(?:chat\/completions|responses|messages)\/*$/i;
  const SECRET_DB_NAME = "stt-translation-secrets";
  const SECRET_STORE_NAME = "apiKeys";
  const SECRET_DB_VERSION = 1;

  function normalizeApiMode(value, rawUrl) {
    const normalized = String(value || "").trim().toLowerCase().replaceAll("-", "_");
    if (["chat", "chat_completion", "chat_completions", "openai"].includes(normalized)) {
      return API_MODES.CHAT_COMPLETIONS;
    }
    if (["response", "responses"].includes(normalized)) {
      return API_MODES.RESPONSES;
    }
    if (["message", "messages", "anthropic", "anthropic_messages"].includes(normalized)) {
      return API_MODES.MESSAGES;
    }
    return inferApiMode(rawUrl);
  }

  function inferApiMode(rawUrl) {
    let path = "";
    try {
      path = new URL(String(rawUrl || "").trim()).pathname.toLowerCase();
    } catch {
      path = String(rawUrl || "").trim().toLowerCase();
    }
    const normalizedPath = path.replace(/\/+$/, "");
    if (normalizedPath.endsWith("/responses")) return API_MODES.RESPONSES;
    if (normalizedPath.endsWith("/messages")) return API_MODES.MESSAGES;
    return API_MODES.CHAT_COMPLETIONS;
  }

  function validateHttpUrl(rawUrl) {
    if (!rawUrl) return "API URL is required when automatic completion is disabled.";
    try {
      const parsed = new URL(rawUrl);
      if (!["http:", "https:"].includes(parsed.protocol)) {
        return "API URL must use http:// or https://.";
      }
      if (!parsed.hostname) return "API URL must include a host.";
      if (parsed.hash) return "API URL must not include a fragment.";
      return "";
    } catch {
      return "API URL must be an absolute http:// or https:// URL.";
    }
  }

  function resolveApiUrl(rawUrl, apiMode, autoComplete = true) {
    const raw = String(rawUrl ?? "").trim();
    const mode = normalizeApiMode(apiMode, raw);
    const completionEnabled = autoComplete !== false;
    const sourceUrl = raw || (completionEnabled ? DEFAULT_API_BASE_URL : "");
    const error = validateHttpUrl(sourceUrl);
    if (error) {
      return {
        rawUrl: raw,
        requestUrl: sourceUrl,
        apiMode: mode,
        autoComplete: completionEnabled,
        error,
      };
    }

    const parsed = new URL(sourceUrl);
    const pathWithoutTrailingSlash = parsed.pathname.replace(/\/+$/, "");
    if (KNOWN_ENDPOINT_PATTERN.test(pathWithoutTrailingSlash)) {
      const endpointMode = inferApiMode(sourceUrl);
      const modeMismatch = Boolean(apiMode) && endpointMode !== mode;
      return {
        rawUrl: raw,
        requestUrl: sourceUrl,
        apiMode: mode,
        autoComplete: completionEnabled,
        error: modeMismatch
          ? `The complete endpoint is ${endpointMode}, but API type is ${mode}.`
          : "",
      };
    }
    if (!completionEnabled) {
      return {
        rawUrl: raw,
        requestUrl: sourceUrl,
        apiMode: mode,
        autoComplete: false,
        error: "",
      };
    }

    const fullPath = API_PATHS[mode];
    const pathSuffix = pathWithoutTrailingSlash.toLowerCase().endsWith("/v1")
      ? fullPath.slice(3)
      : fullPath;
    parsed.pathname = `${pathWithoutTrailingSlash}${pathSuffix}`.replace(/\/{2,}/g, "/");
    return {
      rawUrl: raw,
      requestUrl: parsed.toString(),
      apiMode: mode,
      autoComplete: true,
      error: "",
    };
  }

  function connectionFromModel(model) {
    const source = model?.connection && typeof model.connection === "object"
      ? model.connection
      : model || {};
    const apiUrl = String(
      source.apiUrl ?? source.api_url ?? source.baseUrl ?? source.base_url ?? ""
    ).trim();
    const explicitMode = source.apiMode ?? source.api_mode;
    const explicitAutoComplete =
      source.autoCompleteApiUrl ?? source.auto_complete_api_url;
    return {
      apiUrl,
      apiMode: normalizeApiMode(explicitMode, apiUrl),
      autoCompleteApiUrl: explicitAutoComplete !== false,
    };
  }

  function sanitizeModel(model) {
    const sanitized = { ...(model || {}) };
    const connection = connectionFromModel(model);
    const legacySecret = String(
      model?.apiKey ?? model?.api_key ?? model?.connection?.apiKey ?? model?.connection?.api_key ?? ""
    );
    sanitized.connection = connection;
    sanitized.hasApiKey = Boolean(model?.hasApiKey || legacySecret);
    for (const key of [
      "apiKey",
      "api_key",
      "apiUrl",
      "api_url",
      "baseUrl",
      "base_url",
      "apiMode",
      "api_mode",
      "autoCompleteApiUrl",
      "auto_complete_api_url",
    ]) {
      delete sanitized[key];
    }
    if (sanitized.connection) {
      delete sanitized.connection.apiKey;
      delete sanitized.connection.api_key;
    }
    return sanitized;
  }

  function readLegacySecret(model) {
    return String(
      model?.apiKey ?? model?.api_key ?? model?.connection?.apiKey ?? model?.connection?.api_key ?? ""
    );
  }

  function openSecretDb() {
    if (!global.indexedDB) {
      return Promise.reject(new Error("Secure extension storage is unavailable."));
    }
    return new Promise((resolve, reject) => {
      const request = global.indexedDB.open(SECRET_DB_NAME, SECRET_DB_VERSION);
      request.addEventListener("upgradeneeded", () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(SECRET_STORE_NAME)) {
          db.createObjectStore(SECRET_STORE_NAME);
        }
      });
      request.addEventListener("success", () => resolve(request.result));
      request.addEventListener("error", () => {
        reject(request.error || new Error("Unable to open secure extension storage."));
      });
    });
  }

  async function runSecretRequest(mode, operation) {
    const db = await openSecretDb();
    try {
      return await new Promise((resolve, reject) => {
        const transaction = db.transaction(SECRET_STORE_NAME, mode);
        const store = transaction.objectStore(SECRET_STORE_NAME);
        const request = operation(store);
        let result;
        request.addEventListener("success", () => {
          result = request.result;
        });
        transaction.addEventListener("complete", () => resolve(result));
        transaction.addEventListener("abort", () => {
          reject(transaction.error || new Error("Secure extension storage transaction aborted."));
        });
        transaction.addEventListener("error", () => {
          reject(transaction.error || new Error("Secure extension storage request failed."));
        });
      });
    } finally {
      db.close();
    }
  }

  function getApiKey(modelId) {
    if (!modelId) return Promise.resolve("");
    return runSecretRequest("readonly", (store) => store.get(String(modelId)))
      .then((value) => String(value || ""));
  }

  function setApiKey(modelId, apiKey) {
    if (!modelId) return Promise.reject(new Error("A model id is required to store an API key."));
    const secret = String(apiKey || "");
    if (!secret) return deleteApiKey(modelId);
    return runSecretRequest("readwrite", (store) => store.put(secret, String(modelId)));
  }

  function deleteApiKey(modelId) {
    if (!modelId) return Promise.resolve();
    return runSecretRequest("readwrite", (store) => store.delete(String(modelId)));
  }

  async function migrateModelSecrets(models) {
    const sanitizedModels = [];
    let changed = false;
    for (const model of Array.isArray(models) ? models : []) {
      const legacySecret = readLegacySecret(model);
      if (legacySecret && model?.id) {
        await setApiKey(model.id, legacySecret);
      }
      const sanitized = model?.engine === "openai" ? sanitizeModel(model) : { ...model };
      sanitizedModels.push(sanitized);
      if (JSON.stringify(sanitized) !== JSON.stringify(model)) changed = true;
    }
    return { models: sanitizedModels, changed };
  }

  global.STTConnectionSettings = Object.freeze({
    API_MODES,
    DEFAULT_API_BASE_URL,
    normalizeApiMode,
    inferApiMode,
    resolveApiUrl,
    connectionFromModel,
    sanitizeModel,
    getApiKey,
    setApiKey,
    deleteApiKey,
    migrateModelSecrets,
  });
})(globalThis);

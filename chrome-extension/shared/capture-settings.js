(function (global) {
  "use strict";

  const CONFIG_VERSION = 2;

  function normalizeMode(value, fallback = "inherit") {
    return value === "override" || value === "inherit" ? value : fallback;
  }

  function selectionForModel(model) {
    if (!model || model.id === "desktop-default" || model.engine === "backend") return undefined;
    if (model.selection && typeof model.selection === "object") {
      const kind = String(model.selection.kind || "");
      if (!["local", "profile", "none"].includes(kind)) return undefined;
      return {
        kind,
        ...(model.selection.id ? { id: String(model.selection.id) } : {}),
      };
    }
    if (model.id === "none" || model.engine === "noop") return { kind: "none", id: "none" };
    return undefined;
  }

  function buildTranslationConfig(model, values = {}) {
    const targetMode = normalizeMode(values.targetLanguageMode, "override");
    const partialMode = normalizeMode(values.partialMode, "override");
    const translation = { selection: selectionForModel(model) };
    if (!translation.selection) delete translation.selection;
    if (targetMode === "override" && String(values.targetLanguage || "").trim()) {
      translation.target_language = String(values.targetLanguage).trim();
    }
    if (partialMode === "override") translation.partial = Boolean(values.partial);
    return translation;
  }

  function buildSttConfig(values = {}) {
    const mode = normalizeMode(values.sttMode, "override");
    if (mode === "inherit") return undefined;
    const model = String(values.sttModel || "").trim();
    if (!model) return undefined;
    return {
      model,
      ...(values.sttBackend ? { backend: String(values.sttBackend) } : {}),
    };
  }

  // The extension owns a capture snapshot for each tab. All provider/model
  // selection still travels through the backend lease in the same config.
  function buildCaptureSettings(model, translation, stt, options = {}) {
    const settings = {
      translation: translation && Object.keys(translation).length ? translation : undefined,
      stt: stt && Object.keys(stt).length ? stt : undefined,
    };
    // New UI captures pass a snapshot, which opts into the v2 wire contract.
    // Keep the small legacy helper shape compatible for callers that only
    // serialize a backend default in isolation.
    if (options.versioned !== false && (options.snapshot || model?.selection || model?.engine === "desktop")) {
      settings.version = CONFIG_VERSION;
    }
    if (options.snapshot) settings.snapshot = sanitizeSnapshot(options.snapshot);
    if (!settings.translation) delete settings.translation;
    if (!settings.stt) delete settings.stt;
    return settings;
  }

  function sanitizeSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object") return {};
    const selection = snapshot.selection && typeof snapshot.selection === "object"
      ? {
          kind: String(snapshot.selection.kind || ""),
          ...(snapshot.selection.id ? { id: String(snapshot.selection.id) } : {}),
        }
      : undefined;
    const stt = snapshot.stt && typeof snapshot.stt === "object"
      ? {
          ...(snapshot.stt.model ? { model: String(snapshot.stt.model) } : {}),
          ...(snapshot.stt.backend ? { backend: String(snapshot.stt.backend) } : {}),
        }
      : undefined;
    const result = {
      ...(["local", "profile", "none"].includes(selection?.kind) ? { selection } : {}),
      ...(snapshot.target_language ? { target_language: String(snapshot.target_language) } : {}),
      ...(typeof snapshot.partial === "boolean" ? { partial: snapshot.partial } : {}),
      ...(stt ? { stt } : {}),
    };
    return result;
  }

  global.STTCaptureSettings = Object.freeze({
    CONFIG_VERSION,
    normalizeMode,
    selectionForModel,
    buildTranslationConfig,
    buildSttConfig,
    buildCaptureSettings,
    sanitizeSnapshot,
  });
  if (typeof module !== "undefined") module.exports = global.STTCaptureSettings;
})(globalThis);

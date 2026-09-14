(function (global) {
  "use strict";

  const BACKUP_KEY = "legacyTranslationProfilesBackup";
  const STATE_KEY = "desktopProfileMigration";
  const SECRET_KEYS = new Set(["apiKey", "api_key", "token", "password", "secret"]);

  function sanitizeLegacyProfile(profile) {
    const source = profile && typeof profile === "object" ? profile : {};
    const result = {};
    for (const key of ["id", "name", "engine", "model", "host", "baseUrl", "apiUrl", "apiMode", "autoCompleteApiUrl", "served_model"]) {
      if (source[key] !== undefined) result[key] = source[key];
    }
    if (source.connection && typeof source.connection === "object") {
      result.connection = {};
      for (const key of ["apiUrl", "apiMode", "autoCompleteApiUrl"]) {
        if (source.connection[key] !== undefined) result.connection[key] = source.connection[key];
      }
    }
    for (const key of ["nllb", "ollama", "vllm"]) {
      if (!source[key] || typeof source[key] !== "object") continue;
      const nested = {};
      for (const nestedKey of ["model", "host", "baseUrl", "apiUrl", "apiMode", "autoCompleteApiUrl", "served_model"]) {
        if (source[key][nestedKey] !== undefined) nested[nestedKey] = source[key][nestedKey];
      }
      if (Object.keys(nested).length) result[key] = nested;
    }
    return result;
  }

  function legacyProfiles(models) {
    return (Array.isArray(models) ? models : [])
      .filter((model) => model && model.id && !["backend-default", "desktop-default", "none"].includes(model.id))
      .filter((model) => model.engine !== "desktop")
      .map(sanitizeLegacyProfile);
  }

  function withSecret(profile, apiKey) {
    const result = { ...sanitizeLegacyProfile(profile) };
    const secret = String(apiKey || "");
    if (secret) result.apiKey = secret;
    return result;
  }

  function storageGet(area, keys) {
    if (!area?.get) return Promise.resolve({});
    return new Promise((resolve) => area.get(keys, (data) => resolve(data || {})));
  }

  function storageSet(area, values) {
    if (!area?.set) return Promise.resolve();
    return new Promise((resolve) => area.set(values, () => resolve()));
  }

  function stableValue(value) {
    if (Array.isArray(value)) return value.map(stableValue);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stableValue(value[key])]));
  }

  function profileFingerprint(profile) {
    return JSON.stringify(stableValue(sanitizeLegacyProfile(profile)));
  }

  function mergeProfiles(previous, current) {
    const merged = new Map();
    for (const profile of Array.isArray(previous) ? previous : []) {
      const sanitized = sanitizeLegacyProfile(profile);
      if (sanitized.id) merged.set(String(sanitized.id), sanitized);
    }
    for (const profile of current) merged.set(String(profile.id), profile);
    return Array.from(merged.values());
  }

  function sanitizeImportResponse(response) {
    if (!response || typeof response !== "object") return response;
    return {
      type: response.type,
      version: response.version,
      ok: response.ok === true,
      mapping: response.mapping && typeof response.mapping === "object" ? response.mapping : {},
      revision: response.revision,
      error: response.error ? String(response.error) : undefined,
      errors: Array.isArray(response.errors)
        ? response.errors.map((item) => ({
            id: String(item?.id || "__backend__"),
            message: String(item?.message || item?.error || "Import failed"),
          }))
        : [],
    };
  }

  function importErrorList(response) {
    return Array.isArray(response?.errors)
      ? response.errors
        .map((item) => ({ id: String(item?.id || "__backend__"), message: String(item?.message || item?.error || "Import failed") }))
        .filter((item) => item.id)
      : [];
  }

  function previousImportState(data, backendKey) {
    const state = data?.[STATE_KEY];
    if (!state || state.backendKey !== backendKey) return { mapping: {}, pendingIds: new Set(), fingerprints: {} };
    return {
      mapping: state.mapping && typeof state.mapping === "object" ? { ...state.mapping } : {},
      pendingIds: new Set(Array.isArray(state.pendingIds) ? state.pendingIds.map(String) : []),
      fingerprints: state.fingerprints && typeof state.fingerprints === "object" ? { ...state.fingerprints } : {},
    };
  }

  async function importLegacyProfiles(options = {}) {
    const storageArea = options.storageArea || global.chrome?.storage?.local;
    const allModels = legacyProfiles(options.models);
    if (!allModels.length) return { ok: true, skipped: true, mapping: {} };
    const backendKey = String(options.backendKey || "unknown-backend");
    const data = await storageGet(storageArea, [BACKUP_KEY, STATE_KEY, "selectedTranslationModel"]);
    const prior = previousImportState(data, backendKey);
    const fingerprints = Object.fromEntries(allModels.map((model) => [model.id, profileFingerprint(model)]));
    const models = allModels.filter((model) => {
      const mapped = Boolean(prior.mapping[model.id]);
      const pending = prior.pendingIds.has(model.id);
      const changed = prior.fingerprints[model.id] !== fingerprints[model.id];
      return !mapped || pending || changed;
    });
    const pendingCurrent = allModels.map((model) => model.id).filter((id) => !prior.mapping[id] || prior.pendingIds.has(id) || models.some((item) => item.id === id && prior.fingerprints[id] !== fingerprints[id]));
    const backup = {
      savedAt: Date.now(),
      profiles: mergeProfiles(data[BACKUP_KEY]?.profiles, allModels),
      selectedId: data.selectedTranslationModel || "",
    };

    // Keep a sanitized recovery copy before reading any secure-store value.
    await storageSet(storageArea, {
      [BACKUP_KEY]: backup,
      [STATE_KEY]: {
        status: models.length ? "pending" : (pendingCurrent.length ? "partial" : "complete"),
        backendKey,
        ids: allModels.map((model) => model.id),
        mapping: prior.mapping,
        pendingIds: pendingCurrent,
        fingerprints,
        savedAt: backup.savedAt,
      },
    });

    if (!models.length) {
      const currentSelected = String(data.selectedTranslationModel || "");
      return {
        ok: true,
        skipped: true,
        mapping: prior.mapping,
        pendingIds: [],
        selectedId: prior.mapping[currentSelected] || currentSelected,
        backup,
      };
    }

    const profiles = [];
    const secretFailures = [];
    for (const model of models) {
      let secret = "";
      if (typeof options.getApiKey === "function") {
        try {
          secret = await options.getApiKey(model.id);
        } catch {
          // A failed secure-store read is different from an empty key. Never
          // send an empty credential that could overwrite a saved backend key.
          secretFailures.push({ id: model.id, message: "Unable to read the saved credential." });
          continue;
        }
      }
      profiles.push(withSecret(model, secret));
    }

    const pendingBeforeSend = pendingCurrent;
    if (secretFailures.length && !profiles.length) {
      await storageSet(storageArea, {
        [STATE_KEY]: {
          status: "failed",
          backendKey,
          ids: allModels.map((model) => model.id),
          mapping: prior.mapping,
          pendingIds: pendingBeforeSend,
          fingerprints,
          errors: secretFailures,
          savedAt: backup.savedAt,
        },
      });
      return { ok: false, pending: true, errors: secretFailures, mapping: prior.mapping, backup };
    }

    if (typeof options.send !== "function") {
      return { ok: false, pending: true, error: "Backend import is unavailable", mapping: prior.mapping, backup };
    }
    let response;
    try {
      response = await options.send({
        type: "desktop_import_profiles",
        version: 2,
        profiles,
        ...(options.expectedRevision !== undefined ? { expected_revision: options.expectedRevision } : {}),
      });
    } catch (error) {
      return { ok: false, pending: true, error: String(error?.message || error), mapping: prior.mapping, backup };
    }
    response = sanitizeImportResponse(response);
    const responseMapping = response?.mapping && typeof response.mapping === "object" ? response.mapping : {};
    const mapping = { ...prior.mapping, ...responseMapping };
    const backendErrors = importErrorList(response);
    const errors = [...secretFailures, ...backendErrors];
    const acknowledgedCurrent = new Set(Object.keys(responseMapping));
    const changedCurrent = new Set(models.filter((model) => prior.fingerprints[model.id] && prior.fingerprints[model.id] !== fingerprints[model.id]).map((model) => model.id));
    const pendingIds = allModels
      .map((model) => model.id)
      .filter((id) => !mapping[id] || errors.some((error) => error.id === id) || (models.some((item) => item.id === id) && !acknowledgedCurrent.has(id) && (changedCurrent.has(id) || !prior.mapping[id])));

    if (!response?.ok && !Object.keys(responseMapping).length) {
      const failedState = {
        status: "failed",
        backendKey,
        ids: allModels.map((model) => model.id),
        mapping,
        pendingIds: pendingIds.length ? pendingIds : pendingBeforeSend,
        fingerprints,
        errors: errors.length ? errors : [{ id: "__backend__", message: String(response?.error || "Backend rejected profile import") }],
        revision: response?.revision ?? data[STATE_KEY]?.revision,
        savedAt: backup.savedAt,
      };
      await storageSet(storageArea, { [STATE_KEY]: failedState });
      return { ok: false, pending: true, errors: failedState.errors, mapping, response, backup };
    }

    const currentSelected = String(data.selectedTranslationModel || "");
    const nextSelected = mapping[currentSelected] || currentSelected;
    const nextState = {
      status: pendingIds.length ? "partial" : "complete",
      backendKey,
      ids: allModels.map((model) => model.id),
      mapping,
      pendingIds,
      fingerprints,
      ...(errors.length ? { errors } : {}),
      revision: response?.revision,
      savedAt: backup.savedAt,
    };
    const values = { [STATE_KEY]: nextState };
    if (nextSelected && nextSelected !== currentSelected) values.selectedTranslationModel = nextSelected;
    await storageSet(storageArea, values);
    return {
      ok: true,
      partial: Boolean(pendingIds.length),
      pendingIds,
      errors,
      mapping,
      selectedId: nextSelected,
      revision: response?.revision,
      backup,
    };
  }

  global.STTProfileMigration = Object.freeze({
    BACKUP_KEY,
    STATE_KEY,
    SECRET_KEYS,
    sanitizeLegacyProfile,
    legacyProfiles,
    withSecret,
    importLegacyProfiles,
  });
  if (typeof module !== "undefined") module.exports = global.STTProfileMigration;
})(globalThis);

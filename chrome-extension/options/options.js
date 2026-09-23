(function (global) {
  "use strict";
  const document = global.document;
  if (!document?.body || document.body.dataset.page !== "options") return;
  const $ = (id) => document.getElementById(id);
  const state = { wsUrl: "ws://127.0.0.1:8765/asr", snapshot: null, busy: false, defaultsDirty: false };
  const apiLabels = { chat_completions: "Chat completions", responses: "Responses", messages: "Messages" };
  const appearanceKeys = {
    subtitleOriginalSize: "subtitleOriginalScale",
    subtitleTranslatedSize: "subtitleTranslatedScale",
    overlayOpacity: "subtitleOverlayOpacity",
    subtitleHistoryLines: "subtitleHistoryLines",
    subtitleShowPartial: "subtitleShowPartial",
  };

  function storageGet(keys) {
    return new Promise((resolve) => {
      if (!global.chrome?.storage?.local?.get) return resolve({});
      global.chrome.storage.local.get(keys, (value) => resolve(value || {}));
    });
  }
  function storageSet(values) {
    return new Promise((resolve) => {
      if (!global.chrome?.storage?.local?.set) return resolve();
      global.chrome.storage.local.set(values, resolve);
    });
  }
  function status(message, kind = "idle") {
    $("status").textContent = message;
    $("status").dataset.state = kind;
  }
  function localUrl(raw) {
    try {
      const url = new URL(String(raw || "").trim());
      return ["ws:", "wss:"].includes(url.protocol)
        && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
        && url.pathname === "/asr" ? url.href : "";
    } catch { return ""; }
  }
  function request(action, data = {}, timeoutMs = 8000) {
    const url = localUrl(state.wsUrl);
    if (!url) return Promise.reject(new Error("Enter a local ws:// or wss:// backend URL ending in /asr."));
    return new Promise((resolve, reject) => {
      let socket;
      let finished = false;
      let timer = global.setTimeout(() => finish(new Error("Backend request timed out.")), timeoutMs);
      function finish(error, result) {
        if (finished) return;
        finished = true;
        global.clearTimeout(timer);
        try { socket?.close(); } catch { /* best effort */ }
        if (error) reject(error); else resolve(result);
      }
      try {
        socket = new global.WebSocket(url);
        socket.addEventListener("open", () => socket.send(JSON.stringify({
          type: "desktop_management", version: 1, action, data,
          ...(action === "read" ? {} : { expected_revision: state.snapshot?.revision }),
        })));
        socket.addEventListener("message", (event) => {
          let payload;
          try { payload = JSON.parse(event.data); } catch { return; }
          if (payload?.type === "desktop_management_progress") {
            const percent = payload.total > 0 ? ` · ${Math.min(100, Math.round(payload.done * 100 / payload.total))}%` : "";
            status(`${payload.message || "Downloading"}${percent}`, "syncing");
          } else if (payload?.type === "desktop_management_result") {
            if (payload.ok) finish(null, payload);
            else finish(new Error(String(payload.error || "The backend rejected this change.")));
          } else if (payload?.type === "error") {
            finish(new Error(String(payload.message || "Backend error.")));
          }
        });
        socket.addEventListener("error", () => finish(new Error("Backend is unavailable. Start the service and refresh.")));
        socket.addEventListener("close", () => { if (!finished) finish(new Error("Backend closed the settings connection.")); });
      } catch (error) { finish(error); }
    });
  }
  function setBusy(value) {
    state.busy = value;
    document.querySelectorAll("button").forEach((button) => { button.disabled = value; });
  }
  async function perform(action, data, message, timeoutMs) {
    if (state.busy || !state.snapshot) return;
    setBusy(true);
    status(message, "syncing");
    try {
      const result = await request(action, data, timeoutMs);
      state.snapshot = result;
      state.defaultsDirty = false;
      render();
      const savedProfile = action === "save_profile"
        ? result.profiles?.find((item) => item.id === result.saved_profile_id) : null;
      const detected = savedProfile && ["openai", "openai_compatible"].includes(savedProfile.engine)
        ? apiLabels[savedProfile.api_type] : null;
      status(detected ? `${detected} works. Saved to the shared desktop settings.`
        : "Saved to the shared desktop settings.", "active");
      return result;
    } catch (error) {
      status(error.message, "error");
      if (/revision|changed while editing|refresh/i.test(error.message)) {
        await refresh({ quiet: true });
      }
    } finally { setBusy(false); }
    return null;
  }
  async function refresh({ quiet = false } = {}) {
    if (state.busy) return;
    try {
      const result = await request("read");
      const changed = state.snapshot?.revision !== result.revision
        || state.snapshot?.instance_id !== result.instance_id
        || !state.snapshot;
      state.snapshot = result;
      if (changed) render();
      $("connectionBadge").textContent = "Connected";
      $("connectionBadge").dataset.state = "active";
      $("revisionLabel").textContent = `Shared settings revision ${result.revision} · backend ${result.instance_id.slice(0, 8)}`;
      if (!quiet) status("Connected. Models and services are in sync with the desktop app.", "active");
      await checkMigration();
    } catch (error) {
      $("connectionBadge").textContent = "Offline";
      $("connectionBadge").dataset.state = "error";
      if (!quiet || !state.snapshot) status(error.message, "error");
    }
  }
  function button(label, handler, danger = false) {
    const element = document.createElement("button");
    element.type = "button";
    element.className = danger ? "secondary-button danger" : "secondary-button";
    element.textContent = label;
    element.addEventListener("click", handler);
    return element;
  }
  function row(name, subtitle, stateName, actions) {
    const element = document.createElement("div");
    element.className = "management-row";
    const info = document.createElement("div");
    info.className = "management-info";
    const title = document.createElement("strong");
    title.textContent = name;
    const detail = document.createElement("small");
    detail.textContent = subtitle;
    const pill = document.createElement("span");
    pill.className = "state-pill";
    pill.dataset.state = stateName;
    pill.textContent = stateName;
    info.append(title, detail, pill);
    const actionBox = document.createElement("div");
    actionBox.className = "management-actions";
    actionBox.append(...actions);
    element.append(info, actionBox);
    return element;
  }
  function renderSpeech() {
    const list = $("speechList");
    list.replaceChildren();
    const items = state.snapshot.stt_models || [];
    $("speechCount").textContent = `${items.filter((item) => item.available).length} installed`;
    const current = state.snapshot.defaults?.stt?.model;
    for (const item of items) {
      const actions = [];
      if (item.available) {
        if (item.id !== current) actions.push(button("Use as default", () => perform("set_defaults", { stt_model: item.id }, "Saving speech default…")));
        if (item.id !== current && ["tiny", "base", "small", "medium", "large-v3"].includes(item.id)) {
          actions.push(button("Remove download", () => {
            if (global.confirm(`Remove the downloaded ${item.name} file? It can be downloaded again.`)) perform("remove_stt", { id: item.id }, "Removing speech download…");
          }, true));
        }
      } else if (["tiny", "base", "small", "medium", "large-v3"].includes(item.id)) {
        actions.push(button("Download", () => perform("install_stt", { id: item.id }, `Downloading ${item.name}…`, 60 * 60 * 1000)));
      }
      const label = `${item.id}${item.id === current ? " · desktop default" : ""}${item.reason ? " · " + item.reason : ""}`;
      list.append(row(item.name, label, item.available ? "installed" : item.status || "missing", actions));
    }
  }
  function renderLocal() {
    const list = $("localList");
    list.replaceChildren();
    const items = state.snapshot.local_models || [];
    $("localCount").textContent = `${items.filter((item) => item.published).length} in choices`;
    for (const item of items) {
      const actions = [];
      if (!item.installed) {
        actions.push(button("Download", () => perform("install_local", { id: item.id }, `Downloading ${item.name}…`, 60 * 60 * 1000)));
      } else {
        actions.push(button(item.published ? "Remove choice" : "Add to choices", () => perform("publish_local", { id: item.id, published: !item.published }, "Updating model choices…")));
      }
      if (item.custom) {
        actions.push(button("Edit", () => editLocal(item)));
        actions.push(button("Remove entry", () => {
          if (global.confirm(`Remove ${item.name} from the catalog? Downloaded files are kept.`)) perform("delete_local", { id: item.id }, "Removing model entry…");
        }, true));
      }
      const size = (item.size / (1024 ** 3)).toFixed(2);
      const detail = `${item.repo} · ${size} GiB · ${item.license}${item.reason ? " · " + item.reason : ""}`;
      list.append(row(item.name, detail, item.published ? "published" : item.status || "missing", actions));
    }
  }
  function selectionValue(selection) {
    return selection?.kind && selection?.id ? `${selection.kind}:${selection.id}` : "none:none";
  }
  function renderDefaults() {
    if (state.defaultsDirty) return;
    const defaults = state.snapshot.defaults || {};
    const stt = $("defaultStt");
    stt.replaceChildren();
    for (const item of state.snapshot.stt_models || []) {
      if (!item.available) continue;
      stt.add(new Option(item.name, item.id));
    }
    stt.value = defaults.stt?.model || "";
    const translation = $("defaultTranslation");
    translation.replaceChildren(new Option("None · subtitles only", "none:none"));
    for (const item of state.snapshot.local_models || []) {
      if (item.published) translation.add(new Option(`Local · ${item.name}`, `local:${item.id}`));
    }
    for (const item of state.snapshot.profiles || []) {
      if (!item.available) continue;
      translation.add(new Option(`Service · ${item.name}`, `profile:${item.id}`));
    }
    translation.value = selectionValue(defaults.translation?.selection);
    $("defaultLanguage").value = defaults.translation?.target_language || "zh-TW";
    $("defaultPartial").checked = Boolean(defaults.translation?.partial);
  }
  function renderServices() {
    const list = $("serviceList");
    list.replaceChildren();
    const items = state.snapshot.profiles || [];
    $("serviceCount").textContent = `${items.length} configured`;
    for (const item of items) {
      const actions = [button("Edit", () => editService(item))];
      if (item.available) actions.push(button("Use as default", () => perform("set_defaults", { translation_selection: { kind: "profile", id: item.id } }, "Saving translation default…")));
      actions.push(button("Remove", () => {
          if (global.confirm(`Remove ${item.name} from shared settings?`)) perform("delete_profile", { id: item.id }, "Removing service…");
        }, true));
      const format = ["openai", "openai_compatible"].includes(item.engine)
        ? ` · ${apiLabels[item.api_type] || item.api_type || "Untested"}` : "";
      const detail = `${item.engine} · ${item.model}${format}${item.base_url ? " · " + item.base_url : ""}${item.has_api_key ? " · key saved" : ""}`;
      list.append(row(item.name, detail + (item.reason ? " · " + item.reason : ""), item.status || "configured", actions));
    }
  }
  function render() {
    renderSpeech();
    renderLocal();
    renderServices();
    renderDefaults();
  }
  function editLocal(item) {
    $("editLocalId").value = item.id;
    $("editLocalName").value = item.name;
    $("editLocalNotes").value = item.notes || "";
    $("editLocalForm").hidden = false;
    $("editLocalForm").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  function resetService() {
    $("serviceForm").reset();
    $("serviceId").value = "";
    $("serviceEditorTitle").textContent = "Add translation service";
    $("serviceEditor").open = true;
    $("serviceApiKey").placeholder = "Optional API key";
    $("serviceAutoEndpoint").checked = true;
  }
  function editService(item) {
    resetService();
    $("serviceId").value = item.id;
    $("serviceName").value = item.name;
    $("serviceEngine").value = item.engine;
    $("serviceModel").value = item.model;
    $("serviceEndpoint").value = item.base_url || "";
    $("serviceAutoEndpoint").checked = item.auto_complete_endpoint !== false;
    $("serviceConcurrency").value = String(item.max_concurrency || 0);
    $("serviceApiKey").placeholder = item.has_api_key ? "Key saved · leave blank to keep it" : "Optional API key";
    $("serviceEditorTitle").textContent = `Edit ${item.name}`;
    $("serviceEditor").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  function clamp(value, min, max, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? Math.min(max, Math.max(min, number)) : fallback;
  }
  function appearanceText(id, value) {
    return id === "subtitleHistoryLines" ? String(value) : `${Math.round(value * 100)}%`;
  }
  async function loadAppearance() {
    const data = await storageGet([...Object.values(appearanceKeys), "subtitleFontScale"]);
    for (const [id, key] of Object.entries(appearanceKeys)) {
      const input = $(id);
      if (id === "subtitleShowPartial") input.checked = data[key] !== false;
      else {
        const fallback = id === "subtitleHistoryLines" ? 2 : id === "overlayOpacity" ? 0.85 : data.subtitleFontScale ?? 1;
        const minimum = Number(input.min), maximum = Number(input.max);
        input.value = String(clamp(data[key] ?? fallback, minimum, maximum, fallback));
        $(id + "Value").textContent = appearanceText(id, Number(input.value));
      }
    }
  }
  function migrationKey() {
    return state.snapshot?.server_id ? `${state.snapshot.server_id}|${state.wsUrl.replace(/\/$/, "")}` : "";
  }
  async function checkMigration() {
    const data = await storageGet(["translationModels", "desktopProfileMigration"]);
    const models = global.STTProfileMigration?.legacyProfiles?.(data.translationModels) || [];
    const migration = data.desktopProfileMigration;
    const complete = migration?.status === "complete" && migration.backendKey === migrationKey()
      && !(migration.pendingIds || []).length;
    $("migration").hidden = !models.length || complete;
    if (models.length) {
      $("migrationSummary").textContent = migration?.pendingIds?.length
        ? `${migration.pendingIds.length} saved service(s) still need attention.`
        : `${models.length} older service(s) can be imported into shared settings.`;
    }
  }
  function sendLegacyImport(payload) {
    return new Promise((resolve, reject) => {
      const url = localUrl(state.wsUrl);
      if (!url) return reject(new Error("Connect to a local backend before importing."));
      let socket, finished = false;
      const timer = global.setTimeout(() => finish(new Error("Import timed out.")), 10000);
      function finish(error, value) {
        if (finished) return;
        finished = true;
        global.clearTimeout(timer);
        try { socket?.close(); } catch { /* best effort */ }
        if (error) reject(error); else resolve(value);
      }
      try {
        socket = new global.WebSocket(url);
        socket.addEventListener("open", () => socket.send(JSON.stringify(payload)));
        socket.addEventListener("message", (event) => {
          let result; try { result = JSON.parse(event.data); } catch { return; }
          if (result?.type === "desktop_import_result") finish(null, result);
          if (result?.type === "error") finish(new Error(result.message || "Import failed."));
        });
        socket.addEventListener("error", () => finish(new Error("Backend is unavailable.")));
        socket.addEventListener("close", () => { if (!finished) finish(new Error("Import connection closed.")); });
      } catch (error) { finish(error); }
    });
  }
  async function importLegacy() {
    if (!state.snapshot || state.busy) return;
    setBusy(true);
    status("Importing saved services…", "syncing");
    try {
      const data = await storageGet(["translationModels"]);
      const result = await global.STTProfileMigration.importLegacyProfiles({
        models: data.translationModels,
        storageArea: global.chrome?.storage?.local,
        expectedRevision: state.snapshot.revision,
        backendKey: migrationKey(),
        getApiKey: (id) => global.STTConnectionSettings?.getApiKey?.(id) || Promise.resolve(""),
        send: sendLegacyImport,
      });
      if (!result.ok) throw new Error(result.errors?.map((item) => item.message).join(" · ") || result.error || "Import failed.");
      $("migrationStatus").textContent = result.partial ? "Some services still need attention." : "Import complete.";
      status(result.partial ? "Import partially complete." : "Saved services imported.", result.partial ? "warning" : "active");
      setBusy(false);
      await refresh({ quiet: true });
    } catch (error) {
      $("migrationStatus").textContent = error.message;
      status(error.message, "error");
    } finally { setBusy(false); }
  }
  function wire() {
    $("refreshSettings").addEventListener("click", async () => {
      state.wsUrl = $("wsUrl").value.trim();
      await storageSet({ wsUrl: state.wsUrl });
      state.snapshot = null;
      await refresh();
    });
    $("wsUrl").addEventListener("change", async () => {
      state.wsUrl = $("wsUrl").value.trim();
      await storageSet({ wsUrl: state.wsUrl });
    });
    for (const id of ["defaultStt", "defaultTranslation", "defaultLanguage", "defaultPartial"]) {
      $(id).addEventListener(id === "defaultLanguage" ? "input" : "change", () => { state.defaultsDirty = true; });
    }
    $("saveDefaults").addEventListener("click", () => {
      const value = $("defaultTranslation").value;
      const colon = value.indexOf(":");
      perform("set_defaults", {
        stt_model: $("defaultStt").value,
        translation_selection: { kind: value.slice(0, colon), id: value.slice(colon + 1) },
        target_language: $("defaultLanguage").value.trim(),
        partial: $("defaultPartial").checked,
      }, "Saving desktop defaults…");
    });
    $("addLocalForm").addEventListener("submit", (event) => {
      event.preventDefault();
      perform("add_local", { repo: $("localRepo").value.trim(), filename: $("localFilename").value.trim() }, "Reading pinned GGUF metadata…", 45000);
    });
    $("editLocalForm").addEventListener("submit", (event) => {
      event.preventDefault();
      perform("rename_local", { id: $("editLocalId").value, name: $("editLocalName").value.trim(), notes: $("editLocalNotes").value.trim() }, "Saving model details…");
      $("editLocalForm").hidden = true;
    });
    $("cancelLocalEdit").addEventListener("click", () => { $("editLocalForm").hidden = true; });
    $("serviceForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const profile = {
        id: $("serviceId").value.trim(), name: $("serviceName").value.trim(),
        engine: $("serviceEngine").value, model: $("serviceModel").value.trim(),
        base_url: $("serviceEndpoint").value.trim(),
        auto_complete_endpoint: $("serviceAutoEndpoint").checked,
        max_concurrency: Number($("serviceConcurrency").value || 0),
        api_key: $("serviceApiKey").value.trim(),
      };
      const tested = ["openai", "openai_compatible"].includes(profile.engine);
      const saved = await perform("save_profile", { profile, clear_api_key: $("serviceClearKey").checked },
        tested ? "Testing Chat completions, Responses and Messages…" : "Saving translation service…", tested ? 22000 : 8000);
      if (saved) {
        $("serviceId").value = saved.saved_profile_id || profile.id;
        $("serviceApiKey").value = "";
      }
    });
    $("resetServiceForm").addEventListener("click", resetService);
    $("migrateProfiles").addEventListener("click", importLegacy);
    for (const [id, key] of Object.entries(appearanceKeys)) {
      $(id).addEventListener(id === "subtitleShowPartial" ? "change" : "input", async () => {
        const input = $(id);
        const value = id === "subtitleShowPartial" ? input.checked : Number(input.value);
        if (id !== "subtitleShowPartial") $(id + "Value").textContent = appearanceText(id, value);
        await storageSet({ [key]: value });
      });
    }
    $("themeSelect").addEventListener("change", async () => {
      const theme = global.STTTheme?.applyTheme?.(document.documentElement, $("themeSelect").value) || $("themeSelect").value;
      await storageSet({ extensionTheme: theme });
    });
  }
  async function init() {
    wire();
    const saved = await storageGet(["wsUrl", "extensionTheme"]);
    state.wsUrl = saved.wsUrl || state.wsUrl;
    $("wsUrl").value = state.wsUrl;
    const theme = saved.extensionTheme || "system";
    $("themeSelect").value = theme;
    global.STTTheme?.applyTheme?.(document.documentElement, theme);
    global.STTTheme?.watchSystemTheme?.(document.documentElement, theme);
    await loadAppearance();
    await refresh();
    global.setInterval(() => refresh({ quiet: true }), 2500);
  }
  global.STTOptionsManagement = Object.freeze({ localUrl, request, refresh });
  init().catch((error) => status(error.message, "error"));
})(globalThis);

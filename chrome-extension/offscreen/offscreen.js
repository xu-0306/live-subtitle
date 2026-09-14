const sessions = new Map();

const KEEP_ALIVE_INTERVAL_MS = 15000;
const RECORDER_CHUNK_MS = 100;
const MAX_PENDING_BYTES = 8 * 1024 * 1024;
const MAX_BUFFERED_BYTES = 2 * 1024 * 1024;
const PENDING_FLUSH_INTERVAL_MS = 500;

function normalizeTabId(value) {
  if (Number.isInteger(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number.parseInt(value, 10);
    if (Number.isInteger(parsed)) {
      return parsed;
    }
  }
  return null;
}

function createSession(tabId, wsUrl, settings) {
  return {
    tabId,
    wsUrl,
    settings,
    snapshot: sanitizeCaptureSnapshot(settings),
    browserTranslation: settings?.browserTranslation || null,
    mediaStream: null,
    mediaRecorder: null,
    ws: null,
    running: false,
    status: "Idle",
    phase: "idle",
    keepAliveTimer: null,
    wsPingTimer: null,
    flushTimer: null,
    pendingChunks: [],
    pendingBytes: 0,
    chunkChain: Promise.resolve(),
    audioContext: null,
    sourceNode: null,
    monitorGain: null,
    closing: false,
    disconnectHandled: false,
    translationCache: new Map(),
    translationTokens: new Map(),
  };
}

function sanitizeCaptureSnapshot(settings) {
  const source = settings && typeof settings === "object" ? settings : {};
  const translation = source.translation && typeof source.translation === "object" ? source.translation : {};
  const selection = translation.selection && typeof translation.selection === "object"
    ? { kind: String(translation.selection.kind || ""), ...(translation.selection.id ? { id: String(translation.selection.id) } : {}) }
    : undefined;
  const stt = source.stt && typeof source.stt === "object"
    ? { ...(source.stt.model ? { model: String(source.stt.model) } : {}), ...(source.stt.backend ? { backend: String(source.stt.backend) } : {}) }
    : undefined;
  return {
    version: Number(source.version || 2),
    ...(["local", "profile", "none"].includes(selection?.kind) ? { selection } : {}),
    ...(translation.target_language ? { target_language: String(translation.target_language) } : {}),
    ...(typeof translation.partial === "boolean" ? { partial: translation.partial } : {}),
    ...(stt ? { stt } : {}),
  };
}

function sanitizeResolvedSnapshot(snapshot, fallback) {
  const source = snapshot && typeof snapshot === "object" ? snapshot : {};
  const result = { ...(fallback || {}) };
  const translation = source.translation && typeof source.translation === "object" ? source.translation : source;
  const selectionSource = translation.selection && typeof translation.selection === "object" ? translation.selection : source.selection;
  const selection = selectionSource && typeof selectionSource === "object"
    ? { kind: String(selectionSource.kind || ""), ...(selectionSource.id ? { id: String(selectionSource.id) } : {}) }
    : undefined;
  if (["local", "profile", "none"].includes(selection?.kind)) result.selection = selection;
  if (translation.target_language) result.target_language = String(translation.target_language);
  if (typeof translation.partial === "boolean") result.partial = translation.partial;
  if (source.stt && typeof source.stt === "object") {
    result.stt = {
      ...(source.stt.model ? { model: String(source.stt.model) } : {}),
      ...(source.stt.backend ? { backend: String(source.stt.backend) } : {}),
    };
  }
  if (source.name) result.name = String(source.name);
  if (Number.isFinite(Number(source.captures))) result.captures = Number(source.captures);
  return result;
}

function isSessionActive(session) {
  return sessions.get(session.tabId) === session;
}

function notifyBackground(tabId, status, runningOverride, retryable = true, extra = {}) {
  const session = sessions.get(normalizeTabId(tabId));
  if (session) {
    session.status = status || session.status;
    session.phase = extra.phase || classifyPhase(status);
  }
  chrome.runtime.sendMessage({
    type: "offscreen-state",
    tabId,
    status,
    phase: extra.phase || classifyPhase(status),
    snapshot: extra.snapshot || session?.snapshot || null,
    retryable,
    running: typeof runningOverride === "boolean" ? runningOverride : false,
  });
}

function classifyPhase(status) {
  const value = String(status || "").trim().toLowerCase();
  if (!value) return "idle";
  if (value.includes("starting") || value.includes("prepar")) return "starting";
  if (value.includes("connect")) return "connecting";
  if (value.includes("stopp")) return "stopping";
  if (value.includes("error") || value.includes("fail") || value.includes("disconnect")) return "error";
  if (value.includes("captur") || value.includes("ready")) return "capturing";
  return "status";
}

function buildBackendConfig(settings) {
  const source = settings && typeof settings === "object" ? settings : {};
  const payload = { type: "config", version: Number(source.version || 2) };
  if (source.translation && typeof source.translation === "object") payload.translation = source.translation;
  if (source.stt && typeof source.stt === "object") payload.stt = source.stt;
  return payload;
}

function forwardPayload(session, payload) {
  chrome.runtime.sendMessage(
    { type: "offscreen-subtitle", tabId: session.tabId, payload },
    () => {}
  );
}

function getTranslationCacheKey(session, payload) {
  const cfg = session.browserTranslation || {};
  const openaiCfg = cfg.openai || {};
  return JSON.stringify([
    openaiCfg.base_url || openaiCfg.api_url || "",
    openaiCfg.model || "",
    cfg.target_language || "",
    payload.language || "",
    payload.original || "",
  ]);
}

async function translateSubtitleInBrowser(session, payload) {
  if (!session.browserTranslation || !globalThis.STTTranslationClient) {
    return;
  }
  if (!payload?.original) {
    return;
  }
  const shouldTranslatePartial = Boolean(session.browserTranslation.partial);
  if (!payload.final && !shouldTranslatePartial) {
    return;
  }

  const cacheKey = getTranslationCacheKey(session, payload);
  if (session.translationCache.has(cacheKey)) {
    const translated = session.translationCache.get(cacheKey);
    if (translated) {
      forwardPayload(session, { ...payload, translated });
    }
    return;
  }

  const token = Symbol(String(payload.seq ?? "subtitle"));
  const seqKey = String(payload.seq ?? "subtitle");
  session.translationTokens.set(seqKey, token);
  try {
    const translated = await globalThis.STTTranslationClient.translate(
      session.browserTranslation,
      payload.original,
      payload.language || "auto"
    );
    if (!isSessionActive(session)) return;
    if (session.translationTokens.get(seqKey) !== token) return;
    if (!translated) return;
    session.translationCache.set(cacheKey, translated);
    if (session.translationCache.size > 256) {
      const oldestKey = session.translationCache.keys().next().value;
      if (oldestKey) {
        session.translationCache.delete(oldestKey);
      }
    }
    forwardPayload(session, { ...payload, translated });
  } catch (err) {
    if (!isSessionActive(session)) return;
    const label = globalThis.STTTranslationClient.describeConfig(
      session.browserTranslation
    );
    forwardPayload(session, {
      type: "error",
      message: `Browser translation failed: ${label} :: ${err?.message || err}`,
    });
  } finally {
    if (session.translationTokens.get(seqKey) === token) {
      session.translationTokens.delete(seqKey);
    }
  }
}

function startKeepAlive(session) {
  stopKeepAlive(session);
  session.keepAliveTimer = setInterval(() => {
    chrome.runtime.sendMessage({ type: "offscreen-keepalive" });
  }, KEEP_ALIVE_INTERVAL_MS);
  session.wsPingTimer = setInterval(() => {
    if (session.ws && session.ws.readyState === WebSocket.OPEN) {
      session.ws.send(JSON.stringify({ type: "ping", ts: Date.now() }));
    }
  }, KEEP_ALIVE_INTERVAL_MS);
  session.flushTimer = setInterval(() => {
    flushPendingChunks(session);
  }, PENDING_FLUSH_INTERVAL_MS);
}

function stopKeepAlive(session) {
  if (session.keepAliveTimer) {
    clearInterval(session.keepAliveTimer);
    session.keepAliveTimer = null;
  }
  if (session.wsPingTimer) {
    clearInterval(session.wsPingTimer);
    session.wsPingTimer = null;
  }
  if (session.flushTimer) {
    clearInterval(session.flushTimer);
    session.flushTimer = null;
  }
}

function canSendNow(session) {
  return (
    session.ws &&
    session.ws.readyState === WebSocket.OPEN &&
    session.ws.bufferedAmount < MAX_BUFFERED_BYTES
  );
}

function enqueueChunk(session, buffer) {
  if (!buffer || buffer.byteLength === 0) {
    return;
  }
  if (session.pendingChunks.length === 0 && canSendNow(session)) {
    session.ws.send(buffer);
    return;
  }
  session.pendingChunks.push(buffer);
  session.pendingBytes += buffer.byteLength;
  while (session.pendingBytes > MAX_PENDING_BYTES && session.pendingChunks.length > 0) {
    const dropped = session.pendingChunks.shift();
    if (dropped) {
      session.pendingBytes -= dropped.byteLength;
    }
  }
  if (session.ws && session.ws.readyState === WebSocket.OPEN) {
    flushPendingChunks(session);
  }
}

function flushPendingChunks(session) {
  if (!session.ws || session.ws.readyState !== WebSocket.OPEN || session.pendingChunks.length === 0) {
    return;
  }
  while (session.pendingChunks.length > 0 && session.ws.bufferedAmount < MAX_BUFFERED_BYTES) {
    const chunk = session.pendingChunks.shift();
    if (!chunk) {
      continue;
    }
    session.pendingBytes -= chunk.byteLength;
    session.ws.send(chunk);
  }
  if (session.pendingChunks.length === 0) {
    session.pendingBytes = 0;
  } else if (session.pendingBytes < 0) {
    session.pendingBytes = 0;
  }
}

function pickMimeType() {
  const preferred = "audio/webm;codecs=opus";
  if (MediaRecorder.isTypeSupported(preferred)) {
    return preferred;
  }
  if (MediaRecorder.isTypeSupported("audio/webm")) {
    return "audio/webm";
  }
  return "";
}

async function startCapture(tabId, streamId, wsUrl, settings) {
  const normalizedTabId = normalizeTabId(tabId);
  if (!normalizedTabId) {
    throw new Error("Invalid tab id");
  }
  if (sessions.has(normalizedTabId)) {
    await stopCapture(normalizedTabId);
  }

  const session = createSession(normalizedTabId, wsUrl, settings);
  sessions.set(normalizedTabId, session);

  session.running = true;
  session.closing = false;
  session.disconnectHandled = false;
  notifyBackground(tabId, "Starting...", true, true, { phase: "starting", snapshot: session.snapshot });
  startKeepAlive(session);
  session.pendingChunks = [];
  session.pendingBytes = 0;
  session.chunkChain = Promise.resolve();

  try {
    session.ws = new WebSocket(wsUrl);
    session.ws.binaryType = "arraybuffer";
    session.ws.addEventListener("open", () => {
      if (!isSessionActive(session)) return;
      notifyBackground(tabId, "Connected", true, true, { phase: "connecting", snapshot: session.snapshot });
      if (settings && (settings.translation || settings.stt)) {
        session.ws.send(JSON.stringify(buildBackendConfig(settings)));
      }
      flushPendingChunks(session);
    });
    session.ws.addEventListener("message", (event) => {
      if (!isSessionActive(session)) return;
      if (typeof event.data !== "string") {
        return;
      }
      try {
        const payload = JSON.parse(event.data);
        if (payload.snapshot || payload.capture_snapshot || payload.active) {
          session.snapshot = sanitizeResolvedSnapshot(
            payload.snapshot || payload.capture_snapshot || payload.active,
            session.snapshot
          );
        }
        if (payload.type === "subtitle") {
          forwardPayload(session, payload);
          if (payload.phase || payload.status) {
            notifyBackground(tabId, payload.message || payload.phase || "Status", true, true, { phase: payload.phase, snapshot: session.snapshot });
          }
          if (session.browserTranslation) {
            translateSubtitleInBrowser(session, payload);
          }
        } else if (payload.type === "status" || payload.type === "error") {
          if (payload.type === "error") {
            session.lastError = payload.message;
            forwardPayload(session, payload);
            notifyBackground(tabId, payload.message || "Backend error", false, false, { phase: payload.phase || "error", snapshot: session.snapshot });
          } else {
            const phase = payload.phase || classifyPhase(payload.message);
            forwardPayload(session, payload);
            notifyBackground(tabId, payload.message || "Status", !["error", "failed", "stopping", "stopped", "idle"].includes(phase), true, { phase, snapshot: session.snapshot });
          }
        }
      } catch (err) {
        console.error("[offscreen] JSON parse error:", err.message);
      }
    });
    session.ws.addEventListener("close", (event) => {
      if (!isSessionActive(session)) return;
      const retryable = event.code !== 1008;
      handleWsDisconnect(session, session.lastError || `Disconnected (${event.code || 0})`, retryable);
    });
    session.ws.addEventListener("error", () => {
      if (!isSessionActive(session)) return;
      handleWsDisconnect(session, "WebSocket error");
    });

    session.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: {
          chromeMediaSource: "tab",
          chromeMediaSourceId: streamId,
        },
      },
    });

    try {
      // Route captured audio to output so the tab doesn't go silent during capture.
      session.audioContext = new AudioContext();
      await session.audioContext.resume();
      session.audioContext.addEventListener("statechange", () => {
        if (session.running && session.audioContext && session.audioContext.state === "suspended") {
          session.audioContext.resume();
        }
      });
      session.sourceNode = session.audioContext.createMediaStreamSource(session.mediaStream);
      session.monitorGain = session.audioContext.createGain();
      session.monitorGain.gain.value = 1;
      session.sourceNode.connect(session.monitorGain);
      session.monitorGain.connect(session.audioContext.destination);
    } catch (err) {
      console.warn("[offscreen] audio monitor init error:", err?.message || err);
    }

    const mimeType = pickMimeType();
    const options = mimeType ? { mimeType } : undefined;
    session.mediaRecorder = new MediaRecorder(session.mediaStream, options);
    session.mediaRecorder.addEventListener("dataavailable", (event) => {
      if (!isSessionActive(session)) return;
      const blob = event.data;
      if (!blob || blob.size === 0) {
        return;
      }
      session.chunkChain = session.chunkChain
        .then(async () => {
          const buffer = await blob.arrayBuffer();
          enqueueChunk(session, buffer);
        })
        .catch((err) => {
          console.warn("[offscreen] chunk encode error:", err?.message || err);
        });
    });
    session.mediaRecorder.start(RECORDER_CHUNK_MS);
  } catch (err) {
    session.running = false;
    session.closing = true;
    session.disconnectHandled = true;
    notifyBackground(tabId, err?.message || "Capture start failed", false);
    await cleanupCaptureResources(session, { closeWebSocket: true });
    session.closing = false;
    if (isSessionActive(session)) {
      sessions.delete(normalizedTabId);
    }
    throw err;
  }
}

async function cleanupCaptureResources(session, options = {}) {
  const closeWebSocket = options.closeWebSocket !== false;

  if (session.mediaRecorder) {
    try {
      const recorder = session.mediaRecorder;
      const stopped = new Promise((resolve) => {
        recorder.addEventListener("stop", resolve, { once: true });
      });
      try {
        recorder.requestData();
      } catch (err) {
        // Ignore if recorder is inactive.
      }
      recorder.stop();
      await Promise.race([
        stopped,
        new Promise((resolve) => setTimeout(resolve, 1000)),
      ]);
      await Promise.race([
        session.chunkChain,
        new Promise((resolve) => setTimeout(resolve, 1000)),
      ]);
    } catch (err) {
      console.warn("[offscreen] MediaRecorder stop error:", err.message);
    }
    session.mediaRecorder = null;
  }

  if (session.mediaStream) {
    session.mediaStream.getTracks().forEach((track) => track.stop());
    session.mediaStream = null;
  }

  if (session.monitorGain) {
    session.monitorGain.disconnect();
    session.monitorGain = null;
  }

  if (session.sourceNode) {
    session.sourceNode.disconnect();
    session.sourceNode = null;
  }

  if (session.audioContext) {
    try {
      await session.audioContext.close();
    } catch (err) {
      console.warn("[offscreen] AudioContext close error:", err?.message || err);
    }
    session.audioContext = null;
  }

  if (closeWebSocket && session.ws) {
    session.ws.close();
    session.ws = null;
  }
  if (!closeWebSocket) {
    session.ws = null;
  }
  stopKeepAlive(session);
}

function handleWsDisconnect(session, message, retryable = true) {
  if (session.closing || session.disconnectHandled) return;
  session.disconnectHandled = true;
  session.running = false;
  notifyBackground(session.tabId, message, false, retryable);
  if (session.ws && session.ws.readyState === WebSocket.OPEN) {
    try {
      session.ws.close();
    } catch (err) {
      // Ignore close errors.
    }
  }
  session.ws = null;
  cleanupCaptureResources(session, { closeWebSocket: false })
    .catch((err) => {
      console.warn("[offscreen] cleanup error:", err?.message || err);
    })
    .finally(() => {
      if (isSessionActive(session)) {
        sessions.delete(session.tabId);
      }
    });
}

async function stopCapture(tabId) {
  const normalizedTabId = normalizeTabId(tabId);
  if (!normalizedTabId) {
    throw new Error("Invalid tab id");
  }
  const session = sessions.get(normalizedTabId);
  if (!session) return;
  session.running = false;
  session.closing = true;
  session.disconnectHandled = true;
  notifyBackground(tabId, "Stopping...", false);
  await cleanupCaptureResources(session, { closeWebSocket: true });
  session.closing = false;
  if (isSessionActive(session)) {
    sessions.delete(normalizedTabId);
  }
  notifyBackground(tabId, "Stopped", false);
}

async function stopAllCaptures() {
  const tabIds = Array.from(sessions.keys());
  for (const tabId of tabIds) {
    await stopCapture(tabId);
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "offscreen-start") {
    const tabId = message.tabId;
    startCapture(tabId, message.streamId, message.wsUrl, message.settings)
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }

  if (message.type === "offscreen-status") {
    const requestedTabId = normalizeTabId(message.tabId);
    const session = requestedTabId !== null ? sessions.get(requestedTabId) : null;
    const running = Boolean(session?.running);
    const anyRunning = Array.from(sessions.values()).some((item) => item.running);
    sendResponse({
      ok: true,
      running,
      status: session?.status || (running ? "Capturing" : "Idle"),
      phase: session?.phase || (running ? "capturing" : "idle"),
      snapshot: session?.snapshot || null,
      wsReadyState: session?.ws ? session.ws.readyState : null,
      anyRunning,
      occupancy: Array.from(sessions.values()).filter((item) => item.running).map((item) => ({ tabId: item.tabId, snapshot: item.snapshot })),
    });
    return true;
  }

  if (message.type === "offscreen-stop") {
    const tabId = message.tabId;
    const stopPromise = Number.isInteger(tabId) ? stopCapture(tabId) : stopAllCaptures();
    stopPromise
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }

  return false;
});

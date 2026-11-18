(function (global) {
  "use strict";

  // Looks up the newest GitHub release and compares it with this extension's version. An unpacked
  // extension cannot update itself, so this only detects and records; the popup and Options show it.
  // The only request is a GET of GitHub's public release JSON: no cookies, no referrer, no user data.
  // gui/update_check.py asks the same two URLs; a pytest compares them.
  const RELEASES_API = "https://api.github.com/repos/xu-0306/live-subtitle/releases/latest";
  const RELEASES_PAGE = "https://github.com/xu-0306/live-subtitle/releases/latest";
  const STATE_KEY = "updateCheck"; // written by the service worker only
  const ENABLED_KEY = "updateCheckEnabled"; // false switches the automatic check and the banner off
  const DISMISSED_KEY = "updateDismissed"; // the version whose banner was dismissed (written by the pages)
  const ALARM = "update-check";
  const ALARM_PERIOD_MINUTES = 360; // the alarm only wakes the worker; INTERVAL_* decide whether to ask
  const INTERVAL_OK = 24 * 3600 * 1000;
  const INTERVAL_FAIL = 3600 * 1000;
  const TIMEOUT_MS = 8000;
  const MAX_CHARS = 1000000;

  const VERSION = /^[vV]?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/;

  // 1.2.3, v1.2.3, 1.2.3.4 or 1.2.3-rc.1 (build metadata is ignored); null for anything else.
  function parseVersion(text) {
    const match = VERSION.exec(String(text ?? "").trim());
    if (!match) return null;
    return {
      numbers: [match[1], match[2], match[3], match[4] ?? 0].map(Number),
      pre: match[5] === undefined ? null : match[5].split("."),
    };
  }

  // -1, 0 or 1 by SemVer precedence (a pre-release sorts below its release); null if either is not a version.
  function compareVersions(a, b) {
    const x = parseVersion(a);
    const y = parseVersion(b);
    if (!x || !y) return null;
    for (let i = 0; i < 4; i += 1) {
      if (x.numbers[i] !== y.numbers[i]) return x.numbers[i] < y.numbers[i] ? -1 : 1;
    }
    if (!x.pre || !y.pre) return x.pre === y.pre ? 0 : x.pre ? -1 : 1;
    for (let i = 0; i < Math.min(x.pre.length, y.pre.length); i += 1) {
      const p = x.pre[i];
      const q = y.pre[i];
      if (p === q) continue;
      const pNumeric = /^\d+$/.test(p);
      const qNumeric = /^\d+$/.test(q);
      if (pNumeric && qNumeric) {
        const difference = Number(p) - Number(q);
        if (difference) return Math.sign(difference);
        continue;
      }
      if (pNumeric !== qNumeric) return pNumeric ? -1 : 1;
      return p < q ? -1 : 1;
    }
    return Math.sign(x.pre.length - y.pre.length);
  }

  function isNewer(candidate, current) {
    return compareVersions(candidate, current) === 1;
  }

  function githubPage(url) {
    try {
      const parsed = new URL(url);
      return parsed.protocol === "https:" && parsed.host === "github.com" ? parsed.href : RELEASES_PAGE;
    } catch { return RELEASES_PAGE; }
  }

  // GitHub's "latest release" JSON as {version, tag, url, published}; null for a draft, a pre-release or bad data.
  function parseRelease(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
    if (payload.draft || payload.prerelease) return null;
    const tag = typeof payload.tag_name === "string" ? payload.tag_name.trim() : "";
    if (!parseVersion(tag)) return null;
    return {
      version: tag.replace(/^[vV]/, ""),
      tag,
      url: githubPage(payload.html_url),
      published: typeof payload.published_at === "string" ? payload.published_at : "",
    };
  }

  class UpdateCheckError extends Error {}

  async function fetchLatestRelease(fetchImpl = global.fetch, timeoutMs = TIMEOUT_MS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      let response;
      let body;
      try {
        response = await fetchImpl(RELEASES_API, {
          headers: { Accept: "application/vnd.github+json" },
          credentials: "omit",
          referrerPolicy: "no-referrer",
          cache: "no-store",
          signal: controller.signal,
        });
        if (response.ok) body = await response.text();
      } catch { throw new UpdateCheckError("Could not reach GitHub."); }
      if (!response.ok) {
        if (response.status === 403 || response.status === 429) throw new UpdateCheckError("GitHub is limiting requests right now; try again later.");
        if (response.status === 404) throw new UpdateCheckError("No release was found.");
        throw new UpdateCheckError(`GitHub answered HTTP ${response.status}.`);
      }
      let release = null;
      try { if (body.length <= MAX_CHARS) release = parseRelease(JSON.parse(body)); } catch { /* handled below */ }
      if (!release) throw new UpdateCheckError("GitHub sent an answer this extension does not understand.");
      return release;
    } finally { clearTimeout(timer); }
  }

  // Whether the automatic check may run: switched on, and the last one is old enough (a failure retries sooner).
  function isDue(state, enabled, now = Date.now()) {
    if (enabled === false) return false;
    const last = state?.checkedAt;
    if (typeof last !== "number" || !Number.isFinite(last) || last > now) return true; // never, or the clock went back
    return now - last >= (state.ok ? INTERVAL_OK : INTERVAL_FAIL);
  }

  function storageGet(area, keys) {
    return new Promise((resolve) => {
      try { area.get(keys, (data) => resolve(data || {})); } catch { resolve({}); }
    });
  }
  function storageSet(area, values) {
    return new Promise((resolve) => {
      try { area.set(values, () => resolve()); } catch { resolve(); }
    });
  }

  // Asks GitHub when a check is due (always when `force`) and records the outcome; never rejects.
  async function runCheck({ storage, fetchImpl, now = Date.now(), force = false }) {
    const data = await storageGet(storage, [STATE_KEY, ENABLED_KEY]);
    const state = data[STATE_KEY] && typeof data[STATE_KEY] === "object" ? data[STATE_KEY] : {};
    if (!force && !isDue(state, data[ENABLED_KEY], now)) return state;
    let next;
    try {
      const latest = await fetchLatestRelease(fetchImpl);
      next = { checkedAt: now, ok: true, error: "", latest };
    } catch (error) {
      // Keep the last good answer: a failed check must not hide a banner that is already showing.
      next = { ...state, checkedAt: now, ok: false, error: error instanceof UpdateCheckError ? error.message : "Update check failed." };
    }
    await storageSet(storage, { [STATE_KEY]: next });
    return next;
  }

  // What the pages show. `dismissed` hides the popup banner only; Options always shows the news.
  function describe(state, dismissedVersion, currentVersion) {
    const latest = state?.latest;
    const available = Boolean(latest) && isNewer(latest.version, currentVersion);
    return {
      current: currentVersion,
      available,
      version: available ? latest.version : "",
      url: available ? latest.url : RELEASES_PAGE,
      dismissed: available && dismissedVersion === latest.version,
      checkedAt: Number(state?.checkedAt) || 0,
      ok: state ? state.ok !== false : true,
      error: state?.error || "",
    };
  }

  function summary(info) {
    if (!info.checkedAt) return "Not checked yet.";
    const date = new Date(info.checkedAt);
    const pad = (n) => String(n).padStart(2, "0");
    const when = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
    if (!info.ok) return `Last check ${when}: ${info.error || "could not reach GitHub."}`;
    if (info.available) return `Last check ${when}: version ${info.version} is available.`;
    return `Last check ${when}: this is the latest version.`;
  }

  global.STTUpdateCheck = Object.freeze({
    RELEASES_API,
    RELEASES_PAGE,
    STATE_KEY,
    ENABLED_KEY,
    DISMISSED_KEY,
    ALARM,
    ALARM_PERIOD_MINUTES,
    INTERVAL_OK,
    INTERVAL_FAIL,
    UpdateCheckError,
    parseVersion,
    compareVersions,
    isNewer,
    parseRelease,
    fetchLatestRelease,
    isDue,
    runCheck,
    describe,
    summary,
  });
  if (typeof module !== "undefined") module.exports = global.STTUpdateCheck;
})(globalThis);

(function (global) {
  "use strict";

  // The desktop app can overwrite this unpacked extension's folder with a newer copy (Settings > Chrome extension),
  // writing manifest.json last. Chrome keeps running the old code until the extension is reloaded, so the service
  // worker looks at the manifest on disk now and then and reloads itself when it names a newer version.
  // A newer manifest on disk therefore means the other files are already in place.
  const ALARM = "extension-files"; // wakes the worker; the check itself reads one small local file
  const ALARM_PERIOD_MINUTES = 5;

  // The version written in the manifest file on disk, or "" when it cannot be read.
  async function readDiskVersion(fetchImpl, manifestUrl) {
    try {
      const response = await fetchImpl(`${manifestUrl}?t=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) return "";
      const version = JSON.parse(await response.text()).version;
      return typeof version === "string" ? version : "";
    } catch { return ""; }
  }

  // {action: "reload"} when the files on disk are newer and nothing is capturing, {action: "wait"} when they are
  // newer but a capture is running, {action: ""} otherwise (same version, older, unreadable).
  async function decide({ fetchImpl, manifestUrl, runningVersion, isNewer, isBusy }) {
    const disk = await readDiskVersion(fetchImpl, manifestUrl);
    if (!disk || !isNewer(disk, runningVersion)) return { action: "", disk };
    return { action: (await isBusy()) ? "wait" : "reload", disk };
  }

  global.STTFilesUpdate = Object.freeze({ ALARM, ALARM_PERIOD_MINUTES, readDiskVersion, decide });
  if (typeof module !== "undefined") module.exports = global.STTFilesUpdate;
})(globalThis);

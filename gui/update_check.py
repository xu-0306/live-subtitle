"""Ask GitHub for the newest Live Subtitle release and compare it with this build.

No Qt here, and nothing in this module raises on a network problem: ``check`` always returns a
``Result``.  The only request is a GET of the public "latest release" JSON; nothing about the user,
the audio or the settings is sent.  Opening the download page is left to the caller.

This build's version has a single source, ``chrome-extension/manifest.json`` (``build-release.ps1``
names the extension ZIP from it, and the release tag is ``v`` + that version); the PyInstaller spec
ships the file next to ``backend/config.yaml`` so the packaged app reads the same value.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REPO = "xu-0306/live-subtitle"
# chrome-extension/shared/update-check.js carries the same two URLs; a test compares them.
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
MANIFEST_PATH = Path(__file__).resolve().parents[1] / "chrome-extension" / "manifest.json"

INTERVAL_OK = 24 * 3600  # ask again after a good answer ...
INTERVAL_FAIL = 3600  # ... or after a failed one (offline, rate limited)
TIMEOUT = 8
MAX_BYTES = 1_000_000
MAX_NOTES = 6000

_VERSION = re.compile(
    r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def parse_version(text: object) -> tuple | None:
    """Sort key for ``1.2.3``, ``v1.2.3``, ``1.2.3.4`` or ``1.2.3-rc.1``; ``None`` if it is not one.

    SemVer precedence: a pre-release sorts below its release, numeric identifiers below alphanumeric
    ones, and build metadata (``+...``) is ignored.
    """
    match = _VERSION.match(str(text or "").strip())
    if not match:
        return None
    major, minor, patch, fourth, pre = match.groups()
    numbers = (int(major), int(minor), int(patch), int(fourth or 0))
    if pre is None:
        return numbers, 1, ()
    return numbers, 0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))


def is_newer(candidate: object, current: object) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    return new is not None and old is not None and new > old


def read_app_version(path: Path = MANIFEST_PATH) -> str:
    """This build's version, or ``""`` when the manifest is unreadable (the check is then skipped)."""
    try:
        version = json.loads(Path(path).read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return ""
    return str(version) if parse_version(version) else ""


@dataclass(frozen=True)
class Release:
    version: str  # without the leading "v"
    tag: str
    url: str  # always an https://github.com/ page
    notes: str
    published: str


@dataclass(frozen=True)
class Result:
    status: str  # "new" | "current" | "error"
    release: Release | None = None
    error: str = ""


class UpdateCheckError(Exception):
    """A failed lookup; the message is short enough to show to the user."""


def _github_page(url: object) -> str:
    parts = urlsplit(url) if isinstance(url, str) else None
    return url if parts and parts.scheme == "https" and parts.netloc == "github.com" else RELEASES_PAGE


def parse_release(payload: object) -> Release | None:
    """The GitHub "latest release" JSON as a ``Release``; ``None`` for a draft, a pre-release or bad data."""
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or parse_version(tag) is None:
        return None
    notes = payload.get("body") if isinstance(payload.get("body"), str) else ""
    notes = notes.replace("\r\n", "\n").strip()
    if len(notes) > MAX_NOTES:
        notes = notes[:MAX_NOTES].rstrip() + "\n…"
    published = payload.get("published_at")
    return Release(tag.strip().lstrip("vV"), tag.strip(), _github_page(payload.get("html_url")), notes,
                   published if isinstance(published, str) else "")


def fetch_latest_release(url: str = RELEASES_API, timeout: float = TIMEOUT) -> Release:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "Live-Subtitle-update-check"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise UpdateCheckError("GitHub is limiting requests right now; try again later.") from exc
        if exc.code == 404:
            raise UpdateCheckError("No release was found.") from exc
        raise UpdateCheckError(f"GitHub answered HTTP {exc.code}.") from exc
    except (OSError, ValueError) as exc:  # URLError, timeouts and refused connections are OSErrors
        raise UpdateCheckError("Could not reach GitHub.") from exc
    try:
        release = parse_release(json.loads(raw[:MAX_BYTES].decode("utf-8"))) if len(raw) <= MAX_BYTES else None
    except ValueError:
        release = None
    if release is None:
        raise UpdateCheckError("GitHub sent an answer this app does not understand.")
    return release


def check(current: str, fetch=fetch_latest_release) -> Result:
    if parse_version(current) is None:
        return Result("error", error="This app's version is unknown.")
    try:
        release = fetch()
    except UpdateCheckError as exc:
        return Result("error", error=str(exc))
    except Exception as exc:  # the check must never take the app down
        return Result("error", error=f"{type(exc).__name__}: {exc}")
    return Result("new" if is_newer(release.version, current) else "current", release)


# --- remembered state: {"enabled", "checked_at", "ok", "latest", "skipped"} in update-check.json ---

def load_state(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict) -> bool:
    # ponytail: plain write; a torn file reads back as {} (defaults), so no temp-file dance.
    try:
        Path(path).write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError:
        return False
    return True


def is_enabled(state: dict) -> bool:
    return state.get("enabled") is not False


def is_due(state: dict, now: float | None = None) -> bool:
    """Whether a start-up check may run: enabled, and the last one is old enough (a failure retries sooner)."""
    now = time.time() if now is None else now
    if not is_enabled(state):
        return False
    last = state.get("checked_at")
    if not isinstance(last, (int, float)) or last > now:  # never checked, or the clock went backwards
        return True
    return now - last >= (INTERVAL_OK if state.get("ok") else INTERVAL_FAIL)


def record_result(state: dict, result: Result, now: float | None = None) -> dict:
    updated = dict(state, checked_at=time.time() if now is None else now, ok=result.status != "error")
    if result.release is not None:
        updated["latest"] = result.release.version
    return updated


def is_skipped(state: dict, release: Release) -> bool:
    return state.get("skipped") == release.version


def describe_state(state: dict, current: str) -> str:
    """One line for the About card: when the last check ran and what it found."""
    last = state.get("checked_at")
    if not isinstance(last, (int, float)):
        return "Not checked yet."
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last))
    if not state.get("ok"):
        return f"Last check {when}: could not reach GitHub."
    if is_newer(state.get("latest"), current):
        return f"Last check {when}: version {state['latest']} is available."
    return f"Last check {when}: this is the latest version."

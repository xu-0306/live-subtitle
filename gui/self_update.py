"""Install a new Live Subtitle build over the running one (packaged Windows app only). No Qt here.

A running exe cannot replace its own files, so the update is done in two steps:

1. ``download_and_stage``: download the release ZIP (size and SHA-256 come from GitHub's own record of
   the upload), unpack it into ``<install folder>.new`` and drop a ready marker as the last file.
2. ``apply`` (the user chose "Restart now"): start a hidden PowerShell that waits until every process of
   this install has exited, renames ``<install>`` to ``<install>.old`` and ``<install>.new`` to
   ``<install>``, starts the new exe and waits for it to report in (``confirm_started``).  If it never
   does, the two folders are renamed back and the old version starts again.

Renaming whole folders keeps the old version intact until the new one has proved itself; copying files
over a live install would leave a mix of both when anything fails half way.  The script is passed as
``-EncodedCommand`` because an execution policy only governs script *files*, and every path travels in an
environment variable, so no quoting can go wrong.  Settings, models and runtimes live in
``%APPDATA%\\Live Subtitle``, outside the install folder, and are not touched.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable

from backend import local_runtime
from backend.local_catalog import Artifact
from gui import update_check

READY = ".update-ready"  # last file written into <install>.new: its text is the staged version
OK_ENV = "LIVE_SUBTITLE_UPDATE_OK"  # set by the helper: the file the new version must create once it is up
RESULT = "last-result.json"  # the helper's verdict, shown once at the next start
STARTED = "helper-started"
CANCEL = "helper-cancel"
MAX_UNPACKED = 4 * 1024**3
_SPARE = 256 * 1024**2

Cancelled = local_runtime.Cancelled
Progress = Callable[[str, int, int], None]  # (phase: "download" | "verify" | "unpack", done, total)

_SCRIPT = r"""
$ErrorActionPreference = 'Continue'
$app = $env:LS_UPD_APP; $new = $env:LS_UPD_NEW; $old = $env:LS_UPD_OLD; $exe = $env:LS_UPD_EXE
$ok = $env:LS_UPD_OK; $dir = $env:LS_UPD_DIR; $ver = $env:LS_UPD_VERSION
$grace = [int]$env:LS_UPD_GRACE; $tries = [int]$env:LS_UPD_TRIES; $okWait = [int]$env:LS_UPD_OKWAIT
Set-Location -LiteralPath $dir
Set-Content -LiteralPath (Join-Path $dir 'helper-started') -Value $PID
if (Test-Path -LiteralPath (Join-Path $dir 'helper-cancel')) { exit }
function Result($status, $detail) {
  @{ version = $ver; status = $status; detail = $detail } | ConvertTo-Json |
    Set-Content -LiteralPath (Join-Path $dir 'last-result.json') -Encoding UTF8
}
function Running {
  Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($app + '\', [StringComparison]::OrdinalIgnoreCase) }
}
# Rename-Item, never Move-Item: Move-Item moves a folder file by file when the rename is refused (a locked
# file), leaving the install split in two, and nests it inside the target if that already exists.  A rename
# either happens completely or not at all.  All the folders involved are siblings.
$script:why = ''
function Move-Dir($from, $to) {
  for ($i = 0; $i -lt $tries; $i++) {
    try { Rename-Item -LiteralPath $from -NewName (Split-Path -Leaf $to) -ErrorAction Stop; return $true }
    catch { $script:why = $_.Exception.Message; Start-Sleep -Seconds 1 }
  }
  return $false
}
function Launch { Start-Process -FilePath (Join-Path $app $exe) -WorkingDirectory $app }
function Drop($path) {
  if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }
}
$t = (Get-Date).AddSeconds($grace)
while ((Running) -and (Get-Date) -lt $t) { Start-Sleep -Milliseconds 300 }
Running | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Drop $old
if (Test-Path -LiteralPath $old) {
  Result 'failed' 'An earlier backup folder could not be removed.'; Launch; exit
}
if (-not (Move-Dir $app $old)) {
  Result 'busy' ('The app folder is in use by another program (an open window, a terminal or antivirus): ' + $script:why)
  Launch; exit
}
if (-not (Move-Dir $new $app)) {
  $why = $script:why
  Move-Dir $old $app | Out-Null
  Result 'failed' ('The new version could not be moved into place: ' + $why); Launch; exit
}
Remove-Item -LiteralPath $ok -Force -ErrorAction SilentlyContinue
$env:LIVE_SUBTITLE_UPDATE_OK = $ok
Launch
$t = (Get-Date).AddSeconds($okWait)
while (-not (Test-Path -LiteralPath $ok) -and (Get-Date) -lt $t) { Start-Sleep -Milliseconds 500 }
if (Test-Path -LiteralPath $ok) {
  Result 'ok' ''   # first: deleting the old 1 GB folder takes a while, the new app reads this meanwhile
  Drop $old
} else {
  Running | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1
  $bad = $app + '.failed'
  Drop $bad
  if ((Move-Dir $app $bad) -and (Move-Dir $old $app)) {
    Result 'rolled_back' 'The new version did not start, so the previous version was restored.'
  } else {
    Result 'failed' 'The new version did not start and the previous version could not be restored.'
  }
  Launch
}
"""


def install_dir() -> Path | None:
    """The folder of the running packaged app, or ``None`` when this is not one (a source checkout, not Windows)."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def sibling(install: Path, suffix: str) -> Path:
    return install.with_name(install.name + suffix)


def can_apply(install: Path) -> str:
    """``""`` when the folder can be swapped; otherwise why not. The folder is renamed, so its parent must be writable."""
    try:
        probe = tempfile.mkdtemp(dir=install.parent, prefix=".write-test-")
    except OSError:
        return f"{install.parent} is not writable, so the app cannot replace itself there."
    shutil.rmtree(probe, ignore_errors=True)
    if not (install / Path(sys.executable).name).is_file():
        return "The app folder does not look like an installed copy."
    return ""


def staged_version(install: Path) -> str:
    """Version waiting in ``<install>.new`` (complete: the marker is written last), or ``""``."""
    try:
        return (sibling(install, ".new") / READY).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def pending(install: Path, current: str) -> str:
    version = staged_version(install)
    return version if update_check.is_newer(version, current) else ""


def cleanup(install: Path, current: str) -> None:
    """At a normal start: remove what an update left behind. Not while the helper is still working."""
    for suffix in (".old", ".failed"):
        shutil.rmtree(sibling(install, suffix), ignore_errors=True)
    if not pending(install, current):
        shutil.rmtree(sibling(install, ".new"), ignore_errors=True)


def take_result(updates: Path) -> dict | None:
    """The helper's verdict from the last update, read once."""
    path = updates / RESULT
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    path.unlink(missing_ok=True)
    return data if isinstance(data, dict) else None


def confirm_started() -> None:
    """Tell a waiting helper that this (new) version is up. Does nothing in a normal start."""
    target = os.environ.get(OK_ENV)
    if target:
        try:
            Path(target).write_text("ok", encoding="utf-8")
        except OSError:
            pass


def _safe_member(name: str, flags: int, target: Path, root: Path) -> bool:
    rel = Path(name.replace("\\", "/"))
    return not (rel.is_absolute() or ".." in rel.parts or ":" in name
                or (flags >> 16) & 0o170000 == 0o120000 or not target.resolve().is_relative_to(root))


def stage(archive: Path, install: Path, version: str, cancel: threading.Event, progress: Progress) -> Path:
    """Unpack the release ZIP into ``<install>.new``; it only counts once the ready marker is there."""
    new = sibling(install, ".new")
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir()
    try:
        with zipfile.ZipFile(archive) as zf:
            items = zf.infolist()
            names = {i.filename.replace("\\", "/") for i in items}
            if "live-subtitle.exe" not in names or not any(n.startswith("_internal/") for n in names):
                raise RuntimeError("The downloaded file is not a Live Subtitle build.")
            total = sum(i.file_size for i in items)
            if total > MAX_UNPACKED or shutil.disk_usage(new).free < total + _SPARE:
                raise RuntimeError("Not enough free disk space to unpack the update.")
            root, done = new.resolve(), 0
            for item in items:
                local_runtime.check_cancel(cancel)
                target = new / item.filename.replace("\\", "/")
                if not _safe_member(item.filename, item.external_attr, target, root):
                    raise RuntimeError("The downloaded file contains an unsafe path.")
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(item) as src, target.open("wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        local_runtime.check_cancel(cancel)
                        dst.write(chunk)
                        done += len(chunk)
                        progress("unpack", done, total)
        if update_check.read_app_version(new / "_internal" / "chrome-extension" / "manifest.json") != version:
            raise RuntimeError("The downloaded build is not the version announced by the release.")
        (new / READY).write_text(version, encoding="utf-8")
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    return new


def download_and_stage(release: update_check.Release, install: Path, updates: Path,
                       cancel: threading.Event, progress: Progress, *, opener=None) -> None:
    """Download (resumable, size and hash checked) and unpack. Raises ``RuntimeError``; ``Cancelled`` on cancel.

    ``opener`` is for tests (a local HTTP server); the real one only follows https redirects.
    """
    asset = release.asset
    if asset is None:
        raise RuntimeError("This release has no package that can be verified.")

    def relay(name: str, done: int, total: int) -> None:
        progress("verify" if name.startswith("Verifying") else "download", done, total)

    with local_runtime.install_lock(updates):
        archive = local_runtime.download_artifact(
            Artifact(asset.name, asset.url, asset.size, asset.sha256), updates, cancel, relay, opener=opener)
        stage(archive, install, release.version, cancel, progress)
        archive.unlink(missing_ok=True)


def clean_env(install: Path, environ: dict | None = None) -> dict:
    """The environment for the helper, and so for the new exe, without what the running exe set up for itself.

    A packaged app carries ``_PYI_ARCHIVE_FILE`` (its own exe path - which the new exe will have too) and
    ``_PYI_PARENT_PROCESS_LEVEL``, which would make the new exe believe it is a child of the old one, plus
    PATH / Qt plugin paths into the install folder.  The new exe must start as if from Explorer.
    """
    env = {k: v for k, v in (os.environ if environ is None else environ).items()
           if not k.upper().startswith(("_PYI", "_MEIPASS"))}
    inside = str(install).lower()
    keys = {k.upper(): k for k in env}
    path_key = keys.get("PATH")
    if path_key:
        env[path_key] = os.pathsep.join(p for p in env[path_key].split(os.pathsep) if not p.lower().startswith(inside))
    for name in ("QML2_IMPORT_PATH", "QT_PLUGIN_PATH"):
        if name in keys and env[keys[name]].lower().startswith(inside):
            del env[keys[name]]
    return env


def apply(install: Path, updates: Path, version: str, *, ok_wait: int = 120, grace: int = 20,
          tries: int = 15, started_wait: float = 15.0) -> tuple[bool, str]:
    """Start the helper. The caller must quit right after a ``True``: the helper waits for this install's exit.

    ``False`` means the helper did not report in; it is told to stand down, so the app can keep running.
    """
    updates.mkdir(parents=True, exist_ok=True)
    for name in (STARTED, CANCEL, RESULT):
        (updates / name).unlink(missing_ok=True)
    env = dict(clean_env(install), LS_UPD_APP=str(install), LS_UPD_NEW=str(sibling(install, ".new")),
               LS_UPD_OLD=str(sibling(install, ".old")), LS_UPD_EXE=Path(sys.executable).name,
               LS_UPD_OK=str(updates / "new-version-ok"), LS_UPD_DIR=str(updates), LS_UPD_VERSION=version,
               LS_UPD_GRACE=str(grace), LS_UPD_TRIES=str(tries), LS_UPD_OKWAIT=str(ok_wait))
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-EncodedCommand", base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")]
    # Not DETACHED_PROCESS: powershell.exe then exits at once without running the script (it needs a console,
    # here a hidden one).
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    # cwd outside the install folder: a process's working directory would block the rename.
    options = dict(env=env, cwd=updates, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, close_fds=True)
    try:
        try:  # leave a job object that would end the helper together with this app, if it allows that
            subprocess.Popen(command, creationflags=flags | 0x01000000, **options)
        except OSError:
            subprocess.Popen(command, creationflags=flags, **options)
    except OSError as exc:
        return False, f"The updater could not be started: {exc}"
    deadline = time.monotonic() + started_wait
    while time.monotonic() < deadline:
        if (updates / STARTED).exists():
            return True, ""
        time.sleep(0.1)
    (updates / CANCEL).write_text("late", encoding="utf-8")
    return False, "The updater did not start in time, so nothing was changed."

"""Keep the unpacked Chrome extension the user loaded up to date.

The packaged app carries the extension's files (``_internal/chrome-extension``).  The user names the folder they
loaded in Chrome once (Settings > Chrome extension); ``sync`` copies the bundled files into it, ``manifest.json``
last.  The extension looks at its own manifest on disk and reloads itself when it names a newer version
(``chrome-extension/shared/files-update.js``), so there is no re-download and no click on chrome://extensions.

Safety: only a folder that already holds this extension (same ``name`` in its manifest) is written to, nothing is
ever deleted, and an extension that is already newer is left alone.  No Qt here.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from gui import update_check

SOURCE = update_check.MANIFEST_PATH.parent  # the bundled copy (the repo's chrome-extension folder in a source checkout)
_SKIP = {"tests", "__pycache__"}  # parts of the source that are not copied


def _manifest(folder: Path) -> dict | None:
    try:
        data = json.loads((Path(folder) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def version_of(folder: Path) -> str:
    """The extension version written in ``folder``, or ``""``."""
    return update_check.read_app_version(Path(folder) / "manifest.json")


def problem(folder: Path, source: Path = SOURCE) -> str:
    """``""`` when ``folder`` holds this extension and can be written to; otherwise why not."""
    folder, source = Path(folder), Path(source)
    ours = _manifest(source)
    if ours is None or not version_of(source):
        return "This build has no extension files to copy."
    if not folder.is_dir():
        return "That folder does not exist."
    theirs = _manifest(folder)
    if theirs is None:
        return "There is no manifest.json there. Choose the folder you loaded in Chrome (Load unpacked)."
    if theirs.get("name") != ours.get("name"):
        return f"That folder holds a different extension ({theirs.get('name') or 'unnamed'})."
    here, bundled = folder.resolve(), source.resolve()
    if here == bundled or here in bundled.parents or bundled in here.parents:
        return ("That is the app's own copy of the extension; every app update replaces it and Chrome's extension "
                "reloads by itself, so there is nothing to copy. Choose another folder only if Chrome loaded one.")
    try:
        handle, probe = tempfile.mkstemp(dir=folder, prefix=".write-test-")
    except OSError as exc:
        return f"Cannot write to that folder: {exc.strerror or exc}."
    os.close(handle)
    Path(probe).unlink(missing_ok=True)
    return ""


def status(folder: Path, source: Path = SOURCE) -> tuple[str, str]:
    """``(state, one line for the card)``; state is ``invalid``, ``outdated``, ``current`` or ``newer``."""
    why = problem(folder, source)
    if why:
        return "invalid", why
    have, bundled = version_of(folder), version_of(source)
    if not have or update_check.is_newer(bundled, have):
        return "outdated", f"The extension in that folder is version {have or 'unknown'}; this app carries {bundled}."
    if update_check.is_newer(have, bundled):
        return "newer", f"The extension in that folder (version {have}) is newer than the one in this app ({bundled}); nothing to do."
    return "current", f"Up to date (version {bundled})."


def sync(folder: Path, source: Path = SOURCE) -> int:
    """Copy the bundled extension into ``folder``; the number of files written (0 when nothing needed copying).

    Every file is written to a temporary name and renamed into place, and ``manifest.json`` goes last: the
    extension reloads itself only when the manifest on disk is newer, so by then the other files are in place.
    Raises ``ValueError`` when ``folder`` is not this extension's folder, ``OSError`` when a write fails (the
    manifest is then still the old one, so the extension keeps running and a retry finishes the job).
    """
    state, text = status(folder, source)
    if state == "invalid":
        raise ValueError(text)
    if state != "outdated":
        return 0
    folder, source = Path(folder), Path(source)
    files = [p for p in source.rglob("*")
             if p.is_file() and not p.is_symlink() and not (set(p.relative_to(source).parts[:-1]) & _SKIP)]
    files.sort(key=lambda p: (p.parent == source and p.name == "manifest.json", p.as_posix()))
    written = 0
    for path in files:
        target = folder / path.relative_to(source)
        data = path.read_bytes()
        if target.is_file() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(data)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        written += 1
    return written

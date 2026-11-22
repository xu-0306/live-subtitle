"""The one-click update: staging a release ZIP and the helper that swaps the install folders.

The helper tests run a real hidden PowerShell against a fake install whose "live-subtitle.exe" is a tiny
Windows program built with gcc (skipped when there is no gcc): it records every start, reports in when
its mode says so, and otherwise sleeps - so waiting for a running exe, a locked folder and the roll-back
are exercised for real, not mocked.
"""
from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

from backend import local_runtime
from gui import self_update, update_check

windows = pytest.mark.skipif(sys.platform != "win32", reason="the helper is a Windows PowerShell script")

_FAKE_C = r"""
#include <windows.h>
#include <stdio.h>
#include <string.h>
int WINAPI WinMain(HINSTANCE a, HINSTANCE b, LPSTR c, int d) {
    char dir[MAX_PATH], file[MAX_PATH], mode[16] = {0}, ok[MAX_PATH];
    GetModuleFileNameA(NULL, dir, MAX_PATH);
    *strrchr(dir, '\\') = 0;
    snprintf(file, sizeof file, "%s\\mode.txt", dir);
    FILE *f = fopen(file, "r"); if (f) { fgets(mode, sizeof mode, f); fclose(f); }
    snprintf(file, sizeof file, "%s\\started.txt", dir);
    f = fopen(file, "a"); if (f) { fputs("x", f); fclose(f); }
    DWORD n = GetEnvironmentVariableA("LIVE_SUBTITLE_UPDATE_OK", ok, MAX_PATH);
    if (n > 0 && n < MAX_PATH && !strncmp(mode, "ok", 2)) {
        f = fopen(ok, "w"); if (f) { fputs("ok", f); fclose(f); }
    }
    if (!strncmp(mode, "exit", 4)) return 0;
    Sleep(120000);
    return 0;
}
"""


@pytest.fixture(scope="session")
def fake_exe(tmp_path_factory):
    gcc = shutil.which("gcc")
    if sys.platform != "win32" or not gcc:
        pytest.skip("needs Windows and gcc")
    folder = tmp_path_factory.mktemp("fake-exe")
    (folder / "fake.c").write_text(_FAKE_C)
    out = folder / "live-subtitle.exe"
    subprocess.run([gcc, "-mwindows", "-O1", str(folder / "fake.c"), "-o", str(out)], check=True)
    return out


def _kill_under(root: Path) -> None:
    script = ("Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith('%s\\') } | "
              "Stop-Process -Force" % root)
    subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True)


def _wait(condition, seconds=60.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def _app(folder: Path, fake_exe: Path, marker: str, mode: str) -> None:
    (folder / "_internal").mkdir(parents=True)
    shutil.copy(fake_exe, folder / "live-subtitle.exe")
    (folder / "mode.txt").write_text(mode)
    (folder / "_internal" / "marker.txt").write_text(marker)


class Scene:
    def __init__(self, root: Path, fake_exe: Path, new_mode: str) -> None:
        self.install = root / "apps" / "Live Subtitle"
        self.updates = root / "data" / "updates"
        _app(self.install, fake_exe, "old", "ok")
        _app(self.install.with_name("Live Subtitle.new"), fake_exe, "new", new_mode)
        (self.install.with_name("Live Subtitle.new") / self_update.READY).write_text("1.0.1")

    def marker(self, folder: Path | None = None) -> str:
        return ((folder or self.install) / "_internal" / "marker.txt").read_text()

    def starts(self, expected: int, seconds: float = 10.0) -> int:
        """How many times the exe in the install folder was started (it logs each start), after waiting for ``expected``."""
        def count() -> int:
            try:
                return len((self.install / "started.txt").read_text())
            except OSError:
                return 0
        _wait(lambda: count() >= expected, seconds)
        return count()

    def run(self, *, ok_wait=20, tries=3, hold: Path | None = None, grace=10, stubborn=False) -> dict:
        """Start the old "GUI", ask for the update, let the GUI exit, return the helper's verdict."""
        gui = subprocess.Popen([str(self.install / "live-subtitle.exe")])
        # a second process of the same install that never quits, like a backend that missed its shutdown
        self.leftover = subprocess.Popen([str(self.install / "live-subtitle.exe")]) if stubborn else None
        started, message = self_update.apply(self.install, self.updates, "1.0.1",
                                             ok_wait=ok_wait, grace=grace, tries=tries)
        assert started, message
        handle = hold.open("w") if hold else None
        try:
            assert gui.poll() is None  # the helper must not touch a running app
            gui.terminate()
            gui.wait(10)
            assert _wait(lambda: (self.updates / self_update.RESULT).exists()), "the helper never finished"
        finally:
            if handle:
                handle.close()
        return self_update.take_result(self.updates)


@pytest.fixture
def scene(request, tmp_path, fake_exe, monkeypatch):
    built = Scene(tmp_path, fake_exe, getattr(request, "param", "ok"))
    monkeypatch.setattr(sys, "executable", str(built.install / "live-subtitle.exe"))
    request.addfinalizer(lambda: _kill_under(tmp_path))
    return built


@windows
def test_the_helper_waits_for_the_app_then_swaps_folders_and_starts_the_new_version(scene):
    verdict = scene.run()
    assert verdict["status"] == "ok" and verdict["version"] == "1.0.1"
    assert scene.marker() == "new"
    assert not scene.install.with_name("Live Subtitle.old").exists()
    assert not scene.install.with_name("Live Subtitle.new").exists()
    assert scene.starts(1) == 1  # the new version was started once


@windows
def test_a_leftover_process_of_the_old_install_is_ended_after_the_grace_period(scene):
    verdict = scene.run(grace=2, stubborn=True)
    assert verdict["status"] == "ok" and scene.marker() == "new"
    assert scene.leftover.poll() is not None  # it would have blocked the rename


@windows
@pytest.mark.parametrize("scene", ["hang"], indirect=True)
def test_a_new_version_that_never_reports_in_is_rolled_back(scene):
    verdict = scene.run(ok_wait=4)
    assert verdict["status"] == "rolled_back"
    assert scene.marker() == "old"
    assert scene.marker(scene.install.with_name("Live Subtitle.failed")) == "new"
    assert not scene.install.with_name("Live Subtitle.old").exists()
    assert scene.starts(2) == 2  # the old version's own start plus the restart after the roll-back


@windows
def test_a_locked_folder_leaves_the_old_version_in_place_and_says_busy(scene):
    verdict = scene.run(tries=2, hold=scene.install / "_internal" / "locked.txt")
    assert verdict["status"] == "busy"
    assert scene.marker() == "old"
    # a refused rename must change nothing: no half-moved install, no stray backup folder
    assert not scene.install.with_name("Live Subtitle.old").exists()
    assert {p.name for p in scene.install.iterdir()} == {"_internal", "live-subtitle.exe", "mode.txt", "started.txt"}
    assert scene.install.with_name("Live Subtitle.new").exists()  # the staged update is kept for a retry
    assert scene.starts(2) == 2  # restarted, so the user is not left without the app


@windows
def test_apply_stands_down_when_the_helper_never_starts(scene, monkeypatch):
    with monkeypatch.context() as patch:  # undone before the fixture's cleanup, which needs the real Popen
        patch.setattr(self_update.subprocess, "Popen", lambda *a, **k: None)
        started, message = self_update.apply(scene.install, scene.updates, "1.0.1", started_wait=0.3)
    assert not started and "did not start" in message
    assert (scene.updates / self_update.CANCEL).exists()  # a late helper would see this and leave


def _zip(path: Path, version="1.0.1", extra=None, skip=()):
    files = {"live-subtitle.exe": b"MZ", "_internal/lib.dll": b"x" * 5000,
             "_internal/chrome-extension/manifest.json": json.dumps({"version": version}), **(extra or {})}
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            if name not in skip:
                zf.writestr(name, data)
    return path


def _stage(tmp_path, archive, version="1.0.1", cancel=None):
    install = tmp_path / "apps" / "Live Subtitle"
    install.mkdir(parents=True)
    events = []
    new = self_update.stage(archive, install, version, cancel or threading.Event(),
                            lambda *event: events.append(event))
    return install, new, events


def test_stage_unpacks_the_build_and_marks_it_ready_last(tmp_path):
    install, new, events = _stage(tmp_path, _zip(tmp_path / "a.zip"))
    assert new == install.with_name("Live Subtitle.new")
    assert (new / "live-subtitle.exe").read_bytes() == b"MZ"
    assert len((new / "_internal" / "lib.dll").read_bytes()) == 5000
    assert self_update.staged_version(install) == "1.0.1"
    assert events and events[-1][0] == "unpack" and events[-1][1] == events[-1][2]


@pytest.mark.parametrize("kind", ["not-a-build", "wrong-version", "traversal", "absolute"])
def test_stage_refuses_a_package_that_is_not_the_announced_build(tmp_path, kind):
    archive = tmp_path / "bad.zip"
    if kind == "not-a-build":
        _zip(archive, skip=("live-subtitle.exe",))
    elif kind == "wrong-version":
        _zip(archive, version="0.0.9")
    elif kind == "traversal":
        _zip(archive, extra={"../evil.txt": "x"})
    else:
        _zip(archive, extra={"C:/evil.txt": "x"})
    install = tmp_path / "apps" / "Live Subtitle"
    install.mkdir(parents=True)
    with pytest.raises(RuntimeError):
        self_update.stage(archive, install, "1.0.1", threading.Event(), lambda *e: None)
    assert not install.with_name("Live Subtitle.new").exists()  # nothing half-made stays behind
    assert not (tmp_path / "apps" / "evil.txt").exists() and not (tmp_path / "evil.txt").exists()


def test_stage_can_be_cancelled_and_leaves_nothing(tmp_path):
    cancel = threading.Event()
    cancel.set()
    install = tmp_path / "apps" / "Live Subtitle"
    install.mkdir(parents=True)
    with pytest.raises(local_runtime.Cancelled):
        self_update.stage(_zip(tmp_path / "a.zip"), install, "1.0.1", cancel, lambda *e: None)
    assert not install.with_name("Live Subtitle.new").exists()


def test_stage_refuses_when_the_disk_is_too_small(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10, 9, 1))
    install = tmp_path / "apps" / "Live Subtitle"
    install.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="free disk space"):
        self_update.stage(_zip(tmp_path / "a.zip"), install, "1.0.1", threading.Event(), lambda *e: None)


@pytest.fixture
def server(tmp_path):
    folder = tmp_path / "served"
    folder.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(folder))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield folder, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _release(url: str, archive: Path, sha: str | None = None) -> update_check.Release:
    asset = update_check.Asset(archive.name, url, archive.stat().st_size,
                               sha or hashlib.sha256(archive.read_bytes()).hexdigest())
    return update_check.Release("1.0.1", "v1.0.1", "https://github.com/x/y", "", "", asset)


def _open(request, timeout=5):
    return urllib.request.urlopen(request, timeout=timeout)


def test_download_verifies_stages_and_removes_the_zip(tmp_path, server):
    folder, base = server
    archive = _zip(folder / "Live-Subtitle-windows-x64.zip")
    install = tmp_path / "apps" / "Live Subtitle"
    install.mkdir(parents=True)
    updates = tmp_path / "data" / "updates"
    phases = []
    self_update.download_and_stage(_release(f"{base}/{archive.name}", archive), install, updates,
                                   threading.Event(), lambda phase, *_: phases.append(phase), opener=_open)
    assert self_update.pending(install, "1.0.0") == "1.0.1"
    assert self_update.pending(install, "1.0.1") == ""  # nothing newer than this build
    assert {"download", "verify", "unpack"} <= set(phases)
    assert not (updates / archive.name).exists()


def test_a_download_that_does_not_match_the_published_hash_is_refused(tmp_path, server):
    folder, base = server
    archive = _zip(folder / "Live-Subtitle-windows-x64.zip")
    install = tmp_path / "apps" / "Live Subtitle"
    install.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="checksum"):
        self_update.download_and_stage(_release(f"{base}/{archive.name}", archive, sha="0" * 64), install,
                                       tmp_path / "updates", threading.Event(), lambda *e: None, opener=_open)
    assert not install.with_name("Live Subtitle.new").exists()


def test_a_release_without_a_verifiable_package_cannot_be_installed(tmp_path):
    release = update_check.Release("1.0.1", "v1.0.1", "https://github.com/x/y", "", "")
    with pytest.raises(RuntimeError, match="verified"):
        self_update.download_and_stage(release, tmp_path, tmp_path, threading.Event(), lambda *e: None)


def test_cleanup_removes_leftovers_but_keeps_a_newer_staged_update(tmp_path):
    install = tmp_path / "Live Subtitle"
    for suffix in (".old", ".failed"):
        install.with_name(install.name + suffix).mkdir()
    new = install.with_name(install.name + ".new")
    new.mkdir()
    (new / self_update.READY).write_text("2.0.0")
    self_update.cleanup(install, "1.0.0")
    assert not install.with_name(install.name + ".old").exists()
    assert not install.with_name(install.name + ".failed").exists()
    assert new.exists()
    self_update.cleanup(install, "2.0.0")  # now it is no longer an update
    assert not new.exists()


def test_a_half_unpacked_staging_folder_is_not_pending_and_is_cleaned(tmp_path):
    install = tmp_path / "Live Subtitle"
    new = install.with_name(install.name + ".new")
    new.mkdir()
    assert self_update.pending(install, "1.0.0") == ""
    self_update.cleanup(install, "1.0.0")
    assert not new.exists()


def test_take_result_reads_once_and_tolerates_garbage(tmp_path):
    path = tmp_path / self_update.RESULT
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"status": "ok", "version": "1.0.1"}).encode())
    assert self_update.take_result(tmp_path) == {"status": "ok", "version": "1.0.1"}
    assert self_update.take_result(tmp_path) is None
    path.write_text("not json")
    assert self_update.take_result(tmp_path) is None


def test_confirm_started_only_acts_when_the_helper_asked(tmp_path, monkeypatch):
    target = tmp_path / "ok"
    monkeypatch.delenv(self_update.OK_ENV, raising=False)
    self_update.confirm_started()
    assert not target.exists()
    monkeypatch.setenv(self_update.OK_ENV, str(target))
    self_update.confirm_started()
    assert target.read_text() == "ok"


def test_can_apply_needs_a_writable_parent_and_an_installed_exe(tmp_path, monkeypatch):
    install = tmp_path / "Live Subtitle"
    install.mkdir()
    monkeypatch.setattr(sys, "executable", str(install / "live-subtitle.exe"))
    assert "installed copy" in self_update.can_apply(install)
    (install / "live-subtitle.exe").write_bytes(b"MZ")
    assert self_update.can_apply(install) == ""
    assert not list(tmp_path.glob(".write-test-*"))

    def refuse(**kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(self_update.tempfile, "mkdtemp", refuse)
    assert "not writable" in self_update.can_apply(install)


def test_the_new_exe_does_not_inherit_what_the_old_exe_set_up_for_itself(tmp_path):
    install = tmp_path / "Live Subtitle"
    environ = {
        "_PYI_ARCHIVE_FILE": str(install / "live-subtitle.exe"), "_PYI_PARENT_PROCESS_LEVEL": "1",
        "PATH": os.pathsep.join([str(install / "_internal" / "PySide6"), str(install / "_internal"), r"C:\Windows"]),
        "QT_PLUGIN_PATH": str(install / "_internal" / "PySide6" / "plugins"),
        "QML2_IMPORT_PATH": str(tmp_path / "somewhere" / "else"), "APPDATA": r"C:\Users\x\AppData\Roaming",
    }
    cleaned = self_update.clean_env(install, environ)
    assert cleaned == {"PATH": r"C:\Windows", "QML2_IMPORT_PATH": str(tmp_path / "somewhere" / "else"),
                       "APPDATA": r"C:\Users\x\AppData\Roaming"}
    assert environ["PATH"].count(os.pathsep) == 2  # the caller's mapping is not modified


def test_a_source_checkout_is_not_a_packaged_install():
    assert self_update.install_dir() is None

"""Chrome extension folder sync: copies the bundled files into the folder Chrome loaded, manifest last, safely."""
import json
import os
import shutil
from pathlib import Path

import pytest
from PySide6 import QtWidgets

from gui import config_manager, extension_sync, update_check
from gui.app import MainWindow

NAME = "Live Subtitle"


def _extension(folder: Path, version: str, files: dict | None = None, name: str = NAME) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for rel, text in (files or {}).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, encoding="utf-8")
    (folder / "manifest.json").write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")
    return folder


@pytest.fixture
def source(tmp_path):
    return _extension(tmp_path / "bundled", "0.6.0", {
        "background/background.js": "new background", "shared/files-update.js": "new file", "tests/skip.test.js": "x"})


def _tree(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_status_tells_outdated_current_newer_and_invalid_apart(tmp_path, source):
    assert extension_sync.status(_extension(tmp_path / "a", "0.5.1"), source)[0] == "outdated"
    assert extension_sync.status(_extension(tmp_path / "b", "0.6.0"), source) == ("current", "Up to date (version 0.6.0).")
    assert extension_sync.status(_extension(tmp_path / "c", "0.7.0"), source)[0] == "newer"
    assert extension_sync.status(_extension(tmp_path / "d", "0.5.1", name="Other"), source)[0] == "invalid"
    (tmp_path / "empty").mkdir()
    assert extension_sync.status(tmp_path / "empty", source)[0] == "invalid"
    assert extension_sync.status(tmp_path / "missing", source)[0] == "invalid"


def test_the_apps_own_copy_and_its_parents_are_not_a_target(tmp_path, source):
    assert "own copy" in extension_sync.problem(source, source)
    _extension(tmp_path, "0.1.0")  # a parent that holds a manifest too: copying into it would overwrite the bundled copy
    assert "own copy" in extension_sync.problem(tmp_path, source)
    assert "own copy" in extension_sync.problem(_extension(source / "nested", "0.1.0"), source)


def test_sync_copies_everything_but_tests_and_deletes_nothing(tmp_path, source):
    target = _extension(tmp_path / "chrome", "0.5.1", {"background/background.js": "old", "my-notes.txt": "keep me"})
    written = extension_sync.sync(target, source)
    assert written == 3  # background.js, files-update.js, manifest.json
    tree = _tree(target)
    assert tree["background/background.js"] == b"new background"
    assert tree["shared/files-update.js"] == b"new file"
    assert tree["my-notes.txt"] == b"keep me"
    assert "tests/skip.test.js" not in tree
    assert extension_sync.version_of(target) == "0.6.0"
    assert extension_sync.status(target, source)[0] == "current"
    assert not [p for p in target.rglob("*") if p.name.startswith((".tmp-", ".write-test-"))]


def test_the_manifest_is_written_last(tmp_path, source, monkeypatch):
    target = _extension(tmp_path / "chrome", "0.5.1")
    order = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append(Path(b).name), real(a, b))[1])
    extension_sync.sync(target, source)
    assert order[-1] == "manifest.json" and order.count("manifest.json") == 1


def test_a_failed_copy_leaves_the_old_manifest_so_a_retry_finishes_the_job(tmp_path, source, monkeypatch):
    target = _extension(tmp_path / "chrome", "0.5.1")
    real = os.replace

    def broken(a, b):
        if Path(b).name == "files-update.js":
            raise PermissionError(13, "locked")
        return real(a, b)

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        extension_sync.sync(target, source)
    assert extension_sync.version_of(target) == "0.5.1"  # the extension keeps running as before
    assert not [p for p in target.rglob("*") if p.name.startswith(".tmp-")]
    monkeypatch.setattr(os, "replace", real)
    assert extension_sync.sync(target, source) >= 2
    assert extension_sync.version_of(target) == "0.6.0"


def test_nothing_is_written_when_the_folder_is_current_newer_or_not_the_extension(tmp_path, source):
    for name, version, ext_name in (("cur", "0.6.0", NAME), ("new", "0.9.0", NAME), ("odd", "0.1.0", "Other")):
        target = _extension(tmp_path / name, version, name=ext_name)
        before = _tree(target)
        if name == "odd":
            with pytest.raises(ValueError):
                extension_sync.sync(target, source)
        else:
            assert extension_sync.sync(target, source) == 0
        assert _tree(target) == before


def test_the_real_bundled_extension_syncs_into_an_older_copy_byte_for_byte(tmp_path):
    target = tmp_path / "chrome"
    shutil.copytree(extension_sync.SOURCE, target, ignore=shutil.ignore_patterns("tests"))
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = "0.0.1"
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (target / "shared" / "files-update.js").write_text("stale", encoding="utf-8")
    assert extension_sync.status(target)[0] == "outdated"
    extension_sync.sync(target)
    expected = {k: v for k, v in _tree(extension_sync.SOURCE).items() if not k.startswith("tests/")}
    assert _tree(target) == expected


# --- the Settings card ---

@pytest.fixture
def window(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    path = tmp_path / "config.yaml"
    cfg = config_manager.load_default_config()
    cfg["models"] = {"root": str(tmp_path / "models")}
    config_manager.write_config(path, cfg)
    win = MainWindow(path, auto_start=False, setup_tray=False)
    win.notices = []
    win._show_notice = lambda message, kind="info": win.notices.append((message, kind))
    yield win
    win.backend_runner = None
    win._load_config_into_ui(win.config)
    win.close()
    app.processEvents()


def _older_copy(tmp_path) -> Path:
    target = tmp_path / "chrome"
    shutil.copytree(extension_sync.SOURCE, target, ignore=shutil.ignore_patterns("tests"))
    (target / "manifest.json").write_text(
        json.dumps({"name": json.loads(update_check.MANIFEST_PATH.read_text(encoding="utf-8"))["name"], "version": "0.0.1"}),
        encoding="utf-8")
    return target


def _pick(monkeypatch, folder):
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))


def test_the_card_starts_empty_and_touches_nothing(window, tmp_path):
    assert window.ext_folder_input.text() == ""
    assert not window.ext_sync_btn.isEnabled()
    assert window.ext_auto_check.isChecked()
    window._extension_housekeeping()
    assert window.notices == []
    assert not (tmp_path / "extension-sync.json").exists()


def test_choosing_a_folder_is_remembered_beside_the_settings_and_updates_on_click(window, tmp_path, monkeypatch):
    target = _older_copy(tmp_path)
    _pick(monkeypatch, target)
    window._choose_extension_folder()
    saved = json.loads((tmp_path / "extension-sync.json").read_text(encoding="utf-8"))
    assert saved["folder"] == str(target) and saved.get("auto") is not False
    assert window.ext_sync_btn.isEnabled()
    assert "0.0.1" in window.ext_status.text()
    window.ext_sync_btn.click()
    assert extension_sync.version_of(target) == update_check.read_app_version()
    assert not window.ext_sync_btn.isEnabled()
    assert window.ext_status.text().startswith("Up to date")
    assert window.notices and window.notices[-1][1] == "info"


def test_a_folder_that_is_not_the_extension_is_refused(window, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    _pick(monkeypatch, other)
    window._choose_extension_folder()
    assert window.notices[-1][1] == "warning"
    assert window.ext_folder_input.text() == ""
    assert not (tmp_path / "extension-sync.json").exists()


def test_start_up_updates_the_folder_only_while_automatic_updates_are_on(window, tmp_path, monkeypatch):
    target = _older_copy(tmp_path)
    _pick(monkeypatch, target)
    window._choose_extension_folder()
    window.ext_auto_check.setChecked(False)
    assert json.loads((tmp_path / "extension-sync.json").read_text(encoding="utf-8"))["auto"] is False
    window._extension_housekeeping()
    assert extension_sync.version_of(target) == "0.0.1"
    window.ext_auto_check.setChecked(True)
    window._extension_housekeeping()
    assert extension_sync.version_of(target) == update_check.read_app_version()


def test_a_failed_copy_is_reported_and_the_old_version_stays(window, tmp_path, monkeypatch):
    target = _older_copy(tmp_path)
    _pick(monkeypatch, target)
    window._choose_extension_folder()
    monkeypatch.setattr(extension_sync, "sync", lambda folder: (_ for _ in ()).throw(PermissionError(13, "locked")))
    window._sync_extension(manual=True)
    assert window.notices[-1][1] == "warning" and "locked" in window.notices[-1][0]
    assert extension_sync.version_of(target) == "0.0.1"

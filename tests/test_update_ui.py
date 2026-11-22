"""Update check in the desktop window: prompts only for a new version, stays quiet otherwise, survives offline."""
import dataclasses
import json
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from qfluentwidgets import InfoBar

from gui import config_manager, self_update, update_check, update_dialog
from gui.app import MainWindow

RELEASE = update_check.Release(
    "99.0.0", "v99.0.0", "https://github.com/xu-0306/live-subtitle/releases/tag/v99.0.0",
    "# What's new\n\n- faster\n- [a link](https://example.com/phish)", "2026-12-01T00:00:00Z")
NEW = update_check.Result("new", RELEASE)
CURRENT = update_check.Result("current", RELEASE)
OFFLINE = update_check.Result("error", error="Could not reach GitHub.")


def _make(tmp_path, **kwargs):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    path = tmp_path / "config.yaml"
    if not path.exists():
        cfg = config_manager.load_default_config()
        cfg["models"] = {"root": str(tmp_path / "models")}
        config_manager.write_config(path, cfg)
    return MainWindow(path, auto_start=False, setup_tray=False, **kwargs)


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = _make(tmp_path)
    win.notices = []
    monkeypatch.setattr(win, "_show_notice", lambda message, kind="info": win.notices.append((message, kind)))
    yield win
    win.backend_runner = None
    win._load_config_into_ui(win.config)
    win.close()
    app.processEvents()


class _Dialogs(list):
    """Stands in for the modal dialog: records (version, current) and answers with ``answer``."""

    answer = "later"


@pytest.fixture
def dialogs(monkeypatch):
    shown = _Dialogs()

    def fake(parent, release, current):
        shown.append((release.version, current))
        return shown.answer

    monkeypatch.setattr(update_dialog, "show_update_dialog", fake)
    return shown


@pytest.fixture
def opened(monkeypatch):
    urls = []
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", lambda url: urls.append(url.toString()) or True)
    return urls


def _wait_until(condition, timeout_ms=3000):
    deadline = time.monotonic() + timeout_ms / 1000
    while not condition() and time.monotonic() < deadline:
        QtTest.QTest.qWait(10)
    return bool(condition())


def test_about_card_shows_the_version_and_the_update_controls(window):
    manifest = json.loads(update_check.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert window.version_label.text() == f"Version {manifest['version']}"
    assert window._app_version == manifest["version"]
    assert window.update_auto_check.isChecked()
    assert window.update_check_btn.isEnabled()
    assert window.update_status.text() == "Not checked yet."


def test_the_remembered_answer_sits_beside_the_settings_file_and_never_in_it(window, tmp_path):
    before = (tmp_path / "config.yaml").read_bytes()
    window._on_update_result(NEW, False)
    assert window._update_state_path == tmp_path / "update-check.json"
    state = update_check.load_state(window._update_state_path)
    assert state["ok"] is True and state["latest"] == "99.0.0"
    assert (tmp_path / "config.yaml").read_bytes() == before, "a check must not touch config.yaml (revision, Save all)"


def test_manual_check_with_a_new_version_opens_the_dialog_and_the_download_page(window, dialogs, opened):
    dialogs.answer = "open"
    window._on_update_result(NEW, True)
    assert dialogs == [("99.0.0", window._app_version)]
    assert opened == [RELEASE.url]
    assert "skipped" not in window._update_state
    assert "version 99.0.0 is available" in window.update_status.text()


def test_skip_is_remembered_and_silences_only_the_automatic_prompt(window, dialogs, opened):
    dialogs.answer = "skip"
    window._on_update_result(NEW, True)
    assert opened == []
    assert update_check.load_state(window._update_state_path)["skipped"] == "99.0.0"
    assert window.notices[-1][0].startswith("Version 99.0.0 skipped")
    window._on_update_result(NEW, False)
    assert window.findChildren(InfoBar) == [], "the start-up check stays quiet about a skipped version"
    window._on_update_result(NEW, True)
    assert len(dialogs) == 2, "the button always speaks"


def test_later_changes_nothing(window, dialogs, opened):
    window._on_update_result(NEW, True)
    assert opened == [] and "skipped" not in window._update_state


def test_manual_check_without_news_says_so_and_opens_nothing(window, dialogs):
    window._on_update_result(CURRENT, True)
    assert dialogs == []
    assert window.notices == [(f"You are on the latest version ({window._app_version}).", "info")]


def test_manual_check_offline_shows_a_warning_not_an_exception(window, dialogs):
    window._on_update_result(OFFLINE, True)
    assert dialogs == []
    assert window.notices == [("Could not check for updates: Could not reach GitHub.", "warning")]
    assert "could not reach GitHub" in window.update_status.text()
    assert window.update_check_btn.isEnabled()


def test_startup_check_prompts_with_a_bar_not_a_modal_dialog(window, dialogs):
    window._on_update_result(NEW, False)
    (bar,) = window.findChildren(InfoBar)
    assert bar.titleLabel.text() == "Update available"
    assert "99.0.0" in bar.contentLabel.text()
    assert dialogs == [], "nothing modal appears by itself"
    assert window.notices == []


def test_startup_check_is_silent_when_there_is_nothing_new_or_the_network_is_down(window, dialogs):
    window._on_update_result(CURRENT, False)
    window._on_update_result(OFFLINE, False)
    assert window.findChildren(InfoBar) == [] and dialogs == [] and window.notices == []
    assert window._update_state["ok"] is False


def test_the_bar_leads_to_the_dialog(window, dialogs, opened):
    dialogs.answer = "open"
    window._on_update_result(NEW, False)
    (bar,) = window.findChildren(InfoBar)
    next(b for b in bar.findChildren(QtWidgets.QPushButton) if b.text() == "What's new").click()
    assert opened == [RELEASE.url]


def test_the_lookup_runs_off_the_gui_thread_and_a_second_click_is_ignored(window, monkeypatch, dialogs):
    gate, seen = threading.Event(), []

    def slow_check(current):
        seen.append(threading.current_thread() is threading.main_thread())
        gate.wait(3)
        return NEW

    monkeypatch.setattr(update_check, "check", slow_check)
    window.update_check_btn.click()
    assert _wait_until(lambda: seen), "the worker never ran"
    assert not window.update_check_btn.isEnabled() and window.update_status.text() == "Checking…"
    window._start_update_check(manual=True)  # the guard: one lookup at a time
    gate.set()
    assert _wait_until(lambda: dialogs), "the result never reached the window"
    assert seen == [False], "the lookup ran on a worker thread, and only once"
    assert window.update_check_btn.isEnabled() and not window._update_check_running


def test_offline_end_to_end_through_the_real_thread(window, monkeypatch, dialogs):
    real_check = update_check.check

    def unreachable():
        raise update_check.UpdateCheckError("Could not reach GitHub.")

    monkeypatch.setattr(update_check, "check", lambda current: real_check(current, fetch=unreachable))
    window._start_update_check(manual=False)
    assert _wait_until(lambda: not window._update_check_running)
    assert window.findChildren(InfoBar) == [] and dialogs == [] and window.notices == []
    assert update_check.load_state(window._update_state_path)["ok"] is False


def test_switching_it_off_is_remembered_across_windows(tmp_path, window):
    window.update_auto_check.setChecked(False)
    assert update_check.load_state(tmp_path / "update-check.json")["enabled"] is False
    reopened = _make(tmp_path)
    try:
        assert not reopened.update_auto_check.isChecked()
        assert not update_check.is_due(reopened._update_state)
    finally:
        reopened.close()


@pytest.fixture
def scheduled(monkeypatch):
    """Records the start-up timer without waiting 5 s; every other singleShot still runs."""
    delays, real = [], QtCore.QTimer.singleShot

    def spy(ms, fn, *rest):
        if getattr(fn, "__name__", "") == "_start_update_check":
            delays.append(ms)
            return None
        return real(ms, fn, *rest)

    monkeypatch.setattr(QtCore.QTimer, "singleShot", staticmethod(spy))
    return delays


@pytest.mark.parametrize("kwargs, state, expected", [
    ({"check_updates": True}, {}, True),
    ({"check_updates": True}, {"checked_at": "recent", "ok": True}, True),  # unreadable timestamp: ask again
    ({"check_updates": True}, {"enabled": False}, False),
    ({"check_updates": False}, {}, False),
    ({}, {}, False),  # an explicit config path (tests, tools) never asks the network by itself
])
def test_startup_check_is_scheduled_only_when_enabled_and_due(tmp_path, scheduled, kwargs, state, expected):
    (tmp_path / "update-check.json").write_text(json.dumps(state), encoding="utf-8")
    win = _make(tmp_path, **kwargs)
    try:
        assert (scheduled == [5000]) is expected, scheduled
    finally:
        win.close()


@pytest.mark.parametrize("age, expected", [(60, False), (90000, True)])
def test_startup_check_is_throttled_to_once_a_day(tmp_path, scheduled, age, expected):
    state = {"checked_at": time.time() - age, "ok": True}
    (tmp_path / "update-check.json").write_text(json.dumps(state), encoding="utf-8")
    win = _make(tmp_path, check_updates=True)
    try:
        assert (scheduled == [5000]) is expected
    finally:
        win.close()


def test_the_dialog_shows_version_and_notes_and_maps_each_button(window):
    dialog = update_dialog.UpdateDialog(window, RELEASE, "0.4.1")
    assert "99.0.0" in dialog.viewLayout.itemAt(1).widget().text()
    assert "faster" in dialog.notes.toPlainText()
    assert dialog.notes.openLinks() is False and dialog.notes.openExternalLinks() is False
    assert list(dialog.buttons) == ["Open download page", "Skip this version", "Later"]
    for label, expected in [("Open download page", "open"), ("Skip this version", "skip"), ("Later", "later")]:
        dialog = update_dialog.UpdateDialog(window, RELEASE, "0.4.1")
        dialog.buttons[label].click()
        assert dialog.choice == expected


def test_closing_the_dialog_with_escape_means_later(window):
    dialog = update_dialog.UpdateDialog(window, RELEASE, "0.4.1")
    dialog.reject()
    assert dialog.choice == "later"


def test_a_release_without_notes_still_gets_a_dialog(window):
    bare = update_check.Release("99.0.0", "v99.0.0", update_check.RELEASES_PAGE, "", "")
    dialog = update_dialog.UpdateDialog(window, bare, "")
    assert not hasattr(dialog, "notes") and "unknown version" in dialog.viewLayout.itemAt(1).widget().text()


# --- one-click update: "Update now", the restart prompt and the helper hand-over ---

ASSET = update_check.Asset(
    "Live-Subtitle-windows-x64.zip",
    "https://github.com/xu-0306/live-subtitle/releases/download/v99.0.0/Live-Subtitle-windows-x64.zip",
    450 * 2**20, "a" * 64)
PACKAGED_RELEASE = dataclasses.replace(RELEASE, asset=ASSET)


def test_the_dialog_offers_update_now_only_for_a_packaged_install_with_a_verified_zip(window, tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "install_dir", lambda: tmp_path)
    dialog = update_dialog.UpdateDialog(window, PACKAGED_RELEASE, "0.5.1")
    assert list(dialog.buttons) == ["Update now", "Open download page", "Skip this version", "Later"]
    assert any("450 MB" in label.text() for label in dialog.findChildren(QtWidgets.QLabel))
    dialog.buttons["Update now"].click()
    assert dialog.choice == "update"
    assert "Update now" not in update_dialog.UpdateDialog(window, RELEASE, "0.5.1").buttons  # no verified ZIP
    monkeypatch.setattr(self_update, "install_dir", lambda: None)  # a source checkout
    assert "Update now" not in update_dialog.UpdateDialog(window, PACKAGED_RELEASE, "0.5.1").buttons


@pytest.fixture
def packaged(window, tmp_path, monkeypatch):
    """A window that believes it is a packaged install; download, helper and prompt are recorded, not run."""
    install = tmp_path / "apps" / "Live Subtitle"
    install.mkdir(parents=True)
    window._self_update_install, window._updates_dir = install, tmp_path / "updates"
    monkeypatch.delenv(self_update.OK_ENV, raising=False)
    calls = SimpleNamespace(install=install, stage=[], apply=[], restart=True, apply_result=(True, ""), reason="")

    def fake_stage(release, inst, updates, cancel, progress, **kwargs):
        progress("download", 10 * 2**20, 100 * 2**20)
        progress("unpack", 100 * 2**20, 100 * 2**20)
        calls.stage.append((release.version, inst))

    def fake_apply(inst, updates, version, **kwargs):
        calls.apply.append((inst, version))
        return calls.apply_result

    monkeypatch.setattr(self_update, "can_apply", lambda inst: calls.reason)
    monkeypatch.setattr(self_update, "download_and_stage", fake_stage)
    monkeypatch.setattr(self_update, "apply", fake_apply)
    monkeypatch.setattr(update_dialog, "show_restart_dialog", lambda parent, version: calls.restart)
    return calls


def test_update_now_downloads_asks_to_restart_and_hands_over_to_the_helper_on_close(window, packaged, dialogs):
    dialogs.answer = "update"
    window._show_update_dialog(PACKAGED_RELEASE)
    assert _wait_until(lambda: packaged.apply), "the helper was never started"
    assert packaged.stage == [("99.0.0", packaged.install)]
    assert packaged.apply == [(packaged.install, "99.0.0")]
    assert window._update_download_running is False and window._update_progress_bar is None


def test_progress_text_follows_the_phase(window):
    bar = InfoBar.info("Updating Live Subtitle", "Downloading… 0%", parent=window, duration=-1)
    window._update_progress_bar = bar
    window._on_update_progress("download", 50 * 2**20, 100 * 2**20)
    assert bar.contentLabel.text() == "Downloading… 50% (50 of 100 MB)"
    window._on_update_progress("verify", 0, 0)
    assert "Checking" in bar.contentLabel.text()
    window._on_update_progress("unpack", 100 * 2**20, 100 * 2**20)
    assert bar.contentLabel.text().startswith("Unpacking… 100%")


def test_later_keeps_the_update_and_a_bar_restarts_it(window, packaged):
    packaged.restart = False
    window._on_update_staged("99.0.0", "")
    assert packaged.apply == []
    (bar,) = window.findChildren(InfoBar)
    assert bar.titleLabel.text() == "Update ready" and "99.0.0" in bar.contentLabel.text()
    next(b for b in bar.findChildren(QtWidgets.QPushButton) if b.text() == "Restart now").click()
    assert packaged.apply == [(packaged.install, "99.0.0")]


def test_a_failed_or_cancelled_download_says_so_and_never_asks_to_restart(window, packaged):
    window._on_update_staged("99.0.0", "cancelled")
    window._on_update_staged("99.0.0", "Not enough free disk space to unpack the update.")
    assert window.notices == [
        ("Update cancelled.", "info"),
        ("The update could not be installed: Not enough free disk space to unpack the update.", "warning")]
    assert window.findChildren(InfoBar) == [] and packaged.apply == []


def test_a_helper_that_cannot_start_keeps_the_app_open(window, packaged):
    packaged.apply_result = (False, "The updater did not start in time, so nothing was changed.")
    window._restart_to_update("99.0.0")
    assert window._apply_update_version == ""
    assert window.notices == [
        ("The update was not applied: The updater did not start in time, so nothing was changed.", "warning")]


def test_backing_out_of_unsaved_changes_cancels_the_restart(window, packaged, monkeypatch):
    monkeypatch.setattr(window, "_resolve_unsaved_changes", lambda: False)
    window._restart_to_update("99.0.0")
    assert packaged.apply == [] and window._apply_update_version == ""


def test_a_folder_that_cannot_be_replaced_falls_back_to_the_download_page(window, packaged, opened):
    packaged.reason = "C:/Program Files is not writable, so the app cannot replace itself there."
    window._start_self_update(PACKAGED_RELEASE)
    assert opened == [PACKAGED_RELEASE.url]
    assert packaged.stage == [] and window._update_download_running is False
    assert "not writable" in window.notices[-1][0] and window.notices[-1][1] == "warning"


def test_a_source_checkout_never_starts_a_self_update(window, opened):
    assert window._self_update_install is None
    window._start_self_update(PACKAGED_RELEASE)
    assert opened == [] and window.notices == []


def _staged(install, version):
    new = install.with_name(install.name + ".new")
    new.mkdir()
    (new / self_update.READY).write_text(version)
    return new


def test_start_reminds_of_a_waiting_update(window, packaged):
    _staged(packaged.install, "99.0.0")
    window._update_housekeeping()
    (bar,) = window.findChildren(InfoBar)
    assert bar.titleLabel.text() == "Update ready"


def test_start_reports_how_the_last_update_went(window, packaged):
    updates = window._updates_dir
    updates.mkdir()
    (updates / self_update.RESULT).write_text(json.dumps({"status": "ok", "version": "1.2.3"}))
    window._update_housekeeping()
    assert window.notices == [("Live Subtitle was updated to version 1.2.3.", "info")]
    (updates / self_update.RESULT).write_text(json.dumps(
        {"status": "rolled_back", "version": "1.2.4", "detail": "The new version did not start."}))
    window._update_housekeeping()
    message, kind = window.notices[-1]
    assert kind == "warning" and "1.2.4" in message and "did not start" in message
    assert not (updates / self_update.RESULT).exists(), "a verdict is shown once"


def test_start_removes_what_an_update_left_behind_but_not_while_the_helper_is_running(window, packaged, monkeypatch):
    install = packaged.install
    for suffix in (".old", ".failed"):
        install.with_name(install.name + suffix).mkdir()
    window._update_housekeeping()
    assert _wait_until(lambda: not any(install.with_name(install.name + s).exists() for s in (".old", ".failed")))
    leftover = install.with_name(install.name + ".old")
    leftover.mkdir()
    monkeypatch.setenv(self_update.OK_ENV, "x")  # started by the helper, which deletes .old itself
    window._update_housekeeping()
    QtTest.QTest.qWait(200)
    assert leftover.exists()

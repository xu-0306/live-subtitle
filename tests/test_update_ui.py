"""Update check in the desktop window: prompts only for a new version, stays quiet otherwise, survives offline."""
import json
import threading
import time

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from qfluentwidgets import InfoBar

from gui import config_manager, update_check, update_dialog
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

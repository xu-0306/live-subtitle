"""Task A shell contracts, using isolated configs and no backend or tray."""
from types import SimpleNamespace

import pytest
from PySide6 import QtCore, QtWidgets, QtTest
from qfluentwidgets import FluentWindow, InfoBar, ScrollArea, SimpleCardWidget, Theme, TitleLabel, qconfig

from gui import config_manager, fluent_kit
from gui.app import MainWindow


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(fluent_kit, "ask", lambda *_: QtWidgets.QMessageBox.Discard)
    cfg = config_manager.load_default_config()
    cfg["models"] = {"root": str(tmp_path / "models")}
    path = tmp_path / "config.yaml"
    config_manager.write_config(path, cfg)
    win = MainWindow(path, auto_start=False, setup_tray=False)
    yield win
    win.backend_runner = None
    win._load_config_into_ui(win.config)
    win.close()
    app.processEvents()


def test_six_fluent_routes_and_scroll_surfaces(window):
    assert isinstance(window, FluentWindow)
    assert window.windowTitle() == "Live Subtitle"
    assert (window.minimumWidth(), window.minimumHeight()) == (900, 640)
    assert window.stackedWidget.count() == 6
    assert [window.page_names[i] for i in window.pages] == [
        "Server", "Local translation", "Speech recognition", "Tuning", "Translation services", "Settings"]
    assert not window.navigationInterface.panel._isCollapsible
    assert len({page.objectName() for page in window.pages.values()}) == 6
    for index, page in window.pages.items():
        assert isinstance(page, ScrollArea) and page.widgetResizable()
        assert not page.viewport().autoFillBackground()
        assert not page.widget().autoFillBackground()
        assert page.findChild(TitleLabel).text() == window.page_names[index]
        window.navigationInterface.widget(page.objectName()).click()
        assert window.stackedWidget.currentWidget() is page
        assert window._current_page == index
        assert window.navigationInterface.widget(page.objectName()).isSelected


def _cards_outside_viewport(page):
    viewport = page.viewport()
    return [card.findChild(QtWidgets.QLabel).text() for card in page.findChildren(SimpleCardWidget)
            if card.mapTo(viewport, QtCore.QPoint(card.width(), 0)).x() > viewport.width()]


def test_every_page_follows_the_narrowest_window(window):
    window.resize(900, 640)
    window.show()
    for index, page in window.pages.items():
        window.switchTo(page)  # setCurrentWidget alone animates and leaves the page hidden
        QtWidgets.QApplication.processEvents()
        name = window.page_names[index]
        assert page.isVisible(), name
        assert page.widget().minimumSizeHint().width() <= page.viewport().width(), name
        assert page.widget().width() == page.viewport().width(), name
        assert not _cards_outside_viewport(page), name


def test_a_long_path_in_any_wrapped_label_never_widens_a_page(window):
    path = "G:\\" + "very_long_folder_name\\" * 10 + "model.gguf"
    window.resize(900, 640)
    window.show()
    for index, page in window.pages.items():
        window.switchTo(page)
        for label in page.findChildren(QtWidgets.QLabel):
            if label.wordWrap():
                label.setText(path)
        QtWidgets.QApplication.processEvents()
        name = window.page_names[index]
        assert page.widget().minimumSizeHint().width() <= page.viewport().width(), name
        assert not _cards_outside_viewport(page), name


@pytest.mark.parametrize("message", [
    "Backend exited before ready (exit code 3): " + " | ".join(f"INFO: log line {i} " + "word " * 20 for i in range(8)),
    "FileNotFoundError: " + "C:\\Users\\someone\\AppData\\Local\\" + "deeply\\nested\\" * 25 + "backend.exe",
])
def test_long_backend_failure_never_widens_the_page(window, message):
    window.resize(900, 640)
    window.show()
    window._log_lines = ["INFO: log line 7 first"]
    window._on_backend_error(message)
    QtWidgets.QApplication.processEvents()
    page = window.pages[0]
    assert not _cards_outside_viewport(page)
    assert page.widget().width() == page.viewport().width()
    assert window.server_status.text() == "Failed"  # a badge, not the message
    assert window.server_status.property("status") == "error"
    assert window.server_status_detail.text().startswith(message[:40])
    assert len(window.status_bar.currentMessage()) <= 140  # one line, never clipped mid-word by the bar
    # The whole message and the log tail are one click (here: already open) away, in a wrapping box.
    assert window.error_details.isVisible()
    assert message in window.error_details.toPlainText()
    assert "log line 7 first" in window.error_details.toPlainText()


def test_download_rows_are_hidden_when_idle_and_after_a_failed_download(window):
    window.resize(900, 640)
    window.show()
    window.switchTo(window.pages[2])
    QtWidgets.QApplication.processEvents()
    assert not window.download_progress.isVisible() and not window.download_label.isVisible()
    window.stt_model_form.setRowVisible(window.download_progress, True)
    window.stt_model_form.setRowVisible(window.download_label, True)
    window.download_progress.setValue(40)
    window._on_download_finished(False, "disk full")
    QtWidgets.QApplication.processEvents()
    assert not window.download_progress.isVisible() and not window.download_label.isVisible()
    assert (window.download_progress.minimum(), window.download_progress.maximum()) == (0, 100)


def test_saving_keeps_the_pinned_model_path_until_the_model_changes(window):
    window.config.setdefault("stt", {})["model_path"] = "G:/pinned/model.bin"
    window.config["stt"]["model"] = window.model_select_combo.currentData() or "medium"
    window.config.setdefault("models", {})["root"] = str(window._get_model_storage_root())
    assert window._collect_config_from_ui()["stt"]["model_path"] == "G:/pinned/model.bin"
    window.config["stt"]["model"] = "some-other-model"
    assert "model_path" not in window._collect_config_from_ui()["stt"]


def test_restart_resolves_unsaved_changes_before_stopping_the_backend(window, monkeypatch):
    stopped = []
    window.backend_runner = SimpleNamespace(isRunning=lambda: True, stop=lambda: stopped.append("runner"))
    monkeypatch.setattr(window, "_stop_backend", lambda: stopped.append("stop"))
    monkeypatch.setattr(window, "_resolve_unsaved_changes", lambda: False)  # the user cancelled
    window._restart_backend()
    assert stopped == [] and not window.restart_pending  # still running, nothing half-done
    monkeypatch.setattr(window, "_resolve_unsaved_changes", lambda: True)
    window._restart_backend()
    assert stopped == ["stop"] and window.restart_pending
    window.restart_pending = False


def test_sidebar_is_narrow_and_status_text_clears_window_edge(window):
    window.resize(900, 640)
    window.show()
    QtWidgets.QApplication.processEvents()
    nav = window.navigationInterface
    assert nav.width() == MainWindow.NAV_EXPAND_WIDTH
    assert nav.width() / window.width() < 0.3  # the library default (322 px) was ~36% here
    status_bottom = window.status_bar.mapTo(window, QtCore.QPoint(0, window.status_bar.height())).y()
    assert window.height() - status_bottom >= 6  # descenders stay clear of the rounded window edge


def test_fluent_navigation_never_prompts_for_dirty_pages(window, monkeypatch):
    original = window.host_input.text()
    window.host_input.setText("127.0.0.2")
    assert window._dirty_pages == {0}

    def forbidden(*_args):
        pytest.fail("Moving between pages must never prompt")
    monkeypatch.setattr(fluent_kit, "ask", forbidden)
    window.navigationInterface.widget("services").click()
    assert window._current_page == 5
    assert window.stackedWidget.currentWidget() is window.pages[5]
    assert window.navigationInterface.widget("services").isSelected
    # The edit stays in the form, unsaved, until Save all / close / discard.
    assert window.host_input.text() == "127.0.0.2"
    assert config_manager.load_config(window.config_path)["server"]["host"] == original
    assert window._dirty_pages == {0}


@pytest.mark.parametrize("mode", ["light", "dark", "system"])
def test_theme_selector_dirties_settings_and_roundtrips(window, mode):
    window._set_theme_mode("dark" if mode == "light" else "light", persist=False)
    window.theme_combo.setCurrentIndex(window.theme_combo.findData(mode))
    assert window._dirty_pages == {4}
    assert window._theme_mode() == mode
    window._save_defaults()
    assert not window._dirty
    assert config_manager.load_config(window.config_path)["gui"]["theme"] == mode
    reopened = MainWindow(window.config_path, auto_start=False, setup_tray=False)
    try:
        assert reopened._theme_mode() == mode
        assert reopened.theme_combo.currentData() == mode
        assert not reopened._dirty
    finally:
        reopened.close()


def test_tuning_settings_explain_themselves(window):
    window.show()
    window.switchTo(window.pages[3])
    QtWidgets.QApplication.processEvents()
    fields = {
        "Max chars": window.subtitle_max_chars_input,
        "Max sentences (Latin)": window.subtitle_max_sentences_default_input,
        "Max sentences (CJK)": window.subtitle_max_sentences_cjk_input,
        "Stall timeout (sec)": window.stall_timeout_input,
    }
    labels = {label.text(): label for label in window.pages[3].findChildren(QtWidgets.QLabel)}
    for text, field in fields.items():
        assert labels[text].isVisible()
        assert not field.toolTip() and not labels[text].toolTip()
    for default in (260, 2, 1, 15):
        assert any(label.isVisible() and "Example" in text and f"(Default {default})" in text
                   for text, label in labels.items())
    assert labels["Advanced settings. The defaults work for most videos; changes apply to the next capture."].isVisible()
    assert "Stall check interval (sec)" not in labels


@pytest.mark.parametrize("size", [(1200, 800), (900, 700)])
def test_tuning_field_widths_and_alignment(window, size):
    window.resize(*size)
    window.show()
    window.switchTo(window.pages[3])
    QtWidgets.QApplication.processEvents()
    fields = window.pages[3].findChildren(QtWidgets.QSpinBox)
    assert len(fields) == 4
    assert all(field.width() <= 160 for field in fields)
    hints = [label for label in window.pages[3].findChildren(QtWidgets.QLabel) if "Example:" in label.text()]
    assert len(hints) == 4
    assert all(label.fontMetrics().horizontalAdvance(label.text()) <= label.width() for label in hints)
    left = lambda field: field.mapTo(window, QtCore.QPoint(0, 0)).x()
    assert abs(left(window.subtitle_max_chars_input) - left(window.stall_timeout_input)) <= 1


def test_stall_check_interval_survives_save_and_reset(window, monkeypatch):
    window.config["stt"]["stall_check_interval_sec"] = 37
    assert window._save_config_to_disk()
    assert config_manager.load_config(window.config_path)["stt"]["stall_check_interval_sec"] == 37
    window.switchTo(window.pages[3])
    window.stall_timeout_input.setValue(22)
    monkeypatch.setattr(fluent_kit, "ask", lambda *_: QtWidgets.QMessageBox.Yes)
    window._reset_defaults()
    assert window._save_config_to_disk()
    assert config_manager.load_config(window.config_path)["stt"]["stall_check_interval_sec"] == 37
    reopened = MainWindow(window.config_path, auto_start=False, setup_tray=False)
    try:
        assert reopened.config["stt"]["stall_check_interval_sec"] == 37
    finally:
        reopened.close()


def test_footer_and_status_bar_use_same_caption_font(window):
    assert window.restart_notice.font().pixelSize() == window.status_bar.font().pixelSize()
    assert window.restart_notice.font().weight() == window.status_bar.font().weight()


def test_stt_spoken_language_options_and_codes(window):
    combo = window.stt_language_input
    assert [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())] == [
        ("Automatic", "auto"), ("English", "en"), ("Japanese", "ja"), ("Chinese", "zh")]
    assert any(label.text() == "Spoken language" for label in window.pages[2].findChildren(QtWidgets.QLabel))
    assert "spoken language" in combo.toolTip()
    for code in ("auto", "en", "ja", "zh"):
        config = dict(window.config, stt=dict(window.config["stt"], language=code))
        window._load_config_into_ui(config, page=2)
        assert window._collect_config_from_ui()["stt"]["language"] == code


def test_custom_stt_language_code_survives_load_and_save(window):
    # A code that is not in the suggestions (for example set by hand in config.yaml) used to load as "Automatic".
    config = dict(window.config, stt=dict(window.config["stt"], language="ko"))
    window._load_config_into_ui(config, page=2)
    assert window.stt_language_input.currentText() == "ko"
    assert window._collect_config_from_ui()["stt"]["language"] == "ko"


def test_system_palette_reapplies_without_backend_or_dirty_changes(window, monkeypatch):
    window._set_theme_mode("system", persist=False)
    monkeypatch.setattr("qfluentwidgets.common.config.darkdetect.theme", lambda: "Dark")
    applied = []
    monkeypatch.setattr("gui.app.setTheme", applied.append)
    QtWidgets.QApplication.sendEvent(window, QtCore.QEvent(QtCore.QEvent.ApplicationPaletteChange))
    assert applied == [Theme.AUTO]
    assert qconfig.theme == Theme.DARK
    assert window._theme_mode() == "system"
    assert not window._dirty and window.backend_runner is None
    assert window._shared_config_timer.interval() == 2500


def test_status_bar_timeout_notice_and_badge(window):
    window.show()
    QtWidgets.QApplication.processEvents()
    window.status_bar.showMessage("Temporary", 10)
    assert window.status_bar.currentMessage() == "Temporary"
    QtTest.QTest.qWait(100)
    assert window.status_bar.currentMessage() == ""
    for text, kind in [("Ready", "running"), ("Failed", "error"), ("Stopped", "idle")]:
        window._set_status(text, kind)
        assert window.server_status.text() == text
        assert window.server_status.property("status") == kind
        assert window.status_bar.currentMessage() == text
    window._show_notice("Preserved notice text", "error")
    assert window.status_bar.currentMessage() == "Preserved notice text"
    assert any(bar.content == "Preserved notice text" for bar in window.findChildren(InfoBar))


def test_titlebar_close_runs_existing_shutdown_order(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, "_resolve_unsaved_changes", lambda: calls.append("resolve") or True)
    window._service_test_socket = object()
    monkeypatch.setattr(window, "_finish_service_test", lambda *_: calls.append("service"))
    monkeypatch.setattr(window.local_models, "shutdown", lambda: calls.append("local") or True)
    monkeypatch.setattr(window, "_stop_backend", lambda: calls.append("stop"))
    window.backend_runner = SimpleNamespace(wait=lambda ms: calls.append(("wait", ms)) or True)
    monkeypatch.setattr(QtWidgets.QApplication, "quit", lambda: calls.append("quit"))
    window.show()
    window.titleBar.closeBtn.click()
    assert calls == ["resolve", "service", "local", "stop", ("wait", 6000), "quit"]
    window._service_test_socket = None


@pytest.mark.parametrize("theme,rgb", [("light", (240, 244, 249)), ("dark", (32, 32, 32))])
def test_window_background_is_solid_not_mica(tmp_path, theme, rgb):
    """Mica showed the wallpaper only while active; the solid fill must also survive the startup fade."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cfg = config_manager.load_default_config()
    cfg["models"] = {"root": str(tmp_path / "models")}
    cfg.setdefault("gui", {})["theme"] = theme
    path = tmp_path / "config.yaml"
    config_manager.write_config(path, cfg)
    win = MainWindow(path, auto_start=False, setup_tray=False)
    try:
        QtTest.QTest.qWait(300)  # Fluent's 120 ms background fade must have finished
        assert not win.isMicaEffectEnabled()
        assert win.backgroundColor.getRgb() == (*rgb, 255)
    finally:
        win.close()
        app.processEvents()

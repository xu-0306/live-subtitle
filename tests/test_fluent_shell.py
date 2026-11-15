"""Task A shell contracts, using isolated configs and no backend or tray."""
from types import SimpleNamespace

import pytest
from PySide6 import QtCore, QtGui, QtWidgets, QtTest
from qfluentwidgets import FluentWindow, InfoBar, ScrollArea, Theme, TitleLabel, qconfig

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
        "Server", "Local translation", "STT models", "Tuning", "Translation services", "Settings"]
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


def test_sidebar_is_narrow_and_status_text_clears_window_edge(window):
    window.resize(900, 640)
    window.show()
    QtWidgets.QApplication.processEvents()
    nav = window.navigationInterface
    assert nav.width() == MainWindow.NAV_EXPAND_WIDTH
    assert nav.width() / window.width() < 0.3  # the library default (322 px) was ~36% here
    status_bottom = window.status_bar.mapTo(window, QtCore.QPoint(0, window.status_bar.height())).y()
    assert window.height() - status_bottom >= 6  # descenders stay clear of the rounded window edge


@pytest.mark.parametrize("answer", [QtWidgets.QMessageBox.Cancel, QtWidgets.QMessageBox.Discard,
                                  QtWidgets.QMessageBox.Save])
def test_fluent_navigation_guards_dirty_server(window, monkeypatch, answer):
    original = window.host_input.text()
    window.host_input.setText("127.0.0.2")
    assert window._dirty_pages == {0}
    monkeypatch.setattr(fluent_kit, "ask", lambda *_: answer)
    window.navigationInterface.widget("services").click()
    cancelled = answer == QtWidgets.QMessageBox.Cancel
    assert window._current_page == (0 if cancelled else 5)
    assert window.stackedWidget.currentWidget() is window.pages[window._current_page]
    assert window.navigationInterface.widget("server" if cancelled else "services").isSelected
    assert window.host_input.text() == (original if answer == QtWidgets.QMessageBox.Discard else "127.0.0.2")
    saved = config_manager.load_config(window.config_path)
    assert saved["server"]["host"] == ("127.0.0.2" if answer == QtWidgets.QMessageBox.Save else original)
    assert window._dirty == cancelled


@pytest.mark.parametrize("mode", ["light", "dark", "system"])
def test_theme_selector_dirties_settings_and_roundtrips(window, mode):
    window._set_theme_mode("dark" if mode == "light" else "light", persist=False)
    window.theme_combo.setCurrentIndex(window.theme_combo.findData(mode))
    assert window._dirty_pages == {4}
    assert window._theme_mode() == mode
    assert window._save_page(4)
    assert config_manager.load_config(window.config_path)["gui"]["theme"] == mode
    reopened = MainWindow(window.config_path, auto_start=False, setup_tray=False)
    try:
        assert reopened._theme_mode() == mode
        assert reopened.theme_combo.currentData() == mode
        assert not reopened._dirty
    finally:
        reopened.close()


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

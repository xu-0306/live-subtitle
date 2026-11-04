from __future__ import annotations

import copy
import json
import os
import re
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtGui, QtWidgets, QtWebSockets
from qfluentwidgets import SpinBox

from backend import config_store
from gui import config_manager
from gui.app import MainWindow
from gui.wheel_guard import protect_wheel_controls


def _nav(window, key):
    return window.navigationInterface.widget(key)


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *_args: QtWidgets.QMessageBox.Discard)
    cfg = config_manager.load_default_config()
    cfg["models"] = {"root": str(tmp_path / "models")}
    cfg["translation"]["service_profiles"] = [
        {"id": "api-one", "name": "Office", "kind": "api", "base_url": "https://example.test/v1",
         "model": "old-model", "api_key": "old-secret", "custom_metadata": "preserved"},
        {"id": "api-two", "name": "Backup", "kind": "api", "model": "backup"},
    ]
    path = tmp_path / "config.yaml"
    config_manager.write_config(path, cfg)
    win = MainWindow(path, auto_start=False, setup_tray=False)
    yield win
    win.backend_runner = None
    win._load_config_into_ui(win.config)
    win.close()
    app.processEvents()


def test_service_draft_save_discard_and_draft_test(window):
    catalog = window.service_catalog
    saved_row = catalog.profile_list.item(0).text()
    old_config = copy.deepcopy(window.config)
    before_disk = config_store.load_snapshot(window.config_path)[0]
    catalog.name_input.setText("Renamed")
    catalog.model_input.setText("draft-model")
    assert catalog._profiles[0]["name"] == "Office"
    assert catalog.profile_list.item(0).text() == saved_row + " · Unsaved"
    assert catalog.editor_status.text() == "Unsaved changes"
    assert "Translation services" not in window.dirty_label.text()
    assert window._dirty and window._dirty_pages == {5}
    assert config_store.load_snapshot(window.config_path)[0] == before_disk
    requested = []
    catalog.test_requested.disconnect(window._test_translation_profile)
    catalog.test_requested.connect(requested.append)
    catalog.test_btn.click()
    assert requested[0]["model"] == "draft-model"
    assert config_store.load_snapshot(window.config_path)[0] == before_disk
    window.host_input.setText("127.0.0.2")
    catalog.save_btn.click()
    saved = config_store.load_snapshot(window.config_path)[0]
    assert saved["server"] == old_config["server"]
    assert saved["translation"]["service_profiles"][0]["name"] == "Renamed"
    assert saved["translation"]["service_profiles"][0]["custom_metadata"] == "preserved"
    assert saved["translation"]["service_profiles"][1] == old_config["translation"]["service_profiles"][1]
    assert catalog.editor_status.text() == "Saved"
    assert window._dirty_pages == {0}
    catalog.model_input.setText("discard-me")
    catalog.discard_btn.click()
    assert catalog.model_input.text() == "draft-model"
    assert "Unsaved" not in catalog.profile_list.item(0).text()


@pytest.mark.parametrize("answer", [QtWidgets.QMessageBox.Save, QtWidgets.QMessageBox.Discard, QtWidgets.QMessageBox.Cancel])
def test_switch_service_guards_draft(window, monkeypatch, answer):
    catalog = window.service_catalog
    catalog.name_input.setText("Pending")
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *_args: answer)
    catalog.profile_list.setCurrentRow(1)
    assert catalog.profile_list.currentRow() == (0 if answer == QtWidgets.QMessageBox.Cancel else 1)
    saved = config_store.load_snapshot(window.config_path)[0]["translation"]["service_profiles"]
    assert saved[0]["name"] == ("Pending" if answer == QtWidgets.QMessageBox.Save else "Office")


def test_type_change_has_new_id_and_clears_provider_fields(window, monkeypatch):
    catalog = window.service_catalog
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *_args: QtWidgets.QMessageBox.Cancel)
    catalog.kind_combo.setCurrentIndex(catalog.kind_combo.findData("ollama"))
    assert catalog.kind_combo.currentData() == "api"
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *_args: QtWidgets.QMessageBox.Yes)
    catalog.default_check.setChecked(True)
    catalog.kind_combo.setCurrentIndex(catalog.kind_combo.findData("nllb"))
    identity = catalog.id_input.text()
    assert re.fullmatch(r"service-[0-9a-f]{8}", identity)
    assert catalog.endpoint_input.text() == catalog.model_input.text() == catalog.api_key_input.text() == ""
    assert catalog._profiles[0]["id"] == "api-one"
    assert "API" in catalog.profile_list.item(0).text()
    catalog.name_input.setText("Local")
    assert catalog.id_input.text() == identity
    assert catalog.save_selected()
    saved = config_store.load_snapshot(window.config_path)[0]
    assert saved["translation"]["service_profiles"][0]["id"] == identity
    assert saved["translation"]["default_selection"] == {"kind": "profile", "id": identity}


def test_service_revision_conflict_retains_draft(window):
    catalog = window.service_catalog
    catalog.model_input.setText("unsaved")
    external = copy.deepcopy(window.config)
    external["server"]["port"] = 9999
    config_store.save_config(window.config_path, external, expected_revision=window.config_revision)
    assert not catalog.save_selected()
    assert catalog.model_input.text() == "unsaved"
    assert "Unsaved" in catalog.editor_status.text()
    assert config_store.load_snapshot(window.config_path)[0]["server"]["port"] == 9999


@pytest.mark.parametrize("answer", [QtWidgets.QMessageBox.Save, QtWidgets.QMessageBox.Discard, QtWidgets.QMessageBox.Cancel])
def test_close_dirty_prompt(window, monkeypatch, answer):
    original = window.config["server"]["host"]
    window.host_input.setText("127.0.0.2")
    prompts = []
    def respond(_parent, title, message, *_args):
        prompts.append((title, message))
        return answer
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", respond)
    event = QtGui.QCloseEvent()
    window.closeEvent(event)
    assert "Server" in prompts[0][1]
    assert event.isAccepted() == (answer != QtWidgets.QMessageBox.Cancel)
    saved = config_store.load_snapshot(window.config_path)[0]
    assert saved["server"]["host"] == ("127.0.0.2" if answer == QtWidgets.QMessageBox.Save else original)
    if answer == QtWidgets.QMessageBox.Cancel:
        assert window.host_input.text() == "127.0.0.2"
        assert window._dirty


def test_sidebar_cancel_and_discard(window, monkeypatch):
    window.host_input.setText("127.0.0.2")
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *_args: QtWidgets.QMessageBox.Cancel)
    _nav(window, "services").click()
    assert window.tabs.currentIndex() == 0
    assert _nav(window, "server").isSelected
    assert not _nav(window, "services").isSelected
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *_args: QtWidgets.QMessageBox.Discard)
    _nav(window, "services").click()
    assert window.tabs.currentIndex() == 5
    assert window.host_input.text() == window.config["server"]["host"]


def test_sidebar_guards_theme_preview_from_header(window, monkeypatch):
    original = window._theme_mode()
    window._set_theme_mode("dark" if original != "dark" else "light")
    assert window._dirty_pages == {4}
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *_args: QtWidgets.QMessageBox.Cancel)
    _nav(window, "services").click()
    assert window.tabs.currentIndex() == 0
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *_args: QtWidgets.QMessageBox.Discard)
    _nav(window, "services").click()
    assert window.tabs.currentIndex() == 5
    assert window._theme_mode() == original


def test_new_service_discard_restores_empty_editor(window):
    catalog = window.service_catalog
    catalog.load_from_config({"translation": {"service_profiles": []}})
    catalog.new_btn.click()
    assert catalog.has_unsaved_changes()
    catalog.api_key_input.setText("draft-key")
    catalog.discard_btn.click()
    assert catalog.profile_list.count() == 0
    assert catalog.name_input.text() == catalog.api_key_input.text() == ""
    assert not catalog.has_unsaved_changes()


@pytest.mark.parametrize("page", range(6))
def test_reset_is_page_scoped_and_keeps_saved_services(window, monkeypatch, page):
    window.tabs.setCurrentIndex(page)
    saved = copy.deepcopy(window.config)
    window.service_catalog.model_input.setText("pending-service")
    window.subtitle_max_chars_input.setValue(999)
    window.host_input.setText("127.0.0.2")
    messages = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda _parent, _title, message, *_args:
                        messages.append(message) or QtWidgets.QMessageBox.Yes)
    window._reset_defaults()
    assert window.tabs.tabText(page) in messages[-1]
    assert window.config == saved
    if page != 0:
        assert window.host_input.text() == "127.0.0.2"
    if page != 3:
        assert window.subtitle_max_chars_input.value() == 999
    if page != 5:
        assert window.service_catalog.model_input.text() == "pending-service"
    assert window._save_page(page)
    persisted = config_store.load_snapshot(window.config_path)[0]
    assert persisted["translation"]["service_profiles"] == saved["translation"]["service_profiles"]


@pytest.mark.parametrize("ok", [True, False])
def test_draft_test_sends_current_profile_without_saving(window, monkeypatch, ok):
    window.backend_runner = SimpleNamespace(isRunning=lambda: True)
    window.server_status.setText("Ready")
    window.service_catalog.model_input.setText("untested")
    window.service_catalog.api_key_input.clear()
    before = config_store.load_snapshot(window.config_path)
    def forbidden(*_args):
        pytest.fail("Testing a draft must never prompt or save")
    monkeypatch.setattr(window, "_save_service", forbidden)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", forbidden)
    messages = []
    monkeypatch.setattr(QtWebSockets.QWebSocket, "open", lambda *_args: None)
    monkeypatch.setattr(QtWebSockets.QWebSocket, "sendTextMessage",
                        lambda _socket, raw: messages.append(json.loads(raw)))
    window.service_catalog.test_btn.click()
    window._service_test_socket.connected.emit()
    assert messages[0]["action"] == "translate"
    draft = messages[0]["draft_profile"]
    assert (draft["id"], draft["model"], draft["engine"], draft["api_key"]) == (
        "api-one", "untested", "openai_compatible", "")
    assert "_default" not in draft
    window._on_service_test_message(json.dumps({"type": "test_result", "ok": ok, "message": "result"}))
    assert window.service_catalog.editor_status.text() == ("Test passed: result" if ok else "Test failed: result")
    assert window.service_catalog.has_unsaved_changes()
    assert config_store.load_snapshot(window.config_path) == before
    window.service_catalog.model_input.setText("another draft")
    assert window.service_catalog.editor_status.text() == "Unsaved changes"


def test_service_actions_have_one_inline_status_and_no_page_save(window):
    window.tabs.setCurrentIndex(5)
    catalog = window.service_catalog
    assert catalog.save_btn.property("kind") != "secondary"
    assert catalog.discard_btn.property("kind") == catalog.test_btn.property("kind") == "secondary"
    assert window.save_btn.isHidden()
    catalog.name_input.setText("Pending")
    assert catalog.editor_status.parent() is catalog.save_btn.parent()
    assert catalog.editor_status.objectName() == "mutedText"
    statuses = [label.text() for label in catalog.findChildren(QtWidgets.QLabel)
                if "Unsaved" in label.text()]
    assert statuses == ["Unsaved changes"]
    assert catalog.profile_list.item(0).text().endswith(" · Unsaved")
    assert not window.dirty_label.text()
    window.host_input.setText("127.0.0.2")
    assert window.dirty_label.text() == "Unsaved: Server"
    catalog.discard_btn.click()
    window.tabs.setCurrentIndex(0)
    assert not window.save_btn.isHidden()


def test_backend_start_cancel_does_not_publish_drafts(window, monkeypatch):
    window.service_catalog.model_input.setText("draft")
    before = config_store.load_snapshot(window.config_path)[0]
    monkeypatch.setattr(window, "_validate_server_settings", lambda: True)
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *_args: QtWidgets.QMessageBox.Cancel)
    window._start_backend()
    assert window.backend_runner is None
    assert config_store.load_snapshot(window.config_path)[0] == before
    assert window.service_catalog.model_input.text() == "draft"


def test_local_model_save_preserves_unrelated_service_draft(window):
    window.service_catalog.model_input.setText("draft")
    cfg = window.local_models.get_config()
    assert cfg["translation"]["service_profiles"][0]["model"] == "old-model"
    window._persist_local_config(cfg)
    window._on_local_model_activated(cfg)
    assert window.service_catalog.model_input.text() == "draft"
    assert window.service_catalog.has_unsaved_changes()


@pytest.mark.parametrize("widget_type", [QtWidgets.QComboBox, QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox,
                                        QtWidgets.QSlider, SpinBox])
def test_wheel_requires_focus_and_scrolls_page(widget_type):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    scroll = QtWidgets.QScrollArea()
    content = QtWidgets.QWidget()
    content.setMinimumHeight(1600)
    layout = QtWidgets.QVBoxLayout(content)
    widget = widget_type()
    if isinstance(widget, QtWidgets.QComboBox):
        widget.addItems(["One", "Two", "Three"])
        widget.setCurrentIndex(1)
        value = widget.currentIndex
    else:
        widget.setRange(0, 100)
        widget.setValue(10)
        value = widget.value
    layout.addWidget(widget)
    other = QtWidgets.QLineEdit()
    layout.addWidget(other)
    layout.addStretch()
    scroll.setWidget(content)
    scroll.resize(400, 300)
    protect_wheel_controls(content)
    scroll.show()
    scroll.activateWindow()
    other.setFocus()
    app.processEvents()
    def wheel(delta):
        event = QtGui.QWheelEvent(QtCore.QPointF(5, 5), QtCore.QPointF(widget.mapToGlobal(QtCore.QPoint(5, 5))),
                                 QtCore.QPoint(), QtCore.QPoint(0, delta), QtCore.Qt.NoButton,
                                 QtCore.Qt.NoModifier, QtCore.Qt.ScrollUpdate, False)
        QtWidgets.QApplication.sendEvent(widget, event)
        app.processEvents()
    before = value()
    assert not widget.hasFocus()
    assert widget.focusPolicy() == QtCore.Qt.StrongFocus
    wheel(-120)
    assert value() == before
    assert scroll.verticalScrollBar().value() > 0
    scroll.ensureWidgetVisible(widget)
    widget.setFocus()
    app.processEvents()
    assert widget.hasFocus()
    wheel(120)
    assert value() != before
    scroll.close()

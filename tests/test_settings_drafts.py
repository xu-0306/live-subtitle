from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtGui, QtWidgets, QtWebSockets
from qfluentwidgets import SpinBox

from backend import config_store
from backend.translation_service import resolve_selection
from gui import config_manager, fluent_kit
from gui.app import LANGUAGE_OPTIONS, MainWindow
from gui.wheel_guard import protect_wheel_controls


def _nav(window, key):
    return window.navigationInterface.widget(key)


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(fluent_kit, "ask", lambda *_args: QtWidgets.QMessageBox.Discard)
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
    assert window.dirty_label.text() == "Unsaved: Translation services"
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


def test_service_list_details_and_availability(window):
    catalog = window.service_catalog
    assert catalog.profile_list.item(0).text() == "Office\nold-model · example.test"
    assert catalog.availability_label.text() == "Ready to test"
    assert catalog.profile_list.item(1).text() == "Backup\nNeeds setup · backup"
    catalog.model_input.clear()
    assert catalog.profile_list.item(0).text().endswith(" · Unsaved")
    profiles = [
        ({"kind": "api", "model": "m", "base_url": "http://user:secret@localhost:11434/v1"}, "Ready to test"),
        ({"kind": "api", "model": "m"}, "Needs setup"),
        ({"kind": "nllb", "model": "m", "installed": True}, "Installed"),
        ({"kind": "nllb", "available": False}, "Missing"),
    ]
    for profile, state in profiles:
        assert catalog._profile_state(profile) == state
    catalog._profiles = [dict(id=str(i), name=str(i), **profile) for i, (profile, _) in enumerate(profiles)]
    catalog._active_index = -1
    catalog._refresh_list()
    assert catalog.profile_list.item(0).text() == "0\nm · localhost:11434"
    assert catalog.profile_list.item(2).text() == "2\nm"
    assert catalog.profile_list.item(3).text() == "3\nMissing · No model set"


@pytest.mark.parametrize("answer", [QtWidgets.QMessageBox.Save, QtWidgets.QMessageBox.Discard, QtWidgets.QMessageBox.Cancel])
def test_switch_service_guards_draft(window, monkeypatch, answer):
    catalog = window.service_catalog
    catalog.name_input.setText("Pending")
    monkeypatch.setattr(fluent_kit, "ask", lambda *_args: answer)
    catalog.profile_list.setCurrentRow(1)
    assert catalog.profile_list.currentRow() == (0 if answer == QtWidgets.QMessageBox.Cancel else 1)
    saved = config_store.load_snapshot(window.config_path)[0]["translation"]["service_profiles"]
    assert saved[0]["name"] == ("Pending" if answer == QtWidgets.QMessageBox.Save else "Office")


def test_type_change_has_new_id_and_clears_provider_fields(window, monkeypatch):
    catalog = window.service_catalog
    monkeypatch.setattr(fluent_kit, "ask", lambda *_args: QtWidgets.QMessageBox.Cancel)
    catalog.kind_combo.setCurrentIndex(catalog.kind_combo.findData("ollama"))
    assert catalog.kind_combo.currentData() == "api"
    monkeypatch.setattr(fluent_kit, "ask", lambda *_args: QtWidgets.QMessageBox.Yes)
    catalog.default_check.setChecked(True)
    catalog.kind_combo.setCurrentIndex(catalog.kind_combo.findData("nllb"))
    identity = catalog.id_input.text()
    assert re.fullmatch(r"service-[0-9a-f]{8}", identity)
    assert catalog.endpoint_input.text() == catalog.model_input.text() == catalog.api_key_input.text() == ""
    assert catalog._profiles[0]["id"] == "api-one"
    assert catalog.profile_list.item(0).text() == "Office\nold-model · example.test · Unsaved"
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
    monkeypatch.setattr(fluent_kit, "ask", respond)
    event = QtGui.QCloseEvent()
    window.closeEvent(event)
    assert "Server" in prompts[0][1]
    assert event.isAccepted() == (answer != QtWidgets.QMessageBox.Cancel)
    saved = config_store.load_snapshot(window.config_path)[0]
    assert saved["server"]["host"] == ("127.0.0.2" if answer == QtWidgets.QMessageBox.Save else original)
    if answer == QtWidgets.QMessageBox.Cancel:
        assert window.host_input.text() == "127.0.0.2"
        assert window._dirty


def test_theme_preview_survives_page_switch_and_discard_reverts_it(window, monkeypatch):
    original = window._theme_mode()
    window._set_theme_mode("dark" if original != "dark" else "light")
    assert window._dirty_pages == {4}
    monkeypatch.setattr(fluent_kit, "ask", lambda *_args: pytest.fail("Moving between pages must never prompt"))
    _nav(window, "services").click()
    assert window.tabs.currentIndex() == 5
    assert window._dirty_pages == {4}
    monkeypatch.setattr(fluent_kit, "ask", lambda *_args: QtWidgets.QMessageBox.Discard)
    assert window._resolve_unsaved_changes()
    assert window._theme_mode() == original
    assert not window._dirty


def test_save_all_writes_every_page_in_one_click(window):
    window.host_input.setText("127.0.0.2")
    window.subtitle_max_chars_input.setValue(321)
    window.service_catalog.model_input.setText("all-model")
    assert window._dirty_pages == {0, 3, 5}
    assert window.dirty_label.text() == "Unsaved: Server, Tuning, Translation services"
    window.tabs.setCurrentIndex(3)
    assert not window.save_btn.isHidden()
    window.save_btn.click()
    saved = config_store.load_snapshot(window.config_path)[0]
    assert saved["server"]["host"] == "127.0.0.2"
    assert saved["subtitle"]["max_chars"] == 321
    assert saved["translation"]["service_profiles"][0]["model"] == "all-model"
    assert not window._dirty and window.dirty_label.text() == ""
    assert window.service_catalog.editor_status.text() == "Saved"


@pytest.mark.parametrize("legacy", ["compatible", "managed"])
def test_save_all_keeps_a_legacy_default_translation_service(tmp_path, legacy):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    cfg = config_manager.load_default_config()
    cfg["models"] = {"root": str(tmp_path / "models")}
    if legacy == "compatible":
        cfg["translation"].update(engine="openai_compatible", openai_compatible={
            "model": "old", "base_url": "https://provider.example.test/v1", "api_key": "k"})
    else:
        cfg["translation"]["engine"] = "managed_llama"
        cfg.setdefault("local_llama", {}).update(
            enabled=True, model_id="qwen25-1.5b-q4", installed_models=["qwen25-1.5b-q4"])
    path = tmp_path / "config.yaml"
    config_manager.write_config(path, cfg)

    def effective():
        base, local_id = resolve_selection(config_store.load_snapshot(path)[0], None)
        section = base.get(base["engine"]) if isinstance(base.get(base["engine"]), dict) else {}
        return base["engine"], local_id, section.get("model"), section.get("base_url"), section.get("api_key")

    before = effective()
    win = MainWindow(path, auto_start=False, setup_tray=False)
    try:
        assert win._save_config_to_disk()
        assert effective() == before
    finally:
        win.backend_runner = None
        win.close()


def test_save_all_refuses_a_key_typed_into_a_service_url(window):
    before = config_store.load_snapshot(window.config_path)
    window.service_catalog.endpoint_input.setText("https://sk-secret@example.test/v1")
    assert window._save_config_to_disk() is False
    assert config_store.load_snapshot(window.config_path) == before
    window.service_catalog.endpoint_input.setText("https://example.test/v1")
    assert window._save_config_to_disk() is True


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
    monkeypatch.setattr(fluent_kit, "ask", lambda _parent, _title, message, *_args:
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
    persisted = config_store.load_snapshot(window.config_path)[0]
    assert persisted["translation"]["service_profiles"] == saved["translation"]["service_profiles"]


def test_target_language_options_match_extension():
    popup = (Path(__file__).resolve().parents[1] / "chrome-extension/popup/popup.js").read_text(encoding="utf-8")
    options = re.search(r"const LANGUAGE_OPTIONS\s*=\s*\[(.*?)\];", popup, re.DOTALL)
    assert options is not None
    assert re.findall(r'\["([^"]+)",\s*"([^"]+)"\]', options.group(1)) == list(LANGUAGE_OPTIONS)


def _config_with_target(window, translation, local):
    config = copy.deepcopy(window.config)
    config["translation"].pop("target_language", None)
    config["translation"].update(translation)
    config["local_llama"] = dict(local)
    return config


def test_target_language_lives_on_the_services_page_and_keeps_any_value(window):
    combo = window.target_language_input
    assert [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())] == list(LANGUAGE_OPTIONS)
    texts = lambda page: [label.text() for label in window.pages[page].findChildren(QtWidgets.QLabel)]
    assert "Target language" in texts(5)
    assert not any("target language" in text.lower() for text in texts(1))
    cases = [
        ({"target_language": "Esperanto"}, {"target_language": "Wolof"}, "Esperanto"),  # shared key wins
        ({}, {"target_language": "Wolof"}, "Wolof"),  # the older local-model key is only a fallback
        ({"target_language": "ja"}, {}, "ja"),
        ({}, {}, "zh-TW"),
    ]
    for translation, local, expected in cases:
        window._load_config_into_ui(_config_with_target(window, translation, local), page=5)
        assert window._collect_config_from_ui()["translation"]["target_language"] == expected


def test_language_changed_in_the_extension_shows_on_the_desktop(window):
    snapshot, revision = config_store.load_snapshot(window.config_path)
    # Older GUI versions also saved the language under local_llama; Options only writes translation.
    snapshot["local_llama"] = {**snapshot.get("local_llama", {}), "target_language": "zh-TW"}
    snapshot["translation"]["target_language"] = "es-MX"
    config_store.save_config(window.config_path, snapshot, expected_revision=revision)
    window._sync_shared_config()
    assert window.target_language_input.value("zh-TW") == "es-MX"
    assert not window._dirty_pages


def test_unsaved_target_language_keeps_the_services_page_dirty(window, monkeypatch):
    window.switchTo(window.pages[5])
    saved = window.target_language_input.value("zh-TW")
    window.target_language_input.set_value("Wolof")
    assert window._dirty_pages == {5}
    assert window.dirty_label.text() == "Unsaved: Translation services"
    window.target_language_input.set_value(saved)
    assert not window._dirty_pages
    # Saving one service writes only services, so the language stays pending.
    window.target_language_input.set_value("Wolof")
    window.service_catalog.model_input.setText("edited")
    assert window._save_service()
    assert window._dirty_pages == {5}
    assert config_manager.load_config(window.config_path)["translation"]["target_language"] == saved
    monkeypatch.setattr(fluent_kit, "ask", lambda *_args: QtWidgets.QMessageBox.Yes)
    window._reset_defaults()
    assert window.target_language_input.value("zh-TW") == saved
    assert not window._dirty_pages
    window.target_language_input.set_value("Wolof")
    assert window._save_config_to_disk()
    assert config_manager.load_config(window.config_path)["translation"]["target_language"] == "Wolof"
    assert not window._dirty_pages


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
    monkeypatch.setattr(fluent_kit, "ask", forbidden)
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


def test_service_actions_have_one_inline_status_and_footer_save_all(window):
    window.tabs.setCurrentIndex(5)
    catalog = window.service_catalog
    assert catalog.save_btn.property("kind") != "secondary"
    assert catalog.discard_btn.property("kind") == catalog.test_btn.property("kind") == "secondary"
    assert not window.save_btn.isHidden() and window.save_btn.text() == "Save all"
    catalog.name_input.setText("Pending")
    assert catalog.editor_status.parent() is catalog.save_btn.parent()
    assert catalog.editor_status.objectName() == "mutedText"
    statuses = [label.text() for label in catalog.findChildren(QtWidgets.QLabel)
                if "Unsaved" in label.text()]
    assert statuses == ["Unsaved changes"]
    assert catalog.profile_list.item(0).text().endswith(" · Unsaved")
    assert window.dirty_label.text() == "Unsaved: Translation services"
    window.host_input.setText("127.0.0.2")
    assert window.dirty_label.text() == "Unsaved: Server, Translation services"
    catalog.discard_btn.click()
    window.tabs.setCurrentIndex(0)
    assert not window.save_btn.isHidden()


def test_backend_start_cancel_does_not_publish_drafts(window, monkeypatch):
    window.service_catalog.model_input.setText("draft")
    before = config_store.load_snapshot(window.config_path)[0]
    monkeypatch.setattr(window, "_validate_server_settings", lambda: True)
    monkeypatch.setattr(fluent_kit, "ask", lambda *_args: QtWidgets.QMessageBox.Cancel)
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

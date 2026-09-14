from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtWidgets

from gui import config_manager
from gui.app import MainWindow
from backend import config as backend_config, config_store


def _app() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    return app or QtWidgets.QApplication([])


def _window(tmp_path: Path) -> MainWindow:
    config = config_manager.load_default_config()
    config["stt"]["model_cache_dir"] = str(tmp_path / "models")
    config["translation"]["profiles"] = [
        {
            "id": "ollama-imported",
            "name": "Ollama office",
            "kind": "ollama",
            "host": "http://localhost:11434",
            "model": "gemma3:4b",
            "installed": True,
            "available": True,
        },
        {
            "id": "nllb-missing",
            "name": "NLLB missing",
            "kind": "nllb",
            "model": "facebook/nllb-200-distilled-600M",
            "installed": False,
            "available": False,
        },
    ]
    path = tmp_path / "config.yaml"
    config_manager.write_config(path, config)
    return MainWindow(path, auto_start=False, setup_tray=False)


def test_sidebar_stack_keeps_existing_page_contract_and_scrolls(tmp_path: Path) -> None:
    _app()
    window = _window(tmp_path)
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "Server",
        "Local translation",
        "STT models",
        "Tuning",
        "Settings",
        "Translation services",
    ]
    assert len(window.nav_buttons) == 6
    assert window.windowTitle() == "Live Subtitle"
    assert any(
        label.text() == "Live Subtitle"
        for label in window.findChildren(QtWidgets.QLabel)
    )
    assert window.tabs.findChildren(QtWidgets.QScrollArea)
    assert window.minimumWidth() == 900
    window.nav_buttons[4].click()
    assert window.tabs.currentIndex() == 5
    assert window.page_title.text() == "Translation services"
    window.close()


def test_extension_and_release_assets_use_live_subtitle_brand() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / "chrome-extension" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "Live Subtitle"
    for relative in (
        "chrome-extension/popup/popup.html",
        "chrome-extension/options/options.html",
    ):
        html = (root / relative).read_text(encoding="utf-8")
        assert "Live Subtitle" in html
        assert "STT Subtitle Capture" not in html

    release = (root / "build-release.ps1").read_text(encoding="utf-8")
    assert "Live-Subtitle-windows-x64.zip" in release
    assert '"live-subtitle.exe"' in release
    assert (root / "live-subtitle.spec").is_file()


def test_model_storage_is_shared_and_config_folder_button_is_accessible(
    tmp_path: Path, monkeypatch
) -> None:
    _app()
    window = _window(tmp_path)
    storage_root = tmp_path / "shared-model-storage"
    window.model_cache_input.setText(str(storage_root))
    window._model_storage_changed()

    updated = window._collect_config_from_ui()
    assert updated["models"]["root"] == str(storage_root)
    assert updated["stt"]["model_cache_dir"] == str(storage_root / "Speech models")
    assert updated["local_llama"]["root"] == str(
        storage_root / "Local Translation Models"
    )
    assert window.local_models.root == storage_root / "Local Translation Models"
    assert "Speech models:" in window.model_storage_paths.text()
    assert str(storage_root / "Speech models") in window.model_storage_paths.text()
    assert "Local translation models:" in window.model_storage_paths.text()
    assert str(storage_root / "Local Translation Models") in window.model_storage_paths.text()
    assert window.open_settings_folder_btn.accessibleName() == "Open settings folder"

    opened = []
    monkeypatch.setattr(
        QtGui.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    window.open_settings_folder_btn.click()
    assert [Path(path) for path in opened] == [window.config_path.parent]
    window.close()


def test_backend_honors_canonical_shared_model_root(tmp_path: Path) -> None:
    storage_root = tmp_path / "canonical-storage"
    path = tmp_path / "backend-config.yaml"
    config_manager.write_config(
        path,
        {
            "models": {"root": str(storage_root)},
            "stt": {"model_cache_dir": str(tmp_path / "stale-stt")},
            "local_llama": {"root": str(tmp_path / "stale-local")},
        },
    )

    loaded = backend_config.load_config(str(path))
    assert loaded["stt"]["model_cache_dir"] == str(storage_root / "Speech models")
    assert loaded["local_llama"]["root"] == str(
        storage_root / "Local Translation Models"
    )


def test_new_appdata_path_copies_legacy_config_without_moving_models(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    legacy_dir = tmp_path / "STT-TTS"
    legacy_models = legacy_dir / "models"
    legacy_config = legacy_dir / "config.yaml"
    config_manager.write_config(
        legacy_config,
        {
            "stt": {"model_cache_dir": str(legacy_models)},
            "local_llama": {"root": str(legacy_dir / "managed-models")},
        },
    )

    migrated = config_manager.ensure_user_config()
    assert migrated == tmp_path / "Live Subtitle" / "config.yaml"
    assert migrated.is_file()
    assert legacy_config.is_file()
    copied = config_manager.load_config(migrated)
    assert copied["stt"]["model_cache_dir"] == str(legacy_models)
    assert copied["local_llama"]["root"] == str(legacy_dir / "managed-models")
    assert config_manager.get_default_model_storage_dir() == tmp_path / "Live Subtitle"


def test_fresh_backend_default_uses_live_subtitle_appdata(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = tmp_path / "minimal-config.yaml"
    config_manager.write_config(path, {"stt": {"model_cache_dir": ""}})

    loaded = backend_config.load_config(str(path))
    assert loaded["stt"]["model_cache_dir"] == str(
        tmp_path / "Live Subtitle" / "Speech models"
    )


def test_translation_pages_explain_runtime_vs_service_profiles(tmp_path: Path) -> None:
    _app()
    window = _window(tmp_path)
    assert "MANAGED RUNTIME" in window.local_models.controls["details"].text()
    subtitles = window.service_catalog.findChildren(QtWidgets.QLabel, "mutedText")
    assert any("CONFIGURATION ONLY" in label.text() for label in subtitles)
    assert window.service_catalog.catalog_status.text() == "Connections & adapters"
    window.close()


def test_theme_preference_is_saved_and_service_catalog_preserves_kinds(tmp_path: Path) -> None:
    _app()
    window = _window(tmp_path)
    assert window.service_catalog.profile_list.count() == 2
    assert "OLLAMA" in window.service_catalog.profile_list.item(0).text()
    assert "NLLB" in window.service_catalog.profile_list.item(1).text()

    window._set_theme_mode("dark", persist=True)
    assert config_manager.load_config(window.config_path)["gui"]["theme"] == "dark"
    window.service_catalog.profile_list.setCurrentRow(0)
    window.service_catalog.model_input.setText("gemma3:12b")
    updated = window._collect_config_from_ui()
    profiles = updated["translation"]["profiles"]
    assert {profile["id"] for profile in profiles} == {"ollama-imported", "nllb-missing"}
    assert next(profile for profile in profiles if profile["id"] == "ollama-imported")["model"] == "gemma3:12b"
    assert updated["translation"]["engine"] == "noop"
    window.close()


def test_system_theme_can_reapply_after_palette_change_without_starting_backend(tmp_path: Path) -> None:
    _app()
    window = _window(tmp_path)
    window._set_theme_mode("system", persist=False)
    event = QtCore.QEvent(QtCore.QEvent.ApplicationPaletteChange)
    QtWidgets.QApplication.sendEvent(window, event)
    assert window.backend_runner is None
    assert window._theme_mode() == "system"
    window.close()


def test_stale_gui_save_is_rejected_without_clobbering_external_profile(tmp_path: Path) -> None:
    _app()
    window = _window(tmp_path)
    original_revision = window.config_revision
    external = config_store.load_snapshot(window.config_path)[0]
    external.setdefault("translation", {})["service_profiles"] = [
        {
            "id": "imported-after-open",
            "name": "Imported while editing",
            "engine": "ollama",
            "base_url": "http://localhost:11434",
            "endpoint": "http://localhost:11434",
            "model": "llama3.2",
        }
    ]
    config_store.save_config(window.config_path, external, expected_revision=original_revision)
    window.host_input.setText("127.0.0.2")
    assert window._save_config_to_disk() is False
    current = config_store.load_snapshot(window.config_path)[0]
    assert current["translation"]["service_profiles"][0]["id"] == "imported-after-open"
    assert window.config["server"]["host"] != "127.0.0.2"
    window.close()


def test_noop_default_does_not_infer_vllm_and_profile_selection_is_explicit(tmp_path: Path) -> None:
    _app()
    window = _window(tmp_path)
    assert all(not window.service_catalog._profiles[i].get("_default") for i in range(2))
    untouched = window._collect_config_from_ui()
    assert untouched["translation"]["engine"] == "noop"
    assert "default_selection" not in untouched["translation"]

    window.service_catalog.profile_list.setCurrentRow(0)
    window.service_catalog.default_check.setChecked(True)
    selected = window._collect_config_from_ui()
    assert selected["translation"]["default_selection"] == {
        "kind": "profile",
        "id": "ollama-imported",
    }

    window.service_catalog.default_check.setChecked(False)
    cleared = window._collect_config_from_ui()
    assert cleared["translation"]["default_selection"] == {"kind": "none", "id": "none"}

    while window.service_catalog.profile_list.count():
        window.service_catalog.profile_list.setCurrentRow(0)
        window.service_catalog.remove_btn.click()
    assert window.service_catalog.profile_list.count() == 0
    empty = window._collect_config_from_ui()
    assert empty["translation"]["default_selection"] == {"kind": "none", "id": "none"}
    window.service_catalog.load_from_config(empty)
    assert window.service_catalog.profile_list.count() == 0
    window.close()


def test_api_profile_metadata_survives_edit_and_vllm_keeps_auth_key(tmp_path: Path) -> None:
    _app()
    window = _window(tmp_path)
    catalog = window.service_catalog
    catalog.profile_list.setCurrentRow(0)
    catalog.kind_combo.setCurrentIndex(catalog.kind_combo.findData("api"))
    catalog.api_type_combo.setCurrentText("Chat Completions")
    standard = window._collect_config_from_ui()["translation"]["profiles"][0]
    assert standard["api_type"] == "chat_completions"
    catalog.api_type_combo.setCurrentText("custom_protocol")
    catalog.auto_complete_check.setChecked(False)
    catalog.api_key_input.setText("api-secret")
    api = window._collect_config_from_ui()["translation"]["profiles"][0]
    assert api["api_type"] == "custom_protocol"
    assert api["auto_complete_endpoint"] is False
    assert api["api_key"] == "api-secret"

    catalog.kind_combo.setCurrentIndex(catalog.kind_combo.findData("vllm"))
    catalog.api_key_input.setText("vllm-secret")
    vllm = window._collect_config_from_ui()["translation"]["profiles"][0]
    assert vllm["api_key"] == "vllm-secret"
    window.close()

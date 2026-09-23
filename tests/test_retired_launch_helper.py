"""The retired launcher must not reappear through saved profiles or defaults."""
from __future__ import annotations

import copy
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from backend.config_store import service_profiles
from backend.translation_service import resolve_selection
from backend.translator import resolve_provider_config
from gui import config_manager
from gui.app import MainWindow
from gui.service_catalog_widget import ServiceCatalogWidget


def _legacy_config():
    return {"translation": {
        "engine": "vllm",
        "vllm": {"active_profile": "saved-api", "profiles": [{
            "id": "saved-api", "name": "My API", "mode": "local",
            "model": "G:/weights/model", "served_model": "translation-model",
            "base_url": "http://127.0.0.1:8000/custom/responses",
            "api_key": "preserved-test-key", "api_type": "responses",
            "auto_complete_endpoint": False, "max_concurrency": 3,
            "runtime": {"max_model_len": 4096},
            "options": {"runtime": {"max_num_seqs": 16}, "vendor": "keep"},
        }]},
    }}


def test_legacy_connection_uses_http_model_and_preserves_request_contract():
    config = _legacy_config()
    original = copy.deepcopy(config)
    profile, = service_profiles(config)
    assert profile["engine"] == "openai_compatible"
    assert profile["mode"] == "external"
    assert profile["model"] == "translation-model"
    assert "runtime" not in profile and "runtime" not in profile["options"]
    assert profile["options"]["vendor"] == "keep"
    resolved, _ = resolve_selection(config, {})
    provider = resolve_provider_config(resolved)
    assert provider["profile_id"] == "saved-api"
    assert provider["model"] == "translation-model"
    assert provider["api_key"] == "preserved-test-key"
    assert provider["api_type"] == "responses"
    assert provider["auto_complete_endpoint"] is False
    assert provider["base_url"] == "http://127.0.0.1:8000/custom/responses"
    assert config == original


def test_services_page_has_one_scroll_area_and_saves_without_launcher(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = config_manager.load_default_config()
    assert "vllm" not in config["translation"]
    config["translation"] = _legacy_config()["translation"]
    config["models"] = {"root": str(tmp_path)}
    config["stt"]["model_cache_dir"] = str(tmp_path / "Speech models")
    path = tmp_path / "config.yaml"
    config_manager.write_config(path, config)
    window = MainWindow(path, auto_start=False, setup_tray=False)
    try:
        page = window.tabs.widget(window.tabs.count() - 1)
        assert isinstance(page, QtWidgets.QScrollArea)
        assert page.findChildren(QtWidgets.QScrollArea) == []
        assert not hasattr(window, "vllm_settings")
        assert window.service_catalog.kind_combo.findData("vllm") == -1
        assert window.service_catalog.kind_combo.currentData() == "api"
        window.service_catalog.name_input.setText("Renamed API")
        assert window._save_config_to_disk()
        saved = config_manager.load_config(path)
        assert "vllm" not in saved["translation"]
        assert saved["translation"]["default_selection"] == {"kind": "profile", "id": "saved-api"}
        resolved, _ = resolve_selection(saved, {})
        provider = resolve_provider_config(resolved)
        assert provider["model"] == "translation-model"
        assert provider["api_key"] == "preserved-test-key"
        window._load_config_into_ui(saved)
        assert window.service_catalog.profile_list.count() == 1
        assert window.service_catalog.name_input.text() == "Renamed API"
        window.service_catalog.remove_btn.click()
        empty = window._collect_config_from_ui()
        window._load_config_into_ui(empty)
        assert window.service_catalog.profile_list.count() == 0
        assert service_profiles(empty) == []
    finally:
        window.close()


@pytest.mark.parametrize("shape", ["flat", "flat-with-id", "missing-active", "canonical"])
def test_old_connection_shapes_keep_the_saved_default(shape):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = _legacy_config()
    translation = config["translation"]
    profile = translation["vllm"]["profiles"][0]
    expected_id = profile["id"]
    if shape in {"flat", "flat-with-id"}:
        translation["vllm"] = dict(profile)
        if shape == "flat":
            translation["vllm"].pop("id")
            expected_id = "legacy-api"
    elif shape == "missing-active":
        translation["vllm"].pop("active_profile")
    else:
        translation["service_profiles"] = [{**profile, "engine": "vllm", "kind": "api"}]
    editor = ServiceCatalogWidget()
    try:
        editor.load_from_config(config)
        saved = editor.collect_into_config(config)
        assert "vllm" not in saved["translation"]
        assert saved["translation"]["default_selection"] == {"kind": "profile", "id": expected_id}
        resolved, _ = resolve_selection(saved, {})
        provider = resolve_provider_config(resolved)
        assert provider["model"] == "translation-model"
        assert provider["api_key"] == "preserved-test-key"
        assert "runtime" not in saved["translation"]["service_profiles"][0]
    finally:
        editor.close()

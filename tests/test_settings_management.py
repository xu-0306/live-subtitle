from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import server, settings_management
from backend.config_store import RevisionConflictError, load_snapshot, save_config
from backend.translation_service import TranslationService


def _fixture(tmp_path: Path) -> tuple[Path, TranslationService]:
    speech = tmp_path / "Speech models"
    speech.mkdir()
    (speech / "small.pt").write_bytes(b"speech fixture")
    root = tmp_path / "Local Translation Models"
    path = tmp_path / "config.yaml"
    save_config(path, {
        "server": {"host": "127.0.0.1", "port": 8765},
        "stt": {"model": "small", "model_cache_dir": str(speech)},
        "translation": {"engine": "noop", "target_language": "zh-TW", "service_profiles": []},
        "local_llama": {"root": str(root), "installed_models": []},
    })
    service = TranslationService(lambda: load_snapshot(path)[0], root, config_path=path,
                                 server_id="test-server", instance_id="test-instance")
    return path, service


def test_management_profile_crud_and_revision_keep_gui_snapshot_in_sync(tmp_path: Path) -> None:
    path, service = _fixture(tmp_path)
    first = settings_management.snapshot(path, service)
    revision = first["revision"]
    revision = settings_management.mutate(path, service, "save_profile", {"profile": {
        "id": "office", "name": "Office API", "engine": "openai_compatible",
        "model": "translator", "base_url": "http://127.0.0.1:8000/v1", "api_key": "private-key",
    }}, revision)
    visible = settings_management.snapshot(path, service)
    assert visible["revision"] == revision
    assert visible["profiles"][0]["has_api_key"] is True
    assert [item["id"] for item in visible["models"]] == ["profile:office"]
    assert "private-key" not in json.dumps(visible)
    with pytest.raises(RevisionConflictError):
        settings_management.mutate(path, service, "delete_profile", {"id": "office"}, first["revision"])

    revision = settings_management.mutate(path, service, "save_profile", {"profile": {
        "id": "office", "name": "Office revised", "engine": "openai_compatible",
        "model": "translator-v2", "base_url": "http://127.0.0.1:8001/v1", "api_key": "",
    }}, revision)
    config, gui_revision = load_snapshot(path)
    assert gui_revision == revision
    profile = config["translation"]["service_profiles"][0]
    assert profile["api_key"] == "private-key"
    assert profile["base_url"] == "http://127.0.0.1:8001/v1"
    revision = settings_management.mutate(path, service, "save_profile", {"profile": {
        "id": "office", "name": "Office revised", "engine": "openai_compatible",
        "model": "translator-v2", "base_url": "http://127.0.0.1:8001/v1",
    }, "clear_api_key": True}, revision)
    assert load_snapshot(path)[0]["translation"]["service_profiles"][0]["api_key"] == ""
    revision = settings_management.mutate(path, service, "set_defaults", {
        "translation_selection": {"kind": "profile", "id": "office"},
        "target_language": "ja", "partial": True,
    }, revision)
    revision = settings_management.mutate(path, service, "delete_profile", {"id": "office"}, revision)
    config, _ = load_snapshot(path)
    assert config["translation"]["service_profiles"] == []
    assert config["translation"]["default_selection"] == {"kind": "none", "id": "none"}


def test_management_websocket_exposes_same_revision_and_rejects_web_origin(tmp_path: Path, monkeypatch) -> None:
    path, _ = _fixture(tmp_path)
    monkeypatch.setenv("STT_CONFIG_PATH", str(path))
    monkeypatch.setenv("STT_INSTANCE_ID", "test-instance")
    with TestClient(server.app, client=("127.0.0.1", 55102)) as client:
        with client.websocket_connect("/asr", headers={"origin": "chrome-extension://example"}) as websocket:
            websocket.send_json({"type": "desktop_management", "version": 1, "action": "read"})
            result = websocket.receive_json()
        assert result["ok"] and result["revision"] == load_snapshot(path)[1]
        with client.websocket_connect("/asr", headers={"origin": "https://example.com"}) as websocket:
            websocket.send_json({"type": "desktop_management", "version": 1, "action": "read"})
            rejected = websocket.receive_json()
        assert rejected["ok"] is False


def test_management_reports_a_corrupt_config_without_quoting_it(tmp_path: Path, monkeypatch) -> None:
    path, _ = _fixture(tmp_path)
    monkeypatch.setenv("STT_CONFIG_PATH", str(path))
    with TestClient(server.app, client=("127.0.0.1", 55102)) as client:
        path.write_text("translation:\n  api_key: sk-private\n  broken: [unclosed\n", encoding="utf-8")
        with client.websocket_connect("/asr", headers={"origin": "chrome-extension://example"}) as socket:
            socket.send_json({"type": "desktop_management", "version": 1, "action": "read"})
            result = socket.receive_json()
    assert result["ok"] is False
    assert "not valid YAML" in result["error"]
    assert "sk-private" not in result["error"]


def test_management_generates_service_id_and_saves_detected_format_for_gui(tmp_path: Path, monkeypatch) -> None:
    path, _ = _fixture(tmp_path)
    monkeypatch.setenv("STT_CONFIG_PATH", str(path))

    async def detect(profile, client):
        assert profile["id"].startswith("service-")
        assert profile["api_key"] == "private-key"
        return "responses", None

    monkeypatch.setattr(server.service_probe, "detect_api_type", detect)
    with TestClient(server.app, client=("127.0.0.1", 55102)) as client:
        request = {"type": "desktop_management", "version": 1,
                   "action": "save_profile", "expected_revision": load_snapshot(path)[1],
                   "data": {"profile": {"name": "Office API", "engine": "openai_compatible",
                                        "model": "translator", "base_url": "http://127.0.0.1:8000/v1",
                                        "api_key": "private-key"}}}
        with client.websocket_connect("/asr", headers={"origin": "chrome-extension://example"}) as socket:
            socket.send_json(request)
            result = socket.receive_json()
    assert result["ok"] is True
    profile_id = result["profiles"][0]["id"]
    assert profile_id.startswith("service-")
    assert result["saved_profile_id"] == profile_id
    assert result["profiles"][0]["api_type"] == "responses"
    assert "private-key" not in json.dumps(result)
    config, revision = load_snapshot(path)
    assert revision == result["revision"]
    assert config["translation"]["service_profiles"][0]["id"] == profile_id
    assert config["translation"]["service_profiles"][0]["api_type"] == "responses"


def test_management_does_not_save_when_all_api_formats_fail(tmp_path: Path, monkeypatch) -> None:
    path, _ = _fixture(tmp_path)
    monkeypatch.setenv("STT_CONFIG_PATH", str(path))
    before, revision = load_snapshot(path)

    async def detect(profile, client):
        raise ValueError("All three API formats failed. Check the endpoint, model and API key.")

    monkeypatch.setattr(server.service_probe, "detect_api_type", detect)
    with TestClient(server.app, client=("127.0.0.1", 55102)) as client:
        with client.websocket_connect("/asr", headers={"origin": "chrome-extension://example"}) as socket:
            socket.send_json({"type": "desktop_management", "version": 1,
                              "action": "save_profile", "expected_revision": revision,
                              "data": {"profile": {"name": "Broken API", "engine": "openai_compatible",
                                                   "model": "translator", "base_url": "http://127.0.0.1:8000/v1"}}})
            result = socket.receive_json()
    assert result["ok"] is False
    assert "All three API formats failed" in result["error"]
    assert load_snapshot(path) == (before, revision)


def test_management_refuses_a_key_typed_into_the_service_url(tmp_path: Path) -> None:
    path, service = _fixture(tmp_path)
    revision = settings_management.snapshot(path, service)["revision"]
    with pytest.raises(ValueError, match="API key field"):
        settings_management.mutate(path, service, "save_profile", {"profile": {
            "id": "office", "name": "Office", "engine": "openai_compatible", "model": "m",
            "base_url": "https://sk-secret@host.example/v1"}}, revision)
    assert load_snapshot(path)[0]["translation"]["service_profiles"] == []

from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from backend import server
from backend.config_store import save_config
from backend.translation_service import TranslationService


def _config(model_dir: Path) -> dict:
    return {
        "server": {"host": "127.0.0.1", "port": 8765},
        "stt": {"model": "custom-interview", "model_cache_dir": str(model_dir), "backend": "auto"},
        "translation": {
            "engine": "noop",
            "target_language": "Kiswahili",
            "partial": True,
            "service_profiles": [
                {"id": "ollama", "name": "Ollama", "engine": "ollama", "model": "future-model", "host": "http://127.0.0.1:11434"},
                {"id": "nllb", "name": "NLLB", "engine": "nllb", "model": "facebook/nllb-200-distilled-600M"},
            ],
        },
        "local_llama": {"root": str(model_dir / "managed"), "installed_models": []},
    }


def test_catalog_reports_filesystem_stt_and_provider_status_without_secrets(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "custom-interview.pt").write_bytes(b"fixture")
    config = _config(model_dir)
    config["translation"]["service_profiles"][0]["api_key"] = "catalog-secret"
    config["translation"]["service_profiles"].append(
        {"id": "broken-profile", "name": "Needs repair", "engine": "future-provider"}
    )
    service = TranslationService(lambda: copy.deepcopy(config), tmp_path / "managed")

    catalog = service.desktop_catalog(server_id="cfg-one", instance_id="instance-one")
    assert catalog["type"] == "desktop_catalog"
    assert catalog["server_id"] == "cfg-one"
    assert catalog["instance_id"] == "instance-one"
    custom = next(item for item in catalog["stt_models"] if item["id"] == "custom-interview")
    assert custom["available"] and custom["status"] == "installed"
    assert all("qwen" not in item["id"].lower() for item in catalog["stt_models"])
    assert catalog["defaults"]["translation"] == {
        "target_language": "Kiswahili",
        "partial": True,
    }
    broken = next(item for item in catalog["models"] if item["id"] == "profile:broken-profile")
    assert broken["available"] is False
    assert broken["status"] == "invalid"
    assert broken["reason"]
    encoded = json.dumps(catalog)
    assert "catalog-secret" not in encoded
    assert "127.0.0.1:11434" not in encoded


def test_management_websocket_returns_v2_catalog_and_health_identity(tmp_path: Path, monkeypatch) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "custom-interview.pt").write_bytes(b"fixture")
    config_path = tmp_path / "config.yaml"
    save_config(config_path, _config(model_dir))
    monkeypatch.setenv("STT_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("STT_INSTANCE_ID", "test-instance")

    with TestClient(server.app, client=("127.0.0.1", 55101)) as client:
        health = client.get("/health").json()
        assert health == {"service": "stt-tts", "instance_id": "test-instance", "ready": True}
        with client.websocket_connect("/asr") as websocket:
            websocket.send_json({"type": "desktop_catalog", "version": 2})
            catalog = websocket.receive_json()
        assert catalog["version"] == 2
        assert catalog["instance_id"] == "test-instance"
        assert any(item["id"] == "custom-interview" for item in catalog["stt_models"])


def test_v2_accepts_explicit_whisper_backend_for_available_model(tmp_path: Path, monkeypatch) -> None:
    """An available standard model may be selected with its explicit backend."""

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "small.pt").write_bytes(b"fixture")
    config = _config(model_dir)
    config["stt"].update(model="small", backend="whisper")
    service = TranslationService(lambda: copy.deepcopy(config), tmp_path / "managed")
    monkeypatch.setattr(server.app.state, "translation_service", service, raising=False)
    monkeypatch.setattr(server.app.state, "server_id", "test-server", raising=False)
    monkeypatch.setattr(server.app.state, "instance_id", "test-instance", raising=False)

    server._validate_v2_stt_selection(config["stt"], config, 1)


def test_catalog_honors_configured_stt_path_for_builtin_model(tmp_path: Path) -> None:
    cache_dir = tmp_path / "empty-cache"
    cache_dir.mkdir()
    external_model = tmp_path / "fixtures" / "small-whisper.pt"
    external_model.parent.mkdir()
    external_model.write_bytes(b"fixture")
    config = _config(cache_dir)
    config["stt"].update(model="small", backend="whisper", model_path=str(external_model))

    catalog = TranslationService(lambda: copy.deepcopy(config), tmp_path / "managed").desktop_catalog()
    small = next(item for item in catalog["stt_models"] if item["id"] == "small")
    assert small["available"] is True
    assert small["selection"] == {"model": "small", "backend": "whisper"}


def test_slow_stt_constructor_does_not_block_loop_and_cleans_late_result(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class Engine:
        def close(self):
            closed.set()

    def slow_constructor(**kwargs):
        entered.set()
        release.wait(2)
        return Engine()

    monkeypatch.setattr(server, "TranscriptionEngine", slow_constructor)

    async def scenario() -> None:
        task = asyncio.create_task(server._construct_stt_engine({"model_size": "small"}))
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(asyncio.sleep(0.05), 0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        release.set()
        assert await asyncio.to_thread(closed.wait, 3)

    asyncio.run(scenario())


def test_engine_startup_preserves_existing_audio_and_cleans_once_on_disconnect(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    close_count = 0

    class Engine:
        def close(self):
            nonlocal close_count
            close_count += 1

    def slow_constructor(**kwargs):
        entered.set()
        release.wait(2)
        return Engine()

    class Socket:
        def __init__(self):
            self.state = SimpleNamespace(startup_messages=[{"bytes": b"early"}])
            self.messages = [{"bytes": b"new"}, {"type": "websocket.disconnect", "code": 1000}]

        async def receive(self):
            await asyncio.sleep(0)
            return self.messages.pop(0)

    monkeypatch.setattr(server, "TranscriptionEngine", slow_constructor)

    async def scenario() -> None:
        websocket = Socket()
        task = asyncio.create_task(
            server._construct_stt_engine_for_socket(websocket, {"model_size": "small"})
        )
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(WebSocketDisconnect):
            await task
        release.set()
        await asyncio.sleep(0.1)
        assert websocket.state.startup_messages == [{"bytes": b"early"}, {"bytes": b"new"}]
        assert close_count == 1

    asyncio.run(scenario())


def test_stt_factory_does_not_reuse_whisperlivekit_singleton_between_models(monkeypatch) -> None:
    class SingletonEngine:
        _instance = None
        _initialized = False

        def __new__(cls, **kwargs):
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

        def __init__(self, **kwargs):
            if type(self)._initialized:
                return
            self.model_size = kwargs["model_size"]
            type(self)._initialized = True

    monkeypatch.setattr(server, "TranscriptionEngine", SingletonEngine)

    async def scenario() -> None:
        first = await server._construct_stt_engine({"model_size": "small"})
        second = await server._construct_stt_engine({"model_size": "medium"})
        assert first is not second
        assert first.model_size == "small"
        assert second.model_size == "medium"

    asyncio.run(scenario())

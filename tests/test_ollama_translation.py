from __future__ import annotations

import asyncio
import copy
import json
import time
from types import SimpleNamespace
from pathlib import Path

import httpx
import ollama
import pytest
from fastapi.testclient import TestClient

from backend import server, settings_management
from backend.config_store import load_snapshot, normalize_service_profile, save_config
from backend.translation_scheduler import TranslationScheduler
from backend.translation_service import TranslationService, resolve_selection
from backend.translator import OllamaTranslator, build_translator


HOST = "http://100.82.191.113:11434"
MODEL = "qwen3.5:9b"


def profile(**overrides):
    return {"id": "ollama-default", "engine": "ollama", "model": MODEL,
            "host": "http://localhost:11434", "base_url": HOST, "endpoint": HOST,
            **overrides}


@pytest.fixture
def mock_ollama(monkeypatch):
    original = ollama.Client
    clients = []

    def install(handler):
        def create(**kwargs):
            client = original(**kwargs, transport=httpx.MockTransport(handler))
            clients.append(client)
            return client
        monkeypatch.setattr(ollama, "Client", create)
        return clients

    yield install
    for client in clients:
        client._client.close()


@pytest.mark.parametrize("fields", [
    {},
    {"base_url": ""},
    {"ollama": {"host": "http://localhost:11434"}},
    {"base_url": "", "endpoint": "", "host": HOST},
    {"base_url": "", "endpoint": "", "host": "", "ollama": {"host": HOST}},
])
def test_edited_endpoint_wins_and_normalization_is_idempotent(fields):
    raw = profile(**fields)
    original = copy.deepcopy(raw)
    normalized = normalize_service_profile(raw)
    assert normalized["base_url"] == normalized["endpoint"] == HOST
    assert "host" not in normalized
    assert normalize_service_profile(normalized) == normalized
    assert raw == original


def test_management_save_removes_stale_host(tmp_path):
    path = tmp_path / "config.yaml"
    revision = save_config(path, {"translation": {"service_profiles": [profile()]}})
    service = TranslationService(lambda: load_snapshot(path)[0], tmp_path)
    settings_management.mutate(path, service, "save_profile", {
        "profile": {"id": "ollama-default", "base_url": HOST + "/"},
    }, revision)
    saved = load_snapshot(path)[0]["translation"]["service_profiles"][0]
    assert saved["base_url"] == saved["endpoint"] == HOST + "/"
    assert "host" not in saved


def test_chat_disables_thinking_and_bounds_http_timeout(mock_ollama):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": " translated "}})

    clients = mock_ollama(handle)
    translator = OllamaTranslator(MODEL, HOST, "zh-TW", keep_alive="5m", timeout_sec=8)
    assert translator.initial_timeout_sec == 60
    assert translator.translate("Hello", "en") == "translated"
    assert translator.translate("Hello", "en") == "translated"
    assert translator.initial_timeout_sec == 0
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["think"] is False
    assert body["stream"] is False
    assert body["keep_alive"] == "5m"
    assert body["model"] == MODEL
    assert str(requests[0].url) == HOST + "/api/chat"
    assert clients[0]._client.timeout.read == 60
    assert clients[0]._client.timeout.connect == 8


def test_old_client_without_think_or_keep_alive_still_translates(monkeypatch):
    class OldClient:
        def __init__(self, **kwargs):
            assert kwargs["timeout"].read == 60

        def chat(self, model, messages):
            assert model == MODEL
            return {"message": {"content": "translated"}}

    monkeypatch.setattr(ollama, "Client", OldClient)
    translator = OllamaTranslator(MODEL, HOST, "zh-TW", keep_alive="5m")
    assert translator.translate("Hello", "en") == "translated"


@pytest.mark.parametrize("model", [MODEL, "gemma3"])
def test_health_requests_tags_and_checks_selected_model(mock_ollama, model):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"models": [{"name": MODEL, "model": MODEL}, {"name": "gemma3:latest", "model": "gemma3:latest"}]})

    mock_ollama(handle)
    translator = OllamaTranslator(model, HOST, "zh-TW")
    health = asyncio.run(translator.health_check())
    assert health == {"ok": True, "models": [MODEL, "gemma3:latest"], "url": HOST + "/api/tags"}
    assert [(r.method, str(r.url)) for r in requests] == [("GET", HOST + "/api/tags")]
    assert translator.initial_timeout_sec == 60


@pytest.mark.parametrize("failure, reason", [
    (None, "not pulled"),
    (httpx.ConnectError, "unreachable"),
    (httpx.ReadTimeout, "timed out"),
])
def test_health_reports_missing_model_connection_and_timeout(mock_ollama, failure, reason):
    def handle(request):
        if failure:
            raise failure("test failure", request=request)
        return httpx.Response(200, json={"models": []})

    mock_ollama(handle)
    health = asyncio.run(OllamaTranslator(MODEL, HOST, "zh-TW").health_check())
    assert health["ok"] is False
    assert HOST in health["error"]
    assert reason in health["error"]


def test_app_path_allows_cold_load_then_restores_subtitle_timeout(mock_ollama):
    requests = []

    def handle(request):
        time.sleep(0.08)
        requests.append(request)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "translated"}})

    mock_ollama(handle)
    config = {"translation": {"timeout_sec": 0.01, "target_language": "zh-TW",
                              "ollama": {"keep_alive": "5m"}, "service_profiles": [profile()]}}

    async def run():
        service = TranslationService(lambda: config, root=Path("."))
        cfg = await service.acquire("test", {"selection": {"kind": "profile", "id": "ollama-default"}}, {})
        translator = build_translator(cfg)
        session = server.TranslationSession(cfg, "en", translator, TranslationScheduler())
        messages = []

        async def send_text(raw):
            messages.append(json.loads(raw))

        websocket = SimpleNamespace(send_text=send_text)
        assert await server._translate_text(session, "cold request", "en", websocket) == "translated"
        assert await server._translate_text(session, "warm request", "en", websocket) == ""
        await asyncio.gather(*tuple(session.background_provider_tasks))
        assert session.scheduler.metrics.completed == 2
        assert session.scheduler.metrics.active == 0
        assert not messages

    asyncio.run(run())
    assert len(requests) == 2
    for request in requests:
        assert str(request.url) == HOST + "/api/chat"
        body = json.loads(request.content)
        assert body["think"] is False
        assert body["keep_alive"] == "5m"


@pytest.mark.parametrize("available", [True, False])
def test_initial_websocket_health_reaches_remote_without_loading_stt(tmp_path, monkeypatch, mock_ollama, available):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"models": [{"name": MODEL, "model": MODEL}] if available else []})

    mock_ollama(handle)
    cfg = {"translation": {"engine": "noop", "service_profiles": [profile()]},
           "stt": {"model": "medium", "model_cache_dir": str(tmp_path / "missing-stt")},
           "local_llama": {"root": str(tmp_path)}}
    monkeypatch.setattr(server, "load_config", lambda: copy.deepcopy(cfg))

    def forbidden(*args, **kwargs):
        raise AssertionError("Connection tests must not initialize STT")

    monkeypatch.setattr(server, "TranscriptionEngine", forbidden)
    with TestClient(server.app, client=("127.0.0.1", 55001)) as client:
        with client.websocket_connect("/asr") as websocket:
            websocket.send_json({"type": "test", "target": "translation", "action": "health",
                                 "version": 2, "translation": {
                                     "selection": {"kind": "profile", "id": "ollama-default"}}})
            while True:
                response = websocket.receive_json()
                if response["type"] != "status":
                    break
            if available:
                assert response["ok"] is True
                assert MODEL in response["message"]
            else:
                assert response["type"] == "error"
                assert "not pulled" in response["message"]
    assert [(r.method, str(r.url)) for r in requests] == [("GET", HOST + "/api/tags")]

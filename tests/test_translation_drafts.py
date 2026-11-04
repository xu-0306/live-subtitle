from __future__ import annotations

import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import server
from backend.config_store import load_snapshot, save_config


@pytest.fixture
def draft_backend(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    config = {
        "translation": {"engine": "noop", "target_language": "zh-TW", "service_profiles": [
            {"id": "saved", "name": "Office", "engine": "openai_compatible",
             "base_url": "https://saved.test/v1", "model": "saved-model", "api_key": "stored-secret"},
            {"id": "other", "name": "Office", "engine": "openai_compatible",
             "base_url": "https://other.test/v1", "model": "other-model", "api_key": "other-secret"},
        ]},
        "stt": {"model": "medium", "model_cache_dir": str(tmp_path / "missing-stt")},
        "local_llama": {"root": str(tmp_path / "managed")},
    }
    save_config(path, config)
    monkeypatch.setenv("STT_CONFIG_PATH", str(path))
    def forbidden(*_args, **_kwargs):
        pytest.fail("Translation tests must never initialize STT")
    monkeypatch.setattr(server, "TranscriptionEngine", forbidden)
    return path, config


def _request(client, draft, origin=None):
    responses = []
    with client.websocket_connect("/asr", headers={"origin": origin} if origin else {}) as ws:
        ws.send_json({"type": "test", "target": "translation", "version": 2,
                      "action": "translate", "draft_profile": draft})
        while True:
            response = ws.receive_json()
            responses.append(response)
            if response["type"] != "status":
                return response, json.dumps(responses)


@pytest.mark.parametrize("failure", [None, "models", "translate"])
@pytest.mark.parametrize("identity,key,expected_key", [
    ("saved", "", "stored-secret"), ("saved", "draft-secret", "draft-secret"), ("new", "", ""),
])
def test_draft_tests_are_read_only_and_reuse_keys_by_id(draft_backend, monkeypatch, failure, identity, key, expected_key):
    path, config = draft_backend
    before = {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    snapshot = load_snapshot(path)
    original = copy.deepcopy(config)
    requests = []
    def handle(request):
        requests.append(request)
        if request.method == "GET":
            assert str(request.url) == "https://draft.test/v1/models"
            return httpx.Response(503 if failure == "models" else 200, json={"data": [{"id": "draft-model"}]})
        body = json.loads(request.content)
        assert body["model"] == "draft-model"
        assert any("Hello world" in message["content"] for message in body["messages"])
        return httpx.Response(503 if failure == "translate" else 200,
                              json={"choices": [{"message": {"content": "你好世界"}}]})
    draft = {"id": identity, "name": "Office", "engine": "openai_compatible",
             "base_url": "https://draft.test/v1", "model": "draft-model", "api_key": key}
    with TestClient(server.app, client=("127.0.0.1", 55102)) as client:
        with monkeypatch.context() as patch, client.portal.wrap_async_context_manager(
            httpx.AsyncClient(transport=httpx.MockTransport(handle))
        ) as provider:
            patch.setattr(server.app.state, "http_client", provider)
            response, encoded = _request(client, draft, "chrome-extension://test")
        assert response["type"] == ("error" if failure else "test_result")
        if not failure:
            assert response["ok"] is True and response["message"] == "你好世界"
        assert all(secret not in encoded for secret in ("stored-secret", "other-secret", "draft-secret"))
        assert server.app.state.translation_service.get_config() == snapshot[0]
    assert [request.method for request in requests] == (["GET"] if failure == "models" else ["GET", "POST"])
    assert all(request.headers.get("authorization", "") == (f"Bearer {expected_key}" if expected_key else "")
               for request in requests)
    assert load_snapshot(path) == snapshot
    assert {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()} == before
    assert config == original


@pytest.mark.parametrize("origin", ["https://evil.test", "http://localhost:3000", "null"])
def test_draft_tests_reject_untrusted_origins(draft_backend, monkeypatch, origin):
    path, _ = draft_backend
    before = load_snapshot(path)
    async def forbidden(*_args, **_kwargs):
        pytest.fail("Untrusted drafts must be rejected before acquiring a provider")
    with TestClient(server.app, client=("127.0.0.1", 55102)) as client:
        monkeypatch.setattr(server.app.state.translation_service, "acquire", forbidden)
        response, encoded = _request(client, {"id": "saved", "engine": "openai"}, origin)
    assert response["type"] == "error" and "trusted local client" in response["message"]
    assert "stored-secret" not in encoded
    assert load_snapshot(path) == before


@pytest.mark.parametrize("draft", [None, [], {"engine": "unknown"}, {"engine": "openai", "id": "invalid id"}])
def test_draft_tests_validate_profiles(draft_backend, draft):
    path, _ = draft_backend
    before = load_snapshot(path)
    with TestClient(server.app, client=("127.0.0.1", 55102)) as client:
        response, _ = _request(client, draft)
    assert response["type"] == "error"
    assert load_snapshot(path) == before

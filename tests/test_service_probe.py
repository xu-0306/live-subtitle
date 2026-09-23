from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.service_probe import detect_api_type


def _profile(url: str) -> dict:
    return {"base_url": url, "model": "translator", "api_key": "secret", "auto_complete_endpoint": True}


def test_detects_responses_after_chat_fails() -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json={"output_text": "你好"})
        return httpx.Response(404, json={"error": {"message": "missing route"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await detect_api_type(_profile("http://127.0.0.1:8123/v1"), client)

    api_type, explicit_url = asyncio.run(run())
    assert (api_type, explicit_url) == ("responses", None)
    assert [path for path, _ in seen] == ["/v1/chat/completions", "/v1/responses"]
    assert "messages" in seen[0][1] and "input" in seen[1][1]


def test_full_endpoint_is_replaced_for_each_mode() -> None:
    paths = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"content": [{"type": "text", "text": "你好"}]})
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await detect_api_type(_profile("http://127.0.0.1:8123/v1/chat/completions"), client)

    assert asyncio.run(run()) == ("messages", "http://127.0.0.1:8123/v1/messages")
    assert paths == ["/v1/chat/completions", "/v1/responses", "/v1/messages"]


def test_only_errors_after_all_three_fail_without_leaking_provider_response() -> None:
    paths = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(401, json={"error": {"message": "secret provider detail"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await detect_api_type(_profile("http://127.0.0.1:8123/v1"), client)

    with pytest.raises(ValueError, match="All three API formats failed") as error:
        asyncio.run(run())
    assert len(paths) == 3
    assert "secret" not in str(error.value)
    assert str(error.value).count("HTTP 401") == 3

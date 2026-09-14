from __future__ import annotations

import asyncio
import json
import unittest

from backend.server import TranslationSession, _queue_translation, _translate_text
from backend.translation_scheduler import TranslationScheduler


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        self.messages.append(json.loads(raw))


class LatestWinsProvider:
    profile_id = "http"
    max_concurrency = 2
    cancellation_safe = True

    def __init__(self) -> None:
        self.first_started = asyncio.Event()
        self.first_cancelled = asyncio.Event()

    async def atranslate(self, text: str, source_lang, target_lang=None) -> str:
        if text == "first":
            self.first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.first_cancelled.set()
                raise
        return f"translated:{text}"


class FailingLocalProvider:
    profile_id = "local"
    max_concurrency = 1
    cancellation_safe = False

    async def atranslate(self, text: str, source_lang, target_lang=None) -> str:
        raise RuntimeError("local inference failed")


class LatestWinsQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_rapid_http_requests_cancel_and_consume_previous_task(self) -> None:
        provider = LatestWinsProvider()
        websocket = FakeWebSocket()
        session = TranslationSession(
            {"timeout_sec": 1},
            "en",
            provider,
            TranslationScheduler(global_max_concurrency=2),
        )
        session.pending_seq = 1
        _queue_translation(websocket, session, "first", "en", True, 1)
        await provider.first_started.wait()
        _queue_translation(websocket, session, "second", "en", True, 1)
        assert session.translation_task is not None
        await session.translation_task
        await asyncio.sleep(0)

        self.assertTrue(provider.first_cancelled.is_set())
        self.assertEqual(session.retired_translation_tasks, set())
        subtitles = [m for m in websocket.messages if m.get("type") == "subtitle"]
        self.assertEqual(len(subtitles), 1)
        self.assertEqual(subtitles[0]["translated"], "translated:second")

    async def test_local_provider_error_is_reported_and_consumed(self) -> None:
        websocket = FakeWebSocket()
        session = TranslationSession(
            {"timeout_sec": 1},
            "en",
            FailingLocalProvider(),
            TranslationScheduler(global_max_concurrency=1),
        )

        result = await _translate_text(session, "hello", "en", websocket)

        self.assertEqual(result, "")
        self.assertEqual(websocket.messages, [
            {"type": "error", "message": "Translation error: RuntimeError"}
        ])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from backend.translator import (
    API_TYPE_MESSAGES,
    API_TYPE_RESPONSES,
    ApiUrlPolicy,
    OpenAITranslator,
    resolve_models_url,
    resolve_provider_config,
    _build_translation_prompt,
)


class ApiUrlPolicyTests(unittest.TestCase):
    def test_completes_v1_and_preserves_query(self) -> None:
        url, api_type = ApiUrlPolicy().resolve(
            "http://localhost:8000/v1?tenant=a",
            default_url="https://unused.invalid",
        )
        self.assertEqual(
            url,
            "http://localhost:8000/v1/chat/completions?tenant=a",
        )
        self.assertEqual(api_type, "chat_completions")

    def test_complete_endpoint_is_not_appended_and_controls_mode(self) -> None:
        url, api_type = ApiUrlPolicy(api_type=API_TYPE_RESPONSES).resolve(
            "https://provider.example/v1/messages?beta=1",
            default_url="https://unused.invalid",
        )
        self.assertEqual(url, "https://provider.example/v1/messages?beta=1")
        self.assertEqual(api_type, API_TYPE_MESSAGES)

    def test_completion_can_be_disabled(self) -> None:
        raw = "https://provider.example/custom/inference?api-version=2"
        url, api_type = ApiUrlPolicy(
            api_type=API_TYPE_RESPONSES,
            auto_complete_endpoint=False,
        ).resolve(raw, default_url="https://unused.invalid")
        self.assertEqual(url, raw)
        self.assertEqual(api_type, API_TYPE_RESPONSES)

    def test_models_url_is_derived_without_request_query(self) -> None:
        self.assertEqual(
            resolve_models_url(
                "https://provider.example/api/v1/chat/completions?tenant=a"
            ),
            "https://provider.example/api/v1/models",
        )

    def test_selects_active_vllm_profile_and_ignores_runtime(self) -> None:
        cfg = {
            "engine": "vllm",
            "vllm": {
                "active_profile": "remote",
                "profiles": [
                    {"id": "local", "served_model": "wrong"},
                    {
                        "id": "remote",
                        "base_url": "http://gpu-host:8000/v1",
                        "served_model": "Qwen/Test",
                        "api_type": "responses",
                        "runtime": {"max_model_len": 4096},
                    },
                ],
            },
        }
        resolved = resolve_provider_config(cfg)
        self.assertEqual(resolved["profile_id"], "remote")
        self.assertEqual(resolved["model"], "Qwen/Test")
        self.assertNotIn("runtime", resolved)


class AsyncOpenAITranslatorTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_is_domain_neutral_and_context_is_disambiguation_only(self) -> None:
        prompt = _build_translation_prompt(
            "The bank is closed.",
            "en",
            "粵語（香港）",
            context="We arrived late and checked the opening hours.",
        )
        self.assertIn("粵語（香港）", prompt)
        self.assertIn("Preceding context for disambiguation only", prompt)
        self.assertIn("Current transcript to translate", prompt)
        self.assertNotIn("game", prompt.lower())
        self.assertNotIn("travel", prompt.lower())
        self.assertNotIn("classify", prompt.lower())

    async def test_uses_shared_async_client_for_translation_and_models(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "served-model"}]})
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "served-model")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "你好"}}]},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            translator = OpenAITranslator(
                api_key="secret",
                model="served-model",
                target_language="zh-TW",
                base_url="http://vllm.local:8000/v1",
                auto_complete_endpoint=True,
                http_client=client,
            )
            self.assertEqual(await translator.atranslate("hello", "en"), "你好")
            self.assertEqual(await translator.list_models(), ["served-model"])
            await translator.aclose()
            self.assertFalse(client.is_closed)

        self.assertEqual([request.url.path for request in requests], [
            "/v1/chat/completions",
            "/v1/models",
        ])
        self.assertEqual(requests[0].headers["authorization"], "Bearer secret")

    async def test_context_snapshot_reaches_provider_payload_without_domain_routing(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "翻譯"}}]},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            translator = OpenAITranslator(
                api_key="",
                model="arbitrary-model",
                target_language="isiZulu",
                base_url="https://provider.example/v1",
                http_client=client,
            )
            self.assertEqual(
                await translator.atranslate(
                    "The bank is closed.",
                    "en",
                    context="We arrived late and checked the opening hours.",
                ),
                "翻譯",
            )
        body = json.loads(requests[0].content)
        user_prompt = body["messages"][1]["content"]
        self.assertIn("isiZulu", user_prompt)
        self.assertIn("We arrived late", user_prompt)
        self.assertIn("Current transcript to translate", user_prompt)
        self.assertNotIn("game", user_prompt.lower())
        self.assertNotIn("travel", user_prompt.lower())

    async def test_http_translation_is_cancelled_without_thread_wrapper(self) -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return httpx.Response(500)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            translator = OpenAITranslator(
                api_key="",
                model="served-model",
                target_language="zh-TW",
                base_url="http://vllm.local:8000/v1",
                auto_complete_endpoint=True,
                http_client=client,
            )
            task = asyncio.create_task(translator.atranslate("first", "en"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cancelled.is_set())

    async def test_provider_error_redacts_api_key(self) -> None:
        secret = "do-not-leak-this-key"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={"error": {"message": f"rejected {secret}"}},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            translator = OpenAITranslator(
                api_key=secret,
                model="served-model",
                target_language="zh-TW",
                base_url="http://vllm.local:8000/v1",
                auto_complete_endpoint=True,
                http_client=client,
            )
            with self.assertRaisesRegex(RuntimeError, r"\[redacted\]") as caught:
                await translator.atranslate("hello", "en")
            self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

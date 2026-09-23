from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from backend.server import (
    TranslationSession,
    _apply_stt_engine_evidence_config,
    _build_engine_kwargs,
    _close_open_chunk,
    _ensure_pending_seq,
    _evaluate_transcript_evidence,
    _find_repetition_loop,
    _handle_results,
    _is_annotation_only_text,
    _queue_translation,
    _sanitize_text,
    _snapshot_suffix,
    _stt_stall_reason,
    _update_asr_snapshot,
    _translate_text,
)
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


class GatedLocalProvider:
    profile_id = "managed"
    max_concurrency = 1
    cancellation_safe = False

    def __init__(self) -> None:
        self.first_started = asyncio.Event()
        self.second_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.release_second = asyncio.Event()

    async def atranslate(self, text: str, source_lang, target_lang=None) -> str:
        if text == "old":
            self.first_started.set()
            await self.release_first.wait()
        else:
            self.second_started.set()
            await self.release_second.wait()
        return f"translated:{text}"


class ImmediateProvider:
    profile_id = "immediate"
    max_concurrency = 1
    cancellation_safe = True

    async def atranslate(
        self, text: str, source_lang, target_lang=None, context=None
    ) -> str:
        return f"translated:{text}"


class LatestWinsQueueTests(unittest.IsolatedAsyncioTestCase):
    def test_repetition_guard_distinguishes_speech_from_runaway_loops(self) -> None:
        self.assertIsNone(_find_repetition_loop("はい、はい、分かりました"))
        self.assertIsNone(_find_repetition_loop("no no no, that is not what I meant"))

        japanese_loop = _find_repetition_loop("そうさ、あ、" * 20)
        self.assertIsNotNone(japanese_loop)
        assert japanese_loop is not None
        self.assertGreaterEqual(japanese_loop[1], 6)

        short_loop = _find_repetition_loop("笑" * 20)
        self.assertIsNotNone(short_loop)

    def test_simulstreaming_context_cap_is_configurable(self) -> None:
        self.assertEqual(_build_engine_kwargs({"model": "base"})["max_context_tokens"], 96)
        self.assertEqual(
            _build_engine_kwargs({"model": "base", "max_context_tokens": 48})[
                "max_context_tokens"
            ],
            48,
        )

    def test_annotation_only_filter_is_open_set_and_preserves_real_speech(self) -> None:
        # Neither label appears in the legacy word lists.  Structural handling
        # therefore covers unseen languages instead of growing a blacklist.
        unseen_events = "[bruit de fond]（risate）"
        self.assertTrue(_is_annotation_only_text(unseen_events))
        self.assertEqual(_sanitize_text(unseen_events), "")

        speech = "(I think) this works"
        self.assertFalse(_is_annotation_only_text(speech))
        self.assertEqual(_sanitize_text(speech), speech)

    def test_low_confidence_partial_requires_stable_confirmation(self) -> None:
        stt_session = SimpleNamespace(
            cfg={"evidence_gate": {"confirm_below_probability": 0.35}},
            evidence_candidate_text="",
            evidence_stable_updates=0,
        )
        segment = SimpleNamespace(
            probability=0.2,
            tokens=[],
        )
        response = SimpleNamespace()

        first = _evaluate_transcript_evidence(
            stt_session,
            "新しい未知語",
            response,
            segment,
            is_final=False,
        )
        second = _evaluate_transcript_evidence(
            stt_session,
            "新しい未知語",
            response,
            segment,
            is_final=False,
        )

        self.assertEqual(first.action, "hold")
        self.assertEqual(second.action, "accept")
        self.assertEqual(second.reason, "stable-low-confidence")

    def test_no_speech_and_weak_decode_reject_arbitrary_text(self) -> None:
        stt_session = SimpleNamespace(cfg={}, evidence_candidate_text="")
        segment = SimpleNamespace(
            no_speech_prob=0.91,
            avg_logprob=-1.4,
            tokens=[],
        )
        decision = _evaluate_transcript_evidence(
            stt_session,
            "arbitrary plausible sentence",
            SimpleNamespace(),
            segment,
            is_final=True,
        )
        self.assertEqual(decision.action, "reject")
        self.assertEqual(decision.reason, "no-speech-low-confidence")

    def test_native_nonspeech_threshold_is_configurable(self) -> None:
        native_cfg = SimpleNamespace(nonspeech_prob=0.5)
        engine = SimpleNamespace(asr=SimpleNamespace(cfg=native_cfg))
        _apply_stt_engine_evidence_config(
            engine,
            {"evidence_gate": {"nonspeech_prob": 0.72}},
        )
        self.assertEqual(native_cfg.nonspeech_prob, 0.72)

    async def test_runaway_repetition_is_suppressed_and_requests_context_reset(self) -> None:
        websocket = FakeWebSocket()
        session = TranslationSession(
            {},
            "ja",
            ImmediateProvider(),
            TranslationScheduler(global_max_concurrency=1),
        )
        stt_session = SimpleNamespace(
            cfg={"repetition_guard": {"reset_cooldown_sec": 5}},
            last_result_ts=0.0,
            result_seen=False,
            last_response_ts=0.0,
            response_seen=False,
            last_response_status="",
            last_repetition_reset_ts=0.0,
        )
        reset_requests: asyncio.Queue[str] = asyncio.Queue(maxsize=1)

        async def results():
            yield SimpleNamespace(
                error="",
                lines=[],
                buffer_transcription="そうさ、あ、" * 20,
                status="active_transcription",
            )

        await _handle_results(
            websocket,
            results(),
            session,
            stt_session,
            reset_requests=reset_requests,
        )

        self.assertEqual(reset_requests.get_nowait(), "repetition loop")
        self.assertEqual(
            [item for item in websocket.messages if item.get("type") == "subtitle"],
            [],
        )

    def test_stt_watchdog_treats_detected_silence_as_healthy(self) -> None:
        silent = SimpleNamespace(
            first_audio_ts=10.0,
            last_audio_ts=100.0,
            response_seen=True,
            last_response_ts=20.0,
            last_response_status="no_audio_detected",
        )
        self.assertIsNone(_stt_stall_reason(silent, 100.0, 15.0))

        stalled = SimpleNamespace(
            first_audio_ts=10.0,
            last_audio_ts=100.0,
            response_seen=True,
            last_response_ts=80.0,
            last_response_status="active_transcription",
        )
        self.assertEqual(_stt_stall_reason(stalled, 100.0, 15.0), "stall")

        waiting_for_first_response = SimpleNamespace(
            first_audio_ts=60.0,
            last_audio_ts=100.0,
            response_seen=False,
            last_response_ts=60.0,
            last_response_status="",
        )
        self.assertEqual(
            _stt_stall_reason(waiting_for_first_response, 100.0, 15.0),
            "no processor response",
        )

    def test_non_speech_annotations_are_removed_without_dropping_spoken_asides(self) -> None:
        self.assertEqual(
            _sanitize_text("Right for me. (mouse clicking) I guess I can do my mat."),
            "Right for me. I guess I can do my mat.",
        )
        self.assertEqual(_sanitize_text("[Music] Hello [applause]"), "Hello")
        self.assertEqual(_sanitize_text("（笑聲）你好"), "你好")
        self.assertEqual(_sanitize_text("(speaking in foreign language) (笑)(笑)(笑)"), "")
        self.assertEqual(_sanitize_text("(笑"), "")
        self.assertEqual(_sanitize_text("笑)"), "")
        self.assertEqual(_sanitize_text("笑"), "")
        self.assertEqual(
            _sanitize_text("That plan (I think) still works."),
            "That plan (I think) still works.",
        )
        self.assertEqual(
            _sanitize_text("We discussed (music theory) today."),
            "We discussed (music theory) today.",
        )

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
        self.assertEqual(session.pending_seq, 1)

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

    async def test_cumulative_snapshot_after_force_flush_only_emits_uncommitted_suffix(self) -> None:
        session = TranslationSession(
            {"context_max_chars": 64},
            "en",
            FailingLocalProvider(),
            TranslationScheduler(global_max_concurrency=1),
        )
        changed, _ = _update_asr_snapshot(session, "alpha beta", "en", ("start", "7"))
        self.assertTrue(changed)
        first_seq = _ensure_pending_seq(session)
        self.assertEqual(session.utterance_text, "alpha beta")
        session.asr_committed_text = session.asr_snapshot_text
        _close_open_chunk(session, first_seq, session.utterance_text)

        changed, _ = _update_asr_snapshot(session, "alpha beta gamma", "en", ("start", "7"))
        self.assertTrue(changed)
        self.assertEqual(session.pending_seq, first_seq + 1)
        self.assertEqual(session.utterance_text, "gamma")

        # A different upstream identity with the same words is real speech,
        # not a duplicate snapshot and must remain visible as a new chunk.
        changed, _ = _update_asr_snapshot(session, "alpha beta", "en", ("start", "8"))
        self.assertTrue(changed)
        self.assertEqual(session.utterance_text, "gamma alpha beta")

    async def test_partial_to_timed_final_replaces_snapshot_without_duplication(self) -> None:
        session = TranslationSession(
            {},
            "en",
            ImmediateProvider(),
            TranslationScheduler(global_max_concurrency=1),
        )
        changed, _ = _update_asr_snapshot(session, "hello", "en", None)
        self.assertTrue(changed)
        changed, _ = _update_asr_snapshot(
            session, "hello world", "en", ("start", "1.0")
        )
        self.assertTrue(changed)
        self.assertEqual(session.utterance_text, "hello world")

    async def test_translation_completion_does_not_close_open_chunk(self) -> None:
        websocket = FakeWebSocket()
        session = TranslationSession(
            {},
            "en",
            ImmediateProvider(),
            TranslationScheduler(global_max_concurrency=1),
        )
        _update_asr_snapshot(session, "still growing", "en", ("start", "1"))
        seq = _ensure_pending_seq(session)
        _queue_translation(websocket, session, session.utterance_text, "en", True, seq)
        assert session.translation_task is not None
        await session.translation_task

        self.assertEqual(session.pending_seq, seq)
        self.assertEqual(session.utterance_text, "still growing")
        self.assertEqual(session.asr_committed_text, "")

    async def test_timed_segment_boundary_closes_previous_chunk_not_provider_completion(self) -> None:
        websocket = FakeWebSocket()
        session = TranslationSession(
            {"debounce_ms": 60_000, "segment_max_chars": 0, "segment_max_ms": 0},
            "en",
            ImmediateProvider(),
            TranslationScheduler(global_max_concurrency=1),
        )
        stt_session = SimpleNamespace(last_result_ts=0.0, result_seen=False)

        async def results():
            yield SimpleNamespace(
                error="",
                lines=[SimpleNamespace(text="first thought", start=1.0, detected_language="en")],
                buffer_transcription="",
            )
            yield SimpleNamespace(
                error="",
                lines=[SimpleNamespace(text="new topic", start=3.0, detected_language="en")],
                buffer_transcription="",
            )

        await _handle_results(websocket, results(), session, stt_session)
        if session.translation_task is not None:
            await session.translation_task
        if session.debounce_task is not None:
            session.debounce_task.cancel()
            await asyncio.gather(session.debounce_task, return_exceptions=True)

        subtitles = [item for item in websocket.messages if item.get("type") == "subtitle"]
        self.assertEqual(
            [(item["seq"], item["original"], item["translated"]) for item in subtitles],
            [
                (1, "first thought", ""),
                (2, "new topic", ""),
                (1, "first thought", "translated:first thought"),
            ],
        )
        self.assertEqual(session.pending_seq, 2)
        self.assertEqual(session.utterance_text, "new topic")

    async def test_committed_prefix_revision_does_not_replay_previous_words(self) -> None:
        self.assertEqual(
            _snapshot_suffix("alpha revised beta gamma", "alpha beta"),
            "gamma",
        )
        self.assertEqual(_snapshot_suffix("entirely unrelated", "alpha beta"), "")

    async def test_old_local_completion_cannot_clear_newer_source_revision(self) -> None:
        provider = GatedLocalProvider()
        websocket = FakeWebSocket()
        session = TranslationSession(
            {"timeout_sec": 1},
            "en",
            provider,
            TranslationScheduler(global_max_concurrency=1),
        )
        changed, _ = _update_asr_snapshot(session, "old", "en", ("start", "1"))
        self.assertTrue(changed)
        seq = _ensure_pending_seq(session)
        _queue_translation(websocket, session, "old", "en", True, seq)
        await provider.first_started.wait()

        changed, _ = _update_asr_snapshot(session, "old newer", "en", ("start", "1"))
        self.assertTrue(changed)
        _queue_translation(websocket, session, session.utterance_text, "en", True, seq)
        provider.release_first.set()
        await provider.second_started.wait()
        self.assertEqual(session.utterance_text, "old newer")
        self.assertEqual(
            [item for item in websocket.messages if item.get("type") == "subtitle"],
            [],
        )
        provider.release_second.set()
        await session.translation_task
        subtitles = [item for item in websocket.messages if item.get("type") == "subtitle"]
        self.assertEqual([item["translated"] for item in subtitles], ["translated:old newer"])


if __name__ == "__main__":
    unittest.main()
    _is_annotation_only_text,

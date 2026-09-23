import asyncio
from difflib import SequenceMatcher
import gc
import logging
import inspect
import ipaddress
import json
import math
import os
import re
import threading
import time
import unicodedata
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import httpx

from whisperlivekit import AudioProcessor, TranscriptionEngine

logger = logging.getLogger(__name__)


def _patch_whisperlivekit_asrtoken() -> None:
    """Preserve decoder confidence at the WhisperLiveKit adapter boundary.

    WhisperLiveKit 0.2.17 passes ``probability`` from LocalAgreement backends
    even though its bundled ``ASRToken`` dataclass does not accept the field.
    Older code discarded it merely to avoid a constructor error.  Retain the
    value instead so subtitle policy can use acoustic/decode evidence without
    depending on recognized words.
    """

    try:
        from whisperlivekit.local_agreement import backends as la_backends
        from whisperlivekit import timed_objects
    except Exception:
        return
    asr_token = getattr(la_backends, "ASRToken", None)
    if asr_token is None:
        return
    try:
        sig = inspect.signature(asr_token.__init__)
    except (TypeError, ValueError):
        return
    if "probability" not in sig.parameters and not getattr(
        asr_token, "_live_subtitle_probability_patch", False
    ):
        original_init = asr_token.__init__

        def _init(self, *args, probability=None, **kwargs):
            original_init(self, *args, **kwargs)
            self.probability = probability

        asr_token.__init__ = _init
        asr_token._live_subtitle_probability_patch = True
        logger.info("Patched whisperlivekit.ASRToken to retain decoder probability.")

    if not getattr(asr_token, "_live_subtitle_offset_patch", False):
        original_with_offset = asr_token.with_offset

        def _with_offset(self, offset):
            shifted = original_with_offset(self, offset)
            for name in (
                "probability",
                "min_probability",
                "avg_logprob",
                "no_speech_prob",
            ):
                if hasattr(self, name):
                    setattr(shifted, name, getattr(self, name))
            return shifted

        asr_token.with_offset = _with_offset
        asr_token._live_subtitle_offset_patch = True

    segment_type = getattr(timed_objects, "Segment", None)
    if segment_type is not None and not getattr(
        segment_type, "_live_subtitle_evidence_patch", False
    ):
        original_from_tokens = segment_type.from_tokens

        def _from_tokens(cls, tokens, is_silence=False):
            segment = original_from_tokens(tokens, is_silence=is_silence)
            if segment is None or is_silence:
                return segment
            usable_tokens = [
                token
                for token in tokens
                if not bool(getattr(token, "is_silence", lambda: False)())
            ]
            segment.tokens = usable_tokens
            probabilities = [
                float(token.probability)
                for token in usable_tokens
                if getattr(token, "probability", None) is not None
            ]
            if probabilities:
                segment.probability = sum(probabilities) / len(probabilities)
                segment.min_probability = min(probabilities)
            for name in ("avg_logprob", "no_speech_prob"):
                values = [
                    float(getattr(token, name))
                    for token in usable_tokens
                    if getattr(token, name, None) is not None
                ]
                if values:
                    setattr(segment, name, sum(values) / len(values))
            return segment

        segment_type.from_tokens = classmethod(_from_tokens)
        segment_type._live_subtitle_evidence_patch = True


def _patch_whisperlivekit_simulstreaming_evidence() -> None:
    """Expose evidence already computed by SimulStreaming's decoder.

    The upstream AlignAtt implementation computes the no-speech probability
    and cumulative token log probabilities but drops both before constructing
    ``ASRToken`` objects.  This compatibility adapter records those generic
    signals without changing the installed package or matching transcript
    literals.  Failure is intentionally non-fatal for other versions/backends.
    """

    try:
        import torch
        from whisperlivekit.simul_whisper.simul_whisper import AlignAtt
        from whisperlivekit.whisper.decoding import BeamSearchDecoder, GreedyDecoder
    except Exception:
        return

    def patch_decoder(decoder_type) -> None:
        if getattr(decoder_type, "_live_subtitle_evidence_patch", False):
            return
        original_update = decoder_type.update

        def _update(self, tokens, logits, sum_logprobs):
            updated_tokens, completed = original_update(
                self, tokens, logits, sum_logprobs
            )
            try:
                if updated_tokens.shape[0] > 0:
                    selected_token = int(updated_tokens[0, -1].item())
                    logprobs = torch.log_softmax(logits.float(), dim=-1)
                    # Beam size is one in the default live path.  Taking the
                    # strongest matching beam also remains a conservative
                    # aggregate for custom beam configurations.
                    selected = logprobs[:, selected_token].max().item()
                    history = getattr(self, "_live_subtitle_logprobs", None)
                    if history is None:
                        history = []
                        self._live_subtitle_logprobs = history
                    history.append(float(selected))
            except Exception:
                logger.debug("Unable to collect decoder confidence", exc_info=True)
            return updated_tokens, completed

        decoder_type.update = _update
        decoder_type._live_subtitle_evidence_patch = True

    patch_decoder(GreedyDecoder)
    patch_decoder(BeamSearchDecoder)

    if getattr(AlignAtt, "_live_subtitle_evidence_patch", False):
        return
    original_logits = AlignAtt.logits
    original_infer = AlignAtt.infer

    def _logits(self, tokens, *args, **kwargs):
        result = original_logits(self, tokens, *args, **kwargs)
        try:
            logits = result[0] if isinstance(result, tuple) else result
            sot_index = int(self.state.sot_index)
            no_speech_token = getattr(self.tokenizer, "no_speech", None)
            if (
                no_speech_token is not None
                and logits.ndim == 3
                and logits.shape[1] > sot_index
                and tokens.shape[1] > 1
            ):
                probs = logits[:, sot_index, :].float().softmax(dim=-1)
                self._live_subtitle_no_speech_prob = float(
                    probs[0, no_speech_token].item()
                )
        except Exception:
            logger.debug("Unable to collect no-speech probability", exc_info=True)
        return result

    def _infer(self, *args, **kwargs):
        self._live_subtitle_no_speech_prob = None
        decoder = getattr(self.state, "token_decoder", None)
        if decoder is not None:
            decoder._live_subtitle_logprobs = []
        tokens = original_infer(self, *args, **kwargs)
        try:
            logprobs = list(
                getattr(decoder, "_live_subtitle_logprobs", []) if decoder else []
            )
            if logprobs:
                avg_logprob = sum(logprobs) / len(logprobs)
                probability = math.exp(avg_logprob)
                min_probability = math.exp(min(logprobs))
            else:
                avg_logprob = probability = min_probability = None
            no_speech_prob = getattr(
                self, "_live_subtitle_no_speech_prob", None
            )
            for token in tokens or []:
                token.avg_logprob = avg_logprob
                token.probability = probability
                token.min_probability = min_probability
                token.no_speech_prob = no_speech_prob
        except Exception:
            logger.debug("Unable to attach SimulStreaming evidence", exc_info=True)
        return tokens

    AlignAtt.logits = _logits
    AlignAtt.infer = _infer
    AlignAtt._live_subtitle_evidence_patch = True

try:
    from . import model_manager
    from .cache import LRUCache
    from .config_store import (
        RevisionConflictError,
        import_profiles,
        load_snapshot,
        server_id_for_config,
    )
    from . import service_probe, settings_management
    from .local_catalog import get_model, resolve_hf_model
    from .local_runtime import prepare_install
    from .config import LOCAL_TRANSLATION_MODELS_DIR_NAME, load_config, _default_app_dir
    from .translation_service import TranslationService
    from .translation_scheduler import TranslationScheduler
    from .translator import build_translator, describe_translation_target
except ImportError:  # Fallback when running as a script.
    import model_manager
    from cache import LRUCache
    from config_store import RevisionConflictError, import_profiles, load_snapshot, server_id_for_config
    import service_probe
    import settings_management
    from local_catalog import get_model, resolve_hf_model
    from local_runtime import prepare_install
    from config import LOCAL_TRANSLATION_MODELS_DIR_NAME, load_config, _default_app_dir
    from translation_service import TranslationService
    from translation_scheduler import TranslationScheduler
    from translator import build_translator, describe_translation_target

_patch_whisperlivekit_asrtoken()
_patch_whisperlivekit_simulstreaming_evidence()

_STT_ENGINE_BUILD_LOCK = threading.Lock()


def _get_server_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    server_cfg = cfg.get("server", {})
    return {
        "host": str(server_cfg.get("host", "127.0.0.1")),
        "port": int(server_cfg.get("port", 8765)),
    }


def _configured_path() -> Path:
    configured = os.getenv("STT_CONFIG_PATH")
    return Path(configured).expanduser() if configured else Path(__file__).with_name("config.yaml")


def _trusted_profile_import_origin(websocket: WebSocket) -> bool:
    """Allow writes from the local app or an extension origin only.

    A normal web page can also open a socket to loopback, so checking the
    remote address alone is not sufficient for a profile migration write.
    Native GUI clients generally omit Origin; browser extensions use their
    ``chrome-extension://``/``moz-extension://`` scheme.
    """

    origin = str(websocket.headers.get("origin") or "").strip().lower()
    if not origin:
        return True
    return origin.startswith("chrome-extension://") or origin.startswith("moz-extension://")


MAX_PROFILE_IMPORT_BYTES = 512 * 1024
MAX_PROFILE_IMPORT_ITEMS = 256


def _merge_config(base: Dict[str, Any], update: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not update:
        return dict(base)
    merged = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


_ALLOWED_STT_UPDATE_KEYS = {
    "model",
    "language",
    "backend",
    "min_chunk_size",
    "buffer_trimming",
    "buffer_trimming_sec",
    "confidence_validation",
    "pcm_input",
    "vad",
    "vac",
    "vac_chunk_size",
    "stall_timeout_sec",
    "stall_check_interval_sec",
    "max_context_tokens",
    "repetition_guard",
    "evidence_gate",
}

DEFAULT_STT_MAX_CONTEXT_TOKENS = 96
DEFAULT_REPETITION_MIN_REPEATS = 6
DEFAULT_REPETITION_SHORT_UNIT_MIN_REPEATS = 16
DEFAULT_REPETITION_MIN_SPAN_CHARS = 16
DEFAULT_REPETITION_MAX_UNIT_CHARS = 24
DEFAULT_REPETITION_MIN_COVERAGE = 0.45
DEFAULT_REPETITION_RESET_COOLDOWN_SEC = 5.0
DEFAULT_EVIDENCE_NONSPEECH_PROB = 0.5
DEFAULT_EVIDENCE_NO_SPEECH_THRESHOLD = 0.6
DEFAULT_EVIDENCE_AVG_LOGPROB_THRESHOLD = -1.0
DEFAULT_EVIDENCE_CONFIRM_PROBABILITY = 0.35
DEFAULT_EVIDENCE_MIN_STABLE_UPDATES = 2
DEFAULT_EVIDENCE_MIN_AGREEMENT = 0.8
DEFAULT_EVIDENCE_RESET_REJECTIONS = 3
DEFAULT_EVIDENCE_RESET_COOLDOWN_SEC = 5.0

def _normalize_stt_update(stt_update: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not stt_update or not isinstance(stt_update, dict):
        return {}
    normalized = {k: v for k, v in stt_update.items() if k in _ALLOWED_STT_UPDATE_KEYS}
    if "model" in normalized:
        # Ensure explicit model selection does not keep a stale model_path.
        normalized["model_path"] = None
    return normalized


def _validate_v2_stt_selection(
    stt_cfg: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    revision: Optional[int] = None,
) -> None:
    """Reject unavailable v2 speech selections before capture starts.

    Legacy config messages intentionally retain the historical lazy-download
    behavior.  Only an explicit capture ``version: 2`` opts into the catalog
    availability contract, preventing a browser selection from triggering an
    unexpected model download.
    """

    model_id = str(stt_cfg.get("model") or "").strip()
    if not model_id:
        return
    backend = str(stt_cfg.get("backend") or "").strip()
    catalog = app.state.translation_service.desktop_catalog(
        server_id=app.state.server_id,
        instance_id=app.state.instance_id,
        config_override=config,
        revision_override=revision,
    )
    selected = None
    for item in catalog.get("stt_models", []):
        selection = item.get("selection") if isinstance(item, dict) else None
        if not isinstance(selection, dict) or str(selection.get("model") or "") != model_id:
            continue
        item_backend = str(selection.get("backend") or "")
        # Standard Whisper model IDs can be served by several established
        # backends, so the catalog leaves that backend open.
        if backend and backend != "auto":
            if item_backend and backend != item_backend:
                continue
        selected = item
        break
    if selected is None:
        raise ValueError("Selected STT model is not in the desktop catalog. Refresh the desktop app.")
    if selected.get("available") is not True:
        reason = str(selected.get("reason") or "Model files are not installed.")
        raise ValueError(f"Selected STT model is unavailable: {reason}")


async def _ensure_model_available(
    stt_cfg: Dict[str, Any],
    websocket: WebSocket,
    download_lock: asyncio.Lock,
) -> Dict[str, Any]:
    model_name = str(stt_cfg.get("model") or "").strip()
    if not model_name:
        return stt_cfg
    loop = asyncio.get_running_loop()
    progress_state = {"percent": -1, "mb": -1}

    async def notify_cb(message: str) -> None:
        await _safe_send(websocket, {"type": "status", "message": message})

    def progress_cb(downloaded: int, total: Optional[int]) -> None:
        if total:
            percent = int(downloaded * 100 / total)
            if percent == progress_state["percent"]:
                return
            if percent < 100 and percent - progress_state["percent"] < 5:
                return
            progress_state["percent"] = percent
            message = f"Downloading Whisper model ({model_name}): {percent}%"
        else:
            mb = int(downloaded / (1024 * 1024))
            if mb == progress_state["mb"]:
                return
            progress_state["mb"] = mb
            message = f"Downloading Whisper model ({model_name}): {mb} MB"
        loop.call_soon_threadsafe(
            asyncio.create_task,
            _safe_send(websocket, {"type": "status", "message": message}),
        )

    return await model_manager.ensure_model_available(
        stt_cfg,
        notify_cb=notify_cb,
        progress_cb=progress_cb,
        download_lock=download_lock,
    )


def _build_engine_kwargs(stt_cfg: Dict[str, Any]) -> Dict[str, Any]:
    try:
        max_context_tokens = int(
            stt_cfg.get("max_context_tokens", DEFAULT_STT_MAX_CONTEXT_TOKENS)
        )
    except (TypeError, ValueError):
        max_context_tokens = DEFAULT_STT_MAX_CONTEXT_TOKENS
    max_context_tokens = max(0, max_context_tokens)
    return {
        "model_size": stt_cfg.get("model", "medium"),
        "lan": stt_cfg.get("language", "auto"),
        "backend_policy": stt_cfg.get("backend_policy", "localagreement"),
        "backend": stt_cfg.get("backend", "auto"),
        "min_chunk_size": float(stt_cfg.get("min_chunk_size", 0.1)),
        "buffer_trimming": stt_cfg.get("buffer_trimming", "segment"),
        "buffer_trimming_sec": float(stt_cfg.get("buffer_trimming_sec", 15.0)),
        "confidence_validation": bool(stt_cfg.get("confidence_validation", False)),
        "pcm_input": bool(stt_cfg.get("pcm_input", False)),
        "vad": bool(stt_cfg.get("vad", True)),
        "vac": bool(stt_cfg.get("vac", True)),
        "vac_chunk_size": float(stt_cfg.get("vac_chunk_size", 0.04)),
        "model_cache_dir": stt_cfg.get("model_cache_dir"),
        "model_dir": stt_cfg.get("model_dir"),
        "model_path": stt_cfg.get("model_path"),
        "lora_path": stt_cfg.get("lora_path"),
        # SimulStreaming otherwise keeps almost the full Whisper text window.
        # A shorter, configurable prompt limits how far an early recognition
        # error can feed itself back into subsequent chunks.
        "max_context_tokens": max_context_tokens,
        "target_language": "",
    }


async def _close_stt_engine(engine: Any) -> None:
    """Best-effort cleanup for an engine built after its client disconnected."""

    if engine is None:
        return
    candidates = [engine, getattr(engine, "asr", None)]
    seen: set[int] = set()
    for candidate in candidates:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        for method_name in ("aclose", "close", "shutdown", "cleanup"):
            method = getattr(candidate, method_name, None)
            if not callable(method):
                continue
            try:
                result = await asyncio.to_thread(method)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("Late STT engine cleanup failed", exc_info=True)
            break


def _dispose_stt_task_result(task: asyncio.Task) -> None:
    if getattr(task, "_stt_cleanup_scheduled", False):
        return
    setattr(task, "_stt_cleanup_scheduled", True)
    if task.cancelled():
        return
    try:
        engine = task.result()
    except Exception:
        return
    asyncio.create_task(_close_stt_engine(engine))


def _watch_stt_task_for_cleanup(task: asyncio.Task) -> None:
    if getattr(task, "_stt_cleanup_watched", False):
        return
    setattr(task, "_stt_cleanup_watched", True)
    task.add_done_callback(_dispose_stt_task_result)


async def _construct_stt_engine(engine_kwargs: Dict[str, Any]):
    """Construct the synchronous Whisper engine without blocking the loop.

    Python cannot interrupt a worker thread running model initialization.  If
    the caller is cancelled, leave that worker task attached to a cleanup
    callback so a late-created engine is closed instead of becoming detached.
    """

    def build() -> Any:
        # whisperlivekit currently exposes a process-wide singleton engine;
        # retain the old serialized construction behavior after moving the
        # blocking initialization away from the asyncio event loop.
        with _STT_ENGINE_BUILD_LOCK:
            engine_type = TranscriptionEngine
            has_singleton_state = hasattr(engine_type, "_instance") or hasattr(
                engine_type, "_initialized"
            )
            if not has_singleton_state:
                return engine_type(**engine_kwargs)
            missing = object()
            previous_instance = getattr(engine_type, "_instance", missing)
            previous_initialized = getattr(engine_type, "_initialized", missing)
            try:
                # The installed whisperlivekit class uses these two fields in
                # __new__/__init__. Reset only this adapter-owned boundary so
                # each capture's selected model gets an independent engine;
                # existing sessions retain their object references.
                engine_type._instance = None
                engine_type._initialized = False
                return engine_type(**engine_kwargs)
            except BaseException:
                if previous_instance is not missing:
                    engine_type._instance = previous_instance
                if previous_initialized is not missing:
                    engine_type._initialized = previous_initialized
                raise

    worker = asyncio.create_task(asyncio.to_thread(build))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        _watch_stt_task_for_cleanup(worker)
        raise


async def _close_results_generator(results_generator) -> None:
    closer = getattr(results_generator, "aclose", None)
    if not callable(closer):
        return
    try:
        await closer()
    except Exception:
        pass


def _release_torch_cache() -> None:
    try:
        import torch
    except Exception:
        return
    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _release_memory() -> None:
    try:
        gc.collect()
    except Exception:
        pass
    _release_torch_cache()


def _log_cuda_memory(label: str) -> None:
    try:
        import torch
    except Exception:
        return
    try:
        if not torch.cuda.is_available():
            return
        mb = 1024 * 1024
        allocated = torch.cuda.memory_allocated() / mb
        reserved = torch.cuda.memory_reserved() / mb
        max_alloc = torch.cuda.max_memory_allocated() / mb
        max_reserved = torch.cuda.max_memory_reserved() / mb
        logger.info(
            "CUDA mem %s: alloc=%.1fMB reserved=%.1fMB max_alloc=%.1fMB max_reserved=%.1fMB",
            label,
            allocated,
            reserved,
            max_alloc,
            max_reserved,
        )
    except Exception:
        return


def _pick_latest_segment(lines: list[Any]) -> Optional[Any]:
    for line in reversed(lines):
        if getattr(line, "text", None):
            return line
    return None


def _guess_language(text: str, fallback: Optional[str]) -> Optional[str]:
    if fallback and fallback != "auto":
        return fallback
    for ch in text:
        code = ord(ch)
        if 0x3040 <= code <= 0x30ff or 0x31f0 <= code <= 0x31ff:
            return "ja"
        if 0x4e00 <= code <= 0x9fff:
            return "zh"
    if any("A" <= ch <= "Z" or "a" <= ch <= "z" for ch in text):
        return "en"
    return fallback


NON_SPEECH_ANNOTATION_RE = re.compile(
    r"\(([^()\r\n]{1,80})\)"
    r"|\[([^\[\]\r\n]{1,80})\]"
    r"|\uff08([^\uff08\uff09\r\n]{1,80})\uff09"
    r"|\u3010([^\u3010\u3011\r\n]{1,80})\u3011"
)
# These labels are retained only as a compatibility fallback for inline event
# captions embedded next to real speech.  The primary gate below is structural
# and model-evidence based, so an unseen language or event name does not require
# another source-code entry.
NON_SPEECH_EVENT_WORDS = frozenset(
    {
        "applause",
        "applauding",
        "beep",
        "beeping",
        "breathing",
        "cheering",
        "chuckle",
        "chuckles",
        "chuckling",
        "clicking",
        "clicks",
        "cough",
        "coughing",
        "coughs",
        "crying",
        "gasp",
        "gasps",
        "groan",
        "groaning",
        "inaudible",
        "laugh",
        "laughing",
        "laughs",
        "laughter",
        "music",
        "noise",
        "ringing",
        "silence",
        "sigh",
        "sighing",
        "sighs",
        "sniffing",
        "sobbing",
        "static",
        "typing",
        "whistling",
    }
)
NON_SPEECH_CONTEXT_WORDS = frozenset(
    {
        "a",
        "audience",
        "background",
        "computer",
        "continued",
        "continues",
        "distant",
        "door",
        "faint",
        "keyboard",
        "loud",
        "loudly",
        "man",
        "men",
        "mouse",
        "people",
        "person",
        "phone",
        "soft",
        "softly",
        "someone",
        "the",
        "woman",
        "women",
    }
)
NON_SPEECH_EXACT_LABELS = frozenset(
    {
        "foreign language",
        "speaking in a foreign language",
        "speaking in foreign language",
        "音樂",
        "音乐",
        "掌聲",
        "掌声",
        "笑聲",
        "笑声",
        "笑",
        "雜音",
        "杂音",
        "無法辨識",
        "无法辨识",
        "ミュージック",
        "拍手",
        "笑い",
        "笑い声",
    }
)


_ANNOTATION_GROUP_RE = re.compile(
    r"\((?:[^()\r\n]{1,160})\)"
    r"|\[(?:[^\[\]\r\n]{1,160})\]"
    r"|\uff08(?:[^\uff08\uff09\r\n]{1,160})\uff09"
    r"|\u3010(?:[^\u3010\u3011\r\n]{1,160})\u3011"
)


def _is_annotation_separator(char: str) -> bool:
    if char in "()[]（）【】":
        return False
    return char.isspace() or unicodedata.category(char)[0] in {"P", "S"}


def _is_annotation_only_text(text: str) -> bool:
    """Return true when the complete hypothesis is bracketed metadata.

    This deliberately does not inspect words such as ``laugh`` or ``music``.
    It therefore handles caption conventions in languages the application has
    never seen while preserving speech that merely contains a parenthetical.
    """

    cursor = 0
    found_group = False
    length = len(text)
    while cursor < length:
        while cursor < length and _is_annotation_separator(text[cursor]):
            cursor += 1
        if cursor >= length:
            break
        match = _ANNOTATION_GROUP_RE.match(text, cursor)
        if match is None:
            return False
        found_group = True
        cursor = match.end()
    return found_group


def _is_non_speech_annotation(label: str) -> bool:
    normalized = " ".join(label.casefold().split()).strip(" .,:;!?-_~")
    if not normalized:
        return False
    if normalized in NON_SPEECH_EXACT_LABELS:
        return True
    words = re.findall(r"[a-z]+(?:'[a-z]+)?", normalized)
    if not words or not NON_SPEECH_EVENT_WORDS.intersection(words):
        return False
    allowed = NON_SPEECH_EVENT_WORDS | NON_SPEECH_CONTEXT_WORDS
    return all(word in allowed for word in words)


def _strip_non_speech_annotations(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        label = next((group for group in match.groups() if group is not None), "")
        return " " if _is_non_speech_annotation(label) else match.group(0)

    cleaned = NON_SPEECH_ANNOTATION_RE.sub(replace, text)
    cleaned = re.sub(r"[ \t]+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _sanitize_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("\ufffd", "").replace("\u0000", "")
    if _is_annotation_only_text(cleaned):
        return ""
    cleaned = _strip_non_speech_annotations(cleaned)
    if not cleaned:
        return ""
    # Streaming partials can split an annotation across responses, yielding
    # fragments such as ``(笑`` or ``笑)`` before the complete marker arrives.
    # Suppress a text-only fragment when its unwrapped label is a known event.
    fragment = cleaned.strip(" \t\r\n()[]{}（）【】")
    if not fragment or _is_non_speech_annotation(fragment):
        return ""
    if not any(unicodedata.category(char)[0] in {"L", "N"} for char in cleaned):
        return ""
    return cleaned


@dataclass(frozen=True)
class TranscriptEvidence:
    no_speech_prob: Optional[float] = None
    avg_logprob: Optional[float] = None
    mean_probability: Optional[float] = None
    min_probability: Optional[float] = None


@dataclass(frozen=True)
class EvidenceDecision:
    action: str
    reason: str
    evidence: TranscriptEvidence


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first_evidence_value(objects: list[Any], names: tuple[str, ...]) -> Optional[float]:
    for obj in objects:
        if obj is None:
            continue
        for name in names:
            value = _finite_float(getattr(obj, name, None))
            if value is not None:
                return value
    return None


def _extract_transcript_evidence(response: Any, segment: Any) -> TranscriptEvidence:
    """Read portable evidence fields without depending on one STT backend."""

    objects = [segment, response]
    no_speech_prob = _first_evidence_value(
        objects, ("no_speech_prob", "no_speech_probability", "nonspeech_prob")
    )
    avg_logprob = _first_evidence_value(
        objects, ("avg_logprob", "average_logprob", "mean_logprob")
    )
    mean_probability = _first_evidence_value(
        objects, ("probability", "mean_probability", "confidence")
    )
    min_probability = _first_evidence_value(
        objects, ("min_probability", "min_confidence")
    )

    token_probabilities: list[float] = []
    for token in getattr(segment, "tokens", None) or []:
        probability = _finite_float(
            getattr(token, "probability", getattr(token, "confidence", None))
        )
        if probability is not None:
            token_probabilities.append(probability)
    if token_probabilities:
        if mean_probability is None:
            mean_probability = sum(token_probabilities) / len(token_probabilities)
        if min_probability is None:
            min_probability = min(token_probabilities)

    return TranscriptEvidence(
        no_speech_prob=no_speech_prob,
        avg_logprob=avg_logprob,
        mean_probability=mean_probability,
        min_probability=min_probability,
    )


def _evidence_config(stt_session: Any) -> Dict[str, Any]:
    cfg = getattr(stt_session, "cfg", {})
    value = cfg.get("evidence_gate", {}) if isinstance(cfg, dict) else {}
    return value if isinstance(value, dict) else {}


def _config_enabled(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() not in {"0", "false", "no", "off"}
    return bool(value)


def _evaluate_transcript_evidence(
    stt_session: Any,
    text: str,
    response: Any,
    segment: Any,
    *,
    is_final: bool,
) -> EvidenceDecision:
    """Apply an open-set gate based on structure and decoder evidence."""

    evidence = _extract_transcript_evidence(response, segment)
    cfg = _evidence_config(stt_session)
    if not _config_enabled(cfg.get("enabled"), True):
        return EvidenceDecision("accept", "disabled", evidence)

    if _is_annotation_only_text(text):
        return EvidenceDecision("reject", "annotation-only", evidence)

    no_speech_threshold = _finite_float(cfg.get("no_speech_threshold"))
    if no_speech_threshold is None:
        no_speech_threshold = DEFAULT_EVIDENCE_NO_SPEECH_THRESHOLD
    avg_logprob_threshold = _finite_float(cfg.get("avg_logprob_threshold"))
    if avg_logprob_threshold is None:
        avg_logprob_threshold = DEFAULT_EVIDENCE_AVG_LOGPROB_THRESHOLD
    if (
        evidence.no_speech_prob is not None
        and evidence.avg_logprob is not None
        and evidence.no_speech_prob >= no_speech_threshold
        and evidence.avg_logprob <= avg_logprob_threshold
    ):
        return EvidenceDecision("reject", "no-speech-low-confidence", evidence)

    confirm_below = _finite_float(cfg.get("confirm_below_probability"))
    if confirm_below is None:
        confirm_below = DEFAULT_EVIDENCE_CONFIRM_PROBABILITY
    probability = evidence.mean_probability
    if probability is None or probability >= confirm_below or is_final:
        stt_session.evidence_candidate_text = ""
        stt_session.evidence_stable_updates = 0
        return EvidenceDecision("accept", "sufficient-evidence", evidence)

    candidate = " ".join(text.casefold().split())
    previous = str(getattr(stt_session, "evidence_candidate_text", "") or "")
    agreement_threshold = _finite_float(cfg.get("min_agreement"))
    if agreement_threshold is None:
        agreement_threshold = DEFAULT_EVIDENCE_MIN_AGREEMENT
    agrees = bool(previous) and (
        candidate.startswith(previous)
        or previous.startswith(candidate)
        or SequenceMatcher(None, previous, candidate).ratio() >= agreement_threshold
    )
    if agrees:
        stt_session.evidence_stable_updates = int(
            getattr(stt_session, "evidence_stable_updates", 0) or 0
        ) + 1
    else:
        stt_session.evidence_stable_updates = 1
    stt_session.evidence_candidate_text = candidate

    try:
        min_stable_updates = max(
            1,
            int(cfg.get("min_stable_updates", DEFAULT_EVIDENCE_MIN_STABLE_UPDATES)),
        )
    except (TypeError, ValueError):
        min_stable_updates = DEFAULT_EVIDENCE_MIN_STABLE_UPDATES
    if stt_session.evidence_stable_updates < min_stable_updates:
        return EvidenceDecision("hold", "awaiting-stability", evidence)

    stt_session.evidence_candidate_text = ""
    stt_session.evidence_stable_updates = 0
    return EvidenceDecision("accept", "stable-low-confidence", evidence)


def _apply_stt_engine_evidence_config(engine: Any, stt_cfg: Dict[str, Any]) -> None:
    """Configure native no-speech filtering when the backend exposes it."""

    evidence_cfg = stt_cfg.get("evidence_gate", {})
    if not isinstance(evidence_cfg, dict):
        evidence_cfg = {}
    threshold = _finite_float(evidence_cfg.get("nonspeech_prob"))
    if threshold is None:
        threshold = DEFAULT_EVIDENCE_NONSPEECH_PROB
    threshold = min(1.0, max(0.0, threshold))
    backend_cfg = getattr(getattr(engine, "asr", None), "cfg", None)
    if backend_cfg is not None and hasattr(backend_cfg, "nonspeech_prob"):
        backend_cfg.nonspeech_prob = threshold


MAX_DISPLAY_CHARS = 260
MAX_SENTENCES_DEFAULT = 2
MAX_SENTENCES_CJK = 1
DEFAULT_TRANSLATION_DEBOUNCE_MS = 300
DEFAULT_STALL_TIMEOUT_SEC = 15.0
DEFAULT_STALL_CHECK_INTERVAL_SEC = 5.0
DEFAULT_SEGMENT_MAX_CHARS = 160
DEFAULT_SEGMENT_MAX_MS = 4000
DEFAULT_CONTEXT_MAX_CHARS = 480
DEFAULT_CONTEXT_MAX_AGE_SEC = 30.0
INITIAL_CONFIG_TIMEOUT_SEC = 0.5
SENTENCE_ENDINGS = {".", "!", "?", "\u3002", "\uff01", "\uff1f", "\u2026"}
CLOSING_PUNCTUATION = {")", "]", "}", "\"", "'"}
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\u31f0-\u31ff]")



def _normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _split_sentences(text: str) -> list[str]:
    segments: list[str] = []
    start = 0
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if ch in SENTENCE_ENDINGS:
            end = idx + 1
            while end < len(text) and text[end] in CLOSING_PUNCTUATION:
                end += 1
            part = text[start:end].strip()
            if part:
                segments.append(part)
            start = end
            idx = end
            continue
        idx += 1
    tail = text[start:].strip()
    if tail:
        segments.append(tail)
    return segments


def _dedupe_repeated_sentences(text: str, max_repeats: int) -> str:
    if not text or max_repeats < 1:
        return text
    segments = _split_sentences(text)
    if not segments:
        return text
    deduped: list[str] = []
    last_key: Optional[str] = None
    repeat = 0
    for segment in segments:
        normalized = _normalize_text(segment).strip(")]}\"'")
        if not normalized:
            continue
        key = normalized.casefold()
        if key == last_key:
            repeat += 1
        else:
            repeat = 1
            last_key = key
        if repeat > max_repeats:
            continue
        deduped.append(segment.strip())
    if not deduped:
        return ""
    return " ".join(deduped).strip()


def _trim_text(text: str, max_chars: int) -> str:
    if not max_chars or len(text) <= max_chars:
        return text
    trimmed = text[-max_chars:]
    return trimmed.lstrip(" \t\n\r.,;:!?-")


def _compact_text(
    text: str,
    max_chars: int,
    max_sentences_default: int,
    max_sentences_cjk: int,
) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""
    if not max_chars or len(normalized) <= max_chars:
        return normalized
    sentence_limit = (
        max_sentences_cjk if CJK_RE.search(normalized) else max_sentences_default
    )
    segments = _split_sentences(normalized)
    if len(segments) > sentence_limit:
        tail = " ".join(segments[-sentence_limit:])
        return _trim_text(tail, max_chars)
    return _trim_text(normalized, max_chars)


def _merge_utterance_text(current: str, incoming: str) -> str:
    if not current:
        return incoming
    if incoming.startswith(current):
        return incoming
    if current.startswith(incoming):
        return current
    return f"{current} {incoming}".strip()


def _segment_identity(segment: Any) -> Optional[tuple[str, str]]:
    """Return a stable identity for one upstream ASR segment when available.

    WhisperLiveKit has used a few names for the same protocol concept over
    time.  Keep this compatibility probing at the STT boundary; never use the
    transcript text as an identity because repeated speech is valid input.
    """

    for name in ("segment_id", "id", "uid", "start_ts", "start_time", "start"):
        value = getattr(segment, name, None)
        if value is not None:
            return name, str(value)
    return None


def _same_asr_segment(
    session: "TranslationSession", text: str, identity: Optional[tuple[str, str]]
) -> bool:
    if identity is not None and session.asr_segment_identity is not None:
        return identity == session.asr_segment_identity
    # A buffer partial has no segment metadata.  When the same utterance later
    # becomes a timed Segment, use its cumulative prefix relationship to adopt
    # the identity instead of appending the complete final snapshot.
    previous = session.asr_snapshot_text
    return bool(previous and (text.startswith(previous) or previous.startswith(text)))


def _snapshot_suffix(text: str, committed: str) -> str:
    if not committed:
        return text
    if text.startswith(committed):
        return text[len(committed):].lstrip()
    # A same-segment correction before a force flush is a replacement, not a
    # second utterance.  A correction after a committed prefix cannot be
    # replayed safely without token timestamps, so preserve the committed
    # cursor and wait for a genuinely new suffix.
    if committed.startswith(text):
        return ""
    # SimulStreaming can revise words inside an already committed prefix while
    # continuing the same timed segment.  Align the two snapshots and emit only
    # text after the last well-supported match near the end of the committed
    # snapshot.  If alignment is weak, prefer waiting over replaying the whole
    # committed line as a new subtitle.
    matcher = SequenceMatcher(None, committed, text, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    if not blocks:
        return ""
    matched = sum(block.size for block in blocks)
    last = max(blocks, key=lambda block: block.a + block.size)
    coverage = matched / max(1, len(committed))
    committed_reach = (last.a + last.size) / max(1, len(committed))
    if coverage < 0.6 or committed_reach < 0.8:
        return ""
    return text[last.b + last.size :].lstrip()


def _is_new_timed_segment(
    session: "TranslationSession", identity: Optional[tuple[str, str]]
) -> bool:
    return bool(
        identity is not None
        and session.asr_segment_identity is not None
        and identity != session.asr_segment_identity
    )


def _reset_asr_segment_tracking(session: "TranslationSession") -> None:
    session.asr_segment_identity = None
    session.asr_snapshot_text = ""
    session.asr_committed_text = ""
    session.utterance_prefix_text = ""
    session.segment_chunk_text = ""


def _translation_cache_key(cfg: Dict[str, Any]) -> str:
    return json.dumps(cfg, sort_keys=True, default=str)


def _normalize_cache_size(value: object, default: int) -> int:
    try:
        size = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(0, size)


def _normalize_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _normalize_non_negative_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _normalize_timeout(value: object, default: float) -> float:
    try:
        timeout = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if timeout < 0:
        return default
    return timeout


def _normalize_ratio(value: object, default: float) -> float:
    try:
        ratio = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not 0.0 <= ratio <= 1.0:
        return default
    return ratio


def _repetition_search_text(text: str) -> str:
    """Remove formatting noise while preserving spoken letters and numbers."""

    return "".join(
        char.casefold()
        for char in _normalize_text(text)
        if unicodedata.category(char)[0] in {"L", "N"}
    )


def _find_repetition_loop(
    text: str,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[tuple[str, int, int]]:
    """Return ``(unit, repeats, span)`` for a conservative runaway loop.

    Genuine speech can repeat a word a few times.  A loop is reported only
    when an exact adjacent unit dominates the transcript or grows to twice the
    configured minimum span.  One- and two-character units use a higher repeat
    threshold so ordinary emphasis does not reset the recognizer.
    """

    guard = config if isinstance(config, dict) else {}
    if guard.get("enabled", True) is False:
        return None
    min_repeats = _normalize_positive_int(
        guard.get("min_repeats"), DEFAULT_REPETITION_MIN_REPEATS
    )
    short_unit_min_repeats = _normalize_positive_int(
        guard.get("short_unit_min_repeats"),
        DEFAULT_REPETITION_SHORT_UNIT_MIN_REPEATS,
    )
    min_span = _normalize_positive_int(
        guard.get("min_span_chars"), DEFAULT_REPETITION_MIN_SPAN_CHARS
    )
    max_unit = _normalize_positive_int(
        guard.get("max_unit_chars"), DEFAULT_REPETITION_MAX_UNIT_CHARS
    )
    min_coverage = _normalize_ratio(
        guard.get("min_coverage"), DEFAULT_REPETITION_MIN_COVERAGE
    )
    searchable = _repetition_search_text(text)
    if len(searchable) < min_span:
        return None

    max_unit = min(max_unit, len(searchable) // max(1, min_repeats))
    for unit_length in range(1, max_unit + 1):
        required_repeats = (
            short_unit_min_repeats if unit_length <= 2 else min_repeats
        )
        required_chars = unit_length * required_repeats
        if required_chars > len(searchable):
            continue
        last_start = len(searchable) - required_chars
        for start in range(last_start + 1):
            unit = searchable[start : start + unit_length]
            repeats = 1
            cursor = start + unit_length
            while searchable[cursor : cursor + unit_length] == unit:
                repeats += 1
                cursor += unit_length
            if repeats < required_repeats:
                continue
            span = repeats * unit_length
            coverage = span / len(searchable)
            if span >= min_span and (
                coverage >= min_coverage or span >= min_span * 2
            ):
                return unit, repeats, span
    return None


def _normalize_delay_ms(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _is_local_client(websocket: WebSocket) -> bool:
    client = websocket.client
    if not client or not client.host:
        return False
    host = client.host
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


async def _resolve_translator(
    cache: LRUCache[str, Any],
    cfg: Dict[str, Any],
    http_client: httpx.AsyncClient,
    build_lock: asyncio.Lock,
):
    key = _translation_cache_key(cfg)
    cached, hit = cache.get(key)
    if hit:
        return cached
    async with build_lock:
        # Recheck after waiting so simultaneous sessions never initialize the
        # same local model more than once.
        cached, hit = cache.get(key)
        if hit:
            return cached
        translator = await asyncio.to_thread(build_translator, cfg, http_client)
        evicted = cache.set(key, translator)
        if evicted is not None and hasattr(evicted, "aclose"):
            await evicted.aclose()
        return translator


@dataclass
class TranslationRequest:
    text: str
    language: Optional[str]
    is_final: bool
    seq: int
    version: int
    chunk_revision: int = 0
    context: str = ""
    source_snapshot: str = ""
    segment_identity: Optional[tuple[str, str]] = None


class TranslationSession:
    def __init__(
        self,
        cfg: Dict[str, Any],
        default_lang: Optional[str],
        translator,
        scheduler: TranslationScheduler,
        subtitle_cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.default_lang = default_lang
        self.cfg = dict(cfg)
        self.translator = translator
        self.scheduler = scheduler
        self.session_id = uuid.uuid4().hex
        self.translate_partials = bool(self.cfg.get("partial", False))
        subtitle_cfg = subtitle_cfg or {}
        self.max_chars = _normalize_positive_int(
            subtitle_cfg.get("max_chars"), MAX_DISPLAY_CHARS
        )
        self.max_sentences_default = _normalize_positive_int(
            subtitle_cfg.get("max_sentences_default"), MAX_SENTENCES_DEFAULT
        )
        self.max_sentences_cjk = _normalize_positive_int(
            subtitle_cfg.get("max_sentences_cjk"), MAX_SENTENCES_CJK
        )
        self.max_repeat_sentences = _normalize_positive_int(
            subtitle_cfg.get("max_repeat_sentences"), 1
        )
        self.seq = 0
        self.pending_seq: Optional[int] = None
        self.chunk_revisions: dict[int, int] = {}
        self.latest_request_versions: dict[int, int] = {}
        self.translation_task: Optional[asyncio.Task[None]] = None
        self.active_translation_request: Optional[TranslationRequest] = None
        self.retired_translation_tasks: set[asyncio.Task[None]] = set()
        self.background_provider_tasks: set[asyncio.Task[str]] = set()
        self.pending_final_requests: deque[TranslationRequest] = deque()
        self.debounce_ms = _normalize_delay_ms(
            self.cfg.get("debounce_ms"), DEFAULT_TRANSLATION_DEBOUNCE_MS
        )
        self.debounce_version = 0
        self.debounce_task: Optional[asyncio.Task[None]] = None
        self.utterance_text = ""
        self.utterance_language: Optional[str] = None
        # ASR snapshots and subtitle chunks intentionally have separate
        # lifecycles.  ``asr_snapshot_text`` survives a force flush so a
        # later cumulative snapshot contributes only its new suffix.
        self.asr_segment_identity: Optional[tuple[str, str]] = None
        self.asr_snapshot_text = ""
        self.asr_committed_text = ""
        self.utterance_prefix_text = ""
        self.segment_chunk_text = ""
        self.source_revision = 0
        self.pending_request: Optional[TranslationRequest] = None
        self.request_version = 0
        self.context_max_chars = _normalize_non_negative_int(
            self.cfg.get("context_max_chars"), DEFAULT_CONTEXT_MAX_CHARS
        )
        self.context_max_age_sec = _normalize_timeout(
            self.cfg.get("context_max_age_sec"), DEFAULT_CONTEXT_MAX_AGE_SEC
        )
        self.stable_source_history: deque[tuple[float, str]] = deque()
        self.segment_max_chars = _normalize_non_negative_int(
            self.cfg.get("segment_max_chars"), DEFAULT_SEGMENT_MAX_CHARS
        )
        self.segment_max_ms = _normalize_delay_ms(
            self.cfg.get("segment_max_ms"), DEFAULT_SEGMENT_MAX_MS
        )
        self.segment_start_ts: Optional[float] = None


class SttSession:
    def __init__(
        self,
        cfg: Dict[str, Any],
        engine: TranscriptionEngine,
        audio_processor: AudioProcessor,
    ) -> None:
        self.cfg = dict(cfg)
        self.default_language = str(self.cfg.get("language", "auto"))
        self.engine = engine
        self.audio_processor = audio_processor
        self.first_audio_ts: Optional[float] = None
        self.last_audio_ts = time.monotonic()
        self.last_result_ts = time.monotonic()
        self.result_seen = False
        self.last_response_ts = time.monotonic()
        self.response_seen = False
        self.last_response_status = ""
        self.reset_lock = asyncio.Lock()
        now = time.monotonic()
        self.engine_start_ts = now
        self.last_cleanup_ts = now
        self.last_gpu_log_ts = 0.0
        self.engine_builds = 1
        self.last_repetition_reset_ts = 0.0
        self.evidence_candidate_text = ""
        self.evidence_stable_updates = 0
        self.evidence_rejections = 0
        self.last_evidence_reset_ts = 0.0


async def _safe_send(websocket: WebSocket, payload: Dict[str, Any]) -> bool:
    try:
        await websocket.send_text(json.dumps(payload))
        return True
    except (WebSocketDisconnect, RuntimeError, OSError):
        return False


def _capture_snapshot(stt_cfg: Dict[str, Any], translation_cfg: Dict[str, Any]) -> Dict[str, Any]:
    stt = {"model": str(stt_cfg.get("model") or "medium")}
    if stt_cfg.get("backend"):
        stt["backend"] = str(stt_cfg["backend"])
    translation: Dict[str, Any] = {
        "target_language": str(translation_cfg.get("target_language") or "zh-TW"),
        "partial": bool(translation_cfg.get("partial", False)),
    }
    selection = translation_cfg.get("selection")
    if isinstance(selection, dict) and selection.get("kind") and selection.get("id"):
        translation["selection"] = {
            "kind": str(selection["kind"]),
            "id": str(selection["id"]),
        }
    elif translation_cfg.get("profile_id"):
        translation["selection"] = {"kind": "profile", "id": str(translation_cfg["profile_id"])}
    return {"stt": stt, "translation": translation}


async def _send_capture_status(
    websocket: WebSocket,
    phase: str,
    message: str,
    stt_cfg: Dict[str, Any],
    translation_cfg: Dict[str, Any],
) -> bool:
    return await _safe_send(
        websocket,
        {
            "type": "status",
            "phase": phase,
            "message": message,
            "snapshot": _capture_snapshot(stt_cfg, translation_cfg),
        },
    )


async def _send_subtitle(
    websocket: WebSocket,
    text: str,
    translated: str,
    language: Optional[str],
    is_final: bool,
    seq: int,
) -> bool:
    return await _safe_send(
        websocket,
        {
            "type": "subtitle",
            "original": text,
            "translated": translated,
            "language": language,
            "timestamp": int(time.time() * 1000),
            "final": is_final,
            "seq": seq,
        },
    )


def _ensure_pending_seq(session: TranslationSession) -> int:
    if session.pending_seq is None:
        session.seq += 1
        session.pending_seq = session.seq
        session.chunk_revisions[session.pending_seq] = 0
    return session.pending_seq


def _current_seq(session: TranslationSession) -> int:
    return session.pending_seq if session.pending_seq is not None else session.seq


def _touch_source_revision(session: TranslationSession, seq: int) -> int:
    revision = session.chunk_revisions.get(seq, 0) + 1
    session.chunk_revisions[seq] = revision
    session.source_revision += 1
    return revision


def _stable_context_snapshot(session: TranslationSession, now: Optional[float] = None) -> str:
    if session.context_max_chars <= 0:
        return ""
    current = time.monotonic() if now is None else now
    cutoff = current - session.context_max_age_sec
    while session.stable_source_history and session.stable_source_history[0][0] < cutoff:
        session.stable_source_history.popleft()
    return _trim_text(
        "\n".join(text for _, text in session.stable_source_history),
        session.context_max_chars,
    )


def _remember_stable_source(session: TranslationSession, text: str) -> None:
    normalized = _normalize_text(text)
    if not normalized or session.context_max_chars <= 0:
        return
    now = time.monotonic()
    session.stable_source_history.append((now, normalized))
    _stable_context_snapshot(session, now)


def _close_open_chunk(session: TranslationSession, seq: int, text: str) -> None:
    """Close a chunk without changing the monotonic sequence lifecycle."""

    if session.pending_seq != seq:
        return
    _remember_stable_source(session, text)
    session.pending_seq = None
    session.utterance_text = ""
    session.utterance_language = None
    session.utterance_prefix_text = ""
    session.segment_chunk_text = ""
    session.segment_start_ts = None


def _update_asr_snapshot(
    session: TranslationSession,
    text: str,
    language: Optional[str],
    segment_identity: Optional[tuple[str, str]],
) -> tuple[bool, int]:
    """Apply one cumulative ASR snapshot and return (changed, chunk revision)."""

    normalized = _normalize_text(text)
    if not normalized:
        return False, session.chunk_revisions.get(session.pending_seq or session.seq, 0)
    same_segment = _same_asr_segment(session, normalized, segment_identity)
    if same_segment and normalized == session.asr_snapshot_text:
        return False, session.chunk_revisions.get(session.pending_seq or session.seq, 0)

    seq = _ensure_pending_seq(session)
    if not same_segment:
        # A new upstream segment starts with the text accumulated from the
        # previous one.  Its own snapshot contribution can then be replaced
        # on the next cumulative update without appending the whole snapshot.
        session.utterance_prefix_text = session.utterance_text
        session.asr_committed_text = ""
        session.segment_chunk_text = normalized
    else:
        session.segment_chunk_text = _snapshot_suffix(
            normalized, session.asr_committed_text
        )

    session.asr_segment_identity = segment_identity
    session.asr_snapshot_text = normalized
    candidate = _merge_utterance_text(
        session.utterance_prefix_text, session.segment_chunk_text
    )
    session.utterance_text = _compact_text(
        candidate,
        session.max_chars,
        session.max_sentences_default,
        session.max_sentences_cjk,
    )
    session.utterance_language = language
    return True, _touch_source_revision(session, seq)


def _should_refresh_engine(current: Dict[str, Any], updated: Dict[str, Any]) -> bool:
    return current != updated


def _track_background_provider_task(
    session: TranslationSession, task: asyncio.Task[str]
) -> None:
    if task.done():
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Background translation provider failed", exc_info=True)
        return
    session.background_provider_tasks.add(task)

    def _consume(done: asyncio.Task[str]) -> None:
        session.background_provider_tasks.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Background translation provider failed", exc_info=True)

    task.add_done_callback(_consume)


async def _translate_text(
    session: TranslationSession,
    text: str,
    language: Optional[str],
    websocket: WebSocket,
    context: str = "",
) -> str:
    translation_timeout = _normalize_timeout(session.cfg.get("timeout_sec"), 8.0)
    if context:
        operation = session.scheduler.translate(
            session.translator, text, language, context=context
        )
    else:
        operation = session.scheduler.translate(session.translator, text, language)
    if not bool(getattr(session.translator, "cancellation_safe", False)):
        # Cancelling asyncio.to_thread does not stop local model inference. Keep
        # its scheduler slot until the worker thread really finishes.
        provider_task = asyncio.create_task(operation)
        try:
            if translation_timeout > 0:
                return await asyncio.wait_for(
                    asyncio.shield(provider_task), timeout=translation_timeout
                )
            return await asyncio.shield(provider_task)
        except asyncio.TimeoutError:
            _track_background_provider_task(session, provider_task)
            return ""
        except asyncio.CancelledError:
            _track_background_provider_task(session, provider_task)
            raise
        except Exception as exc:
            await _safe_send(
                websocket,
                {"type": "error", "message": f"Translation error: {type(exc).__name__}"},
            )
            return ""

    if translation_timeout > 0:
        try:
            return await asyncio.wait_for(
                operation,
                timeout=translation_timeout,
            )
        except asyncio.TimeoutError:
            return ""
        except Exception as exc:
            await _safe_send(
                websocket,
                {"type": "error", "message": f"Translation error: {type(exc).__name__}"},
            )
            return ""
    try:
        return await operation
    except Exception as exc:
        await _safe_send(
            websocket,
            {"type": "error", "message": f"Translation error: {type(exc).__name__}"},
        )
        return ""


async def _translate_and_send(
    websocket: WebSocket,
    session: TranslationSession,
    request: TranslationRequest,
) -> None:
    translated = await _translate_text(
        session, request.text, request.language, websocket, request.context
    )
    if session.latest_request_versions.get(request.seq) != request.version:
        return
    if session.chunk_revisions.get(request.seq) != request.chunk_revision:
        return
    if not translated:
        # Empty output is a provider failure/timeout, not a chunk boundary.
        # Keep newer ASR state and the sequence available for a later retry.
        return
    await _send_subtitle(
        websocket,
        request.text,
        translated,
        request.language,
        request.is_final,
        request.seq,
    )


async def _translation_worker(
    websocket: WebSocket,
    session: TranslationSession,
) -> None:
    while True:
        request = None
        if session.pending_final_requests:
            request = session.pending_final_requests.popleft()
        elif session.pending_request is not None:
            request = session.pending_request
            session.pending_request = None
        if request is None:
            return
        session.active_translation_request = request
        try:
            await _translate_and_send(websocket, session, request)
        except asyncio.CancelledError:
            return
        finally:
            if session.active_translation_request is request:
                session.active_translation_request = None


def _enqueue_translation_request(
    session: TranslationSession, request: TranslationRequest
) -> None:
    if request.is_final:
        # A final revision for the same chunk supersedes an older queued final,
        # while different chunks remain ordered and cannot be lost.
        session.pending_final_requests = deque(
            queued
            for queued in session.pending_final_requests
            if queued.seq != request.seq
        )
        session.pending_final_requests.append(request)
        return
    # Draft/partial work is latest-wins.  Do not let it jump ahead of a final
    # request that is already waiting for the same managed runtime.
    if not any(queued.seq == request.seq for queued in session.pending_final_requests):
        session.pending_request = request


def _retire_translation_task(
    session: TranslationSession, task: asyncio.Task[None]
) -> None:
    session.retired_translation_tasks.add(task)

    def _consume(done: asyncio.Task[None]) -> None:
        session.retired_translation_tasks.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Retired translation task failed", exc_info=True)

    task.add_done_callback(_consume)


async def _cancel_session_translation_tasks(session: TranslationSession) -> None:
    tasks = set(session.retired_translation_tasks)
    if session.translation_task and not session.translation_task.done():
        tasks.add(session.translation_task)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    session.retired_translation_tasks.clear()
    session.translation_task = None
    session.active_translation_request = None
    session.pending_request = None
    session.pending_final_requests.clear()
    if session.background_provider_tasks:
        await asyncio.gather(*tuple(session.background_provider_tasks), return_exceptions=True)


def _queue_translation(
    websocket: WebSocket,
    session: TranslationSession,
    text: str,
    language: Optional[str],
    is_final: bool,
    seq: int,
) -> None:
    if seq not in session.chunk_revisions:
        session.chunk_revisions[seq] = 0
    session.request_version += 1
    request = TranslationRequest(
        text=text,
        language=language,
        is_final=is_final,
        seq=seq,
        version=session.request_version,
        chunk_revision=session.chunk_revisions.get(seq, 0),
        context=_stable_context_snapshot(session),
        source_snapshot=session.asr_snapshot_text,
        segment_identity=session.asr_segment_identity,
    )
    session.latest_request_versions[seq] = request.version
    active = session.active_translation_request
    if (
        bool(getattr(session.translator, "cancellation_safe", False))
        and active is not None
        and active.seq == request.seq
        and session.translation_task
        and not session.translation_task.done()
    ):
        # HTTP providers can be cancelled when only the same chunk is being
        # revised.  A different final chunk stays queued for ordered delivery.
        _retire_translation_task(session, session.translation_task)
        session.translation_task.cancel()
        session.translation_task = None
        session.active_translation_request = None

    _enqueue_translation_request(session, request)
    if not session.translation_task or session.translation_task.done():
        session.translation_task = asyncio.create_task(
            _translation_worker(websocket, session)
        )
        return

    # The worker owns non-cancellable local inference.  It keeps one active
    # request and the bounded latest-pending/final queue above.
    if bool(getattr(session.translator, "cancellation_safe", False)):
        return
    if session.translation_task.done():
        session.translation_task = asyncio.create_task(
            _translation_worker(websocket, session)
        )


def _cancel_translation_debounce(session: TranslationSession) -> None:
    session.debounce_version += 1
    if session.debounce_task and not session.debounce_task.done():
        session.debounce_task.cancel()
    session.debounce_task = None


async def _debounced_translation_start(
    websocket: WebSocket,
    session: TranslationSession,
    version: int,
) -> None:
    delay_ms = session.debounce_ms
    if delay_ms > 0:
        try:
            await asyncio.sleep(delay_ms / 1000.0)
        except asyncio.CancelledError:
            return
    if version != session.debounce_version:
        return
    text = session.utterance_text
    if not text:
        return
    seq = _ensure_pending_seq(session)
    _queue_translation(
        websocket,
        session,
        text,
        session.utterance_language,
        True,
        seq,
    )


async def _schedule_translation_debounce(
    websocket: WebSocket,
    session: TranslationSession,
) -> None:
    session.debounce_version += 1
    version = session.debounce_version
    if session.debounce_task and not session.debounce_task.done():
        session.debounce_task.cancel()
    session.debounce_task = asyncio.create_task(
        _debounced_translation_start(websocket, session, version)
    )


def _should_force_segment(session: TranslationSession, now: float) -> bool:
    if (
        session.segment_max_chars > 0
        and len(session.utterance_text) >= session.segment_max_chars
    ):
        return True
    if session.segment_start_ts is None or session.segment_max_ms <= 0:
        return False
    elapsed_ms = (now - session.segment_start_ts) * 1000.0
    return elapsed_ms >= session.segment_max_ms


async def _apply_stt_update(
    stt_session: SttSession,
    translation_session: TranslationSession,
    stt_update: Optional[Dict[str, Any]],
    websocket: WebSocket,
) -> None:
    normalized_update = _normalize_stt_update(stt_update)
    if not normalized_update:
        return
    current_cfg = stt_session.cfg
    merged_cfg = _merge_config(current_cfg, normalized_update)
    if not _should_refresh_engine(current_cfg, merged_cfg):
        return
    try:
        if getattr(websocket.state, "capture_version", None) == 2:
            _validate_v2_stt_selection(
                merged_cfg,
                getattr(websocket.state, "capture_config", None),
                getattr(websocket.state, "capture_revision", None),
            )
        merged_cfg = await _ensure_model_available(
            merged_cfg, websocket, app.state.model_download_lock
        )
        engine_kwargs = _build_engine_kwargs(merged_cfg)
        engine_kwargs["pcm_input"] = bool(merged_cfg.get("pcm_input", False))
        new_engine = await _construct_stt_engine(engine_kwargs)
        _apply_stt_engine_evidence_config(new_engine, merged_cfg)
    except Exception as exc:
        await _safe_send(
            websocket,
            {
                "type": "error",
                "message": f"STT config error: {type(exc).__name__}",
            },
        )
        return
    old_engine = stt_session.engine
    stt_session.cfg = merged_cfg
    stt_session.default_language = str(merged_cfg.get("language", "auto"))
    stt_session.engine = new_engine
    stt_session.audio_processor.transcription_engine = new_engine
    stt_session.last_result_ts = time.monotonic()
    stt_session.result_seen = False
    stt_session.last_response_ts = time.monotonic()
    stt_session.response_seen = False
    stt_session.last_response_status = ""
    stt_session.evidence_candidate_text = ""
    stt_session.evidence_stable_updates = 0
    stt_session.evidence_rejections = 0
    translation_session.default_lang = stt_session.default_language
    stt_session.engine_builds += 1
    gpu_log_interval_sec = _normalize_timeout(
        stt_session.cfg.get("gpu_log_interval_sec"), 0.0
    )
    if gpu_log_interval_sec > 0:
        _log_cuda_memory("engine update")
    old_engine = None
    _release_memory()
    await _safe_send(
        websocket,
        {
            "type": "status",
            "message": "stt updated",
        },
    )


async def _handle_results(
    websocket: WebSocket,
    results_generator,
    session: TranslationSession,
    stt_session: SttSession,
    *,
    reset_requests: Optional[asyncio.Queue[str]] = None,
) -> None:
    async for response in results_generator:
        # WhisperLiveKit emits ``no_audio_detected`` for a healthy silent
        # stream.  Track processor responses separately from subtitle text so
        # muted media is not mistaken for a stalled STT engine.
        stt_session.last_response_ts = time.monotonic()
        stt_session.response_seen = True
        stt_session.last_response_status = str(
            getattr(response, "status", "") or ""
        ).strip().lower()
        if getattr(response, "error", ""):
            await _safe_send(
                websocket,
                {
                    "type": "error",
                    "message": response.error,
                },
            )
            continue

        text = ""
        language = None
        is_final = False
        segment = _pick_latest_segment(response.lines or [])
        if segment is not None:
            text = str(getattr(segment, "text", "") or "").strip()
            language = getattr(segment, "detected_language", None)
            is_final = True
        elif response.buffer_transcription:
            text = response.buffer_transcription.strip()
            is_final = False

        stt_cfg = getattr(stt_session, "cfg", {})
        guard_cfg = (
            stt_cfg.get("repetition_guard", {})
            if isinstance(stt_cfg, dict)
            else {}
        )
        # Inspect the raw transcript before removing event annotations.  This
        # lets a long ``(笑)`` loop reset upstream ASR context even though none
        # of those annotations should reach the subtitle or translator.
        repetition = _find_repetition_loop(text, guard_cfg)
        if repetition is not None:
            unit, repeats, span = repetition
            logger.warning(
                "Suppressing runaway STT repetition: unit=%r repeats=%d span=%d",
                unit,
                repeats,
                span,
            )
            now = time.monotonic()
            cooldown = _normalize_timeout(
                guard_cfg.get("reset_cooldown_sec")
                if isinstance(guard_cfg, dict)
                else None,
                DEFAULT_REPETITION_RESET_COOLDOWN_SEC,
            )
            last_reset = float(
                getattr(stt_session, "last_repetition_reset_ts", 0.0) or 0.0
            )
            if (
                reset_requests is not None
                and now - last_reset >= cooldown
                and reset_requests.empty()
            ):
                stt_session.last_repetition_reset_ts = now
                reset_requests.put_nowait("repetition loop")
                return
            continue

        decision = _evaluate_transcript_evidence(
            stt_session,
            text,
            response,
            segment,
            is_final=is_final,
        )
        if decision.action == "hold":
            logger.debug(
                "Holding low-confidence STT hypothesis: probability=%s",
                decision.evidence.mean_probability,
            )
            continue
        if decision.action == "reject":
            stt_session.evidence_rejections = int(
                getattr(stt_session, "evidence_rejections", 0) or 0
            ) + 1
            logger.info(
                "Suppressing STT hypothesis by evidence gate: reason=%s "
                "no_speech=%s avg_logprob=%s probability=%s",
                decision.reason,
                decision.evidence.no_speech_prob,
                decision.evidence.avg_logprob,
                decision.evidence.mean_probability,
            )
            evidence_cfg = _evidence_config(stt_session)
            try:
                reset_after = max(
                    0,
                    int(
                        evidence_cfg.get(
                            "reset_after_rejections",
                            DEFAULT_EVIDENCE_RESET_REJECTIONS,
                        )
                    ),
                )
            except (TypeError, ValueError):
                reset_after = DEFAULT_EVIDENCE_RESET_REJECTIONS
            reset_cooldown = _finite_float(
                evidence_cfg.get("reset_cooldown_sec")
            )
            if reset_cooldown is None:
                reset_cooldown = DEFAULT_EVIDENCE_RESET_COOLDOWN_SEC
            now = time.monotonic()
            last_reset = float(
                getattr(stt_session, "last_evidence_reset_ts", 0.0) or 0.0
            )
            if (
                reset_after > 0
                and stt_session.evidence_rejections >= reset_after
                and reset_requests is not None
                and now - last_reset >= max(0.0, reset_cooldown)
                and reset_requests.empty()
            ):
                stt_session.last_evidence_reset_ts = now
                stt_session.evidence_rejections = 0
                reset_requests.put_nowait("low speech evidence")
                return
            continue
        stt_session.evidence_rejections = 0
        text = _sanitize_text(text)
        if not text:
            continue
        # Do not deduplicate transcript text here.  Repeated speech is valid;
        # cumulative-snapshot replacement belongs to the segment identity
        # state machine above, not to a similarity heuristic.

        display_text = _compact_text(
            text,
            session.max_chars,
            session.max_sentences_default,
            session.max_sentences_cjk,
        )
        if not display_text:
            continue
        stt_session.last_result_ts = time.monotonic()
        stt_session.result_seen = True

        language = _guess_language(display_text, language or session.default_lang)
        segment_identity = _segment_identity(segment) if segment is not None else None
        if is_final:
            if _is_new_timed_segment(session, segment_identity):
                # A timed ASR segment boundary, unlike translation completion,
                # owns the subtitle chunk boundary.  Finalize the previous
                # immutable chunk before applying the new segment snapshot.
                previous_seq = session.pending_seq
                previous_text = session.utterance_text
                previous_language = session.utterance_language
                if previous_seq is not None and previous_text:
                    _cancel_translation_debounce(session)
                    _queue_translation(
                        websocket,
                        session,
                        previous_text,
                        previous_language,
                        True,
                        previous_seq,
                    )
                    session.asr_committed_text = session.asr_snapshot_text
                    _close_open_chunk(session, previous_seq, previous_text)
                _reset_asr_segment_tracking(session)
            changed, revision = _update_asr_snapshot(
                session, text, language, segment_identity
            )
            if not changed:
                continue
            if not session.utterance_text:
                session.segment_start_ts = time.monotonic()
            language = _guess_language(
                session.utterance_text,
                language or session.default_lang,
            )
            session.utterance_language = language
            seq = _ensure_pending_seq(session)
            await _send_subtitle(
                websocket,
                session.utterance_text,
                "",
                language,
                True,
                seq,
            )
            if _should_force_segment(session, time.monotonic()):
                flush_text = session.utterance_text
                flush_lang = session.utterance_language
                _cancel_translation_debounce(session)
                _queue_translation(
                    websocket,
                    session,
                    flush_text,
                    flush_lang,
                    True,
                    seq,
                )
                # Commit the upstream snapshot before opening the next chunk;
                # translation completion must not own this boundary.
                session.asr_committed_text = session.asr_snapshot_text
                _close_open_chunk(session, seq, flush_text)
            else:
                await _schedule_translation_debounce(websocket, session)
        else:
            changed, revision = _update_asr_snapshot(
                session, text, language, segment_identity
            )
            if not changed:
                continue
            seq = _ensure_pending_seq(session)
            await _send_subtitle(
                websocket,
                display_text,
                "",
                language,
                False,
                seq,
            )
            if session.translate_partials:
                if _should_force_segment(session, time.monotonic()):
                    flush_text = session.utterance_text
                    flush_lang = session.utterance_language
                    _cancel_translation_debounce(session)
                    _queue_translation(
                        websocket,
                        session,
                        flush_text,
                        flush_lang,
                        False,
                        seq,
                    )
                    session.asr_committed_text = session.asr_snapshot_text
                    _close_open_chunk(session, seq, flush_text)
                else:
                    await _schedule_translation_debounce(websocket, session)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    config_path = _configured_path()
    server_id = server_id_for_config(config_path, cfg)
    instance_id = str(os.getenv("STT_INSTANCE_ID") or uuid.uuid4().hex)
    app.state.config_path = config_path
    app.state.server_id = server_id
    app.state.instance_id = instance_id
    app.state.ready = False
    app.state.server_config = _get_server_config(cfg)
    stt_cfg = cfg.get("stt", {})
    translation_cfg = cfg.get("translation", {})
    subtitle_cfg = cfg.get("subtitle", {})
    translator_cache_size = _normalize_cache_size(
        translation_cfg.get("translator_cache_size", 4),
        4,
    )
    scheduler_cfg = translation_cfg.get("scheduler", {})
    if not isinstance(scheduler_cfg, dict):
        scheduler_cfg = {}
    global_max_concurrency = _normalize_positive_int(
        scheduler_cfg.get(
            "global_max_concurrency",
            translation_cfg.get("max_concurrency", 8),
        ),
        8,
    )
    default_profile_max_concurrency = _normalize_positive_int(
        scheduler_cfg.get("default_profile_max_concurrency", 4),
        4,
    )

    app.state.default_stt_cfg = stt_cfg
    app.state.translation_cfg = translation_cfg
    app.state.subtitle_cfg = {
        "max_chars": _normalize_positive_int(
            subtitle_cfg.get("max_chars"), MAX_DISPLAY_CHARS
        ),
        "max_sentences_default": _normalize_positive_int(
            subtitle_cfg.get("max_sentences_default"), MAX_SENTENCES_DEFAULT
        ),
        "max_sentences_cjk": _normalize_positive_int(
            subtitle_cfg.get("max_sentences_cjk"), MAX_SENTENCES_CJK
        ),
        "max_repeat_sentences": _normalize_positive_int(
            subtitle_cfg.get("max_repeat_sentences"), 1
        ),
    }
    app.state.translator_cache = LRUCache(translator_cache_size)
    app.state.translator_build_lock = asyncio.Lock()
    app.state.translation_scheduler = TranslationScheduler(
        global_max_concurrency=global_max_concurrency,
        default_profile_max_concurrency=default_profile_max_concurrency,
    )
    app.state.http_client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=max(global_max_concurrency * 2, 8),
            max_keepalive_connections=max(global_max_concurrency, 4),
        )
    )
    app.state.model_download_lock = asyncio.Lock()
    app.state.management_install_lock = asyncio.Lock()
    async def clear_translators():
        translators = app.state.translator_cache.clear()
        for translator in translators:
            await translator.aclose()
        translators.clear()
        translator = None
        _release_memory()

    def current_config_snapshot():
        # The runner supplies STT_CONFIG_PATH for the shared GUI/backend file.
        # Keep the loader fallback for embedded/test deployments that inject a
        # config callable without creating a file on disk.
        if os.getenv("STT_CONFIG_PATH"):
            try:
                snapshot = load_snapshot(config_path)
                if isinstance(snapshot[0], dict) and snapshot[0]:
                    return snapshot
            except (OSError, ValueError, TypeError):
                pass
        return load_config(), None

    managed_root = Path(
        cfg.get('local_llama', {}).get('root')
        or (_default_app_dir() / LOCAL_TRANSLATION_MODELS_DIR_NAME)
    )
    app.state.translation_service = TranslationService(
        lambda: current_config_snapshot()[0],
        managed_root,
        on_idle=clear_translators,
        config_path=config_path,
        revision_provider=lambda: load_snapshot(config_path)[1],
        server_id=server_id,
        instance_id=instance_id,
        snapshot_provider=current_config_snapshot,
    )
    app.state.ready = True
    try:
        yield
    finally:
        app.state.ready = False
        await app.state.translation_service.close()
        translators = app.state.translator_cache.values()
        for translator in translators:
            if hasattr(translator, "aclose"):
                await translator.aclose()
        await app.state.http_client.aclose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_endpoint() -> Dict[str, Any]:
    """Lightweight backend readiness probe; never includes configuration."""

    return {
        "service": "stt-tts",
        "instance_id": str(getattr(app.state, "instance_id", "")),
        "ready": bool(getattr(app.state, "ready", False)),
    }


@app.get("/readiness")
async def readiness_endpoint() -> Dict[str, Any]:
    # Keep a descriptive alias for process managers that call the endpoint
    # readiness rather than health.  The response contract is identical.
    return await health_endpoint()


async def _handle_desktop_management(websocket: WebSocket, payload: dict) -> None:
    """Serve extension settings without exposing the YAML or service secrets."""

    if payload.get("version") != 1:
        raise ValueError("Unsupported desktop management version.")
    if not _trusted_profile_import_origin(websocket):
        await _safe_send(websocket, {
            "type": "desktop_management_result", "version": 1, "ok": False,
            "error": "Settings require a trusted local client.",
        })
        await websocket.close(code=1008)
        return
    path = app.state.config_path
    service = app.state.translation_service
    action = str(payload.get("action") or "read")
    saved_profile_id = None
    try:
        if action == "read":
            result = settings_management.snapshot(path, service)
        else:
            expected = payload.get("expected_revision")
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ValueError("Refresh settings before saving.")
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                raise ValueError("Settings data must be an object.")
            if action == "save_profile":
                config, revision = load_snapshot(path)
                if revision != expected:
                    raise RevisionConflictError(expected, revision)
                candidate = settings_management.prepare_service_profile(config, data)
                saved_profile_id = candidate["id"]
                if candidate["engine"] in {"openai", "openai_compatible"}:
                    api_type, resolved_url = await service_probe.detect_api_type(
                        candidate, app.state.http_client,
                    )
                    data["profile"] = {**data["profile"], "id": candidate["id"], "api_type": api_type}
                    if resolved_url:
                        data["profile"].update(base_url=resolved_url, auto_complete_endpoint=False)
            if action == "add_local":
                model = await asyncio.to_thread(
                    resolve_hf_model,
                    str(data.get("repo") or "").strip(),
                    str(data.get("filename") or "").strip(),
                )
                settings_management.register_model(path, model, expected)
            elif action == "install_local":
                config, revision = load_snapshot(path)
                if revision != expected:
                    raise RevisionConflictError(expected, revision)
                model = get_model(str(data.get("id") or ""), config)
                local = config.get("local_llama") or {}
                root = Path(str(local.get("root") or service.root)).expanduser()
                kind = "windows-cpu" if int(local.get("gpu_layers", -1)) == 0 else "windows-cuda"
                loop = asyncio.get_running_loop()
                cancel = threading.Event()

                def progress(message, done, total):
                    loop.call_soon_threadsafe(
                        asyncio.create_task,
                        _safe_send(websocket, {
                            "type": "desktop_management_progress", "version": 1,
                            "message": str(message), "done": int(done), "total": int(total),
                        }),
                    )

                async with app.state.management_install_lock:
                    try:
                        await asyncio.to_thread(prepare_install, root, model, kind, cancel, progress)
                    except asyncio.CancelledError:
                        cancel.set()
                        raise
                settings_management.mutate(path, service, "publish_local", {
                    "id": model.id, "published": True,
                }, expected)
            elif action == "install_stt":
                config, revision = load_snapshot(path)
                if revision != expected:
                    raise RevisionConflictError(expected, revision)
                model_id = str(data.get("id") or "").strip()
                if model_id not in model_manager.WHISPER_MODEL_URLS:
                    raise ValueError("Only built-in Whisper models can be downloaded here.")
                stt = dict(config.get("stt") or {})
                stt["model"] = model_id
                stt.pop("model_path", None)
                if not stt.get("model_cache_dir"):
                    raise ValueError("Set a speech model folder in the desktop settings first.")
                loop = asyncio.get_running_loop()

                def progress(done, total):
                    loop.call_soon_threadsafe(
                        asyncio.create_task,
                        _safe_send(websocket, {
                            "type": "desktop_management_progress", "version": 1,
                            "message": f"Downloading Whisper {model_id}",
                            "done": int(done), "total": int(total or 0),
                        }),
                    )

                await model_manager.ensure_model_available(
                    stt, progress_cb=progress,
                    download_lock=app.state.model_download_lock,
                )
            elif action == "remove_stt":
                config, revision = load_snapshot(path)
                if revision != expected:
                    raise RevisionConflictError(expected, revision)
                model_id = str(data.get("id") or "").strip()
                if model_id not in model_manager.WHISPER_MODEL_URLS:
                    raise ValueError("Only built-in Whisper downloads can be removed here.")
                if str((config.get("stt") or {}).get("model") or "") == model_id:
                    raise ValueError("Choose another desktop STT default before removing this download.")
                if service.leases:
                    raise ValueError("Stop active captures before removing a speech model.")
                stt = {**(config.get("stt") or {}), "model": model_id, "model_path": None}
                target = model_manager.resolve_stt_model_target(stt)
                if target is None or target.is_symlink():
                    raise ValueError("This speech model is not a managed download.")
                async with app.state.model_download_lock:
                    target.unlink(missing_ok=True)
            else:
                settings_management.mutate(path, service, action, data, expected)
            result = settings_management.snapshot(path, service)
            if saved_profile_id:
                result["saved_profile_id"] = saved_profile_id
        await _safe_send(websocket, result)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        try:
            revision = load_snapshot(path)[1]
        except (OSError, ValueError):
            revision = None
        await _safe_send(websocket, {
            "type": "desktop_management_result", "version": 1, "ok": False,
            "error": str(exc), "revision": revision,
        })
    await websocket.close()


@app.websocket("/asr")
async def websocket_endpoint(websocket: WebSocket) -> None:
    if not _is_local_client(websocket):
        await websocket.accept()
        await _safe_send(
            websocket,
            {"type": "error", "message": "Local connections only"},
        )
        await websocket.close(code=1008)
        return
    await websocket.accept()
    session_id = uuid.uuid4().hex
    websocket.state.translation_lease = session_id
    stt_cfg = dict(app.state.default_stt_cfg or {})
    update = None
    pending_message = None
    initial = None
    capture_version = None
    capture_config = None
    capture_revision = None
    try:
        try:
            initial = await asyncio.wait_for(websocket.receive(), timeout=INITIAL_CONFIG_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            pass
        payload = {}
        if initial:
            if initial.get('type') == 'websocket.disconnect':
                return
            if initial.get('text'):
                if len(initial['text'].encode('utf-8')) > MAX_PROFILE_IMPORT_BYTES:
                    raise ValueError('Management request is too large.')
                payload = json.loads(initial['text'])
                if not isinstance(payload, dict):
                    raise ValueError('Expected a config object')
            if payload.get('type') == 'desktop_catalog':
                if payload.get('version') != 2:
                    raise ValueError('Unsupported desktop catalog version.')
                await _safe_send(
                    websocket,
                    app.state.translation_service.desktop_catalog(
                        server_id=app.state.server_id,
                        instance_id=app.state.instance_id,
                    ),
                )
                await websocket.close()
                return
            if payload.get('type') == 'desktop_management':
                await _handle_desktop_management(websocket, payload)
                return
            if payload.get('type') == 'desktop_import_profiles':
                if payload.get('version') != 2:
                    raise ValueError('Unsupported desktop profile import version.')
                if not _trusted_profile_import_origin(websocket):
                    await _safe_send(
                        websocket,
                        {
                            'type': 'desktop_import_result',
                            'version': 2,
                            'ok': False,
                            'mapping': {},
                            'revision': load_snapshot(app.state.config_path)[1],
                            'errors': [{'error': 'Profile import requires a trusted local client.'}],
                        },
                    )
                    await websocket.close(code=1008)
                    return
                profiles = payload.get('profiles')
                if not isinstance(profiles, list) or len(profiles) > MAX_PROFILE_IMPORT_ITEMS:
                    raise ValueError('Profiles must be an array with at most 256 entries.')
                expected_revision = payload.get('expected_revision')
                if expected_revision is not None:
                    try:
                        expected_revision = int(expected_revision)
                    except (TypeError, ValueError):
                        raise ValueError('expected_revision must be an integer.')
                try:
                    result = import_profiles(
                        app.state.config_path,
                        profiles,
                        expected_revision=expected_revision,
                    )
                    errors = result.get('errors') or []
                    response = {
                        'type': 'desktop_import_result',
                        'version': 2,
                        'ok': not bool(errors),
                        'mapping': result.get('mapping') or {},
                        'revision': int(result.get('revision', 0)),
                    }
                    if errors:
                        response['errors'] = errors
                    await _safe_send(websocket, response)
                except RevisionConflictError as exc:
                    await _safe_send(
                        websocket,
                        {
                            'type': 'desktop_import_result',
                            'version': 2,
                            'ok': False,
                            'mapping': {},
                            'revision': exc.actual,
                            'errors': [{'error': str(exc)}],
                        },
                    )
                await websocket.close()
                return
            if payload.get('type') == 'translation_catalog':
                await _safe_send(websocket, app.state.translation_service.catalog())
                await websocket.close()
                return
            if payload.get('type') in {'config', 'test'}:
                capture_version = payload.get('version')
                websocket.state.capture_version = capture_version
                if capture_version == 2:
                    try:
                        snapshot = app.state.translation_service.snapshot_provider()
                        if isinstance(snapshot, tuple) and len(snapshot) == 2 and isinstance(snapshot[0], dict):
                            capture_config, capture_revision = snapshot
                        elif isinstance(snapshot, dict):
                            capture_config = snapshot
                    except (AttributeError, OSError, TypeError, ValueError):
                        capture_config = None
                    if isinstance(capture_config, dict):
                        fresh_stt = capture_config.get('stt')
                        stt_cfg = dict(fresh_stt) if isinstance(fresh_stt, dict) else {}
                    websocket.state.capture_config = capture_config
                    websocket.state.capture_revision = capture_revision
                update = payload.get('translation')
                stt_cfg = _merge_config(stt_cfg, _normalize_stt_update(payload.get('stt')))
                if payload.get('version') == 2:
                    _validate_v2_stt_selection(stt_cfg, capture_config, capture_revision)
            else:
                pending_message = initial
        if capture_version == 2:
            await _safe_send(
                websocket,
                {
                    'type': 'status',
                    'phase': 'loading',
                    'message': 'Preparing selected translation service',
                    'snapshot': _capture_snapshot(stt_cfg, update or app.state.translation_cfg or {}),
                },
            )
        else:
            await _safe_send(websocket, {'type': 'status', 'message': 'Preparing selected translation service'})
        translation_cfg = await _acquire_translation_for_socket(
            websocket,
            session_id,
            update,
            stt_cfg,
            config_override=capture_config,
        )
        if capture_version == 2:
            await _send_capture_status(
                websocket,
                'loading',
                'Preparing selected translation service',
                stt_cfg,
                translation_cfg,
            )
        if payload.get('type') == 'test' and payload.get('target') == 'translation':
            translator = await _resolve_translator(app.state.translator_cache, translation_cfg,
                                                  app.state.http_client, app.state.translator_build_lock)
            result = await app.state.translation_scheduler.translate(translator, 'Hello world', 'en')
            await _safe_send(websocket, {'type': 'test_result', 'ok': True, 'message': result})
            await websocket.close()
            return
        await _run_selected_session(websocket, stt_cfg, translation_cfg, pending_message)
    except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
        await _safe_send(websocket, {'type': 'error', 'message': str(exc)})
        await websocket.close(code=1008)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception('Capture startup failed')
        await _safe_send(websocket, {'type': 'error', 'message': 'Capture startup failed; see backend log'})
        await websocket.close(code=1011)
    finally:
        await app.state.translation_service.release(session_id)


async def _selected_translation_update(websocket, current, update, stt):
    merged = _merge_config(current, update)
    # Every route change, including legacy config/test messages, goes through
    # the same lease. A session may change language, but not its active provider.
    merged = await app.state.translation_service.acquire(websocket.state.translation_lease, merged, stt)
    return merged


async def _receive_capture_message(websocket):
    buffered = getattr(websocket.state, 'startup_messages', [])
    if buffered:
        return buffered.pop(0)
    return await websocket.receive()


async def _construct_stt_engine_for_socket(websocket, engine_kwargs: Dict[str, Any]):
    """Build an engine while still servicing disconnect and bounded input."""

    engine_task = asyncio.create_task(_construct_stt_engine(engine_kwargs))
    receive_task = None
    startup_messages = getattr(websocket.state, "startup_messages", None)
    if not isinstance(startup_messages, list):
        startup_messages = []
        websocket.state.startup_messages = startup_messages
    buffered_bytes = sum(
        len(message.get("bytes") or b"") + len(message.get("text") or "")
        for message in startup_messages
        if isinstance(message, dict)
    )

    def buffer(message: Dict[str, Any]) -> None:
        nonlocal buffered_bytes
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        buffered_bytes += len(message.get("bytes") or b"") + len(message.get("text") or "")
        if buffered_bytes > 8 * 1024 * 1024 or len(startup_messages) >= 4096:
            raise ValueError("Model startup audio buffer is full. Stop capture and retry after loading a smaller model.")
        startup_messages.append(message)

    try:
        while not engine_task.done():
            # Any messages already buffered by translation startup must remain
            # in ``startup_messages``.  Read only new socket traffic here;
            # calling _receive_capture_message would pop and append the same
            # buffered item repeatedly while the engine loads.
            receive_task = asyncio.create_task(websocket.receive())
            done, _ = await asyncio.wait(
                (engine_task, receive_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if engine_task in done:
                if receive_task in done:
                    try:
                        buffer(receive_task.result())
                    except WebSocketDisconnect:
                        _dispose_stt_task_result(engine_task)
                        raise
                else:
                    receive_task.cancel()
                    await asyncio.gather(receive_task, return_exceptions=True)
                return await engine_task
            try:
                buffer(receive_task.result())
            except WebSocketDisconnect:
                if not engine_task.done():
                    _watch_stt_task_for_cleanup(engine_task)
                raise
            finally:
                receive_task = None
        return await engine_task
    except BaseException:
        if receive_task is not None and not receive_task.done():
            receive_task.cancel()
            await asyncio.gather(receive_task, return_exceptions=True)
        if not engine_task.done():
            _watch_stt_task_for_cleanup(engine_task)
        raise


async def _acquire_translation_for_socket(websocket, session_id, update, stt, config_override=None):
    """Observe disconnect while the model loads; keep early audio in a bounded queue."""
    websocket.state.startup_messages = []
    buffered_bytes = 0
    if config_override is None:
        acquire = asyncio.create_task(app.state.translation_service.acquire(session_id, update, stt))
    else:
        acquire = asyncio.create_task(
            app.state.translation_service.acquire(
                session_id,
                update,
                stt,
                config_override=config_override,
            )
        )
    incoming = None

    def buffer(message):
        nonlocal buffered_bytes
        if message.get('type') == 'websocket.disconnect':
            raise WebSocketDisconnect(message.get('code', 1000))
        buffered_bytes += len(message.get('bytes') or b'') + len(message.get('text') or '')
        if buffered_bytes > 8 * 1024 * 1024 or len(websocket.state.startup_messages) >= 4096:
            raise ValueError('Model startup audio buffer is full. Stop capture and retry after loading a smaller model.')
        websocket.state.startup_messages.append(message)

    try:
        while not acquire.done():
            incoming = asyncio.create_task(websocket.receive())
            done, _ = await asyncio.wait((acquire, incoming), return_when=asyncio.FIRST_COMPLETED)
            if incoming in done:
                buffer(incoming.result())
                incoming = None
            if acquire.done():
                break
        if incoming is not None and incoming.done():
            buffer(incoming.result())
            incoming = None
        return await acquire
    except BaseException:
        acquire.cancel()
        await asyncio.gather(acquire, return_exceptions=True)
        raise
    finally:
        if incoming is not None:
            incoming.cancel()
            await asyncio.gather(incoming, return_exceptions=True)


def _stt_stall_reason(stt_session: SttSession, now: float, timeout: float) -> Optional[str]:
    """Return a reset reason only when the processor, not the media, stalled."""

    first_audio_ts = stt_session.first_audio_ts
    if first_audio_ts is None or now - stt_session.last_audio_ts > timeout:
        return None
    if stt_session.last_response_status == "no_audio_detected":
        return None
    if stt_session.response_seen:
        if now - stt_session.last_response_ts > timeout:
            return "stall"
        return None
    if now - first_audio_ts > timeout * 2:
        return "no processor response"
    return None


async def _run_selected_session(websocket, stt_cfg, translation_cfg, pending_message):
    try:
        stt_cfg = await _ensure_model_available(
            stt_cfg, websocket, app.state.model_download_lock
        )
    except Exception:
        await websocket.close(code=1011)
        return
    gpu_log_interval_sec = _normalize_timeout(
        stt_cfg.get("gpu_log_interval_sec"), 0.0
    )
    engine_kwargs = _build_engine_kwargs(stt_cfg)
    engine_kwargs["pcm_input"] = bool(stt_cfg.get("pcm_input", False))
    stt_engine = await _construct_stt_engine_for_socket(websocket, engine_kwargs)
    _apply_stt_engine_evidence_config(stt_engine, stt_cfg)
    if gpu_log_interval_sec > 0:
        _log_cuda_memory("engine init")
    translator = await _resolve_translator(
        app.state.translator_cache,
        translation_cfg,
        app.state.http_client,
        app.state.translator_build_lock,
    )
    audio_processor = AudioProcessor(
        transcription_engine=stt_engine,
    )
    stt_session = SttSession(stt_cfg, stt_engine, audio_processor)
    session = TranslationSession(
        translation_cfg,
        stt_session.default_language,
        translator,
        app.state.translation_scheduler,
        app.state.subtitle_cfg,
    )
    reset_requests: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    results_generator = await audio_processor.create_tasks()
    results_task = asyncio.create_task(
        _handle_results(
            websocket,
            results_generator,
            session,
            stt_session,
            reset_requests=reset_requests,
        )
    )
    stall_timeout = _normalize_timeout(
        stt_cfg.get("stall_timeout_sec"), DEFAULT_STALL_TIMEOUT_SEC
    )
    stall_check_interval = _normalize_timeout(
        stt_cfg.get("stall_check_interval_sec"),
        DEFAULT_STALL_CHECK_INTERVAL_SEC,
    )
    recycle_sec = _normalize_timeout(stt_cfg.get("engine_recycle_sec"), 0.0)
    cleanup_interval_sec = _normalize_timeout(
        stt_cfg.get("memory_cleanup_interval_sec"), 0.0
    )

    async def _reset_stt_engine(reason: str, rebuild_engine: bool = True) -> None:
        nonlocal audio_processor, stt_engine, results_generator, results_task
        async with stt_session.reset_lock:
            try:
                old_engine = stt_engine
                await _cancel_session_translation_tasks(session)
                if session.debounce_task and not session.debounce_task.done():
                    session.debounce_task.cancel()
                    try:
                        await session.debounce_task
                    except asyncio.CancelledError:
                        pass
                session.utterance_text = ""
                session.utterance_language = None
                session.pending_seq = None
                session.chunk_revisions.clear()
                session.latest_request_versions.clear()
                session.pending_request = None
                session.pending_final_requests.clear()
                session.request_version = 0
                session.asr_segment_identity = None
                session.asr_snapshot_text = ""
                session.asr_committed_text = ""
                session.utterance_prefix_text = ""
                session.segment_chunk_text = ""
                session.stable_source_history.clear()
                session.segment_start_ts = None
                stt_session.evidence_candidate_text = ""
                stt_session.evidence_stable_updates = 0
                stt_session.evidence_rejections = 0
                if not results_task.done():
                    results_task.cancel()
                    try:
                        await results_task
                    except asyncio.CancelledError:
                        pass
                await _close_results_generator(results_generator)
                await audio_processor.cleanup()
                _release_memory()
                if rebuild_engine:
                    engine_kwargs = _build_engine_kwargs(stt_session.cfg)
                    engine_kwargs["pcm_input"] = bool(
                        stt_session.cfg.get("pcm_input", False)
                    )
                    stt_engine = await _construct_stt_engine(engine_kwargs)
                    _apply_stt_engine_evidence_config(
                        stt_engine, stt_session.cfg
                    )
                    stt_session.engine_builds += 1
                    if gpu_log_interval_sec > 0:
                        _log_cuda_memory(f"engine rebuild ({reason})")
                audio_processor = AudioProcessor(transcription_engine=stt_engine)
                stt_session.engine = stt_engine
                stt_session.audio_processor = audio_processor
                stt_session.first_audio_ts = None
                stt_session.last_audio_ts = time.monotonic()
                stt_session.last_result_ts = time.monotonic()
                stt_session.result_seen = False
                stt_session.last_response_ts = time.monotonic()
                stt_session.response_seen = False
                stt_session.last_response_status = ""
                stt_session.engine_start_ts = time.monotonic()
                stt_session.last_cleanup_ts = stt_session.engine_start_ts
                old_engine = None
                _release_memory()
                results_generator = await audio_processor.create_tasks()
                results_task = asyncio.create_task(
                    _handle_results(
                        websocket,
                        results_generator,
                        session,
                        stt_session,
                        reset_requests=reset_requests,
                    )
                )
                # Non-PCM capture is a continuous WebM stream.  Rebuilding its
                # FFmpeg consumer mid-stream requires the extension to create
                # a fresh MediaRecorder so the next stream includes headers.
                await _safe_send(
                    websocket,
                    {"type": "status", "message": f"stt reset: {reason}"},
                )
            except Exception as exc:
                await _safe_send(
                    websocket,
                    {"type": "error", "message": f"STT reset failed: {type(exc).__name__}"},
                )

    async def _stt_reset_supervisor() -> None:
        while True:
            try:
                reason = await reset_requests.get()
            except asyncio.CancelledError:
                return
            try:
                await _reset_stt_engine(reason, rebuild_engine=False)
            finally:
                reset_requests.task_done()

    async def _stt_watchdog() -> None:
        if (
            stall_check_interval <= 0 or stall_timeout <= 0
        ) and cleanup_interval_sec <= 0 and recycle_sec <= 0:
            return
        watchdog_interval = stall_check_interval if stall_check_interval > 0 else 1.0
        while True:
            try:
                await asyncio.sleep(watchdog_interval)
            except asyncio.CancelledError:
                return
            if stt_session.reset_lock.locked():
                continue
            now = time.monotonic()
            if gpu_log_interval_sec > 0:
                if now - stt_session.last_gpu_log_ts >= gpu_log_interval_sec:
                    stt_session.last_gpu_log_ts = now
                    _log_cuda_memory(f"watchdog engines={stt_session.engine_builds}")
            if cleanup_interval_sec > 0:
                if now - stt_session.last_cleanup_ts >= cleanup_interval_sec:
                    _release_memory()
                    stt_session.last_cleanup_ts = now
            if recycle_sec > 0:
                if now - stt_session.engine_start_ts >= recycle_sec:
                    await _reset_stt_engine("recycle", rebuild_engine=True)
            if stall_check_interval > 0 and stall_timeout > 0:
                reason = _stt_stall_reason(stt_session, now, stall_timeout)
                if reason:
                    await _reset_stt_engine(reason, rebuild_engine=False)

    reset_supervisor_task = asyncio.create_task(_stt_reset_supervisor())
    watchdog_task = asyncio.create_task(_stt_watchdog())

    try:
        if getattr(websocket.state, "capture_version", None) == 2:
            await _send_capture_status(
                websocket,
                "capturing",
                "Capture ready",
                stt_cfg,
                translation_cfg,
            )
        else:
            await _safe_send(websocket, {"type": "status", "message": "connected"})
        while True:
            try:
                if pending_message is not None:
                    message = pending_message
                    pending_message = None
                else:
                    message = await _receive_capture_message(websocket)
            except WebSocketDisconnect:
                break
            except RuntimeError:
                break
            if message.get("type") == "websocket.disconnect":
                break
            if "bytes" in message and message["bytes"] is not None:
                now = time.monotonic()
                stt_session.last_audio_ts = now
                if stt_session.first_audio_ts is None:
                    stt_session.first_audio_ts = now
                try:
                    async with stt_session.reset_lock:
                        await audio_processor.process_audio(message["bytes"])
                except Exception as exc:
                    await _safe_send(
                        websocket,
                        {
                            "type": "error",
                            "message": f"STT audio error: {type(exc).__name__}",
                        },
                    )
                    await _reset_stt_engine("audio error")
                continue
            if "text" in message and message["text"] is not None:
                try:
                    payload = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue

                msg_type = payload.get("type")
                if msg_type == "config":
                    translation_update = payload.get("translation")
                    if translation_update:
                        merged = await _selected_translation_update(websocket, session.cfg, translation_update, stt_cfg)
                        await _cancel_session_translation_tasks(session)
                        session.translator = await _resolve_translator(
                            app.state.translator_cache,
                            merged,
                            app.state.http_client,
                            app.state.translator_build_lock,
                        )
                        session.cfg = merged
                        session.translate_partials = bool(
                            session.cfg.get("partial", False)
                        )
                        session.debounce_ms = _normalize_delay_ms(
                            session.cfg.get("debounce_ms"),
                            DEFAULT_TRANSLATION_DEBOUNCE_MS,
                        )
                        session.segment_max_chars = _normalize_non_negative_int(
                            session.cfg.get("segment_max_chars"),
                            DEFAULT_SEGMENT_MAX_CHARS,
                        )
                        session.segment_max_ms = _normalize_delay_ms(
                            session.cfg.get("segment_max_ms"),
                            DEFAULT_SEGMENT_MAX_MS,
                        )
                        session.segment_start_ts = None
                        await _safe_send(
                            websocket,
                            {
                                "type": "status",
                                "message": "translation updated",
                            },
                        )
                    stt_update = payload.get("stt")
                    if stt_update:
                        await _apply_stt_update(
                            stt_session,
                            session,
                            stt_update,
                            websocket,
                        )
                elif msg_type == "test":
                    if payload.get("target") == "translation":
                        test_cfg = await _selected_translation_update(websocket, session.cfg, payload.get("translation"), stt_cfg)
                        target_desc = describe_translation_target(test_cfg)
                        try:
                            test_translator = await _resolve_translator(
                                app.state.translator_cache,
                                test_cfg,
                                app.state.http_client,
                                app.state.translator_build_lock,
                            )
                            action = str(payload.get("action") or "translate")
                            if action in {"models", "health"}:
                                health = await test_translator.health_check()
                                if not health.get("ok"):
                                    raise RuntimeError(str(health.get("error") or "Health check failed"))
                                models = health.get("models") or []
                                result = f"Models reachable: {', '.join(models[:10]) or '(none listed)'}"
                            else:
                                result = await app.state.translation_scheduler.translate(
                                    test_translator, "Hello world", "en"
                                )
                            await _safe_send(
                                websocket,
                                {
                                    "type": "test_result",
                                    "target": "translation",
                                    "ok": True,
                                    "message": f"Translation ok via {target_desc}: {result[:60]}",
                                },
                            )
                        except Exception as exc:
                            await _safe_send(
                                websocket,
                                {
                                    "type": "test_result",
                                    "target": "translation",
                                    "ok": False,
                                    "message": f"{target_desc} :: {exc}",
                                },
                            )
                elif msg_type == "ping":
                    continue
    except WebSocketDisconnect:
        pass
    finally:
        if not reset_supervisor_task.done():
            reset_supervisor_task.cancel()
            try:
                await reset_supervisor_task
            except asyncio.CancelledError:
                pass
        if not watchdog_task.done():
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
        if not results_task.done():
            results_task.cancel()
            try:
                await results_task
            except asyncio.CancelledError:
                pass
        await _close_results_generator(results_generator)
        await _cancel_session_translation_tasks(session)
        if session.debounce_task and not session.debounce_task.done():
            session.debounce_task.cancel()
            try:
                await session.debounce_task
            except asyncio.CancelledError:
                pass
        await audio_processor.cleanup()
        _release_memory()
        # No global WS lock; each connection is handled independently.


def main() -> None:
    import argparse
    import os
    import uvicorn

    parser = argparse.ArgumentParser(description="WhisperLiveKit backend server")
    parser.add_argument("--config", type=str, default=None, help="Path to config YAML")
    parser.add_argument("--host", type=str, default=None, help="Override server host")
    parser.add_argument("--port", type=int, default=None, help="Override server port")
    args = parser.parse_args()

    if args.config:
        os.environ["STT_CONFIG_PATH"] = args.config

    cfg = load_config(args.config)
    server_cfg = _get_server_config(cfg)
    uvicorn.run(
        app,
        host=args.host or server_cfg["host"],
        port=args.port or server_cfg["port"],
        log_level="info",
    )


if __name__ == "__main__":
    main()

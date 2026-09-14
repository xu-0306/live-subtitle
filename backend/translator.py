from __future__ import annotations

import asyncio
import json
import inspect
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import httpx

try:
    from .cache import LRUCache
except ImportError:  # Fallback when running as a script.
    from cache import LRUCache

DEFAULT_CACHE_SIZE = 512
DEFAULT_OLLAMA_KEEP_ALIVE = "5m"
DEFAULT_OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"
DEFAULT_OPENAI_TIMEOUT_SEC = 15
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_VLLM_ENDPOINT = "http://127.0.0.1:8000/v1/chat/completions"
SYSTEM_TRANSLATION_PROMPT = (
    "You are a translation engine. Follow the user's instructions exactly."
)

LANGUAGE_LABELS = {
    "auto": "auto-detected language",
    "en": "English (en)",
    "en-us": "English (en-US)",
    "en-gb": "English (en-GB)",
    "ja": "Japanese (ja)",
    "jp": "Japanese (ja)",
    "jpn": "Japanese (ja)",
    "zh": "Chinese (zh)",
    "zh-cn": "Simplified Chinese (zh-CN)",
    "zh-hans": "Simplified Chinese (zh-Hans)",
    "zh-tw": "Traditional Chinese (zh-TW)",
    "zh-hant": "Traditional Chinese (zh-Hant)",
    "zh-hk": "Traditional Chinese (zh-HK)",
    "ko": "Korean (ko)",
    "fr": "French (fr)",
    "de": "German (de)",
    "es": "Spanish (es)",
    "pt": "Portuguese (pt)",
    "it": "Italian (it)",
    "ru": "Russian (ru)",
    "id": "Indonesian (id)",
    "vi": "Vietnamese (vi)",
    "th": "Thai (th)",
}


def _describe_language(lang: Optional[str], fallback: str) -> str:
    if not lang:
        return fallback
    normalized = lang.replace("_", "-").strip().lower()
    return LANGUAGE_LABELS.get(normalized, lang.strip())


def _build_translation_prompt(
    text: str, source_lang: Optional[str], target_lang: str
) -> str:
    source_desc = _describe_language(source_lang, "source language")
    target_desc = _describe_language(target_lang, "target language")
    return (
        f"Translate the following {source_desc} text into {target_desc}. "
        f"Output only the translation in {target_desc}. "
        "Do not include the source text, explanations, or extra commentary. "
        "Do not mix other languages. "
        "Preserve meaning, punctuation, numbers, and proper nouns. "
        f"If the input is already in {target_desc}, return it unchanged.\n\n"
        f"{text}"
    )


API_TYPE_CHAT_COMPLETIONS = "chat_completions"
API_TYPE_RESPONSES = "responses"
API_TYPE_MESSAGES = "messages"
API_TYPE_ALIASES = {
    "chat": API_TYPE_CHAT_COMPLETIONS,
    "chat_completion": API_TYPE_CHAT_COMPLETIONS,
    "chat_completions": API_TYPE_CHAT_COMPLETIONS,
    "openai": API_TYPE_CHAT_COMPLETIONS,
    "responses": API_TYPE_RESPONSES,
    "response": API_TYPE_RESPONSES,
    "messages": API_TYPE_MESSAGES,
    "message": API_TYPE_MESSAGES,
    "anthropic": API_TYPE_MESSAGES,
}
API_ENDPOINT_SEGMENTS = {
    API_TYPE_CHAT_COMPLETIONS: ("v1", "chat", "completions"),
    API_TYPE_RESPONSES: ("v1", "responses"),
    API_TYPE_MESSAGES: ("v1", "messages"),
}


def normalize_api_type(value: Optional[object]) -> str:
    normalized = str(value or API_TYPE_CHAT_COMPLETIONS).strip().lower().replace("-", "_")
    return API_TYPE_ALIASES.get(normalized, API_TYPE_CHAT_COMPLETIONS)


def _detect_api_type(path: str) -> Optional[str]:
    normalized_path = path.rstrip("/").lower()
    if normalized_path.endswith("/chat/completions"):
        return API_TYPE_CHAT_COMPLETIONS
    if normalized_path.endswith("/responses"):
        return API_TYPE_RESPONSES
    if normalized_path.endswith("/messages"):
        return API_TYPE_MESSAGES
    return None


def _append_endpoint_path(path: str, api_type: str) -> str:
    desired = API_ENDPOINT_SEGMENTS[api_type]
    existing = [part for part in path.rstrip("/").split("/") if part]
    overlap = 0
    max_overlap = min(len(existing), len(desired))
    for length in range(max_overlap, 0, -1):
        if tuple(part.lower() for part in existing[-length:]) == desired[:length]:
            overlap = length
            break
    completed = existing + list(desired[overlap:])
    prefix = "/" if path.startswith("/") else ""
    return prefix + "/".join(completed)


@dataclass(frozen=True)
class ApiUrlPolicy:
    """Resolve user-entered provider URLs without ever adding a host or scheme."""

    api_type: str = API_TYPE_CHAT_COMPLETIONS
    auto_complete_endpoint: bool = True

    def resolve(self, raw_url: Optional[str], *, default_url: str) -> tuple[str, str]:
        stripped = (raw_url or "").strip() or default_url
        parts = urllib_parse.urlsplit(stripped)
        detected = _detect_api_type(parts.path or "")
        if detected:
            # A known complete endpoint is authoritative, which also keeps the
            # request body compatible with the URL selected by the user.
            return stripped, detected

        api_type = normalize_api_type(self.api_type)
        if not self.auto_complete_endpoint:
            return stripped, api_type

        completed_path = _append_endpoint_path(parts.path or "", api_type)
        return urllib_parse.urlunsplit(parts._replace(path=completed_path)), api_type


def resolve_models_url(api_url: str, explicit_models_url: Optional[str] = None) -> str:
    explicit = (explicit_models_url or "").strip()
    if explicit:
        return explicit
    parts = urllib_parse.urlsplit(api_url)
    path = (parts.path or "").rstrip("/")
    lower_path = path.lower()
    for suffix in ("/chat/completions", "/responses", "/messages"):
        if lower_path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if not path.lower().endswith("/models"):
        path = f"{path}/models"
    return urllib_parse.urlunsplit(parts._replace(path=path, query="", fragment=""))


def _normalize_openai_api_url(raw_url: Optional[str]) -> tuple[str, str]:
    """Legacy URL resolver retained for callers that expect use-as-entered URLs."""
    stripped = (raw_url or "").strip()
    if not stripped:
        return DEFAULT_OPENAI_ENDPOINT, "chat_completions"

    detected = _detect_api_type(urllib_parse.urlsplit(stripped).path or "")
    return stripped, detected or API_TYPE_CHAT_COMPLETIONS


def _coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_coerce_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("output_text", "text", "content", "value"):
            if key in value:
                text = _coerce_text(value.get(key))
                if text:
                    return text
    return ""


def _extract_chat_completion_text(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return ""
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        text = _coerce_text(message.get("content"))
        if text:
            return text
    return ""


def _extract_responses_text(payload: Dict[str, Any]) -> str:
    direct = _coerce_text(payload.get("output_text"))
    if direct:
        return direct
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        text = _coerce_text(item.get("content"))
        if text:
            return text
    return ""


def _extract_messages_text(payload: Dict[str, Any]) -> str:
    return _coerce_text(payload.get("content"))


def _extract_provider_error(payload: Dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = _coerce_text(error.get("message"))
        if message:
            return message
        return json.dumps(error, ensure_ascii=False)
    text = _coerce_text(error)
    if text:
        return text
    return json.dumps(payload, ensure_ascii=False)


def describe_translation_target(cfg: Dict[str, object]) -> str:
    engine = str(cfg.get("engine", "nllb")).lower()
    if engine in {"openai", "vllm", "openai_compatible"}:
        provider_cfg = resolve_provider_config(cfg)
        model = str(provider_cfg.get("model", ""))
        raw_url = str(provider_cfg.get("base_url") or "").strip()
        api_url, api_mode = ApiUrlPolicy(
            api_type=normalize_api_type(provider_cfg.get("api_type")),
            auto_complete_endpoint=bool(provider_cfg.get("auto_complete_endpoint", True)),
        ).resolve(
            raw_url or None,
            default_url=(
                DEFAULT_VLLM_ENDPOINT if engine == "vllm" else DEFAULT_OPENAI_ENDPOINT
            ),
        )
        profile_id = str(provider_cfg.get("profile_id") or engine)
        return (
            f"engine={engine} profile={profile_id} model={model} "
            f"mode={api_mode} url={api_url}"
        )
    if engine == "ollama":
        ollama_cfg = cfg.get("ollama", {}) if isinstance(cfg.get("ollama"), dict) else {}
        model = str(ollama_cfg.get("model", ""))
        host = str(ollama_cfg.get("host", ""))
        return f"engine=ollama model={model} host={host}"
    if engine == "nllb":
        nllb_cfg = cfg.get("nllb", {}) if isinstance(cfg.get("nllb"), dict) else {}
        model = str(nllb_cfg.get("model", ""))
        return f"engine=nllb model={model}"
    return f"engine={engine}"


class BaseTranslator:
    profile_id = "default"
    max_concurrency: Optional[int] = None
    cancellation_safe = False

    def __init__(self, target_language: str, cache_size: int = DEFAULT_CACHE_SIZE) -> None:
        self.target_language = target_language
        self._cache: LRUCache[Tuple[Optional[str], str, str], str] = LRUCache(
            cache_size
        )

    def translate(
        self, text: str, source_lang: Optional[str], target_lang: Optional[str] = None
    ) -> str:
        if not text:
            return ""
        target = target_lang or self.target_language
        key = (source_lang, target, text)
        cached, hit = self._cache.get(key)
        if hit:
            return cached or ""
        result = self._translate_impl(text, source_lang, target)
        self._cache.set(key, result)
        return result

    async def atranslate(
        self, text: str, source_lang: Optional[str], target_lang: Optional[str] = None
    ) -> str:
        """Async provider port; local synchronous engines run off the event loop."""
        return await asyncio.to_thread(self.translate, text, source_lang, target_lang)

    async def list_models(self) -> list[str]:
        return []

    async def health_check(self) -> Dict[str, Any]:
        return {"ok": True, "models": await self.list_models()}

    async def aclose(self) -> None:
        return None

    def _translate_impl(
        self, text: str, source_lang: Optional[str], target_lang: str
    ) -> str:
        raise NotImplementedError


class NoopTranslator(BaseTranslator):
    def _translate_impl(
        self, text: str, source_lang: Optional[str], target_lang: str
    ) -> str:
        return ""


class NLLBTranslator(BaseTranslator):
    def __init__(
        self,
        model: str,
        target_language: str,
        device: Optional[str] = None,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        super().__init__(target_language, cache_size=cache_size)
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        import torch

        self._device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._tokenizer = AutoTokenizer.from_pretrained(model)
        self._model = AutoModelForSeq2SeqLM.from_pretrained(model)
        self._model.to(self._device)
        self._model.eval()
        self._lock = threading.Lock()

    def _translate_impl(
        self, text: str, source_lang: Optional[str], target_lang: str
    ) -> str:
        import torch

        src_lang = _map_nllb_lang(source_lang, target=False) or "eng_Latn"
        tgt_lang = _map_nllb_lang(target_lang, target=True) or target_lang
        if (not re.fullmatch(r'[a-z]{3}_[A-Z][a-z]{3}', tgt_lang)
                or self._tokenizer.convert_tokens_to_ids(tgt_lang) == self._tokenizer.unk_token_id):
            raise ValueError('NLLB requires a supported language token (for example spa_Latn). Use an LLM service for free-text language names.')
        self._tokenizer.src_lang = src_lang
        inputs = self._tokenizer(text, return_tensors="pt")
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
        with self._lock, torch.inference_mode():
            outputs = self._model.generate(
                **inputs,
                forced_bos_token_id=self._tokenizer.convert_tokens_to_ids(tgt_lang),
                max_new_tokens=256,
            )
        return self._tokenizer.decode(outputs[0], skip_special_tokens=True).strip()


class OllamaTranslator(BaseTranslator):
    def __init__(
        self,
        model: str,
        host: str,
        target_language: str,
        keep_alive: Optional[object] = None,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        super().__init__(target_language, cache_size=cache_size)
        import ollama

        self._client = ollama.Client(host=host)
        self._model = model
        self._keep_alive = _normalize_keep_alive(keep_alive)
        self._supports_keep_alive = _has_param(self._client.chat, "keep_alive")

    def _translate_impl(
        self, text: str, source_lang: Optional[str], target_lang: str
    ) -> str:
        prompt = _build_translation_prompt(text, source_lang, target_lang)
        kwargs = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_TRANSLATION_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        if self._keep_alive is not None and self._supports_keep_alive:
            kwargs["keep_alive"] = self._keep_alive
        response = self._client.chat(**kwargs)
        return response["message"]["content"].strip()


class OpenAITranslator(BaseTranslator):
    def __init__(
        self,
        api_key: str,
        model: str,
        target_language: str,
        base_url: Optional[str] = None,
        cache_size: int = DEFAULT_CACHE_SIZE,
        api_type: Optional[str] = None,
        auto_complete_endpoint: bool = False,
        models_url: Optional[str] = None,
        timeout_sec: float = DEFAULT_OPENAI_TIMEOUT_SEC,
        http_client: Optional[httpx.AsyncClient] = None,
        profile_id: str = "openai",
        max_concurrency: Optional[int] = None,
        max_tokens: Optional[int] = None,
    ) -> None:
        super().__init__(target_language, cache_size=cache_size)
        self._api_key = api_key.strip()
        self._api_url, self._api_mode = ApiUrlPolicy(
            api_type=normalize_api_type(api_type),
            auto_complete_endpoint=auto_complete_endpoint,
        ).resolve(base_url, default_url=DEFAULT_OPENAI_ENDPOINT)
        self._models_url = resolve_models_url(self._api_url, models_url)
        self._model = model
        self._timeout_sec = max(0.0, float(timeout_sec))
        self._http_client = http_client
        self.profile_id = profile_id or "openai"
        self.max_concurrency = max_concurrency
        self._max_tokens = max_tokens
        self.cancellation_safe = True

    @property
    def api_url(self) -> str:
        return self._api_url

    @property
    def api_type(self) -> str:
        return self._api_mode

    @property
    def models_url(self) -> str:
        return self._models_url

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
            headers["x-api-key"] = self._api_key
        if self._api_mode == API_TYPE_MESSAGES:
            headers["anthropic-version"] = DEFAULT_ANTHROPIC_VERSION
        return headers

    def _request_payload(
        self, text: str, source_lang: Optional[str], target_lang: str
    ) -> Dict[str, Any]:
        prompt = _build_translation_prompt(text, source_lang, target_lang)
        if self._api_mode == API_TYPE_RESPONSES:
            return {
                "model": self._model,
                "instructions": SYSTEM_TRANSLATION_PROMPT,
                "input": prompt,
                "temperature": 0.2,
            }
        if self._api_mode == API_TYPE_MESSAGES:
            return {
                "model": self._model,
                "system": SYSTEM_TRANSLATION_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1024,
                "temperature": 0.2,
            }
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_TRANSLATION_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            **({"max_tokens": self._max_tokens} if self._max_tokens else {}),
        }

    def _extract_translation(self, response_payload: Dict[str, Any]) -> str:
        if self._api_mode == API_TYPE_RESPONSES:
            return _extract_responses_text(response_payload).strip()
        if self._api_mode == API_TYPE_MESSAGES:
            return _extract_messages_text(response_payload).strip()
        return _extract_chat_completion_text(response_payload).strip()

    def _redact_api_key(self, value: str) -> str:
        if not self._api_key:
            return value
        return value.replace(self._api_key, "[redacted]")

    def _http_error(self, response: httpx.Response) -> RuntimeError:
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            detail = response.text[:400]
        else:
            detail = _extract_provider_error(payload)[:400]
        detail = self._redact_api_key(detail)
        return RuntimeError(f"{response.status_code} {response.reason_phrase}: {detail}")

    def _translate_impl(
        self, text: str, source_lang: Optional[str], target_lang: str
    ) -> str:
        payload = self._request_payload(text, source_lang, target_lang)
        body = json.dumps(payload).encode("utf-8")
        request = urllib_request.Request(
            self._api_url,
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib_request.urlopen(
                request, timeout=self._timeout_sec or None
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib_error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                raise RuntimeError(
                    f"{exc.code} {exc.reason}: {self._redact_api_key(raw[:400])}"
                ) from exc
            detail = self._redact_api_key(_extract_provider_error(payload)[:400])
            raise RuntimeError(
                f"{exc.code} {exc.reason}: {detail}"
            ) from exc

        response_payload = json.loads(raw)
        return self._extract_translation(response_payload)

    async def _request(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        timeout: Optional[float] = self._timeout_sec or None
        if self._http_client is not None:
            return await self._http_client.request(method, url, timeout=timeout, **kwargs)
        async with httpx.AsyncClient() as client:
            return await client.request(method, url, timeout=timeout, **kwargs)

    async def atranslate(
        self, text: str, source_lang: Optional[str], target_lang: Optional[str] = None
    ) -> str:
        if not text:
            return ""
        target = target_lang or self.target_language
        key = (source_lang, target, text)
        cached, hit = self._cache.get(key)
        if hit:
            return cached or ""
        response = await self._request(
            "POST",
            self._api_url,
            headers=self._headers(),
            json=self._request_payload(text, source_lang, target),
        )
        if response.is_error:
            raise self._http_error(response)
        translated = self._extract_translation(response.json())
        self._cache.set(key, translated)
        return translated

    async def list_models(self) -> list[str]:
        response = await self._request(
            "GET",
            self._models_url,
            headers=self._headers(),
        )
        if response.is_error:
            raise self._http_error(response)
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        models: list[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("id"):
                    models.append(str(item["id"]))
                elif isinstance(item, str):
                    models.append(item)
        return models

    async def health_check(self) -> Dict[str, Any]:
        try:
            models = await self.list_models()
        except Exception as exc:
            return {"ok": False, "error": str(exc), "url": self._models_url}
        return {"ok": True, "models": models, "url": self._models_url}


def _map_nllb_lang(lang: Optional[str], target: bool = False) -> Optional[str]:
    if not lang:
        return None
    if "_" in lang:
        return lang
    normalized = lang.replace("_", "-").lower()
    if target:
        if normalized in {"zh-tw", "zh-hant", "zh-hk"}:
            return "zho_Hant"
        if normalized in {"zh-cn", "zh-hans", "zh"}:
            return "zho_Hans"
    mapping = {
        "en": "eng_Latn",
        "en-us": "eng_Latn",
        "en-gb": "eng_Latn",
        "ja": "jpn_Jpan",
        "jp": "jpn_Jpan",
        "jpn": "jpn_Jpan",
        "zh": "zho_Hans",
        "zh-cn": "zho_Hans",
        "zh-hans": "zho_Hans",
        "zh-tw": "zho_Hant",
        "zh-hant": "zho_Hant",
        "zh-hk": "zho_Hant",
    }
    return mapping.get(normalized)


def _normalize_cache_size(value: object, default: int) -> int:
    try:
        size = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(0, size)


def _normalize_keep_alive(value: object) -> Optional[object]:
    if value is None:
        return None
    if isinstance(value, bool):
        return DEFAULT_OLLAMA_KEEP_ALIVE if value else 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            return int(stripped)
        return stripped
    return None


def _has_param(func, name: str) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    return name in signature.parameters


def _normalize_optional_positive_int(value: object) -> Optional[int]:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _select_vllm_profile(vllm_cfg: Dict[str, Any]) -> Dict[str, Any]:
    profiles = vllm_cfg.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        # A flat vLLM mapping is also accepted for headless deployments.
        return dict(vllm_cfg)
    active_id = str(vllm_cfg.get("active_profile") or "").strip()
    if active_id:
        for profile in profiles:
            if isinstance(profile, dict) and str(profile.get("id") or "") == active_id:
                return dict(profile)
    for profile in profiles:
        if isinstance(profile, dict):
            return dict(profile)
    return {}


def resolve_provider_config(cfg: Dict[str, object]) -> Dict[str, Any]:
    """Return a common OpenAI-compatible connection mapping.

    vLLM runtime values are deliberately ignored here: they configure a managed
    server process and cannot be applied through the inference HTTP API.
    """
    engine = str(cfg.get("engine", "openai")).strip().lower()
    if engine == "vllm":
        vllm_cfg = cfg.get("vllm", {}) if isinstance(cfg.get("vllm"), dict) else {}
        profile = _select_vllm_profile(vllm_cfg)
        return {
            "profile_id": str(profile.get("id") or vllm_cfg.get("active_profile") or "vllm"),
            "base_url": str(profile.get("base_url") or profile.get("api_url") or "").strip(),
            "api_key": str(profile.get("api_key") or ""),
            "model": str(profile.get("served_model") or profile.get("model") or ""),
            "api_type": normalize_api_type(
                profile.get("api_type") or profile.get("api_mode")
            ),
            "auto_complete_endpoint": bool(
                profile.get(
                    "auto_complete_endpoint",
                    profile.get(
                        "auto_complete_api_url",
                        profile.get("auto_complete_url", True),
                    ),
                )
            ),
            "models_url": str(profile.get("models_url") or "").strip() or None,
            "max_concurrency": _normalize_optional_positive_int(
                profile.get("max_concurrency")
                or profile.get("client_max_concurrency")
            ),
            "mode": str(profile.get("mode") or "external"),
        }

    openai_key = "openai_compatible" if engine == "openai_compatible" and isinstance(
        cfg.get("openai_compatible"), dict
    ) else "openai"
    openai_cfg = cfg.get(openai_key, {}) if isinstance(cfg.get(openai_key), dict) else {}
    return {
        "profile_id": str(openai_cfg.get("profile_id") or engine or "openai"),
        "base_url": str(
            openai_cfg.get("base_url") or openai_cfg.get("api_url") or ""
        ).strip(),
        "api_key": str(openai_cfg.get("api_key") or ""),
        "model": str(openai_cfg.get("served_model") or openai_cfg.get("model") or "gpt-4o-mini"),
        "api_type": normalize_api_type(
            openai_cfg.get("api_type") or openai_cfg.get("api_mode")
        ),
        "auto_complete_endpoint": bool(
            openai_cfg.get(
                "auto_complete_endpoint",
                openai_cfg.get(
                    "auto_complete_api_url",
                    openai_cfg.get("auto_complete_url", True),
                ),
            )
        ),
        "models_url": str(openai_cfg.get("models_url") or "").strip() or None,
        "max_concurrency": _normalize_optional_positive_int(
            openai_cfg.get("max_concurrency")
            or openai_cfg.get("client_max_concurrency")
        ),
        "mode": "external",
    }


def build_translator(
    cfg: Dict[str, object],
    http_client: Optional[httpx.AsyncClient] = None,
) -> BaseTranslator:
    engine = str(cfg.get("engine", "nllb")).lower()
    target_language = str(cfg.get("target_language", "zh-TW"))
    cache_size = _normalize_cache_size(cfg.get("cache_size"), DEFAULT_CACHE_SIZE)

    if engine == "managed_llama":
        local = cfg.get("managed_llama") or {}
        from urllib.parse import urlsplit
        endpoint = str(local.get("base_url") or "")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.username or parsed.password:
            raise ValueError("Managed runtime must use its loopback HTTP endpoint")
        translator = OpenAITranslator(
            api_key=str(local.get("api_key") or ""), model=str(local.get("model") or ""),
            target_language=target_language, base_url=endpoint, cache_size=cache_size,
            auto_complete_endpoint=True, timeout_sec=float(cfg.get("timeout_sec", 30)),
            http_client=http_client, profile_id="managed_llama", max_concurrency=1,
            max_tokens=max(1, min(512, int(local.get("max_tokens", 192)))),
        )
        # Closing HTTP does not guarantee inference cancellation in the runtime.
        # Keep its one scheduler slot until the actual provider call completes.
        translator.cancellation_safe = False
        return translator

    if engine == "nllb":
        nllb_cfg = cfg.get("nllb", {}) if isinstance(cfg.get("nllb"), dict) else {}
        model = str(nllb_cfg.get("model", "facebook/nllb-200-distilled-600M"))
        device = nllb_cfg.get("device")
        return NLLBTranslator(
            model=model,
            device=device,
            target_language=target_language,
            cache_size=cache_size,
        )

    if engine == "ollama":
        ollama_cfg = cfg.get("ollama", {}) if isinstance(cfg.get("ollama"), dict) else {}
        model = str(ollama_cfg.get("model", "gemma3:4b"))
        host = str(ollama_cfg.get("host", "http://localhost:11434"))
        keep_alive = ollama_cfg.get("keep_alive")
        return OllamaTranslator(
            model=model,
            host=host,
            keep_alive=keep_alive,
            target_language=target_language,
            cache_size=cache_size,
        )

    if engine in {"openai", "vllm", "openai_compatible"}:
        provider_cfg = resolve_provider_config(cfg)
        api_key = str(provider_cfg.get("api_key") or "")
        model = str(provider_cfg.get("model") or "")
        if not model:
            if engine == "vllm":
                raise ValueError("The active vLLM profile must define served_model")
            model = "gpt-4o-mini"
        base_url = str(provider_cfg.get("base_url") or "").strip() or None
        timeout_sec = cfg.get("timeout_sec", DEFAULT_OPENAI_TIMEOUT_SEC)
        try:
            provider_timeout = float(timeout_sec)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            provider_timeout = DEFAULT_OPENAI_TIMEOUT_SEC
        return OpenAITranslator(
            api_key=api_key,
            model=model,
            target_language=target_language,
            base_url=base_url,
            cache_size=cache_size,
            api_type=str(provider_cfg.get("api_type") or API_TYPE_CHAT_COMPLETIONS),
            auto_complete_endpoint=bool(
                provider_cfg.get("auto_complete_endpoint", True)
            ),
            models_url=str(provider_cfg.get("models_url") or "") or None,
            timeout_sec=provider_timeout,
            http_client=http_client,
            profile_id=str(provider_cfg.get("profile_id") or engine),
            max_concurrency=_normalize_optional_positive_int(
                provider_cfg.get("max_concurrency")
            ),
        )

    return NoopTranslator(target_language=target_language, cache_size=cache_size)

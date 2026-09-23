"""Detect a translation service's working API format before it is saved."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .translator import (
    API_TYPE_CHAT_COMPLETIONS,
    API_TYPE_MESSAGES,
    API_TYPE_RESPONSES,
    OpenAITranslator,
)


API_TYPES = (API_TYPE_CHAT_COMPLETIONS, API_TYPE_RESPONSES, API_TYPE_MESSAGES)
API_LABELS = ("Chat completions", "Responses", "Messages")


def _strip_known_endpoint(url: str) -> tuple[str, bool]:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/messages"):
        if path.lower().endswith(suffix):
            return urlunsplit(parts._replace(path=path[:-len(suffix)])), True
    return url, False


async def detect_api_type(profile: dict[str, Any], client: httpx.AsyncClient) -> tuple[str, str | None]:
    """Return the first format that produces usable translation text.

    A complete endpoint may name one format already. Replace that suffix while
    testing so all three formats reach their own paths, then store the winning
    endpoint explicitly to keep the runtime request body and URL aligned.
    """
    base_url, was_explicit = _strip_known_endpoint(profile["base_url"])
    failures: list[str] = []
    for api_type, label in zip(API_TYPES, API_LABELS):
        translator = OpenAITranslator(
            api_key=profile.get("api_key", ""),
            model=profile["model"],
            target_language="zh-TW",
            base_url=base_url,
            api_type=api_type,
            auto_complete_endpoint=was_explicit or profile.get("auto_complete_endpoint", True),
            timeout_sec=5,
            http_client=client,
        )
        try:
            result = await translator.atranslate("Hello world", "en")
            if result.strip():
                return api_type, translator.api_url if was_explicit else None
            failures.append(f"{label}: empty response")
        except Exception as exc:
            # Provider response bodies can contain secrets; expose only a code
            # or the exception class in the settings UI.
            description = str(exc)
            code = description.split(" ", 1)[0]
            reason = f"HTTP {code}" if code.isdigit() and len(code) == 3 else type(exc).__name__
            failures.append(f"{label}: {reason}")
    raise ValueError("All three API formats failed (" + "; ".join(failures) + "). Check the endpoint, model and API key.")

from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


API_PATHS = {
    "chat_completions": "/v1/chat/completions",
    "responses": "/v1/responses",
    "messages": "/v1/messages",
}

DEFAULT_RUNTIME: Dict[str, Any] = {
    "max_model_len": 4096,
    "max_num_seqs": 16,
    "tensor_parallel_size": 1,
    "gpu_memory_utilization": 0.9,
    "dtype": "auto",
    "quantization": "auto",
}


def default_profile(profile_id: str = "default") -> Dict[str, Any]:
    return {
        "id": profile_id,
        "name": "Translation API",
        "mode": "external",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "",
        "served_model": "",
        "api_type": "chat_completions",
        "auto_complete_endpoint": True,
        "max_concurrency": 0,
        "model": "",
        "runtime": copy.deepcopy(DEFAULT_RUNTIME),
    }


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _as_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    if value is None:
        return fallback
    return bool(value)


def normalize_profile(profile: Mapping[str, Any], index: int = 0) -> Dict[str, Any]:
    defaults = default_profile(f"profile-{index + 1}")
    normalized = copy.deepcopy(dict(profile))
    profile_id = str(profile.get("id") or defaults["id"]).strip()
    normalized["id"] = profile_id or defaults["id"]
    normalized["name"] = str(profile.get("name") or f"Translation service {index + 1}")

    mode = str(profile.get("mode") or "external").lower()
    normalized["mode"] = mode if mode in {"local", "external"} else "external"
    normalized["base_url"] = str(profile.get("base_url") or "")
    normalized["api_key"] = str(profile.get("api_key") or "")
    normalized["served_model"] = str(profile.get("served_model") or "")
    normalized["model"] = str(profile.get("model") or "")

    api_type = str(profile.get("api_type") or "chat_completions")
    normalized["api_type"] = (
        api_type if api_type in API_PATHS else "chat_completions"
    )
    normalized["auto_complete_endpoint"] = _as_bool(
        profile.get("auto_complete_endpoint"), True
    )
    normalized["max_concurrency"] = max(
        0, _as_int(profile.get("max_concurrency"), 0)
    )

    runtime_source = profile.get("runtime")
    runtime = runtime_source if isinstance(runtime_source, Mapping) else {}
    normalized_runtime = copy.deepcopy(dict(runtime))
    normalized_runtime["max_model_len"] = max(
        1,
        _as_int(runtime.get("max_model_len"), DEFAULT_RUNTIME["max_model_len"]),
    )
    normalized_runtime["max_num_seqs"] = max(
        1,
        _as_int(runtime.get("max_num_seqs"), DEFAULT_RUNTIME["max_num_seqs"]),
    )
    normalized_runtime["tensor_parallel_size"] = max(
        1,
        _as_int(
            runtime.get("tensor_parallel_size"),
            DEFAULT_RUNTIME["tensor_parallel_size"],
        ),
    )
    gpu_memory = _as_float(
        runtime.get("gpu_memory_utilization"),
        DEFAULT_RUNTIME["gpu_memory_utilization"],
    )
    normalized_runtime["gpu_memory_utilization"] = (
        gpu_memory
        if 0.0 < gpu_memory <= 1.0
        else DEFAULT_RUNTIME["gpu_memory_utilization"]
    )
    normalized_runtime["dtype"] = str(
        runtime.get("dtype") or DEFAULT_RUNTIME["dtype"]
    )
    normalized_runtime["quantization"] = str(
        runtime.get("quantization") or DEFAULT_RUNTIME["quantization"]
    )
    normalized["runtime"] = normalized_runtime
    return normalized


def load_profiles(config: Mapping[str, Any]) -> tuple[list[Dict[str, Any]], str, bool]:
    translation = config.get("translation")
    translation_map = translation if isinstance(translation, Mapping) else {}
    vllm = translation_map.get("vllm")
    section_present = isinstance(vllm, Mapping)
    vllm_map = vllm if section_present else {}
    raw_profiles = vllm_map.get("profiles")
    profile_items: Iterable[Any] = (
        raw_profiles if isinstance(raw_profiles, list) else []
    )
    profiles = [
        normalize_profile(item, index)
        for index, item in enumerate(profile_items)
        if isinstance(item, Mapping)
    ]
    if not profiles:
        profiles = [default_profile()]
    active_profile = str(vllm_map.get("active_profile") or profiles[0]["id"])
    if active_profile not in {profile["id"] for profile in profiles}:
        active_profile = profiles[0]["id"]
    return profiles, active_profile, section_present


def update_config(
    config: Mapping[str, Any],
    profiles: Iterable[Mapping[str, Any]],
    active_profile: str,
    activate: bool | None = False,
) -> Dict[str, Any]:
    updated = copy.deepcopy(dict(config))
    translation = updated.get("translation")
    updated["translation"] = dict(translation) if isinstance(translation, Mapping) else {}
    vllm = updated["translation"].get("vllm")
    vllm_config = dict(vllm) if isinstance(vllm, Mapping) else {}
    normalized_profiles = [
        normalize_profile(profile, index) for index, profile in enumerate(profiles)
    ]
    if not normalized_profiles:
        normalized_profiles = [default_profile()]
    ids = {profile["id"] for profile in normalized_profiles}
    vllm_config["active_profile"] = (
        active_profile if active_profile in ids else normalized_profiles[0]["id"]
    )
    vllm_config["profiles"] = normalized_profiles
    current_engine = str(updated["translation"].get("engine") or "").strip().lower()
    if activate:
        if current_engine and current_engine != "vllm":
            vllm_config["previous_engine"] = current_engine
        updated["translation"]["engine"] = "vllm"
    elif activate is False and current_engine == "vllm":
        previous_engine = str(vllm_config.get("previous_engine") or "").strip().lower()
        updated["translation"]["engine"] = (
            previous_engine
            if previous_engine and previous_engine != "vllm"
            else "nllb"
        )
    updated["translation"]["vllm"] = vllm_config
    return updated


def resolve_request_url(base_url: str, api_type: str, auto_complete: bool) -> str:
    raw_url = base_url.strip()
    if not raw_url or not auto_complete:
        return raw_url
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return raw_url
    normalized_path = parsed.path.rstrip("/")
    lower_path = normalized_path.lower()
    if any(lower_path.endswith(path) for path in API_PATHS.values()):
        return raw_url
    endpoint_path = API_PATHS.get(api_type, API_PATHS["chat_completions"])
    if lower_path.endswith("/v1"):
        completed_path = normalized_path + endpoint_path[3:]
    else:
        completed_path = normalized_path + endpoint_path
    return urlunsplit(parsed._replace(path=completed_path))


def _quote_cli(value: str) -> str:
    if not value:
        return '""'
    if re.fullmatch(r"[A-Za-z0-9_./:\\-]+", value):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def build_launch_command(profile: Mapping[str, Any]) -> str:
    normalized = normalize_profile(profile)
    model = normalized["model"] or normalized["served_model"] or "<model-or-path>"
    try:
        endpoint = urlsplit(normalized["base_url"])
        host = endpoint.hostname or "127.0.0.1"
        port = endpoint.port or (443 if endpoint.scheme == "https" else 8000)
    except ValueError:
        host = "127.0.0.1"
        port = 8000
    runtime = normalized["runtime"]
    parts = [
        "python",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        _quote_cli(model),
        "--host",
        host,
        "--port",
        str(port),
        "--max-model-len",
        str(runtime["max_model_len"]),
        "--max-num-seqs",
        str(runtime["max_num_seqs"]),
        "--tensor-parallel-size",
        str(runtime["tensor_parallel_size"]),
        "--gpu-memory-utilization",
        f'{runtime["gpu_memory_utilization"]:.2f}',
        "--dtype",
        str(runtime["dtype"]),
    ]
    if normalized["served_model"]:
        parts.extend(
            ["--served-model-name", _quote_cli(normalized["served_model"])]
        )
    quantization = str(runtime["quantization"])
    if quantization not in {"", "auto", "none"}:
        parts.extend(["--quantization", quantization])
    return " ".join(parts)

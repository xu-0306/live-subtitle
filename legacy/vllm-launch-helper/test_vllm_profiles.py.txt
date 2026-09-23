from __future__ import annotations

from gui.vllm_profiles import (
    build_launch_command,
    load_profiles,
    normalize_profile,
    resolve_request_url,
    update_config,
)


def test_missing_section_uses_virtual_default_without_claiming_presence() -> None:
    config = {"translation": {"engine": "nllb"}}

    profiles, active_profile, section_present = load_profiles(config)

    assert section_present is False
    assert profiles[0]["id"] == active_profile
    assert config == {"translation": {"engine": "nllb"}}


def test_normalize_profile_bounds_bad_runtime_values_and_preserves_extensions() -> None:
    profile = {
        "id": "remote",
        "name": "Remote",
        "auto_complete_endpoint": "false",
        "max_concurrency": -3,
        "vendor_option": "keep-me",
        "runtime": {
            "max_model_len": -10,
            "max_num_seqs": "bad",
            "tensor_parallel_size": 0,
            "gpu_memory_utilization": 2.0,
            "future_runtime_option": True,
        },
    }

    normalized = normalize_profile(profile)

    assert normalized["auto_complete_endpoint"] is False
    assert normalized["max_concurrency"] == 0
    assert normalized["vendor_option"] == "keep-me"
    assert normalized["runtime"]["max_model_len"] == 1
    assert normalized["runtime"]["max_num_seqs"] == 16
    assert normalized["runtime"]["tensor_parallel_size"] == 1
    assert normalized["runtime"]["gpu_memory_utilization"] == 0.9
    assert normalized["runtime"]["future_runtime_option"] is True


def test_update_config_preserves_existing_translation_and_vllm_keys() -> None:
    config = {
        "translation": {
            "engine": "openai",
            "openai": {"model": "existing"},
            "vllm": {"scheduler_limit": 12},
        }
    }
    profiles = [
        {
            "id": "remote",
            "name": "Remote",
            "mode": "external",
            "vendor_option": "keep-me",
            "runtime": {},
        }
    ]

    updated = update_config(config, profiles, "remote")

    assert updated["translation"]["engine"] == "openai"
    assert updated["translation"]["openai"] == {"model": "existing"}
    assert updated["translation"]["vllm"]["scheduler_limit"] == 12
    assert updated["translation"]["vllm"]["profiles"][0]["vendor_option"] == "keep-me"


def test_update_config_activation_roundtrip_restores_previous_engine() -> None:
    config = {"translation": {"engine": "nllb"}}
    profiles = [{"id": "local", "name": "Local", "runtime": {}}]

    inactive = update_config(config, profiles, "local", activate=False)
    active = update_config(inactive, profiles, "local", activate=True)
    restored = update_config(active, profiles, "local", activate=False)

    assert inactive["translation"]["engine"] == "nllb"
    assert active["translation"]["engine"] == "vllm"
    assert active["translation"]["vllm"]["previous_engine"] == "nllb"
    assert restored["translation"]["engine"] == "nllb"


def test_deactivating_legacy_vllm_config_falls_back_to_nllb() -> None:
    config = {"translation": {"engine": "vllm", "vllm": {}}}
    profiles = [{"id": "local", "name": "Local", "runtime": {}}]

    restored = update_config(config, profiles, "local", activate=False)

    assert restored["translation"]["engine"] == "nllb"


def test_resolve_request_url_honors_completion_policy() -> None:
    assert (
        resolve_request_url("https://host.example/v1", "chat_completions", True)
        == "https://host.example/v1/chat/completions"
    )
    assert (
        resolve_request_url(
            "https://host.example/v1/responses", "responses", True
        )
        == "https://host.example/v1/responses"
    )
    assert (
        resolve_request_url(
            "https://host.example/v1/responses/?region=tw", "responses", True
        )
        == "https://host.example/v1/responses/?region=tw"
    )
    assert (
        resolve_request_url("https://provider.example/custom", "messages", False)
        == "https://provider.example/custom"
    )
    assert (
        resolve_request_url(
            "https://provider.example/v1?region=tw", "responses", True
        )
        == "https://provider.example/v1/responses?region=tw"
    )


def test_launch_command_contains_runtime_but_never_api_key() -> None:
    command = build_launch_command(
        {
            "mode": "local",
            "base_url": "http://0.0.0.0:9000/v1",
            "api_key": "top-secret-token",
            "model": "Qwen/Qwen3-8B",
            "served_model": "translator",
            "runtime": {
                "max_model_len": 8192,
                "max_num_seqs": 32,
                "tensor_parallel_size": 2,
                "gpu_memory_utilization": 0.85,
                "dtype": "bfloat16",
                "quantization": "awq",
            },
        }
    )

    assert "top-secret-token" not in command
    assert "--max-model-len 8192" in command
    assert "--max-num-seqs 32" in command
    assert "--tensor-parallel-size 2" in command
    assert "--gpu-memory-utilization 0.85" in command
    assert "--quantization awq" in command

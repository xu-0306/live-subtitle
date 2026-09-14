from __future__ import annotations

import json
from pathlib import Path

from backend.config_store import import_profiles, load_snapshot, service_profiles, save_config
from backend.translation_service import resolve_selection
from backend.translator import resolve_provider_config


def _legacy_openai(profile_id: str = "old-openai", *, endpoint: str = "https://provider.example/v1") -> dict:
    return {
        "id": profile_id,
        "name": "Research provider",
        "engine": "openai",
        "model": "future-model",
        "connection": {
            "apiUrl": endpoint,
            "apiMode": "responses",
            "autoCompleteApiUrl": False,
        },
        "apiKey": "secret-that-must-stay-in-storage",
    }


def test_import_normalizes_nested_connection_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_config(path, {"translation": {"engine": "noop"}})
    profile = _legacy_openai()

    first = import_profiles(path, [profile], expected_revision=1)
    assert first["errors"] == []
    catalog_id = first["mapping"][profile["id"]]
    saved, revision = load_snapshot(path)
    stored = next(item for item in saved["translation"]["service_profiles"] if "profile:" + item["id"] == catalog_id)
    assert stored["base_url"] == profile["connection"]["apiUrl"]
    assert stored["api_type"] == "responses"
    assert stored["auto_complete_endpoint"] is False
    assert stored["api_key"] == profile["apiKey"]

    repeated = import_profiles(path, [profile], expected_revision=revision)
    assert repeated["mapping"][profile["id"]] == catalog_id
    assert repeated["revision"] == revision
    repeated_config, _ = load_snapshot(path)
    assert len(repeated_config["translation"]["service_profiles"]) == 1

    public = json.dumps(service_profiles(repeated_config))
    # The storage layer retains credentials for runtime use; catalog callers
    # use the public projection and must never serialize this value.
    assert profile["apiKey"] in public


def test_changed_settings_do_not_overwrite_imported_profile_and_failures_are_retained(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_config(path, {"translation": {"engine": "noop"}})
    original = _legacy_openai()
    first = import_profiles(path, [original])
    changed = _legacy_openai(endpoint="https://another.example/v1")
    invalid = {"id": "broken", "name": "Broken", "engine": "future-provider"}

    result = import_profiles(path, [changed, invalid], expected_revision=first["revision"])
    assert result["mapping"][changed["id"]] != first["mapping"][original["id"]]
    assert result["errors"] and result["errors"][0]["id"] == invalid["id"]
    config, _ = load_snapshot(path)
    profiles = config["translation"]["service_profiles"]
    assert len(profiles) == 2
    assert {item["base_url"] for item in profiles} == {
        original["connection"]["apiUrl"],
        changed["connection"]["apiUrl"],
    }
    assert "secret-that-must-stay-in-storage" not in json.dumps(result)


def test_ollama_and_nllb_entries_keep_provider_adapters(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_config(path, {"translation": {"engine": "noop"}})
    profiles = [
        {"id": "ollama-local", "name": "Ollama", "engine": "ollama", "model": "new-model", "host": "http://127.0.0.1:11434"},
        {"id": "nllb-local", "name": "NLLB", "engine": "nllb", "model": "facebook/nllb-200-distilled-600M"},
    ]
    result = import_profiles(path, profiles)
    assert result["errors"] == []
    config, _ = load_snapshot(path)
    by_id = {item["imported_from"]["id"]: item for item in config["translation"]["service_profiles"]}
    assert by_id["ollama-local"]["engine"] == "ollama"
    assert by_id["ollama-local"]["base_url"].startswith("http://")
    assert by_id["nllb-local"]["engine"] == "nllb"


def test_missing_selection_inherits_configured_profile_default() -> None:
    config = {
        "local_llama": {"installed_models": []},
        "translation": {
            "engine": "noop",
            "default_selection": {"kind": "profile", "id": "ollama-local"},
            "service_profiles": [
                {"id": "ollama-local", "name": "Ollama", "engine": "ollama", "model": "future-model", "host": "http://localhost:11434"},
            ],
        },
    }
    resolved, local_id = resolve_selection(config, {"target_language": "Te Reo Māori"})
    assert local_id is None
    assert resolved["engine"] == "ollama"
    assert resolved["ollama"]["model"] == "future-model"
    assert resolved["target_language"] == "Te Reo Māori"


def test_none_selection_is_explicit_no_translation_and_has_no_lease() -> None:
    config = {
        "translation": {
            "engine": "managed_llama",
            "target_language": "zh-TW",
            "default_selection": {"kind": "local", "id": "installed"},
        },
        "local_llama": {"installed_models": ["installed"], "model_id": "installed", "enabled": True},
    }
    resolved, local_id = resolve_selection(config, {"selection": {"kind": "none", "id": "none"}})
    assert resolved["engine"] == "noop"
    assert resolved["selection"] == {"kind": "none", "id": "none"}
    assert local_id is None


def test_openai_compatible_profile_uses_existing_provider_adapter() -> None:
    config = {
        "translation": {
            "engine": "noop",
            "service_profiles": [
                {
                    "id": "generic-api",
                    "name": "Generic API",
                    "engine": "openai_compatible",
                    "model": "future-model",
                    "base_url": "https://provider.example/v1",
                    "api_key": "secret",
                }
            ],
        }
    }
    resolved, _ = resolve_selection(
        config,
        {"selection": {"kind": "profile", "id": "generic-api"}},
    )
    provider = resolve_provider_config(resolved)
    assert resolved["engine"] == "openai_compatible"
    assert provider["model"] == "future-model"
    assert provider["base_url"] == "https://provider.example/v1"


def test_explicit_empty_catalog_does_not_resurrect_legacy_vllm_profiles() -> None:
    config = {
        "translation": {
            "service_profiles": [],
            "vllm": {
                "active_profile": "old",
                "profiles": [{"id": "old", "name": "Legacy", "model": "legacy-model", "base_url": "http://127.0.0.1:8000/v1"}],
            },
        }
    }
    assert service_profiles(config) == []


def test_import_preserves_legacy_vllm_profiles_when_creating_catalog(tmp_path: Path) -> None:
    """The first canonical import must retain profiles from the v1 vLLM adapter."""
    path = tmp_path / "config.yaml"
    save_config(
        path,
        {
            "translation": {
                "engine": "vllm",
                "vllm": {
                    "active_profile": "legacy",
                    "profiles": [
                        {
                            "id": "legacy",
                            "name": "Legacy vLLM",
                            "model": "legacy-model",
                            "base_url": "http://127.0.0.1:8000/v1",
                        }
                    ],
                },
            }
        },
    )

    result = import_profiles(
        path,
        [
            {
                "id": "new-ollama",
                "name": "New Ollama",
                "engine": "ollama",
                "model": "new-model",
                "host": "http://127.0.0.1:11434",
            }
        ],
    )

    assert result["errors"] == []
    config, _ = load_snapshot(path)
    profiles = config["translation"]["service_profiles"]
    assert any(profile["id"] == "legacy" for profile in profiles)
    imported = next(
        profile
        for profile in profiles
        if profile.get("imported_from", {}).get("id") == "new-ollama"
    )
    assert result["mapping"]["new-ollama"] == "profile:" + imported["id"]
    legacy = next(profile for profile in profiles if profile["id"] == "legacy")
    assert legacy["engine"] == "vllm"
    assert legacy["base_url"] == "http://127.0.0.1:8000/v1"

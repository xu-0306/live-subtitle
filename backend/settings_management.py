"""Small, revision-checked settings surface shared by the desktop and extension.

Only explicit fields leave this module. In particular, API keys and local file
paths never appear in a management snapshot sent to a browser extension.
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config_store import (
    load_snapshot,
    normalize_service_profile,
    public_profile,
    save_config,
    service_profiles,
)
from .local_catalog import MODELS, ModelSpec, get_model, list_models, model_artifact_state
from .model_manager import WHISPER_MODEL_URLS, resolve_stt_model_target


PROFILE_FIELDS = frozenset({
    "id", "name", "engine", "model", "base_url", "api_type",
    "auto_complete_endpoint", "max_concurrency", "api_key",
})


def _local_root(config: dict[str, Any], default_root: Path) -> Path:
    value = config.get("local_llama", {}).get("root")
    return Path(str(value)).expanduser() if value else default_root


def snapshot(config_path: Path, service: Any) -> dict[str, Any]:
    config, revision = load_snapshot(config_path)
    local = config.get("local_llama") or {}
    root = _local_root(config, service.root)
    published = set(local.get("installed_models") or [])
    if local.get("enabled") and local.get("model_id"):
        published.add(str(local["model_id"]))
    local_models = []
    builtin = {model.id for model in MODELS}
    for model in list_models(config):
        present, status, reason = model_artifact_state(root, model)
        local_models.append({
            "id": model.id,
            "name": model.name,
            "repo": model.repo,
            "revision": model.revision,
            "files": [item.filename for item in model.files],
            "size": model.size,
            "estimated_vram_mb": model.estimated_vram_mb,
            "license": model.license,
            "notes": model.notes,
            "custom": model.id not in builtin,
            "installed": present,
            "published": present and model.id in published,
            "status": status,
            **({"reason": reason} if reason else {}),
        })
    profiles = []
    for profile in service_profiles(config):
        visible = public_profile(profile)
        visible["has_api_key"] = bool(profile.get("api_key"))
        available, status, reason = service._profile_catalog_state(profile)
        visible.update(available=available, status=status)
        if reason:
            visible["reason"] = reason
        profiles.append(visible)
    catalog = service.desktop_catalog(config_override=config, revision_override=revision)
    return {
        "type": "desktop_management_result",
        "version": 1,
        "ok": True,
        "revision": revision,
        "server_id": catalog["server_id"],
        "instance_id": catalog["instance_id"],
        "stt_models": catalog["stt_models"],
        "local_models": local_models,
        "profiles": profiles,
        "defaults": catalog["defaults"],
    }


def _selection_exists(config: dict[str, Any], selection: dict[str, str], root: Path, service: Any) -> bool:
    kind, item_id = selection.get("kind"), selection.get("id")
    if kind == "none" and item_id == "none":
        return True
    if kind == "profile":
        profile = next((item for item in service_profiles(config) if item["id"] == item_id), None)
        return profile is not None and service._profile_catalog_state(profile)[0]
    if kind == "local":
        model = get_model(str(item_id), config)
        present, _, _ = model_artifact_state(root, model)
        return present and item_id in set(config.get("local_llama", {}).get("installed_models") or [])
    return False


def prepare_service_profile(config: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Build a profile for validation or saving, preserving an existing secret."""
    incoming = data.get("profile")
    if not isinstance(incoming, dict):
        raise ValueError("A translation service is required.")
    existing = service_profiles(config)
    profile_id = str(incoming.get("id") or "").strip()
    if not profile_id:
        profile_id = f"service-{uuid.uuid4().hex}"
        incoming["id"] = profile_id
    old = next((item for item in existing if item["id"] == profile_id), None)
    candidate = copy.deepcopy(old) if old else {}
    candidate.update({key: value for key, value in incoming.items() if key in PROFILE_FIELDS and key != "api_key"})
    engine = str(candidate.get("engine") or "").replace("-", "_")
    nested = candidate.get(engine)
    if isinstance(nested, dict) and "base_url" in incoming:
        nested["host" if engine == "ollama" else "base_url"] = incoming["base_url"]
    if "api_key" in incoming and str(incoming["api_key"]).strip():
        candidate["api_key"] = str(incoming["api_key"]).strip()
    if data.get("clear_api_key") is True:
        candidate["api_key"] = ""
        for container in (candidate, candidate.get("connection"), candidate.get(engine)):
            if isinstance(container, dict):
                for key in ("api_key", "apiKey", "token", "authorization"):
                    container.pop(key, None)
    normalized = normalize_service_profile(candidate)
    if normalized["engine"] not in {"ollama", "openai", "openai_compatible", "nllb"}:
        raise ValueError("This service type is not editable in the browser.")
    if not normalized["model"]:
        raise ValueError("A service model is required.")
    if normalized["engine"] != "nllb" and not normalized["base_url"].startswith(("http://", "https://")):
        raise ValueError("Enter a complete HTTP or HTTPS endpoint.")
    return normalized


def mutate(config_path: Path, service: Any, action: str, data: dict[str, Any], expected_revision: int) -> int:
    """Apply one narrow edit; save_config rejects a concurrent GUI/browser edit."""
    config, revision = load_snapshot(config_path)
    if revision != expected_revision:
        from .config_store import RevisionConflictError
        raise RevisionConflictError(expected_revision, revision)
    updated = copy.deepcopy(config)
    local = updated.setdefault("local_llama", {})
    translation = updated.setdefault("translation", {})
    root = _local_root(updated, service.root)
    item_id = str(data.get("id") or "").strip()

    if action == "save_profile":
        normalized = prepare_service_profile(updated, data)
        existing = service_profiles(updated)
        old = next((item for item in existing if item["id"] == normalized["id"]), None)
        translation["service_profiles"] = [normalized if item["id"] == normalized["id"] else item for item in existing]
        if old is None:
            translation["service_profiles"].append(normalized)
    elif action == "delete_profile":
        existing = service_profiles(updated)
        if item_id not in {item["id"] for item in existing}:
            raise ValueError("Service no longer exists.")
        translation["service_profiles"] = [item for item in existing if item["id"] != item_id]
        if translation.get("default_selection") == {"kind": "profile", "id": item_id}:
            translation["default_selection"] = {"kind": "none", "id": "none"}
    elif action == "rename_local":
        custom = local.get("custom_models") or []
        entry = next((item for item in custom if item.get("id") == item_id), None)
        if entry is None:
            raise ValueError("Only custom GGUF entries can be edited.")
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 160:
            raise ValueError("Enter a model name up to 160 characters.")
        entry["name"] = name
        entry["notes"] = str(data.get("notes") or "")[:1000]
    elif action == "delete_local":
        custom = local.get("custom_models") or []
        if item_id not in {item.get("id") for item in custom}:
            raise ValueError("Only custom GGUF entries can be removed.")
        local["custom_models"] = [item for item in custom if item.get("id") != item_id]
        local["installed_models"] = [value for value in local.get("installed_models", []) if value != item_id]
        if local.get("model_id") == item_id:
            local["enabled"] = False
            local["model_id"] = MODELS[0].id
        if translation.get("default_selection") == {"kind": "local", "id": item_id}:
            translation["default_selection"] = {"kind": "none", "id": "none"}
    elif action == "publish_local":
        model = get_model(item_id, updated)
        present, _, _ = model_artifact_state(root, model)
        if bool(data.get("published")) and not present:
            raise ValueError("Download and verify this model before publishing it.")
        installed = set(local.get("installed_models") or [])
        if bool(data.get("published")):
            installed.add(item_id)
        else:
            installed.discard(item_id)
            if local.get("model_id") == item_id:
                local["enabled"] = False
            if translation.get("default_selection") == {"kind": "local", "id": item_id}:
                translation["default_selection"] = {"kind": "none", "id": "none"}
        local["installed_models"] = sorted(installed)
    elif action == "set_defaults":
        if "stt_model" in data:
            model_id = str(data["stt_model"] or "").strip()
            if model_id not in WHISPER_MODEL_URLS and model_id not in {
                item["id"] for item in service._stt_catalog(updated)
            }:
                raise ValueError("Unknown speech model.")
            stt = updated.setdefault("stt", {})
            target = resolve_stt_model_target({**stt, "model": model_id, "model_path": None})
            if target is None or not target.is_file():
                raise ValueError("Download the speech model before selecting it as default.")
            stt["model"] = model_id
            stt.pop("model_path", None)
        if "translation_selection" in data:
            selection = data["translation_selection"]
            if not isinstance(selection, dict) or not _selection_exists(updated, selection, root, service):
                raise ValueError("The selected translation model is unavailable.")
            translation["default_selection"] = {"kind": selection["kind"], "id": selection["id"]}
        if "target_language" in data:
            value = str(data["target_language"] or "").strip()
            if not value or len(value) > 128:
                raise ValueError("Enter a target language up to 128 characters.")
            translation["target_language"] = value
        if "partial" in data:
            translation["partial"] = bool(data["partial"])
    else:
        raise ValueError("Unknown management action.")
    return save_config(config_path, updated, expected_revision=expected_revision)


def register_model(config_path: Path, model: ModelSpec, expected_revision: int) -> int:
    config, revision = load_snapshot(config_path)
    if revision != expected_revision:
        from .config_store import RevisionConflictError
        raise RevisionConflictError(expected_revision, revision)
    if model.id in {item.id for item in list_models(config)}:
        raise ValueError("This GGUF model is already in the catalog.")
    config.setdefault("local_llama", {}).setdefault("custom_models", []).append(asdict(model))
    return save_config(config_path, config, expected_revision=expected_revision)

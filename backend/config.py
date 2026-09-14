from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

APP_DIR_NAME = "Live Subtitle"
LEGACY_APP_DIR_NAME = "STT-TTS"
SPEECH_MODELS_DIR_NAME = "Speech models"
LOCAL_TRANSLATION_MODELS_DIR_NAME = "Local Translation Models"
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")
CONFIG_ENV_VAR = "STT_CONFIG_PATH"


def _default_app_dir() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        base = Path(appdata)
    else:
        base = Path.home()
    target = base / (APP_DIR_NAME if os.getenv("APPDATA") else ".live-subtitle")
    legacy = base / (LEGACY_APP_DIR_NAME if os.getenv("APPDATA") else ".stt-tts")
    # A direct backend launch cannot migrate the user config itself. Until the
    # GUI creates the new directory, keep existing installations on their
    # legacy model cache instead of silently downloading everything again.
    if not target.exists() and (legacy / "config.yaml").is_file():
        return legacy
    target.mkdir(parents=True, exist_ok=True)
    return target


def _ensure_model_cache_dir(cfg: Dict[str, Any]) -> None:
    stt_cfg = cfg.get("stt")
    if not isinstance(stt_cfg, dict):
        stt_cfg = {}
        cfg["stt"] = stt_cfg
    storage_cfg = cfg.get("models")
    if isinstance(storage_cfg, dict) and storage_cfg.get("root"):
        root = Path(str(storage_cfg["root"])).expanduser()
        stt_cfg["model_cache_dir"] = str(root / SPEECH_MODELS_DIR_NAME)
        local_cfg = cfg.get("local_llama")
        if not isinstance(local_cfg, dict):
            local_cfg = {}
            cfg["local_llama"] = local_cfg
        local_cfg["root"] = str(root / LOCAL_TRANSLATION_MODELS_DIR_NAME)
    if not stt_cfg.get("model_cache_dir"):
        model_dir = _default_app_dir() / SPEECH_MODELS_DIR_NAME
        model_dir.mkdir(parents=True, exist_ok=True)
        stt_cfg["model_cache_dir"] = str(model_dir)


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    config_override = path or os.getenv(CONFIG_ENV_VAR)
    config_path = Path(config_override) if config_override else DEFAULT_CONFIG_PATH
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping: {config_path}")
    # config_store commits its optimistic-concurrency marker in the same
    # atomic document as the YAML.  Runtime consumers should only see the
    # application settings, never storage bookkeeping.
    data.pop("_stt_tts_store", None)
    _ensure_model_cache_dir(data)
    return data

from __future__ import annotations

from backend.local_runtime import Hardware
from gui.setup_assistant import (
    SETUP_ASSISTANT_VERSION,
    apply_recommendation,
    recommendations_for_hardware,
)


def _recommended(hardware: Hardware):
    choices = recommendations_for_hardware(hardware)
    selected = [item for item in choices if item.recommended]
    assert len(selected) == 1
    return selected[0]


def test_8gb_gpu_gets_conservative_shared_model_pair() -> None:
    item = _recommended(Hardware("RTX 4060", 8192, 7600))
    assert item.stt_model == "small"
    assert item.translation_model_id == "qwen35-2b-q4"
    assert item.gpu_layers == -1
    assert item.estimated_vram_mb <= 7600


def test_24gb_gpu_gets_high_quality_pair() -> None:
    item = _recommended(Hardware("RTX 3090", 24576, 23000))
    assert item.stt_model == "large-v3"
    assert item.translation_model_id == "qwen25-7b-q4"
    assert item.estimated_vram_mb < 16 * 1024


def test_busy_gpu_uses_current_free_vram_for_the_default() -> None:
    item = _recommended(Hardware("RTX 3090", 24576, 6000))
    assert item.estimated_vram_mb <= 6000
    assert item.stt_model in {"base", "small"}


def test_gpu_with_too_little_free_memory_falls_back_to_cpu() -> None:
    item = _recommended(Hardware("RTX 3090", 24576, 3000))
    assert item.gpu_layers == 0
    assert item.stt_model == "base"


def test_low_vram_uses_cpu_translation_and_offers_external_fallback() -> None:
    choices = recommendations_for_hardware(Hardware("GTX 1660", 6144, 5500))
    item = _recommended(Hardware("GTX 1660", 6144, 5500))
    assert item.stt_model == "base"
    assert item.translation_model_id == "qwen25-1.5b-q4"
    assert item.gpu_layers == 0
    assert any(choice.translation_model_id is None for choice in choices)


def test_apply_recommendation_preserves_unrelated_settings() -> None:
    config = {
        "server": {"port": 9000},
        "stt": {"model": "tiny", "language": "ja"},
        "translation": {"target_language": "zh-TW"},
        "local_llama": {"installed_models": ["existing"]},
    }
    item = _recommended(Hardware("RTX 3090", 24576, 23000))

    updated = apply_recommendation(config, item)

    assert updated["server"]["port"] == 9000
    assert updated["stt"]["language"] == "ja"
    assert updated["stt"]["model"] == item.stt_model
    assert updated["local_llama"]["installed_models"] == ["existing"]
    assert updated["local_llama"]["model_id"] == item.translation_model_id
    assert updated["translation"]["default_selection"] == {"kind": "none", "id": "none"}
    assert updated["local_llama"]["pending_default_model_id"] == item.translation_model_id
    assert updated["gui"]["setup_assistant_version"] == SETUP_ASSISTANT_VERSION
    assert config["stt"]["model"] == "tiny"


def test_installed_recommendation_can_become_default_immediately() -> None:
    item = _recommended(Hardware("RTX 3090", 24576, 23000))
    updated = apply_recommendation(
        {"local_llama": {"installed_models": [item.translation_model_id]}}, item
    )
    assert updated["translation"]["default_selection"] == {
        "kind": "local",
        "id": item.translation_model_id,
    }
    assert "pending_default_model_id" not in updated["local_llama"]

"""First-run hardware guidance for choosing a safe local model pair."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from PySide6 import QtCore, QtWidgets

from backend.local_catalog import get_model
from backend.local_runtime import (
    Hardware,
    SHARED_VRAM_HEADROOM_MB,
    STT_VRAM_RESERVE_MB,
)


SETUP_ASSISTANT_VERSION = 1

@dataclass(frozen=True)
class ModelRecommendation:
    title: str
    stt_model: str
    translation_model_id: str | None
    gpu_layers: int
    estimated_vram_mb: int
    rationale: str
    recommended: bool = False


_GPU_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("Low latency", "base", "qwen25-1.5b-q4"),
    ("Everyday", "small", "qwen35-2b-q4"),
    ("Balanced plus", "medium", "qwen35-2b-q4"),
    ("Quality", "medium", "qwen35-4b-q4"),
    ("High quality", "large-v3", "qwen35-4b-q4"),
    ("Maximum local quality", "large-v3", "qwen25-7b-q4"),
)


def estimate_pair_vram_mb(stt_model: str, translation_model_id: str) -> int:
    return (
        STT_VRAM_RESERVE_MB.get(
            stt_model, STT_VRAM_RESERVE_MB["large-v3"]
        )
        + get_model(translation_model_id).estimated_vram_mb
        + SHARED_VRAM_HEADROOM_MB
    )


def recommendations_for_hardware(hardware: Hardware) -> list[ModelRecommendation]:
    """Return understandable choices with one conservative default.

    Current free memory is used when available, so another GPU-heavy program
    cannot make the first recommendation fail immediately.  Re-running the
    assistant after closing that program can expose a larger recommendation.
    """

    if hardware.total_mb < 8 * 1024:
        translation = get_model("qwen25-1.5b-q4")
        return [
            ModelRecommendation(
                title="CPU-safe local setup (recommended)",
                stt_model="base",
                translation_model_id=translation.id,
                gpu_layers=0,
                estimated_vram_mb=STT_VRAM_RESERVE_MB["base"],
                rationale=(
                    "A supported NVIDIA GPU with at least 8 GiB was not found. "
                    "Whisper Base keeps speech recognition lighter and local translation "
                    "runs on the CPU. Translation speed will depend on the processor; an "
                    "external translation service remains the faster fallback."
                ),
                recommended=True,
            ),
            ModelRecommendation(
                title="Speech only / external translation",
                stt_model="base",
                translation_model_id=None,
                gpu_layers=0,
                estimated_vram_mb=STT_VRAM_RESERVE_MB["base"],
                rationale=(
                    "Choose a translation service later, or use live captions without "
                    "translation. No local translation model is selected."
                ),
            ),
        ]

    # ``estimate_pair_vram_mb`` already includes a fixed 1 GiB allowance.
    # Keep a proportional margin as well for the compositor, browser video
    # decode and short-lived inference peaks, which matter most on 8 GiB GPUs.
    capacity = int(hardware.total_mb * 0.85)
    if hardware.free_mb > 0:
        capacity = min(capacity, int(hardware.free_mb * 0.90))

    candidates: list[ModelRecommendation] = []
    for title, stt_model, translation_model_id in _GPU_PAIRS:
        needed = estimate_pair_vram_mb(stt_model, translation_model_id)
        if needed > hardware.total_mb:
            continue
        candidates.append(
            ModelRecommendation(
                title=title,
                stt_model=stt_model,
                translation_model_id=translation_model_id,
                gpu_layers=-1,
                estimated_vram_mb=needed,
                rationale=(
                    f"Estimated shared GPU memory: {needed / 1024:.1f} GiB, including "
                    "Whisper, local translation and 1 GiB of headroom. This is a sizing "
                    "estimate, not a latency benchmark."
                ),
            )
        )

    if not candidates:
        # The >=8 GiB branch always fits the first curated pair, but retain a
        # deterministic fallback if the catalogue estimates change later.
        return recommendations_for_hardware(Hardware(hardware.name, 0, 0))

    fitting = [item for item in candidates if item.estimated_vram_mb <= capacity]
    if not fitting and hardware.free_mb > 0:
        cpu_choices = recommendations_for_hardware(
            Hardware(f"Insufficient free VRAM on {hardware.name}", 0, 0)
        )
        tight = candidates[0]
        return [
            cpu_choices[0],
            ModelRecommendation(
                title=tight.title + " (close GPU apps first)",
                stt_model=tight.stt_model,
                translation_model_id=tight.translation_model_id,
                gpu_layers=tight.gpu_layers,
                estimated_vram_mb=tight.estimated_vram_mb,
                rationale=(
                    tight.rationale
                    + " Current free VRAM is below this estimate; close GPU-heavy apps and rescan first."
                ),
            ),
            cpu_choices[1],
        ]
    recommended_index = len(fitting) - 1 if fitting else 0
    selected: list[ModelRecommendation] = []
    for index in sorted({0, recommended_index, min(len(candidates) - 1, recommended_index + 1)}):
        item = candidates[index]
        rationale = item.rationale
        if item.estimated_vram_mb > capacity:
            rationale += " Current free VRAM is below this estimate; close GPU-heavy apps first."
        selected.append(
            ModelRecommendation(
                title=item.title + (" (recommended)" if index == recommended_index else ""),
                stt_model=item.stt_model,
                translation_model_id=item.translation_model_id,
                gpu_layers=item.gpu_layers,
                estimated_vram_mb=item.estimated_vram_mb,
                rationale=rationale,
                recommended=index == recommended_index,
            )
        )
    return selected


def apply_recommendation(
    config: Mapping[str, Any], recommendation: ModelRecommendation
) -> dict[str, Any]:
    """Apply only defaults; model files remain an explicit user download."""

    updated = copy.deepcopy(dict(config))
    stt = updated.setdefault("stt", {})
    stt["model"] = recommendation.stt_model

    local = updated.setdefault("local_llama", {})
    local["gpu_layers"] = recommendation.gpu_layers
    local["enabled"] = False

    translation = updated.setdefault("translation", {})
    if recommendation.translation_model_id:
        local["model_id"] = recommendation.translation_model_id
        installed = set(local.get("installed_models") or [])
        if recommendation.translation_model_id in installed:
            translation["default_selection"] = {
                "kind": "local",
                "id": recommendation.translation_model_id,
            }
            local.pop("pending_default_model_id", None)
        else:
            # Do not publish an unavailable default to the extension. The
            # local-model controller promotes this selection only after the
            # download has passed its size/hash checks.
            local["pending_default_model_id"] = recommendation.translation_model_id
            translation["default_selection"] = {"kind": "none", "id": "none"}
    else:
        local.pop("pending_default_model_id", None)
        translation["default_selection"] = {"kind": "none", "id": "none"}

    gui = updated.setdefault("gui", {})
    gui["setup_assistant_version"] = SETUP_ASSISTANT_VERSION
    return updated


def mark_setup_assistant_seen(config: Mapping[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(dict(config))
    updated.setdefault("gui", {})["setup_assistant_version"] = SETUP_ASSISTANT_VERSION
    return updated


class SetupAssistantDialog(QtWidgets.QDialog):
    """Small, repeatable setup surface rather than a one-way wizard."""

    def __init__(
        self,
        hardware: Hardware,
        recommendations: Sequence[ModelRecommendation],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Recommended model setup")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._recommendations = list(recommendations)

        root = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel("Choose a model pair for this computer")
        title.setObjectName("pageTitle")
        intro = QtWidgets.QLabel(
            "Live Subtitle sizes speech recognition and local translation together, "
            "because both may share the same GPU during capture."
        )
        intro.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(intro)

        hardware_group = QtWidgets.QGroupBox("Detected hardware")
        hardware_layout = QtWidgets.QVBoxLayout(hardware_group)
        if hardware.total_mb:
            detected = (
                f"{hardware.name} · {hardware.total_mb / 1024:.1f} GiB total · "
                f"{hardware.free_mb / 1024:.1f} GiB currently free"
            )
        else:
            detected = hardware.name
        self.hardware_label = QtWidgets.QLabel(detected)
        self.hardware_label.setWordWrap(True)
        hardware_layout.addWidget(self.hardware_label)
        root.addWidget(hardware_group)

        form = QtWidgets.QFormLayout()
        self.choice = QtWidgets.QComboBox()
        for item in self._recommendations:
            self.choice.addItem(item.title)
        recommended_index = next(
            (index for index, item in enumerate(self._recommendations) if item.recommended),
            0,
        )
        self.choice.setCurrentIndex(recommended_index)
        form.addRow("Setup", self.choice)
        root.addLayout(form)

        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        root.addWidget(self.summary)

        note = QtWidgets.QLabel(
            "Applying saves the defaults only. Downloads do not start automatically. "
            "You can review the files and sizes on STT models and Local translation first. "
            "The translation choice becomes the default after its verified download finishes."
        )
        note.setWordWrap(True)
        root.addWidget(note)

        buttons = QtWidgets.QDialogButtonBox()
        self.apply_button = buttons.addButton(
            "Use this setup", QtWidgets.QDialogButtonBox.AcceptRole
        )
        self.skip_button = buttons.addButton(
            "Not now", QtWidgets.QDialogButtonBox.RejectRole
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.choice.currentIndexChanged.connect(self._refresh_summary)
        self._refresh_summary()

    def selected_recommendation(self) -> ModelRecommendation:
        index = max(0, self.choice.currentIndex())
        return self._recommendations[index]

    def _refresh_summary(self, *_args: object) -> None:
        item = self.selected_recommendation()
        if item.translation_model_id:
            translation_name = get_model(item.translation_model_id).name
        else:
            translation_name = "None (choose a service later)"
        execution = "CPU" if item.gpu_layers == 0 else "GPU"
        self.summary.setText(
            f"Speech recognition: Whisper {item.stt_model}\n"
            f"Translation: {translation_name} · {execution}\n\n"
            f"{item.rationale}"
        )

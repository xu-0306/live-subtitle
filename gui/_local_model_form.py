from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QToolButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


def build_form(parent: QWidget, models: list[dict]) -> dict[str, QWidget]:
    """Build the passive local translation form owned by the main window.

    The builder only creates and lays out widgets.  Runtime behavior, including
    downloads, process control, persistence, and signal connections, belongs to
    the caller that consumes the returned controls.
    """

    layout = QVBoxLayout(parent)
    layout.setContentsMargins(8, 4, 14, 20)
    layout.setSpacing(14)

    overview = QFrame()
    overview.setObjectName("localOverviewCard")
    overview_layout = QVBoxLayout(overview)
    overview_layout.setContentsMargins(18, 16, 18, 16)
    overview_layout.setSpacing(10)

    details = QLabel(
        '<p style="margin:0 0 7px 0;"><b>ON-DEVICE · MANAGED RUNTIME</b></p>'
        '<p style="margin:0;">Download local GGUF models and the built-in llama.cpp runtime here, '
        'then select a model in the browser extension. No provider account or API endpoint is required.</p>'
    )
    details.setObjectName("details")
    details.setTextFormat(Qt.TextFormat.RichText)
    details.setWordWrap(True)
    details.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    hardware = QLabel("Checking hardware...")
    hardware.setObjectName("runtimeNotice")
    hardware.setWordWrap(True)
    hardware.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    info_toggle = QToolButton()
    info_toggle.setText("Model details")
    info_toggle.setCheckable(True)
    info_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    info_toggle.setArrowType(Qt.ArrowType.RightArrow)
    technical = QWidget()
    technical_layout = QVBoxLayout(technical)
    technical_layout.setContentsMargins(0, 0, 0, 0)
    technical_details = QLabel()
    technical_details.setWordWrap(True)
    technical_layout.addWidget(technical_details)
    technical_layout.addWidget(hardware)
    technical.hide()
    def toggle_details(expanded):
        technical.setVisible(expanded)
        info_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
    info_toggle.toggled.connect(toggle_details)
    hardware_warning = QLabel()
    hardware_warning.setWordWrap(True)
    hardware_warning.setObjectName("runtimeNotice")
    hardware_warning.hide()

    model = QComboBox()
    model.setObjectName("model")
    model.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    model.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    for entry in models:
        model.addItem(str(entry["name"]), entry["id"])

    target = QComboBox()
    target.setEditable(True)
    target.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    target.lineEdit().setMaxLength(128)
    target.setToolTip("Enter any language name or code. Suggestions are optional; quality depends on the model.")
    target.setObjectName("target")
    target.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    for label, language in (
        ("Traditional Chinese", "zh-TW"),
        ("English", "en"),
        ("Japanese", "ja"),
    ):
        target.addItem(label, language)

    model_group = QGroupBox("Local model")
    model_group.setObjectName("card")
    model_form = QFormLayout(model_group)
    model_form.setContentsMargins(18, 20, 18, 16)
    model_form.setHorizontalSpacing(14)
    model_form.setVerticalSpacing(10)
    model_form.addRow("Model", model)
    model_form.addRow("Default target language", target)

    add_model = QPushButton("Add Hugging Face GGUF…")
    add_model.setObjectName("add_model")
    add_model.setProperty("kind", "secondary")
    model_form.addRow("", add_model)

    install = QPushButton("Download model")
    install.setObjectName("install")
    install.setEnabled(bool(models))

    cancel = QPushButton("Cancel")
    cancel.setObjectName("cancel")
    cancel.setProperty("kind", "secondary")
    cancel.setEnabled(False)

    stop = QPushButton("Remove from extension choices")
    stop.setObjectName("stop")
    stop.setProperty("kind", "danger")
    stop.setEnabled(False)

    actions = QHBoxLayout()
    actions.addWidget(install)
    actions.addWidget(cancel)
    actions.addWidget(stop)
    actions.addStretch(1)

    progress = QProgressBar()
    progress.setObjectName("progress")
    progress.setRange(0, 100)
    progress.setValue(0)

    status = QLabel("Select a model to download. Captures start and stop it automatically.")
    status.setObjectName("localModelStatus")
    status.setWordWrap(True)
    status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    advanced = QGroupBox("Advanced")
    advanced.setObjectName("card")
    advanced_form = QFormLayout(advanced)
    advanced_form.setContentsMargins(18, 20, 18, 16)
    advanced_form.setHorizontalSpacing(14)
    advanced_form.setVerticalSpacing(10)

    context = QSpinBox()
    context.setObjectName("context")
    context.setRange(512, 8192)
    context.setValue(2048)
    context.setToolTip("Context memory available to the local model during translation.")

    gpu_layers = QSpinBox()
    gpu_layers.setObjectName("gpu_layers")
    gpu_layers.setRange(-1, 999)
    gpu_layers.setValue(-1)
    gpu_layers.setSpecialValueText("Auto")
    gpu_layers.setToolTip(
        "Auto chooses the highest layer count that fits while reserving VRAM for Whisper. "
        "Set 0 for CPU translation, or enter a positive number for an exact manual limit."
    )

    gpu_layers_summary = QLabel(
        "Auto dynamically sizes GPU offload for the selected Whisper model."
    )
    gpu_layers_summary.setObjectName("gpu_layers_summary")
    gpu_layers_summary.setWordWrap(True)
    gpu_layers_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    port = QSpinBox()
    port.setObjectName("port")
    port.setRange(1024, 65535)
    port.setValue(18080)
    port.setToolTip("Loopback service port used by local translation.")

    advanced_form.addRow("Context memory", context)
    advanced_form.addRow("GPU offload", gpu_layers)
    advanced_form.addRow("", gpu_layers_summary)
    advanced_form.addRow("Loopback port", port)

    overview_layout.addWidget(details)
    overview_layout.addWidget(hardware_warning)
    overview_layout.addWidget(info_toggle)
    overview_layout.addWidget(technical)
    layout.addWidget(overview)
    layout.addWidget(model_group)
    layout.addLayout(actions)
    layout.addWidget(progress)
    layout.addWidget(status)
    layout.addWidget(advanced)
    layout.addStretch(1)

    return {
        "add_model": add_model,
        "model": model,
        "details": details,
        "hardware": hardware,
        "hardware_warning": hardware_warning,
        "technical_details": technical_details,
        "info_toggle": info_toggle,
        "technical": technical,
        "target": target,
        "install": install,
        "cancel": cancel,
        "stop": stop,
        "progress": progress,
        "status": status,
        "advanced": advanced,
        "context": context,
        "gpu_layers": gpu_layers,
        "gpu_layers_summary": gpu_layers_summary,
        "port": port,
    }

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
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

    details = QLabel(
        "ON-DEVICE · MANAGED RUNTIME\n"
        "Download local GGUF models and the built-in llama.cpp runtime here, then select a model in the browser extension. "
        "No provider account or API endpoint is required."
    )
    details.setObjectName("details")
    details.setWordWrap(True)
    details.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    hardware = QLabel("Checking hardware...")
    hardware.setObjectName("hardware")
    hardware.setWordWrap(True)
    hardware.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

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
    model_form = QFormLayout(model_group)
    model_form.addRow("Model", model)
    model_form.addRow("Default target language", target)

    add_model = QPushButton("Add Hugging Face GGUF…")
    add_model.setObjectName("add_model")
    model_form.addRow("", add_model)

    install = QPushButton("Download model")
    install.setObjectName("install")
    install.setEnabled(bool(models))

    cancel = QPushButton("Cancel")
    cancel.setObjectName("cancel")
    cancel.setEnabled(False)

    stop = QPushButton("Remove from extension choices")
    stop.setObjectName("stop")
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
    status.setObjectName("status")
    status.setWordWrap(True)
    status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    advanced = QGroupBox("Advanced")
    advanced.setObjectName("advanced")
    advanced_form = QFormLayout(advanced)

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
        "GPU layers to offload; 0 GPU layers keeps inference on the CPU, "
        "while Auto lets the runtime choose."
    )

    port = QSpinBox()
    port.setObjectName("port")
    port.setRange(1024, 65535)
    port.setValue(18080)
    port.setToolTip("Loopback service port used by local translation.")

    advanced_form.addRow("Context memory", context)
    advanced_form.addRow("GPU layers", gpu_layers)
    advanced_form.addRow("Loopback port", port)

    layout.addWidget(details)
    layout.addWidget(hardware)
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
        "target": target,
        "install": install,
        "cancel": cancel,
        "stop": stop,
        "progress": progress,
        "status": status,
        "advanced": advanced,
    }

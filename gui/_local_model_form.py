from __future__ import annotations

from gui.fluent_kit import (
    BusyProgressBar,
    ElidedComboBox,
    disclosure_button,
    make_card,
    muted_label,
)
from gui.wheel_guard import protect_wheel_controls

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FlowLayout,
    FluentIcon,
    PrimaryPushButton,
    PushButton,
    SpinBox,
)

def _form(layout: QVBoxLayout) -> QFormLayout:
    form = QFormLayout()
    form.setHorizontalSpacing(14)
    form.setVerticalSpacing(10)
    form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    layout.addLayout(form)
    return form


def build_form(parent: QWidget, models: list[dict]) -> dict[str, QWidget]:
    """Build the passive local translation form owned by the main window.

    The builder only creates and lays out widgets.  Runtime behavior, including
    downloads, process control, persistence, and signal connections, belongs to
    the caller that consumes the returned controls.
    """

    layout = QVBoxLayout(parent)
    layout.setContentsMargins(8, 4, 14, 20)
    layout.setSpacing(14)

    overview = make_card()
    overview.setObjectName("localOverviewCard")
    overview_layout = overview.layout()

    details = BodyLabel(
        '<p style="margin:0 0 7px 0;"><b>ON-DEVICE · MANAGED RUNTIME</b></p>'
        '<p style="margin:0;">Download local GGUF models and the built-in llama.cpp runtime here, '
        'then select a model in the browser extension. No provider account or API endpoint is required.</p>'
    )
    details.setObjectName("details")
    details.setTextFormat(Qt.TextFormat.RichText)
    details.setWordWrap(True)
    details.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    hardware = muted_label("Checking hardware...", small=True)
    hardware.setObjectName("runtimeNotice")
    hardware.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    technical = QWidget()
    technical_layout = QVBoxLayout(technical)
    technical_layout.setContentsMargins(0, 0, 0, 0)
    technical_layout.setSpacing(8)
    technical_details = CaptionLabel()
    technical_details.setWordWrap(True)
    technical_layout.addWidget(technical_details)
    technical_layout.addWidget(hardware)
    # ``disclosure_button`` hides ``technical`` and toggles it with the button.
    info_toggle = disclosure_button("Model details", technical)

    hardware_warning = CaptionLabel()
    hardware_warning.setWordWrap(True)
    hardware_warning.setObjectName("runtimeNotice")
    hardware_warning.setTextColor(QColor(157, 93, 0), QColor(252, 225, 0))
    hardware_warning.hide()

    model = ElidedComboBox()
    model.setObjectName("model")
    model.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    for entry in models:
        model.addItem(str(entry["name"]), userData=entry["id"])

    model_group = make_card("Local model")
    model_form = _form(model_group.layout())
    model_form.addRow(BodyLabel("Model"), model)

    add_model = PushButton(FluentIcon.ADD, "Add Hugging Face GGUF…")
    add_model.setObjectName("add_model")
    add_model.setProperty("kind", "secondary")
    add_row = QHBoxLayout()
    add_row.addWidget(add_model)
    add_row.addStretch(1)
    model_form.addRow("", add_row)

    install = PrimaryPushButton(FluentIcon.DOWNLOAD, "Download model")
    install.setObjectName("install")
    install.setEnabled(bool(models))

    cancel = PushButton("Cancel")
    cancel.setObjectName("cancel")
    cancel.setProperty("kind", "secondary")
    cancel.setEnabled(False)

    stop = PushButton("Remove from extension choices")
    stop.setObjectName("stop")
    stop.setProperty("kind", "danger")
    stop.setEnabled(False)

    # Wraps onto a second line in the narrowest window instead of clipping.
    actions = FlowLayout()
    actions.setHorizontalSpacing(8)
    actions.setVerticalSpacing(8)
    actions.addWidget(install)
    actions.addWidget(cancel)
    actions.addWidget(stop)

    progress = BusyProgressBar()
    progress.setObjectName("progress")
    progress.setRange(0, 100)
    progress.setValue(0)

    status = BodyLabel("Select a model to download. Captures start and stop it automatically.")
    status.setObjectName("localModelStatus")
    status.setWordWrap(True)
    status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    advanced = make_card("Advanced")
    advanced_grid = QGridLayout()
    advanced_grid.setHorizontalSpacing(14)
    advanced_grid.setVerticalSpacing(10)
    advanced_grid.setColumnStretch(2, 1)
    advanced.layout().addLayout(advanced_grid)

    context = SpinBox()
    context.setObjectName("context")
    context.setRange(512, 8192)
    context.setValue(2048)
    context.setToolTip("Context memory available to the local model during translation.")

    gpu_layers = SpinBox()
    gpu_layers.setObjectName("gpu_layers")
    gpu_layers.setRange(-1, 999)
    gpu_layers.setValue(-1)
    gpu_layers.setSpecialValueText("Auto")
    gpu_layers.setToolTip(
        "Auto chooses the highest layer count that fits while reserving VRAM for Whisper. "
        "Set 0 for CPU translation, or enter a positive number for an exact manual limit."
    )

    gpu_layers_summary = muted_label(
        "Auto dynamically sizes GPU offload for the selected Whisper model.", small=True
    )
    gpu_layers_summary.setObjectName("gpu_layers_summary")
    gpu_layers_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    port = SpinBox()
    port.setObjectName("port")
    port.setRange(1024, 65535)
    port.setValue(18080)
    port.setToolTip("Loopback service port used by local translation.")

    for spin in (context, gpu_layers, port):
        spin.setFixedWidth(160)

    # A grid (not QFormLayout) so the wrapped summary gets its real height.
    advanced_grid.addWidget(BodyLabel("Context memory"), 0, 0)
    advanced_grid.addWidget(context, 0, 1, Qt.AlignLeft)
    advanced_grid.addWidget(BodyLabel("GPU offload"), 1, 0)
    advanced_grid.addWidget(gpu_layers, 1, 1, Qt.AlignLeft)
    advanced_grid.addWidget(gpu_layers_summary, 2, 1, 1, 2)
    advanced_grid.addWidget(BodyLabel("Loopback port"), 3, 0)
    advanced_grid.addWidget(port, 3, 1, Qt.AlignLeft)

    overview_layout.addWidget(details)
    overview_layout.addWidget(hardware_warning)
    overview_layout.addWidget(info_toggle, 0, Qt.AlignLeft)
    overview_layout.addWidget(technical)
    model_group.layout().addLayout(actions)
    model_group.layout().addWidget(progress)
    model_group.layout().addWidget(status)
    layout.addWidget(overview)
    layout.addWidget(model_group)
    layout.addWidget(advanced)
    layout.addStretch(1)

    protect_wheel_controls(parent)
    return {
        "add_model": add_model,
        "model": model,
        "details": details,
        "hardware": hardware,
        "hardware_warning": hardware_warning,
        "technical_details": technical_details,
        "info_toggle": info_toggle,
        "technical": technical,
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

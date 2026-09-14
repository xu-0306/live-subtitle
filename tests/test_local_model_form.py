from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets

from gui._local_model_form import build_form


def _application() -> QtWidgets.QApplication:
    application = QtWidgets.QApplication.instance()
    if application is None:
        application = QtWidgets.QApplication([])
    return application


def test_build_form_exposes_contract_controls_and_initial_values() -> None:
    _application()
    parent = QtWidgets.QWidget()
    models = [
        {"id": "qwen-small", "name": "Qwen small"},
        {"id": "a" * 120, "name": "A very long local translation model name " + "x" * 120},
    ]

    controls = build_form(parent, models)

    assert isinstance(parent.layout(), QtWidgets.QVBoxLayout)
    assert set(controls) == {
        "model",
        "add_model",
        "details",
        "hardware",
        "target",
        "install",
        "cancel",
        "stop",
        "progress",
        "status",
        "advanced",
    }

    model = controls["model"]
    assert isinstance(model, QtWidgets.QComboBox)
    assert [model.itemText(index) for index in range(model.count())] == [
        "Qwen small",
        "A very long local translation model name " + "x" * 120,
    ]
    assert [model.itemData(index) for index in range(model.count())] == [
        "qwen-small",
        "a" * 120,
    ]

    details = controls["details"]
    hardware = controls["hardware"]
    status = controls["status"]
    assert isinstance(details, QtWidgets.QLabel)
    assert details.wordWrap() is True
    assert "download" in details.text().lower()
    assert "local" in details.text().lower()
    assert isinstance(hardware, QtWidgets.QLabel)
    assert hardware.wordWrap() is True
    assert hardware.text() == "Checking hardware..."
    assert isinstance(status, QtWidgets.QLabel)
    assert status.wordWrap() is True
    assert "automatically" in status.text()

    target = controls["target"]
    assert isinstance(target, QtWidgets.QComboBox)
    assert target.isEditable()
    assert [target.itemText(index) for index in range(target.count())] == [
        "Traditional Chinese",
        "English",
        "Japanese",
    ]
    assert [target.itemData(index) for index in range(target.count())] == [
        "zh-TW",
        "en",
        "ja",
    ]

    target.setEditText('Te Reo Māori')
    assert target.currentText() == 'Te Reo Māori'
    assert controls["install"].isEnabled() is True
    assert controls["cancel"].isEnabled() is False
    assert controls["stop"].isEnabled() is False

    progress = controls["progress"]
    assert isinstance(progress, QtWidgets.QProgressBar)
    assert progress.minimum() == 0
    assert progress.maximum() == 100
    assert progress.value() == 0

    advanced = controls["advanced"]
    assert isinstance(advanced, QtWidgets.QGroupBox)
    context = advanced.findChild(QtWidgets.QSpinBox, "context")
    gpu_layers = advanced.findChild(QtWidgets.QSpinBox, "gpu_layers")
    port = advanced.findChild(QtWidgets.QSpinBox, "port")
    assert context is not None
    assert (context.minimum(), context.maximum(), context.value()) == (512, 8192, 2048)
    assert gpu_layers is not None
    assert (gpu_layers.minimum(), gpu_layers.maximum(), gpu_layers.value()) == (-1, 999, -1)
    assert gpu_layers.specialValueText() == "Auto"
    assert port is not None
    assert (port.minimum(), port.maximum(), port.value()) == (1024, 65535, 18080)
    assert "context" in context.toolTip().lower()
    assert "cpu" in gpu_layers.toolTip().lower()
    assert "0" in gpu_layers.toolTip()
    assert "loopback" in port.toolTip().lower()

    # The builder is passive: none of its action buttons has a behavioral slot.
    assert controls["install"].receivers(QtCore.SIGNAL("clicked()")) == 0
    assert controls["cancel"].receivers(QtCore.SIGNAL("clicked()")) == 0
    assert controls["stop"].receivers(QtCore.SIGNAL("clicked()")) == 0

    parent.show()
    assert parent.minimumWidth() < 800
    parent.close()


def test_empty_catalog_keeps_selector_empty_and_disables_install() -> None:
    _application()
    parent = QtWidgets.QWidget()

    controls = build_form(parent, [])

    model = controls["model"]
    assert isinstance(model, QtWidgets.QComboBox)
    assert model.count() == 0
    assert controls["install"].isEnabled() is False
    assert controls["cancel"].isEnabled() is False
    assert controls["stop"].isEnabled() is False

    parent.close()

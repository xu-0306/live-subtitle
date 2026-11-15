"""The former native ``QDialog`` windows are Fluent overlay dialogs (no Windows title bar)."""
from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtTest, QtWidgets

from backend.local_runtime import Hardware
from gui.app import LogsDialog
from gui.fluent_kit import FluentDialog
from gui.local_model_controller import AddModelDialog
from gui.setup_assistant import SetupAssistantDialog, recommendations_for_hardware

ACCEPTED, REJECTED = QtWidgets.QDialog.Accepted, QtWidgets.QDialog.Rejected
HARDWARE = Hardware("RTX 4060", 8192, 7600)


@pytest.fixture
def parent():
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QtWidgets.QWidget()
    window.resize(900, 700)
    window.show()
    yield window
    window.close()


def _exec(dialog, act):
    """exec() the overlay and let ``act(dialog)`` answer it from the event loop; returns the code."""
    state = {}

    def answer() -> None:
        if not dialog.isVisible():
            QtCore.QTimer.singleShot(10, answer)
            return
        act(dialog)

    def watchdog() -> None:  # a dialog that never closes must fail the test, not hang the suite
        if dialog.isVisible():
            state["hung"] = True
            dialog.done(0)

    QtCore.QTimer.singleShot(0, answer)
    QtCore.QTimer.singleShot(5000, watchdog)
    code = dialog.exec()
    assert "hung" not in state, "dialog did not close"
    return code


def _buttons(dialog):
    layout = dialog.buttonLayout
    return {w.text(): w for w in (layout.itemAt(i).widget() for i in range(layout.count())) if w}


def _assert_overlay(dialog, parent) -> None:
    assert isinstance(dialog, FluentDialog)
    # A native QDialog is its own top-level window with a Windows title bar; the overlay is a child.
    assert not dialog.isWindow() and dialog.parent() is parent


def test_setup_assistant_overlay_accepts_the_recommended_pair(parent) -> None:
    dialog = SetupAssistantDialog(HARDWARE, recommendations_for_hardware(HARDWARE), parent)
    _assert_overlay(dialog, parent)
    assert list(_buttons(dialog)) == ["Not now", "Use this setup"]
    assert dialog.selected_recommendation().recommended
    assert _exec(dialog, lambda d: d.apply_button.click()) == ACCEPTED


@pytest.mark.parametrize("act", [
    lambda d: d.skip_button.click(),
    lambda d: QtTest.QTest.keyClick(d, QtCore.Qt.Key_Escape),
], ids=["not-now", "escape"])
def test_setup_assistant_not_now_and_escape_reject(parent, act) -> None:
    dialog = SetupAssistantDialog(HARDWARE, recommendations_for_hardware(HARDWARE), parent)
    assert _exec(dialog, act) == REJECTED


def test_add_model_dialog_returns_the_typed_names(parent) -> None:
    dialog = AddModelDialog(parent)
    _assert_overlay(dialog, parent)
    assert list(_buttons(dialog)) == ["Cancel", "OK"]

    def fill_and_confirm(d) -> None:
        d.repo.setText("owner/repo")
        d.filename.setText("model-Q4_K_M.gguf")
        _buttons(d)["OK"].click()

    assert _exec(dialog, fill_and_confirm) == ACCEPTED
    assert (dialog.repo.text(), dialog.filename.text()) == ("owner/repo", "model-Q4_K_M.gguf")
    assert _exec(AddModelDialog(parent), lambda d: _buttons(d)["Cancel"].click()) == REJECTED


def test_logs_dialog_shows_the_captured_output(parent) -> None:
    dialog = LogsDialog(parent, "one\ntwo")
    _assert_overlay(dialog, parent)
    assert dialog.output.toPlainText() == "one\ntwo" and dialog.output.isReadOnly()
    assert list(_buttons(dialog)) == ["Close"]
    assert _exec(dialog, lambda d: _buttons(d)["Close"].click()) == ACCEPTED


def test_gui_package_has_no_native_dialogs() -> None:
    """New popups must use FluentDialog / fluent_kit.ask, not a native QDialog or QMessageBox call."""
    native = re.compile(r"QtWidgets\.QDialog\b(?!\.)|QtWidgets\.QMessageBox\.(question|warning|information|critical|about)\(")
    offenders = [path.name for path in Path(__file__).resolve().parents[1].joinpath("gui").glob("*.py")
                 if native.search(path.read_text(encoding="utf-8"))]
    assert offenders == []

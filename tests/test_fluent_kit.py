from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtTest, QtWidgets

import pytest

from gui.fluent_kit import BusyProgressBar, ElidedComboBox, FreeTextComboBox, _AskDialog, ask


def _application() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_free_text_combo_keeps_qcombobox_noinsert_semantics() -> None:
    _application()
    combo = FreeTextComboBox()
    combo.addItem("English", userData="en")
    combo.addItem("Japanese", userData="ja")
    seen: list[tuple[str, int]] = []
    combo.currentTextChanged.connect(lambda text: seen.append((text, combo.currentIndex())))

    # Selecting a known label reports its index *before* the text signal fires.
    combo.setCurrentText("Japanese")
    assert seen == [("Japanese", 1)]
    assert combo.itemData(combo.currentIndex()) == "ja"

    # Unknown text is accepted as free-form and has no current item.
    combo.setEditText("Te Reo Māori")
    assert combo.currentText() == "Te Reo Māori"
    assert combo.currentIndex() == -1

    # Enter must not turn typed text into a new list entry.
    combo.setFocus()
    QtTest.QTest.keyClick(combo, QtCore.Qt.Key_Return)
    assert combo.count() == 2
    assert combo.isEditable()


def test_elided_combo_box_does_not_grow_with_long_names() -> None:
    _application()
    combo = ElidedComboBox()
    long_name = "A very long local translation model name " + "x" * 120
    combo.addItem(long_name, userData="long")
    combo.resize(300, 33)
    combo.show()
    assert combo.currentText() == long_name
    assert combo.text() != long_name and combo.text().endswith("…")
    assert combo.toolTip() == long_name
    assert combo.minimumSizeHint().width() < 200
    combo.close()


def test_busy_progress_bar_animates_only_for_an_empty_range() -> None:
    _application()
    bar = BusyProgressBar()
    bar.setRange(0, 0)
    assert bar._busy_ani.state() == QtCore.QAbstractAnimation.Running
    bar.setRange(0, 100)
    bar.setValue(40)
    assert bar._busy_ani.state() == QtCore.QAbstractAnimation.Stopped
    assert (bar.minimum(), bar.maximum(), bar.value()) == (0, 100, 40)
    bar.setRange(0, 0)
    bar.grab()  # the busy paint path renders without error
    bar.setRange(0, 100)


SB = QtWidgets.QMessageBox.StandardButton


def _run_ask(act, buttons, default=None):
    """Open ``ask`` over a real parent and let ``act(dialog, buttons)`` answer it from the event loop."""
    app = _application()
    parent = QtWidgets.QWidget()
    parent.resize(600, 400)
    parent.show()
    seen = {}

    def answer() -> None:
        dialog = next((w for w in parent.findChildren(_AskDialog) if w.isVisible()), None)
        if dialog is None:  # exec() has not shown it yet
            QtCore.QTimer.singleShot(10, answer)
            return
        layout = dialog.buttonLayout
        seen["buttons"] = {w.text(): w for w in (layout.itemAt(i).widget() for i in range(layout.count()))
                           if w is not None}
        seen["default"] = [t for t, w in seen["buttons"].items() if w.isDefault()]
        act(dialog, seen["buttons"])

    def watchdog() -> None:  # a dialog that never closes must fail the test, not hang the suite
        for w in parent.findChildren(_AskDialog):
            if w.isVisible():
                seen["hung"] = True
                w.done(0)

    QtCore.QTimer.singleShot(0, answer)
    QtCore.QTimer.singleShot(5000, watchdog)
    result = ask(parent, "Title", "Body text", buttons, default)
    parent.close()
    assert "hung" not in seen, "ask() dialog did not close"
    return result, seen


@pytest.mark.parametrize("label,expected", [("Save", SB.Save), ("Discard", SB.Discard), ("Cancel", SB.Cancel)])
def test_ask_returns_the_clicked_standard_button(label, expected) -> None:
    result, seen = _run_ask(lambda _d, b: b[label].click(), SB.Save | SB.Discard | SB.Cancel, SB.Cancel)
    assert result == expected
    assert list(seen["buttons"]) == ["Save", "Discard", "Cancel"]
    assert seen["default"] == ["Cancel"]  # Discard/Save are never the Enter default here


@pytest.mark.parametrize("buttons,expected", [
    (SB.Save | SB.Discard | SB.Cancel, SB.Cancel),
    (SB.Yes | SB.No, SB.No),
    (SB.Yes | SB.Cancel, SB.Cancel),
    (SB.Ok, SB.Ok),
])
def test_ask_escape_never_means_go_ahead(buttons, expected) -> None:
    result, _ = _run_ask(lambda d, _b: QtTest.QTest.keyClick(d, QtCore.Qt.Key_Escape), buttons)
    assert result == expected


def test_ask_defaults_to_first_button_when_none_given() -> None:
    result, seen = _run_ask(lambda _d, b: b["Yes"].click(), SB.Yes | SB.No)
    assert result == SB.Yes
    assert seen["default"] == ["Yes"]

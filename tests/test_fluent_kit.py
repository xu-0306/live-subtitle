from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtTest, QtWidgets

from gui.fluent_kit import BusyProgressBar, ElidedComboBox, FreeTextComboBox


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

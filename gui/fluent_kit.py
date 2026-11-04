"""Small Fluent building blocks shared by the local-model form, the service editor and the setup assistant.

These only change presentation.  The native-Qt behaviour the callers rely on
(elided long names, free-form combo text, a busy progress bar) is reproduced
here once instead of in every module.
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt
from qfluentwidgets import (
    BodyLabel, CaptionLabel, ComboBox, EditableComboBox, FluentIcon, ProgressBar,
    SimpleCardWidget, StrongBodyLabel, TransparentPushButton, isDarkTheme,
)


def make_card(title: str = "") -> SimpleCardWidget:
    """Titled card section; mirrors ``MainWindow._card`` so pages stay consistent."""
    card = SimpleCardWidget()
    card.setObjectName("card")
    layout = QtWidgets.QVBoxLayout(card)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(10)
    if title:
        label = StrongBodyLabel(title)
        label.setObjectName("cardTitle")
        layout.addWidget(label)
    return card


def muted_label(text: str = "", *, small: bool = False) -> QtWidgets.QLabel:
    """Wrapped help text. The ``mutedText`` object name is part of the GUI contract."""
    label = (CaptionLabel if small else BodyLabel)(text)
    label.setObjectName("mutedText")
    label.setWordWrap(True)
    return label


def disclosure_button(text: str, target: QtWidgets.QWidget) -> TransparentPushButton:
    """Checkable chevron button that shows or hides ``target`` (hidden initially)."""
    button = TransparentPushButton(FluentIcon.CHEVRON_RIGHT, text)
    button.setCheckable(True)

    def sync(expanded: bool) -> None:
        button.setIcon(FluentIcon.CHEVRON_DOWN_MED if expanded else FluentIcon.CHEVRON_RIGHT)
        target.setVisible(expanded)

    button.toggled.connect(sync)
    target.hide()
    return button


class ElidedComboBox(ComboBox):
    """Fluent ``ComboBox`` that elides a long current item instead of widening its layout."""

    # QSS padding of the Fluent combo box: 11px left, 31px right (arrow).
    _TEXT_INSET = 11 + 31

    def setText(self, text: str) -> None:
        self._full_text = text
        self._elide()

    def _elide(self) -> None:
        full = getattr(self, "_full_text", "")
        shown = self.fontMetrics().elidedText(full, Qt.ElideRight, max(0, self.width() - self._TEXT_INSET))
        QtWidgets.QPushButton.setText(self, shown)
        self.setToolTip(full if shown != full else "")

    def sizeHint(self) -> QtCore.QSize:
        # Independent of the text, otherwise eliding and growing feed each other.
        return QtCore.QSize(220, super().sizeHint().height())

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(120, super().minimumSizeHint().height())

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()


class FreeTextComboBox(EditableComboBox):
    """``EditableComboBox`` with native ``QComboBox`` (NoInsert) semantics for free-form values.

    * Enter never turns typed text into a new entry.
    * ``setEditText``/``setCurrentText``/``isEditable`` exist and accept unknown text.
    * ``currentIndex`` is updated *before* ``currentTextChanged`` fires, as in Qt.
    """

    def isEditable(self) -> bool:
        return True

    def setEditText(self, text: str) -> None:
        self.setText(text)

    def setCurrentText(self, text: str) -> None:
        self.setText(text)

    def _onReturnPressed(self) -> None:
        pass

    def _onComboTextChanged(self, text: str) -> None:
        self._currentIndex = next((i for i, item in enumerate(self.items) if item.text == text), -1)
        self.currentTextChanged.emit(text)
        if self._currentIndex >= 0:
            self.currentIndexChanged.emit(self._currentIndex)


class BusyProgressBar(ProgressBar):
    """Fluent ``ProgressBar`` that animates when its range is empty (Qt's busy state)."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._busy_pos = 0.0
        self._busy_ani = QtCore.QPropertyAnimation(self, b"busyPos", self)
        self._busy_ani.setStartValue(0.0)
        self._busy_ani.setEndValue(1.0)
        self._busy_ani.setDuration(1200)
        self._busy_ani.setLoopCount(-1)

    def getBusyPos(self) -> float:
        return self._busy_pos

    def setBusyPos(self, value: float) -> None:
        self._busy_pos = value
        self.update()

    busyPos = QtCore.Property(float, getBusyPos, setBusyPos)

    def setRange(self, minimum: int, maximum: int) -> None:
        super().setRange(minimum, maximum)
        if minimum >= maximum:
            self._busy_ani.start()
        else:
            self._busy_ani.stop()
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        if self.minimum() < self.maximum():
            super().paintEvent(event)
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHints(QtGui.QPainter.Antialiasing)
        painter.setPen(self.darkBackgroundColor if isDarkTheme() else self.lightBackgroundColor)
        y = self.height() // 2
        painter.drawLine(0, y, self.width(), y)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.barColor())
        length = self.width() * 0.3
        x = (self.width() + length) * self._busy_pos - length
        radius = self.height() / 2
        painter.drawRoundedRect(QtCore.QRectF(x, 0, length, self.height()), radius, radius)

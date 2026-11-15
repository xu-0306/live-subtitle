"""Small Fluent building blocks shared by the local-model form, the service editor and the setup assistant.

These only change presentation.  The native-Qt behaviour the callers rely on
(elided long names, free-form combo text, a busy progress bar) is reproduced
here once instead of in every module.
"""
from __future__ import annotations

from functools import partial
from typing import Callable, Sequence

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt
from qfluentwidgets import (
    BodyLabel, CaptionLabel, ComboBox, EditableComboBox, FluentIcon, MessageBoxBase,
    PrimaryPushButton, ProgressBar, PushButton, SimpleCardWidget, StrongBodyLabel,
    ToolTipFilter, TransparentPushButton, isDarkTheme,
)

_SB = QtWidgets.QMessageBox.StandardButton
# Display order: affirmative first, Cancel last (Fluent dialogs read left to right).
_BUTTON_ORDER = (
    (_SB.Save, "Save"), (_SB.Yes, "Yes"), (_SB.Ok, "OK"),
    (_SB.Discard, "Discard"), (_SB.No, "No"), (_SB.Cancel, "Cancel"),
)


class FluentDialog(MessageBoxBase):
    """Overlay dialog: a mask over ``parent.window()``, no native title bar, caller-chosen buttons.

    Put content in ``viewLayout``. ``parent`` must be a widget of the window to cover (the mask
    is sized from it); ``exec()`` returns the usual ``QDialog`` code, Escape rejects.
    """

    def set_buttons(self, specs: Sequence[tuple[str, bool, Callable[[], None]]]
                    ) -> dict[str, QtWidgets.QPushButton]:
        """Replace the stock OK/Cancel pair. ``specs`` are ``(label, accent, on_click)``, left to right."""
        for old in (self.yesButton, self.cancelButton):
            self.buttonLayout.removeWidget(old)
            # deleteLater() alone is not enough: exec() runs a nested event loop that never
            # processes a delete queued outside it, so the stock button stayed painted (a stray
            # "Cancel" at the corner of the button bar).
            old.hide()
            old.setParent(None)
            old.deleteLater()
        buttons: dict[str, QtWidgets.QPushButton] = {}
        for label, accent, action in specs:
            widget = (PrimaryPushButton if accent else PushButton)(label, self.buttonGroup)
            widget.clicked.connect(lambda _=False, act=action: act())
            self.buttonLayout.addWidget(widget, 1, Qt.AlignVCenter)
            buttons[label] = widget
        return buttons


class _AskDialog(FluentDialog):
    def __init__(self, parent: QtWidgets.QWidget, title: str, text: str,
                 buttons: _SB, default: _SB | None) -> None:
        super().__init__(parent)
        self.chosen = _SB.NoButton
        shown = [(b, label) for b, label in _BUTTON_ORDER if buttons & b]
        flags = [b for b, _ in shown]
        # Escape / close must never mean "go ahead": Cancel, else No, else the only button.
        self.reject_value = next((b for b in (_SB.Cancel, _SB.No) if b in flags), flags[0])
        default = default if default in flags else flags[0]

        self.viewLayout.addWidget(StrongBodyLabel(title))
        body = BodyLabel(text)
        body.setWordWrap(True)
        self.viewLayout.addWidget(body)
        self.widget.setMinimumWidth(380)

        # Only a non-destructive default gets the accent colour.
        widgets = self.set_buttons([
            (label, button == default and button != _SB.Discard, partial(self._choose, button))
            for button, label in shown])
        focus = widgets[dict(shown)[default]]
        focus.setDefault(True)
        focus.setFocus()

    def _choose(self, button: _SB) -> None:
        self.chosen = button
        self.accept()

    def reject(self) -> None:
        self.chosen = self.reject_value
        super().reject()


def ask(parent: QtWidgets.QWidget, title: str, text: str,
        buttons: _SB = _SB.Ok, default: _SB | None = None) -> _SB:
    """Fluent stand-in for ``QMessageBox.question/warning``; returns the same ``StandardButton``.

    ``default`` (first button if omitted) takes focus; Escape returns Cancel, else No.
    """
    dialog = _AskDialog(parent.window(), title, text, buttons, default)
    dialog.exec()
    return dialog.chosen


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


def set_tip(text: str, *widgets: QtWidgets.QWidget) -> None:
    """Fluent hover tip on each widget. The Fluent tip does not wrap, so break lines in ``text``."""
    for widget in widgets:
        widget.setToolTip(text)
        widget.installEventFilter(ToolTipFilter(widget, showDelay=350))


def tip_row(form: QtWidgets.QFormLayout, label: str, field: QtWidgets.QWidget, text: str) -> None:
    """``form.addRow(label, field)`` with the same hover tip on the label and the field."""
    caption = QtWidgets.QLabel(label)
    set_tip(text, caption, field)
    form.addRow(caption, field)


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

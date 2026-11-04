"""Keep scrolling from changing settings until a control has focus."""
from PySide6 import QtCore, QtWidgets


class WheelGuard(QtCore.QObject):
    def eventFilter(self, widget, event):
        if event.type() == QtCore.QEvent.Wheel and not widget.hasFocus():
            parent = widget.parentWidget()
            while parent is not None:
                if isinstance(parent, QtWidgets.QAbstractScrollArea):
                    QtWidgets.QApplication.sendEvent(parent.viewport(), event)
                    break
                parent = parent.parentWidget()
            event.ignore()
            return True
        return super().eventFilter(widget, event)


def protect_wheel_controls(root):
    guard = WheelGuard(root)
    for widget in root.findChildren(QtWidgets.QWidget):
        if isinstance(widget, (QtWidgets.QComboBox, QtWidgets.QSpinBox,
                               QtWidgets.QDoubleSpinBox, QtWidgets.QSlider)):
            widget.setFocusPolicy(QtCore.Qt.StrongFocus)
            widget.installEventFilter(guard)

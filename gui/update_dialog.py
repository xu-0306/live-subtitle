"""Fluent pieces of the update check: the "update available" dialog and the thread-to-window bridge."""
from __future__ import annotations

from functools import partial

from PySide6 import QtCore, QtWidgets
from qfluentwidgets import BodyLabel, StrongBodyLabel, TextBrowser

from gui import fluent_kit
from gui.update_check import Release


class UpdateBridge(QtCore.QObject):
    """Hands a finished lookup from its worker thread to the window; the slot runs on the GUI thread."""

    done = QtCore.Signal(object, bool)  # (update_check.Result, started by the user)


class UpdateDialog(fluent_kit.FluentDialog):
    """Version, release notes and three answers. Escape or closing the dialog means "later"."""

    def __init__(self, parent: QtWidgets.QWidget, release: Release, current: str) -> None:
        super().__init__(parent)
        self.choice = "later"
        self.viewLayout.addWidget(StrongBodyLabel("Update available"))
        intro = BodyLabel(f"Live Subtitle {release.version} is available. You have {current or 'an unknown version'}.")
        intro.setWordWrap(True)
        self.viewLayout.addWidget(intro)
        if release.notes:
            self.notes = TextBrowser()
            # Release notes come from the network: show them as text, never follow a link inside them.
            self.notes.setOpenLinks(False)
            self.notes.setOpenExternalLinks(False)
            self.notes.setMarkdown(release.notes)
            self.notes.setFixedHeight(220)
            self.viewLayout.addWidget(self.notes)
        self.viewLayout.addWidget(fluent_kit.muted_label(
            "Nothing is downloaded or installed for you. The download page has the new Windows ZIP "
            "and the new extension ZIP.", small=True))
        self.widget.setMinimumWidth(480)
        self.buttons = self.set_buttons([
            ("Open download page", True, partial(self._finish, "open")),
            ("Skip this version", False, partial(self._finish, "skip")),
            ("Later", False, partial(self._finish, "later")),
        ])

    def _finish(self, choice: str) -> None:
        self.choice = choice
        self.accept()


def show_update_dialog(parent: QtWidgets.QWidget, release: Release, current: str) -> str:
    """Run the dialog over ``parent``'s window; returns ``"open"``, ``"skip"`` or ``"later"``."""
    dialog = UpdateDialog(parent.window(), release, current)
    dialog.exec()
    return dialog.choice

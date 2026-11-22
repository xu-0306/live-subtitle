"""Fluent pieces of the update flow: the "update available" dialog, the "restart to update" dialog and the
thread-to-window bridge."""
from __future__ import annotations

from functools import partial

from PySide6 import QtCore, QtWidgets
from qfluentwidgets import BodyLabel, StrongBodyLabel, TextBrowser

from gui import fluent_kit, self_update
from gui.update_check import Release


class UpdateBridge(QtCore.QObject):
    """Hands results from worker threads to the window; the slots run on the GUI thread."""

    done = QtCore.Signal(object, bool)  # (update_check.Result, started by the user)
    progress = QtCore.Signal(str, int, int)  # (phase "download"|"verify"|"unpack", done, total)
    staged = QtCore.Signal(str, str)  # (version, "" = ready to install | "cancelled" | an error message)


class UpdateDialog(fluent_kit.FluentDialog):
    """Version, release notes and the answers. Escape or closing the dialog means "later".

    "Update now" is offered only for a packaged install and a release with a verified Windows ZIP;
    otherwise the dialog is the old notify-only one.
    """

    def __init__(self, parent: QtWidgets.QWidget, release: Release, current: str) -> None:
        super().__init__(parent)
        self.choice = "later"
        can_update = self_update.install_dir() is not None and release.asset is not None
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
        if can_update:
            megabytes = max(1, release.asset.size // 2**20)
            hint = (f"Update now downloads the new version here (about {megabytes} MB) and installs it when "
                    "you restart. Your settings and models are kept.")
        else:
            hint = ("Nothing is downloaded or installed for you. The download page has the new Windows ZIP "
                    "and the new extension ZIP.")
        self.viewLayout.addWidget(fluent_kit.muted_label(hint, small=True))
        self.widget.setMinimumWidth(560 if can_update else 480)
        specs = [("Open download page", not can_update, partial(self._finish, "open")),
                 ("Skip this version", False, partial(self._finish, "skip")),
                 ("Later", False, partial(self._finish, "later"))]
        if can_update:
            specs.insert(0, ("Update now", True, partial(self._finish, "update")))
        self.buttons = self.set_buttons(specs)

    def _finish(self, choice: str) -> None:
        self.choice = choice
        self.accept()


def show_update_dialog(parent: QtWidgets.QWidget, release: Release, current: str) -> str:
    """Run the dialog over ``parent``'s window; returns ``"update"``, ``"open"``, ``"skip"`` or ``"later"``."""
    dialog = UpdateDialog(parent.window(), release, current)
    dialog.exec()
    return dialog.choice


class RestartDialog(fluent_kit.FluentDialog):
    """The update is downloaded and unpacked; it takes effect when the app restarts."""

    def __init__(self, parent: QtWidgets.QWidget, version: str) -> None:
        super().__init__(parent)
        self.restart = False
        self.viewLayout.addWidget(StrongBodyLabel("Update ready"))
        body = BodyLabel(f"Live Subtitle {version} has been downloaded. Please restart the app to apply the "
                         "update: it closes and opens again as the new version. A running capture stops. "
                         "Your settings and models are kept.")
        body.setWordWrap(True)
        self.viewLayout.addWidget(body)
        self.widget.setMinimumWidth(440)
        self.buttons = self.set_buttons([
            ("Restart now", True, partial(self._finish, True)),
            ("Later", False, partial(self._finish, False)),
        ])

    def _finish(self, restart: bool) -> None:
        self.restart = restart
        self.accept()


def show_restart_dialog(parent: QtWidgets.QWidget, version: str) -> bool:
    """``True`` when the user chose "Restart now"; Escape means "Later"."""
    dialog = RestartDialog(parent.window(), version)
    dialog.exec()
    return dialog.restart

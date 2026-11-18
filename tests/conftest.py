"""Shared test hygiene.

A closed MainWindow is never freed by itself: each one keeps ~390 widgets and a live timer, and qfluentwidgets
keeps restyling all of them, so every leaked window makes the next one slower to build.  Over a full GUI run the
suite crawled and unrelated tests (the local-model install, an 8 s deadline) timed out.  Deleting the windows when
a test ends keeps every test as fast as it is alone.
"""
import sys

import pytest


@pytest.fixture(autouse=True)
def _free_main_windows():
    yield
    module = sys.modules.get("gui.app")
    if module is None:  # no GUI test has imported the app yet
        return
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        if isinstance(widget, module.MainWindow):
            widget.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)

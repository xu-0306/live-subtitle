"""Executable entry point for both the desktop UI and its backend child.

Frozen builds cannot use ``sys.executable -m backend.server`` because
``sys.executable`` points to the packaged application rather than a Python
interpreter.  The private ``--backend`` switch lets the packaged application
relaunch itself as the backend while keeping normal source launches unchanged.
"""
from __future__ import annotations

import sys


def _restore_backend_stdio() -> None:
    """Attach Python streams to the pipes supplied by ``BackendRunner``.

    PyInstaller's windowed bootloader sets these streams to ``None``.  The
    backend is launched with redirected standard handles, so rebuilding the
    wrappers lets Uvicorn log normally and lets the GUI collect diagnostics.
    """

    for name, descriptor in (("stdout", 1), ("stderr", 2)):
        if getattr(sys, name) is not None:
            continue
        try:
            stream = open(
                descriptor,
                mode="w",
                encoding="utf-8",
                errors="replace",
                buffering=1,
                closefd=False,
            )
        except (OSError, ValueError):
            stream = open(
                "NUL",
                mode="w",
                encoding="utf-8",
                errors="replace",
                buffering=1,
            )
        setattr(sys, name, stream)


def main() -> None:
    if "--backend" in sys.argv:
        sys.argv.remove("--backend")
        _restore_backend_stdio()
        from backend.server import main as backend_main

        backend_main()
        return

    if "--smoke-test-gui" in sys.argv:
        sys.argv.remove("--smoke-test-gui")
        _restore_backend_stdio()
        from PySide6 import QtWidgets

        from gui import config_manager
        from gui.app import MainWindow

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = MainWindow(
            config_path=config_manager.DEFAULT_CONFIG_PATH,
            auto_start=False,
            setup_tray=False,
        )
        window.show()
        app.processEvents()
        window.close()
        app.processEvents()
        print("GUI_SMOKE_OK", flush=True)
        return

    from gui.app import main as gui_main

    gui_main()


if __name__ == "__main__":
    main()

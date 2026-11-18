"""Regenerate the desktop-app screenshots used in README.md.

Run from the repository root:
    python -X utf8 docs/media/render_gui.py [--out DIR] [--pages server,stt]

Uses a throwaway config in a temp folder, so no personal settings, paths or
models appear in the images. The backend is not started.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6 import QtWidgets  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from gui import config_manager  # noqa: E402
from gui.app import MainWindow  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(Path(__file__).parent))
    parser.add_argument("--pages", default="", help="comma-separated nav keys; default all")
    parser.add_argument("--modes", default="light,dark")
    parser.add_argument("--size", default="1100x880")  # tall enough for the Translation services page, which has a defaults card on top
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    width, height = (int(v) for v in args.size.split("x"))
    wanted = {p for p in args.pages.split(",") if p}

    app = QtWidgets.QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ls-readme-"))
    cfg = config_manager.load_default_config()
    cfg["models"] = {"root": str(tmp / "models")}
    path = tmp / "config.yaml"
    config_manager.write_config(path, cfg)

    win = MainWindow(path, auto_start=False, setup_tray=False)
    win.resize(width, height)
    win.show()
    QTest.qWait(800)
    for mode in args.modes.split(","):
        win._set_theme_mode(mode, persist=False)
        QTest.qWait(1200)
        for _label, key in MainWindow.NAV_ITEMS:
            if wanted and key not in wanted:
                continue
            win.navigationInterface.widget(key).click()
            QTest.qWait(600)
            target = out / f"gui-{key}-{mode}.png"
            win.grab().save(str(target))
            print(target)
    win.close()
    app.quit()


if __name__ == "__main__":
    main()

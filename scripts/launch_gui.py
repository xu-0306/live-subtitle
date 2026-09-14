"""Source-checkout GUI launcher; diagnostics never install packages or models."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_MODULES = {
    'PySide6': 'PySide6',
    'yaml': 'PyYAML',
    'httpx': 'httpx',
    'fastapi': 'fastapi',
    'uvicorn': 'uvicorn',
    'whisperlivekit': 'whisperlivekit',
    'torch': 'torch',
    'huggingface_hub': 'huggingface_hub',
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Start Live Subtitle with environment diagnostics.')
    parser.add_argument('--check', action='store_true', help='Check dependencies without opening the GUI or downloading models.')
    args = parser.parse_args(argv)
    os.chdir(PROJECT_ROOT)
    sys.path.insert(0, str(PROJECT_ROOT))
    print('Project:', PROJECT_ROOT, flush=True)
    print('Python: ', sys.executable, flush=True)
    if sys.version_info < (3, 10):
        print('Python 3.10 or newer is required.')
        return 1

    missing = [package for module, package in REQUIRED_MODULES.items()
               if importlib.util.find_spec(module) is None]
    if missing:
        print('Missing dependencies:', ', '.join(missing))
        print('Install into the Python environment shown above:')
        command = [sys.executable, '-m', 'pip', 'install', '-r', str(PROJECT_ROOT / 'gui/requirements.txt'),
                   '-r', str(PROJECT_ROOT / 'backend/requirements.txt')]
        print('  ' + subprocess.list2cmdline(command))
        print('Install the appropriate PyTorch CPU/CUDA build separately; see docs/GUI_STARTUP.md.')
        return 1

    try:
        # Import the real GUI to catch missing Qt DLLs and transitive GUI dependencies.
        from gui.app import main as _gui_main
    except Exception:
        print('The GUI could not be imported. Check the dependency/DLL error below:')
        traceback.print_exc()
        return 1
    if args.check:
        print('GUI import and core package checks passed. Models, GPU inference and backend startup were not tested.')
        return 0

    try:
        from gui.config_manager import get_app_dir
        log_path = get_app_dir() / 'logs' / 'gui-launch.log'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        print('Starting Live Subtitle. Close it through the app; keep this launcher window open.')
        print('GUI and backend log:', log_path, flush=True)
        # Use python.exe so the backend inherits working stdout/stderr handles.
        # One latest-run log avoids an unbounded append-only file.
        with log_path.open('w', encoding='utf-8') as log:
            env = dict(os.environ, PYTHONUTF8='1')
            result = subprocess.run([sys.executable, '-u', '-m', 'gui'], cwd=PROJECT_ROOT,
                                    env=env, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            print(f'GUI exited with code {result.returncode}. Details: {log_path}')
            print(log_path.read_text(encoding='utf-8', errors='replace')[-6000:])
        return result.returncode
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

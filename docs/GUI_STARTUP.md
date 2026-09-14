# Windows GUI startup

Double-click **Start-GUI.bat** in the project root. It works independently of the
current working directory, including paths containing spaces or `&`.

The launcher chooses Python in this order:

1. `STT_PYTHON`, if set to the full path of your preferred `python.exe`.
2. Project `.venv`, project `venv`, or `backend/.venv`.
3. The active `VIRTUAL_ENV`.
4. A working Windows `py -3` launcher, then `python.exe` on PATH.
5. A Python 3.10+ installation under `%LOCALAPPDATA%/Programs/Python/`, when
   neither PATH nor the Python launcher can find it.

It uses the selected environment consistently for the GUI and backend. A local
environment with missing packages is reported rather than silently replaced by
another interpreter. No packages or models are automatically installed.

The launcher console stays open while the GUI runs. Close the app using its GUI;
closing the console can interrupt the processes. Startup errors remain visible.
GUI/backend output is saved to `%APPDATA%/Live Subtitle/logs/gui-launch.log`, replaced on
each launch. A backend failure after the GUI opens is shown in the GUI and this
log; it does not necessarily cause the GUI itself to exit with an error.

## First-time setup

Python and the project dependencies are still required. From the project folder
in PowerShell, create an environment if you do not already have one:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```

Install the PyTorch build appropriate to your hardware using the
[official PyTorch installation selector](https://pytorch.org/get-started/locally/),
with `.\.venv\Scripts\python.exe -m pip` as the installer. Then install the project
dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r gui/requirements.txt -r backend/requirements.txt
.\Start-GUI.bat --check
```

`--check` imports the actual GUI and checks that core backend packages can be
found. It does not start the backend, test CUDA, change the app configuration,
download models, or guarantee that every optional provider is installed.

For an existing environment elsewhere:

```powershell
$env:STT_PYTHON = 'D:\my environment\Scripts\python.exe'
.\Start-GUI.bat
```

See [managed local translation](LOCAL_TRANSLATION_MVP.md) for the model download
flow after the GUI opens. The Chrome extension is loaded separately.

## Packaging assessment

An application bundle is feasible, but the current source entrypoints need a
packaging pass before distributing it to users without Python:

- `gui/backend_runner.py` launches `sys.executable -m backend.server`. In a frozen
  application, `sys.executable` identifies the application executable rather than
  Python. Add an explicit backend executable or an early `--backend` dispatch
  before constructing Qt. See [PyInstaller runtime information](https://pyinstaller.org/en/stable/runtime-information.html).
- Include `backend/config.yaml`, Qt plugins, WhisperLiveKit resources and the
  native libraries used by the selected Whisper/PyTorch build. Verify optional
  imports and provider dependencies in a clean environment.
- Keep downloaded models and llama.cpp runtime outside the application bundle,
  in the existing user-data directory. Validate writable paths, child shutdown
  and operation on a machine with no development Python installed.

The recommended first package is a Windows **one-folder portable app** containing
an executable, followed by an installer around that folder once verified.
PyInstaller documents that this layout is easier to inspect and debug than the
single-file layout: [operating modes](https://pyinstaller.org/en/stable/operating-mode.html#bundling-to-one-folder).
This launcher change does not claim to produce or validate such a bundle.

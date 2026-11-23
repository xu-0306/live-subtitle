import os
import shutil
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


# Live Subtitle ships the desktop controller and its backend in one folder.
ROOT = Path(SPECPATH)

whisper_datas = collect_data_files("whisperlivekit")
whisper_binaries = collect_dynamic_libs("whisperlivekit")
whisper_hiddenimports = collect_submodules(
    "whisperlivekit",
    filter=lambda name: ".tests" not in name and not name.endswith(".test"),
)

# WhisperLiveKit decodes the extension's WebM stream by running a plain "ffmpeg", so
# the release must carry one or a machine without FFmpeg fails with "FFmpeg state: FAILED".
# backend/ffmpeg_path.py puts this folder on PATH at startup.
ffmpeg_exe = os.environ.get("LIVE_SUBTITLE_FFMPEG") or shutil.which("ffmpeg")
if not ffmpeg_exe or not Path(ffmpeg_exe).is_file():
    raise SystemExit("ffmpeg.exe not found: put it on PATH or set LIVE_SUBTITLE_FFMPEG before building.")

# The extension's files (not its tests) ride along: Settings > Chrome extension (gui/extension_sync.py) copies them
# into the folder Chrome loaded, and manifest.json is also the app's version source for gui/update_check.py.
extension_dir = ROOT / "chrome-extension"
extension_datas = [
    (str(path), str(Path("chrome-extension") / path.relative_to(extension_dir).parent))
    for path in sorted(extension_dir.rglob("*"))
    if path.is_file() and not {"tests", "__pycache__"} & set(path.relative_to(extension_dir).parts)
]

datas = [
    (ffmpeg_exe, "ffmpeg"),
    (str(ROOT / "ffmpeg-NOTICE.txt"), "ffmpeg"),
    (str(ROOT / "backend" / "config.yaml"), "backend"),
    (str(ROOT / "backend" / "local_models.json"), "backend"),
    *extension_datas,
    *whisper_datas,
    *copy_metadata("whisperlivekit"),
]

a = Analysis(
    [str(ROOT / "gui" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=whisper_binaries,
    datas=datas,
    hiddenimports=whisper_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Keep bundled torch out of the PYZ so an external CUDA wheel takes precedence.
    module_collection_mode={"torch": "py", "functorch": "py", "torchgen": "py"},
    excludes=[
        "IPython",
        "jupyter",
        "matplotlib",
        "notebook",
        "pytest",
        "tensorflow",
        "torchaudio",
        "torch._dynamo",
        "torch._inductor",
    ],
    noarchive=False,
    optimize=0,
)

# A developer tool may prepend its own native runtimes to PATH.  Never ship
# DLLs discovered from Codex's bundled Poppler/libheif directories; generic
# names such as icuuc.dll can shadow the matching Qt runtime at application
# startup.
a.binaries = [
    entry
    for entry in a.binaries
    if "codex-runtimes" not in str(entry[1]).lower()
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="live-subtitle",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="live-subtitle",
)

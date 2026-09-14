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

datas = [
    (str(ROOT / "backend" / "config.yaml"), "backend"),
    (str(ROOT / "backend" / "local_models.json"), "backend"),
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
    excludes=[
        "IPython",
        "jupyter",
        "matplotlib",
        "notebook",
        "pytest",
        "tensorflow",
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

"""Locate the FFmpeg that WhisperLiveKit shells out to.

WhisperLiveKit decodes the extension's WebM/Opus stream by starting a plain
``ffmpeg`` process, so it works only when ``ffmpeg`` is on PATH.  The Windows
release ships its own copy under ``ffmpeg/`` next to the bundled libraries;
``ensure_ffmpeg_on_path`` puts that folder first so a fresh machine needs no
separate FFmpeg install.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

_EXE_NAME = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"

MISSING_MESSAGE = (
    "FFmpeg was not found, so captured audio cannot be decoded. Reinstall Live Subtitle "
    "(antivirus software may have removed ffmpeg.exe from the app folder), or install "
    "FFmpeg and add it to PATH, then restart the app."
)


def bundled_ffmpeg() -> Optional[Path]:
    """Path of the FFmpeg shipped inside a frozen build, if any."""

    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    exe = Path(base) / "ffmpeg" / _EXE_NAME
    return exe if exe.is_file() else None


def ensure_ffmpeg_on_path() -> Optional[str]:
    """Prefer the bundled FFmpeg, then return whichever ``ffmpeg`` PATH resolves."""

    bundled = bundled_ffmpeg()
    if bundled is not None:
        os.environ["PATH"] = str(bundled.parent) + os.pathsep + os.environ.get("PATH", "")
    return shutil.which("ffmpeg")


def missing_message(pcm_input: bool) -> Optional[str]:
    """User-facing error when decoding is needed but FFmpeg cannot be started."""

    if pcm_input or shutil.which("ffmpeg"):
        return None
    return MISSING_MESSAGE

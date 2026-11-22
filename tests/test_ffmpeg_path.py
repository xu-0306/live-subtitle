"""WhisperLiveKit runs a plain ``ffmpeg``; the release must supply one and say so when it is gone."""
import asyncio
import json
import os
import sys
from pathlib import Path

from backend import ffmpeg_path, server

EXE = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"


def _fake_bundle(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "ffmpeg"
    folder.mkdir()
    exe = folder / EXE
    exe.write_bytes(b"")
    os.chmod(exe, 0o755)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    return folder


def test_bundled_ffmpeg_goes_first_on_path(tmp_path, monkeypatch):
    folder = _fake_bundle(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    found = ffmpeg_path.ensure_ffmpeg_on_path()
    assert os.environ["PATH"].split(os.pathsep)[0] == str(folder)
    assert Path(found) == folder / EXE


def test_without_a_bundle_path_is_untouched(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert ffmpeg_path.ensure_ffmpeg_on_path() is None
    assert os.environ["PATH"] == str(tmp_path)


def test_missing_ffmpeg_is_only_an_error_when_decoding_is_needed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert ffmpeg_path.missing_message(False) == ffmpeg_path.MISSING_MESSAGE
    assert ffmpeg_path.missing_message(True) is None


def test_the_release_bundles_and_checks_ffmpeg():
    root = Path(__file__).resolve().parents[1]
    spec = (root / "live-subtitle.spec").read_text(encoding="utf-8")
    assert '(ffmpeg_exe, "ffmpeg")' in spec and '"ffmpeg-NOTICE.txt"' in spec
    assert (root / "ffmpeg-NOTICE.txt").is_file()
    build = (root / "build-release.ps1").read_text(encoding="utf-8")
    assert "_internal\\ffmpeg\\ffmpeg.exe" in build


class _Socket:
    def __init__(self):
        self.sent, self.closed = [], []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000):
        self.closed.append(code)


def test_session_without_ffmpeg_reports_once_and_closes_without_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    socket = _Socket()
    asyncio.run(server._run_selected_session(socket, {"model": "tiny"}, {}, None))
    assert socket.sent == [{"type": "error", "message": ffmpeg_path.MISSING_MESSAGE}]
    # 1008 is the one close code the extension never retries.
    assert socket.closed == [1008]

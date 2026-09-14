"""Managed llama.cpp downloads and process ownership, without Qt or torch imports."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sysconfig
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .local_catalog import Artifact, ModelSpec, RUNTIMES, RUNTIME_VERSION
from .process_ownership import ChildJob

Progress = Callable[[str, int, int], None]


class Cancelled(RuntimeError):
    pass


def check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled('Cancelled. Completed files and resumable downloads were kept.')


def digest(path: Path, cancel: threading.Event) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            check_cancel(cancel)
            h.update(chunk)
    return h.hexdigest()


@contextlib.contextmanager
def install_lock(root: Path):
    """OS-owned advisory lock: released by process death, never stale PID locks."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.install.lock').open('a+b') as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another model operation is using this folder. Try again when it finishes.') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class _DownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != 'https':
            raise RuntimeError('Download redirected to an insecure URL.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_artifact(artifact: Artifact, folder: Path, cancel: threading.Event,
                      progress: Progress, *, opener=None) -> Path:
    """Immutable Range resume with size/hash verification before atomic publication.

    Caller holds install_lock. Tests may inject a local HTTP opener.
    """
    check_cancel(cancel)
    if Path(artifact.filename).name != artifact.filename or '\\' in artifact.filename:
        raise ValueError('Artifact filename must be a basename.')
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / artifact.filename
    part = folder / (artifact.filename + '.part')
    if target.exists() and target.stat().st_size == artifact.size:
        progress('Verifying ' + artifact.filename, 0, 0)
        if digest(target, cancel) == artifact.sha256:
            return target
    if part.exists() and part.stat().st_size > artifact.size:
        part.unlink()
    offset = part.stat().st_size if part.exists() else 0
    if shutil.disk_usage(folder).free < artifact.size - offset + 64 * 1024 * 1024:
        raise RuntimeError('Not enough free disk space for this download.')
    if offset < artifact.size:
        headers = {'User-Agent': 'Live-Subtitle', 'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        request = urllib.request.Request(artifact.url, headers=headers)
        if opener is None:
            if urllib.parse.urlsplit(artifact.url).scheme != 'https':
                raise ValueError('Catalog downloads require HTTPS.')
            opener = urllib.request.build_opener(_DownloadRedirect()).open
        with opener(request, timeout=5) as response:
            status = response.status
            if status == 206:
                expected = f'bytes {offset}-{artifact.size - 1}/{artifact.size}'
                if response.headers.get('Content-Range') != expected:
                    raise RuntimeError('Unexpected download range. Retry after removing the partial file.')
            elif status == 200:
                offset = 0  # Server ignored Range: restart, never append a full response.
            else:
                raise RuntimeError(f'Download HTTP status {status}')
            with part.open('ab' if offset else 'wb') as out:
                while True:
                    check_cancel(cancel)
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    if offset + len(chunk) > artifact.size:
                        raise RuntimeError('Download exceeded the catalog size.')
                    out.write(chunk)
                    offset += len(chunk)
                    progress(artifact.filename, offset, artifact.size)
                out.flush()
                os.fsync(out.fileno())
    check_cancel(cancel)
    progress('Verifying ' + artifact.filename, 0, 0)
    if not part.exists() or part.stat().st_size != artifact.size:
        raise RuntimeError('Download interrupted. Retry to resume.')
    if digest(part, cancel) != artifact.sha256:
        part.unlink()
        raise RuntimeError('Download checksum mismatch. Please retry.')
    check_cancel(cancel)
    part.replace(target)
    # Cancellation may race with the atomic rename. Keep the verified artifact,
    # but do not let this operation report success or continue toward activation.
    check_cancel(cancel)
    return target


def _remove_stage(stage: Path, root: Path) -> None:
    if stage.resolve().parent != root.resolve() or not stage.name.startswith('.staging-'):
        raise ValueError('Invalid staging cleanup path')
    shutil.rmtree(stage)


def extract_runtime(archives: list[Path], destination: Path, cancel: threading.Event) -> Path:
    """Reject traversal/symlinks; build a versioned directory before publishing it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / ('.staging-' + uuid.uuid4().hex)
    stage.mkdir()
    try:
        seen = set()
        unpacked = 0
        for archive in archives:
            with zipfile.ZipFile(archive) as zf:
                for item in zf.infolist():
                    check_cancel(cancel)
                    relative = Path(item.filename.replace('\\', '/'))
                    target = stage / relative
                    if (relative.is_absolute() or '..' in relative.parts or ':' in item.filename
                            or (item.external_attr >> 16) & 0o170000 == 0o120000
                            or not target.resolve().is_relative_to(stage.resolve())):
                        raise RuntimeError('Runtime archive contains an unsafe path.')
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    key = relative.as_posix().casefold()
                    if key in seen:
                        raise RuntimeError('Runtime archives contain conflicting files.')
                    seen.add(key)
                    unpacked += item.file_size
                    if unpacked > 3 * 1024**3 or shutil.disk_usage(stage).free < item.file_size + 64 * 1024**2:
                        raise RuntimeError('Not enough disk space to extract runtime.')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(item) as src, target.open('wb') as dst:
                        while chunk := src.read(1024 * 1024):
                            check_cancel(cancel)
                            dst.write(chunk)
        matches = list(stage.rglob('llama-server.exe'))
        if len(matches) != 1:
            raise RuntimeError('Runtime archive must contain one llama-server.exe.')
        executable = matches[0].relative_to(stage).as_posix()
        manifest = {'executable': executable, 'files': {
            f.relative_to(stage).as_posix(): digest(f, cancel) for f in stage.rglob('*') if f.is_file()
        }}
        (stage / 'installed.json').write_text(json.dumps(manifest), encoding='utf-8')
        check_cancel(cancel)
        if destination.exists():
            # Preserve an older installation; never replace a possibly running directory.
            raise RuntimeError('Runtime folder exists but is invalid. Choose a new managed folder or remove it after stopping the app.')
        stage.replace(destination)
        return destination / executable
    finally:
        if stage.exists():
            _remove_stage(stage, destination.parent)


def installed_runtime(folder: Path, cancel: threading.Event) -> Path | None:
    marker = folder / 'installed.json'
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding='utf-8'))
        for name, expected in data['files'].items():
            path = folder / name
            if not path.resolve().is_relative_to(folder.resolve()) or not path.is_file() or digest(path, cancel) != expected:
                return None
        executable = folder / data['executable']
        if executable.resolve().is_relative_to(folder.resolve()) and executable.is_file() and data['executable'] in data['files']:
            return executable
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


@dataclass(frozen=True)
class Hardware:
    name: str
    total_mb: int
    free_mb: int


def detect_hardware() -> Hardware:
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,memory.free',
                                 '--format=csv,noheader,nounits'], capture_output=True, text=True,
                                timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        row = result.stdout.splitlines()[0].rsplit(',', 2)
        return Hardware(row[0].strip(), int(row[1]), int(row[2]))
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return Hardware('No supported NVIDIA GPU detected', 0, 0)


def preflight(model: ModelSpec, hardware: Hardware, settings: dict, stt: dict) -> str:
    if int(settings.get('gpu_layers', -1)) == 0:
        return 'CPU translation selected. Real-time performance depends on your CPU.'
    if hardware.total_mb < 8 * 1024:
        raise RuntimeError('Local GPU mode targets at least 8 GB VRAM. Select CPU (GPU layers = 0), or use an external translation service.')
    reserve = {'tiny': 768, 'base': 1024, 'small': 2048, 'medium': 3500, 'large-v3': 5500}.get(str(stt.get('model', 'medium')), 5500)
    needed = model.estimated_vram_mb + reserve + 1024
    if hardware.free_mb < needed:
        raise RuntimeError(f'Estimated shared workload needs {needed / 1024:.1f} GiB free; {hardware.free_mb / 1024:.1f} GiB available. Choose Compact, a smaller Whisper model, CPU translation, or an external service. Estimates include Whisper headroom.')
    return f'{hardware.name}: {hardware.free_mb / 1024:.1f} GiB free. Shared-workload estimate: {needed / 1024:.1f} GiB; not a benchmark.'


def prepare_install(root: Path, model: ModelSpec, kind: str, cancel: threading.Event,
                    progress: Progress, *, allow_download: bool = True) -> tuple[Path, Path]:
    if platform.system() != 'Windows' or (platform.machine() or sysconfig.get_platform()).lower() not in ('amd64', 'x86_64', 'win-amd64'):
        raise RuntimeError('Managed runtime MVP supports Windows x64. Use an external compatible service on this platform.')
    with install_lock(root):
        runtime_dir = root / 'runtimes' / RUNTIME_VERSION / kind
        progress('Checking runtime', 0, 0)
        executable = installed_runtime(runtime_dir, cancel)
        if executable is None:
            if not allow_download:
                raise RuntimeError('Runtime is not installed. Click Download and enable.')
            archives = [download_artifact(a, root / 'downloads', cancel, progress) for a in RUNTIMES[kind]]
            progress('Extracting runtime', 0, 0)
            executable = extract_runtime(archives, runtime_dir, cancel)
        paths = []
        for artifact in model.files:
            folder = root / 'models' / model.id
            if not allow_download:
                path = folder / artifact.filename
                if not path.is_file() or path.stat().st_size != artifact.size or digest(path, cancel) != artifact.sha256:
                    raise RuntimeError('Model is not installed or is incomplete. Click Download and enable.')
                paths.append(path)
            else:
                paths.append(download_artifact(artifact, folder, cancel, progress))
        return executable, paths[0]


class ManagedRuntime:
    """A single owner holds its Popen handle; no global process discovery/killing."""
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._log = None
        self._job = None
        self.token = 'stt_' + secrets.token_urlsafe(32)
        self.base_url = ''

    def alive(self) -> bool:
        with self._lock:
            return self.process is not None and self.process.poll() is None

    def start(self, executable: Path, model_path: Path, model_id: str, settings: dict,
              cancel: threading.Event, log_path: Path, progress: Progress,
              *, timeout: float = 180, command_prefix: list[str] | None = None) -> dict:
        import httpx
        from .translator import SYSTEM_TRANSLATION_PROMPT, _build_translation_prompt

        port = int(settings.get('port', 18080))
        context = int(settings.get('context', 2048))
        layers = int(settings.get('gpu_layers', -1))
        if not 1024 <= port <= 65535 or not 512 <= context <= 8192 or not -1 <= layers <= 999:
            raise ValueError('Invalid local runtime settings.')
        check_cancel(cancel)
        with self._lock:
            if self.alive():
                raise RuntimeError('Stop the current local translation model first.')
            with socket.socket() as probe:
                try:
                    if os.name == 'nt':
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    probe.bind(('127.0.0.1', port))
                except OSError as exc:
                    raise RuntimeError('Local translation port is in use. Choose another port in Advanced.') from exc
            self.base_url = f'http://127.0.0.1:{port}'
            self.token = 'stt_' + secrets.token_urlsafe(32)
            args = (command_prefix or [str(executable)]) + [
                '--model', str(model_path), '--alias', model_id, '--host', '127.0.0.1', '--port', str(port),
                '--ctx-size', str(context), '--parallel', '1', '--n-gpu-layers', '99' if layers == -1 else str(layers),
                '--batch-size', '256', '--ubatch-size', '128', '--api-key', self.token,
            ]
            if settings.get('disable_thinking'):
                args += ['--jinja', '--reasoning', 'off']
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = log_path.open('wb')
            try:
                self.process = subprocess.Popen(args, cwd=str(executable.parent), stdin=subprocess.DEVNULL,
                                                stdout=self._log, stderr=subprocess.STDOUT,
                                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self._job = ChildJob(self.process)
            except Exception:
                if self.process is not None:
                    self.process.kill()
                    self.process.wait(timeout=3)
                    self.process = None
                self._log.close()
                self._log = None
                raise
        try:
            deadline = time.monotonic() + timeout
            progress('Starting local translation', 0, 0)
            with httpx.Client(trust_env=False, headers={'Authorization': f'Bearer {self.token}'}) as client:
                while True:
                    check_cancel(cancel)
                    if not self.alive():
                        raise RuntimeError(f'Local runtime exited. Check {log_path}; the GPU driver or model may be incompatible.')
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Local runtime startup timed out.')
                    try:
                        response = client.get(self.base_url + '/health', timeout=1)
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    cancel.wait(0.15)
                progress('Testing translation', 0, 0)
                started = time.monotonic()
                response = client.post(self.base_url + '/v1/chat/completions', json={
                    'model': model_id, 'messages': [
                        {'role': 'system', 'content': SYSTEM_TRANSLATION_PROMPT},
                        {'role': 'user', 'content': _build_translation_prompt(
                            'Hello, this is a subtitle test.', 'en', settings.get('target_language', 'zh-TW'))},
                    ], 'max_tokens': 64, 'temperature': 0, 'stream': False,
                }, timeout=min(timeout, 60))
                check_cancel(cancel)
                response.raise_for_status()
                payload = response.json()
                content = payload['choices'][0]['message']['content']
                if not isinstance(content, str) or not content.strip() or not self.alive():
                    raise RuntimeError('Local translation warmup did not produce text.')
                return {'base_url': self.base_url, 'api_key': self.token, 'model': model_id,
                        'warmup_ms': round((time.monotonic() - started) * 1000)}
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        with self._lock:
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
                self.process = None
            if self._job is not None:
                self._job.close()
                self._job = None
            if self._log:
                self._log.close()
                self._log = None

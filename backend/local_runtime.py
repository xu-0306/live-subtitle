"""Managed llama.cpp downloads and process ownership, without Qt or torch imports."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
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
    vendor: str = ''  # 'nvidia', 'amd', 'intel', or '' when unknown / none


VULKAN_VENDORS = {0x1002: 'amd', 0x8086: 'intel'}  # PCI vendor ids


def uses_vulkan(hardware: Hardware) -> bool:
    """AMD/Intel GPUs run the Vulkan llama.cpp build; speech recognition stays on the CPU."""

    return hardware.vendor in VULKAN_VENDORS.values()


def runtime_kind(gpu_layers: int, hardware: Hardware) -> str:
    if int(gpu_layers) == 0:
        return 'windows-cpu'
    return 'windows-vulkan' if uses_vulkan(hardware) else 'windows-cuda'


STT_VRAM_RESERVE_MB = {
    'tiny': 768,
    'base': 1024,
    'small': 2048,
    'medium': 3500,
    'large-v3': 5500,
}
SHARED_VRAM_HEADROOM_MB = 1024


def stt_vram_reserve_mb(stt: dict) -> int:
    """Return the shared-GPU allowance for an STT configuration."""

    return STT_VRAM_RESERVE_MB.get(
        str(stt.get('model', 'medium')),
        STT_VRAM_RESERVE_MB['large-v3'],
    )


def auto_gpu_fit_target_mb(stt: dict, hardware: Hardware | None = None) -> int:
    """VRAM llama.cpp must leave available for Whisper and shared peaks.

    Whisper has no GPU path on AMD/Intel, so a Vulkan GPU only keeps the shared headroom.
    """

    whisper = 0 if hardware is not None and uses_vulkan(hardware) else stt_vram_reserve_mb(stt)
    return whisper + SHARED_VRAM_HEADROOM_MB


def gpu_offload_args(settings: dict) -> list[str]:
    """Translate the persisted GPU-layer contract into llama.cpp arguments.

    ``-1`` is the product's Auto mode.  The bundled llama.cpp runtime can read
    arbitrary GGUF architecture metadata and choose the largest fitting layer
    count itself, which is more general than maintaining per-model layer maps.
    """

    layers = int(settings.get('gpu_layers', -1))
    if not -1 <= layers <= 999:
        raise ValueError('Invalid GPU layer setting.')
    device = settings.get('device')
    if device is not None and not re.fullmatch(r'Vulkan\d{1,2}', str(device)):
        raise ValueError('Invalid GPU device.')
    pinned = ['--device', str(device)] if device and layers != 0 else []
    return pinned + _layer_args(settings, layers)


def _layer_args(settings: dict, layers: int) -> list[str]:
    if layers == -1:
        fit_target = int(
            settings.get(
                'gpu_fit_target_mb',
                STT_VRAM_RESERVE_MB['medium'] + SHARED_VRAM_HEADROOM_MB,
            )
        )
        if fit_target < SHARED_VRAM_HEADROOM_MB:
            raise ValueError('Invalid automatic GPU memory reserve.')
        return [
            '--n-gpu-layers', 'auto',
            '--fit', 'on',
            '--fit-target', str(fit_target),
        ]
    return ['--n-gpu-layers', str(layers), '--fit', 'off']


BATCH_SIZES = (64, 128, 256, 512, 1024, 2048)


def cpu_tuning_args(settings: dict) -> list[str]:
    """llama-server CPU and batch flags; the defaults reproduce the historic fixed command line."""

    threads = int(settings.get('threads', -1))
    poll = int(settings.get('poll', 50))
    priority = int(settings.get('priority', 0))
    batch = int(settings.get('batch_size', 256))
    if (not (threads == -1 or 1 <= threads <= 64) or poll not in (0, 50)
            or priority not in (0, -1) or batch not in BATCH_SIZES):
        raise ValueError('Invalid local runtime settings.')
    args = ['--threads', str(threads), '--threads-batch', str(threads)] if threads != -1 else []
    if poll == 0:
        args += ['--poll', '0']
    if priority == -1:
        args += ['--prio', '-1']
    return args + ['--batch-size', str(batch), '--ubatch-size', str(batch // 2)]


# llama-server with every layer on the CPU still held ~253 MB of VRAM (CUDA context and
# buffers) on an RTX 3080; whatever is above this is model weights on the GPU.
VRAM_BASELINE_MB = 300
# The Vulkan build held 67 MB with no layers on the GPU (RTX 3080, --device pinned).
VULKAN_VRAM_BASELINE_MB = 100


def process_vram_mb(pid: int) -> int | None:
    """Dedicated VRAM of one process from the Windows "GPU Process Memory" counter, or None.

    llama-server logs its layer placement only at debug verbosity, which also grows the log
    ~4x per request, so the placement is read from the process's memory instead.
    """

    if os.name != 'nt':
        return None
    return _counter_mb(f'\\GPU Process Memory(pid_{int(pid)}_*)\\Dedicated Usage')


def _counter_mb(path: str) -> int | None:
    """Sum of a Windows GPU memory performance counter in MiB (works for every GPU vendor)."""

    if os.name != 'nt':
        return None
    query = (f"(Get-Counter '{path}')"
             ".CounterSamples | Measure-Object CookedValue -Sum | ForEach-Object Sum")
    try:
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', query],
                                capture_output=True, text=True, timeout=15,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return round(float(result.stdout.strip()) / 1024 ** 2) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def classify_placement(vram_mb: float, model_mb: float, baseline_mb: int = VRAM_BASELINE_MB) -> dict:
    """Where the weights sit, judged by how much of the model file is resident in VRAM."""

    # ponytail: VRAM heuristic, not a layer count; parse "offloaded N/M layers" if llama.cpp
    # ever reports it at the default log level or through its API.
    share = max(0.0, vram_mb - baseline_mb) / max(model_mb, 1.0)
    state = 'gpu' if share >= 0.85 else 'cpu' if share <= 0.15 else 'partial'
    return {'state': state, 'vram_mb': round(vram_mb)}


def detect_hardware() -> Hardware:
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total,memory.free',
                                 '--format=csv,noheader,nounits'], capture_output=True, text=True,
                                timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        detected = []
        for line in result.stdout.splitlines():
            row = line.rsplit(',', 2)
            if len(row) == 3:
                detected.append(Hardware(row[0].strip(), int(row[1]), int(row[2]), 'nvidia'))
        if detected:
            # The managed CUDA runtime can use an NVIDIA GPU; recommend against
            # the card with the largest capacity and prefer current free memory
            # as the tie breaker on multi-GPU systems.
            return max(detected, key=lambda item: (item.total_mb, item.free_mb))
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    try:
        adapters = _dxgi_adapters()
    except Exception:  # a broken graphics driver must not stop the app; fall back to CPU
        adapters = []
    # Integrated GPUs (about 128 MiB dedicated memory) are not offered GPU translation.
    # ponytail: integrated GPUs could use shared memory through Vulkan; add when tested.
    usable = [item for item in adapters if item[1] in VULKAN_VENDORS and item[2] >= 1024]
    if usable:
        name, vendor_id, total, luid = max(usable, key=lambda item: item[2])
        used = _counter_mb(f'\\GPU Adapter Memory({luid}_phys_*)\\Dedicated Usage')
        # Without the counter assume it is all free; llama.cpp --fit still checks the real budget.
        free = total if used is None else max(0, total - used)
        return Hardware(name, total, free, VULKAN_VENDORS[vendor_id])
    return Hardware('No supported GPU detected', 0, 0)


def vulkan_device(executable: Path, gpu_name: str, run=subprocess.run) -> str | None:
    """The llama.cpp Vulkan device id ("Vulkan1") of the detected GPU, matched by name.

    Without --device the Vulkan build splits layers across every GPU it sees, including an
    integrated one listed first (measured: Intel UHD 770 + RTX 3080 ran at 21 prompt tok/s).
    """

    try:
        result = run([str(executable), '--list-devices'], capture_output=True, text=True,
                     timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (result.stdout or '').splitlines():
        found = re.match(r'\s*(Vulkan\d{1,2}): (.+?) \(\d+ MiB', line)
        if found and found[2].strip().casefold() == gpu_name.strip().casefold():
            return found[1]
    return None


def _dxgi_adapters() -> list[tuple[str, int, int, str]]:
    """(name, PCI vendor id, dedicated MiB, counter LUID prefix) per hardware GPU, from DXGI."""

    if os.name != 'nt':
        return []
    import ctypes
    from ctypes import wintypes

    class LUID(ctypes.Structure):
        _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]

    class AdapterDesc1(ctypes.Structure):
        _fields_ = [('Description', ctypes.c_wchar * 128), ('VendorId', wintypes.UINT),
                    ('DeviceId', wintypes.UINT), ('SubSysId', wintypes.UINT),
                    ('Revision', wintypes.UINT), ('DedicatedVideoMemory', ctypes.c_size_t),
                    ('DedicatedSystemMemory', ctypes.c_size_t),
                    ('SharedSystemMemory', ctypes.c_size_t), ('AdapterLuid', LUID),
                    ('Flags', wintypes.UINT)]

    class GUID(ctypes.Structure):
        _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                    ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

    def method(obj, index, *argtypes):
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])

    iid_factory1 = GUID(0x770AAE78, 0xF26F, 0x4DBA,
                        (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
    factory = ctypes.c_void_p()
    if ctypes.WinDLL('dxgi').CreateDXGIFactory1(ctypes.byref(iid_factory1), ctypes.byref(factory)):
        return []
    adapters = []
    try:
        for index in range(16):
            adapter = ctypes.c_void_p()
            # IDXGIFactory1::EnumAdapters1 (vtable 12) fails with DXGI_ERROR_NOT_FOUND at the end.
            if method(factory, 12, wintypes.UINT, ctypes.c_void_p)(factory, index, ctypes.byref(adapter)):
                break
            desc = AdapterDesc1()
            try:
                ok = method(adapter, 10, ctypes.c_void_p)(adapter, ctypes.byref(desc)) == 0  # GetDesc1
            finally:
                method(adapter, 2)(adapter)  # Release
            if ok and not desc.Flags & 2:  # skip DXGI_ADAPTER_FLAG_SOFTWARE
                luid = (f'luid_0x{desc.AdapterLuid.HighPart & 0xFFFFFFFF:08x}'
                        f'_0x{desc.AdapterLuid.LowPart:08x}')
                adapters.append((desc.Description, desc.VendorId,
                                 desc.DedicatedVideoMemory // 1024 ** 2, luid))
    finally:
        method(factory, 2)(factory)
    return adapters


def preflight(model: ModelSpec, hardware: Hardware, settings: dict, stt: dict) -> str:
    gpu_layers = int(settings.get('gpu_layers', -1))
    if gpu_layers == 0:
        return 'CPU translation selected. Real-time performance depends on your CPU.'
    if hardware.total_mb <= 0:
        raise RuntimeError(
            'No supported GPU was detected. Select CPU (GPU layers = 0), '
            'or use an external translation service.'
        )
    shared_reserve = auto_gpu_fit_target_mb(stt, hardware)
    if uses_vulkan(hardware):
        kept_for, smaller = 'shared buffers (speech recognition runs on the CPU)', ''
    else:
        kept_for, smaller = 'Whisper and shared buffers', 'choose a smaller Whisper model, '

    if gpu_layers == -1:
        if hardware.free_mb < shared_reserve:
            raise RuntimeError(
                f'Automatic GPU offload needs to keep about {shared_reserve / 1024:.1f} GiB '
                f'free for {kept_for}; {hardware.free_mb / 1024:.1f} GiB '
                f'is currently available. Close GPU-heavy applications, {smaller}'
                'or select CPU translation.'
            )
        return (
            f'{hardware.name}: {hardware.free_mb / 1024:.1f} GiB free. '
            'Auto lets llama.cpp select the highest fitting GPU layer count while '
            f'keeping {shared_reserve / 1024:.1f} GiB available for {kept_for}.'
        )
    # The catalog estimate describes a full model offload. An explicit finite
    # --n-gpu-layers value is a partial offload whose actual VRAM use depends on
    # GGUF architecture metadata; charging it the full estimate rejects safe
    # configurations such as two layers on a 10 GiB card. Reserve enough room
    # for Whisper and shared buffers here and let llama.cpp validate the chosen
    # partial layer count when it loads the model.
    if gpu_layers > 0:
        needed = shared_reserve
        if hardware.free_mb < needed:
            raise RuntimeError(
                f'Keeping {kept_for} free needs about {needed / 1024:.1f} GiB; '
                f'{hardware.free_mb / 1024:.1f} GiB available. Reduce GPU layers, '
                f'{smaller}or use CPU translation.'
            )
        return (
            f'{hardware.name}: {hardware.free_mb / 1024:.1f} GiB free. '
            f'Partial translation offload: {gpu_layers} layer(s); '
            f'{needed / 1024:.1f} GiB reserved for {kept_for}.'
        )

    raise ValueError('GPU layers must be Auto (-1), CPU (0), or a positive layer count.')


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
                '--ctx-size', str(context), '--parallel', '1',
            ] + gpu_offload_args(settings) + cpu_tuning_args(settings) + ['--api-key', self.token]
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
                warmup_ms = round((time.monotonic() - started) * 1000)
                if layers == 0:
                    placement = {'state': 'cpu', 'vram_mb': 0}
                else:
                    vram = process_vram_mb(self.process.pid)
                    baseline = (VULKAN_VRAM_BASELINE_MB if settings.get('device')
                                or Path(executable).parent.name == 'windows-vulkan' else VRAM_BASELINE_MB)
                    placement = None if vram is None else classify_placement(
                        vram, model_path.stat().st_size / 1024 ** 2, baseline)
                return {
                    'base_url': self.base_url,
                    'api_key': self.token,
                    'model': model_id,
                    'warmup_ms': warmup_ms,
                    'gpu_layers': 'auto' if layers == -1 else layers,
                    'placement': placement,
                    **(
                        {'gpu_fit_target_mb': int(settings['gpu_fit_target_mb'])}
                        if layers == -1 and settings.get('gpu_fit_target_mb') is not None
                        else {}
                    ),
                }
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

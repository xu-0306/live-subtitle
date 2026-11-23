"""Download, verify and activate the optional Windows CUDA torch wheel."""
from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PureWindowsPath
from threading import Event

from .local_catalog import Artifact
from .local_runtime import Progress, check_cancel, download_artifact, install_lock

TORCH_VERSION = '2.10.0'
PACKS = {
    'cu126': Artifact(
        f'torch-{TORCH_VERSION}+cu126-cp312-cp312-win_amd64.whl',
        f'https://download.pytorch.org/whl/cu126/torch-{TORCH_VERSION}%2Bcu126-cp312-cp312-win_amd64.whl',
        2589881452, '95d8409b8a15191de4c2958e86ca47f3ea8f9739b994ee4ca0e7586f37336413',
    ),
    'cu128': Artifact(
        f'torch-{TORCH_VERSION}+cu128-cp312-cp312-win_amd64.whl',
        f'https://download.pytorch.org/whl/cu128/torch-{TORCH_VERSION}%2Bcu128-cp312-cp312-win_amd64.whl',
        2867409626, 'fbde8f6a9ec8c76979a0d14df21c10b9e5cab6f0d106a73ca73e2179bc597cae',
    ),
}


def _driver_version(driver: str) -> tuple[int, ...]:
    if not re.fullmatch(r'\d+(?:\.\d+)+', driver):
        raise ValueError('Invalid NVIDIA driver version.')
    return tuple(map(int, driver.split('.')))


def detect_nvidia(run=subprocess.run) -> dict | None:
    try:
        result = run(
            ['nvidia-smi', '--query-gpu=name,compute_cap,driver_version', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=10, check=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        name, capability, driver = (value.strip() for value in next(csv.reader(result.stdout.splitlines())))
        capability = float(capability)
        _driver_version(driver)
        if name and math.isfinite(capability):
            return {'name': name, 'compute_cap': capability, 'driver': driver}
    except (OSError, subprocess.SubprocessError, StopIteration, ValueError, csv.Error):
        pass
    return None


def choose_pack(gpu: dict | None) -> tuple[str | None, str]:
    if not gpu:
        return None, 'No NVIDIA GPU found'
    driver = _driver_version(gpu['driver'])
    capability = gpu['compute_cap']
    if driver < (560, 76):
        return None, 'Update the NVIDIA driver to 560.76 or newer to install the GPU speech pack.'
    if capability >= 10.0 and driver < (572, 61):
        return None, 'Update the NVIDIA driver to 572.61 or newer to use this GPU for speech recognition.'
    if capability < 5.0:
        return None, 'This GPU is too old for CUDA speech recognition'
    if capability >= 7.5 and driver >= (572, 61):
        return 'cu128', 'This GPU supports CUDA 12.8.'
    return 'cu126', 'This GPU supports CUDA 12.6.'


def pack_root() -> Path:
    override = os.getenv('LIVE_SUBTITLE_GPU_PACK_DIR')
    if override:
        return Path(override)
    return Path(os.getenv('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'Live Subtitle' / 'gpu-pack'


def _created_root() -> Path:
    # Create before resolving: a redirected or linked folder (e.g. MSIX LocalCache) resolves
    # differently once it exists, which would fail the _inside() checks.
    root = pack_root()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _folder_name(tag: str) -> str:
    return f'torch-{TORCH_VERSION}-{tag}'


def _inside(root: Path, path: Path) -> Path:
    relative = path.relative_to(root)
    if path == root or path.resolve() != root / relative:
        raise ValueError(f'Unsafe GPU pack path: {path.name}')
    return path


def _rmtree(root: Path, path: Path) -> None:
    _inside(root, path)
    if path.exists():
        shutil.rmtree(path)


def _installed(root: Path) -> dict | None:
    unverified = None
    for tag in PACKS:
        folder = root / _folder_name(tag)
        try:
            manifest = _inside(root, folder / 'pack.json')
            data = json.loads(manifest.read_text(encoding='utf-8'))
            if (not isinstance(data, dict) or data.get('torch_version') != TORCH_VERSION
                    or data.get('tag') != tag or not isinstance(data.get('verified'), bool)):
                continue
            data['path'] = folder
            if data['verified']:
                return data
            unverified = data
        except (OSError, ValueError, RuntimeError):
            continue
    return unverified


def installed() -> dict | None:
    try:
        return _installed(pack_root().resolve())
    except (OSError, ValueError, RuntimeError):
        return None


def _write_manifest(folder: Path, data: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream)
        temporary.replace(folder / 'pack.json')
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _extract(wheel: Path, folder: Path, cancel: Event, progress: Progress) -> None:
    with zipfile.ZipFile(wheel) as archive:
        files = []
        for member in archive.infolist():
            check_cancel(cancel)
            path = PureWindowsPath(member.filename)
            parts = path.parts
            if path.anchor or not parts or any(
                ':' in part or part.endswith(('.', ' ')) for part in parts
            ):
                raise ValueError(f'Unsafe path in GPU pack wheel: {member.filename}')
            if not member.is_dir() and not parts[0].endswith('.data'):
                files.append((member, folder.joinpath(*parts)))
        progress('Unpacking GPU speech pack', 0, len(files))
        for count, (member, target) in enumerate(files, 1):
            check_cancel(cancel)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open('xb') as output:
                while chunk := source.read(1024 * 1024):
                    check_cancel(cancel)
                    output.write(chunk)
            progress('Unpacking GPU speech pack', count, len(files))


def install(tag: str, cancel: Event, progress: Progress, opener=None) -> Path:
    artifact = PACKS[tag]
    root = _created_root()
    with install_lock(root):
        folder = _inside(root, root / _folder_name(tag))
        downloads = _inside(root, root / 'downloads')
        partial = _inside(root, root / f'.partial-{tag}')
        if _installed(root) is not None:
            raise RuntimeError('A GPU speech pack is already installed.')
        for filename in (artifact.filename, artifact.filename + '.part'):
            _inside(root, downloads / filename)
        wheel = download_artifact(artifact, downloads, cancel, progress, opener=opener)
        if shutil.disk_usage(root).free < 2 * artifact.size:
            raise RuntimeError('Not enough free disk space to unpack the GPU speech pack.')
        _rmtree(root, partial)
        partial.mkdir()
        published = False
        try:
            _extract(wheel, partial, cancel, progress)
            check_cancel(cancel)
            _rmtree(root, folder)
            check_cancel(cancel)
            partial.rename(folder)
            published = True
            check_cancel(cancel)
            _write_manifest(folder, {
                'torch_version': TORCH_VERSION, 'tag': tag, 'sha256': artifact.sha256,
                'verified': False, 'reason': 'CUDA has not been checked yet.',
            })
            check_cancel(cancel)
        except Exception:
            _rmtree(root, folder if published else partial)
            raise
        wheel.unlink()
    return folder


def activate(force: bool = False) -> Path | None:
    if not (force or getattr(sys, 'frozen', False)) or 'LIVE_SUBTITLE_NO_GPU_PACK' in os.environ:
        return None
    pack = installed()
    if pack is not None and (force or pack['verified']):
        path = pack['path']
        sys.path.insert(0, str(path))
        return path
    return None


def _check_result(command_prefix) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [*command_prefix], capture_output=True, encoding='utf-8', errors='replace', timeout=180,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except subprocess.TimeoutExpired:
        return False, 'The CUDA check timed out.'
    except OSError:
        return False, 'The CUDA check could not start.'
    if result.returncode != 0:
        return False, f'The CUDA check exited with code {result.returncode}.'
    for line in result.stdout.splitlines():
        if line.startswith('GPU_PACK '):
            try:
                data = json.loads(line.removeprefix('GPU_PACK '))
                if isinstance(data, dict) and isinstance(data.get('ok'), bool) and isinstance(data.get('reason'), str):
                    return data['ok'], data['reason']
            except ValueError:
                pass
    return False, 'The CUDA check did not report a result.'


def verify(command_prefix) -> dict:
    root = _created_root()
    with install_lock(root):
        pack = _installed(root)
        if pack is None:
            return {'verified': False, 'reason': 'No GPU speech pack is installed.'}
        folder = pack.pop('path')
        pack['verified'], pack['reason'] = _check_result(command_prefix)
        _write_manifest(folder, pack)
        return pack


def check_main() -> None:
    result = {'ok': False, 'reason': '', 'cuda': None, 'device': None}
    try:
        folder = activate(force=True)
        if folder is None:
            raise RuntimeError('The GPU speech pack is missing or disabled.')
        import torch

        if not Path(torch.__file__).resolve().is_relative_to(folder):
            raise RuntimeError('Torch did not load from the GPU speech pack.')
        if not torch.__version__.startswith(TORCH_VERSION):
            raise RuntimeError('The GPU speech pack has the wrong torch version.')
        result['cuda'] = torch.version.cuda
        if not result['cuda'] or not torch.cuda.is_available():
            raise RuntimeError('Torch could not start CUDA.')
        if float((torch.ones(4, device='cuda') * 2).sum()) != 8.0:
            raise RuntimeError('The CUDA test returned the wrong result.')
        result.update(ok=True, device=torch.cuda.get_device_name(0))
    except Exception as exc:
        result.update(ok=False, reason=' '.join(str(exc).split())[:300] or type(exc).__name__)
    print('GPU_PACK ' + json.dumps(result), flush=True)


def remove() -> None:
    root = pack_root().resolve()
    if not root.exists():
        return
    with install_lock(root):
        for path in [*(root / _folder_name(tag) for tag in PACKS), *root.glob('.partial-*'), root / 'downloads']:
            _rmtree(root, path)


if __name__ == '__main__' and '--check' in sys.argv:
    check_main()

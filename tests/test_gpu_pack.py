"""GPU sharing plan, phase 3: downloadable CUDA torch pack."""
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import zipfile
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest

from backend import gpu_pack
from backend.local_catalog import Artifact
from backend.local_runtime import Cancelled


@pytest.mark.parametrize('gpu, tag', [
    ({'compute_cap': 8.6, 'driver': '616.56'}, 'cu128'),     # RTX 30, new driver
    ({'compute_cap': 8.6, 'driver': '572.61'}, 'cu128'),
    ({'compute_cap': 8.6, 'driver': '572.60'}, 'cu126'),     # RTX 30, older driver
    ({'compute_cap': 7.0, 'driver': '616.56'}, 'cu126'),     # Volta
    ({'compute_cap': 6.1, 'driver': '600.10'}, 'cu126'),     # GTX 10
    ({'compute_cap': 12.0, 'driver': '572.61'}, 'cu128'),    # RTX 50
    ({'compute_cap': 12.0, 'driver': '570.00'}, None),       # RTX 50 needs 572.61
    ({'compute_cap': 8.6, 'driver': '560.75'}, None),        # driver too old
    ({'compute_cap': 3.7, 'driver': '600.00'}, None),
    (None, None),
])
def test_pack_choice_follows_the_driver_and_gpu_table(gpu, tag):
    chosen, reason = gpu_pack.choose_pack(gpu)
    assert chosen == tag and reason


def test_old_driver_reason_names_the_minimum():
    assert '560.76' in gpu_pack.choose_pack({'compute_cap': 8.6, 'driver': '552.22'})[1]
    assert '572.61' in gpu_pack.choose_pack({'compute_cap': 12.0, 'driver': '566.03'})[1]


def test_detect_nvidia_parses_the_first_gpu_and_survives_missing_tools():
    out = 'NVIDIA GeForce RTX 3080, 8.6, 616.56\nNVIDIA GeForce RTX 3090, 8.6, 616.56\n'
    run = lambda *a, **k: SimpleNamespace(stdout=out)
    assert gpu_pack.detect_nvidia(run) == {'name': 'NVIDIA GeForce RTX 3080', 'compute_cap': 8.6, 'driver': '616.56'}

    def missing(*a, **k):
        raise FileNotFoundError('nvidia-smi')
    assert gpu_pack.detect_nvidia(missing) is None
    assert gpu_pack.detect_nvidia(lambda *a, **k: SimpleNamespace(stdout='garbage')) is None


def fake_wheel(extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('torch/__init__.py', '__version__ = "2.10.0+cu128"\n')
        archive.writestr('torch/lib/fake.dll', b'x' * 10)
        archive.writestr('torch-2.10.0+cu128.dist-info/METADATA', 'Name: torch\nVersion: 2.10.0+cu128\n')
        archive.writestr('torch-2.10.0+cu128.data/scripts/x.exe', b'skip me')
        for name in extra or ():
            archive.writestr(name, b'bad')
    return buffer.getvalue()


@pytest.fixture
def pack_dir(tmp_path, monkeypatch):
    root = tmp_path / 'gpu-pack'
    monkeypatch.setenv('LIVE_SUBTITLE_GPU_PACK_DIR', str(root))
    monkeypatch.delenv('LIVE_SUBTITLE_NO_GPU_PACK', raising=False)
    return root


def stage(root, monkeypatch, payload):
    """Put the 'downloaded' wheel in place; download_artifact accepts it by size + sha256."""
    artifact = Artifact('torch-fake+cu128.whl', 'https://example.invalid/torch.whl', len(payload),
                        hashlib.sha256(payload).hexdigest())
    monkeypatch.setitem(gpu_pack.PACKS, 'cu128', artifact)
    (root / 'downloads').mkdir(parents=True, exist_ok=True)
    (root / 'downloads' / artifact.filename).write_bytes(payload)
    return artifact


def test_install_activate_verify_remove(pack_dir, monkeypatch):
    stage(pack_dir, monkeypatch, fake_wheel())
    folder = gpu_pack.install('cu128', threading.Event(), lambda *a: None)
    assert folder == (pack_dir / 'torch-2.10.0-cu128').resolve()
    assert (folder / 'torch' / '__init__.py').is_file() and (folder / 'torch' / 'lib' / 'fake.dll').is_file()
    assert not any(p.name.endswith('.data') for p in folder.iterdir())
    assert not list((pack_dir / 'downloads').glob('*.whl'))     # wheel deleted after unpacking
    pack = gpu_pack.installed()
    assert pack['verified'] is False and pack['tag'] == 'cu128'

    monkeypatch.setattr(sys, 'path', list(sys.path))
    assert gpu_pack.activate() is None                            # not frozen, not forced
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert gpu_pack.activate() is None                            # frozen but not verified
    ok = [sys.executable, '-c', 'print("GPU_PACK " + __import__("json").dumps({"ok": True, "reason": ""}))']
    assert gpu_pack.verify(ok)['verified'] is True
    assert gpu_pack.activate() == folder and sys.path[0] == str(folder)
    monkeypatch.setenv('LIVE_SUBTITLE_NO_GPU_PACK', '1')
    assert gpu_pack.activate() is None                            # escape hatch

    gpu_pack.remove()
    assert gpu_pack.installed() is None and not folder.exists()


@pytest.mark.parametrize('command, reason', [
    ([sys.executable, '-c', 'import sys; sys.exit(3)'], 'exited with code 3'),
    ([sys.executable, '-c', 'print("hello")'], 'did not report'),
    ([sys.executable, '-c', 'print("GPU_PACK " + __import__("json").dumps({"ok": False, "reason": "no CUDA"}))'],
     'no CUDA'),
])
def test_failed_checks_leave_the_pack_unverified(pack_dir, monkeypatch, command, reason):
    stage(pack_dir, monkeypatch, fake_wheel())
    gpu_pack.install('cu128', threading.Event(), lambda *a: None)
    result = gpu_pack.verify(command)
    assert result['verified'] is False and reason in result['reason']
    assert gpu_pack.installed()['reason'] == result['reason']


@pytest.mark.parametrize('bad', ['../evil.py', 'C:/evil.py', 'torch/../../evil.py'])
def test_unsafe_wheel_paths_are_refused(pack_dir, monkeypatch, bad):
    stage(pack_dir, monkeypatch, fake_wheel([bad]))
    with pytest.raises(ValueError):
        gpu_pack.install('cu128', threading.Event(), lambda *a: None)
    assert gpu_pack.installed() is None
    assert not (pack_dir.parent / 'evil.py').exists()
    assert not (pack_dir / 'torch-2.10.0-cu128').exists()


def test_cancelled_unpack_leaves_no_pack_but_keeps_the_download(pack_dir, monkeypatch):
    artifact = stage(pack_dir, monkeypatch, fake_wheel())
    cancel = threading.Event()

    def progress(text, done, total):
        if text.startswith('Unpacking') and done:
            cancel.set()
    with pytest.raises(Cancelled):
        gpu_pack.install('cu128', cancel, progress)
    assert gpu_pack.installed() is None
    assert not (pack_dir / 'torch-2.10.0-cu128').exists()
    assert (pack_dir / 'downloads' / artifact.filename).exists()   # no re-download on retry
    assert gpu_pack.install('cu128', threading.Event(), lambda *a: None).is_dir()


def test_other_torch_versions_and_unfinished_folders_are_ignored(pack_dir):
    (pack_dir / 'torch-2.10.0-cu128').mkdir(parents=True)          # no pack.json = unfinished
    assert gpu_pack.installed() is None
    (pack_dir / 'torch-2.10.0-cu128' / 'pack.json').write_text(json.dumps(
        {'torch_version': '2.9.0', 'tag': 'cu128', 'verified': True}), encoding='utf-8')
    assert gpu_pack.installed() is None


def test_check_child_reports_missing_pack(pack_dir):
    env = {**os.environ, 'LIVE_SUBTITLE_GPU_PACK_DIR': str(pack_dir)}
    out = subprocess.run([sys.executable, '-m', 'backend.gpu_pack', '--check'], capture_output=True, text=True,
                         env=env, timeout=120, cwd=os.path.dirname(os.path.dirname(__file__))).stdout
    line = next(l for l in out.splitlines() if l.startswith('GPU_PACK '))
    data = json.loads(line.partition(' ')[2])
    assert data['ok'] is False and 'missing' in data['reason']


# --- GUI card ---

@pytest.fixture(scope='module')
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def make_card(saved, config):
    from gui.gpu_pack_card import GpuPackCard
    return GpuPackCard(get_config=lambda: config, save_config=lambda c: saved.append(c) or True,
                       get_runner=lambda: None, stop_backend=lambda: None, start_backend=lambda: None,
                       notice=lambda *a: None)


def visible_buttons(card):
    return [b.text() for b in card._buttons if not b.isHidden()]


def test_card_states(app, pack_dir, monkeypatch):
    card = make_card([], {})
    assert card.status.text() == 'Not needed when running from source.' and visible_buttons(card) == []
    card._gpu = {'name': 'NVIDIA GeForce RTX 3080', 'compute_cap': 8.6, 'driver': '616.56'}
    card._refresh()
    assert card.status.text().startswith('Runs speech recognition on NVIDIA GeForce RTX 3080 with CUDA 12.8. 2.9 GB')
    assert visible_buttons(card) == ['Install']
    card._gpu = {'name': 'Old', 'compute_cap': 8.6, 'driver': '552.22'}
    card._refresh()
    assert '560.76' in card.status.text() and visible_buttons(card) == []

    card._gpu = {'name': 'RTX', 'compute_cap': 8.6, 'driver': '616.56'}
    stage(pack_dir, monkeypatch, fake_wheel())
    gpu_pack.install('cu128', threading.Event(), lambda *a: None)
    card._refresh()
    assert 'CUDA did not start' in card.status.text() and visible_buttons(card) == ['Check again', 'Remove']
    gpu_pack.verify([sys.executable, '-c', 'print("GPU_PACK {\\"ok\\": true, \\"reason\\": \\"\\"}")'])
    card._refresh()
    assert card.status.text().startswith('Installed · CUDA 12.8') and visible_buttons(card) == ['Remove']
    card.deleteLater()


def test_policy_switch_and_revert_only_when_the_pack_set_it(app):
    saved = []
    config = {'stt': {'backend_policy': 'localagreement'}}
    card = make_card(saved, config)
    assert card._save_policy(installed=True)
    assert saved[-1]['stt'] == {'backend_policy': 'simulstreaming', 'policy_set_by_gpu_pack': True}
    card._get_config = lambda: saved[-1]
    assert card._save_policy(installed=False)
    assert saved[-1]['stt'] == {'backend_policy': 'localagreement'}
    # A user who chose SimulStreaming themselves keeps it after removing the pack.
    saved.clear()
    card._get_config = lambda: {'stt': {'backend_policy': 'simulstreaming'}}
    assert card._save_policy(installed=True) and card._save_policy(installed=False) and saved == []
    card.deleteLater()

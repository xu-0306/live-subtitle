"""GPU sharing plan, phase 1: llama CPU flags, placement, SimulStreaming without CUDA torch."""
import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
import pytest
from PySide6 import QtWidgets

from backend import local_runtime as lr
from backend import server
from backend.local_runtime import Hardware
from gui import local_model_controller as lc


def test_default_tuning_reproduces_the_historic_command_line():
    assert lr.cpu_tuning_args({}) == ['--batch-size', '256', '--ubatch-size', '128']


def test_every_tuning_option_maps_to_its_llama_flag():
    args = lr.cpu_tuning_args({'threads': 4, 'poll': 0, 'priority': -1, 'batch_size': 512})
    assert args == ['--threads', '4', '--threads-batch', '4', '--poll', '0', '--prio', '-1',
                    '--batch-size', '512', '--ubatch-size', '256']


@pytest.mark.parametrize('bad', [{'threads': 0}, {'threads': 65}, {'poll': 10}, {'priority': 1},
                                 {'batch_size': 300}])
def test_invalid_tuning_is_refused(bad):
    with pytest.raises(ValueError):
        lr.cpu_tuning_args(bad)


@pytest.mark.parametrize('vram, state', [(1529, 'gpu'), (1077, 'partial'), (253, 'cpu')])
def test_placement_from_measured_vram_of_a_1221_mb_model(vram, state):
    # Values measured on an RTX 3080 with Qwen3.5 2B Q4_K_M: auto, 12 layers, 0 layers.
    assert lr.classify_placement(vram, 1221) == {'state': state, 'vram_mb': vram}


def test_vram_probe_gives_none_for_a_process_without_gpu_memory():
    assert lr.process_vram_mb(os.getpid()) in (None, 0)


class _FakeStorage:
    def __init__(self, data):
        self.data, self.device = data, 'cuda'

    def to_device(self, device):
        moved = _FakeStorage(self.data)
        moved.device = str(device)
        return moved

    def __array__(self, dtype=None, copy=None):
        assert self.device != 'cuda', 'GPU storage must be copied to the host first'
        return np.asarray(self.data, dtype=dtype)


def test_simulstreaming_encoder_output_reaches_a_cpu_only_torch(monkeypatch):
    import torch
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
    encoder = SimpleNamespace(encode=lambda mel: _FakeStorage(np.ones((1, 2), dtype=np.float16)))

    class FakeEngine:
        def __init__(self, **kwargs):
            self.asr = SimpleNamespace(fw_encoder=encoder)

    monkeypatch.setattr(server, 'TranscriptionEngine', FakeEngine)
    engine = asyncio.run(server._construct_stt_engine({'backend_policy': 'simulstreaming'}))
    out = engine.asr.fw_encoder.encode(None)
    assert isinstance(out, np.ndarray) and out.dtype == np.float32
    assert torch.as_tensor(out).shape == (1, 2)


def test_localagreement_engine_is_left_alone(monkeypatch):
    import torch
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
    original = object()

    class FakeEngine:
        def __init__(self, **kwargs):
            self.asr = SimpleNamespace(fw_encoder=SimpleNamespace(encode=original))

    monkeypatch.setattr(server, 'TranscriptionEngine', FakeEngine)
    engine = asyncio.run(server._construct_stt_engine({'backend_policy': 'localagreement'}))
    assert engine.asr.fw_encoder.encode is original


def test_stt_devices_for_both_policies():
    fw = SimpleNamespace(model=SimpleNamespace(device='cuda'))
    simul = SimpleNamespace(asr=SimpleNamespace(shared_model=SimpleNamespace(device='cpu'), fw_encoder=fw))
    assert server._stt_devices(simul, 'simulstreaming') == {'encoder': 'cuda', 'decoder': 'cpu'}
    la = SimpleNamespace(asr=SimpleNamespace(model=SimpleNamespace(model=SimpleNamespace(device='cuda'))))
    assert server._stt_devices(la, 'localagreement') == {'encoder': 'cuda', 'decoder': 'cuda'}
    assert server._stt_devices(SimpleNamespace(asr=None), 'localagreement') is None


@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def controller(app, tmp_path, monkeypatch):
    monkeypatch.setattr(lc, 'detect_hardware', lambda: Hardware('test gpu', 12288, 10000))
    config = {'stt': {'model': 'small'}, 'translation': {'engine': 'noop'},
              'local_llama': {'threads': 4, 'poll': 0, 'priority': -1, 'batch_size': 512}}
    parent = QtWidgets.QWidget()  # keep a reference: Qt deletes the controls with their parent
    ctrl = lc.LocalModelController(parent, tmp_path, lambda: config, lambda cfg: None)
    ctrl.load_config(config)
    yield ctrl, config
    ctrl.shutdown()
    parent.deleteLater()


def test_tuning_controls_roundtrip_and_old_configs_get_defaults(controller):
    ctrl, config = controller
    local = ctrl.collect_into_config(config)['local_llama']
    assert {k: local[k] for k in ('threads', 'poll', 'priority', 'batch_size')} == {
        'threads': 4, 'poll': 0, 'priority': -1, 'batch_size': 512}
    ctrl.load_config({'stt': {}, 'translation': {'engine': 'noop'}, 'local_llama': {}})
    local = ctrl.collect_into_config(config)['local_llama']
    assert {k: local[k] for k in ('threads', 'poll', 'priority', 'batch_size')} == {
        'threads': -1, 'poll': 50, 'priority': 0, 'batch_size': 256}
    assert lr.cpu_tuning_args(local) == ['--batch-size', '256', '--ubatch-size', '128']


@pytest.mark.parametrize('placement, text', [
    ({'state': 'gpu', 'vram_mb': 1536}, 'Running on GPU · 1.5 GiB VRAM'),
    ({'state': 'partial', 'vram_mb': 1024}, 'Partly on GPU · 1.0 GiB VRAM · translation will be slower'),
    ({'state': 'cpu', 'vram_mb': 0}, 'Running on CPU'),
    (None, 'Placement unknown'),
])
def test_placement_text(controller, placement, text):
    ctrl, _ = controller
    ctrl.show_runtime_placement({'placement': placement})
    assert ctrl.controls['placement'].text().startswith(text)
    ctrl.show_runtime_placement({'placement': placement}, 'Test load: ')
    assert ctrl.controls['placement'].text().startswith('Test load: ' + text)
    ctrl.show_runtime_placement(None)
    assert ctrl.controls['placement'].text() == ''

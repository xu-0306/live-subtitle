import copy
import os
import socket
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6 import QtCore, QtWidgets
import pytest

from gui import config_manager
from gui import local_model_controller as lc
from backend.local_runtime import Hardware


@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def wait_until(app, predicate):
    deadline = time.monotonic() + 8
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert predicate()
    app.processEvents()


def fixture_runtime(monkeypatch, tmp_path, mode='ok'):
    monkeypatch.setattr(lc, 'detect_hardware', lambda: Hardware('test', 24576, 20000))
    monkeypatch.setattr(lc, 'prepare_install', lambda *a, **k: (Path(sys.executable), tmp_path/'model.gguf'))
    original_start = lc.ManagedRuntime.start
    def start(self, *args, **kwargs):
        kwargs.update(command_prefix=[sys.executable, str(Path(__file__).with_name('llama_server_fixture.py')), '--mode', mode], timeout=3)
        return original_start(self, *args, **kwargs)
    monkeypatch.setattr(lc.ManagedRuntime, 'start', start)


def unused_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_real_qt_download_publishes_without_activating_another_provider(app, tmp_path, monkeypatch):
    fixture_runtime(monkeypatch, tmp_path)
    config = {'translation': {'engine': 'ollama', 'ollama': {'model': 'keep'}}, 'stt': {'model': 'medium'}}
    initial = copy.deepcopy(config)
    file = tmp_path/'config.yaml'
    config_manager.write_config(file, config)
    def save(cfg):
        config_manager.write_config(file, cfg)
        config.clear()
        config.update(cfg)
    widget = QtWidgets.QWidget()
    controller = lc.LocalModelController(widget, tmp_path, lambda: config, save)
    controller.controls['port'].setValue(unused_port())
    activations = []
    controller.activated.connect(lambda cfg: activations.append(cfg))
    controller.controls['install'].click()
    wait_until(app, lambda: not controller.busy)
    try:
        assert len(activations) == 1
        assert config_manager.load_config(file)['translation']['engine'] == 'ollama'
        assert lc.MODELS[0].id in config['local_llama']['installed_models']
        assert not controller.runtime.alive()
        controller.controls['stop'].click()
        assert config['translation'] == initial['translation']
        assert not controller.runtime.alive()
    finally:
        controller.shutdown()


def test_qt_failed_download_keeps_original_file(app, tmp_path, monkeypatch):
    fixture_runtime(monkeypatch, tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError('hash mismatch')
    monkeypatch.setattr(lc, 'prepare_install', fail)
    config = {'translation': {'engine': 'ollama'}}
    file = tmp_path/'config.yaml'
    config_manager.write_config(file, config)
    before = file.read_bytes()
    widget = QtWidgets.QWidget()
    controller = lc.LocalModelController(widget, tmp_path, lambda: config, lambda c: config_manager.write_config(file, c))
    controller.controls['port'].setValue(unused_port())
    controller.controls['install'].click()
    wait_until(app, lambda: not controller.busy)
    assert file.read_bytes() == before
    assert not controller.runtime.alive()
    assert controller.controls['install'].isEnabled()
    controller.shutdown()


def test_persistence_failure_stops_ready_runtime(app, tmp_path, monkeypatch):
    fixture_runtime(monkeypatch, tmp_path)
    def fail(_cfg):
        raise OSError('disk full')
    widget = QtWidgets.QWidget()
    controller = lc.LocalModelController(widget, tmp_path, lambda: {'translation': {'engine': 'noop'}}, fail)
    controller.controls['port'].setValue(unused_port())
    controller.controls['install'].click()
    wait_until(app, lambda: not controller.busy)
    assert not controller.runtime.alive()
    assert 'disk full' in controller.controls['status'].text()
    controller.shutdown()


def test_config_activation_does_not_mutate_prior_or_override_new_external_choice():
    before = {'translation': {'engine': 'ollama', 'custom': 'keep'}, 'custom_root': {'keep': True}}
    updated = lc.activate_config(before, 'model', {'context': 2048}, {'base_url': 'http://127.0.0.1:18080', 'api_key': 'token'})
    assert before['translation']['engine'] == 'ollama'
    assert updated['custom_root'] == before['custom_root']
    updated['translation'] = {'engine': 'vllm'}
    assert lc.deactivate_config(updated)['translation'] == {'engine': 'vllm'}


def test_removing_a_local_model_clears_only_its_own_default_choice():
    local = {'model_id': 'a', 'installed_models': ['a']}
    none = {'kind': 'none', 'id': 'none'}
    own = {'local_llama': dict(local), 'translation': {'default_selection': {'kind': 'local', 'id': 'a'}}}
    assert lc.deactivate_config(own)['translation']['default_selection'] == none
    other = {'local_llama': dict(local), 'translation': {'default_selection': {'kind': 'profile', 'id': 'svc'}}}
    assert lc.deactivate_config(other)['translation']['default_selection'] == {'kind': 'profile', 'id': 'svc'}


def test_recommended_model_becomes_default_only_after_verified_install():
    model_id = lc.MODELS[0].id
    config = {
        'translation': {'default_selection': {'kind': 'none', 'id': 'none'}},
        'local_llama': {'pending_default_model_id': model_id},
    }
    updated = lc.activate_config(
        config,
        model_id,
        {'gpu_layers': -1},
        {'installed': True},
    )
    assert updated['translation']['default_selection'] == {
        'kind': 'local',
        'id': model_id,
    }
    assert 'pending_default_model_id' not in updated['local_llama']


def test_stale_installed_flag_is_not_shown_as_a_completed_download(app, tmp_path):
    model_id = lc.MODELS[0].id
    config = {
        'translation': {'engine': 'noop'},
        'local_llama': {'model_id': model_id, 'installed_models': [model_id]},
    }
    widget = QtWidgets.QWidget()
    controller = lc.LocalModelController(widget, tmp_path, lambda: config, lambda cfg: None)
    controller.load_config(config)
    try:
        assert 'missing' in controller.controls['status'].text().lower()
        assert controller.controls['stop'].isEnabled() is False
        assert controller.controls['progress'].isHidden()
        assert controller.controls['install'].text() == 'Download model'
    finally:
        controller.shutdown()


def test_download_progress_reappears_after_selecting_missing_model(app, tmp_path):
    widget = QtWidgets.QWidget()
    controller = lc.LocalModelController(widget, tmp_path, lambda: {}, lambda cfg: None)
    try:
        assert controller.controls['progress'].isHidden()
        controller._progress('Checking runtime', 0, 0)
        assert not controller.controls['progress'].isHidden()
        assert controller.controls['progress'].maximum() == 0
        controller._progress('Downloading model', 30, 100)
        assert not controller.controls['progress'].isHidden()
        assert controller.controls['progress'].value() == 30
        assert controller.controls['technical'].isHidden()
        controller.controls['info_toggle'].click()
        assert not controller.controls['technical'].isHidden()
    finally:
        controller.shutdown()


def test_real_mainwindow_exposes_local_tab_and_shutdown(app, tmp_path, monkeypatch):
    from gui.app import MainWindow
    file = tmp_path/'config.yaml'
    cfg = config_manager.load_default_config()
    cfg['stt']['model_cache_dir'] = str(tmp_path/'models')
    config_manager.write_config(file, cfg)
    monkeypatch.setattr(config_manager, 'ensure_user_config', lambda: file)
    monkeypatch.setattr(MainWindow, '_setup_tray', lambda self: None)
    monkeypatch.setattr(MainWindow, '_auto_start_if_valid', lambda self: None)
    window = MainWindow()
    assert window.tabs.tabText(1) == 'Local translation'
    assert window.local_models.controls['model'].count() == len(lc.MODELS)
    assert window.tabs.tabText(window.tabs.count()-1) == 'Translation services'
    assert window.local_models.controls['context'].value() == 2048
    # Publishing model availability must not restart existing captures.
    calls = []
    monkeypatch.setattr(window, '_restart_backend', lambda: calls.append('restart'))
    result = lc.activate_config(cfg, 'qwen25-1.5b-q4', {}, {'base_url': 'http://127.0.0.1:18080', 'api_key': 'random'})
    window._persist_local_config(result)
    window.local_models.activated.emit(result)
    assert calls == []
    assert config_manager.load_config(file)['translation']['engine'] == cfg['translation']['engine']
    window.close()


def test_gui_profile_save_does_not_activate_a_local_model(app, tmp_path):
    from gui.service_catalog_widget import ServiceCatalogWidget
    config = {'translation': {'engine': 'noop', 'target_language': '粵語（香港）'}}
    parent = QtWidgets.QWidget()
    controller = lc.LocalModelController(parent, tmp_path, lambda: config, lambda cfg: None)
    controller.load_config(config)
    saved = controller.collect_into_config(config)
    # The target language belongs to the shared translation settings, not to a local model.
    assert saved['translation']['target_language'] == '粵語（香港）'
    assert 'target_language' not in saved['local_llama']
    services = ServiceCatalogWidget()
    services.load_from_config(saved)
    services.name_input.setText('A new cloud vendor')
    services.model_input.setText('future/model')
    updated = services.collect_into_config(saved)
    assert updated['translation']['engine'] == 'noop'
    assert updated['translation']['service_profiles'][0]['model'] == 'future/model'
    controller.shutdown()


def test_gpu_offload_control_roundtrips_auto_cpu_and_manual_modes(app, tmp_path, monkeypatch):
    monkeypatch.setattr(lc, 'detect_hardware', lambda: Hardware('test gpu', 12288, 10000))
    config = {
        'stt': {'model': 'small'},
        'translation': {'engine': 'noop'},
        'local_llama': {'gpu_layers': -1},
    }
    parent = QtWidgets.QWidget()
    controller = lc.LocalModelController(parent, tmp_path, lambda: config, lambda cfg: None)
    controller.load_config(config)
    try:
        assert controller.controls['gpu_layers'].value() == -1
        assert 'highest fitting GPU layer count' in controller.controls['gpu_layers_summary'].text()
        assert controller.collect_into_config(config)['local_llama']['gpu_layers'] == -1

        controller.controls['gpu_layers'].setValue(0)
        assert 'CPU' in controller.controls['gpu_layers_summary'].text()
        assert controller.collect_into_config(config)['local_llama']['gpu_layers'] == 0

        controller.controls['gpu_layers'].setValue(23)
        assert '23 layer(s)' in controller.controls['gpu_layers_summary'].text()
        assert controller.collect_into_config(config)['local_llama']['gpu_layers'] == 23
    finally:
        controller.shutdown()

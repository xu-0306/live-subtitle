"""GPU sharing plan, phase 2: speed test entry, recommendation rules, Test speed dialog, notice."""
import json
import os
import sys
import threading
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6 import QtCore, QtWidgets
from qfluentwidgets import InfoBar, PushButton

from backend import benchmark, translation_service
from backend.local_runtime import Hardware
from gui import config_manager
from gui.setup_assistant import SETUP_ASSISTANT_VERSION

GPU = {'name': 'RTX', 'total_mb': 10240, 'free_mb': 9000}
FAST = {'p50_ms': 50, 'p95_ms': 70, 'rtf': 0.5, 'end_lag_s': 0.05}
TRANSLATION = {'p50_ms': 150, 'p95_ms': 160, 'errors': 0}


def result(**overrides):
    base = {'hardware': GPU, 'stt_model': 'small', 'translation_model': 'qwen35-4b-q4',
            'stt_devices': {'encoder': 'cuda', 'decoder': 'cuda'}, 'placement': {'state': 'gpu', 'vram_mb': 4000},
            'local_llama': {'gpu_layers': -1}, 'stt': FAST, 'translation': TRANSLATION,
            'together': {'stt': {**FAST, 'p95_ratio': 1.2}, 'translation': TRANSLATION}, 'error': None}
    return {**base, **overrides}


def ids(items):
    return [item['id'] for item in items]


def test_fast_setup_keeps_up():
    items = benchmark.recommend(result())
    assert items == [{'id': 'keeps_up', 'text': 'This setup keeps up on this computer.', 'changes': {}}]
    assert benchmark.verdict(result()['together']) == 'keeps up'


def test_saturated_speech_suggests_the_next_smaller_whisper_model():
    items = benchmark.recommend(result(stt={**FAST, 'rtf': 0.95}))
    assert items[0] == {'id': 'stt_limit', 'text': 'Speech recognition is at its limit on this PC. Apply switches to Whisper base.',
                        'changes': {'stt.model': 'base'}}
    assert benchmark.recommend(result(stt_model='tiny', stt={**FAST, 'rtf': 0.95}))[0]['changes'] == {}
    assert benchmark.verdict({**FAST, 'rtf': 0.95}) == 'falls behind'


@pytest.mark.parametrize('together_stt', [{**FAST, 'end_lag_s': 2.0}, {**FAST, 'p95_ms': 150}])
def test_falling_behind_together_suggests_a_smaller_translation_model(together_stt):
    items = benchmark.recommend(result(together={'stt': together_stt, 'translation': TRANSLATION}))
    assert items[0]['id'] == 'shared_load'
    assert items[0]['changes'] == {'local_llama.model_id': 'qwen35-2b-q4'}


def test_smallest_translation_model_falls_back_to_a_smaller_whisper_model():
    items = benchmark.recommend(result(translation_model='qwen35-2b-q4',
                                       together={'stt': {**FAST, 'end_lag_s': 2.0}, 'translation': TRANSLATION}))
    assert items[0]['changes'] == {'stt.model': 'base'}


def test_smaller_translation_needs_clearly_less_vram():
    assert benchmark._smaller_translation('qwen25-7b-q4') == 'qwen35-4b-q4'
    assert benchmark._smaller_translation('qwen35-4b-q4') == 'qwen35-2b-q4'  # not the same-size Qwen3 4B
    assert benchmark._smaller_translation('qwen35-2b-q4') is None
    assert benchmark._smaller_translation('unknown') is None


def test_partial_offload_and_cpu_rules():
    slow = {**TRANSLATION, 'p95_ms': 1500}
    assert ids(benchmark.recommend(result(translation=slow, placement={'state': 'partial', 'vram_mb': 900}))) == [
        'partial_offload']
    cpu = benchmark.recommend(result(placement={'state': 'cpu', 'vram_mb': 0}, local_llama={'gpu_layers': 0}))
    assert cpu[0] == {'id': 'auto_offload', 'text': 'Try Auto GPU layers for translation.',
                      'changes': {'local_llama.gpu_layers': -1}}
    low_vram = {**GPU, 'free_mb': 2000}
    assert benchmark.recommend(result(hardware=low_vram, placement={'state': 'cpu', 'vram_mb': 0},
                                      local_llama={'gpu_layers': 0}))[0]['id'] == 'keeps_up'
    stt_cpu = benchmark.recommend(result(stt_devices={'encoder': 'cpu', 'decoder': 'cpu'}))
    assert ids(stt_cpu) == ['stt_cpu'] and stt_cpu[0]['changes'] == {}
    # No GPU speech pack exists for AMD/Intel, but Auto GPU layers still applies there.
    amd = {**GPU, 'vendor': 'amd'}
    assert benchmark.recommend(result(hardware=amd, stt_devices={'encoder': 'cpu', 'decoder': 'cpu'}))[0]['id'] == 'keeps_up'
    assert benchmark.recommend(result(hardware=amd, placement={'state': 'cpu', 'vram_mb': 0},
                                      local_llama={'gpu_layers': 0}))[0]['id'] == 'auto_offload'


def test_failed_run_gives_no_keeps_up_claim():
    assert benchmark.recommend(result(error='Windows speech synthesis is not available', stt=None,
                                      together=None)) == []


def test_main_prints_protocol_lines_and_saves_benchmark_json(tmp_path, monkeypatch, capfd):
    config = tmp_path / 'config.yaml'
    config.write_text('{}', encoding='utf-8')

    def measure(path, res, progress):
        progress('speech', 'Generating test speech')
        print('library noise on stdout')
        res.update(result())

    monkeypatch.setattr(benchmark, '_measure', measure)
    monkeypatch.setattr(sys, 'argv', ['benchmark', '--config', str(config)])
    benchmark.main()
    lines = capfd.readouterr().out.splitlines()
    assert lines[0] == 'PROGRESS {"step": "speech", "label": "Generating test speech"}'
    assert len(lines) == 2 and lines[1].startswith('RESULT ')
    printed = json.loads(lines[1].partition(' ')[2])
    assert printed == json.loads((tmp_path / 'benchmark.json').read_text(encoding='utf-8'))
    assert printed['version'] == 1 and printed['recommendations'][0]['id'] == 'keeps_up'
    assert [p.name for p in tmp_path.iterdir()] == ['benchmark.json', 'config.yaml']


def test_main_reports_errors_in_the_result(tmp_path, monkeypatch, capfd):
    config = tmp_path / 'config.yaml'
    config.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(benchmark, '_measure', lambda *a: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(sys, 'argv', ['benchmark', '--config', str(config)])
    benchmark.main()
    out = json.loads(capfd.readouterr().out.splitlines()[-1].partition(' ')[2])
    assert out['error'] == 'boom' and out['recommendations'] == []


def test_installed_local_model_is_measured_when_the_default_is_another_service(tmp_path, monkeypatch):
    config = tmp_path / 'config.yaml'
    cfg = config_manager.load_default_config()
    cfg['models'] = {'root': str(tmp_path)}
    cfg['translation'] = {'engine': 'nllb', 'target_language': 'zh-TW'}
    cfg['local_llama'] = {'model_id': 'qwen35-2b-q4', 'installed_models': ['qwen35-2b-q4']}
    config_manager.write_config(config, cfg)
    monkeypatch.setattr('backend.local_runtime.detect_hardware', lambda: Hardware('RTX', 10240, 9000))
    monkeypatch.setattr(benchmark, '_speech', lambda path: (_ for _ in ()).throw(RuntimeError('stop here')))
    res = {}
    with pytest.raises(RuntimeError, match='stop here'):
        benchmark._measure(config, res, lambda *a: None)
    assert res['translation_model'] == 'qwen35-2b-q4' and 'translation' not in res


def test_start_local_runtime_matches_the_capture_start(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(translation_service, 'detect_hardware', lambda: Hardware('RTX', 10240, 9000))
    monkeypatch.setattr(translation_service, 'preflight', lambda *a: calls.setdefault('preflight', True))
    monkeypatch.setattr(translation_service, 'auto_gpu_fit_target_mb', lambda stt, hardware=None: 1500)

    def prepare(root, model, kind, cancel, progress, allow_download):
        calls.update(kind=kind, allow_download=allow_download)
        return Path('llama-server.exe'), Path('model.gguf')

    class Runtime:
        def start(self, exe, weights, model_id, settings, cancel, log, progress):
            calls.update(settings=settings, model_id=model_id, log=log)
            return {'base_url': 'http://127.0.0.1:1', 'api_key': 'k'}

    monkeypatch.setattr(translation_service, 'prepare_install', prepare)
    config = {'local_llama': {'gpu_layers': -1, 'threads': 4}}
    out = translation_service.start_local_runtime(Runtime(), tmp_path, config, {'target_language': 'ja'},
                                                  'qwen35-2b-q4', {'model': 'small'}, threading.Event())
    assert out['api_key'] == 'k' and calls['kind'] == 'windows-cuda' and calls['allow_download'] is False
    assert calls['settings']['gpu_fit_target_mb'] == 1500 and calls['settings']['threads'] == 4
    assert calls['settings']['target_language'] == 'ja' and 'disable_thinking' in calls['settings']
    assert calls['log'] == tmp_path / 'runtime.log' and config['local_llama'] == {'gpu_layers': -1, 'threads': 4}
    translation_service.start_local_runtime(Runtime(), tmp_path, {'local_llama': {'gpu_layers': 0}},
                                            {'target_language': 'ja'}, 'qwen35-2b-q4', {}, threading.Event())
    assert calls['kind'] == 'windows-cpu'
    monkeypatch.setattr(translation_service, 'detect_hardware', lambda: Hardware('RX 7800 XT', 16384, 15000, 'amd'))
    monkeypatch.setattr(translation_service, 'vulkan_device', lambda exe, name: 'Vulkan0')
    translation_service.start_local_runtime(Runtime(), tmp_path, {'local_llama': {'gpu_layers': -1}},
                                            {'target_language': 'ja'}, 'qwen35-2b-q4', {}, threading.Event())
    assert calls['kind'] == 'windows-vulkan' and calls['settings']['device'] == 'Vulkan0'
    assert calls['settings']['gpu_fit_target_mb'] == 1500


# --- GUI ---

@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def run_dialog_with(app, tmp_path, script):
    from gui.speed_test import SpeedTestDialog
    parent = QtWidgets.QWidget()
    parent.resize(900, 700)
    applied = []
    dialog = SpeedTestDialog(tmp_path / 'config.yaml', lambda changes: applied.append(changes) or True, parent)
    dialog.process.setArguments(['-c', script])
    loop = QtCore.QEventLoop()
    dialog.process.finished.connect(loop.quit)
    QtCore.QTimer.singleShot(30000, loop.quit)
    dialog.start()
    loop.exec()
    app.processEvents()
    return parent, dialog, applied


def texts(dialog):
    return [label.text() for label in dialog.findChildren(QtWidgets.QLabel)]


def test_dialog_reads_the_process_protocol_and_applies_a_recommendation(app, tmp_path):
    payload = {**result(stt={**FAST, 'rtf': 0.95}), 'version': 1, 'gpu_layers': 'auto'}
    payload['recommendations'] = benchmark.recommend(payload)
    script = ('import json,sys;print("noise");print("PROGRESS "+json.dumps({"step":"stt","label":"x"}));'
              f'print("RESULT "+json.dumps({payload!r}));sys.stdout.flush()')
    parent, dialog, applied = run_dialog_with(app, tmp_path, script)
    shown = texts(dialog)
    assert 'Speed test complete' in shown
    assert any(t.startswith('Speech: Falls behind · typical delay 50 ms, worst 70 ms') for t in shown)
    assert any(t.startswith('Together: Keeps up') for t in shown)
    assert 'Speech recognition: GPU (CUDA)\nTranslation: Running on GPU · 3.9 GiB VRAM' in shown
    apply = [b for b in dialog.findChildren(PushButton) if b.text() == 'Apply']
    assert len(apply) == 1
    apply[0].click()
    assert applied == [{'stt.model': 'base'}] and not apply[0].isEnabled()
    assert dialog.widget.width() <= 560
    dialog.done(0)
    parent.deleteLater()


def test_dialog_shows_the_error_and_stderr_tail_when_the_test_fails(app, tmp_path):
    script = 'import sys;sys.stderr.write("Traceback: model missing\\n");sys.exit(3)'
    parent, dialog, _ = run_dialog_with(app, tmp_path, script)
    shown = texts(dialog)
    assert 'The speed test ended without a usable result (exit code 3).' in shown
    from qfluentwidgets import PlainTextEdit
    assert 'Traceback: model missing' in dialog.findChildren(PlainTextEdit)[0].toPlainText()
    dialog.done(0)
    parent.deleteLater()


def test_closing_the_dialog_kills_the_running_test(app, tmp_path):
    from gui.speed_test import SpeedTestDialog
    parent = QtWidgets.QWidget()
    dialog = SpeedTestDialog(tmp_path / 'config.yaml', lambda changes: True, parent)
    dialog.process.setArguments(['-c', 'import time;time.sleep(60)'])
    dialog.start()
    assert dialog.process.waitForStarted(10000)
    dialog.done(0)
    assert dialog.process.state() == QtCore.QProcess.ProcessState.NotRunning
    parent.deleteLater()


def make_window(tmp_path, monkeypatch, version, existed=True):
    from gui import app as gui_app
    path = tmp_path / 'config.yaml'
    monkeypatch.setattr(config_manager, 'get_user_config_path', lambda: path)
    if existed:
        cfg = config_manager.load_default_config()
        cfg['models'] = {'root': str(tmp_path / 'models')}
        cfg.setdefault('gui', {})['setup_assistant_version'] = version
        config_manager.write_config(path, cfg)
    shown = []
    monkeypatch.setattr(gui_app.MainWindow, '_show_speed_test_notice', lambda self: shown.append('notice'))
    monkeypatch.setattr(gui_app.MainWindow, '_run_first_start_setup_assistant', lambda self: shown.append('modal'))
    win = gui_app.MainWindow(None, auto_start=False, setup_tray=False)
    QtWidgets.QApplication.processEvents()
    return win, shown


@pytest.mark.parametrize('version, existed, expected', [
    (1, True, ['notice']), (SETUP_ASSISTANT_VERSION, True, []), (0, False, ['modal'])])
def test_one_time_notice_only_for_older_existing_configs(app, tmp_path, monkeypatch, version, existed, expected):
    win, shown = make_window(tmp_path, monkeypatch, version, existed)
    assert shown == expected
    win.close()


def test_notice_fits_the_window_and_is_stored_at_once(app, tmp_path):
    from gui.app import MainWindow
    path = tmp_path / 'config.yaml'
    cfg = config_manager.load_default_config()
    cfg['models'] = {'root': str(tmp_path / 'models')}
    config_manager.write_config(path, cfg)
    win = MainWindow(path, auto_start=False, setup_tray=False)
    win.resize(900, 700)
    win.show()
    win._show_speed_test_notice()
    bar = win.findChildren(InfoBar)[-1]
    assert bar.sizeHint().width() <= win.width() - 100
    assert [b.text() for b in bar.findChildren(PushButton)] == ['Test speed', 'Not now']
    saved = config_manager.load_config(path)
    assert saved['gui']['setup_assistant_version'] == SETUP_ASSISTANT_VERSION
    win.close()


# --- progress feedback (a CPU warm-up took minutes with one static label) ---

def _fake_stt(monkeypatch, clock_step=0.25):
    import types
    processor = types.SimpleNamespace(insert_audio_chunk=lambda *a: None, process_iter=lambda: (['word'], None))
    monkeypatch.setitem(sys.modules, 'whisperlivekit.core',
                        types.SimpleNamespace(online_factory=lambda args, asr: processor))
    now = [0.0]

    def clock():
        now[0] += clock_step
        return now[0]
    monkeypatch.setattr(benchmark.time, 'perf_counter', clock)
    return types.SimpleNamespace(args=types.SimpleNamespace(min_chunk_size=0.1), asr=None)


def test_stt_progress_is_throttled_increasing_and_ends_at_the_total(monkeypatch):
    engine = _fake_stt(monkeypatch)
    calls = []
    stats = benchmark._run_stt(engine, [0.0] * (8 * benchmark.RATE), 8, realtime=False,
                               progress=lambda done, total: calls.append((done, total)))
    assert stats['rtf'] > 0
    assert calls[-1] == (8, 8)
    assert all(total == 8 for _done, total in calls)
    dones = [done for done, _total in calls]
    assert dones == sorted(dones) and len(set(dones)) == len(dones)
    assert 3 <= len(calls) < 80  # 80 chunks of 0.1 s; one update per chunk would flood the dialog


def test_a_failing_progress_callback_does_not_stop_measuring(monkeypatch):
    engine = _fake_stt(monkeypatch)

    def broken(done, total):
        raise RuntimeError('dialog went away')
    assert benchmark._run_stt(engine, [0.0] * (2 * benchmark.RATE), 2, realtime=False, progress=broken)['rtf'] > 0
    calls = []
    out = benchmark._run_translation(lambda text: (0.01, 100.0), ['a'], 0.5, interval=0.1,
                                     progress=lambda done, total: calls.append((done, total)))
    assert out['errors'] == 0 and calls[-1][1] == 0.5 and calls[-1][0] <= 0.5


def test_stage_labels_say_how_far_along_and_where_it_runs():
    seen = []
    update = benchmark._stage_progress(lambda step, label, **details: seen.append((step, label, details)),
                                       'stt_warmup', 'Warming up speech recognition on the CPU', 8,
                                       unit='s of audio', device='cpu')
    update(3.7, 8)
    assert seen[0] == ('stt_warmup', 'Warming up speech recognition on the CPU: 0 of 8 s of audio',
                       {'done': 0, 'total': 8, 'device': 'cpu'})
    assert seen[1][1] == 'Warming up speech recognition on the CPU: 3 of 8 s of audio'


def test_dialog_shows_live_progress_elapsed_time_and_the_cpu_hint(app, tmp_path):
    from gui.speed_test import SpeedTestDialog
    payload = {**result(stt_devices={'encoder': 'cpu', 'decoder': 'cpu'}), 'version': 1}
    lines = [
        {'step': 'stt_load', 'label': 'Loading Whisper small'},
        {'step': 'stt_warmup', 'label': 'Warming up speech recognition on the CPU: 3 of 8 s of audio',
         'done': 3, 'total': 8, 'device': 'cpu'},
    ]
    script = ('import json,sys,time\n'
              + ''.join(f'print("PROGRESS "+json.dumps({line!r}),flush=True)\n' for line in lines)
              + 'time.sleep(6)\n'
              + f'print("RESULT "+json.dumps({payload!r}),flush=True)\n')
    parent = QtWidgets.QWidget()
    parent.resize(900, 700)
    dialog = SpeedTestDialog(tmp_path / 'config.yaml', lambda changes: True, parent)
    dialog.process.setArguments(['-c', script])
    finished = QtCore.QEventLoop()
    dialog.process.finished.connect(finished.quit)
    dialog.start()
    import re
    import time
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not (dialog.status.text().startswith('Warming up')
                                               and dialog.elapsed.text() != 'Elapsed 0:00'):
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 100)
    try:
        assert dialog.status.text() == 'Warming up speech recognition on the CPU: 3 of 8 s of audio'
        assert not dialog.cpu_hint.isHidden() and 'GPU speech pack' in dialog.cpu_hint.text()
        assert not dialog.step_progress.isHidden() and dialog.step_progress.value() == 38
        assert dialog.busy.isHidden()
        assert re.fullmatch(r'Elapsed 0:0[1-5]', dialog.elapsed.text())
        if dialog.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            QtCore.QTimer.singleShot(30000, finished.quit)
            finished.exec()
        app.processEvents()
        assert dialog.status.text() == 'Speed test complete'
        assert dialog.cpu_hint.isHidden() and dialog.elapsed.isHidden() and dialog.step_progress.isHidden()
        assert 'Speech recognition: CPU' in '\n'.join(texts(dialog))
    finally:
        dialog.done(0)
        parent.deleteLater()


def test_cpu_hint_never_shows_for_the_gpu(app, tmp_path):
    from gui.speed_test import SpeedTestDialog
    parent = QtWidgets.QWidget()
    dialog = SpeedTestDialog(tmp_path / 'config.yaml', lambda changes: True, parent)
    dialog._show_progress({'step': 'stt', 'label': 'Measuring speech recognition: 5 of 30 s',
                           'done': 5, 'total': 30, 'device': 'cuda'})
    assert dialog.cpu_hint.isHidden()
    dialog._show_progress({'step': 'translation', 'label': 'Measuring translation: 1 of 15 s',
                           'done': 1, 'total': 15, 'device': 'cpu'})
    assert dialog.cpu_hint.isHidden()  # the hint is about speech recognition only
    dialog._show_progress({'step': 'stt_load', 'label': 'Loading Whisper small'})
    assert not dialog.busy.isHidden() and dialog.step_progress.isHidden()
    dialog.done(0)
    parent.deleteLater()

import hashlib
import socket
import subprocess
import sys
import threading
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from backend import local_runtime as lr
from backend.local_catalog import Artifact, MODELS
from backend.translator import build_translator


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def download_server():
    payload = b'GGUF' + b'x' * 1024 * 768
    state = {'ranges': [], 'ignore_range': False, 'bad_range': False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            value = self.headers.get('Range')
            state['ranges'].append(value)
            offset = int(value.split('=')[1].split('-')[0]) if value and not state['ignore_range'] else 0
            self.send_response(206 if offset else 200)
            if offset:
                self.send_header('Content-Range', 'bytes 0-0/1' if state['bad_range'] else f'bytes {offset}-{len(payload)-1}/{len(payload)}')
            self.send_header('Content-Length', str(len(payload)-offset))
            self.end_headers()
            try:
                self.wfile.write(payload[offset:])
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    artifact = Artifact('test.gguf', f'http://127.0.0.1:{server.server_port}/model', len(payload), hashlib.sha256(payload).hexdigest())
    yield artifact, payload, state
    server.shutdown()
    server.server_close()
    thread.join()


def test_cancel_and_resume_download(download_server, tmp_path):
    artifact, payload, state = download_server
    cancel = threading.Event()
    def progress(_name, done, _total):
        if done:
            cancel.set()
    with pytest.raises(lr.Cancelled):
        lr.download_artifact(artifact, tmp_path, cancel, progress, opener=urllib.request.urlopen)
    assert not (tmp_path / artifact.filename).exists()
    assert (tmp_path / (artifact.filename + '.part')).exists()
    cancel.clear()
    target = lr.download_artifact(artifact, tmp_path, cancel, lambda *a: None, opener=urllib.request.urlopen)
    assert target.read_bytes() == payload
    assert state['ranges'][-1].startswith('bytes=')


def test_ignored_range_restarts_and_bad_range_rejected(download_server, tmp_path):
    artifact, payload, state = download_server
    part = tmp_path / (artifact.filename + '.part')
    part.write_bytes(payload[:200])
    state['bad_range'] = True
    with pytest.raises(RuntimeError, match='range'):
        lr.download_artifact(artifact, tmp_path, threading.Event(), lambda *a: None, opener=urllib.request.urlopen)
    assert part.read_bytes() == payload[:200]
    state['ignore_range'] = True
    result = lr.download_artifact(artifact, tmp_path, threading.Event(), lambda *a: None, opener=urllib.request.urlopen)
    assert result.read_bytes() == payload


def test_checksum_and_commit_cancellation(download_server, tmp_path, monkeypatch):
    artifact, _payload, _ = download_server
    wrong = Artifact(artifact.filename, artifact.url, artifact.size, '0' * 64)
    with pytest.raises(RuntimeError, match='checksum'):
        lr.download_artifact(wrong, tmp_path, threading.Event(), lambda *a: None, opener=urllib.request.urlopen)
    assert not (tmp_path / artifact.filename).exists()
    cancel = threading.Event()
    real = lr.digest
    def cancel_after_hash(path, event):
        value = real(path, event)
        cancel.set()
        return value
    monkeypatch.setattr(lr, 'digest', cancel_after_hash)
    with pytest.raises(lr.Cancelled):
        lr.download_artifact(artifact, tmp_path, cancel, lambda *a: None, opener=urllib.request.urlopen)
    assert not (tmp_path / artifact.filename).exists()


def test_cancel_racing_with_atomic_rename_keeps_verified_file_but_reports_cancel(download_server, tmp_path, monkeypatch):
    artifact, payload, _ = download_server
    cancel = threading.Event()
    original_replace = Path.replace
    def cancel_during_replace(source, target):
        cancel.set()
        return original_replace(source, target)
    monkeypatch.setattr(Path, 'replace', cancel_during_replace)
    with pytest.raises(lr.Cancelled):
        lr.download_artifact(artifact, tmp_path, cancel, lambda *a: None, opener=urllib.request.urlopen)
    assert (tmp_path / artifact.filename).read_bytes() == payload


def test_archive_traversal_rejected_and_valid_install_checked(tmp_path):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('../escape.exe', b'bad')
    with pytest.raises(RuntimeError, match='unsafe'):
        lr.extract_runtime([archive], tmp_path / 'runtime', threading.Event())
    assert not (tmp_path / 'escape.exe').exists()
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('bin/llama-server.exe', b'fixture')
        z.writestr('bin/library.dll', b'lib')
    exe = lr.extract_runtime([archive], tmp_path / 'runtime', threading.Event())
    assert lr.installed_runtime(tmp_path / 'runtime', threading.Event()) == exe
    exe.write_bytes(b'corrupt')
    assert lr.installed_runtime(tmp_path / 'runtime', threading.Event()) is None


def test_install_lock_excludes_another_process(tmp_path):
    code = 'from pathlib import Path; from backend.local_runtime import install_lock; import sys;\nwith install_lock(Path(sys.argv[1])): print("claimed")'
    with lr.install_lock(tmp_path):
        result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True)
        assert result.returncode != 0
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True)
    assert result.returncode == 0


def start_fixture(runtime, tmp_path, mode='ok', cancel=None, timeout=5, progress=lambda *a: None):
    script = Path(__file__).with_name('llama_server_fixture.py')
    return runtime.start(Path(sys.executable), tmp_path / 'model.gguf', 'fixture-model',
                         {'port': free_port(), 'gpu_layers': 0}, cancel or threading.Event(),
                         tmp_path / 'runtime.log', progress, timeout=timeout,
                         command_prefix=[sys.executable, str(script), '--mode', mode])


def test_real_child_readiness_translation_adapter_and_shutdown(tmp_path, monkeypatch):
    import asyncio
    # A random URL-safe secret may begin with '-', which CLI parsers treat as an option.
    monkeypatch.setattr(lr.secrets, 'token_urlsafe', lambda size: '-leading-dash-secret')
    runtime = lr.ManagedRuntime()
    try:
        result = start_fixture(runtime, tmp_path)
        assert runtime.alive()
        process = runtime.process
        async def call():
            async with httpx.AsyncClient(trust_env=False) as client:
                provider = build_translator({'engine': 'managed_llama', 'managed_llama': result}, client)
                assert provider.max_concurrency == 1
                assert provider.cancellation_safe is False
                assert provider._request_payload('hello', 'en', 'ja')['max_tokens'] == 192
                return await provider.atranslate('hello', 'en')
        assert asyncio.run(call()) == 'translated fixture'
    finally:
        runtime.stop()
    assert process.poll() is not None
    assert not runtime.alive()


@pytest.mark.parametrize('mode', ['exit', 'fail', 'unready', 'null', 'empty', 'object'])
def test_failed_startup_leaves_no_child(tmp_path, mode):
    runtime = lr.ManagedRuntime()
    with pytest.raises(Exception):
        start_fixture(runtime, tmp_path, mode=mode, timeout=1)
    assert not runtime.alive()
    assert runtime.process is None


def test_cancel_after_spawn_cleans_owned_child(tmp_path):
    runtime = lr.ManagedRuntime()
    cancel = threading.Event()
    def progress(message, *_):
        if message.startswith('Starting'):
            cancel.set()
    with pytest.raises(lr.Cancelled):
        start_fixture(runtime, tmp_path, cancel=cancel, progress=progress)
    assert runtime.process is None


def test_port_collision_does_not_adopt_or_stop_foreign_listener(tmp_path):
    runtime = lr.ManagedRuntime()
    with socket.socket() as foreign:
        foreign.bind(('127.0.0.1', 0))
        foreign.listen()
        with pytest.raises(RuntimeError, match='port is in use'):
            runtime.start(Path(sys.executable), tmp_path/'x', 'x', {'port': foreign.getsockname()[1]},
                          threading.Event(), tmp_path/'x.log', lambda *a: None)
        runtime.stop()
        assert foreign.fileno() >= 0


def test_hardware_preflight_and_catalog():
    with pytest.raises(RuntimeError, match='8 GB'):
        lr.preflight(MODELS[0], lr.Hardware('small', 6144, 6144), {}, {'model': 'medium'})
    assert 'CPU' in lr.preflight(MODELS[0], lr.Hardware('none', 0, 0), {'gpu_layers': 0}, {})
    assert 'not a benchmark' in lr.preflight(MODELS[0], lr.Hardware('3090', 24576, 20000), {}, {'model': 'medium'})
    assert len(MODELS[1].files) == 2
    assert all(len(a.sha256) == 64 for m in MODELS for a in m.files)

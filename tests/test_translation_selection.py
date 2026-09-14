import asyncio
import copy
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend import translation_service as ts
from backend.local_catalog import MODELS, list_models, resolve_hf_model
from backend.translator import OpenAITranslator, _build_translation_prompt


def config():
    return {'local_llama': {'installed_models': [MODELS[0].id], 'gpu_layers': 0},
            'translation': {'engine': 'noop', 'target_language': 'zh-TW', 'vllm': {
                'profiles': [dict(id='remote-future', name='私有翻譯', base_url='https://example.test/v1',
                                  served_model='unknown/future-model', api_key='never-export-this')]}}}


def local(language='Kiswahili'):
    return {'selection': {'kind': 'local', 'id': MODELS[0].id}, 'target_language': language}


def external(language='pt-BR'):
    return {'selection': {'kind': 'profile', 'id': 'remote-future'}, 'target_language': language}


def test_languages_are_open_and_profiles_are_opaque():
    cfg = config()
    before = copy.deepcopy(cfg)
    for language in ['Kiswahili', '粵語（香港）', 'es-MX', 'हिन्दी', 'zh-CN']:
        resolved, _ = ts.resolve_selection(cfg, external(language))
        assert resolved['target_language'] == language
        assert language in _build_translation_prompt('hello', 'en', language) or language == 'zh-CN'
        assert resolved['vllm']['active_profile'] == 'remote-future'
    assert cfg == before
    for value in ['', 'x\nnew prompt', 'x' * 129]:
        with pytest.raises(ValueError, match='target language'):
            ts.resolve_selection(cfg, external(value))
    with pytest.raises(ValueError, match='no longer exists'):
        ts.resolve_selection(cfg, {'selection': {'kind': 'profile', 'id': 'missing'}})
    with pytest.raises(ValueError, match='not installed'):
        ts.resolve_selection(cfg, {'selection': {'kind': 'local', 'id': 'missing'}})


def test_catalog_exposes_ids_not_credentials_or_paths(tmp_path):
    service = ts.TranslationService(config, tmp_path)
    catalog = service.catalog()
    assert len(catalog['models']) == 2
    text = json.dumps(catalog)
    assert 'never-export-this' not in text and 'example.test' not in text
    assert catalog['models'][1]['selection'] == {'kind': 'profile', 'id': 'remote-future'}


class FakeRuntime:
    def __init__(self):
        self.running = False
        self.events = []

    def alive(self):
        return self.running

    def start(self, *args, **kwargs):
        assert not self.running
        self.events.append('start')
        self.running = True
        return {'base_url': 'http://127.0.0.1:18080', 'api_key': 'ephemeral', 'model': MODELS[0].id}

    def stop(self):
        if self.running:
            self.events.append('stop')
        self.running = False


def test_local_and_cloud_are_exclusive_and_runtime_unloads(tmp_path, monkeypatch):
    runtime = FakeRuntime()
    monkeypatch.setattr(ts, 'preflight', lambda *a: None)
    monkeypatch.setattr(ts, 'detect_hardware', lambda: None)
    monkeypatch.setattr(ts, 'prepare_install', lambda *a, **k: (tmp_path/'server', tmp_path/'weights'))
    async def scenario():
        service = ts.TranslationService(config, tmp_path, runtime)
        assert not runtime.alive()
        result = await service.acquire('a', local(), {})
        assert result['engine'] == 'managed_llama'
        assert result['managed_llama']['api_key'] == 'ephemeral'
        await service.acquire('b', local('বাংলা'), {})
        assert runtime.events == ['start']
        with pytest.raises(ValueError, match='Stop existing captures'):
            await service.acquire('c', external(), {})
        await service.release('a')
        assert runtime.alive()
        await service.release('b')
        assert runtime.events == ['start', 'stop']
        cloud = await service.acquire('c', external(), {})
        assert cloud['engine'] == 'vllm' and not runtime.alive()
        with pytest.raises(ValueError):
            await service.acquire('a', local(), {})
        await service.release('c')
        await service.acquire('d', local(), {})
        assert runtime.events == ['start', 'stop', 'start']
        await service.close()
    asyncio.run(scenario())


def test_failed_start_and_same_session_switch_do_not_leave_lease(tmp_path, monkeypatch):
    runtime = FakeRuntime()
    def fail(*a, **k):
        raise RuntimeError('incompatible model')
    monkeypatch.setattr(ts, 'detect_hardware', lambda: None)
    monkeypatch.setattr(ts, 'preflight', fail)
    async def scenario():
        service = ts.TranslationService(config, tmp_path, runtime)
        with pytest.raises(RuntimeError, match='incompatible'):
            await service.acquire('a', local(), {})
        assert not service.leases and not runtime.alive()
        await service.acquire('a', external(), {})
        with pytest.raises(ValueError, match='Stop this capture'):
            await service.acquire('a', {'engine': 'noop'}, {})
        await service.close()
    asyncio.run(scenario())


def test_unknown_hf_repo_resolves_nested_shards_and_rejects_incomplete_metadata():
    repo = 'future-lab/Multilingual-Subtitle-Model'
    names = ['weights/model-Q4-00001-of-00002.gguf', 'weights/model-Q4-00002-of-00002.gguf']
    metadata = {'sha': 'a' * 40, 'siblings': [dict(rfilename=n, size=123, lfs={'sha256': str(i)*64})
                                            for i,n in enumerate(names, 1)]}
    model = resolve_hf_model(repo, names[1], fetch=lambda url: metadata)
    assert [a.filename for a in model.files] == [Path(n).name for n in names]
    assert all('/' + 'a'*40 + '/weights/' in a.url for a in model.files)
    assert model in list_models({'local_llama': {'custom_models': [asdict(model)]}})
    metadata['siblings'].pop()
    with pytest.raises(ValueError, match='Missing file'):
        resolve_hf_model(repo, names[0], fetch=lambda url: metadata)
    with pytest.raises(ValueError):
        resolve_hf_model(repo, '../model.gguf', fetch=lambda url: metadata)


def test_actual_provider_payload_preserves_unlisted_language():
    seen = []
    async def request(req):
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Sawubona'}}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
            translator = OpenAITranslator(api_key='test', model='any-new-model', target_language='isiZulu',
                                          base_url='https://example.test/v1', http_client=client)
            assert await translator.atranslate('Hello', 'en') == 'Sawubona'
        assert 'isiZulu' in seen[0]['messages'][1]['content']
    asyncio.run(scenario())


def test_websocket_catalog_and_api_test_never_initialize_stt(tmp_path, monkeypatch):
    from backend import server
    from fastapi.testclient import TestClient
    cfg = config()
    cfg['local_llama']['root'] = str(tmp_path)
    monkeypatch.setattr(server, 'load_config', lambda: copy.deepcopy(cfg))
    def forbidden(*a, **k):
        raise AssertionError('Catalog and translation test must not load STT')
    monkeypatch.setattr(server, 'TranscriptionEngine', forbidden)
    seen = []
    async def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Hola'}}]})
    with TestClient(server.app, client=('127.0.0.1', 55001)) as client:
        server.app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with client.websocket_connect('/asr') as ws:
            ws.send_json({'type': 'translation_catalog'})
            response = ws.receive_json()
            assert response['type'] == 'translation_catalog'
            assert len(response['models']) == 2
            assert 'never-export-this' not in json.dumps(response)
        with client.websocket_connect('/asr') as ws:
            ws.send_json({'type': 'test', 'target': 'translation', 'translation': external('es-MX')})
            assert ws.receive_json()['type'] == 'status'
            response = ws.receive_json()
            assert response['ok'] and response['message'] == 'Hola'
        assert 'es-MX' in seen[0]['messages'][1]['content']
        assert seen[0]['model'] == 'unknown/future-model'


def test_websocket_rejects_second_provider_before_stt_and_releases_lease(tmp_path, monkeypatch):
    from backend import server
    from fastapi.testclient import TestClient
    cfg = config()
    cfg['local_llama']['root'] = str(tmp_path)
    monkeypatch.setattr(server, 'load_config', lambda: copy.deepcopy(cfg))
    entered = []
    async def capture(ws, stt, translation, pending):
        entered.append(translation['engine'])
        await ws.send_json({'type': 'status', 'message': 'test capture'})
        await ws.receive()
    monkeypatch.setattr(server, '_run_selected_session', capture)
    with TestClient(server.app, client=('127.0.0.1', 55002)) as client:
        with client.websocket_connect('/asr') as first:
            first.send_json({'type': 'config', 'translation': external()})
            first.receive_json()
            first.receive_json()
            with client.websocket_connect('/asr') as second:
                second.send_json({'type': 'config', 'translation': local()})
                second.receive_json()
                error = second.receive_json()
                assert error['type'] == 'error' and 'Stop existing captures' in error['message']
            assert entered == ['vllm']
        # A catalog request completes on the same event loop after disconnect.
        with client.websocket_connect('/asr') as check:
            check.send_json({'type': 'translation_catalog'})
            assert check.receive_json()['active'] is None


def test_cancelling_start_reaps_runtime_before_cloud_can_acquire(tmp_path, monkeypatch):
    import threading
    runtime = FakeRuntime()
    started = threading.Event()
    monkeypatch.setattr(ts, 'preflight', lambda *a: None)
    monkeypatch.setattr(ts, 'detect_hardware', lambda: None)
    monkeypatch.setattr(ts, 'prepare_install', lambda *a, **k: (tmp_path/'server', tmp_path/'model'))
    def slow_start(exe, weights, model_id, settings, cancel, *args):
        runtime.running = True
        started.set()
        assert cancel.wait(3)
        raise RuntimeError('cancelled')
    runtime.start = slow_start
    async def scenario():
        service = ts.TranslationService(config, tmp_path, runtime)
        pending = asyncio.create_task(service.acquire('a', local(), {}))
        await asyncio.to_thread(started.wait, 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not runtime.alive() and not service.leases
        await service.acquire('cloud', external(), {})
        await service.close()
    asyncio.run(scenario())


def test_websocket_disconnect_cancels_loading_without_initializing_stt(tmp_path, monkeypatch):
    import threading
    from backend import server
    from fastapi.testclient import TestClient
    cfg = config()
    cfg['local_llama']['root'] = str(tmp_path)
    runtime = FakeRuntime()
    started, cancelled = threading.Event(), threading.Event()
    def slow_start(exe, weights, model_id, settings, cancel, *args):
        runtime.running = True
        started.set()
        if cancel.wait(5):
            cancelled.set()
        raise RuntimeError('cancelled')
    runtime.start = slow_start
    monkeypatch.setattr(server, 'load_config', lambda: copy.deepcopy(cfg))
    monkeypatch.setattr(ts, 'ManagedRuntime', lambda: runtime)
    monkeypatch.setattr(ts, 'preflight', lambda *a: None)
    monkeypatch.setattr(ts, 'detect_hardware', lambda: None)
    monkeypatch.setattr(ts, 'prepare_install', lambda *a, **k: (tmp_path/'server', tmp_path/'model'))
    async def forbidden(*args):
        raise AssertionError('Disconnected capture must not initialize STT')
    monkeypatch.setattr(server, '_run_selected_session', forbidden)
    with TestClient(server.app, client=('127.0.0.1', 55003)) as client:
        with client.websocket_connect('/asr') as ws:
            ws.send_json({'type': 'config', 'translation': local()})
            ws.receive_json()
            assert started.wait(3)
        assert cancelled.wait(3)
        with client.websocket_connect('/asr') as check:
            check.send_json({'type': 'translation_catalog'})
            response = check.receive_json()
            assert response['active'] is None
        assert not runtime.alive()


def test_pending_start_preserves_early_audio_and_rejects_oversized_buffer():
    from backend import server
    from starlette.websockets import WebSocketDisconnect
    class Socket:
        def __init__(self):
            self.state = SimpleNamespace()
            self.messages = asyncio.Queue()
        async def receive(self):
            return await self.messages.get()
    async def scenario():
        ready = asyncio.Event()
        class Service:
            async def acquire(self, *args):
                await ready.wait()
                return {'engine':'noop'}
        previous = getattr(server.app.state, 'translation_service', None)
        server.app.state.translation_service = Service()
        try:
            ws = Socket()
            pending = asyncio.create_task(server._acquire_translation_for_socket(ws, 'a', {}, {}))
            await ws.messages.put({'bytes': b'first'})
            await ws.messages.put({'bytes': b'second'})
            for _ in range(10):
                await asyncio.sleep(0)
            ready.set()
            await pending
            assert (await server._receive_capture_message(ws))['bytes'] == b'first'
            assert (await server._receive_capture_message(ws))['bytes'] == b'second'
            ready.clear()
            ws = Socket()
            await ws.messages.put({'bytes': b'x' * (8 * 1024 * 1024 + 1)})
            with pytest.raises(ValueError, match='buffer is full'):
                await server._acquire_translation_for_socket(ws, 'b', {}, {})
        finally:
            server.app.state.translation_service = previous
    asyncio.run(scenario())

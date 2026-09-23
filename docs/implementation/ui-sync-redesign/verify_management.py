"""Exercise the real backend management protocol using an isolated config.

Run from the project root with the same Python environment used by the app.
No inference, credentials, or user settings are used by this verification.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import httpx
import yaml
from websockets.sync.client import connect

from backend.config_store import RevisionConflictError, load_snapshot, save_config


def main() -> None:
    output = ROOT / '.work' / 'ui-sync-management' / uuid.uuid4().hex[:10]
    output.mkdir(parents=True)
    models = output / 'models'
    models.mkdir()
    (models / 'interview-special.pt').write_bytes(b'catalog-only-fixture')
    config = yaml.safe_load((ROOT / 'backend' / 'config.yaml').read_text(encoding='utf-8'))
    config['translation'] = {'engine': 'noop', 'target_language': 'Kiswahili', 'partial': False}
    config['stt'].update(model='interview-special', model_cache_dir=str(models))
    config['local_llama'] = {'root': str(output / 'managed'), 'installed_models': []}
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    config['server'] = {'host': '127.0.0.1', 'port': port}
    config_path = output / 'config.yaml'
    save_config(config_path, config)
    _, initial_revision = load_snapshot(config_path)
    instance = uuid.uuid4().hex
    env = dict(os.environ, STT_CONFIG_PATH=str(config_path), STT_INSTANCE_ID=instance, PYTHONUTF8='1')
    checks = []

    def record(name: str, condition: bool) -> None:
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS', name, flush=True)

    def request(payload: dict, *, origin: str | None = None) -> dict:
        with connect(f'ws://127.0.0.1:{port}/asr', origin=origin, open_timeout=5) as ws:
            ws.send(json.dumps(payload))
            return json.loads(ws.recv(timeout=10))

    with (output / 'backend.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(
            [sys.executable, '-u', '-m', 'backend.server', '--host', '127.0.0.1', '--port', str(port), '--config', str(config_path)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        try:
            deadline = time.monotonic() + 60
            health = None
            with httpx.Client(trust_env=False, timeout=1) as client:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f'Backend exited with {process.returncode}; see {output / "backend.log"}')
                    try:
                        response = client.get(f'http://127.0.0.1:{port}/health')
                        if response.status_code == 200:
                            health = response.json()
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
            record('health identifies this process and is ready', bool(health and health.get('instance_id') == instance and health.get('ready')))
            catalog = request({'type': 'desktop_catalog', 'version': 2})
            record('catalog v2 and stable identity', catalog.get('version') == 2 and bool(catalog.get('server_id')))
            record('custom STT comes from directory', any(m.get('selection', {}).get('model') == 'interview-special' for m in catalog['stt_models']))
            record('desktop language retained', catalog['defaults']['translation']['target_language'] == 'Kiswahili')
            legacy = {
                'id': 'old-personal-profile', 'name': 'Research provider', 'engine': 'openai',
                'model': 'unseen-model-2027',
                'connection': {'apiUrl': 'https://provider.example.test/custom/responses', 'apiMode': 'responses', 'autoCompleteApiUrl': False},
                'apiKey': 'FAKE-INTEGRATION-CREDENTIAL',
            }
            payload = {'type': 'desktop_import_profiles', 'version': 2, 'profiles': [legacy], 'expected_revision': catalog['revision']}
            imported = request(payload, origin='chrome-extension://' + 'a' * 32)
            record('legacy profile import acknowledged', bool(imported.get('ok') and legacy['id'] in imported.get('mapping', {})))
            selected = imported['mapping'][legacy['id']]
            current = request({'type': 'desktop_catalog', 'version': 2})
            record('new service visible without restart', any(m['id'] == selected for m in current['models']))
            record('catalog excludes credentials and endpoints', 'FAKE-INTEGRATION-CREDENTIAL' not in json.dumps(current) and 'provider.example.test' not in json.dumps(current))
            repeated = request({**payload, 'expected_revision': current['revision']}, origin='chrome-extension://' + 'a' * 32)
            record('repeat import retains stable id', repeated.get('mapping', {}).get(legacy['id']) == selected)
            state, revision = load_snapshot(config_path)
            saved = next(p for p in state['translation']['service_profiles'] if 'profile:' + p['id'] == selected)
            record('migration retains endpoint and API semantics', saved.get('base_url') == legacy['connection']['apiUrl'] and saved.get('api_type') == 'responses' and saved.get('auto_complete_endpoint') is False)
            try:
                save_config(config_path, config, expected_revision=initial_revision)
            except RevisionConflictError:
                rejected = True
            else:
                rejected = False
            record('stale GUI save rejected', rejected)
            changed = copy.deepcopy(state)
            next(p for p in changed['translation']['service_profiles'] if p['id'] == saved['id'])['name'] = 'Updated from desktop'
            save_config(config_path, changed, expected_revision=revision)
            refreshed = request({'type': 'desktop_catalog', 'version': 2})
            record('desktop save reflected in fresh catalog', any(m['id'] == selected and m['name'] == 'Updated from desktop' for m in refreshed['models']))
            v1 = request({'type': 'translation_catalog'})
            record('v1 read protocol preserved', v1.get('type') == 'translation_catalog' and v1.get('version') == 1)
            hostile_payload = {**payload, 'profiles': [{**legacy, 'id': 'must-not-import'}]}
            try:
                hostile = request(hostile_payload, origin='https://untrusted.example.test')
                origin_rejected = not hostile.get('ok')
            except Exception:
                origin_rejected = True
            latest, _ = load_snapshot(config_path)
            record('untrusted website cannot import', origin_rejected and not any(p.get('imported_from', {}).get('id') == 'must-not-import' for p in latest['translation'].get('service_profiles', [])))
            (output / 'result.json').write_text(json.dumps({'passed': checks, 'backend_instance_verified': True, 'inference': False}, indent=2), encoding='utf-8')
            print('Evidence:', output, flush=True)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == '__main__':
    main()

"""Launch an isolated real backend for verify_browser.cjs; stop it on exit."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from backend.config_store import save_config


def main() -> None:
    output = ROOT / '.work' / 'ui-sync-browser' / uuid.uuid4().hex[:10]
    output.mkdir(parents=True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    config = yaml.safe_load((ROOT / 'backend/config.yaml').read_text(encoding='utf-8'))
    config['server'] = {'host': '127.0.0.1', 'port': port}
    config['stt'].update(model='small', model_cache_dir=str(ROOT), backend='whisper')
    config['translation'] = {
        'engine': 'noop', 'target_language': 'zh-TW', 'partial': False,
        'service_profiles': [{'id': 'research', 'name': 'Research translation', 'engine': 'openai',
                              'model': 'custom-research-model', 'base_url': 'https://provider.example.test/v1',
                              'api_type': 'chat_completions', 'auto_complete_endpoint': True}],
    }
    config['local_llama'] = {'root': str(output / 'managed'), 'installed_models': []}
    if os.environ.get('UI_SYNC_MANAGED') == '1':
        config['local_llama'].update(root=str(ROOT / '.work/managed-smoke'),
            installed_models=['qwen35-2b-q4'], model_id='qwen35-2b-q4', enabled=True,
            context=2048, gpu_layers=-1, disable_thinking=True)
        config['translation'].update(engine='managed_llama', default_selection={'kind':'local','id':'qwen35-2b-q4'}, timeout_sec=60)
    config['stt'].update(stall_timeout_sec=60, stall_check_interval_sec=10)
    config_path = output / 'config.yaml'
    save_config(config_path, config)
    instance = uuid.uuid4().hex
    env = dict(os.environ, STT_CONFIG_PATH=str(config_path), STT_INSTANCE_ID=instance, PYTHONUTF8='1',
               UI_SYNC_WS_URL=f'ws://127.0.0.1:{port}/asr', UI_SYNC_OUTPUT=str(output),
               UI_SYNC_CONFIG_PATH=str(config_path), UI_SYNC_PYTHON=sys.executable,
               NUMBA_CACHE_DIR=str(ROOT / '.work/ui-validation/numba-cache'),
               TRITON_CACHE_DIR=str(ROOT / '.work/ui-validation/triton-cache'))
    with (output / 'backend.log').open('w', encoding='utf-8') as log:
        launch = [sys.executable, '-u', '-m', 'backend.server']
        if os.environ.get('UI_SYNC_TRACE') == '1':
            launch = [sys.executable, '-u', '-c',
                      "import faulthandler,runpy; faulthandler.dump_traceback_later(30,repeat=True); runpy.run_module('backend.server',run_name='__main__')"]
        process = subprocess.Popen(
            [*launch, '--config', str(config_path)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        try:
            env['UI_SYNC_BACKEND_PID'] = str(process.pid)
            deadline = time.monotonic() + 60
            with httpx.Client(trust_env=False, timeout=1) as client:
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f'Backend exited; see {output / "backend.log"}')
                    try:
                        health = client.get(f'http://127.0.0.1:{port}/health').json()
                        if health.get('instance_id') == instance and health.get('ready'):
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    if time.monotonic() > deadline:
                        raise TimeoutError('Backend health timeout')
                    time.sleep(0.2)
            script = os.environ.get('UI_SYNC_VERIFY_SCRIPT', 'verify_browser.cjs')
            command = [sys.executable, '-X', 'utf8'] if script.endswith('.py') else ['node']
            subprocess.run([*command, str(Path(__file__).with_name(script))], cwd=ROOT, env=env, check=True)
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

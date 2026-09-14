"""Opt-in real GPU smoke: downloads catalog assets only with --download.

python -m tests.smoke_managed_runtime is not used; run this file from the repo
root with PYTHONPATH set, or use runpy. Ordinary pytest never downloads models.
"""
import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.local_catalog import MODELS, get_model
from backend.local_runtime import ManagedRuntime, detect_hardware, prepare_install, preflight


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--cpu', action='store_true')
    parser.add_argument('--model-id', default=MODELS[0].id)
    args = parser.parse_args()
    args.root = args.root.resolve()
    spec = get_model(args.model_id)
    cancel = threading.Event()
    runtime = ManagedRuntime()
    settings = {'port': 18087, 'context': 2048, 'gpu_layers': 0 if args.cpu else -1,
                'disable_thinking': spec.disable_thinking}
    seen = {'message': '', 'time': 0}
    def progress(message, done, total):
        now = time.monotonic()
        if message != seen['message'] or now - seen['time'] >= 5:
            print(message, f'{done}/{total}' if total else '', flush=True)
            seen.update(message=message, time=now)
    print(preflight(spec, detect_hardware(), settings, {'model': 'medium'}), flush=True)
    executable, model = prepare_install(args.root, spec, 'windows-cpu' if args.cpu else 'windows-cuda',
                                        cancel, progress, allow_download=args.download)
    try:
        result = runtime.start(executable, model, spec.id, settings, cancel, args.root/'smoke-runtime.log', progress)
        result.pop('api_key', None)
        result['runtime_alive_before_stop'] = runtime.alive()
    finally:
        runtime.stop()
    result['runtime_alive_after_stop'] = runtime.alive()
    (args.root/'smoke-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()

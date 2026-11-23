"""Measure the saved desktop models without starting the capture server."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from .local_catalog import list_models

THRESHOLDS = dict(stt_rtf=0.8, together_lag_s=1.5, together_p95_ratio=2,
                  translation_p95_ms=1000, free_vram_mb=4096,
                  smaller_model_vram_ratio=0.8)
WHISPER_SIZES = ('tiny', 'base', 'small', 'medium', 'large-v3')
RATE = 16000
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
SENTENCES = {
    'en': (
        'On Saturday morning, our neighbors met outside the library to plan a walk across town.',
        'The bakery on the corner opened early, so we picked up some bread and a few cups of tea.',
        'After the rain stopped, we followed the river and watched the boats pass under the old stone bridge.',
        'A group of children had drawn a map of the park, with small blue stars next to their favorite trees.',
        'We took the longer path home and agreed to meet again next week, if the weather stayed clear.',
    ),
    'zh': (
        '星期六早上，鄰居們在圖書館門口集合，準備一起走到城鎮的另一端。',
        '街角的麵包店很早就開門了，我們買了一些麵包，也帶了幾杯熱茶。',
        '雨停之後，我們沿著河邊慢慢走，看著幾艘船從古老的石橋下經過。',
        '孩子們畫了一張公園的地圖，在自己最喜歡的樹旁畫上藍色的小星星。',
        '回家的時候，我們特地繞了一點路，約好下週天氣晴朗的話再出來散步。',
    ),
}


def verdict(section: dict) -> str:
    if 'stt' in section:
        return ('falls behind' if any(verdict(section[key]) == 'falls behind' for key in ('stt', 'translation'))
                else 'keeps up')
    falls_behind = (
        section.get('rtf', 0) > THRESHOLDS['stt_rtf']
        or section.get('end_lag_s', 0) > THRESHOLDS['together_lag_s']
        or section.get('p95_ratio', 0) > THRESHOLDS['together_p95_ratio']
        or ('errors' in section and (section.get('p95_ms') or 0) > THRESHOLDS['translation_p95_ms'])
        or section.get('errors', 0) > 0
    )
    return 'falls behind' if falls_behind else 'keeps up'


def _smaller_translation(model_id: str | None) -> str | None:
    """Largest built-in model that needs clearly less VRAM (<80%) than the current one."""
    catalog = list_models()
    current = next((model for model in catalog if model.id == model_id), None)
    if current is None:
        return None
    limit = current.estimated_vram_mb * THRESHOLDS['smaller_model_vram_ratio']
    candidates = [(model.estimated_vram_mb, model.id) for model in catalog if model.estimated_vram_mb < limit]
    return max(candidates)[1] if candidates else None


def recommend(result: dict) -> list[dict]:
    items = []

    names = {model.id: model.name for model in list_models()}

    def add(item_id, text, changes=None):
        changes = changes or {}
        if 'stt.model' in changes:
            text += f" Apply switches to Whisper {changes['stt.model']}."
        if 'local_llama.model_id' in changes:
            model_id = changes['local_llama.model_id']
            text += f' Apply switches translation to {names.get(model_id, model_id)}.'
        items.append({'id': item_id, 'text': text, 'changes': changes})

    model = result.get('stt_model')
    smaller_stt = ({'stt.model': WHISPER_SIZES[WHISPER_SIZES.index(model) - 1]}
                   if model in WHISPER_SIZES[1:] else {})
    speech = result.get('stt') or {}
    translation = result.get('translation') or {}
    together = (result.get('together') or {}).get('stt') or {}
    placement = (result.get('placement') or {}).get('state')
    hardware = result.get('hardware') or {}
    gpu = hardware.get('total_mb', 0) > 0
    # Results saved before the vendor field existed only ever described NVIDIA cards.
    nvidia = gpu and hardware.get('vendor', 'nvidia') == 'nvidia'
    if speech.get('rtf', 0) > THRESHOLDS['stt_rtf']:
        add('stt_limit', 'Speech recognition is at its limit on this PC.', smaller_stt)
    alone_p95 = speech.get('p95_ms') or 0
    if (together.get('end_lag_s', 0) > THRESHOLDS['together_lag_s'] or
            (alone_p95 and (together.get('p95_ms') or 0) > alone_p95 * THRESHOLDS['together_p95_ratio'])):
        smaller = _smaller_translation(result.get('translation_model'))
        changes = {'local_llama.model_id': smaller} if smaller else smaller_stt
        add('shared_load', 'Speech recognition falls behind while translating. Try a smaller model.', changes)
    if (translation.get('p95_ms') or 0) > THRESHOLDS['translation_p95_ms'] and placement == 'partial':
        add('partial_offload', 'Translation is partly on the CPU. Try a smaller translation model or free VRAM.')
    if (placement == 'cpu' and (result.get('local_llama') or {}).get('gpu_layers') == 0
            and gpu and hardware.get('free_mb', 0) >= THRESHOLDS['free_vram_mb']):
        add('auto_offload', 'Try Auto GPU layers for translation.', {'local_llama.gpu_layers': -1})
    devices = result.get('stt_devices') or {}
    if nvidia and devices.get('encoder') == devices.get('decoder') == 'cpu':
        add('stt_cpu', 'Speech recognition runs on the CPU. Install the GPU speech pack on the Speech recognition page.')
    if not items and not result.get('error') and speech:
        sections = [speech, translation, together, (result.get('together') or {}).get('translation') or {}]
        if all(verdict(section) == 'keeps up' for section in sections):
            add('keeps_up', 'This setup keeps up on this computer.')
    return items


def _speech(path: Path):
    import numpy as np

    script = """
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Speech
$speaker = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $voices = @($speaker.GetInstalledVoices() | Where-Object Enabled)
    if (-not $voices.Count) { throw 'No installed voice' }
    $english = $voices | Where-Object { $_.VoiceInfo.Culture.Name -like 'en-*' } | Select-Object -First 1
    $voice = if ($english) { $english } else { $voices[0] }
    $language = if ($english) { 'en' } else { 'zh' }
    $speaker.SelectVoice($voice.VoiceInfo.Name)
    $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000, 'Sixteen', 'Mono')
    $speaker.SetOutputToWaveFile($env:BENCH_WAV, $format)
    $text = if ($english) { $env:BENCH_EN } else { $env:BENCH_ZH }
    $speaker.Speak($text + ' ' + $text + ' ' + $text)
    $speaker.SetOutputToNull()
    Write-Output $language
} finally { $speaker.Dispose() }
"""
    try:
        completed = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            env={**os.environ, 'BENCH_WAV': str(path), 'BENCH_EN': ' '.join(SENTENCES['en']),
                 'BENCH_ZH': ' '.join(SENTENCES['zh'])},
            capture_output=True, check=True, timeout=60, creationflags=NO_WINDOW,
        )
        language = completed.stdout.decode('ascii').strip()
        with wave.open(str(path)) as audio:
            if ((audio.getframerate(), audio.getsampwidth(), audio.getnchannels()) != (RATE, 2, 1)
                    or audio.getnframes() < 30 * RATE or language not in SENTENCES):
                raise ValueError('Unexpected speech audio format, length, or language')
            samples = np.frombuffer(audio.readframes(35 * RATE), dtype='<i2').astype('float32')
        return language, samples / 32768.0
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr.decode('utf-8', errors='replace').strip(), file=sys.stderr)
        raise RuntimeError('Windows speech synthesis is not available') from None
    except (OSError, ValueError, EOFError, wave.Error, subprocess.SubprocessError) as exc:
        raise RuntimeError('Windows speech synthesis is not available') from exc


def _latencies(values):
    ordered = sorted(values)

    def percentile(fraction):
        return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))] * 1000, 1) if ordered else None

    return dict(n=len(ordered), p50_ms=percentile(0.5), p95_ms=percentile(0.95))


def _progress_reporter(callback, total, start):
    last = start

    def report(done, now, *, final=False):
        nonlocal last
        if not final and now - last < 1:
            return
        last = now
        try:
            callback(done, total)
        except Exception:
            pass

    return report


def _run_stt(engine, audio, seconds, *, realtime=True, progress=None):
    from whisperlivekit.core import online_factory

    processor = online_factory(engine.args, engine.asr)
    step = max(1, round(RATE * engine.args.min_chunk_size))
    total = seconds * RATE
    latencies = []
    recognized = False
    start = time.perf_counter()
    report = _progress_reporter(progress, seconds, start) if progress is not None else None
    position = 0
    while position < total:
        next_chunk = min(total, position + step)
        if realtime:
            time.sleep(max(0, start + next_chunk / RATE - time.perf_counter()))
            elapsed_samples = (time.perf_counter() - start) * RATE
            arrived = min(total, max(next_chunk, int(elapsed_samples // step) * step))
        else:
            arrived = next_chunk
        processor.insert_audio_chunk(audio[position:arrived], arrived / RATE)
        position = arrived
        started = time.perf_counter()
        tokens, _ = processor.process_iter()
        finished = time.perf_counter()
        latencies.append(finished - started)
        recognized = recognized or bool(tokens)
        if report is not None and position < total:
            report(position / RATE, finished)
    if realtime and not recognized:
        if report is not None:
            report(seconds, time.perf_counter(), final=True)
        raise RuntimeError('Speech recognition returned no words. Check the diagnostic log.')
    stats = {**_latencies(latencies), 'rtf': round(sum(latencies) / seconds, 3),
             'end_lag_s': round(max(0, time.perf_counter() - start - seconds), 3)}
    if report is not None:
        report(seconds, time.perf_counter(), final=True)
    return stats


def _translate(client, model, source, target, text):
    from .translator import SYSTEM_TRANSLATION_PROMPT, _build_translation_prompt

    started = time.perf_counter()
    response = client.post('/v1/chat/completions', json={
        'model': model, 'max_tokens': 192, 'temperature': 0, 'stream': False,
        'messages': [{'role': 'system', 'content': SYSTEM_TRANSLATION_PROMPT},
                     {'role': 'user', 'content': _build_translation_prompt(text, source, target)}],
    })
    response.raise_for_status()
    elapsed = time.perf_counter() - started
    return elapsed, (response.json().get('timings') or {}).get('predicted_per_second')


def _run_translation(one, sentences, seconds, *, interval=0, stop=None, progress=None):
    stop = stop or threading.Event()
    start = time.perf_counter()
    deadline = start + seconds
    report = _progress_reporter(progress, seconds, start) if progress is not None else None
    latencies, rates = [], []
    errors = 0
    index = 0
    while not stop.is_set() and time.perf_counter() < deadline:
        started = time.perf_counter()
        try:
            elapsed, rate = one(sentences[index % len(sentences)])
            latencies.append(elapsed)
            if rate:
                rates.append(rate)
        except Exception:
            errors += 1
            traceback.print_exc(file=sys.stderr)
        index += 1
        if interval or errors:
            stop.wait(max(0, started + (interval or 0.1) - time.perf_counter()))
        if report is not None:
            now = time.perf_counter()
            if now < deadline and not stop.is_set():
                report(now - start, now)
    if report is not None:
        now = time.perf_counter()
        report(min(seconds, now - start), now, final=True)
    return {**_latencies(latencies), 'tok_per_s': round(mean(rates), 1) if rates else None, 'errors': errors}


def _sample_vram(stop, samples):
    while not stop.is_set():
        try:
            completed = subprocess.run(
                ['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, check=True, timeout=2, creationflags=NO_WINDOW,
            )
            samples.extend(int(value) for value in completed.stdout.splitlines() if value.strip().isdigit())
        except (OSError, subprocess.SubprocessError):
            return
        stop.wait(0.5)


def _stage_progress(progress, step, label, seconds, *, unit='s', device=None):
    def update(done, total):
        details = dict(done=done, total=total)
        if device is not None:
            details['device'] = device
        progress(step, f'{label}: {int(done)} of {total} {unit}', **details)

    update(0, seconds)
    return update


def _measure(config_path, result, progress):
    from functools import partial

    from . import model_manager, translation_service
    from .config import LOCAL_TRANSLATION_MODELS_DIR_NAME, _default_app_dir, load_config
    from .local_runtime import ManagedRuntime, detect_hardware

    cfg = load_config(config_path)
    stt = dict(cfg['stt'])
    stt.setdefault('model', 'medium')
    cfg_t, local_id = translation_service.resolve_selection(cfg, None)
    chosen = cfg.get('local_llama', {}).get('model_id')
    if not local_id and chosen in cfg.get('local_llama', {}).get('installed_models', []):
        # The desktop default may be a cloud/NLLB service while captures pick the local
        # model in the extension; measure the local model chosen on the Local model page.
        cfg_t, local_id = translation_service.resolve_selection(
            cfg, {'selection': {'kind': 'local', 'id': chosen}})
    layers = int(cfg.get('local_llama', {}).get('gpu_layers', -1))
    result.update(hardware=asdict(detect_hardware()), stt_model=stt['model'],
                  policy=stt.get('backend_policy', 'localagreement'), translation_model=local_id,
                  local_llama={'gpu_layers': layers})
    if not local_id:
        result['translation'] = {'status': 'skipped: not a local model'}

    progress('speech', 'Generating test speech')
    with tempfile.TemporaryDirectory(prefix='live-subtitle-speed-') as folder:
        language, audio = _speech(Path(folder) / 'speech.wav')

    model = stt['model']
    progress('stt_load', f'Loading Whisper {model}')
    os.environ['HF_HUB_OFFLINE'] = '1'
    from . import server

    reference = stt.get('model_path') or stt.get('model_dir')
    target = Path(reference) if reference else model_manager.resolve_stt_model_target(stt)
    if stt.get('backend') == 'openai-api':
        raise RuntimeError('The speed test requires a local speech recognition model.')
    if target is None or not target.exists():
        raise RuntimeError(f'Whisper {model} is not installed. Download it on the Speech recognition page.')
    stt['model_path'] = str(target)
    kwargs = server._build_engine_kwargs(stt)
    kwargs.update(lan=language, vac=False, vad=False, pcm_input=True)
    try:
        engine = asyncio.run(server._construct_stt_engine(kwargs))
    except FileNotFoundError as exc:
        raise RuntimeError(f'Whisper {model} could not load. Download it on the Speech recognition page: {exc}') from exc
    result['stt_devices'] = server._stt_devices(engine, result['policy'])
    devices = set((result['stt_devices'] or {}).values())
    device = next(iter(devices)) if len(devices) == 1 else None
    warmup_label = 'Warming up speech recognition'
    if device is not None:
        warmup_label += ' on the CPU' if device == 'cpu' else ' on the GPU'
    elif devices:
        warmup_label += ' on the CPU and GPU'
    warmup_progress = _stage_progress(progress, 'stt_warmup', warmup_label, 8, unit='s of audio', device=device)
    _run_stt(engine, audio, 8, realtime=False, progress=warmup_progress)

    stt_progress = _stage_progress(progress, 'stt', 'Measuring speech recognition', 30, device=device)
    result['stt'] = _run_stt(engine, audio, 30, progress=stt_progress)
    if not local_id:
        return

    import httpx

    progress('translation_load', 'Loading and warming up local translation')
    start_local_runtime = getattr(translation_service, 'start_local_runtime', None)
    if start_local_runtime is None:
        raise RuntimeError('The shared local translation launcher is unavailable in this build.')
    root = Path(cfg['local_llama'].get('root') or (_default_app_dir() / LOCAL_TRANSLATION_MODELS_DIR_NAME))
    runtime = ManagedRuntime()
    stop = threading.Event()
    try:
        started = start_local_runtime(runtime, root, cfg, cfg_t, local_id, stt, stop)
        result.update({key: started[key] for key in ('placement', 'gpu_layers', 'warmup_ms')})
        with httpx.Client(trust_env=False, base_url=started['base_url'], timeout=15,
                          headers={'Authorization': f"Bearer {started['api_key']}"}) as client:
            one = partial(_translate, client, local_id, language, cfg_t['target_language'])
            sentences = SENTENCES[language]
            one(sentences[0])
            translation_progress = _stage_progress(progress, 'translation', 'Measuring translation', 15)
            result['translation'] = _run_translation(one, sentences, 15, progress=translation_progress)

            together_progress = _stage_progress(progress, 'together',
                                                'Measuring speech recognition and translation together',
                                                30, device=device)
            samples, translated = [], {}

            def translate_together():
                translated.update(_run_translation(one, sentences, 30, interval=2, stop=stop))

            sampler = threading.Thread(target=_sample_vram, args=(stop, samples), daemon=True)
            translator = threading.Thread(target=translate_together, daemon=True)
            sampler.start()
            translator.start()
            try:
                speech = _run_stt(engine, audio, 30, progress=together_progress)
            finally:
                stop.set()
                translator.join()
                sampler.join()
            alone_p95 = result['stt']['p95_ms']
            if alone_p95:
                speech['p95_ratio'] = speech['p95_ms'] / alone_p95
            result['together'] = {'stt': speech, 'translation': translated}
            result['vram_peak_mb'] = max(samples, default=None)
    finally:
        runtime.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    result = dict.fromkeys(('hardware', 'stt_model', 'policy', 'stt_devices', 'translation_model',
                            'placement', 'gpu_layers', 'warmup_ms', 'stt', 'translation',
                            'together', 'vram_peak_mb', 'error'))
    result.update(version=1, timestamp=datetime.now(timezone.utc).isoformat())
    sys.stdout.flush()
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    descriptor = sys.stdout.fileno()
    with os.fdopen(os.dup(descriptor), 'w', encoding='utf-8', buffering=1) as output:
        def emit(prefix, payload):
            print(prefix, json.dumps(payload), file=output, flush=True)

        os.dup2(sys.stderr.fileno(), descriptor)
        try:
            with redirect_stdout(sys.stderr):
                try:
                    _measure(args.config, result,
                             lambda step, label, **details: emit('PROGRESS', dict(step=step, label=label, **details)))
                except Exception as exc:
                    traceback.print_exc()
                    result['error'] = str(exc) or type(exc).__name__
                result['recommendations'] = recommend(result)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False,
                                                     dir=args.config.parent, suffix='.tmp') as handle:
                        temporary = Path(handle.name)
                        json.dump(result, handle)
                    temporary.replace(args.config.with_name('benchmark.json'))
                except OSError as exc:
                    result['error'] = ' '.join(filter(None, (result['error'], f'Could not save benchmark.json: {exc}')))
                    traceback.print_exc()
                finally:
                    if temporary is not None:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            traceback.print_exc()
            emit('RESULT', result)
        finally:
            sys.stderr.flush()
            os.dup2(output.fileno(), descriptor)


if __name__ == '__main__':
    main()

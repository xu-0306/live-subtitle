"""Real WebM/Opus inference, managed translation and lease cleanup.

This exercises the backend transport directly. It does NOT claim browser
tabCapture permission or toolbar verification (see verify_live_capture.cjs).
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import psutil

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[3]


def main():
    output = Path(os.environ['UI_SYNC_OUTPUT'])
    url = os.environ['UI_SYNC_WS_URL']
    audio = output / 'speech.webm'
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                    '-i', str(ROOT / '.work/ui-validation/jfk.flac'), '-af', 'apad=pad_dur=5',
                    '-c:a', 'libopus', '-b:a', '64k', '-ar', '48000', str(audio)], check=True)
    events = []
    checks = []

    def request(payload):
        with connect(url, open_timeout=5, close_timeout=10, ping_timeout=120) as ws:
            ws.send(json.dumps(payload))
            while True:
                event = json.loads(ws.recv(timeout=90))
                if event.get('type') in {'desktop_catalog', 'test_result', 'error'}:
                    return event

    def check(name, condition):
        assert condition, name
        checks.append(name)
        print('PASS', name, flush=True)

    first = request({'type':'desktop_catalog','version':2})
    check('catalog starts without capture leases', first['active']['captures'] == 0)
    with connect(url, open_timeout=5, close_timeout=15, max_size=4*1024*1024, ping_timeout=120) as ws:
        ws.send(json.dumps({'type':'config','version':2,'stt':{},'translation':{}}))
        while True:
            event = json.loads(ws.recv(timeout=120))
            events.append(event)
            print('EVENT', json.dumps(event, ensure_ascii=False), flush=True)
            if event.get('type') == 'error':
                raise AssertionError(event)
            if event.get('phase') == 'capturing':
                break
        snapshot = event['snapshot']
        owned_runtimes = [process for process in psutil.Process(int(os.environ['UI_SYNC_BACKEND_PID'])).children(recursive=True)
                          if process.name().lower() == 'llama-server.exe']
        check('backend resolves inherited STT model', snapshot['stt']['model'] == 'small')
        check('backend resolves inherited target language', snapshot['translation']['target_language'] == 'zh-TW')
        if os.environ.get('UI_SYNC_MANAGED') == '1':
            check('backend resolves inherited translation model', snapshot['translation']['selection']['id'] == 'qwen35-2b-q4')
            same = request({'type':'test','version':2,'target':'translation','translation':{'target_language':'es-MX'}})
            check('same managed model accepts another language', same.get('ok') and bool(same.get('message')))
            busy = request({'type':'test','version':2,'target':'translation','translation':{'selection':{'kind':'profile','id':'research'}}})
            check('different service is rejected while capture holds model', busy.get('type')=='error' and 'in use' in busy.get('message',''))
        none = request({'type':'test','version':2,'target':'translation','translation':{'engine':'noop'}})
        check('no-translation session coexists', none.get('ok') is True)
        packet = audio.read_bytes()
        def feed():
            for offset in range(0, len(packet), 1600):
                ws.send(packet[offset:offset+1600])
                time.sleep(0.14)
        sender = threading.Thread(target=feed, daemon=True)
        sender.start()
        deadline = time.monotonic()+120
        found = None
        while time.monotonic() < deadline:
            event = json.loads(ws.recv(timeout=120))
            events.append(event)
            if event.get('type') == 'subtitle':
                print('SUBTITLE', json.dumps(event,ensure_ascii=False),flush=True)
                if 'country' in event.get('original','').lower() and (os.environ.get('UI_SYNC_MANAGED') != '1' or event.get('translated')):
                    found = event
                    break
            elif event.get('type') == 'error':
                raise AssertionError(event)
        sender.join(timeout=30)
        check('public speech recognized from real WebM/Opus', found is not None)
        if os.environ.get('UI_SYNC_MANAGED') == '1':
            check('managed model returns translated subtitles', bool(found['translated']))
    deadline = time.monotonic()+30
    while True:
        last = request({'type':'desktop_catalog','version':2})
        if last['active']['captures'] == 0 and not last['active'].get('selection'):
            break
        if time.monotonic()>deadline:
            raise AssertionError('capture lease did not release')
        time.sleep(.3)
    check('last capture releases translation occupancy', not last['active'].get('selection'))
    if os.environ.get('UI_SYNC_MANAGED') == '1':
        check('owned llama runtime has exited before backend shutdown', bool(owned_runtimes) and all(not process.is_running() for process in owned_runtimes))
    (output/'audio-result.json').write_text(json.dumps({'checks':checks,'events':events,
        'real_inference':True,'real_browser_tab_capture':False,'audio_source':'https://raw.githubusercontent.com/openai/whisper/main/tests/jfk.flac'},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Evidence:',output,flush=True)


if __name__=='__main__':
    main()

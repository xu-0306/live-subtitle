"""Actual Qt edits/save -> running backend catalog (isolated configuration)."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import threading
import time
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from PySide6 import QtWidgets
from websockets.sync.client import connect
from backend.config_store import load_snapshot
from backend.translation_service import resolve_selection
from gui.app import MainWindow


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow(Path(os.environ['UI_SYNC_CONFIG_PATH']), auto_start=False, setup_tray=False)
    checks = []
    requests = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            body=json.dumps({'choices':[{'message':{'content':'Verified provider response'}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    provider = ThreadingHTTPServer(('127.0.0.1',0),Provider)
    threading.Thread(target=provider.serve_forever,daemon=True).start()
    def check(name, condition):
        assert condition, name
        checks.append(name)
        print('PASS', name, flush=True)
    def catalog():
        with connect(os.environ['UI_SYNC_WS_URL'],open_timeout=5) as ws:
            ws.send(json.dumps({'type':'desktop_catalog','version':2}))
            return json.loads(ws.recv(timeout=5))
    try:
        initial = window._collect_config_from_ui()
        resolved, local = resolve_selection(initial,{})
        check('opening and collecting GUI preserves no-translation default',resolved['engine']=='noop' and local is None)
        services = window.service_catalog
        services.profile_list.setCurrentRow(0)
        services.name_input.setText('Saved from actual Qt editor')
        services.default_check.setChecked(True)
        app.processEvents()
        check('GUI save commits shared config',window._save_config_to_disk() is True)
        changed = catalog()
        check('live backend sees GUI profile rename',any(item['id']=='profile:research' and item['name']=='Saved from actual Qt editor' for item in changed['models']))
        check('GUI default checkbox reaches live catalog',changed['defaults']['translation'].get('selection')=={'kind':'profile','id':'research'})
        snapshot, _ = load_snapshot(window.config_path)
        selected, _ = resolve_selection(snapshot,{})
        check('inherited provider resolves GUI model',selected.get('openai',{}).get('model')=='custom-research-model')

        # Replace the driver's server with the actual GUI runner, preserving
        # the isolated config/port. The provider response is explicitly a fixture.
        services.endpoint_input.setText(f'http://127.0.0.1:{provider.server_port}/v1')
        check('test endpoint is saved through GUI',window._save_config_to_disk() is True)
        os.kill(int(os.environ['UI_SYNC_BACKEND_PID']),signal.SIGTERM)
        time.sleep(.3)
        window._start_backend()
        deadline=time.monotonic()+45
        while time.monotonic()<deadline and window.server_status.text()!='Ready':
            app.processEvents()
            time.sleep(.02)
        check('actual GUI runner reaches verified Ready',window.server_status.text()=='Ready')
        notices=[]
        original_notice=window._show_notice
        def notice(message,*args,**kwargs):
            notices.append(message)
            original_notice(message,*args,**kwargs)
        window._show_notice=notice
        services.test_btn.click()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline and not any('Service test passed:' in text for text in notices):
            app.processEvents()
            time.sleep(.02)
        check('Test button sends real backend/provider request and displays result', bool(requests) and any('Verified provider response' in text for text in notices))
        check('test keeps saved model semantics',requests[-1]['model']=='custom-research-model')
        services.default_check.setChecked(False)
        check('unchecking default saves',window._save_config_to_disk() is True)
        selected, _ = resolve_selection(load_snapshot(window.config_path)[0],{})
        check('unchecking selected default restores explicit no translation',selected['engine']=='noop')
        services.remove_btn.click()
        check('last service can be removed',services.profile_list.count()==0)
        check('empty catalog saves',window._save_config_to_disk() is True)
        check('removed service does not resurrect from legacy adapters',not catalog()['models'])
        (Path(os.environ['UI_SYNC_OUTPUT'])/'gui-backend-result.json').write_text(json.dumps({'checks':checks,'real_qt':True,'real_backend':True,'inference':False},indent=2),encoding='utf-8')
        print('Evidence:',os.environ['UI_SYNC_OUTPUT'],flush=True)
    finally:
        window.close()
        app.processEvents()
        provider.shutdown()
        provider.server_close()


if __name__=='__main__':
    main()

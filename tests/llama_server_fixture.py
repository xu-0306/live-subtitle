"""Actual child process for managed runtime integration tests."""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

p = argparse.ArgumentParser()
p.add_argument('--port', type=int, required=True)
p.add_argument('--api-key', required=True)
p.add_argument('--mode', default='ok')
args, _ = p.parse_known_args()
if args.mode == 'exit':
    raise SystemExit(3)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(503 if args.mode == 'unready' else 200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if args.mode == 'slow':
            time.sleep(5)
        ok = self.headers.get('Authorization') == 'Bearer ' + args.api_key
        self.send_response(200 if ok and args.mode != 'fail' else 500)
        self.end_headers()
        content = {'null': None, 'empty': '', 'object': {'text': 'invalid'}}.get(args.mode, 'translated fixture')
        self.wfile.write(json.dumps({'choices': [{'message': {'content': content}}]}).encode())


ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()

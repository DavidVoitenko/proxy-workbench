import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import gui
from proxy_workbench import proxytool as p


class SourceActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.server = gui.make_server(self.home)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.server.server_port}', trust_env=False,
                                   headers={'X-Workbench-Token': self.server.app.token}, timeout=5)

    def tearDown(self):
        self.server.app.close()
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def settings(self, sources):
        return dict(gui.defaults(), sources=sources)

    def test_prune_removes_only_well_sampled_dead_sources(self):
        dead, small, alive, unseen = ('https://dead.example/list.txt', 'https://small.example/list.txt',
                                      'socks5 https://alive.example/list.txt', 'https://new.example/list.txt')
        quality = {p.source_key(dead): {'checked': 500, 'passed': 0}, p.source_key(small): {'checked': 5, 'passed': 0},
                   p.source_key(alive): {'checked': 300, 'passed': 12}}
        (self.home / 'exports').mkdir()
        (self.home / 'exports' / 'status.json').write_text(json.dumps({'source_quality': quality}))
        response = self.client.post('/api/sources/prune', json=self.settings([dead, small, alive, unseen]))
        response.raise_for_status()
        body = response.json()
        self.assertEqual(body['removed'], ['https://dead.example/'])
        self.assertEqual(body['settings']['sources'], [small, alive, unseen])
        saved = json.loads((self.home / 'gui-settings.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['sources'], [small, alive, unseen])

    def test_update_adds_only_new_upstream_sources(self):
        bundled = gui.defaults()['sources']
        mine = bundled[1:] + ['https://mine.example/list.txt']  # the user removed bundled[0]
        upstream = bundled + ['text https://fresh.example/page', 'auto https://mine.example/list.txt']

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps(upstream).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass
        upstream_server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=upstream_server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{upstream_server.server_port}/sources.json'
        try:
            with mock.patch.dict(os.environ, {'PROXY_WORKBENCH_SOURCES_URL': url}):
                body = self.client.post('/api/sources/update', json=self.settings(mine)).json()
        finally:
            upstream_server.shutdown()
            upstream_server.server_close()
        self.assertEqual(body['added'], ['text https://fresh.example/', 'auto https://mine.example/'])
        self.assertNotIn(bundled[0], body['settings']['sources'])
        self.assertEqual(body['settings']['sources'][-2:], ['text https://fresh.example/page', 'auto https://mine.example/list.txt'])

    def test_update_reports_unreachable_upstream(self):
        with mock.patch.dict(os.environ, {'PROXY_WORKBENCH_SOURCES_URL': 'http://127.0.0.1:9/none'}):
            response = self.client.post('/api/sources/update', json=self.settings([]))
        self.assertEqual(response.status_code, 400)
        self.assertIn('GitHub', response.json()['error'])


if __name__ == '__main__':
    unittest.main()

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api
import proxytool as p


def result_row(proxy, latency, score, level=None):
    row = dict(proxy=proxy, reliability=1, min_target_reliability=1, latency_ms=latency, jitter_ms=5,
               score=score, successes=3, requests=3, checked_at=0, samples=[])
    if level:
        row['anonymity'] = {'level': level, 'signals': []}
    return row


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        db = p.open_db(self.home / 'proxies.sqlite3')
        cfg = dict(targets=[dict(url='https://one.invalid/')], anonymity={'judge_url': 'https://judge.invalid/'})
        db.execute('INSERT INTO profiles VALUES (?,?)', ('fx', json.dumps(cfg)))
        rows = [result_row('http://11.0.0.1:8080', 900, 50, 'transparent'),
                result_row('socks5://11.0.0.2:1080', 200, 90, 'elite'),
                result_row('https://11.0.0.3:443', 400, 70, 'anonymous')]
        for row in rows:
            db.execute('INSERT INTO candidates VALUES (?)', (row['proxy'],))
            db.execute('INSERT INTO results VALUES (?,?,?)', ('fx', row['proxy'], json.dumps(row)))
        db.execute("INSERT INTO candidate_meta(proxy, country) VALUES ('socks5://11.0.0.2:1080', 'DE')")
        db.commit()
        self.db = db
        self.export()

    def export(self):
        p.export(self.db, 'fx', self.home / 'exports', min_success=1, country_of=p.country_resolver(self.db))

    def start(self, **options):
        self.server = api.make_api_server(self.home, port=0, **options)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return httpx.Client(base_url=f'http://127.0.0.1:{self.server.server_port}', trust_env=False, timeout=5)

    def tearDown(self):
        if hasattr(self, 'server'):
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
        self.db.close()
        self.temp.cleanup()

    def test_filters_formats_and_random(self):
        with self.start() as client:
            status = client.get('/status').json()
            self.assertEqual((status['available'], status['version']), (3, p.PRODUCT_VERSION))
            body = client.get('/proxies').json()
            self.assertEqual([row['proxy'] for row in body['proxies']],
                             ['socks5://11.0.0.2:1080', 'https://11.0.0.3:443', 'http://11.0.0.1:8080'])
            first = body['proxies'][0]
            self.assertEqual((first['protocol'], first['host'], first['port'], first['country'], first['anonymity']),
                             ('socks5', '11.0.0.2', 1080, 'DE', 'elite'))
            self.assertEqual(client.get('/proxies?country=de&format=txt').text, 'socks5://11.0.0.2:1080\n')
            self.assertEqual(client.get('/proxies?protocol=https&format=hostport').text, '11.0.0.3:443\n')
            self.assertEqual(client.get('/proxies?max_latency=500&limit=1&format=txt').text, 'socks5://11.0.0.2:1080\n')
            self.assertEqual(client.get('/proxies?anonymity=anonymous').json()['count'], 2)
            picked = client.get('/random?protocol=http&format=txt').text
            self.assertEqual(picked, 'http://11.0.0.1:8080\n')
            self.assertEqual(len(client.get('/random?limit=2').json()['proxies']), 2)
            self.assertEqual(client.get('/random?country=US').status_code, 404)
            for bad in ('protocol=ftp', 'country=Germany', 'limit=-1', 'format=xml', 'anonymity=max',
                        'max_latency=abc'):
                self.assertEqual(client.get('/proxies?' + bad).status_code, 400, bad)
            self.assertEqual(client.get('/nope').status_code, 404)
            self.assertEqual(client.get('/status', headers={'Host': 'evil.example'}).status_code, 403)

    def test_picks_up_a_new_export_without_restart(self):
        with self.start() as client:
            self.assertEqual(client.get('/status').json()['available'], 3)
            self.db.execute("DELETE FROM results WHERE proxy='http://11.0.0.1:8080'")
            self.db.commit()
            self.export()
            self.assertEqual(client.get('/status').json()['available'], 2)

    def test_token_and_network_binding(self):
        with self.assertRaises(ValueError):
            api.make_api_server(self.home, host='0.0.0.0', port=0)
        with self.start(token='secret') as client:
            self.assertEqual(client.get('/proxies').status_code, 401)
            self.assertEqual(client.get('/proxies', headers={'Authorization': 'Bearer wrong'}).status_code, 401)
            self.assertEqual(client.get('/proxies', headers={'Authorization': 'Bearer secret'}).status_code, 200)
            self.assertEqual(client.get('/proxies?token=secret').status_code, 200)

    def test_empty_data_folder(self):
        empty = self.home / 'empty'
        server = api.make_api_server(empty, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False, timeout=5) as client:
                self.assertEqual(client.get('/proxies').json(), {'count': 0, 'generated_at': None, 'proxies': []})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_cli_rejects_open_bind_without_token(self):
        self.assertEqual(p.main(['serve', '--data', str(self.home), '--host', '0.0.0.0', '--port', '0']), 2)


if __name__ == '__main__':
    unittest.main()

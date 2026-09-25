import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import formats, gui
from proxy_workbench import proxytool as p


def result_row(proxy, score=90, passes=1, checks=1):
    return dict(proxy=proxy, reliability=1, min_target_reliability=1, latency_ms=100, jitter_ms=1, score=score,
                successes=1, requests=1, checked_at=0, samples=[],
                history=dict(checks=checks, passes=passes, first_checked=0, last_ok=0))


class RecommendedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db = p.open_db(self.home / 'proxies.sqlite3')
        self.db.execute('INSERT INTO profiles VALUES (?,?)', ('fx', json.dumps(dict(targets=[dict(url='https://one.invalid/')]))))
        # crowded: same score but offered by five lists; rare: one niche list with a good record.
        rows = [result_row('http://11.0.0.1:80', score=90), result_row('http://11.0.0.2:80', score=85)]
        for row in rows:
            self.db.execute('INSERT INTO candidates VALUES (?)', (row['proxy'],))
            self.db.execute('INSERT INTO results VALUES (?,?,?)', ('fx', row['proxy'], json.dumps(row)))
        for source in 'abcde':
            self.db.execute('INSERT INTO candidate_seen VALUES (?,?)', ('http://11.0.0.1:80', source))
        self.db.execute('INSERT INTO candidate_seen VALUES (?,?)', ('http://11.0.0.2:80', 'niche'))
        self.db.executemany('INSERT INTO candidate_meta(proxy, source) VALUES (?,?)',
                            [('http://11.0.0.1:80', 'a'), ('http://11.0.0.2:80', 'niche')])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_rare_proxy_from_a_niche_list_ranks_first(self):
        report = p.export(self.db, 'fx', self.home / 'exports', min_success=1, sort='recommended')
        order = (self.home / 'exports' / 'proxies.txt').read_text().split()
        self.assertEqual(order, ['http://11.0.0.2:80', 'http://11.0.0.1:80'])
        quality = (self.home / 'exports' / 'proxies.txt')
        p.export(self.db, 'fx', self.home / 'exports', min_success=1, sort='quality')
        self.assertEqual(quality.read_text().split()[0], 'http://11.0.0.1:80')
        ranked = json.loads((self.home / 'exports' / 'ranked.json').read_text())
        self.assertEqual({row['proxy']: row['listed_in'] for row in ranked}, {'http://11.0.0.1:80': 5, 'http://11.0.0.2:80': 1})
        self.assertEqual(report['exported'], 2)
        self.assertEqual(p.parser().parse_args(['export']).sort, 'recommended')

    def test_gui_recommended_sort(self):
        p.export(self.db, 'fx', self.home / 'exports', min_success=1)
        (self.home / 'last-profile.txt').write_text('fx')
        server = gui.make_server(self.home)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False,
                              headers={'X-Workbench-Token': server.app.token}, timeout=5) as client:
                rows = client.get('/api/results?min_success=1&sort=recommended').json()['rows']
                self.assertEqual([row['proxy'] for row in rows], ['http://11.0.0.2:80', 'http://11.0.0.1:80'])
                self.assertGreater(rows[0]['recommended'], rows[1]['recommended'])
        finally:
            server.app.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_get_command_prints_filtered_proxies(self):
        p.export(self.db, 'fx', self.home / 'exports', min_success=1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(p.main(['get', '--data', str(self.home), '--top', '1', '--format', 'hostport']), 0)
        self.assertEqual(out.getvalue().split(), ['11.0.0.1:80'])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(p.main(['get', '--data', str(self.home), '--protocol', 'socks5']), 1)


class TestCommandTests(unittest.TestCase):
    def test_reports_each_proxy(self):
        async def fake_check(proxy, config, rate):
            ok = proxy.endswith(':80')
            samples = [dict(ok=ok, ms=12, target=0, attempt=1, error=None if ok else 'ConnectError')]
            return p.summarize(proxy, samples, config)
        out = io.StringIO()
        with mock.patch.object(p, 'check_proxy', fake_check), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = p.main(['test', 'http://11.0.0.1:80', 'socks5://11.0.0.2:1080', 'not-a-proxy', '--min-success', '1'])
        self.assertEqual(code, 1)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith('OK   http://11.0.0.1:80  1/1  12 ms'))
        self.assertTrue(lines[1].startswith('FAIL socks5://11.0.0.2:1080') and 'ConnectError' in lines[1])


class SingboxTests(unittest.TestCase):
    def test_config(self):
        config = json.loads(formats.singbox([dict(proxy='socks4://11.0.0.1:4145', country='DE'),
                                             dict(proxy='http://11.0.0.2:80'), dict(proxy='https://11.0.0.3:443')]))
        auto, first, second = config['outbounds']
        self.assertEqual(auto['outbounds'], ['DE socks4 11.0.0.1:4145', '?? http 11.0.0.2:80'])
        self.assertEqual((first['type'], first['version'], first['server_port']), ('socks', '4', 4145))
        self.assertEqual(second['type'], 'http')
        self.assertEqual(config['route']['final'], 'auto')
        empty = json.loads(formats.singbox([]))
        self.assertEqual(empty['outbounds'][0]['type'], 'block')
        self.assertEqual(empty['route']['final'], 'blocked')
        self.assertNotIn('direct', json.dumps(empty))


if __name__ == '__main__':
    unittest.main()

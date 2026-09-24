import asyncio
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gui
import proxytool as p


def scan_config(targets=1, attempts=3, fail_fast=None):
    cfg = dict(version=2, targets=[dict(url=f'http://service{i}.invalid/check', method='GET', statuses=[200],
                                        headers={}, contains='healthy', sha256=None) for i in range(targets)],
               attempts=attempts, timeout=0.5, max_bytes=1024)
    if fail_fast is not None:
        cfg['fail_fast'] = {'min_success': fail_fast}
    return cfg


def result_row(proxy, latency, jitter, score):
    return dict(proxy=proxy, reliability=1, min_target_reliability=1, latency_ms=latency, jitter_ms=jitter,
                score=score, successes=3, requests=3, checked_at=0, samples=[])


class FailFastTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, statuses):
        requests = []

        async def handler(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                status = statuses[min(len(requests), len(statuses) - 1)]
                requests.append(status)
                body = b'healthy'
                writer.write(b'HTTP/1.1 %d X\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % (status, len(body)) + body)
                await writer.drain()
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        return server, f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}', requests

    def test_allowed_failures(self):
        self.assertIsNone(p.allowed_failures(scan_config()))
        self.assertEqual(p.allowed_failures(scan_config(attempts=3, fail_fast=2/3)), 1)
        self.assertEqual(p.allowed_failures(scan_config(attempts=3, fail_fast=1)), 0)
        self.assertEqual(p.allowed_failures(scan_config(attempts=3, fail_fast=0)), 2)
        self.assertEqual(p.allowed_failures(scan_config(attempts=10, fail_fast=0.5)), 5)

    async def test_dead_proxy_stops_after_threshold_is_out_of_reach(self):
        server, proxy, requests = await self.serve([503])
        async with server:
            row = await p.check_proxy(proxy, scan_config(fail_fast=2/3), p.Rate(0))
        self.assertEqual(len(requests), 2)
        self.assertTrue(row['aborted'])
        self.assertEqual(row['min_target_reliability'], 0)

    async def test_recovering_proxy_is_measured_fully(self):
        server, proxy, requests = await self.serve([503, 200, 200])
        async with server:
            row = await p.check_proxy(proxy, scan_config(fail_fast=2/3), p.Rate(0))
        self.assertEqual(len(requests), 3)
        self.assertNotIn('aborted', row)
        self.assertAlmostEqual(row['min_target_reliability'], 2/3)

    async def test_strict_threshold_aborts_before_later_targets(self):
        server, proxy, requests = await self.serve([503])
        async with server:
            row = await p.check_proxy(proxy, scan_config(targets=2, fail_fast=1), p.Rate(0))
        self.assertEqual(len(requests), 1)
        self.assertTrue(row['aborted'])
        self.assertEqual(row['min_target_reliability'], 0)

    async def test_without_fail_fast_every_attempt_runs(self):
        server, proxy, requests = await self.serve([503])
        async with server:
            row = await p.check_proxy(proxy, scan_config(), p.Rate(0))
        self.assertEqual(len(requests), 3)
        self.assertNotIn('aborted', row)

    def test_aborted_rows_never_pass_their_threshold(self):
        for attempts in range(1, 8):
            for threshold in (0, 0.5, 2/3, 1):
                cfg = scan_config(attempts=attempts, fail_fast=threshold)
                limit = p.allowed_failures(cfg)
                # Worst case for the check: every success first, then failures up to the abort.
                ok = attempts - limit - 1
                samples = [dict(ok=i < ok, ms=10, target=0, attempt=i + 1) for i in range(ok + limit + 1)]
                row = p.summarize('http://11.0.0.1:80', samples, cfg)
                self.assertFalse(p.result_allowed(row, threshold), (attempts, threshold))


class ConfigTests(unittest.TestCase):
    def args(self, **values):
        base = dict(config=None, url='http://service.invalid/', attempts=3, timeout=8, max_bytes=1024,
                    request_profile=None, judge_url=None, connect_timeout=None, fail_fast=False, min_success=2/3)
        return SimpleNamespace(**dict(base, **values))

    def test_optional_keys_keep_old_profiles_stable(self):
        plain = p.target_config(self.args())
        self.assertNotIn('connect_timeout', plain)
        self.assertNotIn('fail_fast', plain)
        self.assertNotIn('connect_timeout', p.target_config(self.args(connect_timeout=8)))
        tuned = p.target_config(self.args(connect_timeout=3, fail_fast=True))
        self.assertEqual(tuned['connect_timeout'], 3)
        self.assertEqual(tuned['fail_fast'], {'min_success': 2/3})
        timeout = p.request_timeout(tuned)
        self.assertEqual((timeout.connect, timeout.read), (3, 8))

    def test_cli_defaults_enable_speedups(self):
        args = p.parser().parse_args(['scan'])
        self.assertEqual(args.connect_timeout, 4)
        self.assertTrue(args.fail_fast)
        self.assertFalse(p.parser().parse_args(['scan', '--no-fail-fast']).fail_fast)
        self.assertEqual(p.parser().parse_args(['export', '--protocol', 'socks5']).protocol, 'socks5')

    def test_gui_settings_validation(self):
        clean = gui.validate(dict(gui.defaults(), protocol='socks5', max_latency=1500, sort='stability', fail_fast=False))
        self.assertEqual((clean['protocol'], clean['max_latency'], clean['sort'], clean['fail_fast']),
                         ('socks5', 1500, 'stability', False))
        for bad in (dict(protocol='socks4'), dict(max_latency=-1), dict(sort='random'), dict(fail_fast='yes'),
                    dict(connect_timeout=0)):
            with self.assertRaises(ValueError):
                gui.validate(dict(gui.defaults(), **bad))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db = p.open_db(self.home / 'proxies.sqlite3')
        rows = [result_row('http://11.0.0.1:80', 900, 5, 50), result_row('socks5://11.0.0.2:1080', 200, 90, 70),
                result_row('socks5h://11.0.0.3:1080', 300, 10, 80), result_row('https://11.0.0.4:443', 100, 40, 90)]
        self.db.execute('INSERT INTO profiles VALUES (?,?)', ('fixture', json.dumps(dict(targets=[dict(url='https://one.invalid/')]))))
        for row in rows:
            self.db.execute('INSERT INTO candidates VALUES (?)', (row['proxy'],))
            self.db.execute('INSERT INTO results VALUES (?,?,?)', ('fixture', row['proxy'], json.dumps(row)))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def exported(self, **options):
        out = self.home / 'out'
        report = p.export(self.db, 'fixture', out, min_success=1, **options)
        return report, (out / 'proxies.txt').read_text(encoding='utf-8').split()

    def test_matches_selection(self):
        row = result_row('socks5h://11.0.0.3:1080', 300, 10, 80)
        self.assertTrue(p.matches_selection(row, 'socks5'))
        self.assertFalse(p.matches_selection(row, 'http'))
        self.assertTrue(p.matches_selection(row, 'all', 300))
        self.assertFalse(p.matches_selection(row, 'all', 299))
        self.assertFalse(p.matches_selection(dict(row, latency_ms=None), 'all', 1000))

    def test_export_protocol_latency_and_stability(self):
        _, quality = self.exported()
        self.assertEqual(quality, ['https://11.0.0.4:443', 'socks5h://11.0.0.3:1080', 'socks5://11.0.0.2:1080', 'http://11.0.0.1:80'])
        report, socks = self.exported(protocol='socks5')
        self.assertEqual(socks, ['socks5h://11.0.0.3:1080', 'socks5://11.0.0.2:1080'])
        self.assertEqual((report['protocol'], report['passed']), ('socks5', 2))
        _, fast = self.exported(max_latency=250)
        self.assertEqual(fast, ['https://11.0.0.4:443', 'socks5://11.0.0.2:1080'])
        _, stable = self.exported(sort='stability')
        self.assertEqual(stable[0], 'http://11.0.0.1:80')


class GuiSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        db = p.open_db(self.home / 'proxies.sqlite3')
        db.execute('INSERT INTO profiles VALUES (?,?)', ('fixture', json.dumps(dict(targets=[dict(url='https://one.invalid/')]))))
        for row in (result_row('http://11.0.0.1:8080', 900, 5, 50), result_row('socks5://11.0.0.2:1080', 200, 90, 70)):
            db.execute('INSERT INTO candidates VALUES (?)', (row['proxy'],))
            db.execute('INSERT INTO results VALUES (?,?,?)', ('fixture', row['proxy'], json.dumps(row)))
        db.commit()
        db.close()
        (self.home / 'last-profile.txt').write_text('fixture')
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

    def proxies(self, query):
        response = self.client.get('/api/results?min_success=1&' + query)
        response.raise_for_status()
        return [row['proxy'] for row in response.json()['rows']]

    def test_results_filters_search_and_sort(self):
        self.assertEqual(self.proxies('sort=quality'), ['socks5://11.0.0.2:1080', 'http://11.0.0.1:8080'])
        self.assertEqual(self.proxies('sort=stability'), ['http://11.0.0.1:8080', 'socks5://11.0.0.2:1080'])
        self.assertEqual(self.proxies('protocol=http'), ['http://11.0.0.1:8080'])
        self.assertEqual(self.proxies('max_latency=500'), ['socks5://11.0.0.2:1080'])
        self.assertEqual(self.proxies('q=8080'), ['http://11.0.0.1:8080'])
        for bad in ('sort=random', 'protocol=socks4', 'max_latency=-5'):
            self.assertEqual(self.client.get('/api/results?' + bad).status_code, 400)


if __name__ == '__main__':
    unittest.main()

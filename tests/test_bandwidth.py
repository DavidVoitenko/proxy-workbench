import asyncio
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests.workbench_support import add_candidate, store_result  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import api, gui
from proxy_workbench import proxytool as p

#: A result fixture describes a measurement that just happened; the
#: admission contract has no "fresh forever" state (CONTRACTS §2.4).
_NOW = time.time()

BIG = 400_000


def config(speedtest=True):
    cfg = dict(version=2, targets=[dict(url='http://service.invalid/health', method='GET', statuses=[200], headers={},
                                        contains='healthy', sha256=None)], attempts=1, timeout=5, max_bytes=1024)
    if speedtest:
        cfg['speedtest'] = {'url': 'http://speed.invalid/file', 'max_bytes': 100_000}
    return cfg


class BandwidthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []

        async def proxy(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                target = head.split(b' ')[1]
                self.requests.append(target)
                body = b'x' * BIG if b'speed.invalid' in target else b'healthy'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body))
                for start in range(0, len(body), 16384):
                    writer.write(body[start:start + 16384])
                    await writer.drain()
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        self.server = await asyncio.start_server(proxy, '127.0.0.1', 0)
        self.proxy = f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_a_transfer_too_fast_to_measure_reports_no_number(self):
        """Defect 15: a plausible-looking number is worse than no number.

        The old test asserted ``mbps > 0`` for a 100 KB loopback transfer.  The
        window that transfer gives is far below the 0.25 s minimum, and
        ``probes.measure_speed`` now says ``insufficient`` and carries no
        figure at all.  The assertion pinned the old arithmetic, which divided
        a real byte count by a window too short to mean anything -- exactly the
        "plausible number" defect 15 exists to close.
        """
        row = await p.check_proxy(self.proxy, config(), p.Rate(0))
        self.assertEqual(row['speed']['state'], 'insufficient', row['speed'])
        self.assertIsNone(row['speed']['mbps'], row['speed'])
        self.assertIsNotNone(row['speed']['code'], row['speed'])
        self.assertGreaterEqual(row['speed']['bytes'], 100_000)
        self.assertEqual(self.requests[-1], b'http://speed.invalid/file')
        self.requests.clear()
        row = await p.check_proxy(self.proxy, config(speedtest=False), p.Rate(0))
        self.assertNotIn('speed', row)
        self.assertEqual(len(self.requests), 1)

    async def test_a_transfer_long_enough_to_measure_gives_a_number(self):
        """The other half: a real window still produces a real figure."""
        from proxy_workbench import probes
        trace = probes.TransferTrace(url='http://speed.invalid/file', connection='cold')
        trace.begin(0.0)
        trace.add(1 << 20, 0.10)
        trace.add(1 << 20, 0.40)
        trace.finish(0.40)
        measured = probes.measure_speed(trace)
        self.assertEqual(measured.state, 'ok')
        self.assertGreater(measured.mbps, 0)
        # And the number comes from the measured window, not from a byte count.
        # The window is first byte to last byte, 0.10 -> 0.40, never from the
        # start of the request: 2 MiB over 0.30 s.
        self.assertAlmostEqual(measured.mbps,
                               round((2 << 20) * 8 / 0.30 / 1e6, 2), places=1)

    async def test_failed_proxy_is_not_speed_tested(self):
        cfg = config()
        cfg['targets'][0]['contains'] = 'missing'
        row = await p.check_proxy(self.proxy, cfg, p.Rate(0))
        self.assertNotIn('speed', row)


class BandwidthSelectionTests(unittest.TestCase):
    def test_config_validation(self):
        args = SimpleNamespace(config=None, url='http://service.invalid/', attempts=1, timeout=8, max_bytes=1024,
                               request_profile=None, judge_url=None, connect_timeout=None, fail_fast=False,
                               min_success=1, speedtest_url='https://speed.invalid/f', speedtest_bytes=50_000)
        self.assertEqual(p.target_config(args)['speedtest'], {'url': 'https://speed.invalid/f', 'max_bytes': 50_000})
        self.assertNotIn('speedtest', p.target_config(SimpleNamespace(**dict(vars(args), speedtest_url=None))))
        for bad in ({'url': 'ftp://x/'}, {'url': 'https://speed.invalid/', 'max_bytes': 5}):
            with self.assertRaises(ValueError):
                p.validate_speedtest(bad)
        settings = gui.validate(dict(gui.defaults(), speedtest={'url': ' https://speed.invalid/f ', 'max_bytes': 100_000}))
        self.assertEqual(settings['speedtest'], {'url': 'https://speed.invalid/f', 'max_bytes': 100_000})

    def test_sort_export_and_api_filter(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            db = p.open_db(home / 'db.sqlite3')
            db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fx', json.dumps(dict(targets=[dict(url='https://one.invalid/')]))))
            for index, mbps in enumerate((2.5, None, 40.0)):
                row = dict(proxy=f'http://11.0.0.{index}:80', reliability=1, min_target_reliability=1, latency_ms=100 + index,
                           jitter_ms=1, score=90 - index, successes=1, requests=1, checked_at=_NOW, samples=[])
                if mbps is not None:
                    row['speed'] = {'mbps': mbps}
                add_candidate(db, (row['proxy']))
                store_result(db, ('fx', row['proxy'], json.dumps(row)))
            db.commit()
            p.export(db, 'fx', home / 'exports', min_success=1, sort='bandwidth')
            db.close()
            order = (home / 'exports' / 'proxies.txt').read_text().split()
            self.assertEqual(order, ['http://11.0.0.2:80', 'http://11.0.0.0:80', 'http://11.0.0.1:80'])
            self.assertIn(',40.0,', (home / 'exports' / 'ranked.csv').read_text())
            rows, _ = api.Exports(home / 'exports').load()
            query = api.parse_query('min_mbps=10')
            self.assertEqual([row['proxy'] for row in api.select(rows, query)], ['http://11.0.0.2:80'])


if __name__ == '__main__':
    unittest.main()

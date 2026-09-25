import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from tests.workbench_support import add_candidates  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import proxytool as p


def config():
    return dict(version=2, targets=[dict(url='http://service.invalid/', method='GET', statuses=[200], headers={},
                                         contains=None, sha256=None)], attempts=1, timeout=1, max_bytes=1024)


async def closed_ports(count):
    """Distinct local ports with nothing listening: bound together, then released."""
    servers = [await asyncio.start_server(lambda r, w: None, '127.0.0.1', 0) for _ in range(count)]
    ports = [server.sockets[0].getsockname()[1] for server in servers]
    for server in servers:
        server.close()
        await server.wait_closed()
    return ports


class PrefilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = p.open_db(Path(self.temp.name) / 'db.sqlite3')

        async def accept(reader, writer):
            writer.close()
        self.server = await asyncio.start_server(accept, '127.0.0.1', 0)
        self.live = f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'
        self.dead = [f'socks5://127.0.0.1:{port}' for port in await closed_ports(40)]
        add_candidates(self.db, ((proxy,) for proxy in [self.live, *self.dead]))
        self.db.commit()
        self.probed = []

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.db.close()
        self.temp.cleanup()

    async def probe(self, proxy, cfg, rate):
        self.probed.append(proxy)
        samples = [dict(ok=True, ms=5, target=0, attempt=1, status=200, error=None, bytes=1)]
        return p.summarize(proxy, samples, cfg)

    def rows(self):
        return {proxy: json.loads(payload) for proxy, payload in self.db.execute('SELECT proxy, payload FROM results')}

    async def test_only_reachable_addresses_reach_the_full_check(self):
        progress = []
        started = time.monotonic()
        await p.scan(self.db, config(), workers=2, rate=0, probe=self.probe, progress=False, min_success=1,
                     prefilter=16, prefilter_timeout=1, on_progress=progress.append)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.probed, [self.live])
        rows = self.rows()
        self.assertEqual(len(rows), 41)
        self.assertEqual({rows[proxy]['error'] for proxy in self.dead}, {'UNREACHABLE'})
        self.assertEqual(rows[self.live]['successes'], 1)
        self.assertEqual((progress[-1]['checked'], progress[-1]['passed'], progress[-1]['unreachable']), (41, 1, 40))
        # Resuming the same profile has nothing left to do.
        self.probed.clear()
        await p.scan(self.db, config(), workers=2, rate=0, probe=self.probe, progress=False, prefilter=16)
        self.assertEqual(self.probed, [])

    async def test_want_stops_both_stages(self):
        await p.scan(self.db, config(), workers=1, rate=0, probe=self.probe, progress=False, min_success=1,
                     prefilter=4, prefilter_timeout=1, want=1)
        self.assertEqual(self.probed, [self.live])
        self.assertLessEqual(len(self.rows()), 41)

    def test_fit_prefilter(self):
        self.assertEqual(p.fit_prefilter(128, 0), 0)
        self.assertGreaterEqual(p.fit_prefilter(8, 64), 1)
        self.assertLessEqual(p.fit_prefilter(8, 64), 64)
        args = p.parser().parse_args(['scan'])
        self.assertEqual((args.prefilter, args.prefilter_timeout), (512, 3))


if __name__ == '__main__':
    unittest.main()

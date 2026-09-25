import asyncio
import contextlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest

from tests.workbench_support import add_candidate, store_result  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import proxytool as p
from proxy_workbench import socks4

#: A result fixture describes a measurement that just happened; the
#: admission contract has no "fresh forever" state (CONTRACTS §2.4).
_NOW = time.time()


def scan_config(url):
    return dict(version=2, targets=[dict(url=url, method='GET', statuses=[200], headers={}, contains='healthy',
                                         sha256=None)], attempts=2, timeout=3, max_bytes=1024)


async def close(writer):
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


class Socks4Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def service(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nhealthy')
                await writer.drain()
            finally:
                await close(writer)
        self.service = await asyncio.start_server(service, '127.0.0.1', 0)
        self.service_port = self.service.sockets[0].getsockname()[1]
        self.requests = []
        self.grant = True

        async def proxy(reader, writer):
            try:
                header = await reader.readexactly(8)
                await reader.readuntil(b'\x00')
                version, command, port, address = struct.unpack('>BBH4s', header)
                self.requests.append((version, command, port, address))
                if not self.grant:
                    writer.write(b'\x00\x5b' + b'\x00' * 6)
                    await writer.drain()
                    return
                upstream_reader, upstream_writer = await asyncio.open_connection('127.0.0.1', port)
                writer.write(b'\x00\x5a' + b'\x00' * 6)
                await writer.drain()

                async def pipe(source, target):
                    with contextlib.suppress(OSError, asyncio.IncompleteReadError):
                        while data := await source.read(4096):
                            target.write(data)
                            await target.drain()
                    await close(target)
                await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
            finally:
                await close(writer)
        self.proxy = await asyncio.start_server(proxy, '127.0.0.1', 0)
        self.proxy_url = f'socks4://127.0.0.1:{self.proxy.sockets[0].getsockname()[1]}'

    async def asyncTearDown(self):
        for server in (self.proxy, self.service):
            server.close()
            await server.wait_closed()

    async def test_working_socks4_proxy_passes(self):
        cfg = scan_config(f'http://localhost:{self.service_port}/health')
        row = await p.check_proxy(self.proxy_url, cfg, p.Rate(0))
        self.assertEqual((row['successes'], row['requests']), (2, 2))
        # Hostnames are resolved locally because SOCKS4 only carries IPv4.
        self.assertEqual(self.requests[0], (4, 1, self.service_port, bytes([127, 0, 0, 1])))

    async def test_rejected_connect_fails_cleanly(self):
        self.grant = False
        row = await p.check_proxy(self.proxy_url, scan_config(f'http://127.0.0.1:{self.service_port}/'), p.Rate(0))
        self.assertEqual(row['successes'], 0)
        # httpx reports it like any other refused proxy.
        self.assertEqual(row['samples'][0]['error'], 'ProxyError')


class Socks4FormatTests(unittest.TestCase):
    def test_normalize_and_sources(self):
        self.assertEqual(p.normalize('socks4://8.8.8.8:1080'), 'socks4://8.8.8.8:1080')
        self.assertIsNone(p.normalize('socks4://[2001:4860::8888]:1080'))
        self.assertEqual(p.source_spec('socks4 https://example.org/list.txt'), ('socks4', 'https://example.org/list.txt'))
        self.assertEqual(p.proxy_protocol('socks4://8.8.8.8:1080'), 'socks4')

    def test_reply_parsing(self):
        socks4.check_reply(b'\x00\x5a' + b'\x00' * 6)
        for bad in (b'\x00\x5b' + b'\x00' * 6, b'\x05\x5a' + b'\x00' * 6, b'\x00\x5a'):
            with self.assertRaises(socks4.Socks4Error):
                socks4.check_reply(bad)
        self.assertEqual(socks4.connect_request(bytes([1, 2, 3, 4]), 443), b'\x04\x01\x01\xbb\x01\x02\x03\x04\x00')

    def test_export_writes_socks4_files(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            db = p.open_db(home / 'db.sqlite3')
            db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fx', json.dumps(dict(targets=[dict(url='https://one.invalid/')]))))
            row = dict(proxy='socks4://11.0.0.5:4145', reliability=1, min_target_reliability=1, latency_ms=100,
                       jitter_ms=1, score=90, successes=3, requests=3, checked_at=_NOW, samples=[])
            add_candidate(db, (row['proxy']))
            store_result(db, ('fx', row['proxy'], json.dumps(row)))
            db.commit()
            p.export(db, 'fx', home / 'out', min_success=1, protocol='socks4')
            db.close()
            self.assertEqual((home / 'out' / 'socks4.txt').read_text(), '11.0.0.5:4145\n')
            self.assertIn('socks4 11.0.0.5 4145', (home / 'out' / 'proxychains.txt').read_text())


if __name__ == '__main__':
    unittest.main()

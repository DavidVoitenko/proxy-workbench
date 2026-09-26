import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

import httpx
from tests.workbench_support import add_candidate  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import proxytool as p


class PinnedSourceTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_keeps_a_port_that_is_default_for_a_different_scheme(self):
        for url, hostname, expected in (
            ('http://lists.example:443/list', 'lists.example', 'lists.example:443'),
            ('https://lists.example:80/list', 'lists.example', 'lists.example:80'),
            ('https://lists.example:443/list', 'lists.example', 'lists.example'),
            ('http://[2001:db8::1]:443/list', '2001:db8::1', '[2001:db8::1]:443'),
        ):
            with self.subTest(url=url):
                captured = []

                async def respond(request):
                    captured.append(request)
                    return httpx.Response(200, content=b'list')

                transport = p.PinnedSourceTransport('127.0.0.1', hostname)
                await transport.transport.aclose()
                transport.transport = httpx.MockTransport(respond)
                async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
                    await client.get(url)
                self.assertEqual(captured[0].headers.get_list('host'), [expected])
                self.assertEqual(captured[0].url.host, '127.0.0.1')
                if url.startswith('https:'):
                    self.assertEqual(captured[0].extensions['sni_hostname'], hostname)

    async def test_sends_exactly_one_host_header(self):
        # Real sources are hostnames, so they always go through the pinned transport.
        heads = []

        async def handler(reader, writer):
            heads.append(await reader.readuntil(b'\r\n\r\n'))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 12\r\nConnection: close\r\n\r\n8.8.8.8:8080')
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            client = httpx.AsyncClient(transport=p.PinnedSourceTransport('127.0.0.1', 'lists.example'), trust_env=False)
            async with client:
                response = await client.get(f'http://lists.example:{port}/http.txt')
        self.assertEqual(response.text, '8.8.8.8:8080')
        host_lines = [line for line in heads[0].split(b'\r\n') if line.lower().startswith(b'host:')]
        self.assertEqual(host_lines, [f'Host: lists.example:{port}'.encode()])

    async def test_collect_through_a_hostname_source(self):
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            body = b'8.8.8.8:8080\n1.1.1.1:3128\n'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as temp:
            db = p.open_db(Path(temp) / 'db.sqlite3')
            # "localhost" is a hostname, so the source is resolved and pinned like a public one.
            async with server:
                report = await p.collect(db, [f'http://localhost:{port}/list'], [], allow_private_sources=True)
            count = db.execute('SELECT count(*) FROM candidates').fetchone()[0]
            db.close()
        self.assertEqual((count, report['sources'][0].get('error')), (2, None))


class MalformedSocksTests(unittest.IsolatedAsyncioTestCase):
    async def test_garbage_socks5_reply_does_not_stop_the_scan(self):
        async def garbage(reader, writer):
            await reader.read(3)
            writer.write(b'\x05\xff\x00junk')  # not a valid SOCKS5 greeting reply
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(garbage, '127.0.0.1', 0)
        proxy = f'socks5://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        cfg = dict(version=2, targets=[dict(url='http://service.invalid/', method='GET', statuses=[200], headers={},
                                            contains=None, sha256=None)], attempts=1, timeout=3, max_bytes=1024)
        with tempfile.TemporaryDirectory() as temp:
            db = p.open_db(Path(temp) / 'db.sqlite3')
            add_candidate(db, (proxy))
            db.commit()
            async with server:
                await p.scan(db, cfg, workers=1, rate=0, progress=False, prefilter=4)
            row = json.loads(db.execute('SELECT payload FROM results').fetchone()[0])
            db.close()
        self.assertEqual(row['successes'], 0)
        self.assertTrue(row['samples'][0]['error'])


if __name__ == '__main__':
    unittest.main()

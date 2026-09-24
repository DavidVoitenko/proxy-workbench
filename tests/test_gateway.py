import asyncio
import base64
import contextlib
import ipaddress
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import gateway


def row(proxy, latency=100, country=None):
    return dict(proxy=proxy, reliability=1, min_target_reliability=1, latency_ms=latency, jitter_ms=1, score=90,
                successes=3, requests=3, checked_at=0, samples=[], country=country)


async def close(writer):
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


async def splice(a_reader, a_writer, b_reader, b_writer):
    async def one(reader, writer):
        with contextlib.suppress(OSError):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
    await asyncio.gather(one(a_reader, b_writer), one(b_reader, a_writer))
    await close(a_writer)
    await close(b_writer)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.seen = []
        self.servers = []

        async def service(reader, writer):
            head = await reader.readuntil(b'\r\n\r\n')
            body = b'hello from ' + head.split(b' ')[1]
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
            await writer.drain()
            await close(writer)
        self.service_port = await self.listen(service)

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        self.temp.cleanup()

    async def listen(self, handler):
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def socks5_proxy(self):
        async def handler(reader, writer):
            greeting = await reader.readexactly(2)
            await reader.readexactly(greeting[1])
            writer.write(b'\x05\x00')
            _, _, _, kind = await reader.readexactly(4)
            if kind == 1:
                host = ipaddress.IPv4Address(await reader.readexactly(4)).compressed
            else:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            port = struct.unpack('>H', await reader.readexactly(2))[0]
            self.seen.append(('socks5', host, port))
            upstream = await asyncio.open_connection(host, port)
            writer.write(b'\x05\x00\x00\x01' + bytes(6))
            await splice(reader, writer, *upstream)
        return f'socks5://127.0.0.1:{await self.listen(handler)}'

    async def http_proxy(self):
        async def handler(reader, writer):
            head = await reader.readuntil(b'\r\n\r\n')
            method, target, _ = head.split(b'\r\n')[0].split(b' ')
            if method == b'CONNECT':
                host, port = target.decode().rsplit(':', 1)
                self.seen.append(('connect', host, int(port)))
                upstream = await asyncio.open_connection(host, int(port))
                writer.write(b'HTTP/1.1 200 OK\r\n\r\n')
                return await splice(reader, writer, *upstream)
            url = httpx.URL(target.decode())
            self.seen.append(('forward', target.decode(), b'connection: close' in head.lower()))
            up_reader, up_writer = await asyncio.open_connection(url.host, url.port)
            up_writer.write(head.replace(target, url.raw_path, 1))
            await splice(reader, writer, up_reader, up_writer)
        return f'http://127.0.0.1:{await self.listen(handler)}'

    async def dead_proxy(self):
        server = await asyncio.start_server(lambda r, w: None, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        return f'socks5://127.0.0.1:{port}'

    def publish(self, proxies):
        (self.home / 'exports').mkdir(exist_ok=True)
        (self.home / 'exports' / 'ranked.json').write_text(json.dumps([row(proxy) for proxy in proxies]))

    async def start(self, **options):
        server = await gateway.start(self.home, port=0, **options)
        self.servers.append(server)
        return server, f'127.0.0.1:{server.sockets[0].getsockname()[1]}'

    async def test_http_and_socks5_clients_rotate_over_upstreams(self):
        socks, http = await self.socks5_proxy(), await self.http_proxy()
        self.publish([socks, http])
        server, address = await self.start()
        url = f'http://127.0.0.1:{self.service_port}/page'
        async with httpx.AsyncClient(proxy=f'http://{address}', trust_env=False) as client:
            bodies = [(await client.get(url)).text for _ in range(2)]
        async with httpx.AsyncClient(proxy=f'socks5://{address}', trust_env=False) as client:
            bodies.append((await client.get(url)).text)
        self.assertEqual(bodies, ['hello from /page'] * 3)
        # Plain HTTP goes to an HTTP upstream as an absolute-form request, never as CONNECT;
        # the SOCKS5 client needs a tunnel, so its request may use CONNECT.
        self.assertEqual(sorted(entry[0] for entry in self.seen[:2]), ['forward', 'socks5'])
        self.assertIn(self.seen[2][0], ('connect', 'socks5'))
        self.assertTrue(all(entry[2] for entry in self.seen if entry[0] == 'forward'))
        self.assertEqual(server.gateway.pool.stats['connections'], 3)

    async def test_connect_tunnel(self):
        self.publish([await self.http_proxy()])
        _, address = await self.start()
        host, port = address.split(':')
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(f'CONNECT 127.0.0.1:{self.service_port} HTTP/1.1\r\n\r\n'.encode())
        self.assertIn(b' 200 ', await reader.readuntil(b'\r\n\r\n'))
        writer.write(b'GET /tunnel HTTP/1.1\r\nHost: x\r\n\r\n')
        self.assertTrue((await reader.read()).endswith(b'hello from /tunnel'))
        await close(writer)
        self.assertEqual(self.seen[0], ('connect', '127.0.0.1', self.service_port))

    async def test_failover_rests_dead_proxies(self):
        dead, alive = await self.dead_proxy(), await self.socks5_proxy()
        self.publish([dead, alive])
        server, address = await self.start()
        async with httpx.AsyncClient(proxy=f'http://{address}', trust_env=False) as client:
            for _ in range(4):
                self.assertEqual((await client.get(f'http://127.0.0.1:{self.service_port}/x')).status_code, 200)
        pool = server.gateway.pool
        self.assertIn(dead, pool.resting)
        self.assertEqual(pool.available(), [alive])

    async def test_no_proxies_and_auth(self):
        _, address = await self.start()
        async with httpx.AsyncClient(proxy=f'http://{address}', trust_env=False) as client:
            response = await client.get(f'http://127.0.0.1:{self.service_port}/')
        self.assertEqual(response.status_code, 502)
        self.publish([await self.socks5_proxy()])
        _, secured = await self.start(token='secret')
        async with httpx.AsyncClient(proxy=f'http://{secured}', trust_env=False) as client:
            self.assertEqual((await client.get(f'http://127.0.0.1:{self.service_port}/')).status_code, 407)
        async with httpx.AsyncClient(proxy=f'http://user:secret@{secured}', trust_env=False) as client:
            self.assertEqual((await client.get(f'http://127.0.0.1:{self.service_port}/')).status_code, 200)
        async with httpx.AsyncClient(proxy=f'socks5://user:wrong@{secured}', trust_env=False) as client:
            with self.assertRaises(httpx.ProxyError):
                await client.get(f'http://127.0.0.1:{self.service_port}/')
        with self.assertRaises(ValueError):
            await gateway.start(self.home, host='0.0.0.0', port=0)

    async def tagged_socks(self, tag):
        """A SOCKS5 upstream that records which client connection it served."""
        async def handler(reader, writer):
            greeting = await reader.readexactly(2)
            await reader.readexactly(greeting[1])
            writer.write(b'\x05\x00')
            _, _, _, kind = await reader.readexactly(4)
            if kind == 1:
                host = ipaddress.IPv4Address(await reader.readexactly(4)).compressed
            else:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            port = struct.unpack('>H', await reader.readexactly(2))[0]
            self.seen.append(tag)
            upstream = await asyncio.open_connection(host, port)
            writer.write(b'\x05\x00\x00\x01' + bytes(6))
            await splice(reader, writer, *upstream)
        return f'socks5://127.0.0.1:{await self.listen(handler)}'

    async def test_client_options_sessions_and_status(self):
        de, nl = await self.tagged_socks('DE'), await self.tagged_socks('NL')
        (self.home / 'exports').mkdir(exist_ok=True)
        (self.home / 'exports' / 'ranked.json').write_text(json.dumps([row(de, country='DE'), row(nl, country='NL')]))
        server, address = await self.start()
        url = f'http://127.0.0.1:{self.service_port}/'

        async def fetch(user, scheme='http'):
            async with httpx.AsyncClient(proxy=f'{scheme}://{user}:x@{address}', trust_env=False) as client:
                return (await client.get(url)).status_code
        for _ in range(3):
            self.assertEqual(await fetch('country-nl'), 200)
            self.assertEqual(await fetch('country-de', 'socks5'), 200)
        self.assertEqual(self.seen, ['NL', 'DE'] * 3)
        self.seen.clear()
        for _ in range(4):
            await fetch('session-alpha')
        self.assertEqual(len(set(self.seen)), 1)  # a session keeps its proxy
        self.assertEqual(await fetch('country-us'), 502)  # nothing matches
        self.assertEqual(await fetch('protocol-ftp'), 400)
        async with httpx.AsyncClient(trust_env=False) as client:
            status = (await client.get(f'http://{address}/status')).json()
        self.assertEqual((status['proxies'], status['sessions']), (2, 1))
        self.assertGreaterEqual(sum(item['ok'] for item in status['top']), 10)
        self.assertEqual(status['active'], 0)

    def test_client_options_parsing(self):
        self.assertEqual(gateway.client_options('user'), ({}, None))
        self.assertEqual(gateway.client_options('country-de_nl-protocol-SOCKS5-latency-800-anonymity-elite-session-s1'),
                         ({'countries': ('DE', 'NL'), 'protocol': 'socks5', 'max_latency': 800.0, 'anonymity': 'elite'}, 's1'))
        for bad in ('country-germany', 'protocol-https', 'latency-fast', 'session-a/b'):
            with self.assertRaises(ValueError):
                gateway.client_options(bad)

    def test_per_proxy_limit(self):
        pool = gateway.Pool(self.home, max_per_proxy=1)
        (self.home / 'exports').mkdir(exist_ok=True)
        (self.home / 'exports' / 'ranked.json').write_text(json.dumps([row('http://11.0.0.1:80'), row('http://11.0.0.2:80')]))
        first = pool.pick()
        pool.acquire(first)
        second = pool.pick()
        pool.acquire(second)
        self.assertNotEqual(first, second)
        self.assertIsNone(pool.pick())
        pool.release(first)
        self.assertEqual(pool.pick(), first)

    def test_pool_filters_and_skips_https_proxies(self):
        (self.home / 'exports').mkdir()
        rows = [row('http://11.0.0.1:80', country='DE'), row('https://11.0.0.2:443', country='DE'),
                row('socks4://11.0.0.3:4145', country='NL'), row('socks5://11.0.0.4:1080', latency=900, country='DE')]
        (self.home / 'exports' / 'ranked.json').write_text(json.dumps(rows))
        self.assertEqual(gateway.Pool(self.home).refresh(), ['http://11.0.0.1:80', 'socks4://11.0.0.3:4145',
                                                             'socks5://11.0.0.4:1080'])
        pool = gateway.Pool(self.home, dict(countries=('DE',), max_latency=500))
        self.assertEqual(pool.refresh(), ['http://11.0.0.1:80'])


if __name__ == '__main__':
    unittest.main()

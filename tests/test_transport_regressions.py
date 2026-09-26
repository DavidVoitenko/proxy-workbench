"""Production probe transports over local HTTP, TLS and SOCKS listeners."""
import asyncio
import base64
import hashlib
import ssl
import unittest
from unittest import mock

from proxy_workbench import probes, proxytool
from tests.gateway_support import GatewayCase, shutdown
from tests.test_gateway_transports import openssl_available, self_signed


class ProbeTransportTests(GatewayCase):
    def options(self):
        return probes.validate_options({})

    async def test_raw_probes_use_each_upstream_protocol(self):
        upstreams = [await self.http_upstream('relay')]
        upstreams.extend([await self.socks_upstream(scheme)
                          for scheme in ('socks4', 'socks5', 'socks5h')])
        for upstream in upstreams:
            with self.subTest(scheme=upstream.scheme):
                reader, writer = await proxytool.Transport(upstream.url)._open(
                    f'http://127.0.0.1:{self.target}/', 3)
                try:
                    writer.write(b'GET /probe HTTP/1.1\r\nHost: localhost\r\n\r\n')
                    await writer.drain()
                    answer = await asyncio.wait_for(reader.read(), 3)
                    self.assertIn(b'local target for /probe', answer)
                    self.assertEqual(upstream.connections, 1)
                finally:
                    await shutdown(writer)

    @unittest.skipUnless(openssl_available(), 'openssl is needed for a local TLS certificate')
    async def test_direct_tls_and_tls_to_proxy_are_both_negotiated(self):
        paths = self_signed(self.home)
        if paths is None:
            self.skipTest('local certificate generation unavailable')
        cert, key = paths
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        client_context = ssl.create_default_context(cafile=cert)

        async def origin(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nsecure')
                await writer.drain()
            finally:
                await shutdown(writer)

        port = await self.listen(origin, ssl=server_context)
        upstream = await self.http_upstream('relay', scheme='https', tls=server_context)
        with mock.patch.object(proxytool, 'TLS', client_context):
            for proxy in (None, upstream.url):
                reader, writer = await proxytool.Transport(proxy)._open(
                    f'https://127.0.0.1:{port}/', 3)
                try:
                    writer.write(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
                    await writer.drain()
                    self.assertIn(b'secure', await asyncio.wait_for(reader.read(), 3))
                finally:
                    await shutdown(writer)

    async def test_send_and_download_keep_an_unaligned_byte_budget(self):
        port = await self.target_service(body=b'x' * 131072)
        url = f'http://127.0.0.1:{port}/'
        transport = proxytool.Transport()
        response = await transport.send(probes.ProbeRequest(url=url, max_bytes=10003),
                                        options=self.options())
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.body), 10003)
        target = probes.validate_speed_target({'url': url, 'max_bytes': 100003})
        trace = await transport.download(target, options=self.options())
        self.assertEqual(trace.bytes, 100003)

    async def websocket(self, mode, scheme='http'):
        async def origin(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                headers = dict(line.split(b': ', 1) for line in head.split(b'\r\n')[1:] if b': ' in line)
                key = headers[b'Sec-WebSocket-Key']
                accept = base64.b64encode(hashlib.sha1(
                    key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                if mode == 'wrong_nonce':
                    accept = b'wrong'
                writer.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                             b'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                await writer.drain()
                if mode == 'wrong_nonce':
                    await reader.read()
                    return
                await reader.read(4096)
                if mode == 'oversized_frame':
                    # The size alone must stop the probe, without buffering the body.
                    writer.write(b'\x82\x7f' + (1 << 40).to_bytes(8, 'big'))
                else:
                    payload = b'wrong' if mode == 'wrong_pong' else b'ping0'
                    writer.write(bytes((0x8A, len(payload))) + payload)
                await writer.drain()
                await reader.read()
            finally:
                await shutdown(writer)

        port = await self.listen(origin)
        spec = probes.validate_capability({'kind': 'websocket', 'id': 'local-ws',
            'url': f'{scheme}://127.0.0.1:{port}/ws', 'budget': {'ping_timeout_s': 0.1}})
        return await probes.run_websocket(spec, self.options(), proxytool.Transport())

    async def test_wrong_handshake_nonce_is_never_admitted(self):
        result = await self.websocket('wrong_nonce')
        self.assertEqual(result.code, 'WS_HANDSHAKE_INVALID')
        self.assertNotEqual(result.state, 'ok')

    async def test_oversized_frame_stops_before_its_payload_arrives(self):
        result = await asyncio.wait_for(self.websocket('oversized_frame'), 2)
        self.assertEqual(result.code, 'BODY_TOO_LARGE')

    async def test_only_the_matching_pong_counts(self):
        rejected = await self.websocket('wrong_pong')
        accepted = await self.websocket('correct_pong')
        self.assertNotEqual(rejected.state, 'ok')
        self.assertEqual(accepted.state, 'ok')

    async def test_native_websocket_urls_reach_the_transport(self):
        result = await self.websocket('correct_pong', scheme='ws')
        self.assertEqual(result.state, 'ok')
        spec = probes.validate_capability({'kind': 'websocket', 'id': 'secure-ws',
                                           'url': 'wss://local.invalid/ws'})
        self.assertEqual(proxytool._tunnel_host_port(spec.url), ('local.invalid', 443))

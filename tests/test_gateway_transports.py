"""F16 transport parity: HTTP, CONNECT, HTTPS-to-proxy, SOCKS4, SOCKS4a, SOCKS5,
SOCKS5h, both DNS modes and IPv6.

Every upstream and every target is a loopback server started by this test, so a
check says what the gateway put on the wire without touching a public proxy.
"""
import asyncio
import contextlib
import ipaddress
import shutil
import socket
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path

from proxy_workbench import gateway
from tests.gateway_support import GatewayCase, shutdown


def openssl_available():
    return shutil.which('openssl') is not None


def self_signed(directory):
    """A throwaway certificate for 127.0.0.1, made locally by openssl."""
    result = subprocess.run(
        ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
         '-keyout', str(Path(directory) / 'key.pem'), '-out', str(Path(directory) / 'cert.pem'),
         '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
        capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return None
    return str(Path(directory) / 'cert.pem'), str(Path(directory) / 'key.pem')


class TransportTests(GatewayCase):
    async def test_plain_http_is_forwarded_in_absolute_form(self):
        up = await self.http_upstream('relay')
        self.publish([up.url])
        _server, address = await self.start()
        response = await self.get(address, '/page')
        self.assertEqual(response.status_code, 200)
        self.assertIn('page', response.text)
        self.assertEqual(up.targets, [('forward', f'http://127.0.0.1:{self.target}/page')])
        self.assertIn(b'connection: close', up.requests[0].lower())

    async def test_connect_tunnel_carries_bytes_untouched(self):
        up = await self.http_upstream('relay')
        self.publish([up.url])
        _server, address = await self.start()
        reader, writer = await self.http_client(
            address, b'CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n' % self.target)
        self.assertIn(b' 200 ', await reader.readuntil(b'\r\n\r\n'))
        writer.write(b'GET /tunneled HTTP/1.1\r\n\r\n')
        await writer.drain()
        self.assertIn(b'tunneled', await asyncio.wait_for(reader.read(4096), 5))
        self.assertEqual(up.targets, [('connect', '127.0.0.1', self.target)])
        await shutdown(writer)

    async def test_socks5_resolves_a_name_locally(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        _server, address = await self.start()
        original = gateway.resolve
        seen = []

        async def spy(host, port, family=socket.AF_UNSPEC):
            seen.append((host, port))
            return await original(host, port, family)

        gateway.resolve = spy
        try:
            granted, _reader, writer = await self.socks_client(address, target_host='localhost')
        finally:
            gateway.resolve = original
        self.assertTrue(granted)
        self.assertIn(('localhost', self.target), seen, 'socks5 resolves the name at the gateway')
        self.assertIn(up.targets[-1]['mode'], ('name-resolved-v4', 'name-resolved-v6'),
                      'socks5 sends an address, never the name')
        self.assertTrue(ipaddress.ip_address(up.targets[-1]['host']).is_loopback)
        await shutdown(writer)

    async def test_socks5h_lets_the_proxy_resolve_the_name(self):
        up = await self.socks_upstream('socks5h')
        self.publish([up.url])
        _server, address = await self.start()
        original = gateway.resolve
        seen = []

        async def spy(host, port, family=socket.AF_UNSPEC):
            seen.append((host, port))
            return await original(host, port, family)

        gateway.resolve = spy
        try:
            granted, _reader, writer = await self.socks_client(address, target_host='localhost')
        finally:
            gateway.resolve = original
        self.assertTrue(granted)
        self.assertEqual(seen, [], 'socks5h must not resolve the name at the gateway')
        self.assertEqual(up.targets[-1]['mode'], 'name-at-proxy')
        self.assertEqual(up.targets[-1]['host'], 'localhost')
        await shutdown(writer)

    async def test_socks5_accepts_a_literal_address_without_resolving(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        _server, address = await self.start()
        looked_up = []
        original = socket.getaddrinfo

        def spy(*args, **kwargs):
            looked_up.append(args[0] if args else None)
            return original(*args, **kwargs)

        socket.getaddrinfo = spy
        try:
            granted, _reader, writer = await self.socks_client(address, target_host='127.0.0.1')
        finally:
            socket.getaddrinfo = original
        self.assertTrue(granted)
        self.assertEqual(looked_up, [], 'a literal address must not trigger a name lookup')
        self.assertEqual(up.targets[-1]['mode'], 'name-resolved-v4')
        self.assertEqual(up.targets[-1]['host'], '127.0.0.1')
        await shutdown(writer)

    async def test_socks4_carries_a_dotted_quad(self):
        up = await self.socks_upstream('socks4')
        self.publish([up.url])
        _server, address = await self.start()
        granted, _reader, writer = await self.socks_client(address, target_host='127.0.0.1')
        self.assertTrue(granted)
        self.assertEqual(up.targets[-1]['mode'], 'socks4-ipv4')
        self.assertEqual(up.targets[-1]['host'], '127.0.0.1')
        await shutdown(writer)

    async def test_socks4a_carries_the_name(self):
        up = await self.socks_upstream('socks4a')
        self.publish([up.url])
        _server, address = await self.start()
        granted, _reader, writer = await self.socks_client(address, target_host='localhost')
        self.assertTrue(granted)
        self.assertEqual(up.targets[-1]['mode'], 'socks4a-name')
        self.assertEqual(up.targets[-1]['host'], 'localhost')
        await shutdown(writer)

    async def test_ipv6_target_over_socks5(self):
        if self.target6 is None:
            self.skipTest('no usable ::1 on this host')
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        _server, address = await self.start()
        granted, _reader, writer = await self.socks_client(address, target_host='::1',
                                                           target_port=self.target6)
        self.assertTrue(granted)
        self.assertEqual(up.targets[-1]['mode'], 'name-resolved-v6',
                         'an IPv6 literal is sent as ATYP 4, never as a name')
        self.assertEqual(up.targets[-1]['host'], '::1')
        await shutdown(writer)

    async def test_ipv6_target_over_connect_is_bracketed(self):
        if self.target6 is None:
            self.skipTest('no usable ::1 on this host')
        up = await self.http_upstream('relay')
        self.publish([up.url])
        _server, address = await self.start()
        reader, writer = await self.http_client(
            address, b'CONNECT [::1]:%d HTTP/1.1\r\n\r\n' % self.target6)
        self.assertIn(b' 200 ', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5))
        self.assertEqual(up.targets, [('connect', '::1', self.target6)])
        await shutdown(writer)

    async def test_the_gateway_itself_listens_on_ipv6(self):
        if self.target6 is None:
            self.skipTest('no usable ::1 on this host')
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server = await gateway.start(self.home, host='::1', port=0)
        self.addAsyncCleanup(self._stop, server)
        port = server.sockets[0].getsockname()[1]
        granted, _reader, writer = await self.socks_client(f'[::1]:{port}')
        self.assertTrue(granted)
        await shutdown(writer)

    async def test_every_socks_variant_reaches_its_own_handshake(self):
        # A transport in SUPPORTED that open_tunnel does not implement would
        # only fail in the field, so each one is driven end to end here.
        # https:// is covered by HttpsUpstreamTests below.
        expected = {'socks4': 'socks4-ipv4', 'socks4a': 'socks4a-name',
                    'socks5': 'name-resolved-v4', 'socks5h': 'name-at-proxy'}
        self.assertLessEqual(set(expected) | {'http', 'https'}, set(gateway.SUPPORTED))
        for scheme, mode in expected.items():
            with self.subTest(scheme=scheme):
                up = await self.socks_upstream(scheme)
                self.publish([up.url], generation=f'.generation-{scheme}8888')
                _server, address = await self.start()
                target = 'localhost' if scheme in ('socks4a', 'socks5h') else '127.0.0.1'
                granted, _reader, writer = await self.socks_client(address, target_host=target)
                self.assertTrue(granted, f'{scheme} did not complete its handshake')
                self.assertEqual(up.targets[-1]['mode'], mode)
                await shutdown(writer)

    async def test_an_unknown_scheme_is_refused(self):
        with self.assertRaises(gateway.UpstreamError) as caught:
            await gateway.open_tunnel('ftp://127.0.0.1:21', 'example.org', 80)
        self.assertEqual(str(caught.exception), 'UNSUPPORTED')


class HttpsUpstreamTests(GatewayCase):
    """HTTPS-to-proxy: the connection to the proxy itself is wrapped in TLS."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.certdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.certdir.cleanup)
        self.pair = self_signed(self.certdir.name) if openssl_available() else None

    def server_context(self):
        cert, key = self.pair
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        return context

    def client_context(self, check=True):
        cert, _key = self.pair
        context = ssl.create_default_context()
        if not check:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context.load_verify_locations(cafile=cert)
        return context

    async def test_https_upstream_carries_a_plain_request_over_tls(self):
        if not self.pair:
            self.skipTest('openssl is not available for a throwaway certificate')
        up = await self.http_upstream('relay', tls=self.server_context(), scheme='https')
        self.publish([up.url])
        server, address = await self.start()
        server.gateway.ssl_context = self.client_context()
        response = await self.get(address, '/secure')
        self.assertEqual(response.status_code, 200)
        self.assertIn('secure', response.text)
        self.assertEqual(up.targets[0][0], 'forward')

    async def test_https_upstream_carries_a_connect_tunnel(self):
        if not self.pair:
            self.skipTest('openssl is not available for a throwaway certificate')
        up = await self.http_upstream('relay', tls=self.server_context(), scheme='https')
        self.publish([up.url])
        server, address = await self.start()
        server.gateway.ssl_context = self.client_context()
        reader, writer = await self.http_client(
            address, b'CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n' % self.target)
        self.assertIn(b' 200 ', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10))
        writer.write(b'GET /viacerts HTTP/1.1\r\n\r\n')
        await writer.drain()
        self.assertIn(b'viacerts', await asyncio.wait_for(reader.read(4096), 5))
        await shutdown(writer)

    async def test_https_upstream_verifies_the_proxy_host_name(self):
        # The TLS handshake to an https:// upstream must verify the proxy host,
        # not the target the client asked for.
        seen = {}

        async def fake_open_connection(host, port, **kwargs):
            seen['host'] = host
            seen['port'] = port
            seen['ssl'] = kwargs.get('ssl')
            seen['server_hostname'] = kwargs.get('server_hostname')
            raise OSError('stop here')

        original = asyncio.open_connection
        asyncio.open_connection = fake_open_connection
        try:
            with self.assertRaises(OSError):
                await gateway.open_tunnel('https://proxy.example:8443', 'example.org', 443)
            self.assertEqual(seen['host'], 'proxy.example')
            self.assertEqual(seen['port'], 8443)
            self.assertIsInstance(seen['ssl'], ssl.SSLContext)
            self.assertEqual(seen['server_hostname'], 'proxy.example')
            self.assertTrue(seen['ssl'].check_hostname, 'verification stays on by default')

            seen.clear()
            with self.assertRaises(OSError):
                await gateway.open_tunnel('http://proxy.example:8080', 'example.org', 443)
            self.assertIsNone(seen['ssl'], 'a plain http:// upstream gets no TLS')
            self.assertIsNone(seen['server_hostname'])
        finally:
            asyncio.open_connection = original

    async def test_an_unverifiable_https_upstream_is_not_silently_used(self):
        if not self.pair:
            self.skipTest('openssl is not available for a throwaway certificate')
        up = await self.http_upstream('relay', tls=self.server_context(), scheme='https')
        self.publish([up.url])
        # The default context does not trust the throwaway certificate, so the
        # upstream must be counted as a fault instead of being talked to in clear.
        server, address = await self.start(max_failures=1, cooldown=300)
        response = await self.get(address, '/secure')
        self.assertEqual(response.status_code, 502)
        self.assertIn(up.url, server.gateway.pool.resting)


if __name__ == '__main__':
    unittest.main()

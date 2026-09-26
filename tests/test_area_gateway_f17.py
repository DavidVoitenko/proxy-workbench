"""F17 acceptance, end to end: the connection path a user is actually given.

Scenario 17 of the master prompt asks for three things that a page-level test
cannot prove: that the QR *decodes*, that the recipe fields really connect a
client, and that a loopback listener is not sold as a phone connection while a
LAN one is.

``tests/test_web_connect.py`` already covers the page's own strings.  This file
covers the part that only a live listener can answer: a real
``gateway.Background`` on a real interface, the exact bytes a client sends with
the published fields, and a QR that is encoded by the page's own encoder and
decoded back.
"""
import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import web_support as ws
from proxy_workbench import gateway

#: Literal fixtures, standing in for the two identities a phone must never get.
GUI_SESSION_TOKEN = 'gui-session-fixture-value'
API_TOKEN = 'api-token-fixture-value'
GATEWAY_TOKEN = 'gateway-fixture-password-4-phone'


def telegram_link(host, port, username, password):
    """The link the page builds, character for character."""
    return (f'tg://socks?server={quote(host, safe="")}&port={port}'
            f'&username={quote(username, safe="")}&password={quote(password, safe="")}')


def page_link(address, username, password):
    """Rebuild the link the way ``app.js`` does, from the published address."""
    host, _, port = address.rpartition(':')
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    return telegram_link(host, port, username, password)


class ListenerRecipeTests(unittest.IsolatedAsyncioTestCase):
    """The published fields, used as a client would, against a real upstream."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.target = await self.start_target()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def start_target(self, body=b'the real target'):
        async def handler(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n'
                             % len(body) + body)
                await writer.drain()
            except (asyncio.IncompleteReadError, OSError):
                pass
            finally:
                writer.close()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        self.addAsyncCleanup(self._stop, server)
        return server.sockets[0].getsockname()[1]

    @staticmethod
    async def _stop(server):
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()

    async def start_upstream(self):
        """An open HTTP upstream that records what it was asked for."""
        seen = []

        async def handler(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                seen.append(head)
                method, target = head.split(b'\r\n')[0].split(b' ')[:2]
                if method == b'CONNECT':
                    host, _, port = target.decode().rpartition(':')
                    upstream = await asyncio.open_connection(host.strip('[]'), int(port))
                    writer.write(b'HTTP/1.1 200 OK\r\n\r\n')
                    await writer.drain()
                else:
                    from urllib.parse import urlsplit
                    parts = urlsplit(target.decode())
                    upstream = await asyncio.open_connection(parts.hostname, parts.port)
                    upstream[1].write(head.replace(target, (parts.path or '/').encode(), 1))

                async def one(a, b):
                    with contextlib.suppress(OSError):
                        while data := await a.read(65536):
                            b.write(data)
                            await b.drain()
                await asyncio.gather(one(reader, upstream[1]), one(upstream[0], writer))
            except (asyncio.IncompleteReadError, OSError):
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        self.addAsyncCleanup(self._stop, server)
        return f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}', seen

    async def publish(self, proxies):
        from tests.gateway_support import write_export
        write_export(self.home, proxies)

    async def start_listener(self, **options):
        """A real listener on this test's loop, with the fields Background exposes."""
        server = await gateway.start(self.home, '127.0.0.1', 0, **options)
        self.addAsyncCleanup(self._stop_listener, server)
        return server

    @staticmethod
    async def _stop_listener(server):
        with contextlib.suppress(Exception):
            await server.gateway.shutdown(0)
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()

    async def test_the_published_recipe_really_connects_a_client(self):
        """Server, port, user name and password, exactly as published."""
        upstream, seen = await self.start_upstream()
        await self.publish([upstream])
        server = await self.start_listener(token=GATEWAY_TOKEN)
        state = server.gateway.state()
        self.assertEqual(state['bind']['host'], '127.0.0.1')
        self.assertFalse(state['reachable_from_lan'],
                         'a loopback listener is not a phone connection')
        self.assertEqual(state['authenticated'], True)

        address = f'127.0.0.1:{server.sockets[0].getsockname()[1]}'
        # Exactly the recipe the page shows: user name, password, address.
        import base64
        host, _, port = address.rpartition(':')
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(b'GET http://127.0.0.1:%d/recipe HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                     b'Proxy-Authorization: Basic %s\r\n\r\n'
                     % (self.target, base64.b64encode(f'workbench:{GATEWAY_TOKEN}'.encode())))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
        self.assertIn(b' 200 ', head)
        body = await asyncio.wait_for(reader.read(4096), 5)
        self.assertIn(b'the real target', body)
        self.assertEqual(len(seen), 1, 'the upstream really carried the request')
        writer.close()

    async def test_the_socks5_recipe_really_connects_a_client(self):
        upstream, seen = await self.start_upstream()
        await self.publish([upstream])
        server = await self.start_listener(token=GATEWAY_TOKEN)
        reader, writer = await asyncio.open_connection(
            '127.0.0.1', server.sockets[0].getsockname()[1])
        writer.write(b'\x05\x01\x02')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(2), 5), b'\x05\x02')
        writer.write(b'\x01' + len('workbench').to_bytes(1, 'big') + b'workbench'
                     + len(GATEWAY_TOKEN).to_bytes(1, 'big') + GATEWAY_TOKEN.encode())
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(2), 5), b'\x01\x00')
        writer.write(b'\x05\x01\x00\x01\x7f\x00\x00\x01' + int(self.target).to_bytes(2, 'big'))
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(10), 5)
        self.assertEqual(reply[1], 0, 'the tunnel was granted')
        self.assertEqual(reply[3], 1, 'the bound address is ATYP 1 (IPv4)')
        writer.write(b'GET /socks-recipe HTTP/1.1\r\nHost: x\r\n\r\n')
        await writer.drain()
        self.assertIn(b'the real target', await asyncio.wait_for(reader.read(4096), 5))
        self.assertEqual(len(seen), 1)
        writer.close()

    async def test_the_stored_binding_is_what_the_listener_serves(self):
        upstream, _seen = await self.start_upstream()
        await self.publish([upstream])
        binding = gateway.Binding(pool_id='picked-in-the-gui', policy={'max_per_proxy': 4})
        server = await self.start_listener(token=GATEWAY_TOKEN, binding=binding)
        # What `App.gateway_state()` would show, read from the listener.
        state = server.gateway.state()
        self.assertEqual(state['binding']['pool_id'], 'picked-in-the-gui')
        self.assertEqual(state['binding']['policy'], {'max_per_proxy': 4})
        applied = await server.gateway.aset_binding(gateway.Binding(pool_id='other'))
        self.assertEqual(applied['binding']['pool_id'], 'other')
        self.assertEqual(applied['rows'], 1, 'the new binding still serves the export')
        self.assertEqual(server.gateway.state()['binding']['pool_id'], 'other')


class LanRecipeTests(unittest.TestCase):
    """A real LAN listener: reachable, authenticated, and never the wildcard."""

    @classmethod
    def setUpClass(cls):
        cls.interfaces = gateway.lan_interfaces()
        if not cls.interfaces:
            raise unittest.SkipTest('this host has no private LAN interface to bind')
        cls.temp = tempfile.TemporaryDirectory()
        cls.background = gateway.Background(
            Path(cls.temp.name), '127.0.0.1', 0, token=GATEWAY_TOKEN, lan=True,
            interface=cls.interfaces[0])

    @classmethod
    def tearDownClass(cls):
        cls.background.close()
        cls.temp.cleanup()

    def test_the_lan_listener_binds_the_interface_and_publishes_it(self):
        state = self.background.state()
        self.assertTrue(state['bind']['lan'])
        self.assertEqual(state['bind']['listen_host'], self.interfaces[0])
        self.assertEqual(state['bind']['published_host'], self.interfaces[0])
        self.assertEqual(state['reachable_from_lan'], True)
        self.assertEqual(state['token_origin'], 'explicit')
        self.assertEqual(self.background.token, GATEWAY_TOKEN)
        self.assertNotEqual(self.background.token, GUI_SESSION_TOKEN)
        self.assertNotEqual(self.background.token, API_TOKEN)

    def test_it_answers_on_that_interface_and_not_only_on_loopback(self):
        import socket
        probe = socket.socket()
        probe.settimeout(5)
        with self.subTest('interface'):
            probe.connect((self.interfaces[0], self.background.port))
        probe.close()
        with self.subTest('loopback is refused while LAN is open'):
            # A LAN bind is bound to one adapter, so 127.0.0.1 is not it.
            refused = socket.socket()
            refused.settimeout(2)
            with self.assertRaises((ConnectionRefusedError, socket.timeout, OSError)):
                refused.connect(('127.0.0.1', self.background.port))
            refused.close()

    def test_the_published_state_never_carries_a_control_secret(self):
        state = self.background.state()
        body = json.dumps(state, default=str)
        for secret in (GUI_SESSION_TOKEN, API_TOKEN):
            self.assertNotIn(secret, body)
        self.assertIn(GATEWAY_TOKEN, str(self.background.token))


@unittest.skipIf(shutil.which('node') is None, 'node is not available for the page encoder')
class QrOfALiveListenerTests(unittest.TestCase):
    """The QR is rendered by the page's own encoder and decoded back."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_a_loopback_listener_publishes_no_qr_and_says_why(self):
        background = gateway.Background(Path(self.temp.name), '127.0.0.1', 0, token=GATEWAY_TOKEN)
        self.addCleanup(background.close)
        state = background.state()
        # `mobile_ready` in App.gateway_state() is exactly this test: the bind
        # address and the published address are both off loopback and there is
        # a password.  A loopback listener fails it, and so does the flag the
        # listener itself reports.
        mobile_ready = (not gateway.is_loopback(background.host)
                        and not gateway.is_loopback(background.display_host)
                        and bool(background.token))
        self.assertFalse(mobile_ready, 'a loopback gateway is not a phone connection')
        self.assertFalse(state['reachable_from_lan'])
        self.assertEqual(state['bind']['published_host'], '127.0.0.1')

    def test_a_lan_qr_decodes_back_to_the_published_fields(self):
        interfaces = gateway.lan_interfaces()
        if not interfaces:
            self.skipTest('this host has no private LAN interface to bind')
        background = gateway.Background(Path(self.temp.name), '127.0.0.1', 0,
                                       token=GATEWAY_TOKEN, lan=True)
        self.addCleanup(background.close)
        host = background.display_host
        address = f'[{host}]:{background.port}' if ':' in host else f'{host}:{background.port}'
        link = page_link(address, 'workbench', GATEWAY_TOKEN)
        decoded = ws.decode_qr(ws.make_qr(link))
        self.assertEqual(decoded, link, 'the QR decodes back to the same link')
        self.assertIn(f'server={quote(host, safe="")}', decoded)
        self.assertIn(f'port={background.port}', decoded)
        self.assertIn('username=workbench', decoded)
        for secret in (GUI_SESSION_TOKEN, API_TOKEN):
            self.assertNotIn(secret, decoded, 'a control secret must never be in a QR')

    def test_a_password_with_url_characters_survives_the_qr(self):
        # The gateway password is generated, so it can contain anything
        # token_urlsafe produces, and the link is percent-encoded.
        odd = 'a+b/c=d&e f'
        link = telegram_link('192.168.0.41', '8899', 'workbench', odd)
        self.assertEqual(ws.decode_qr(ws.make_qr(link)), link)
        self.assertIn('password=a%2Bb%2Fc%3Dd%26e%20f', link)


if __name__ == '__main__':
    unittest.main()

"""Defect 18 / R12 / CONTRACTS §5.1: the gateway password is its own identity and LAN is opt-in.

The old code let the GUI hand its session token to the gateway listener and
bound ``0.0.0.0`` by default, so the secret printed into a phone QR was also the
secret that controlled the local UI.  These tests keep the three identities
apart, keep loopback as the default, and check that the LAN choice is visible.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from proxy_workbench import gateway
from tests.gateway_support import GatewayCase, write_export

#: Values that stand in for the other two identities.  They are literal test
#: fixtures, not credentials, and they exist only to prove separation.
GUI_SESSION_TOKEN = 'gui-session-fixture-value'
API_TOKEN = 'api-token-fixture-value'


class BindTests(unittest.TestCase):
    def test_a_non_loopback_address_needs_an_explicit_opt_in(self):
        with self.assertRaises(ValueError):
            gateway.Bind(host='0.0.0.0')
        with self.assertRaises(ValueError):
            gateway.Bind(host='192.168.1.5')
        # Loopback needs no ceremony and is the default.
        self.assertEqual(gateway.Bind().host, '127.0.0.1')
        self.assertFalse(gateway.Bind().lan)
        self.assertTrue(gateway.Bind(host='0.0.0.0', lan=True).lan)

    def test_a_lan_interface_cannot_be_loopback(self):
        with self.assertRaises(ValueError):
            gateway.Bind(host='0.0.0.0', lan=True, interface='127.0.0.1')

    def test_the_wildcard_address_is_never_the_published_target(self):
        bind = gateway.Bind(host='0.0.0.0', lan=True, interface='192.168.1.5')
        self.assertEqual(bind.published_host, '192.168.1.5', 'the chosen interface is what a phone dials')
        self.assertNotIn(bind.published_host, ('0.0.0.0', '::', ''))
        self.assertEqual(gateway.Bind().as_dict()['published_host'], '127.0.0.1')

    def test_lan_interfaces_are_private_and_never_loopback(self):
        for address in gateway.lan_interfaces():
            self.assertNotIn(address, ('0.0.0.0', '::'))
            self.assertFalse(gateway.is_loopback(address))

    def test_generated_tokens_are_fresh_and_differ_from_the_other_identities(self):
        tokens = {gateway.new_gateway_token() for _ in range(8)}
        self.assertEqual(len(tokens), 8, 'a generated gateway password must be fresh every time')
        for token in tokens:
            self.assertTrue(token)
            self.assertNotIn(token, (GUI_SESSION_TOKEN, API_TOKEN))
            self.assertGreaterEqual(len(token), 22, 'the phone password needs real entropy')

    def test_contradictory_lan_arguments_are_refused(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ValueError):
                    await gateway.start(Path(temp), host='0.0.0.0', port=0, lan=False)
                with self.assertRaises(ValueError):
                    await gateway.start(Path(temp), bind=gateway.Bind(host='0.0.0.0', lan=True), lan=False)
                server = await gateway.start(Path(temp), port=0, lan=True)
                server.close()
        asyncio.run(run())


class LanBackgroundTests(unittest.TestCase):
    """One short, authenticated LAN bind, isolated from every other test.

    The port is ephemeral and the password is required, so nothing on the
    network can use it while it exists.
    """

    def test_lan_listener_gets_its_own_password_and_keeps_the_state_visible(self):
        with tempfile.TemporaryDirectory() as temp:
            background = gateway.Background(Path(temp), '0.0.0.0', 0, token='lan-fixture-password', lan=True)
            try:
                self.assertTrue(background.lan)
                self.assertEqual(background.token, 'lan-fixture-password')
                self.assertEqual(background.token_origin, 'explicit')
                self.assertNotEqual(background.token, GUI_SESSION_TOKEN)
                self.assertNotEqual(background.token, API_TOKEN)
                state = background.state()
                self.assertTrue(state['authenticated'])
                self.assertEqual(state['bind']['lan'], True)
                self.assertNotIn('0.0.0.0', (state['bind']['published_host'],))
                # The visible state is what a GUI or an API would show: no secret.
                self.assertNotIn(background.token, json.dumps(state, default=str))
                self.assertIsInstance(state['interfaces'], list)
            finally:
                background.close()
            self.assertIsNotNone(background.shutdown_report)

    def test_a_lan_listener_without_a_password_makes_its_own(self):
        # Checked without binding anything: the decision is what matters, and a
        # test should not open a socket other machines on the LAN can reach.
        token, origin = gateway.resolve_token(gateway.Bind(host='0.0.0.0', lan=True))
        self.assertEqual(origin, 'generated')
        self.assertTrue(token)
        self.assertNotIn(token, (GUI_SESSION_TOKEN, API_TOKEN))
        other, _ = gateway.resolve_token(gateway.Bind(host='0.0.0.0', lan=True))
        self.assertNotEqual(token, other)
        explicit, origin = gateway.resolve_token(gateway.Bind(host='0.0.0.0', lan=True),
                                                 'lan-fixture-password')
        self.assertEqual((explicit, origin), ('lan-fixture-password', 'explicit'))
        # A loopback listener keeps the old behaviour: no invented password.
        self.assertEqual(gateway.resolve_token(gateway.Bind()), (None, 'none'))

    def test_local_listener_stays_unauthenticated_and_on_loopback(self):
        with tempfile.TemporaryDirectory() as temp:
            background = gateway.Background(Path(temp), '127.0.0.1', 0)
            try:
                self.assertEqual(background.host, '127.0.0.1')
                self.assertFalse(background.lan)
                self.assertIsNone(background.token)
                self.assertEqual(background.state()['token_origin'], 'none')
            finally:
                background.close()


class AuthenticationTests(GatewayCase):
    async def test_a_password_is_required_from_every_client_of_a_bound_listener(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server, address = await self.start(token='lan-fixture-password')
        host, _, port = address.rpartition(':')
        for attempt in (b'', b'wrong'):
            reader, writer = await self.http_client(
                address, b'GET http://127.0.0.1:%d/ HTTP/1.1\r\n\r\n' % self.target,
                auth=b'workbench:' + attempt)
            self.assertIn(b' 407 ', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5))
            writer.close()
        reader, writer = await self.http_client(
            address, b'GET http://127.0.0.1:%d/ HTTP/1.1\r\n\r\n' % self.target,
            auth=b'workbench:lan-fixture-password')
        self.assertIn(b' 200 ', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5))
        writer.close()

    async def test_socks5_client_on_a_lan_listener_must_authenticate(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server, address = await self.start(token='lan-fixture-password')
        granted, _reader, writer = await self.socks_client(address, password=b'wrong')
        self.assertFalse(granted, 'a wrong SOCKS5 password must not open a tunnel')
        writer.close()
        granted, _reader, writer = await self.socks_client(address, password=b'lan-fixture-password')
        self.assertTrue(granted)
        writer.close()

    async def test_gateway_state_exposes_no_password(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server, address = await self.start(token='lan-fixture-password')
        state = server.gateway.state()
        self.assertEqual(state['token_origin'], 'explicit')
        self.assertTrue(state['authenticated'])
        self.assertNotIn('lan-fixture-password', json.dumps(state, default=str))
        self.assertNotIn('token', json.dumps(state, default=str).replace('"token_origin"', ''))


if __name__ == '__main__':
    unittest.main()

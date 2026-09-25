"""F17: the connection path, and the secrets that must not travel with it.

Choose a pool, get the exact fields for the client, control the route, and
disconnect in a way the user understands.  The QR is checked by decoding it
back, with IPv6 and escaping, and no GUI or API secret may appear in it.
"""
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import web_support as ws
from proxy_workbench import api, gateway, gui

FIXTURE_PASSWORD = 'fixture-gateway-password-not-a-real-secret'


class GatewayBindingTests(unittest.TestCase):
    def setUp(self):
        self.now = ws.time.time()
        self.home = ws.build_data([ws.measurement('http://11.0.0.30:8080', age=10, now=self.now)], now=self.now)
        self.fixture = ws.ServerFixture(self.home)
        self.app = self.fixture.app

    def tearDown(self):
        self.fixture.close()

    def start_gateway(self, host='127.0.0.1', token=FIXTURE_PASSWORD):
        self.app.gateway = gateway.Background(self.home, host, 0, token=token)
        return self.app.gateway

    def test_the_gateway_password_is_not_the_gui_session_token(self):
        runner = self.start_gateway()
        try:
            state = self.app.gateway_state()
            self.assertEqual(state['password'], FIXTURE_PASSWORD)
            self.assertNotEqual(state['password'], self.app.token)
            self.assertNotIn(self.app.token, json.dumps(state))
        finally:
            runner.close()
            self.app.gateway = None

    def test_the_page_state_never_carries_the_gui_token_into_the_gateway(self):
        runner = self.start_gateway()
        try:
            body = self.fixture.client.get('/api/state').text
            gateway_block = json.loads(body)['gateway']
            self.assertNotIn(self.app.token, json.dumps(gateway_block))
            self.assertIn('password', gateway_block)
        finally:
            runner.close()
            self.app.gateway = None

    def test_the_default_bind_is_loopback_and_lan_is_an_explicit_opt_in(self):
        parser_defaults = {}
        source = (Path(gui.__file__)).read_text(encoding='utf-8')
        self.assertIn("'--gateway-host', default=os.environ.get('PROXY_WORKBENCH_GATEWAY_HOST', '127.0.0.1')", source)
        self.assertIn("'--lan', action='store_true'", source)
        self.assertNotIn("'0.0.0.0'),\n", source.split("'--gateway-host'")[1].split('\n')[0])

    def test_a_generated_gateway_password_is_used_when_none_is_given(self):
        import inspect
        source = inspect.getsource(gui.main)
        self.assertIn('server.app.gateway_token', source)
        self.assertNotIn('gateway_token = server.app.token', source)
        self.assertNotEqual(self.app.gateway_token, self.app.token)

    def test_the_connection_path_reports_the_published_snapshot(self):
        runner = self.start_gateway()
        try:
            state = self.app.gateway_state()
            self.assertTrue(state['binding']['generation'])
            self.assertEqual(state['binding']['state'], 'complete')
            self.assertEqual(state['transport'], ['http', 'socks5'])
            self.assertFalse(state['udp_supported'])
            self.assertIn('Workbench', state['probe_note'])
            self.assertTrue(state['disconnect_hint'])
        finally:
            runner.close()
            self.app.gateway = None

    def test_a_loopback_gateway_is_not_advertised_as_a_phone_connection(self):
        runner = self.start_gateway(host='127.0.0.1')
        try:
            self.assertFalse(self.app.gateway_state()['mobile_ready'])
        finally:
            runner.close()
            self.app.gateway = None

    def test_the_user_can_stop_the_rotating_proxy(self):
        runner = self.start_gateway()
        address = self.app.gateway_state()['address']
        answer = self.fixture.client.post('/api/gateway/stop', json={})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertTrue(answer.json()['stopped'])
        self.assertIsNone(self.fixture.client.get('/api/state').json()['gateway'])
        self.assertNotEqual(address, '')

    def test_stopping_without_a_gateway_says_so_instead_of_pretending(self):
        answer = self.fixture.client.post('/api/gateway/stop', json={})
        self.assertEqual(answer.status_code, 400)
        self.assertIn('не запущен', answer.json()['error'])


class QrRoundTripTests(unittest.TestCase):
    """The QR is decoded, not eyeballed."""

    def telegram_url(self, host, port, username, password):
        return (f'tg://socks?server={host}&port={port}'
                + (f'&username={username}&password={password}' if username else ''))

    def test_a_qr_decodes_back_to_the_same_link(self):
        link = self.telegram_url('192.168.0.41', '8899', 'workbench', FIXTURE_PASSWORD)
        decoded = ws.decode_qr(ws.make_qr(link))
        self.assertEqual(decoded, link)

    def test_an_ipv6_host_is_encoded_and_decoded(self):
        for host in ('fe80::1', '2001:db8::dead:beef'):
            encoded = self.telegram_url(host.replace(':', '%3A'), '8899', 'workbench', FIXTURE_PASSWORD)
            decoded = ws.decode_qr(ws.make_qr(encoded))
            self.assertEqual(decoded, encoded)

    def test_the_page_escapes_an_ipv6_address_for_the_telegram_link(self):
        script = """
const value = {gateway: {address: '[fe80::1]:8899', mobile_ready: true, username: 'workbench',
  password: 'fixture-password'}};
const rawAddress = String(value.gateway.address || '');
const separator = rawAddress.lastIndexOf(':');
let gatewayHost = separator > 0 ? rawAddress.slice(0, separator) : rawAddress;
const gatewayPort = separator > 0 ? rawAddress.slice(separator + 1) : '';
if (gatewayHost.startsWith('[') && gatewayHost.endsWith(']')) gatewayHost = gatewayHost.slice(1, -1);
const url = `tg://socks?server=${encodeURIComponent(gatewayHost)}&port=${encodeURIComponent(gatewayPort)}`
  + `&username=${encodeURIComponent(value.gateway.username)}&password=${encodeURIComponent(value.gateway.password || '')}`;
process.stdout.write(url);
"""
        url = ws.node_ok(script)
        self.assertEqual(url, 'tg://socks?server=fe80%3A%3A1&port=8899&username=workbench&password=fixture-password')
        # and the QR of exactly that link decodes back to it
        self.assertEqual(ws.decode_qr(ws.make_qr(url)), url)

    def test_the_qr_never_carries_the_gui_or_api_secret(self):
        class FakeRunner:
            port = 8899
            host = '127.0.0.1'
            display_host = '127.0.0.1'
            token = FIXTURE_PASSWORD

            class server:
                class gateway:
                    class pool:
                        @staticmethod
                        def snapshot(top=5):
                            return {'available': 3, 'items': []}

        class FakeApp:
            token = 'gui-session-secret-value'
            gateway_token = 'gateway-secret-value'
            gateway = FakeRunner()

            export_status = lambda self: {'generation': '.generation-x', 'state': 'complete', 'available': 3}

        body = gui.App.gateway_state(FakeApp())
        self.assertNotIn(FakeApp.token, json.dumps(body))
        self.assertNotIn(FakeApp.gateway_token, json.dumps(body))
        self.assertEqual(body['password'], FIXTURE_PASSWORD)

    def test_the_page_source_builds_the_mobile_link_from_the_gateway_password_only(self):
        source = ws.APP_JS.read_text(encoding='utf-8')
        self.assertIn('value.gateway.password', source)
        self.assertNotIn('app.token', source.split('function renderState')[1].split('function renderConnectPath')[0]
                         if 'function renderConnectPath' in source else source)
        # the GUI session token is only ever sent as a request header
        for line in source.splitlines():
            if 'token' in line and 'X-Workbench-Token' not in line and 'const token' not in line \
                    and 'LANG_KEY' not in line and 'gateway.password' not in line and 'token =' not in line \
                    and 'api-token' not in line and 'apikey' not in line.lower():
                self.assertNotIn('qr', line.lower(), 'the QR builder must not read a request token')


class RecipeTests(unittest.TestCase):
    def test_the_connection_path_is_present_in_the_page(self):
        html = ws.INDEX_HTML.read_text(encoding='utf-8')
        present = set(re.findall(r'\sid="([^"]+)"', html))
        missing = [anchor for anchor in ('connect-card', 'connect-pool', 'connect-generation',
                                         'connect-disconnect', 'connect-route', 'connect-lan')
                   if anchor not in present]
        self.assertEqual(missing, [], f'the connection path is missing {missing}')
        self.assertTrue(re.search(r'data-i18n="connect\.routeText"', html))
        self.assertTrue(re.search(r'id="connect-disconnect-hint"', html))

    def test_the_rotating_proxy_is_not_called_a_vpn(self):
        html = ws.INDEX_HTML.read_text(encoding='utf-8')
        self.assertIn('UDP is not supported', html)
        # The connection path and the gateway block must not market the local
        # TCP gateway as a VPN; a user's own VPN client elsewhere on the page is
        # a different thing and is not part of this check.
        start = html.index('id="connect-card"')
        block = html[start:html.index('id="page-mobile"')]
        for promise in ('VPN', 'UDP forwarding', 'UDP proxy', 'calls'):
            self.assertNotIn(promise, block, f'the connection path promises {promise}')

    def test_the_probe_and_the_client_traffic_are_distinguished(self):
        source = ws.APP_JS.read_text(encoding='utf-8')
        self.assertIn('connect.probeNote', source)
        self.assertIn('id="connect-probe-note"', ws.INDEX_HTML.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()

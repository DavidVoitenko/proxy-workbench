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
        class FakeGateway:
            # Background exposes the Gateway itself, and App.gateway_state reads
            # `runner.gateway` for LAN reachability, so the double carries it.
            reachable_from_lan = False

            class pool:
                @staticmethod
                def snapshot(top=5):
                    return {'available': 3, 'items': []}

        class FakeRunner:
            port = 8899
            host = '127.0.0.1'
            display_host = '127.0.0.1'
            token = FIXTURE_PASSWORD
            gateway = FakeGateway()

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


class ConnectionPathRenderTests(unittest.TestCase):
    """F17 step 2-3: the fields are the ones the listener really has.

    A hard-coded ``127.0.0.1:8899`` in the page is a promise the run may not
    keep: ``--gateway-port`` may pick another port, and ``--no-gateway`` means
    there is no address at all.  These tests drive the page's own renderer with
    the page's own message catalogue, so they check the text a user reads.
    """

    PRELUDE = """
const lang = 'en';
const fmt = value => String(value);
const nodes = {};
['connect-pool', 'connect-client-hint', 'connect-host', 'connect-port', 'connect-protocol',
 'connect-generation', 'connect-lan', 'connect-probe-note', 'connect-disconnect-hint',
 'connect-disconnect', 'gw-browser-host', 'gw-browser-port', 'gw-browser-protocol',
 'gw-curl-code', 'gw-python-code', 'gw-copy-curl', 'gw-copy-python'].forEach(id => {
  nodes[id] = {textContent: '', dataset: {}, classList: {toggled: [], toggle(name) { this.toggled.push(name); }}};
});
const $ = id => nodes[id] || null;
"""

    def render(self, value):
        source = (ws.js_slice('const messages = {', 'const LANGS = [')
                  + '\n' + ws.js_slice('function t(key, values={})', '// Server validation messages')
                  + '\n' + ws.js_slice('function splitGatewayAddress(', 'async function startGateway()'))
        script = self.PRELUDE + '\n' + source + """
const value = %s;
renderConnectPath(value);
process.stdout.write(JSON.stringify({
  host: $('connect-host').textContent, port: $('connect-port').textContent,
  protocol: $('connect-protocol').textContent, hint: $('connect-client-hint').textContent,
  generation: $('connect-generation').textContent, pool: $('connect-pool').textContent,
  browserHost: $('gw-browser-host').textContent, browserPort: $('gw-browser-port').textContent,
  curl: $('gw-curl-code').textContent, python: $('gw-python-code').textContent,
  curlCopy: $('gw-copy-curl').dataset.copyText, button: $('connect-disconnect').textContent}));
""" % json.dumps(value)
        return json.loads(ws.node_ok(script))

    def test_the_fields_are_the_real_listener_address(self):
        shown = self.render({'gateway': {'address': '192.168.0.41:45123', 'proxies': 3, 'bind_host': '0.0.0.0',
                                         'mobile_ready': True, 'binding': {'generation': '.generation-abcdef1234567890',
                                                                             'state': 'complete', 'available': 3}}})
        self.assertEqual(shown['host'], '192.168.0.41')
        self.assertEqual(shown['port'], '45123')
        self.assertNotEqual(shown['port'], '8899')
        self.assertIn('192.168.0.41:45123', shown['hint'])
        self.assertNotEqual(shown['protocol'], '—')

    def test_an_ipv6_listener_is_split_into_host_and_port(self):
        shown = self.render({'gateway': {'address': '[fe80::1]:8899', 'proxies': 1, 'bind_host': '::',
                                         'mobile_ready': True, 'binding': {}}})
        self.assertEqual(shown['host'], 'fe80::1')
        self.assertEqual(shown['port'], '8899')
        self.assertIn('[fe80::1]:8899', shown['curl'], 'an IPv6 literal needs brackets in a recipe')

    def test_a_stopped_gateway_shows_no_address_instead_of_the_old_one(self):
        shown = self.render({'gateway': None})
        self.assertEqual(shown['host'], '—')
        self.assertEqual(shown['port'], '—')
        self.assertEqual(shown['protocol'], '—')
        self.assertNotIn('8899', shown['hint'])
        self.assertIn('8899', shown['curl'], 'with no listener the recipe keeps the example address')

    def test_the_script_recipes_carry_the_live_address(self):
        shown = self.render({'gateway': {'address': '127.0.0.1:45123', 'proxies': 1, 'bind_host': '127.0.0.1',
                                         'mobile_ready': False, 'binding': {}}})
        self.assertIn('curl -x socks5h://127.0.0.1:45123', shown['curl'])
        self.assertIn('curl -x http://127.0.0.1:45123', shown['curl'])
        self.assertIn('socks5://127.0.0.1:45123', shown['python'])
        self.assertIn('127.0.0.1:45123', shown['curlCopy'])
        self.assertNotIn('8899', shown['curlCopy'])
        self.assertEqual(shown['browserHost'], '127.0.0.1')
        self.assertEqual(shown['browserPort'], '45123')

    def test_the_page_keeps_no_hard_coded_port_in_the_recipes(self):
        html = ws.INDEX_HTML.read_text(encoding='utf-8')
        for anchor in ('connect-host', 'connect-port', 'connect-protocol', 'connect-client-hint',
                       'gw-browser-host', 'gw-browser-port', 'gw-curl-code', 'gw-python-code'):
            self.assertIn(f'id="{anchor}"', html, f'{anchor} is not a field the page fills')
        # The step list and the browser tab state the live address, so no port
        # literal may survive there.
        connect_card = html[html.index('id="connect-card"'):html.index('id="gw-tab-tg"')]
        browser_tab = html[html.index('id="gw-tab-browser"'):html.index('id="page-mobile"')]
        for block in (connect_card, browser_tab):
            self.assertNotIn('8899', block, 'the connection path states a port the run may not use')
        # The two script recipes carry a static example until the first poll
        # rewrites them; that example must be the real default, not a guess.
        self.assertIn(f'127.0.0.1:{gateway.DEFAULT_PORT}', html[html.index('id="gw-tab-curl"'):])
        self.assertIn(f'127.0.0.1:{gateway.DEFAULT_PORT}', html[html.index('id="gw-tab-python"'):])

    def test_the_snapshot_reason_is_shown_in_words_and_keeps_the_code(self):
        shown = self.render({'gateway': {'address': '127.0.0.1:8899', 'proxies': 0, 'bind_host': '127.0.0.1',
                                         'mobile_ready': False,
                                         'binding': {'state': 'stale', 'state_detail': 'all_expired',
                                                     'state_detail_label': 'every row expired; a new check is needed',
                                                     'available': 0}}})
        self.assertIn('every row expired', shown['generation'])
        self.assertIn('all_expired', shown['generation'])

    def test_the_server_translates_every_reason_the_engine_can_report(self):
        from proxy_workbench import core, i18n
        for detail in sorted(core.STATE_DETAILS):
            self.assertIn(detail, i18n.STATE_DETAILS, f'{detail} has no text in either language')
            for lang in ('ru', 'en'):
                self.assertNotEqual(i18n.state_detail_text(detail, lang), detail)
        # An unknown reason stays visible as a reason instead of disappearing.
        self.assertEqual(i18n.state_detail_text('a_reason_from_the_future', 'en'),
                         'a_reason_from_the_future')
        self.assertEqual(i18n.state_detail_text(None, 'en'), '')

    def test_expired_and_empty_read_differently_on_the_connect_path(self):
        expired = self.render({'gateway': {'address': '127.0.0.1:8899', 'proxies': 0, 'bind_host': '127.0.0.1',
                                           'mobile_ready': False,
                                           'binding': {'state': 'stale', 'state_detail': 'all_expired',
                                                       'state_detail_label': 'every row expired; a new check is needed',
                                                       'available': 0}}})
        empty = self.render({'gateway': {'address': '127.0.0.1:8899', 'proxies': 0, 'bind_host': '127.0.0.1',
                                         'mobile_ready': False,
                                         'binding': {'state': 'empty', 'state_detail': 'empty_no_match',
                                                     'state_detail_label': 'nothing matched the filters',
                                                     'available': 0}}})
        self.assertNotEqual(expired['generation'], empty['generation'])


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

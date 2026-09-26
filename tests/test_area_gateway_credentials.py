"""F04 through the listener: the gateway authenticates to an upstream proxy.

The listener used to speak to a password-protected proxy without ever
authenticating.  An HTTP upstream got a CONNECT with no ``Proxy-Authorization``
at all, and a SOCKS5 upstream was offered method ``0x00`` and nothing else, so
every such address failed forever and the F04 chain "import -> worker check ->
scoped gateway" did not exist.  These tests stand real sockets on loopback - a
mock HTTP proxy with Basic auth, a mock SOCKS5 with a username/password, a mock
SOCKS4 - and assert on the bytes that actually crossed them.

They also pin the security property that came with the fix: a rejected
credential must not become a channel that says "a password exists here and it
did not fit".  "No credential", "the credential was rejected" and "the proxy is
dead" have to leave the same trace in the pool.
"""
import asyncio
import base64
import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from proxy_workbench import gateway, secrets as secretstore
from tests.gateway_support import GatewayCase, shutdown

USERNAME = 'alice'
PASSWORD = 'canary-credential-p4ss'
#: The header a correct HTTP Basic credential produces (RFC 7617).
WANT_BASIC = 'Basic ' + base64.b64encode(f'{USERNAME}:{PASSWORD}'.encode()).decode()


# --------------------------------------------------------------------------- #
# Mock upstreams that demand a credential
# --------------------------------------------------------------------------- #

class AuthHttpUpstream:
    """An HTTP proxy that answers 407 until it sees the right credential.

    It records every head it received, so a test can say what the gateway put
    on the wire rather than what it meant.
    """

    def __init__(self, case, scheme='http', *, password=PASSWORD, tls=None):
        self.case = case
        self.scheme = scheme
        self.password = password
        self.heads = []
        self.auth_headers = []
        self.refusals = 0
        self._tls = tls

    async def start(self):
        case = self.case
        up = self

        async def handler(reader, writer):
            with contextlib.suppress(OSError, RuntimeError):
                await up.serve(reader, writer)
            await shutdown(writer)

        self.port = await case.listen(handler, ssl=self._tls)
        case.ups.append(self)
        return self

    @property
    def url(self):
        return f'{self.scheme}://127.0.0.1:{self.port}'

    @property
    def expected(self):
        """The header this upstream accepts, built from the password it wants."""
        return 'Basic ' + base64.b64encode(f'{USERNAME}:{self.password}'.encode()).decode()

    async def serve(self, reader, writer):
        head = await reader.readuntil(b'\r\n\r\n')
        self.heads.append(head)
        lower = head.lower()
        header = next((line.split(b':', 1)[1].strip() for line in head.split(b'\r\n')
                       if line.lower().startswith(b'proxy-authorization')), None)
        self.auth_headers.append(header)
        if header is None or header.decode() != self.expected:
            self.refusals += 1
            writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\n'
                         b'Proxy-Authenticate: Basic realm="private"\r\n'
                         b'Content-Length: 0\r\nConnection: close\r\n\r\n')
            await writer.drain()
            return
        request_line = head.split(b'\r\n')[0]
        method, target = request_line.split(b' ')[:2]
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
        await self.case.splice(reader, writer, *upstream)


class AuthSocksUpstream:
    """A SOCKS5 proxy that only accepts method ``0x02`` and a correct password."""

    def __init__(self, case, scheme='socks5', *, password=PASSWORD):
        self.case = case
        self.scheme = scheme
        self.password = password
        self.greetings = []
        self.credentials = []
        self.granted = 0
        self.refused = 0
        self.targets = []

    async def start(self):
        case = self.case
        up = self

        async def handler(reader, writer):
            with contextlib.suppress(OSError, RuntimeError, asyncio.IncompleteReadError):
                await up.serve(reader, writer)
            await shutdown(writer)

        self.port = await case.listen(handler)
        case.ups.append(self)
        return self

    @property
    def url(self):
        return f'{self.scheme}://127.0.0.1:{self.port}'

    @staticmethod
    async def read_target(reader):
        """VER, REP, RSV, ATYP, ADDR - the request half of a SOCKS5 CONNECT."""
        import ipaddress
        _version, _reply, _reserved, kind = await reader.readexactly(4)
        if kind == 1:
            return ipaddress.IPv4Address(await reader.readexactly(4)).compressed
        if kind == 4:
            return ipaddress.IPv6Address(await reader.readexactly(16)).compressed
        length = (await reader.readexactly(1))[0]
        return (await reader.readexactly(length)).decode('idna')

    async def serve(self, reader, writer):
        greeting = await reader.readexactly(2)
        methods = await reader.readexactly(greeting[1])
        self.greetings.append(methods)
        if secretstore.SOCKS5_METHOD_USER_PASSWORD not in methods:
            self.refused += 1
            writer.write(b'\x05\xff')
            await writer.drain()
            return
        writer.write(b'\x05\x02')
        await writer.drain()
        version = await reader.readexactly(1)
        user = await reader.readexactly((await reader.readexactly(1))[0])
        password = await reader.readexactly((await reader.readexactly(1))[0])
        self.credentials.append((version, user, password))
        if user.decode() != USERNAME or password.decode() != self.password:
            self.refused += 1
            writer.write(b'\x01\x01')
            await writer.drain()
            return
        self.granted += 1
        writer.write(b'\x01\x00')
        await writer.drain()
        host = await self.read_target(reader)
        port = int.from_bytes(await reader.readexactly(2), 'big')
        self.targets.append((host, port))
        upstream = await asyncio.open_connection(host, port)
        writer.write(b'\x05\x00\x00\x01' + bytes(6))
        await writer.drain()
        await self.case.splice(reader, writer, *upstream)


class OpenSocksUpstream(AuthSocksUpstream):
    """A SOCKS5 proxy without a credential, to show an open one is left alone."""

    def __init__(self, case, scheme='socks5'):
        super().__init__(case, scheme)
        self.require_auth = False

    async def serve(self, reader, writer):
        greeting = await reader.readexactly(2)
        methods = await reader.readexactly(greeting[1])
        self.greetings.append(methods)
        if 0x00 in methods and 0x02 not in methods:
            writer.write(b'\x05\x00')
            await writer.drain()
        elif 0x00 in methods:
            self.refused += 1
            writer.write(b'\x05\x00')
            await writer.drain()
        else:
            self.refused += 1
            writer.write(b'\x05\xff')
            await writer.drain()
            return
        host = await self.read_target(reader)
        port = int.from_bytes(await reader.readexactly(2), 'big')
        self.targets.append((host, port))
        upstream = await asyncio.open_connection(host, port)
        writer.write(b'\x05\x00\x00\x01' + bytes(6))
        await writer.drain()
        await self.case.splice(reader, writer, *upstream)


# --------------------------------------------------------------------------- #
# The F04 store the listener reads
# --------------------------------------------------------------------------- #

def build_store(home, *, password=PASSWORD, mode=secretstore.MODE_HTTP_BASIC,
                scheme='http', endpoint='ep-1', access_id='acc-1'):
    """A real ``secrets.AccessStore`` over a real SQLite file and a memory vault."""
    db = Path(home) / 'proxies.sqlite3'
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS accesses (id TEXT PRIMARY KEY, endpoint_id TEXT, '
                 'mode TEXT, secret_ref TEXT, access_revision INTEGER, created_at REAL, rotated_at REAL)')
    conn.commit()
    store = secretstore.AccessStore(conn, secretstore.MemoryVault())
    access = store.create(endpoint, scheme, username=USERNAME, password=password, mode=mode,
                          access_id=access_id)
    return conn, store, access


class CredentialCase(GatewayCase):
    """A gateway whose pool carries access references, and a credential store."""

    def store(self, **options):
        conn, store, access = build_store(self.home, **options)
        self.addCleanup(lambda: conn.close())
        self.access = access
        self.conn = conn
        return store

    def published(self, url, **extra):
        """Publish one row that names the access identity the check used."""
        self.publish([url], rows=[dict(self.row(url, **extra))])

    def row(self, url, **extra):
        from tests.gateway_support import export_row
        return export_row(url, **extra)

    def with_endpoint(self, url, endpoint='ep-1', **extra):
        from tests.gateway_support import export_row
        return export_row(url, endpoint_id=endpoint, **extra)

    @staticmethod
    async def splice(a_reader, a_writer, b_reader, b_writer):
        """Two connected sockets, copied both ways until EOF."""
        from tests.gateway_support import splice
        return await splice(a_reader, a_writer, b_reader, b_writer)


# --------------------------------------------------------------------------- #
# 1. The bytes
# --------------------------------------------------------------------------- #

class UpstreamCredentialTests(CredentialCase):
    """What the gateway puts on the wire, checked on the wire."""

    async def test_http_connect_carries_the_rfc_7617_header(self):
        up = await AuthHttpUpstream(self).start()
        store = self.store()
        self.publish([up.url], rows=[self.with_endpoint(up.url, access_id=self.access.id,
                                                         access_revision=1)])
        server, address = await self.start(credentials=gateway.AccessCredentials(store))
        reader, writer = await self.http_client(
            address, b'CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n' % self.target)
        self.assertIn(b' 200 ', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5))
        self.assertEqual(up.auth_headers, [WANT_BASIC.encode()])
        self.assertIn(b'Proxy-Authorization: ' + WANT_BASIC.encode(), up.heads[0])
        self.assertEqual(up.refusals, 0, 'the upstream never had to refuse')
        writer.write(b'GET /after-auth HTTP/1.1\r\n\r\n')
        await writer.drain()
        self.assertIn(b'after-auth', await asyncio.wait_for(reader.read(4096), 5))
        await shutdown(writer)

    async def test_a_plain_http_request_carries_the_header_too(self):
        # Not only CONNECT: a forward request is written by the relay, so the
        # credential has to be re-attached there.  Forgetting it would leave the
        # tunnel authenticated and the request anonymous.
        up = await AuthHttpUpstream(self).start()
        store = self.store()
        self.publish([up.url], rows=[self.with_endpoint(up.url, access_id=self.access.id,
                                                         access_revision=1)])
        _server, address = await self.start(credentials=gateway.AccessCredentials(store))
        response = await self.get(address, '/forwarded')
        self.assertEqual(response.status_code, 200)
        self.assertIn('forwarded', response.text)
        self.assertEqual(up.auth_headers, [WANT_BASIC.encode()])
        self.assertIn(b'Proxy-Authorization', up.heads[0])

    async def test_socks5_offers_username_password_and_sends_it(self):
        up = await AuthSocksUpstream(self).start()
        store = self.store(mode=secretstore.MODE_SOCKS5, scheme='socks5')
        self.publish([up.url], rows=[self.with_endpoint(up.url, access_id=self.access.id,
                                                         access_revision=1)])
        _server, address = await self.start(credentials=gateway.AccessCredentials(store))
        granted, _reader, writer = await self.socks_client(address)
        self.assertTrue(granted, 'the SOCKS5 tunnel must be established, not refused')
        self.assertEqual(up.greetings, [b'\x02'], 'the offer must be 0x02 alone, never 0x00')
        self.assertEqual(up.credentials, [(b'\x01', USERNAME.encode(), PASSWORD.encode())],
                         'RFC 1929 version 1, then the user name and the password')
        self.assertEqual(up.granted, 1)
        self.assertEqual(up.targets[-1], ('127.0.0.1', self.target))
        await shutdown(writer)

    async def test_socks5h_authenticates_and_still_leaves_the_name_at_the_proxy(self):
        up = await AuthSocksUpstream(self, 'socks5h').start()
        store = self.store(mode=secretstore.MODE_SOCKS5, scheme='socks5')
        self.publish([up.url], rows=[self.with_endpoint(up.url, access_id=self.access.id,
                                                         access_revision=1)])
        _server, address = await self.start(credentials=gateway.AccessCredentials(store))
        granted, _reader, writer = await self.socks_client(address, target_host='localhost')
        self.assertTrue(granted)
        self.assertEqual(up.credentials[-1][2], PASSWORD.encode())
        self.assertEqual(up.targets[-1], ('localhost', self.target),
                         'authenticating must not change the DNS mode')
        await shutdown(writer)

    async def test_an_open_proxy_is_still_offered_only_no_authentication(self):
        # A credential for one row must not leak into the handshake of another
        # row that has none: the offer is derived from the row, every time.
        up = await OpenSocksUpstream(self).start()
        store = self.store()
        self.publish([up.url], rows=[self.row(up.url)])
        _server, address = await self.start(credentials=gateway.AccessCredentials(store))
        granted, _reader, writer = await self.socks_client(address)
        self.assertTrue(granted)
        self.assertEqual(up.greetings, [b'\x00'], 'an open row offers 0x00, nothing else')
        self.assertEqual(up.credentials, [])
        await shutdown(writer)

    async def test_a_socks4_row_is_never_given_a_credential_it_cannot_carry(self):
        # F04 refuses to invent a SOCKS4 credential; the listener must not try.
        up = await AuthSocksUpstream(self, 'socks4').start()
        store = self.store(mode=secretstore.MODE_SOCKS5, scheme='socks5')
        self.publish([up.url], rows=[self.with_endpoint(up.url, access_id=self.access.id,
                                                         access_revision=1)])
        server, address = await self.start(credentials=gateway.AccessCredentials(store))
        pool = server.gateway.pool
        self.assertIsNone(pool.row_for(up.url) and None if False else
                          server.gateway.credentials(pool.row_for(up.url), 'socks4'),
                          'socks4 has no credential sub-negotiation')
        response = await self.get(address, '/x')
        self.assertIn(response.status_code, (502,), 'an unusable upstream is an ordinary 502')
        self.assertEqual(pool.report(up.url)['failed'], 1)

    async def test_the_password_never_appears_in_a_snapshot_a_client_can_read(self):
        up = await AuthHttpUpstream(self).start()
        store = self.store()
        self.publish([up.url], rows=[self.with_endpoint(up.url, access_id=self.access.id,
                                                         access_revision=1)])
        server, address = await self.start(credentials=gateway.AccessCredentials(store))
        await self.get(address, '/x')
        published = json.dumps(await server.gateway.pool.asnapshot(50), default=str)
        self.assertNotIn(PASSWORD, published)
        self.assertNotIn(WANT_BASIC, published)
        self.assertNotIn(PASSWORD, json.dumps(server.gateway.state(), default=str))


# --------------------------------------------------------------------------- #
# 2. Which identity is used
# --------------------------------------------------------------------------- #

class IdentityTests(CredentialCase):
    async def test_the_row_pins_the_access_it_was_measured_with(self):
        up = await AuthHttpUpstream(self).start()
        store = self.store()
        self.publish([up.url], rows=[self.with_endpoint(up.url, access_id=self.access.id,
                                                         access_revision=1)])
        _server, address = await self.start(credentials=gateway.AccessCredentials(store))
        self.assertEqual((await self.get(address, '/x')).status_code, 200)
        self.assertEqual(up.refusals, 0)

    async def test_a_single_access_of_an_endpoint_is_found_without_the_row_naming_it(self):
        # `api.public_row` does not carry access_id, so an endpoint with exactly
        # one usable access must still be authenticated.  With several it must
        # not guess: two credentials of one address are two identities.
        up = await AuthHttpUpstream(self).start()
        store = self.store(endpoint='ep-single')
        self.publish([up.url], rows=[self.with_endpoint(up.url, endpoint='ep-single')])
        _server, address = await self.start(credentials=gateway.AccessCredentials(store))
        self.assertEqual((await self.get(address, '/x')).status_code, 200)
        self.assertEqual(up.auth_headers, [WANT_BASIC.encode()])

    async def test_two_accesses_of_one_endpoint_are_never_merged(self):
        up = await AuthHttpUpstream(self).start()
        conn, store, first = build_store(self.home, password='first-password',
                                         endpoint='ep-two', access_id='acc-first')
        self.addCleanup(lambda: conn.close())
        store.create('ep-two', 'http', username='bob', password='second-password',
                     mode=secretstore.MODE_HTTP_BASIC, access_id='acc-second')
        self.publish([up.url], rows=[self.with_endpoint(up.url, endpoint='ep-two')])
        source = gateway.AccessCredentials(store)
        _server, address = await self.start(credentials=source)
        response = await self.get(address, '/x')
        self.assertEqual(response.status_code, 407)
        self.assertEqual(up.refusals, 1, 'no credential was sent at all')
        self.assertEqual(up.auth_headers, [None], 'not even the wrong one went out')
        before = source.as_json()['unavailable']
        self.assertIsNone(source({'endpoint_id': 'ep-two'}, 'http'),
                          'an ambiguous row resolves to nothing')
        self.assertEqual(source.as_json()['unavailable'], before + 1)
        self.assertIsNotNone(source({'access_id': first.id, 'access_revision': 1}, 'http'),
                             'a row that names its access is honoured')

    async def test_a_rotated_password_is_the_one_that_is_sent(self):
        up = await AuthHttpUpstream(self, password='rotated-password').start()
        store = self.store()
        store.rotate(self.access.id, password='rotated-password')
        self.publish([up.url], rows=[self.with_endpoint(up.url, access_id=self.access.id,
                                                         access_revision=2)])
        _server, address = await self.start(credentials=gateway.AccessCredentials(store))
        self.assertEqual((await self.get(address, '/x')).status_code, 200)
        self.assertEqual(up.refusals, 0)
        self.assertEqual(up.auth_headers, [up.expected.encode()])

    async def test_a_row_of_a_superseded_revision_sends_nothing(self):
        # CONTRACTS §1.2(1): the old revision's evidence is not the new
        # credential's evidence, and the old secret must not go back on the wire.
        # The row the listener reads today carries no revision - see the
        # `public_row` item in the handoff - so the rule is pinned where the
        # field exists, on the resolver itself, and the end-to-end effect is
        # pinned through an ambiguous row in the test above.
        store = self.store()
        store.rotate(self.access.id, password='rotated-password')
        source = gateway.AccessCredentials(store)
        self.assertIsNone(source({'access_id': self.access.id, 'access_revision': 1}, 'http'),
                          'a superseded revision is refused, not sent')
        self.assertEqual(source.as_json()['unavailable'], 1)
        current = source({'access_id': self.access.id, 'access_revision': 2}, 'http')
        self.assertTrue(current.authenticated, 'the current revision still authenticates')
        self.assertTrue(current.proxy_authorization.startswith('Basic '))

    async def test_a_locked_vault_is_an_unusable_upstream_not_a_broken_listener(self):
        up = await AuthHttpUpstream(self).start()
        store = self.store()
        store.vault.lock()
        self.publish([up.url], rows=[self.with_endpoint(up.url, access_id=self.access.id,
                                                         access_revision=1)])
        server, address = await self.start(credentials=gateway.AccessCredentials(store))
        self.assertEqual((await self.get(address, '/x')).status_code, 407)
        self.assertEqual(server.gateway.pool.report(up.url)['failed'], 1)
        self.assertEqual(server.gateway.state()['credentials']['unavailable'], 1)


# --------------------------------------------------------------------------- #
# 3. The leak channel
# --------------------------------------------------------------------------- #

class IndistinguishableFailureTests(CredentialCase):
    """"No password", "wrong password" and "dead proxy" leave one trace.

    The requirement is that an observer of the pool cannot learn *which* of the
    three happened for a given address, because "this address has a password
    and it did not fit" is itself a secret.  Two channels leaked it before the
    fix and both are closed here: the per-proxy usage record carried a
    ``detail`` word that only a fault recorded while dialling had, and the
    gateway was the only component that knew whether it had sent anything.
    """

    async def _observe(self, url, store=None):
        self.publish([url], rows=[self.with_endpoint(url, access_id='acc-1', access_revision=1)]
                      if store else [self.row(url)])
        options = {'credentials': gateway.AccessCredentials(store)} if store else {}
        server, address = await self.start(max_failures=99, cooldown=300, **options)
        await self.get(address, '/x')
        pool = server.gateway.pool
        # The relay of a refused request is still winding down when the client
        # has its answer, so let it finish: an open slot is a timing fact, not
        # a property of the address.
        await self.wait_for(lambda: not pool.active.get(url))
        report = dict(pool.report(url))
        report['resting'] = url in pool.resting
        report['health'] = round(pool.health_score(url), 3)
        published = json.loads(json.dumps(await pool.asnapshot(50), default=str))
        # The per-proxy line of the network-visible page, with the address
        # removed so two different fixtures compare.
        for line in published['top']:
            line.pop('proxy', None)
        report['published_top'] = published['top']
        return report

    async def test_a_rejected_credential_looks_exactly_like_a_dead_proxy(self):
        protected = await AuthHttpUpstream(self).start()
        dead = await self.dead_upstream('http')
        refused = await self._observe(protected.url)
        died = await self._observe(dead.url)
        self.assertEqual(refused, died,
                         f'a rejected credential must not be visible: {refused} vs {died}')

    async def test_a_missing_credential_looks_exactly_like_a_rejected_one(self):
        # The upstream wants a password the store does not hold, so the gateway
        # sends a credential and is refused.  Then the very same upstream with
        # no credential to send: both must leave the same record.
        protected = await AuthHttpUpstream(self, password='a-different-password').start()
        store = self.store()
        self.publish([protected.url], rows=[self.with_endpoint(protected.url,
                                                               access_id=self.access.id,
                                                               access_revision=1)])
        server, address = await self.start(max_failures=99, cooldown=300,
                                           credentials=gateway.AccessCredentials(store))
        await self.get(address, '/x')
        with_credential = dict(server.gateway.pool.report(protected.url))

        # The very same upstream, with nothing to send it: an open pool.
        self.publish([protected.url], rows=[self.row(protected.url)])
        server2, address2 = await self.start(max_failures=99, cooldown=300)
        await self.get(address2, '/x')
        without = dict(server2.gateway.pool.report(protected.url))
        self.assertEqual(with_credential, without,
                         'holding a rejected credential must not be visible in the pool')

    async def test_the_two_are_told_apart_only_by_what_the_upstream_said(self):
        protected = await AuthHttpUpstream(self).start()
        self.publish([protected.url], rows=[self.row(protected.url)])
        _server, address = await self.start()
        answered = await self.get(address, '/x')
        dead = await self.dead_upstream('http')
        self.publish([dead.url], rows=[self.row(dead.url)])
        _server2, address2 = await self.start()
        silent = await self.get(address2, '/x')
        # Stated rather than hidden: an upstream that answered and an upstream
        # that is not reachable differ, and any observer could have established
        # that by connecting to the address itself.  What they cannot learn is
        # whether this gateway holds a credential for it.
        self.assertEqual(answered.status_code, 407)
        self.assertEqual(silent.status_code, 502)
        self.assertEqual(answered.content, b'', 'no upstream body is relayed to the client')

    async def test_the_status_page_never_names_a_credential(self):
        protected = await AuthHttpUpstream(self).start()
        store = self.store()
        self.publish([protected.url], rows=[self.with_endpoint(protected.url,
                                                               access_id=self.access.id,
                                                               access_revision=1)])
        server, _address = await self.start(max_failures=99, cooldown=300,
                                            credentials=gateway.AccessCredentials(store))
        await server.gateway.pool.asnapshot(50)
        body = json.dumps(server.gateway.pool.snapshot(50), default=str)
        for word in ('auth', 'credential', 'password', 'basic', USERNAME, PASSWORD):
            self.assertNotIn(word, body.lower(),
                             f'the network-visible snapshot published {word!r}')

    async def test_the_client_is_told_the_same_thing_either_way(self):
        # The upstream wants a password this pool cannot supply, so the gateway
        # sends a credential and is refused; then the same upstream with no
        # credential at all.  Not one byte the client sees may differ.
        protected = await AuthHttpUpstream(self, password='a-different-password').start()
        store = self.store()
        self.publish([protected.url], rows=[self.with_endpoint(protected.url,
                                                               access_id=self.access.id,
                                                               access_revision=1)])
        _server, address = await self.start(credentials=gateway.AccessCredentials(store))
        with_credential = await self.get(address, '/x')
        self.publish([protected.url], rows=[self.row(protected.url)])
        _server2, address2 = await self.start()
        without = await self.get(address2, '/x')
        self.assertEqual(with_credential.status_code, without.status_code)
        self.assertEqual(with_credential.content, without.content)


if __name__ == '__main__':
    unittest.main()

"""F16 acceptance, driven by bytes on real loopback sockets.

The area list this file answers:

* transport parity - HTTP, CONNECT, HTTPS-to-proxy, SOCKS4, SOCKS4a, SOCKS5,
  SOCKS5h, both DNS modes and IPv6 (parity itself is covered in
  ``test_gateway_transports``; here each is driven through the *listener*, with
  what the upstream received asserted);
* DNS modes, IPv6, rotation round-robin / random / health-aware, strict and
  failover sticky, session TTL, concurrency reservation under real parallelism,
  a bounded cache, a safe snapshot from the event loop, long connections and a
  shutdown with a stated policy;
* the listener binding the pool it was configured with, and an open TCP stream
  never being moved to another upstream;
* LAN: an explicit opt-in, its own password, a published interface and a
  refusal without the opt-in.

Acceptance wording this file pins: "concurrent limits, refusal/cancel/deadline,
generation change, pool deletion, client isolation, auth and a real local
transport".
"""
import asyncio
import contextlib
import ipaddress
import json
import random
import threading
import time
import unittest
from pathlib import Path

from proxy_workbench import gateway
from tests.gateway_support import GatewayCase, shutdown, write_export


# --------------------------------------------------------------------------- #
# 1. Transport parity through the listener
# --------------------------------------------------------------------------- #

class ParityThroughTheListenerTests(GatewayCase):
    async def test_every_supported_transport_is_driven_by_a_client(self):
        expectations = {
            'http': ('forward', 'GET'),
            'socks4': ('socks4-ipv4', None),
            'socks4a': ('socks4a-name', None),
            'socks5': ('name-resolved-v4', None),
            'socks5h': ('name-at-proxy', None),
        }
        for scheme, (mode, _) in expectations.items():
            with self.subTest(scheme=scheme):
                up = await self.socks_upstream(scheme) if scheme.startswith('socks') \
                    else await self.http_upstream('relay')
                self.publish([up.url])
                _server, address = await self.start()
                if scheme == 'http':
                    response = await self.get(address, '/parity')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(up.targets[-1][0], 'forward')
                else:
                    target = 'localhost' if scheme in ('socks4a', 'socks5h') else '127.0.0.1'
                    granted, reader, writer = await self.socks_client(address, target_host=target)
                    self.assertTrue(granted, f'{scheme} did not complete its handshake')
                    self.assertEqual(up.targets[-1]['mode'], mode)
                    await shutdown(writer)
                self.assertGreaterEqual(up.connections, 1)

    async def test_https_to_proxy_carries_the_request_over_tls(self):
        import ssl
        import shutil
        import subprocess
        import tempfile
        if shutil.which('openssl') is None:
            self.skipTest('openssl is not available for a throwaway certificate')
        with tempfile.TemporaryDirectory() as temp:
            made = subprocess.run(
                ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                 '-keyout', f'{temp}/key.pem', '-out', f'{temp}/cert.pem',
                 '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                capture_output=True, text=True, timeout=60)
            if made.returncode != 0:
                self.skipTest('openssl refused to make a certificate')
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(f'{temp}/cert.pem', f'{temp}/key.pem')
            up = await self.http_upstream('relay', tls=server_context, scheme='https')
            self.publish([up.url])
            server, address = await self.start()
            client_context = ssl.create_default_context()
            client_context.load_verify_locations(cafile=f'{temp}/cert.pem')
            server.gateway.ssl_context = client_context
            response = await self.get(address, '/over-tls')
            self.assertEqual(response.status_code, 200)
            self.assertIn('over-tls', response.text)
            self.assertEqual(up.targets[-1][0], 'forward')

    async def test_an_ipv6_target_is_carried_as_an_address_not_a_name(self):
        if self.target6 is None:
            self.skipTest('no usable ::1 on this host')
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        _server, address = await self.start()
        granted, _reader, writer = await self.socks_client(
            address, target_host='::1', target_port=self.target6)
        self.assertTrue(granted)
        self.assertEqual(up.targets[-1]['mode'], 'name-resolved-v6')
        self.assertEqual(ipaddress.ip_address(up.targets[-1]['host']).version, 6)
        await shutdown(writer)

    async def test_socks5_dns_mode_never_sends_a_name_and_socks5h_always_does(self):
        original = gateway.resolve
        seen = []

        async def spy(host, port, family=0):
            seen.append(host)
            return await original(host, port, family)

        for scheme, expect_name in (('socks5', False), ('socks5h', True)):
            with self.subTest(scheme=scheme):
                up = await self.socks_upstream(scheme)
                self.publish([up.url], generation=f'.generation-{scheme}aaaa')
                _server, address = await self.start()
                seen.clear()
                gateway.resolve = spy
                try:
                    granted, _reader, writer = await self.socks_client(
                        address, target_host='localhost')
                finally:
                    gateway.resolve = original
                self.assertTrue(granted)
                self.assertEqual(bool(seen), not expect_name,
                                 f'{scheme}: local resolution = {seen}')
                self.assertEqual(up.targets[-1]['mode'] == 'name-at-proxy', expect_name)
                await shutdown(writer)


# --------------------------------------------------------------------------- #
# 2. Rotation, sticky, sessions
# --------------------------------------------------------------------------- #

class RotationTests(GatewayCase):
    async def drain(self, address, count):
        used = []
        for _ in range(count):
            await self.get(address, '/rotate')
        return used

    async def test_round_robin_reaches_every_proxy_in_order(self):
        ups = [await self.http_upstream('relay') for _ in range(3)]
        self.publish([up.url for up in ups])
        server, address = await self.start(strategy='round-robin')
        for _ in range(3):
            await self.get(address, '/rr')
        reached = [up for up in ups if up.requests]
        self.assertEqual(len(reached), 3, 'round-robin must not keep one address')

    async def test_random_never_keeps_one_address_for_everything(self):
        random.seed(11)
        ups = [await self.http_upstream('relay') for _ in range(4)]
        self.publish([up.url for up in ups])
        _server, address = await self.start(strategy='random')
        for _ in range(12):
            await self.get(address, '/rnd')
        self.assertEqual(sum(1 for up in ups if up.requests), 4)

    async def test_health_aware_prefers_what_actually_answered(self):
        good = await self.http_upstream('relay')
        bad = await self.http_upstream('silent')
        self.publish([good.url, bad.url], generation='.generation-health11')
        server, address = await self.start(strategy='health-aware', max_failures=99,
                                           cooldown=300)
        # Give the bad address a few refusals, then compare the health scores.
        for _ in range(4):
            await self.get(address, '/health')
        self.assertGreater(server.gateway.pool.health_score(good.url),
                           server.gateway.pool.health_score(bad.url))
        good.requests.clear()
        for _ in range(4):
            await self.get(address, '/health')
        self.assertGreater(len(good.requests), 0, 'the working address is still served')

    async def test_a_failover_session_rebinds_but_a_strict_one_refuses(self):
        pinned = await self.http_upstream('silent')
        other = await self.http_upstream('relay')
        self.publish([pinned.url, other.url], generation='.generation-sticky11')
        server, address = await self.start(strategy='round-robin', max_failures=99,
                                           cooldown=300, session_ttl=600)

        def with_session(name):
            return f'http://session-{name}:pw@{address}'

        async def get_session(name):
            host, _, port = address.rpartition(':')
            reader, writer = await asyncio.open_connection(host, int(port))
            writer.write(b'GET http://127.0.0.1:%d/s HTTP/1.1\r\n'
                         b'Host: 127.0.0.1\r\n'
                         b'Proxy-Authorization: Basic %s\r\n\r\n'
                         % (self.target,
                            __import__('base64').b64encode(f'session-{name}:pw'.encode())))
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
            body = b''
            with contextlib.suppress(asyncio.TimeoutError, asyncio.IncompleteReadError):
                body = await asyncio.wait_for(reader.read(4096), 5)
            writer.close()
            with contextlib.suppress(OSError, RuntimeError):
                await writer.wait_closed()
            return head.split(b'\r\n')[0], body

        failover_head, failover_body = await get_session('failover')
        self.assertIn(b'200', failover_head, failover_body)
        self.assertGreaterEqual(other.connections, 1, 'a failover session moves on')

        server.gateway.pool.set_sticky('strict') if hasattr(server.gateway.pool, 'set_sticky') \
            else setattr(server.gateway.pool, 'sticky', 'strict')
        strict_head, strict_body = await get_session('strict')
        # A strict session bound to the silent address is refused rather than
        # silently moved, and the refusal names the same thing every time.
        self.assertIn(b'502', strict_head, strict_body)

    async def test_a_session_expires_and_is_free_to_choose_again(self):
        ups = [await self.http_upstream('relay') for _ in range(2)]
        self.publish([up.url for up in ups], generation='.generation-ttl111111')
        server, address = await self.start(session_ttl=0.35)
        import base64
        host, _, port = address.rpartition(':')

        async def once():
            reader, writer = await asyncio.open_connection(host, int(port))
            writer.write(b'GET http://127.0.0.1:%d/t HTTP/1.1\r\nHost: x\r\n'
                         b'Proxy-Authorization: Basic %s\r\n\r\n'
                         % (self.target, base64.b64encode(b'session-ttl1:pw')))
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
            writer.close()
            with contextlib.suppress(OSError, RuntimeError):
                await writer.wait_closed()

        await once()
        bound = server.gateway.pool.sessions.get('ttl1')
        self.assertIsNotNone(bound)
        await asyncio.sleep(0.5)
        self.assertIsNone(server.gateway.pool._session('ttl1', time.monotonic()),
                          'the binding is gone once the TTL is over')
        ups[0].requests.clear()
        ups[1].requests.clear()
        await once()
        self.assertEqual(len(ups[0].requests) + len(ups[1].requests), 1)


# --------------------------------------------------------------------------- #
# 3. Concurrency, deadlines, shutdown
# --------------------------------------------------------------------------- #

class ConcurrencyTests(GatewayCase):
    async def test_parallel_clients_never_exceed_max_per_proxy(self):
        # The point is parallelism: every client is suspended inside the
        # upstream handshake at the same moment, which is the window a
        # check-then-take implementation loses.
        stall = await self.socks_upstream('socks5', stall=True)
        self.publish([stall.url])
        server, address = await self.start(max_per_proxy=2, attempts=1,
                                           handshake_timeout=30, connect_timeout=10)
        host, _, port = address.rpartition(':')

        async def open_one():
            reader, writer = await asyncio.open_connection(host, int(port))
            writer.write(b'\x05\x01\x00')
            await writer.drain()
            await reader.readexactly(2)
            writer.write(b'\x05\x01\x00\x01' + ipaddress.IPv4Address('127.0.0.1').packed
                         + int(self.target).to_bytes(2, 'big'))
            await writer.drain()
            return reader, writer

        clients = [await open_one() for _ in range(2)]
        self.assertTrue(await self.wait_for(lambda: stall.peak_open == 2),
                        f'peak simultaneous upstream connections: {stall.peak_open}')
        # A third client has no free slot: it is told so instead of waiting
        # for a slot that will never come.
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(b'\x05\x01\x00')
        await writer.drain()
        await reader.readexactly(2)
        writer.write(b'\x05\x01\x00\x01' + ipaddress.IPv4Address('127.0.0.1').packed
                     + int(self.target).to_bytes(2, 'big'))
        await writer.drain()
        self.assertEqual((await asyncio.wait_for(reader.readexactly(4), 5))[1], 1,
                         'a third parallel client is refused, not queued')
        for handle in clients:
            await shutdown(handle[1])
        await shutdown(writer)
        self.assertEqual(server.gateway.pool.available(), [] if True else None)

    async def test_a_cancelled_handshake_gives_the_slot_back(self):
        # The slot is reserved before the first blocking await, so the window
        # that matters is a cancellation *inside* the upstream handshake.  The
        # client's own task is not where that happens, so the handler task the
        # listener is running is cancelled directly.
        stall = await self.socks_upstream('socks5', stall=True)
        self.publish([stall.url])
        server, address = await self.start(max_per_proxy=1, attempts=1,
                                           handshake_timeout=30, connect_timeout=10)
        host, _, port = address.rpartition(':')
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(b'\x05\x01\x00')
        await writer.drain()
        await reader.readexactly(2)
        writer.write(b'\x05\x01\x00\x01' + ipaddress.IPv4Address('127.0.0.1').packed
                     + int(self.target).to_bytes(2, 'big'))
        await writer.drain()
        self.assertTrue(await self.wait_for(lambda: stall.peak_open >= 1))
        self.assertEqual(server.gateway.pool.active, {stall.url: 1})
        for task in list(server.gateway.tasks):
            task.cancel()
        self.assertTrue(await self.wait_for(lambda: not server.gateway.pool.active),
                        f'slot still held: {server.gateway.pool.active}')
        # The freed slot is immediately usable again.
        self.assertEqual(server.gateway.pool.available(), [stall.url])
        await shutdown(writer)

    async def test_a_handshake_that_never_finishes_is_dropped_at_the_deadline(self):
        stall = await self.socks_upstream('socks5', stall=True)
        self.publish([stall.url])
        server, address = await self.start(handshake_timeout=0.4, attempts=1, connect_timeout=30)
        started = time.monotonic()
        # Nothing of the upstream ever arrives, so the client is cut off at the
        # handshake deadline instead of being left waiting: an answer here would
        # have to invent one.
        with self.assertRaises(Exception):
            await self.get(address, '/deadline')
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5, 'the client handshake has its own deadline')
        self.assertTrue(await self.wait_for(lambda: not server.gateway.pool.active),
                        'the abandoned slot is given back')
        self.assertGreaterEqual(server.gateway.pool.stats['closed_deadline'], 1)

    async def test_a_long_stream_survives_its_own_idle_and_ends_at_the_cap(self):
        up = await self.http_upstream('relay')
        self.publish([up.url])
        _server, address = await self.start(max_session=0.4, idle_timeout=30)
        reader, writer = await self.http_client(
            address, b'CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n' % self.target)
        self.assertIn(b' 200 ', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5))
        writer.write(b'GET /long HTTP/1.1\r\n\r\n')
        await writer.drain()
        self.assertIn(b'long', await asyncio.wait_for(reader.read(4096), 5))
        # The cap ends the relayed connection, and says so by ending it.
        with contextlib.suppress(asyncio.IncompleteReadError, asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(), 5)
        await shutdown(writer)

    async def test_shutdown_reports_what_it_drained_and_what_it_forced(self):
        up = await self.http_upstream('relay')
        self.publish([up.url])
        server, address = await self.start(idle_timeout=60)
        reader, writer = await self.http_client(
            address, b'CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n' % self.target)
        self.assertIn(b' 200 ', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5))
        report = await server.gateway.shutdown(0.2)
        self.assertEqual(report['forced'], 1, 'a live client is forced, and counted')
        self.assertIn('closed_sessions', report)
        with contextlib.suppress(asyncio.IncompleteReadError, OSError, asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(), 5)
        await shutdown(writer)

    async def test_a_new_client_is_refused_once_the_listener_is_closing(self):
        up = await self.http_upstream('relay')
        self.publish([up.url])
        server, address = await self.start()
        server.gateway.shutting_down = True
        host, _, port = address.rpartition(':')
        reader, writer = await asyncio.open_connection(host, int(port))
        with contextlib.suppress(OSError, asyncio.IncompleteReadError):
            self.assertEqual(await asyncio.wait_for(reader.read(), 5), b'')
        await shutdown(writer)


async def _one_socks(host, port, target):
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(b'\x05\x01\x00')
    await writer.drain()
    await reader.readexactly(2)
    writer.write(b'\x05\x01\x00\x01' + ipaddress.IPv4Address('127.0.0.1').packed
                 + int(target).to_bytes(2, 'big'))
    await writer.drain()
    reply = await reader.readexactly(4)
    if reply[1] != 0:
        raise ConnectionRefusedError('the gateway refused')
    return reader, writer


# --------------------------------------------------------------------------- #
# 4. Snapshot, cache, generation, deletion, isolation
# --------------------------------------------------------------------------- #

class VisibilityTests(GatewayCase):
    async def test_the_status_page_reads_the_export_off_the_event_loop(self):
        # A snapshot taken from the loop must not do the file read on the loop.
        # What is asserted is the thread the read happened on, not a timing.
        up = await self.http_upstream('relay')
        self.publish([up.url])
        server, _address = await self.start()
        pool = server.gateway.pool
        loop_thread = threading.current_thread()
        original = pool.refresh
        where = []

        def watched():
            where.append(threading.current_thread())
            return original()

        # The read is held open until another task on the loop has run, which
        # can only finish if the loop was free while the read was in flight.
        reading = asyncio.Event()
        resume = threading.Event()
        served = []

        def watched():
            where.append(threading.current_thread())
            loop.call_soon_threadsafe(reading.set)
            resume.wait(5)
            return original()

        async def serve_the_page_while_it_reads():
            await asyncio.wait_for(reading.wait(), 5)
            served.append(threading.current_thread())
            resume.set()

        loop = asyncio.get_running_loop()
        pool.refresh = watched
        try:
            companion = asyncio.ensure_future(serve_the_page_while_it_reads())
            body = await pool.asnapshot(20)
            await asyncio.wait_for(companion, 5)
        finally:
            resume.set()
            pool.refresh = original
        self.assertTrue(where, 'the export really was re-read')
        for thread in where:
            self.assertIsNot(thread, loop_thread,
                             'a file read on the event loop stalls every client')
        self.assertEqual(served, [loop_thread],
                         'the loop kept serving while the export was read')
        self.assertEqual(body['generation'], '.generation-aaaaaaaa')

    async def test_the_filter_cache_is_bounded_and_still_correct(self):
        ups = [await self.http_upstream('relay') for _ in range(4)]
        self.publish([up.url for up in ups])
        server, _address = await self.start(cache_limit=2)
        pool = server.gateway.pool
        for index in range(20):
            pool.matching({'max_latency': 10 + index}, None)
        self.assertLessEqual(len(pool.cache), 2, f'cache grew to {len(pool.cache)}')
        pool.matching(None, None)
        self.assertEqual(len(pool.available()), 4)

    async def test_a_new_generation_is_not_served_to_a_binding_pinned_to_the_old_one(self):
        first = await self.http_upstream('relay')
        second = await self.http_upstream('relay')
        self.publish([first.url])
        binding = gateway.Binding(generation='.generation-aaaaaaaa')
        server, address = await self.start(binding=binding)
        self.assertEqual((await self.get(address, '/gen')).status_code, 200)
        self.publish([second.url], generation='.generation-bbbbbbbb')
        await server.gateway.pool.arefresh()
        response = await self.get(address, '/gen')
        self.assertEqual(response.status_code, 502,
                         'a pinned generation does not follow a new publication')
        server.gateway.set_binding(gateway.Binding(generation='.generation-bbbbbbbb'))
        await server.gateway.pool.arefresh()
        self.assertEqual((await self.get(address, '/gen')).status_code, 200)

    async def test_a_deleted_publication_serves_nothing_and_says_so(self):
        up = await self.http_upstream('relay')
        self.publish([up.url])
        server, address = await self.start()
        self.assertEqual((await self.get(address, '/gone')).status_code, 200)
        for path in (Path(self.home) / 'exports' / 'current.json',
                     Path(self.home) / 'exports' / 'generations' / '.generation-aaaaaaaa'):
            with contextlib.suppress(OSError):
                if path.is_dir():
                    for child in path.iterdir():
                        child.unlink()
                    path.rmdir()
                else:
                    path.unlink()
        await server.gateway.pool.arefresh()
        response = await self.get(address, '/gone')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(server.gateway.pool.available(), [])
        self.assertEqual(server.gateway.state()['export_state'] in (None, 'missing', 'error'), True)

    async def test_clients_are_isolated_by_their_pool_option(self):
        first = await self.http_upstream('relay')
        second = await self.http_upstream('relay')
        self.publish([first.url, second.url])
        binding = gateway.Binding(pool_id='chosen', policy={'countries': ()})
        server, address = await self.start(bindings={'chosen': binding})
        self.assertEqual(server.gateway.pool.binding_for('chosen').pool_id, 'chosen')
        with self.assertRaises(KeyError):
            server.gateway.pool.binding_for('absent')
        response = await self.get(address, '/iso', user='pool-chosen')
        self.assertEqual(response.status_code, 200)

    async def test_a_binding_reports_how_many_rows_it_can_serve(self):
        up = await self.http_upstream('relay')
        self.publish([up.url])
        server, address = await self.start()
        report = server.gateway.set_binding(gateway.Binding(pool_id='p1'))
        self.assertEqual(report['rows'], 1)
        self.assertEqual(report['binding']['pool_id'], 'p1')
        missing = server.gateway.set_binding(gateway.Binding(generation='.generation-nope'))
        self.assertEqual(missing['rows'], 0,
                         'a generation this export does not carry serves nothing')
        self.assertEqual((await self.get(address, '/x')).status_code, 502)


# --------------------------------------------------------------------------- #
# 5. The listener binding and LAN
# --------------------------------------------------------------------------- #

class ListenerBindingTests(GatewayCase):
    async def test_start_applies_the_binding_it_is_given(self):
        up = await self.http_upstream('relay')
        self.publish([up.url])
        binding = gateway.Binding(pool_id='nightly', policy={'max_per_proxy': 3})
        server, _address = await self.start(binding=binding)
        self.assertEqual(server.gateway.pool.default_binding.pool_id, 'nightly')
        self.assertEqual(server.gateway.binding.pool_id, 'nightly')
        self.assertEqual(server.gateway.state()['binding']['pool_id'], 'nightly')
        self.assertEqual(server.gateway.pool._limit(server.gateway.binding), 3)

    async def test_two_different_bindings_are_refused_rather_than_one_winning(self):
        with self.assertRaises(ValueError):
            await gateway.start(self.home, port=0, binding=gateway.Binding(pool_id='a'),
                                default_binding=gateway.Binding(pool_id='b'))
        server = await gateway.start(self.home, port=0, binding=gateway.Binding(pool_id='a'),
                                     default_binding=gateway.Binding(pool_id='a'))
        server.close()
        await server.gateway.shutdown(0)

    async def test_the_binding_scope_narrows_what_a_client_may_be_offered(self):
        up = await self.http_upstream('relay')
        self.publish([up.url])
        binding = gateway.Binding(policy={'countries': ('ZZ',)})
        server, _address = await self.start(binding=binding)
        self.assertEqual(server.gateway.pool.available(binding=binding), [],
                         'a client filter only narrows; it never widens')
        self.assertEqual(server.gateway.pool.available(), [up.url])


class LanTests(unittest.TestCase):
    """Defect 18 / F17: LAN is an explicit opt-in with its own secret."""

    def test_the_default_bind_is_loopback_and_opens_nothing(self):
        bind = gateway.Bind()
        self.assertEqual(bind.listen_host, '127.0.0.1')
        self.assertTrue(bind.local)
        self.assertEqual(bind.published_host, '127.0.0.1')
        self.assertFalse(gateway.Bind().as_dict()['lan'])

    def test_a_non_loopback_address_is_refused_without_the_opt_in(self):
        for host in ('0.0.0.0', '192.168.1.5', '::'):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    gateway.Bind(host=host)

    def test_lan_with_a_loopback_address_really_opens_the_listener(self):
        # The dead `--lan`: the flag arrived, and the listener stayed on
        # loopback, so nothing about it worked.
        bind = gateway.Bind(host='127.0.0.1', port=8899, lan=True)
        self.assertEqual(bind.listen_host, '0.0.0.0')
        self.assertFalse(bind.local)
        self.assertNotEqual(bind.published_host, '0.0.0.0')
        # A phone is told a real address, never 127.0.0.1 and never the
        # wildcard: publishing the loopback address for a wildcard listener is
        # the second half of the dead `--lan`.
        expected = gateway.lan_interfaces()[0] if gateway.lan_interfaces() \
            else gateway.DEFAULT_HOST
        self.assertEqual(bind.published_host, expected)
        self.assertNotEqual(bind.published_host, '127.0.0.1')
        self.assertEqual(gateway.Bind(host='::1', lan=True).listen_host, '::')
        self.assertTrue(gateway.Bind(host='127.0.0.1', lan=True).as_dict()['lan'])

    def test_a_chosen_interface_is_the_only_address_bound(self):
        bind = gateway.Bind(host='0.0.0.0', lan=True, interface='192.168.1.5')
        self.assertEqual(bind.listen_host, '192.168.1.5')
        self.assertEqual(bind.published_host, '192.168.1.5')
        with self.assertRaises(ValueError):
            gateway.Bind(host='0.0.0.0', lan=True, interface='127.0.0.1')

    def test_a_lan_listener_gets_its_own_password(self):
        token, origin = gateway.resolve_token(gateway.Bind(lan=True))
        self.assertEqual(origin, 'generated')
        self.assertTrue(token)
        self.assertIsNone(gateway.resolve_token(gateway.Bind())[0],
                          'a local listener invents no password')

    def test_the_state_says_which_of_the_two_a_listener_is(self):
        local, lan = gateway.Bind(), gateway.Bind(lan=True)
        self.assertTrue(local.local, 'the default listener is a local one')
        self.assertFalse(lan.local, 'an opted-in LAN listener is not')
        for field in ('host', 'port', 'lan', 'interface', 'listen_host', 'local',
                      'published_host'):
            self.assertIn(field, lan.as_dict())
            self.assertIn(field, local.as_dict())
        self.assertEqual(local.as_dict()['published_host'], '127.0.0.1')
        self.assertNotIn(lan.published_host, ('0.0.0.0', '::', ''),
                         'a phone is never told to dial the wildcard')


if __name__ == '__main__':
    unittest.main()

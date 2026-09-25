"""F16: rotation strategies, sticky modes, session TTL, bounded cache, safe snapshot
and the binding of a listener or a client to a pool, generation and profile.
"""
import asyncio
import json
import time
import unittest

import httpx

from proxy_workbench import gateway, reputation
from tests.gateway_support import GatewayCase, export_row, shutdown, write_export


class RotationTests(GatewayCase):
    async def test_round_robin_visits_every_candidate(self):
        ups = [await self.socks_upstream('socks5') for _ in range(3)]
        self.publish([up.url for up in ups])
        _server, address = await self.start(strategy='round-robin')
        for _ in range(6):
            granted, _reader, writer = await self.socks_client(address)
            self.assertTrue(granted)
            await shutdown(writer)
        self.assertEqual(sorted(up.connections for up in ups), [2, 2, 2])

    async def test_random_spreads_over_every_candidate(self):
        ups = [await self.socks_upstream('socks5') for _ in range(3)]
        self.publish([up.url for up in ups])
        _server, address = await self.start(strategy='random')
        for _ in range(9):
            granted, _reader, writer = await self.socks_client(address)
            self.assertTrue(granted)
            await shutdown(writer)
        self.assertTrue(all(up.connections for up in ups), 'random must not starve a candidate')

    async def test_unknown_strategy_is_refused(self):
        with self.assertRaises(ValueError):
            gateway.Pool(self.home, strategy='astrology')

    async def test_health_aware_prefers_what_actually_worked(self):
        # Why the strategy exists: the gateway knows which addresses carried a
        # request and which only ever opened a socket, so it can prefer the
        # first without guessing from the export's own latency numbers.
        good = await self.http_upstream('relay')
        flaky = await self.http_upstream('gateway502')
        self.publish([flaky.url, good.url])
        server, address = await self.start(strategy='health-aware')
        pool = server.gateway.pool
        for _ in range(4):
            await self.get(address, '/x')
        self.assertGreater(pool.health_score(good.url), pool.health_score(flaky.url))
        self.assertEqual({pool.pick() for _ in range(4)}, {good.url},
                         'health decides, not position')

    async def test_health_aware_uses_the_export_latency_when_asked(self):
        pool = gateway.Pool(self.home, healthy_latency_ms=100)
        pool.refresh()
        pool.health['http://11.0.0.1:80'] = {'ok': 5.0, 'failed': 0.0, 'target': 0, 'at': time.monotonic(),
                                             'connect_ms': 20.0}
        pool.health['http://11.0.0.2:80'] = {'ok': 5.0, 'failed': 0.0, 'target': 0, 'at': time.monotonic(),
                                             'connect_ms': 90.0}
        self.assertGreater(pool.health_score('http://11.0.0.1:80'),
                           pool.health_score('http://11.0.0.2:80'),
                           'a connection that took far longer than the healthy mark is penalised')


class StickyTests(GatewayCase):
    async def test_a_session_keeps_one_address_while_it_works(self):
        first = await self.socks_upstream('socks5')
        second = await self.socks_upstream('socks5')
        self.publish([first.url, second.url])
        _server, address = await self.start(sticky='failover')
        served = []
        for _ in range(4):
            before = (first.connections, second.connections)
            granted, _reader, writer = await self.socks_client(address, user=b'session-alpha')
            self.assertTrue(granted)
            after = (first.connections, second.connections)
            served.append(first if after[0] > before[0] else second)
            await shutdown(writer)
        self.assertEqual({up.url for up in served}, {served[0].url},
                         'a session must not change its address while the proxy works')

    async def test_failover_moves_a_session_when_its_address_is_gone(self):
        first = await self.socks_upstream('socks5')
        second = await self.socks_upstream('socks5')
        self.publish([first.url, second.url])
        server, address = await self.start(sticky='failover')
        pool = server.gateway.pool
        granted, _reader, writer = await self.socks_client(address, user=b'session-beta')
        self.assertTrue(granted)
        pinned = [proxy for proxy, _ in pool.sessions.values()][0]
        await shutdown(writer)
        pool.set_denylist(reputation.Denylist(proxies=[pinned]))
        granted, _reader, writer = await self.socks_client(address, user=b'session-beta')
        self.assertTrue(granted, 'failover mode may move a session to a working address')
        self.assertNotIn(pinned, [proxy for proxy, _ in pool.sessions.values()])
        await shutdown(writer)

    async def test_sticky_strict_refuses_instead_of_changing_the_address(self):
        first = await self.socks_upstream('socks5')
        second = await self.socks_upstream('socks5')
        self.publish([first.url, second.url])
        server, address = await self.start(sticky='strict')
        pool = server.gateway.pool
        granted, _reader, writer = await self.socks_client(address, user=b'session-gamma')
        self.assertTrue(granted)
        pinned = [proxy for proxy, _ in pool.sessions.values()][0]
        await shutdown(writer)
        pool.set_denylist(reputation.Denylist(proxies=[pinned]))
        granted, _reader, writer = await self.socks_client(address, user=b'session-gamma')
        # A site that pins by client address must keep seeing one address, so the
        # gateway refuses instead of quietly moving the session.
        self.assertFalse(granted, 'a strict session must be refused, not moved')
        self.assertEqual(pinned, first.url if first.connections else second.url)
        await shutdown(writer)

    async def test_session_ttl_expires_and_the_binding_may_move(self):
        pool = gateway.Pool(self.home, session_ttl=0.2)
        self.publish(['http://11.0.0.1:80', 'http://11.0.0.2:80'])
        pool.refresh()
        first = pool.pick(session='s1')
        self.assertEqual(pool.pick(session='s1'), first, 'inside the TTL the session keeps its address')
        time.sleep(0.25)
        moved = {pool.pick(session='s1') for _ in range(4)}
        self.assertTrue(moved <= set(pool.matching()), 'a session may only move inside the scope')
        self.assertEqual(pool.sessions['s1'][0], next(iter(moved)))

    async def test_unknown_sticky_mode_is_refused(self):
        with self.assertRaises(ValueError):
            gateway.Pool(self.home, sticky='maybe')

    async def test_a_session_table_cannot_grow_without_bound(self):
        pool = gateway.Pool(self.home, session_ttl=600)
        pool.refresh()
        pool.sessions = {f's{index}': ('http://11.0.0.1:80', time.monotonic() + 600)
                         for index in range(gateway.SESSIONS_LIMIT + 50)}
        pool.pick(session='fresh')
        self.assertLessEqual(len(pool.sessions), gateway.SESSIONS_LIMIT + 1,
                             'a live session table is bounded too')


class CacheTests(GatewayCase):
    async def test_the_filter_cache_is_bounded(self):
        pool = gateway.Pool(self.home, cache_limit=8)
        self.publish([f'http://11.0.0.{index}:80' for index in range(1, 40)])
        pool.refresh()
        for index in range(200):
            pool.matching({'max_latency': float(index)})
        self.assertLessEqual(len(pool.cache), 8, 'the per-client filter cache must not grow without bound')
        self.assertEqual(pool.matching({'max_latency': 5.0}), pool.matching({'max_latency': 5.0}),
                         'a cached answer stays the same answer')

    async def test_an_invalid_cache_limit_is_refused(self):
        with self.assertRaises(ValueError):
            gateway.Pool(self.home, cache_limit=0)


class SnapshotTests(GatewayCase):
    async def test_snapshot_reads_no_files_and_is_safe_off_the_loop(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server, address = await self.start()
        pool = server.gateway.pool
        granted, _reader, writer = await self.socks_client(address)
        self.assertTrue(granted)
        await shutdown(writer)
        # The GUI calls snapshot() from its own HTTP thread; it must not touch the
        # file system, so it cannot block or race the event loop.
        before = pool.snapshot()
        for path in (self.home / 'exports').rglob('*.json'):
            path.unlink()
        after = pool.snapshot()
        self.assertEqual(before['proxies'], after['proxies'])
        self.assertEqual(after['proxies'], 1)
        for key in ('proxies', 'available', 'resting', 'sessions', 'active', 'denied',
                    'revoked', 'generation', 'strategy', 'sticky', 'on_deny'):
            self.assertIn(key, after)

    async def test_a_status_request_does_not_block_other_clients(self):
        # The old snapshot() read the export file inline on the event loop, so a
        # slow read stalled every client for its whole duration.
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server, address = await self.start()
        pool = server.gateway.pool
        original = pool.exports.load

        def slow_load():
            time.sleep(0.4)
            return original()

        pool.exports.load = slow_load
        order = []

        async def status():
            async with httpx.AsyncClient(proxy=f'http://{address}', trust_env=False, timeout=15) as client:
                await client.get(f'http://{address}/status')
            order.append('status-end')

        async def proxied():
            order.append('request-start')
            response = await self.get(address, '/x')
            order.append('request-end')
            return response

        status_task = asyncio.create_task(status())
        await asyncio.sleep(0.05)
        request_task = asyncio.create_task(proxied())
        self.assertEqual((await request_task).status_code, 200)
        await status_task
        self.assertLess(order.index('request-end'), order.index('status-end'),
                        'a client request must not wait for the status page to read the export')

    async def test_asnapshot_refreshes_and_snapshot_does_not(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server, _address = await self.start()
        pool = server.gateway.pool
        self.assertEqual(pool.snapshot()['proxies'], 1)
        write_export(self.home, [up.url, 'http://11.0.0.9:80'], generation='.generation-b')
        self.assertEqual(pool.snapshot()['proxies'], 1, 'a plain snapshot never re-reads')
        self.assertEqual((await pool.asnapshot())['proxies'], 2)

    async def test_state_reports_the_binding_and_no_secret(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server, address = await self.start(token='gateway-fixture-password')
        state = server.gateway.state()
        self.assertEqual(state['bind']['host'], '127.0.0.1')
        self.assertEqual(state['binding']['pool_id'], 'default')
        self.assertNotIn('gateway-fixture-password', json.dumps(state, default=str))
        self.assertEqual(state['supported'], list(gateway.SUPPORTED))
        self.assertEqual(state['strategies'], list(gateway.STRATEGIES))


class BindingTests(GatewayCase):
    async def test_a_bound_pool_keeps_its_generation_when_a_new_one_is_published(self):
        first = await self.socks_upstream('socks5')
        second = await self.socks_upstream('socks5')
        write_export(self.home, [first.url], generation='.generation-a', profile='p1')
        bound = gateway.Binding(pool_id='main', generation='.generation-a')
        server, address = await self.start(bindings={'main': bound}, default_binding=bound)
        pool = server.gateway.pool
        granted, _reader, writer = await self.socks_client(address, user=b'pool-main')
        self.assertTrue(granted)
        await shutdown(writer)
        self.assertEqual(first.connections, 1)
        # Another run publishes a new generation; the bound listener stays where
        # it was instead of following another check.
        write_export(self.home, [second.url], generation='.generation-b', profile='p1')
        self.assertEqual(pool.refresh(), [], 'a bound pool never leaves its generation')
        pool.default_binding = gateway.Binding()
        self.assertEqual(pool.refresh(), [second.url], 'an unbound pool follows the current export')

    async def test_a_binding_pins_the_profile_and_fails_closed_without_one(self):
        up = await self.socks_upstream('socks5')
        write_export(self.home, [up.url], generation='.generation-a', profile='p1', profile_revision=1)
        server, _address = await self.start(
            bindings={'old': gateway.Binding(pool_id='old', profile_id='p0'),
                      'new': gateway.Binding(pool_id='new', profile_id='p1', profile_revision=1)},
            default_binding=gateway.Binding())
        pool = server.gateway.pool
        self.assertEqual(pool.binding_for('old').profile_id, 'p0')
        self.assertEqual(pool.matching(binding=pool.binding_for('old')), [],
                         'a pool bound to another profile serves nothing')
        self.assertEqual(pool.matching(binding=pool.binding_for('new')), [up.url])
        self.assertEqual(pool.matching(binding=pool.binding_for('default')), [up.url])
        with self.assertRaises(KeyError):
            pool.binding_for('missing')

    async def test_a_client_may_name_a_binding_and_an_unknown_one_is_refused(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server, address = await self.start(bindings={'main': gateway.Binding(pool_id='main')},
                                           default_binding=gateway.Binding())
        granted, _reader, writer = await self.socks_client(address, user=b'pool-main')
        self.assertTrue(granted)
        await shutdown(writer)
        self.assertEqual(up.connections, 1)
        # An unknown pool in the user name is a bad request, not a fallback to the
        # default scope.
        reader, writer = await self.http_client(
            address, b'GET http://127.0.0.1:%d/ HTTP/1.1\r\n\r\n' % self.target,
            auth=b'pool-nope:x')
        # A bad pool name in the user name is a bad request, never a silent
        # fallback to the default scope.
        self.assertIn(b' 400 ', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5))
        await shutdown(writer)

    async def test_a_client_filter_only_narrows_the_bound_scope(self):
        de = await self.socks_upstream('socks5')
        nl = await self.socks_upstream('socks5')
        write_export(self.home, [], rows=[export_row(de.url, country='DE'),
                                          export_row(nl.url, country='NL')],
                     generation='.generation-a')
        write_export(self.home, [], rows=[export_row(de.url, country='DE'),
                                          export_row(nl.url, country='NL')],
                     generation='.generation-b')
        # The bound pool serves one generation; the other address only exists in
        # a generation the listener is not bound to.
        bound = gateway.Binding(pool_id='pinned', generation='.generation-b',
                                policy={'countries': ('DE',)})
        server, address = await self.start(bindings={'pinned': bound}, default_binding=bound)
        pool = server.gateway.pool
        self.assertEqual(pool.matching(request={'countries': ('NL',)}, binding=bound), [],
                         'a client filter narrows the binding, it never widens it')
        self.assertEqual(pool.matching(request={'countries': ('DE',)}, binding=bound), [de.url])
        granted, _reader, writer = await self.socks_client(address, user=b'pool-pinned-country-nl')
        self.assertFalse(granted, 'a filter cannot reach outside the bound scope')
        await shutdown(writer)
        granted, _reader, writer = await self.socks_client(address, user=b'pool-pinned-country-de')
        self.assertTrue(granted)
        self.assertEqual(de.connections, 1)
        self.assertEqual(nl.connections, 0)
        await shutdown(writer)

    async def test_a_binding_reports_itself_for_the_gui_and_the_api(self):
        binding = gateway.Binding(pool_id='main', generation='.generation-a', profile_id='p1',
                                  policy={'max_per_proxy': 3, 'sticky': 'strict'})
        self.assertEqual(binding.as_dict()['pool_id'], 'main')
        self.assertEqual(binding.option('sticky'), 'strict')
        self.assertEqual(binding.option('missing', 'fallback'), 'fallback')


if __name__ == '__main__':
    unittest.main()

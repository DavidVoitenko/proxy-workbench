"""Defect 17 / F16: a healthy TCP connection is not a successful request, and a sent
request is never sent again.

The old code called ``pool.ok(proxy)`` as soon as ``open_tunnel`` returned, so a
proxy that accepted a connection and never carried a byte was counted as
working, and a target that was down rested healthy proxies.  The second half of
the defect is the mirror image: a request that already reached one upstream must
never be written to another one, whatever its method.
"""
import asyncio
import unittest

from proxy_workbench import gateway
from tests.gateway_support import GatewayCase, shutdown


class HealthTests(GatewayCase):
    async def test_upstream_that_answers_502_is_not_rested(self):
        # A real proxy answers 502 when the target is unreachable.  The proxy is
        # working; the target is not.  Resting it would punish a good address
        # for somebody else's outage.
        up = await self.http_upstream('gateway502')
        self.publish([up.url])
        server, address = await self.start(max_failures=2, cooldown=300)
        pool = server.gateway.pool
        for _ in range(4):
            self.assertEqual((await self.get(address, '/x')).status_code, 502)
        report = pool.report(up.url)
        self.assertEqual(report['target_failed'], 4, report)
        self.assertEqual(report['failed'], 0, 'a target failure is not a proxy fault')
        self.assertNotIn(up.url, pool.resting, 'a working proxy must not rest because a target was down')
        self.assertEqual(pool.available(), [up.url])

    async def test_upstream_that_closes_without_answers_is_rested(self):
        # TCP open, handshake never happened: the old health check called this
        # "working" because the socket connected.
        up = await self.http_upstream('silent')
        self.publish([up.url])
        server, address = await self.start(max_failures=2, cooldown=300)
        pool = server.gateway.pool
        self.assertEqual((await self.get(address, '/x')).status_code, 502)
        self.assertEqual((await self.get(address, '/x')).status_code, 502)
        self.assertIn(up.url, pool.resting, 'an upstream that eats requests must rest')
        self.assertEqual(pool.report(up.url)['failed'], 2)
        self.assertEqual(pool.available(), [])

    async def test_upstream_answering_407_is_rested(self):
        # The upstream wants its own credentials: through this gateway it is
        # unusable, so it is a fault and it rests.
        up = await self.http_upstream('auth')
        self.publish([up.url])
        server, address = await self.start(max_failures=2, cooldown=300)
        pool = server.gateway.pool
        self.assertEqual((await self.get(address, '/x')).status_code, 407)
        self.assertEqual((await self.get(address, '/x')).status_code, 407)
        self.assertIn(up.url, pool.resting)
        self.assertEqual(pool.report(up.url)['failed'], 2)

    async def test_upstream_answering_non_http_is_rested(self):
        up = await self.http_upstream('garbage')
        self.publish([up.url])
        server, address = await self.start(max_failures=1, cooldown=300)
        pool = server.gateway.pool
        self.assertEqual((await self.get(address, '/x')).status_code, 502)
        self.assertIn(up.url, pool.resting, 'bytes that are not an HTTP answer are not a working proxy')

    async def test_working_upstream_is_counted_by_what_it_carried(self):
        up = await self.http_upstream('relay')
        self.publish([up.url])
        server, address = await self.start(max_failures=2, cooldown=300)
        pool = server.gateway.pool
        for _ in range(3):
            self.assertEqual((await self.get(address, '/x')).status_code, 200)
        self.assertEqual(pool.report(up.url)['ok'], 3)
        self.assertEqual(pool.report(up.url)['target_failed'], 0)
        self.assertNotIn(up.url, pool.resting)
        self.assertGreater(pool.health_score(up.url), 0.5)

    async def test_health_score_starts_neutral_and_ignores_target_failures(self):
        pool = gateway.Pool(self.home)
        self.assertEqual(pool.health_score('http://11.0.0.1:80'), 0.5, 'an unmeasured proxy is neither good nor bad')
        for _ in range(5):
            pool.outcome('http://11.0.0.2:80', 'upstream_unavailable')
        self.assertEqual(pool.health_score('http://11.0.0.2:80'), 0.5,
                         'a target that keeps refusing says nothing about the proxy')
        for _ in range(3):
            pool.outcome('http://11.0.0.3:80', 'upstream_refused')
        self.assertLess(pool.health_score('http://11.0.0.3:80'), 0.5)

    async def test_unknown_health_outcome_is_rejected(self):
        pool = gateway.Pool(self.home)
        with self.assertRaises(ValueError):
            pool.outcome('http://11.0.0.1:80', 'felt-fine')

    async def test_tunnel_evidence_only_counts_when_bytes_arrive(self):
        # A client that opens a socket and leaves is not evidence against the
        # proxy; a client that speaks and gets silence is.
        silent = await self.socks_upstream('socks5')
        self.publish([silent.url])
        server, address = await self.start(max_failures=1, cooldown=300)
        pool = server.gateway.pool
        granted, _reader, writer = await self.socks_client(address)
        self.assertTrue(granted)
        await shutdown(writer)
        await asyncio.sleep(0.2)
        self.assertEqual(pool.report(silent.url).get('failed', 0), 0, 'an abandoned socket is not a fault')
        self.assertNotIn(silent.url, pool.resting)

        granted, reader, writer = await self.socks_client(address)
        self.assertTrue(granted)
        writer.write(b'GET / HTTP/1.1\r\n\r\n')
        await writer.drain()
        self.assertTrue(await self.wait_for(lambda: pool.report(silent.url).get('ok') == 1))
        await shutdown(writer)


class ReplayTests(GatewayCase):
    async def test_a_request_that_reached_an_upstream_is_never_sent_again(self):
        silent = await self.http_upstream('silent')   # takes the request, answers nothing
        other = await self.http_upstream('relay')
        # Round-robin starts on the second candidate, so the silent upstream is
        # the one that receives the POST.
        self.publish([other.url, silent.url])
        server, address = await self.start()
        request = (b'POST http://127.0.0.1:%d/pay HTTP/1.1\r\nHost: x\r\n'
                   b'Content-Length: 4\r\n\r\nBODY' % self.target)
        reader, writer = await self.http_client(address, request)
        try:
            await asyncio.wait_for(reader.read(64), 10)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        delivered = len(silent.requests) + len(other.requests)
        self.assertEqual(delivered, 1, 'the POST must be delivered exactly once, '
                                       f'saw {len(silent.requests)}+{len(other.requests)}')
        self.assertEqual(server.gateway.pool.stats['replay_refused'], 0)
        await shutdown(writer)

    async def test_replay_guard_refuses_a_second_write_structurally(self):
        guard = gateway.ReplayGuard()
        lease = gateway.Lease(gateway.Pool(self.home), 'http://11.0.0.1:80')

        class Writer:
            def __init__(self):
                self.data = b''

            def write(self, payload):
                self.data += payload

        writer = Writer()
        guard.write_once(lease, writer, b'POST /pay HTTP/1.1\r\n\r\n')
        self.assertEqual(guard.sent_to, lease.proxy)
        with self.assertRaises(gateway.UpstreamError) as caught:
            guard.write_once(lease, writer, b'POST /pay HTTP/1.1\r\n\r\n')
        self.assertEqual(str(caught.exception), 'REPLAY_REFUSED')
        self.assertEqual(writer.data.count(b'POST /pay'), 1)

    async def test_a_tunnel_failure_before_anything_is_sent_may_move_on(self):
        # The rule is about bytes on the wire, not about the method name: while
        # the request is still unsent, trying the next upstream is not a replay.
        dead = await self.dead_upstream('http')
        alive = await self.http_upstream('relay')
        self.publish([dead.url, alive.url])
        server, address = await self.start()
        for index in range(2):
            self.assertEqual((await self.get(address, f'/moved{index}')).status_code, 200)
        # The dead address is taken at least once and the request is served
        # after the tunnel moved, not by replaying a written request.
        self.assertGreaterEqual(server.gateway.pool.stats['retries'], 1)
        self.assertEqual(len(alive.requests), 2)

    async def test_an_open_tunnel_is_never_moved_to_another_upstream(self):
        # Once a CONNECT tunnel exists, the bytes on it belong to the client and
        # to that upstream.  The gateway has no code path that re-sends them.
        first = await self.http_upstream('relay')
        second = await self.http_upstream('relay')
        self.publish([first.url, second.url])
        server, address = await self.start()
        reader, writer = await self.http_client(
            address, b'CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n' % self.target)
        self.assertIn(b' 200 ', await reader.readuntil(b'\r\n\r\n'))
        owner, other = (first, second) if first.connections else (second, first)
        before = (len(owner.requests), len(other.requests))
        writer.write(b'GET /t HTTP/1.1\r\n\r\n')
        await writer.drain()
        await asyncio.sleep(0.3)
        self.assertEqual((len(owner.requests), len(other.requests)), before,
                         'no second tunnel may be opened for bytes already in flight')
        self.assertEqual(server.gateway.pool.stats['replay_refused'], 0)
        self.assertIn(owner.targets[-1], (('connect', '127.0.0.1', self.target),))
        await shutdown(writer)


if __name__ == '__main__':
    unittest.main()

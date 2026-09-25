"""Defect 16 / R11: a slot is reserved before the await, and the whole handshake has a deadline.

The old code picked a proxy, awaited ``open_tunnel`` and only then called
``acquire()``, so N clients suspended in the same handshake all saw one free
slot.  These tests reproduce that interleaving for real: the upstreams accept
TCP and never finish their handshake, so the gateway is genuinely suspended in
``open_tunnel`` for several clients at once.
"""
import asyncio
import time
import unittest

from tests.gateway_support import GatewayCase, shutdown


class ReservationTests(GatewayCase):
    async def test_parallel_handshakes_never_share_a_slot(self):
        stuck = [await self.socks_upstream('socks5', stall=True),
                 await self.socks_upstream('socks5', stall=True)]
        self.publish([up.url for up in stuck])
        server, address = await self.start(max_per_proxy=1, connect_timeout=30, handshake_timeout=30)
        pool = server.gateway.pool
        peak = {'per_proxy': 0, 'upstream': 0}
        stop = asyncio.Event()

        async def sample():
            # Watch the reservation counters while the clients are in flight.
            while not stop.is_set():
                peak['per_proxy'] = max(peak['per_proxy'], max(pool.active.values(), default=0))
                peak['upstream'] = max(peak['upstream'], sum(up.open for up in stuck))
                await asyncio.sleep(0.005)

        watcher = asyncio.create_task(sample())
        clients = [asyncio.create_task(self.socks_client(address)) for _ in range(3)]
        # Two of the three find a free slot and suspend inside open_tunnel; the
        # third has nothing left to take and must be refused, not queued.
        self.assertTrue(await self.wait_for(lambda: sum(up.open for up in stuck) == 2),
                        f'expected two suspended connects, got {[up.open for up in stuck]}')
        await asyncio.sleep(0.05)
        # With the old pick()->await->acquire() order all three clients were
        # inside open_tunnel at once and one upstream carried two of them.
        self.assertEqual(peak['upstream'], 2, 'max_per_proxy was exceeded while handshakes overlapped')
        self.assertEqual(sum(up.connections for up in stuck), 2,
                         'the third client must never reach an upstream that is already busy')
        self.assertEqual(peak['per_proxy'], 1, 'the pool never reported more than one slot per proxy')
        self.assertEqual(sum(pool.active.values()), 2)
        refused = [task for task in clients if task.done() and not task.exception() and not task.result()[0]]
        self.assertEqual(len(refused), 1, 'the third client must be refused, not given a busy proxy')

        stop.set()
        await watcher
        for task in clients:
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)

    async def test_cancelled_connect_releases_the_reservation(self):
        stuck = await self.socks_upstream('socks5', stall=True)
        self.publish([stuck.url])
        server, _ = await self.start(max_per_proxy=1, connect_timeout=30, handshake_timeout=30)
        pool = server.gateway.pool
        connect = asyncio.create_task(server.gateway.connect('127.0.0.1', self.target))
        self.assertTrue(await self.wait_for(lambda: pool.active.get(stuck.url, 0) == 1))
        connect.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await connect
        # A cancellation between the reservation and the handshake used to leak
        # the slot for as long as the connect timeout.
        self.assertTrue(await self.wait_for(lambda: not pool.active), f'active={pool.active}')
        lease = pool.reserve()
        self.assertIsNotNone(lease, 'the slot must be free again after a cancel')
        lease.release()

    async def test_shutdown_releases_a_slot_held_by_a_suspended_handshake(self):
        stuck = await self.socks_upstream('socks5', stall=True)
        self.publish([stuck.url])
        server, address = await self.start(max_per_proxy=1, connect_timeout=30, handshake_timeout=30)
        pool = server.gateway.pool
        client = asyncio.create_task(self.socks_client(address))
        self.assertTrue(await self.wait_for(lambda: pool.active.get(stuck.url, 0) == 1))
        report = await server.gateway.shutdown(0)
        self.assertEqual(report['forced'], 1)
        self.assertTrue(await self.wait_for(lambda: not pool.active), f'active={pool.active}')
        client.cancel()
        await asyncio.gather(client, return_exceptions=True)

    async def test_failed_handshake_gives_the_slot_back(self):
        broken = await self.http_upstream('silent')
        self.publish([broken.url])
        server, address = await self.start(max_per_proxy=1)
        pool = server.gateway.pool
        response = await self.get(address, '/x')
        self.assertEqual(response.status_code, 502, 'the client is told, not left with a dead socket')
        self.assertTrue(await self.wait_for(lambda: not pool.active))
        self.assertEqual(pool.report(broken.url)['failed'], 1)
        self.assertNotIn(broken.url, pool.resting, 'one failure is not yet a resting proxy')
        lease = pool.reserve()
        self.assertIsNotNone(lease)
        lease.release()

    async def test_lease_release_is_idempotent(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server, _ = await self.start(max_per_proxy=1)
        pool = server.gateway.pool
        lease = pool.reserve()
        self.assertEqual(pool.active[up.url], 1)
        lease.release()
        lease.release()
        self.assertNotIn(up.url, pool.active)

    async def test_deadline_covers_the_whole_handshake_not_the_first_byte(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        # The old code bounded only readexactly(1); read_head then waited
        # forever, so a client that said "G" and stopped held a handler open.
        server, address = await self.start(handshake_timeout=0.5, connect_timeout=30)
        host, _, port = address.rpartition(':')
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(b'G')
        await writer.drain()
        started = time.monotonic()
        self.assertEqual(await asyncio.wait_for(reader.read(1), 10), b'')
        self.assertLess(time.monotonic() - started, 5, 'the handshake deadline must end the stalled request')
        self.assertGreaterEqual(server.gateway.pool.stats['closed_deadline'], 1)
        await shutdown(writer)

    async def test_deadline_also_covers_the_upstream_tunnel(self):
        stuck = await self.socks_upstream('socks5', stall=True)
        self.publish([stuck.url])
        # The client sends a complete SOCKS5 greeting; the upstream never
        # answers.  A generous connect_timeout must not extend the handshake.
        server, address = await self.start(handshake_timeout=0.5, connect_timeout=30)
        host, _, port = address.rpartition(':')
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(b'\x05\x01\x00'
                     + b'\x05\x01\x00\x01' + bytes([127, 0, 0, 1]) + self.target.to_bytes(2, 'big'))
        await writer.drain()
        self.assertEqual(await reader.readexactly(2), b'\x05\x00', 'the greeting is answered at once')
        started = time.monotonic()
        self.assertEqual(await asyncio.wait_for(reader.read(1), 10), b'')
        self.assertLess(time.monotonic() - started, 5,
                        'the upstream tunnel is part of the handshake and shares its deadline')
        self.assertEqual(stuck.connections, 1)
        await shutdown(writer)

    async def test_incoming_connections_are_bounded(self):
        up = await self.socks_upstream('socks5', stall=True)
        self.publish([up.url])
        server, address = await self.start(max_clients=2, connect_timeout=30, handshake_timeout=30)
        host, _, port = address.rpartition(':')
        writers = []
        for _ in range(4):
            _reader, writer = await asyncio.open_connection(host, int(port))
            writers.append(writer)
        # The ones over the bound are told the service is unavailable instead of
        # being allowed to pile up as undialed sockets.
        self.assertTrue(await self.wait_for(lambda: server.gateway.pool.stats['rejected'] >= 2))
        self.assertLessEqual(len(server.gateway.tasks), 2)
        for writer in writers:
            await shutdown(writer)

    async def test_max_per_proxy_applies_to_parallel_plain_http_requests(self):
        first = await self.http_upstream('relay', head_delay=1.0)
        second = await self.http_upstream('relay', head_delay=1.0)
        self.publish([first.url, second.url])
        server, address = await self.start(max_per_proxy=1, response_timeout=10)
        pool = server.gateway.pool
        responses = await asyncio.gather(*(self.get(address, f'/p{index}') for index in range(4)),
                                         return_exceptions=True)
        codes = [getattr(item, 'status_code', repr(item)) for item in responses]
        self.assertEqual(codes.count(200), 2, f'only two requests fit, got {codes}')
        self.assertEqual(first.peak_open, 1, 'one concurrent connection per proxy')
        self.assertEqual(second.peak_open, 1, 'one concurrent connection per proxy')
        self.assertTrue(await self.wait_for(lambda: not pool.active), f'active={pool.active}')


if __name__ == '__main__':
    unittest.main()

"""Defect 19 / R13: the denylist is applied before any connect, and it revokes admissions.

The old gateway never looked at ``data/denylist.txt`` at all, so a forbidden
address stayed in the rotation and kept being dialled.  Adding a rule must also
do something about streams that are already open - and that is a separate,
explicit decision (``on_deny``), not a side effect.
"""
import asyncio
import unittest

from proxy_workbench import reputation
from proxy_workbench import gateway
from tests.gateway_support import GatewayCase, shutdown


class DenylistTests(GatewayCase):
    async def test_a_denied_address_is_never_dialled(self):
        denied = await self.socks_upstream('socks5')
        allowed = await self.socks_upstream('socks5')
        self.publish([denied.url, allowed.url])
        # The rule names the upstream the way the GUI writes one.
        self.deny(denied.url)
        server, address = await self.start(denylist_normalizer=self.local_normalizer)
        pool = server.gateway.pool
        self.assertNotIn(denied.url, pool.refresh(), 'a denied address is not an admission')
        self.assertEqual(pool.available(), [allowed.url])
        for _ in range(3):
            self.assertEqual((await self.get(address, '/x')).status_code, 200)
        # The denylist is consulted before the prefilter, so the denied upstream
        # never even gets a TCP connection - not a "failed" one.
        self.assertEqual(denied.connections, 0, 'a denied address must not be dialled at all')
        self.assertGreater(allowed.connections, 0)

    async def test_a_denied_address_is_never_reserved(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        pool = gateway.Pool(self.home)
        pool.refresh()
        lease = pool.reserve()
        self.assertEqual(lease.proxy, up.url)
        lease.release()
        pool.set_denylist(reputation.Denylist(proxies=[up.url]))
        self.assertIsNone(pool.reserve(), 'a denied address must not be reservable')
        self.assertIsNone(pool.pick())
        self.assertEqual(pool.denied_proxies(), [up.url])

    async def test_a_new_rule_revokes_admissions_immediately(self):
        first = await self.socks_upstream('socks5')
        second = await self.socks_upstream('socks5')
        self.publish([first.url, second.url])
        server, address = await self.start()
        pool = server.gateway.pool
        self.assertEqual(sorted(pool.available()), sorted([first.url, second.url]))
        # Give one address a session so the revocation has something to drop.
        granted, _reader, _writer = await self.socks_client(address, user=b'session-keepme')
        self.assertTrue(granted)
        self.assertTrue(pool.sessions)

        pool.set_denylist(reputation.Denylist(proxies=[first.url]))
        self.assertNotIn(first.url, pool.available())
        self.assertNotIn(first.url, [row['proxy'] for row in pool.rows])
        self.assertEqual(pool.cache, {}, 'a cached filter result must not survive a revocation')
        # A session binding is left alone on purpose: whether a denied address
        # means "move" or "refuse" is the sticky mode's decision, and the strict
        # refusal is covered in test_gateway_rotation.
        self.assertEqual(pool.sticky, 'failover')
        before = first.connections
        for _ in range(3):
            await self.get(address, '/y')
        self.assertEqual(first.connections, before, 'a revoked address must not serve new requests')

    async def test_the_rule_file_is_re_read_without_a_restart(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server, address = await self.start(refresh_interval=0.05,
                                           denylist_normalizer=self.local_normalizer)
        pool = server.gateway.pool
        self.assertEqual(pool.available(), [up.url])
        self.deny(up.url)
        self.assertTrue(await self.wait_for(lambda: pool.available() == []),
                        'a new rule must revoke admissions without a restart')
        self.assertEqual((await self.get(address, '/z')).status_code, 502)
        self.assertEqual(up.connections, 0)

    async def test_an_open_stream_survives_by_default(self):
        # The default is 'keep' and it is a decision, not an omission: bytes are
        # already on the wire and cutting them is the user's call.
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server, address = await self.start()
        granted, reader, writer = await self.socks_client(address)
        self.assertTrue(granted)
        server.gateway.set_denylist(reputation.Denylist(proxies=[up.url]))
        self.assertEqual(server.gateway.pool.denied_proxies(), [up.url])
        writer.write(b'GET /still HTTP/1.1\r\n\r\n')
        await writer.drain()
        self.assertTrue(await self.wait_for(lambda: reader.at_eof() is False))
        answer = await asyncio.wait_for(reader.read(4096), 5)
        self.assertIn(b'still', answer, 'an open stream is not cut when on_deny is keep')
        await shutdown(writer)

    async def test_open_streams_are_closed_when_the_user_asks_for_it(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        server, address = await self.start(on_deny='close')
        granted, reader, writer = await self.socks_client(address)
        self.assertTrue(granted)
        closed = server.gateway.set_denylist(reputation.Denylist(proxies=[up.url]))
        self.assertEqual(closed, [up.url])
        self.assertTrue(await self.wait_for(lambda: reader.at_eof()),
                        'on_deny=close ends the streams the rule names')
        await shutdown(writer)

    async def test_revoke_policy_is_validated_and_reported(self):
        with self.assertRaises(ValueError):
            gateway.Pool(self.home, on_deny='burn-it-down')
        pool = gateway.Pool(self.home)
        self.assertEqual(pool.snapshot()['on_deny'], 'keep')
        self.assertEqual(gateway.REVOKE_POLICIES, ('keep', 'close'))

    async def test_a_broken_denylist_file_does_not_silently_deny_everything(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        pool = gateway.Pool(self.home)
        pool.refresh()
        pool.set_denylist(reputation.Denylist(error='READ_ERROR'))
        self.assertEqual(pool.denylist.error, 'READ_ERROR')
        self.assertEqual(pool.available(), [up.url], 'a rule file that could not be read admits nothing away')
        self.assertEqual(pool.available(), [up.url])

    async def test_removing_a_rule_gives_the_address_back(self):
        # A denylist that can only subtract is a one-way ratchet: rows used to
        # be edited in place, so a rule the user deleted left its proxy out of
        # the rotation for the whole life of the generation.
        denied = await self.socks_upstream('socks5')
        kept = await self.socks_upstream('socks5')
        self.publish([denied.url, kept.url])
        self.deny(denied.url)
        server, _ = await self.start(denylist_normalizer=self.local_normalizer)
        pool = server.gateway.pool
        self.assertEqual(pool.available(), [kept.url])

        # The user deletes the rule, exactly as the GUI rewrites the file.
        self.deny()
        self.assertTrue(await self.wait_for(lambda: sorted(pool.available()) == sorted([denied.url, kept.url])),
                        f'removing a rule must restore its address, got {pool.available()}')
        self.assertEqual(pool.denied_proxies(), [])
        self.assertEqual(pool.revoked, 1, 'the revocation is still counted, it is only not permanent')
        lease = pool.reserve()
        self.assertIsNotNone(lease)
        lease.release()

    async def test_an_explicit_empty_denylist_restores_everything(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        pool = gateway.Pool(self.home, denylist_normalizer=self.local_normalizer)
        pool.refresh()
        pool.set_denylist(reputation.Denylist(proxies=[up.url]))
        self.assertEqual(pool.available(), [])
        # The documented revoke entry point has to be able to undo itself, not
        # only add: a GUI that offers "revoke from the active pool" needs both.
        pool.set_denylist(reputation.Denylist.empty())
        self.assertEqual(pool.available(), [up.url])
        self.assertEqual(pool.pick(), up.url)

    async def test_the_revoke_policy_lives_in_one_place(self):
        up = await self.socks_upstream('socks5')
        self.publish([up.url])
        # 'keep' is the configured default, so the pool itself answers "no
        # streams to cut"; Gateway.set_denylist() asks it rather than keeping a
        # second copy of the decision.
        keep = gateway.Pool(self.home, on_deny='keep')
        keep.refresh()
        keep.set_denylist(reputation.Denylist(proxies=[up.url]))
        self.assertEqual(keep.revoke_streams(), [], 'keep must not cut a stream that is already open')
        close = gateway.Pool(self.home, on_deny='close')
        close.refresh()
        close.set_denylist(reputation.Denylist(proxies=[up.url]))
        self.assertEqual(close.revoke_streams(), [up.url])
        # An explicit request overrides the configured policy for one call.
        self.assertEqual(keep.revoke_streams(force=True), [up.url])
        self.assertEqual(close.revoke_streams(force=False), [])


if __name__ == '__main__':
    unittest.main()

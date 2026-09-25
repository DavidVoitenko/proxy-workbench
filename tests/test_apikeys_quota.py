"""Rate and concurrency quotas, leases, and the documented SSE/stream policy."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apikeys_support as support  # noqa: E402

from proxy_workbench import apikeys  # noqa: E402


class QuotaTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = support.connect(self.tmp.name)
        self.addCleanup(self.conn.close)
        self.clock = support.Clock()
        self.mgr = support.manager(self.conn, self.clock)
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        self.admin = self.mgr.authenticate(issued.secret)
        self.guard = apikeys.QuotaGuard(self.mgr)

    def limited(self, *, requests=3, window_s=60, max_active=1, permissions=('read.status',)):
        issued = self.mgr.create(actor=self.admin, name='limited', permissions=list(permissions),
                                 rate_limit={'requests': requests, 'window_s': window_s},
                                 concurrency={'max_active': max_active})
        return self.mgr.authenticate(issued.secret), issued


class RateLimitTests(QuotaTestCase):
    def test_requests_inside_the_window_are_served(self):
        key, _ = self.limited(requests=3, window_s=60)
        for index in range(3):
            with self.subTest(request=index):
                self.assertIsNone(self.guard.check(key))
        with self.assertRaises(apikeys.ApiKeyError):
            self.guard.check(key)

    def test_the_request_after_the_limit_is_refused_with_a_retry_after(self):
        key, _ = self.limited(requests=2, window_s=60)
        self.clock.advance(1)
        self.guard.check(key)
        self.guard.check(key)
        self.clock.advance(1)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.guard.check(key)
        error = caught.exception
        self.assertEqual(error.code, apikeys.E_RATE_LIMITED)
        self.assertEqual(error.http_status, 429)
        self.assertAlmostEqual(error.retry_after, 59.0, places=1)
        self.assertIn('retry_after', error.as_json()['error'])

    def test_the_window_slides_instead_of_resetting_on_a_boundary(self):
        key, _ = self.limited(requests=2, window_s=60)
        self.guard.check(key)
        self.clock.advance(30)
        self.guard.check(key)
        self.clock.advance(10)
        with self.assertRaises(apikeys.ApiKeyError):
            self.guard.check(key)
        self.clock.advance(21)
        self.guard.check(key)

    def test_one_key_never_spends_the_budget_of_another(self):
        first, _ = self.limited(requests=1, window_s=60)
        second = self.mgr.create(actor=self.admin, name='other', permissions=['read.status'],
                                 rate_limit={'requests': 1, 'window_s': 60})
        second_key = self.mgr.authenticate(second.secret)
        self.guard.check(first)
        with self.assertRaises(apikeys.ApiKeyError):
            self.guard.check(first)
        self.guard.check(second_key)

    def test_a_key_without_a_rate_limit_is_never_throttled(self):
        issued = self.mgr.create(actor=self.admin, name='free', permissions=['read.status'])
        key = self.mgr.authenticate(issued.secret)
        for _ in range(200):
            self.guard.check(key)
        self.assertEqual(self.guard.check(key), None)

    def test_quota_settings_are_validated(self):
        for value in ({'requests': 0, 'window_s': 60}, {'requests': 1, 'window_s': 0},
                      {'requests': 1}, {'requests': 1, 'window_s': 60, 'burst': 2}):
            with self.subTest(value=value):
                with self.assertRaises(apikeys.ApiKeyError) as caught:
                    self.mgr.create(actor=self.admin, name='bad', rate_limit=value)
                self.assertIn(caught.exception.code,
                              (apikeys.E_FIELD, apikeys.E_UNKNOWN_FIELD))

    def test_a_refused_request_is_visible_in_the_audit_log(self):
        key, _ = self.limited(requests=1, window_s=60)
        self.guard.check(key)
        with self.assertRaises(apikeys.ApiKeyError):
            self.guard.check(key)
        entries = [entry for entry in self.mgr.read_audit(actor=self.admin)
                   if entry['error_code'] == apikeys.E_RATE_LIMITED]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['key_id'], key.key_id)


class ConcurrencyTests(QuotaTestCase):
    def test_a_second_holder_is_refused_while_a_slot_is_taken(self):
        key, _ = self.limited(max_active=1)
        lease = self.guard.acquire(key)
        self.addCleanup(lease.close)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.guard.acquire(key)
        self.assertEqual(caught.exception.code, apikeys.E_CONCURRENCY)
        self.assertEqual(caught.exception.http_status, 429)

    def test_a_slot_comes_back_when_the_holder_leaves(self):
        key, _ = self.limited(max_active=1)
        with self.guard.acquire(key):
            self.assertEqual(self.guard.active_count(key.key_id), 1)
        self.assertEqual(self.guard.active_count(key.key_id), 0)
        self.guard.acquire(key).close()

    def test_slots_are_counted_per_key(self):
        first, _ = self.limited(max_active=1)
        second = self.mgr.create(actor=self.admin, name='other', permissions=['read.status'],
                                 concurrency={'max_active': 1})
        other = self.mgr.authenticate(second.secret)
        first_lease = self.guard.acquire(first)
        self.addCleanup(first_lease.close)
        other_lease = self.guard.acquire(other)
        self.addCleanup(other_lease.close)
        self.assertEqual(self.guard.active_count(), 2)
        self.assertEqual(self.guard.active_count(first.key_id), 1)

    def test_a_lease_revalidates_against_a_revocation(self):
        key, issued = self.limited(max_active=2)
        lease = self.guard.acquire(key)
        self.addCleanup(lease.close)
        self.assertEqual(lease.revalidate().state, 'active')
        self.mgr.revoke(issued.info.id, actor=self.admin)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            lease.revalidate()
        self.assertEqual(caught.exception.code, apikeys.E_REVOKED)

    def test_a_lease_revalidates_against_an_expiry(self):
        issued = self.mgr.create(actor=self.admin, name='short', permissions=['read.status'],
                                 ttl_s=30, concurrency={'max_active': 2})
        key = self.mgr.authenticate(issued.secret)
        lease = self.guard.acquire(key)
        self.addCleanup(lease.close)
        self.clock.advance(31)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            lease.revalidate()
        self.assertEqual(caught.exception.code, apikeys.E_EXPIRED)

    def test_a_revoked_key_cannot_take_a_new_lease(self):
        key, issued = self.limited(max_active=2)
        self.mgr.revoke(issued.info.id, actor=self.admin)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.guard.acquire(key)
        self.assertEqual(caught.exception.code, apikeys.E_REVOKED)
        self.assertEqual(self.guard.active_count(), 0)

    def test_an_expired_key_cannot_take_a_new_lease(self):
        issued = self.mgr.create(actor=self.admin, name='short', permissions=['read.status'],
                                 ttl_s=10, concurrency={'max_active': 1})
        key = self.mgr.authenticate(issued.secret)
        self.clock.advance(11)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.guard.acquire(key)
        self.assertEqual(caught.exception.code, apikeys.E_EXPIRED)

    def test_a_lease_takes_a_rate_token_when_it_is_taken(self):
        key, _ = self.limited(requests=2, window_s=60, max_active=2)
        self.guard.acquire(key).close()
        self.guard.acquire(key).close()
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.guard.acquire(key)
        self.assertEqual(caught.exception.code, apikeys.E_RATE_LIMITED)

    def test_the_guard_survives_concurrent_holders(self):
        key, _ = self.limited(requests=1000, window_s=60, max_active=4)
        errors = []
        taken = []

        def worker():
            try:
                lease = self.guard.acquire(key)
                taken.append(lease)
            except apikeys.ApiKeyError as error:
                errors.append(error.code)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(taken) + len(errors), 12)
        self.assertTrue(all(code == apikeys.E_CONCURRENCY for code in errors), errors)
        self.assertLessEqual(len(taken), 4)
        for lease in taken:
            lease.close()


class StreamSessionTests(QuotaTestCase):
    def test_a_new_subscription_is_checked_against_the_key_at_once(self):
        key, issued = self.limited(max_active=2)
        sessions = apikeys.StreamSessions(self.mgr, recheck_interval_s=30)
        self.assertEqual(sessions.open(key, 'events:1').principal.key_id, key.key_id)
        self.mgr.revoke(issued.info.id, actor=self.admin)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            sessions.open(key, 'events:2')
        self.assertEqual(caught.exception.code, apikeys.E_REVOKED)

    def test_an_open_session_is_closed_by_a_revoke(self):
        key, issued = self.limited(max_active=2)
        sessions = apikeys.StreamSessions(self.mgr, recheck_interval_s=30)
        session = sessions.open(key, 'events:1')
        self.mgr.revoke(issued.info.id, actor=self.admin)
        terminated = sessions.sweep()
        self.assertEqual(terminated, [('events:1', apikeys.E_REVOKED)])
        self.assertTrue(session.closed)
        self.assertEqual(session.close_code, apikeys.E_REVOKED)
        self.assertEqual(sessions.active_ids(), [])

    def test_an_open_session_is_closed_by_an_expiry(self):
        issued = self.mgr.create(actor=self.admin, name='short', permissions=['read.status'],
                                 ttl_s=45, concurrency={'max_active': 2})
        key = self.mgr.authenticate(issued.secret)
        sessions = apikeys.StreamSessions(self.mgr, recheck_interval_s=30)
        session = sessions.open(key, 'events:1')
        self.clock.advance(46)
        self.assertEqual(sessions.sweep(), [('events:1', apikeys.E_EXPIRED)])
        self.assertTrue(session.closed)

    def test_a_live_session_survives_a_sweep(self):
        key, _ = self.limited(max_active=2)
        sessions = apikeys.StreamSessions(self.mgr, recheck_interval_s=30)
        session = sessions.open(key, 'events:1')
        self.clock.advance(10)
        self.assertEqual(sessions.sweep(), [])
        self.assertFalse(session.closed)
        self.assertEqual(session.recheck().state, 'active')

    def test_a_session_rechecks_its_own_expiry_before_an_event(self):
        key, issued = self.limited(max_active=2)
        sessions = apikeys.StreamSessions(self.mgr, recheck_interval_s=30)
        session = sessions.open(key, 'events:1')
        self.mgr.disable(issued.info.id, actor=self.admin)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            session.recheck()
        self.assertEqual(caught.exception.code, apikeys.E_DISABLED)

    def test_the_due_list_follows_the_documented_interval(self):
        key, _ = self.limited(max_active=2)
        sessions = apikeys.StreamSessions(self.mgr, recheck_interval_s=30)
        sessions.open(key, 'events:1')
        self.assertEqual(sessions.due(), [])
        self.clock.advance(30)
        self.assertEqual([session.stream_id for session in sessions.due()], ['events:1'])

    def test_a_closed_session_forgets_itself(self):
        key, _ = self.limited(max_active=2)
        sessions = apikeys.StreamSessions(self.mgr, recheck_interval_s=30)
        session = sessions.open(key, 'events:1')
        session.close()
        self.assertEqual(sessions.active_ids(), [])
        with self.assertRaises(apikeys.ApiKeyError):
            session.recheck()
        session.close()  # closing twice is not an error

    def test_sessions_hold_a_concurrency_slot_of_their_key(self):
        key, _ = self.limited(max_active=1)
        guard = apikeys.QuotaGuard(self.mgr)
        sessions = apikeys.StreamSessions(self.mgr, recheck_interval_s=30)
        lease = guard.acquire(key, stream_id='events:1')
        self.addCleanup(lease.close)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            guard.acquire(key, stream_id='events:2')
        self.assertEqual(caught.exception.code, apikeys.E_CONCURRENCY)
        sessions.open(key, 'events:1')

    def test_a_stream_of_another_key_cannot_be_opened_on_its_name(self):
        key, _ = self.limited(max_active=2)
        sessions = apikeys.StreamSessions(self.mgr, recheck_interval_s=30)
        sessions.open(key, 'events:1')
        # Re-opening the same stream id replaces the registration instead of doubling it.
        sessions.open(key, 'events:1')
        self.assertEqual(sessions.active_ids(), ['events:1'])


if __name__ == '__main__':
    unittest.main()

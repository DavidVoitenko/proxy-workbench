"""Key lifecycle: list, rename, disable, revoke, delete, rotate and the narrow grace window."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apikeys_support as support  # noqa: E402

from proxy_workbench import apikeys  # noqa: E402


class LifecycleTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = support.connect(self.tmp.name)
        self.addCleanup(self.conn.close)
        self.clock = support.Clock()
        self.mgr = support.manager(self.conn, self.clock)
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        self.admin = self.mgr.authenticate(issued.secret)
        self.admin_issued = issued


class ListingTests(LifecycleTestCase):
    def test_the_list_shows_id_prefix_and_dates_but_never_a_secret(self):
        issued = self.mgr.create(actor=self.admin, name='phone subscription',
                                 purpose='read only for a phone', ttl_s=3600,
                                 permissions=['read.status'], rate_limit={'requests': 10,
                                                                          'window_s': 60},
                                 concurrency={'max_active': 2})
        listed = {info.id: info for info in self.mgr.list_keys(actor=self.admin)}
        entry = listed[issued.info.id]
        self.assertEqual(entry.name, 'phone subscription')
        self.assertEqual(entry.purpose, 'read only for a phone')
        self.assertEqual(entry.prefix, issued.info.prefix)
        self.assertEqual(entry.created_at, self.clock.value)
        self.assertEqual(entry.expires_at, self.clock.value + 3600)
        self.assertEqual(entry.expires_in_s(self.clock.value), 3600)
        self.assertIsNone(entry.last_used_at)
        self.assertEqual(entry.state, 'active')
        self.assertEqual(entry.rate_limit, apikeys.RateLimit(10, 60))
        self.assertEqual(entry.concurrency, apikeys.Concurrency(2))
        self.assertNotIn(issued.secret, repr(entry.as_dict()))

    def test_last_used_moves_after_the_key_is_used(self):
        issued = self.mgr.create(actor=self.admin, name='reader', permissions=['read.status'])
        self.assertIsNone(self.mgr.get_key(issued.info.id).last_used_at)
        self.clock.advance(120)
        self.mgr.authenticate(issued.secret)
        self.assertEqual(self.mgr.get_key(issued.info.id).last_used_at, self.clock.value)

    def test_last_used_is_not_rewritten_on_every_call(self):
        issued = self.mgr.create(actor=self.admin, name='reader', permissions=['read.status'])
        self.clock.advance(10)
        self.mgr.authenticate(issued.secret)
        self.assertEqual(self.mgr.get_key(issued.info.id).last_used_at, self.clock.value)
        first = self.clock.value
        self.clock.advance(5)
        self.mgr.authenticate(issued.secret)
        self.assertEqual(self.mgr.get_key(issued.info.id).last_used_at, first)
        self.clock.advance(600)
        self.mgr.authenticate(issued.secret)
        self.assertEqual(self.mgr.get_key(issued.info.id).last_used_at, self.clock.value)

    def test_a_revoked_key_stays_in_the_list_with_its_metadata(self):
        issued = self.mgr.create(actor=self.admin, name='temporary', ttl_s=60)
        self.mgr.revoke(issued.info.id, actor=self.admin)
        states = {info.id: info.state for info in self.mgr.list_keys(actor=self.admin)}
        self.assertEqual(states[issued.info.id], 'revoked')
        self.assertEqual([info.id for info in self.mgr.list_keys(actor=self.admin, state='revoked')],
                         [issued.info.id])
        self.assertEqual([info.id for info in self.mgr.list_keys(actor=self.admin, state='active')],
                         [self.admin.key_id])


class MetadataTests(LifecycleTestCase):
    def test_rename_and_redocumentation(self):
        issued = self.mgr.create(actor=self.admin, name='first', purpose='old')
        updated = self.mgr.update_metadata(issued.info.id, actor=self.admin, name='second',
                                           purpose='new purpose')
        self.assertEqual(updated.name, 'second')
        self.assertEqual(updated.purpose, 'new purpose')
        self.assertEqual(updated.id, issued.info.id)
        self.assertEqual(updated.permissions, issued.info.permissions)

    def test_renaming_does_not_change_the_secret_or_the_rights(self):
        issued = self.mgr.create(actor=self.admin, name='first', permissions=['read.results'])
        self.mgr.update_metadata(issued.info.id, actor=self.admin, name='second')
        principal = self.mgr.authenticate(issued.secret)
        self.assertEqual(principal.key_id, issued.info.id)
        self.assertTrue(principal.has('read.results'))

    def test_metadata_never_carries_a_permission_or_a_scope_change(self):
        issued = self.mgr.create(actor=self.admin, name='first', permissions=['read.results'])
        with self.assertRaises(TypeError):
            self.mgr.update_metadata(issued.info.id, actor=self.admin, permissions=['admin.keys'])
        with self.assertRaises(TypeError):
            self.mgr.update_metadata(issued.info.id, actor=self.admin, scope={})
        self.assertEqual(self.mgr.get_key(issued.info.id).permissions, ('read.results',))

    def test_an_expiry_can_be_moved_forward(self):
        issued = self.mgr.create(actor=self.admin, name='short', ttl_s=60)
        updated = self.mgr.update_metadata(issued.info.id, actor=self.admin,
                                           expires_at=self.clock.value + 600)
        self.assertEqual(updated.expires_at, self.clock.value + 600)
        self.assertEqual(updated.state, 'active')

    def test_an_expiry_in_the_past_is_refused(self):
        issued = self.mgr.create(actor=self.admin, name='short')
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.update_metadata(issued.info.id, actor=self.admin,
                                     expires_at=self.clock.value - 1)
        self.assertEqual(caught.exception.code, apikeys.E_FIELD)

    def test_a_name_is_required_and_bounded(self):
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.create(actor=self.admin, name='  ')
        self.assertEqual(caught.exception.code, apikeys.E_FIELD)
        with self.assertRaises(apikeys.ApiKeyError):
            self.mgr.create(actor=self.admin, name='x' * (apikeys.MAX_NAME_LENGTH + 1))

    def test_ttl_and_expires_at_are_mutually_exclusive(self):
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.create(actor=self.admin, name='x', ttl_s=60,
                            expires_at=self.clock.value + 60)
        self.assertEqual(caught.exception.code, apikeys.E_FIELD)
        with self.assertRaises(apikeys.ApiKeyError):
            self.mgr.create(actor=self.admin, name='x', ttl_s=0)
        with self.assertRaises(apikeys.ApiKeyError):
            self.mgr.create(actor=self.admin, name='x', ttl_s=-5)


class DisableRevokeDeleteTests(LifecycleTestCase):
    def test_disable_stops_the_key_and_enable_brings_it_back(self):
        issued = self.mgr.create(actor=self.admin, name='paused', permissions=['read.status'])
        self.assertEqual(self.mgr.disable(issued.info.id, actor=self.admin).state, 'disabled')
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(issued.secret)
        self.assertEqual(caught.exception.code, apikeys.E_DISABLED)
        self.assertEqual(caught.exception.http_status, 401)
        self.mgr.enable(issued.info.id, actor=self.admin)
        self.assertEqual(self.mgr.authenticate(issued.secret).key_id, issued.info.id)

    def test_revoke_is_terminal_and_a_revoked_key_keeps_its_metadata(self):
        issued = self.mgr.create(actor=self.admin, name='doomed', purpose='keep me')
        self.mgr.revoke(issued.info.id, actor=self.admin)
        entry = self.mgr.get_key(issued.info.id)
        self.assertEqual(entry.state, 'revoked')
        self.assertIsNotNone(entry.revoked_at)
        self.assertEqual(entry.purpose, 'keep me')
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(issued.secret)
        self.assertEqual(caught.exception.code, apikeys.E_REVOKED)
        for action in (self.mgr.enable, self.mgr.disable, self.mgr.rotate):
            with self.subTest(action=action.__name__):
                with self.assertRaises(apikeys.ApiKeyError) as caught:
                    action(issued.info.id, actor=self.admin)
                self.assertEqual(caught.exception.code, apikeys.E_REVOKED)

    def test_revoke_twice_keeps_the_first_moment(self):
        issued = self.mgr.create(actor=self.admin, name='doomed')
        first = self.mgr.revoke(issued.info.id, actor=self.admin)
        self.clock.advance(500)
        second = self.mgr.revoke(issued.info.id, actor=self.admin)
        self.assertEqual(first.revoked_at, second.revoked_at)

    def test_delete_removes_the_row_and_keeps_the_audit_trail(self):
        issued = self.mgr.create(actor=self.admin, name='temporary')
        self.mgr.delete(issued.info.id, actor=self.admin)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.get_key(issued.info.id)
        self.assertEqual(caught.exception.code, apikeys.E_FIELD)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(issued.secret)
        self.assertEqual(caught.exception.code, apikeys.E_INVALID)
        operations = [entry['operation'] for entry in self.mgr.read_audit(actor=self.admin,
                                                                         key_id=self.admin.key_id)]
        self.assertIn('key.delete', operations)
        entries = self.mgr.read_audit(actor=self.admin, limit=500)
        self.assertTrue(any(entry['object_id'] == issued.info.id for entry in entries))

    def test_an_unknown_key_id_is_named_in_the_error(self):
        for action in (lambda: self.mgr.get_key('nope'),
                       lambda: self.mgr.delete('nope', actor=self.admin),
                       lambda: self.mgr.revoke('nope', actor=self.admin),
                       lambda: self.mgr.disable('nope', actor=self.admin),
                       lambda: self.mgr.enable('nope', actor=self.admin),
                       lambda: self.mgr.rotate('nope', actor=self.admin),
                       lambda: self.mgr.update_metadata('nope', actor=self.admin, name='x')):
            with self.subTest(action=action):
                with self.assertRaises(apikeys.ApiKeyError) as caught:
                    action()
                self.assertEqual(caught.exception.code, apikeys.E_FIELD)


class ExpiryTests(LifecycleTestCase):
    def test_a_key_stops_working_the_moment_it_expires(self):
        issued = self.mgr.create(actor=self.admin, name='short', ttl_s=60,
                                 permissions=['read.status'])
        self.mgr.authenticate(issued.secret)
        self.clock.advance(59)
        self.assertEqual(self.mgr.authenticate(issued.secret).state, 'active')
        self.clock.advance(2)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(issued.secret)
        self.assertEqual(caught.exception.code, apikeys.E_EXPIRED)
        self.assertEqual(self.mgr.get_key(issued.info.id).state, 'expired')

    def test_expiry_is_reported_as_expired_and_not_as_invalid(self):
        issued = self.mgr.create(actor=self.admin, name='short', ttl_s=10)
        self.clock.advance(11)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(issued.secret)
        self.assertEqual(caught.exception.code, apikeys.E_EXPIRED)
        self.assertEqual(caught.exception.state, 'expired')

    def test_a_key_without_a_deadline_keeps_working(self):
        issued = self.mgr.create(actor=self.admin, name='permanent')
        self.clock.advance(365 * 24 * 3600)
        self.assertEqual(self.mgr.authenticate(issued.secret).state, 'active')
        self.assertIsNone(self.mgr.get_key(issued.info.id).expires_at)

    def test_a_disabled_key_that_also_expired_reports_the_disable_first(self):
        issued = self.mgr.create(actor=self.admin, name='both', ttl_s=10)
        self.clock.advance(11)
        self.mgr.disable(issued.info.id, actor=self.admin)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(issued.secret)
        self.assertEqual(caught.exception.code, apikeys.E_DISABLED)


class RotationTests(LifecycleTestCase):
    def test_rotation_replaces_the_secret_and_keeps_the_metadata(self):
        issued = self.mgr.create(actor=self.admin, name='rotating', purpose='same key',
                                 permissions=['read.status'], ttl_s=3600)
        rotated = self.mgr.rotate(issued.info.id, actor=self.admin)
        self.assertEqual(rotated.info.id, issued.info.id)
        self.assertEqual(rotated.info.purpose, 'same key')
        self.assertEqual(rotated.info.permissions, ('read.status',))
        self.assertEqual(rotated.info.expires_at, issued.info.expires_at)
        self.assertNotEqual(rotated.secret, issued.secret)
        # The public handle belongs to the key, so a settings screen keeps the same prefix.
        self.assertEqual(rotated.info.prefix, issued.info.prefix)
        self.assertEqual(apikeys.secret_prefix(rotated.secret), issued.info.prefix)
        self.assertEqual(self.mgr.authenticate(rotated.secret).key_id, issued.info.id)
        with self.assertRaises(apikeys.ApiKeyError):
            self.mgr.authenticate(issued.secret)

    def test_without_a_grace_window_the_old_secret_dies_at_once(self):
        issued = self.mgr.create(actor=self.admin, name='rotating')
        self.mgr.rotate(issued.info.id, actor=self.admin)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(issued.secret)
        self.assertEqual(caught.exception.code, apikeys.E_INVALID)

    def test_a_narrow_grace_window_lets_the_old_secret_through_and_then_closes(self):
        issued = self.mgr.create(actor=self.admin, name='rotating')
        rotated = self.mgr.rotate(issued.info.id, actor=self.admin, grace_s=120)
        self.assertEqual(rotated.info.rotation_grace_until, self.clock.value + 120)

        inside = self.mgr.authenticate(issued.secret, allow_grace=True)
        self.assertTrue(inside.in_grace)
        self.assertEqual(inside.code, apikeys.E_ROTATION_GRACE)
        self.assertEqual(self.mgr.authenticate(rotated.secret).key_id, issued.info.id)

        self.clock.advance(121)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(issued.secret, allow_grace=True)
        self.assertEqual(caught.exception.code, apikeys.E_INVALID)
        self.assertEqual(self.mgr.authenticate(rotated.secret).key_id, issued.info.id)

    def test_the_grace_window_is_a_per_key_setting_and_never_a_default(self):
        issued = self.mgr.create(actor=self.admin, name='rotating')
        rotated = self.mgr.rotate(issued.info.id, actor=self.admin, grace_s=60)
        self.assertEqual(rotated.info.rotation_grace_until, self.clock.value + 60)
        other = self.mgr.create(actor=self.admin, name='other')
        self.assertIsNone(self.mgr.get_key(other.info.id).rotation_grace_until)

    def test_a_management_call_refuses_the_superseded_secret_even_inside_the_window(self):
        issued = self.mgr.create(actor=self.admin, name='rotating', permissions=['read.status'])
        self.mgr.rotate(issued.info.id, actor=self.admin, grace_s=300)
        # A client that was told to move over is served inside the window.
        self.assertTrue(self.mgr.authenticate(issued.secret, permission='read.status',
                                              allow_grace=True).in_grace)
        # Everybody else is told to move, instead of being served silently.
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(issued.secret, permission='read.status')
        self.assertEqual(caught.exception.code, apikeys.E_ROTATION_GRACE)

    def test_the_window_can_be_purged_and_leaves_no_second_verifier(self):
        issued = self.mgr.create(actor=self.admin, name='rotating')
        self.mgr.rotate(issued.info.id, actor=self.admin, grace_s=60)
        self.clock.advance(61)
        self.assertEqual(self.mgr.purge_expired_grace(), 1)
        row = self.conn.execute('SELECT * FROM api_keys WHERE id = ?',
                                (issued.info.id,)).fetchone()
        self.assertIsNone(row['previous_verifier'])
        self.assertIsNone(row['rotation_grace_until'])
        with self.assertRaises(apikeys.ApiKeyError):
            self.mgr.authenticate(issued.secret, allow_grace=True)

    def test_rotation_refuses_a_negative_window(self):
        issued = self.mgr.create(actor=self.admin, name='rotating')
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.rotate(issued.info.id, actor=self.admin, grace_s=-1)
        self.assertEqual(caught.exception.code, apikeys.E_FIELD)
        self.assertEqual(self.mgr.authenticate(issued.secret).key_id, issued.info.id)

    def test_a_revoked_key_keeps_nothing_usable_after_revocation(self):
        issued = self.mgr.create(actor=self.admin, name='rotating')
        rotated = self.mgr.rotate(issued.info.id, actor=self.admin, grace_s=300)
        self.mgr.revoke(issued.info.id, actor=self.admin)
        row = self.conn.execute('SELECT * FROM api_keys WHERE id = ?',
                                (issued.info.id,)).fetchone()
        self.assertIsNone(row['previous_verifier'])
        self.assertIsNone(row['rotation_grace_until'])
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(rotated.secret, allow_grace=True)
        self.assertEqual(caught.exception.code, apikeys.E_REVOKED)
        # The superseded verifier is destroyed by the revoke, so the old secret is
        # not merely refused but gone.
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(issued.secret, allow_grace=True)
        self.assertEqual(caught.exception.code, apikeys.E_INVALID)


if __name__ == '__main__':
    unittest.main()

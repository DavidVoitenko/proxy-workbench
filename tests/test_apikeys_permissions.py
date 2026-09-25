"""Rights and resource scope: per-operation checks that filters cannot widen."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apikeys_support as support  # noqa: E402

from proxy_workbench import apikeys  # noqa: E402


class PermissionCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = support.connect(self.tmp.name)
        self.addCleanup(self.conn.close)
        self.mgr = support.manager(self.conn, support.Clock())
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        self.admin = self.mgr.authenticate(issued.secret)

    def test_the_catalogue_is_the_contract_set(self):
        self.assertEqual(apikeys.PERMISSIONS, frozenset({
            'read.status', 'read.results', 'read.results.detail', 'read.export.artifact',
            'collections.read', 'collections.write', 'import.read', 'import.commit',
            'profiles.read', 'profiles.write', 'sources.read', 'sources.write',
            'jobs.read', 'jobs.submit', 'jobs.control',
            'pools.read', 'pools.write', 'gateway.read', 'gateway.write',
            'schedules.read', 'schedules.write',
            'export.create', 'export.secret',
            'admin.settings', 'admin.keys', 'admin.audit',
        }))
        self.assertTrue(apikeys.SENSITIVE_PERMISSIONS <= apikeys.PERMISSIONS)
        self.assertTrue(apikeys.ADMIN_PERMISSIONS <= apikeys.PERMISSIONS)

    def test_result_reading_and_status_reading_are_separate_rights(self):
        self.assertIn('read.results', apikeys.READ_PERMISSIONS)
        self.assertIn('read.status', apikeys.READ_PERMISSIONS)
        self.assertNotEqual(apikeys.READ_PERMISSIONS, apikeys.WRITE_PERMISSIONS)

    def test_every_documented_domain_has_its_own_right(self):
        for right in ('collections.write', 'import.commit', 'profiles.write', 'sources.write',
                      'jobs.submit', 'jobs.control', 'pools.write', 'gateway.write',
                      'schedules.write', 'export.create', 'admin.keys', 'admin.settings'):
            self.assertIn(right, apikeys.PERMISSIONS)

    def test_an_unknown_permission_is_refused_by_name(self):
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.create(actor=self.admin, name='x', permissions=['read.everything'])
        self.assertEqual(caught.exception.code, apikeys.E_UNKNOWN_FIELD)


class AuthorizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = support.connect(self.tmp.name)
        self.addCleanup(self.conn.close)
        self.clock = support.Clock()
        self.mgr = support.manager(self.conn, self.clock)
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        self.admin = self.mgr.authenticate(issued.secret)

    def test_a_key_without_the_right_is_refused(self):
        issued = self.mgr.create(actor=self.admin, name='reader',
                                 permissions=['read.results'])
        reader = self.mgr.authenticate(issued.secret)
        self.assertTrue(reader.require('read.results').allowed)
        decision = reader.require('jobs.submit')
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, apikeys.E_PERMISSION)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            reader.require('jobs.submit').raise_if_denied()
        self.assertEqual(caught.exception.code, apikeys.E_PERMISSION)
        self.assertEqual(caught.exception.http_status, 403)

    def test_authentication_can_check_the_right_in_one_call(self):
        issued = self.mgr.create(actor=self.admin, name='reader', permissions=['read.status'])
        reader = self.mgr.authenticate(issued.secret, permission='read.status')
        self.assertEqual(reader.key_id, issued.info.id)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(issued.secret, permission='jobs.control')
        self.assertEqual(caught.exception.code, apikeys.E_PERMISSION)

    def test_a_secret_bearing_export_needs_its_own_right_and_its_own_argument(self):
        issued = self.mgr.create(actor=self.admin, name='exporter',
                                 permissions=['export.create'])
        exporter = self.mgr.authenticate(issued.secret)
        self.assertTrue(exporter.require('export.create').allowed)
        self.assertTrue(exporter.require('export.create', include_secrets=True).allowed is False)
        denied = exporter.require('export.create', include_secrets=True)
        self.assertEqual(denied.code, apikeys.E_PERMISSION)
        full = self.mgr.create(actor=self.admin, name='exporter with credentials',
                               permissions=['export.create', 'export.secret'])
        exporter_full = self.mgr.authenticate(full.secret)
        self.assertTrue(exporter_full.require('export.create', include_secrets=True).allowed)
        # The default path stays redacted: only the explicit parameter reveals credentials.
        self.assertFalse(exporter_full.require('export.create').include_secrets)

    def test_an_unknown_permission_name_never_passes_silently(self):
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.admin.require('not.a.right')
        self.assertEqual(caught.exception.code, apikeys.E_UNKNOWN_FIELD)

    def test_default_grant_is_nothing(self):
        issued = self.mgr.create(actor=self.admin, name='empty')
        empty = self.mgr.authenticate(issued.secret)
        self.assertEqual(empty.info.permissions, ())
        for right in sorted(apikeys.PERMISSIONS):
            self.assertFalse(empty.has(right), right)


class ResourceScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = support.connect(self.tmp.name)
        self.addCleanup(self.conn.close)
        self.clock = support.Clock()
        self.mgr = support.manager(self.conn, self.clock)
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        self.admin = self.mgr.authenticate(issued.secret)
        self.own = self.mgr.create(actor=self.admin, name='own collection',
                                   permissions=['read.results', 'jobs.submit', 'export.create',
                                              'pools.read'],
                                   scope={'collections': ['mine'], 'pools': ['pool-mine']})
        self.foreign = self.mgr.create(actor=self.admin, name='other collection',
                                       permissions=['read.results', 'jobs.submit', 'export.create'],
                                       scope={'collections': ['theirs']})
        self.own_principal = self.mgr.authenticate(self.own.secret)
        self.foreign_principal = self.mgr.authenticate(self.foreign.secret)

    def test_a_key_is_confined_to_its_own_collections_and_pools(self):
        self.assertTrue(self.own_principal.require('read.results', collection_id='mine').allowed)
        denied = self.own_principal.require('read.results', collection_id='theirs')
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.code, apikeys.E_SCOPE)
        self.assertEqual(denied.http_status, 403)
        self.assertTrue(self.own_principal.require('pools.read', pool_id='pool-mine').allowed)
        self.assertFalse(self.own_principal.require('pools.read', pool_id='pool-theirs').allowed)

    def test_a_filter_cannot_widen_the_scope_of_a_key(self):
        self.assertEqual(apikeys.effective_collections(self.own_principal, None), ['mine'])
        self.assertEqual(apikeys.effective_collections(self.own_principal, ['mine']), ['mine'])
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            apikeys.effective_collections(self.own_principal, ['mine', 'theirs'])
        self.assertEqual(caught.exception.code, apikeys.E_SCOPE)

    def test_a_key_never_reads_the_scope_of_another_key(self):
        first = apikeys.object_visible(self.own_principal, 'collection', 'mine')
        second = apikeys.object_visible(self.own_principal, 'collection', 'theirs')
        self.assertTrue(first)
        self.assertFalse(second)

    def test_a_forbidden_object_and_a_missing_object_answer_the_same_way(self):
        foreign = apikeys.object_visible(self.own_principal, 'collection', 'theirs')
        missing = apikeys.object_visible(self.own_principal, 'collection', 'no-such-collection')
        self.assertEqual(foreign, missing)
        denied_foreign = self.own_principal.require('read.results', collection_id='theirs')
        denied_missing = self.own_principal.require('read.results', collection_id='no-such-one')
        self.assertEqual(denied_foreign.code, denied_missing.code)

    def test_an_empty_scope_means_everything_the_rights_allow(self):
        unscoped = self.mgr.create(actor=self.admin, name='unscoped', permissions=['read.results'])
        principal = self.mgr.authenticate(unscoped.secret)
        self.assertIsNone(apikeys.visible_collections(principal))
        self.assertIsNone(apikeys.visible_pools(principal))
        self.assertTrue(principal.require('read.results', collection_id='any').allowed)

    def test_a_scope_is_validated_at_creation(self):
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.create(actor=self.admin, name='bad', scope={'collections': 'mine'})
        self.assertEqual(caught.exception.code, apikeys.E_FIELD)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.create(actor=self.admin, name='bad', scope={'databases': ['mine']})
        self.assertEqual(caught.exception.code, apikeys.E_UNKNOWN_FIELD)

    def test_a_batch_or_job_status_under_a_foreign_id_is_refused(self):
        # Every operation in the API resolves its object the same way, so one helper
        # answers for a batch, a job status, a download and an export artifact.
        self.assertTrue(apikeys.object_visible(self.own_principal, 'job', 'job-mine',
                                              collection_id='mine'))
        for kind in ('job', 'artifact', 'result', 'batch', 'stream'):
            with self.subTest(kind=kind):
                self.assertFalse(apikeys.object_visible(self.own_principal, kind, 'theirs',
                                                        collection_id='theirs'))
                # An object that does not name its owner stays invisible to a scoped key.
                self.assertFalse(apikeys.object_visible(self.own_principal, kind, 'job-theirs'))

    def test_counts_cannot_leak_a_foreign_collection(self):
        mine = apikeys.effective_collections(self.own_principal, None)
        self.assertEqual(mine, ['mine'])
        self.assertNotIn('theirs', mine)
        listed = self.mgr.list_keys(actor=self.admin)
        self.assertEqual({info.id for info in listed}, {self.admin.key_id, self.own.info.id,
                                                        self.foreign.info.id})


class ManagementPermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = support.connect(self.tmp.name)
        self.addCleanup(self.conn.close)
        self.clock = support.Clock()
        self.mgr = support.manager(self.conn, self.clock)
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        self.admin = self.mgr.authenticate(issued.secret)

    def test_a_read_only_key_cannot_manage_keys(self):
        reader = self.mgr.create(actor=self.admin, name='reader',
                                 permissions=['read.results', 'read.status'])
        reader_principal = self.mgr.authenticate(reader.secret)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.create(actor=reader_principal, name='self promoted',
                            permissions=['admin.keys'])
        self.assertEqual(caught.exception.code, apikeys.E_PERMISSION)

    def test_a_read_only_key_cannot_revoke_or_delete_a_key(self):
        reader = self.mgr.create(actor=self.admin, name='reader', permissions=['read.results'])
        reader_principal = self.mgr.authenticate(reader.secret)
        for action in (self.mgr.revoke, self.mgr.delete, self.mgr.disable, self.mgr.rotate):
            with self.subTest(action=action.__name__):
                with self.assertRaises(apikeys.ApiKeyError) as caught:
                    action(self.admin.key_id, actor=reader_principal)
                self.assertEqual(caught.exception.code, apikeys.E_PERMISSION)
        self.assertEqual(self.mgr.get_key(self.admin.key_id).state, 'active')

    def test_a_read_only_key_cannot_read_the_audit_log(self):
        reader = self.mgr.create(actor=self.admin, name='reader',
                                 permissions=['read.results', 'admin.audit'][:1])
        reader_principal = self.mgr.authenticate(reader.secret)
        with self.assertRaises(apikeys.ApiKeyError):
            self.mgr.read_audit(actor=reader_principal)

    def test_bootstrap_refuses_a_caller_that_is_not_a_local_trusted_surface(self):
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.bootstrap_admin(local_trusted=False, name='remote')
        self.assertEqual(caught.exception.code, apikeys.E_PERMISSION)
        self.assertEqual(caught.exception.state, 'not_local')
        with self.assertRaises(TypeError):
            self.mgr.bootstrap_admin(name='no flag at all')
        self.assertEqual([info.id for info in self.mgr.list_keys(actor=self.admin)],
                         [self.admin.key_id])

    def test_the_bootstrap_key_carries_the_admin_rights_and_nothing_is_left_implicit(self):
        self.assertIn('admin.keys', self.admin.info.permissions)
        self.assertIn('admin.audit', self.admin.info.permissions)
        self.assertIn('admin.settings', self.admin.info.permissions)
        self.assertIn('read.results', self.admin.info.permissions)
        self.assertNotIn('export.secret', self.admin.info.permissions)

    def test_manage_operations_are_audited(self):
        reader = self.mgr.create(actor=self.admin, name='reader', permissions=['read.results'])
        with self.assertRaises(apikeys.ApiKeyError):
            self.mgr.revoke(reader.info.id, actor=self.mgr.authenticate(reader.secret))
        self.assertNotIn('key.revoke', [entry['operation'] for entry
                                        in self.mgr.read_audit(actor=self.admin)])


if __name__ == '__main__':
    unittest.main()

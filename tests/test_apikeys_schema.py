"""The manager against a database that ``db.migrate()`` really created.

``db.py`` is another writer's file.  These tests do not change it: they only
check that this module works on the schema it actually produces, and that a
column the module needs is named in the error instead of surfacing as a raw
SQLite failure.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apikeys_support as support  # noqa: E402

from proxy_workbench import apikeys  # noqa: E402

try:
    from proxy_workbench import db
except ImportError:  # pragma: no cover - db.py is a parallel deliverable
    db = None


@unittest.skipIf(db is None, 'db.py is not in the tree yet')
class MigratedDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'workbench.db'
        self.clock = support.Clock()
        db.migrate(self.path, now=self.clock.value)
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        self.mgr = apikeys.ApiKeyManager(conn, now=self.clock)

    def test_the_migrated_table_has_the_contract_columns(self):
        columns = [row[1] for row in self.mgr.conn.execute('PRAGMA table_info(api_keys)')]
        for name in apikeys.CONTRACT_API_KEYS_COLUMNS:
            self.assertIn(name, columns, name)

    def test_a_key_can_be_issued_and_used_on_a_migrated_database(self):
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        principal = self.mgr.authenticate(issued.secret)
        self.assertEqual(principal.key_id, issued.info.id)
        self.assertEqual([info.id for info in self.mgr.list_keys(actor=principal)],
                         [issued.info.id])

    def test_a_missing_column_is_named_instead_of_raising_a_sqlite_error(self):
        if 'disabled_at' in self.mgr._columns:
            self.skipTest('migration 8 already carries disabled_at')
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        principal = self.mgr.authenticate(issued.secret)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.disable(issued.info.id, actor=principal)
        self.assertEqual(caught.exception.code, apikeys.E_INVALID)
        self.assertIn('disabled_at', caught.exception.detail)
        self.assertIn('ensure_schema', caught.exception.action)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.rotate(issued.info.id, actor=principal, grace_s=60)
        self.assertIn('previous_verifier', caught.exception.detail)
        with self.assertRaises(apikeys.ApiKeyError):
            self.mgr.purge_expired_grace()

    def test_the_operations_of_the_contract_columns_work_on_a_migrated_database(self):
        admin = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        principal = self.mgr.authenticate(admin.secret)
        created = self.mgr.create(actor=principal, name='reader', permissions=['read.results'],
                                  ttl_s=600, rate_limit={'requests': 5, 'window_s': 60},
                                  concurrency={'max_active': 1}, scope={'collections': ['mine']})
        self.mgr.update_metadata(created.info.id, actor=principal, name='renamed')
        self.assertEqual(self.mgr.get_key(created.info.id).name, 'renamed')
        self.mgr.revoke(created.info.id, actor=principal)
        self.assertEqual(self.mgr.get_key(created.info.id).state, 'revoked')
        self.mgr.delete(created.info.id, actor=principal)
        self.assertTrue(self.mgr.read_audit(actor=principal, limit=20))

    def test_ensure_schema_completes_the_migrated_table_without_rewriting_it(self):
        before = [tuple(row) for row in self.mgr.conn.execute('PRAGMA table_info(api_keys)')]
        apikeys.ensure_schema(self.mgr.conn)
        after = [tuple(row) for row in self.mgr.conn.execute('PRAGMA table_info(api_keys)')]
        self.assertEqual(before, after[:len(before)])
        for name in ('disabled_at', 'previous_verifier', 'previous_verifier_salt',
                     'previous_verifier_algo'):
            self.assertIn(name, [row[1] for row in after], name)
        apikeys.ensure_schema(self.mgr.conn)
        self.assertEqual([tuple(row) for row in self.mgr.conn.execute('PRAGMA table_info(api_keys)')],
                         after)


if __name__ == '__main__':
    unittest.main()

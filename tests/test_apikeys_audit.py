"""The local audit log: what it records, and what it must never receive."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apikeys_support as support  # noqa: E402

from proxy_workbench import apikeys  # noqa: E402


class AuditTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'workbench.db'
        self.conn = support.connect(self.tmp.name)
        self.addCleanup(self.conn.close)
        self.clock = support.Clock()
        self.mgr = support.manager(self.conn, self.clock)
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        self.admin = self.mgr.authenticate(issued.secret)

    def raw_rows(self):
        return [dict(zip(apikeys.AUDIT_LOG_COLUMNS, row))
                for row in self.conn.execute('SELECT at, key_id, operation, object_kind,'
                                             ' object_id, scope_json, result, error_code'
                                             ' FROM audit_log ORDER BY at, rowid')]


class AuditContentTests(AuditTestCase):
    def test_every_managed_operation_leaves_key_object_result_and_time(self):
        issued = self.mgr.create(actor=self.admin, name='audited', purpose='first',
                                 scope={'collections': ['mine']})
        self.mgr.update_metadata(issued.info.id, actor=self.admin, name='renamed')
        self.mgr.disable(issued.info.id, actor=self.admin)
        self.mgr.enable(issued.info.id, actor=self.admin)
        self.mgr.rotate(issued.info.id, actor=self.admin, grace_s=30)
        self.mgr.revoke(issued.info.id, actor=self.admin)
        operations = [entry['operation'] for entry in self.mgr.read_audit(actor=self.admin,
                                                                         limit=50)]
        for expected in ('key.bootstrap', 'key.create', 'key.update_metadata', 'key.disable',
                         'key.enable', 'key.rotate', 'key.revoke'):
            self.assertIn(expected, operations)
        created = [entry for entry in self.mgr.read_audit(actor=self.admin, limit=50)
                   if entry['operation'] == 'key.create'][0]
        self.assertEqual(created['key_id'], self.admin.key_id)
        self.assertEqual(created['object_kind'], 'api_key')
        self.assertEqual(created['object_id'], issued.info.id)
        self.assertEqual(created['result'], 'ok')
        self.assertEqual(created['at'], self.clock.value)
        self.assertEqual(created['scope'], {'collections': ['mine']})

    def test_a_refusal_is_recorded_with_its_code(self):
        self.mgr.revoke(self.admin.key_id, actor=self.admin)
        with self.assertRaises(apikeys.ApiKeyError):
            self.mgr.authenticate(self.mgr.list_keys(actor=self._fresh_admin())[0].id
                                  and 'pwk_00000000_' + 'A' * 43)
        entries = [entry for entry in self.mgr.read_audit(actor=self._fresh_admin(), limit=50)
                   if entry['result'] == 'denied']
        self.assertTrue(entries)
        self.assertIn(entries[-1]['error_code'], (apikeys.E_INVALID, apikeys.E_REVOKED))

    def test_the_reader_filters_by_key_operation_and_time(self):
        issued = self.mgr.create(actor=self.admin, name='one')
        self.mgr.create(actor=self.admin, name='two')
        self.clock.advance(60)
        self.mgr.rotate(issued.info.id, actor=self.admin)
        everything = self.mgr.read_audit(actor=self.admin, limit=50)
        self.assertTrue([entry for entry in everything if entry['operation'] == 'key.rotate'])
        self.assertTrue(self.mgr.read_audit(actor=self.admin, operation='key.rotate'))
        self.assertFalse(self.mgr.read_audit(actor=self.admin, operation='key.rotate',
                                             since=self.clock.value + 1))
        self.assertTrue(self.mgr.read_audit(actor=self.admin, key_id=self.admin.key_id,
                                            limit=50))
        self.assertFalse(self.mgr.read_audit(actor=self.admin, key_id=issued.info.id))

    def test_the_reader_is_limited_and_returns_the_newest_first(self):
        for index in range(10):
            self.mgr.create(actor=self.admin, name=f'key {index}')
        entries = self.mgr.read_audit(actor=self.admin, limit=3)
        self.assertEqual(len(entries), 3)
        self.assertEqual([entry['at'] for entry in entries], sorted(
            (entry['at'] for entry in entries), reverse=True))

    def test_the_reader_needs_the_audit_right(self):
        reader = self.mgr.create(actor=self.admin, name='reader',
                                 permissions=['read.results', 'admin.keys'])
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.read_audit(actor=self.mgr.authenticate(reader.secret))
        self.assertEqual(caught.exception.code, apikeys.E_PERMISSION)

    def test_the_log_is_bounded(self):
        bounded = support.manager(support.connect(self.tmp.name, 'bounded.db'),
                                  self.clock, audit_retention=10)
        admin = bounded.bootstrap_admin(local_trusted=True, name='admin')
        principal = bounded.authenticate(admin.secret)
        for index in range(40):
            bounded.create(actor=principal, name=f'key {index}')
        total = bounded.conn.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0]
        self.assertEqual(total, 10)

    def _fresh_admin(self):
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='second admin')
        return self.mgr.authenticate(issued.secret)


class RedactionTests(AuditTestCase):
    def test_a_secret_never_reaches_the_audit_log(self):
        issued = self.mgr.create(actor=self.admin, name='phone', purpose='mobile',
                                 permissions=['read.status'])
        self.mgr.authenticate(issued.secret)
        blob = json.dumps(self.raw_rows())
        self.assertNotIn(issued.secret, blob)
        # The log names keys by id, so it stays readable without holding any secret.
        self.assertIn(issued.info.id, blob)

    def test_a_secret_shaped_value_is_masked_even_when_it_arrives_as_a_scope(self):
        canary = f'{apikeys.SECRET_MARK}canarycanary0123456789abcdefghijk'
        issued = self.mgr.create(actor=self.admin, name='scoped',
                                 scope={'collections': ['mine']})
        self.mgr._audit(issued.info.id, 'test.op', scope={'collections': [canary]},
                        result='ok')
        entry = [row for row in self.raw_rows() if row['operation'] == 'test.op'][0]
        self.assertNotIn(canary, entry['scope_json'])
        self.assertIn('***', entry['scope_json'])
        self.assertIn('pwk_canarycanary', entry['scope_json'])

    def test_a_bearer_token_is_masked(self):
        secret = f'{apikeys.SECRET_MARK}abcdefgh_' + 'Z' * 43
        masked = apikeys.redact(f'Authorization: Bearer {secret}')
        self.assertNotIn(secret, masked)
        self.assertIn(apikeys.REDACTED, masked)

    def test_redaction_keeps_the_handle_so_the_log_stays_readable(self):
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        masked = apikeys.redact(issued.secret)
        self.assertIn(issued.info.prefix, masked)
        self.assertTrue(masked.endswith(apikeys.REDACTED))
        self.assertNotIn(issued.secret, masked)

    def test_redaction_walks_lists_and_mappings(self):
        secret = f'{apikeys.SECRET_MARK}abcdefgh_' + 'Z' * 43
        masked = apikeys._redact_all({'a': [secret], 'b': {'c': secret}, 'd': 7, 'e': None})
        self.assertNotIn(secret, json.dumps(masked))
        self.assertEqual(masked['d'], 7)
        self.assertIsNone(masked['e'])

    def test_the_audit_api_takes_no_free_text_bodies(self):
        import inspect
        signature = inspect.signature(self.mgr._audit)
        for name in ('detail', 'body', 'message', 'response', 'payload', 'text'):
            self.assertNotIn(name, signature.parameters)
        columns = set(apikeys.AUDIT_LOG_COLUMNS)
        self.assertEqual(columns, {'at', 'key_id', 'operation', 'object_kind', 'object_id',
                                   'scope_json', 'result', 'error_code'})

    def test_the_database_file_of_a_full_session_holds_no_secret(self):
        issued = self.mgr.create(actor=self.admin, name='key', permissions=['read.status'])
        self.mgr.authenticate(issued.secret)
        self.mgr.rotate(issued.info.id, actor=self.admin, grace_s=30)
        self.mgr.authenticate(issued.secret, allow_grace=True)
        self.conn.commit()
        self.conn.close()
        blob = self.path.read_bytes()
        self.assertNotIn(issued.secret.encode(), blob)
        # The handle is expected to be in the file: it is what the settings screen shows.
        self.assertIn(issued.info.prefix.encode(), blob)


if __name__ == '__main__':
    unittest.main()

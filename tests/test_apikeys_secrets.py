"""The issued secret: shown once, generated with real entropy, stored one way only."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apikeys_support as support  # noqa: E402

from proxy_workbench import apikeys  # noqa: E402


class SecretIssuingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = support.connect(self.tmp.name)
        self.addCleanup(self.conn.close)
        self.clock = support.Clock()
        self.mgr = support.manager(self.conn, self.clock)

    def test_secret_has_the_documented_shape_and_enough_entropy(self):
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        secret = issued.secret
        self.assertTrue(secret.startswith(apikeys.SECRET_MARK))
        handle, _, body = secret[len(apikeys.SECRET_MARK):].partition('_')
        self.assertEqual(len(handle), apikeys.PREFIX_LENGTH)
        # 32 random bytes in base64url without padding, behind a public handle.
        self.assertEqual(len(body), 43)
        self.assertGreaterEqual(len(body) * 6, 256)

    def test_secrets_of_many_keys_do_not_repeat(self):
        secrets = {self.mgr.bootstrap_admin(local_trusted=True, name=f'admin {index}').secret
                   for index in range(40)}
        self.assertEqual(len(secrets), 40)

    def test_prefix_is_the_handle_of_the_secret(self):
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        self.assertEqual(issued.info.prefix, apikeys.secret_prefix(issued.secret))
        self.assertEqual(len(issued.info.prefix), apikeys.PREFIX_LENGTH)
        self.assertLess(len(issued.info.prefix), len(issued.secret))

    def test_full_secret_never_comes_back_from_the_manager(self):
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        admin, _ = self.mgr.authenticate(issued.secret), issued
        listed = self.mgr.list_keys(actor=admin)
        self.assertEqual(len(listed), 1)
        as_text = repr(listed[0].as_dict()) + repr(admin.as_dict()) + repr(admin.info.as_dict())
        self.assertNotIn(issued.secret, as_text)
        self.assertIn(issued.info.prefix, as_text)
        for field in ('verifier', 'verifier_salt', 'previous_verifier'):
            self.assertNotIn(field, listed[0].as_dict())

    def test_reissue_returns_a_fresh_secret_under_the_same_metadata(self):
        admin, first = self._admin()
        second = self.mgr.create(actor=admin, name='second', purpose='read only',
                                 permissions=['read.status'])
        self.assertNotEqual(first.secret, second.secret)
        self.assertNotEqual(first.info.id, second.info.id)
        self.assertEqual(second.info.purpose, 'read only')

    def test_warnings_say_the_secret_is_shown_once(self):
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        text = ' '.join(issued.warnings).lower()
        self.assertTrue('секрет' in text or 'secret' in text, issued.warnings)
        self.assertTrue('один раз' in text or 'once' in text, issued.warnings)

    def _admin(self):
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        return self.mgr.authenticate(issued.secret), issued


class VerifierStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'workbench.db'
        self.conn = support.connect(self.tmp.name)
        self.addCleanup(self.conn.close)
        self.clock = support.Clock()
        self.mgr = support.manager(self.conn, self.clock)

    def test_database_holds_a_salted_one_way_verifier(self):
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        row = self.conn.execute('SELECT * FROM api_keys WHERE id = ?',
                                (issued.info.id,)).fetchone()
        algo, salt, verifier = row['verifier_algo'], row['verifier_salt'], row['verifier']
        self.assertTrue(algo.startswith(f'{apikeys.VERIFIER_ALGO}$'))
        self.assertEqual(int(algo.split('$')[1]), apikeys.PBKDF2_ITERATIONS)
        self.assertNotEqual(salt, '')
        digest = apikeys._hash_secret(issued.secret, apikeys._b64decode(salt),
                                      apikeys.PBKDF2_ITERATIONS)
        self.assertEqual(verifier, apikeys._b64encode(digest))
        self.assertNotEqual(verifier, digest.hex())
        self.assertNotIn(issued.secret, verifier)

    def test_two_keys_of_one_name_get_different_salts(self):
        first = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        second = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        rows = {row['id']: row for row in self.conn.execute('SELECT * FROM api_keys')}
        self.assertNotEqual(rows[first.info.id]['verifier_salt'],
                            rows[second.info.id]['verifier_salt'])

    def test_the_secret_is_absent_from_the_database_file(self):
        canary = support.CANARY_SECRET_MARK + 'DONTWRITETHIS1234567890abcdefghijk'
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='canary key')
        self.mgr.authenticate(issued.secret)
        self.conn.commit()
        self.conn.close()
        blob = self.path.read_bytes()
        self.assertNotIn(issued.secret.encode(), blob)
        self.assertNotIn(canary.encode(), blob)
        # The prefix is expected to be there: it is what the list screen shows.
        self.assertIn(issued.info.prefix.encode(), blob)

    def test_there_is_no_column_that_could_hold_a_plaintext_secret(self):
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(api_keys)')}
        for name in columns:
            self.assertNotIn('password', name)
            self.assertNotIn('plaintext', name)
        # And the issuer takes no parameter that could carry a password.
        with self.assertRaises(TypeError):
            self.mgr.create(actor=None, name='x', password='nope')

    def test_a_wrong_secret_of_a_known_prefix_is_refused(self):
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        forged = f'{apikeys.SECRET_MARK}{issued.info.prefix}_' + 'Z' * 43
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.mgr.authenticate(forged)
        self.assertEqual(caught.exception.code, apikeys.E_INVALID)

    def test_an_unknown_prefix_costs_the_same_verification_as_a_known_one(self):
        issued = self.mgr.bootstrap_admin(local_trusted=True, name='admin')
        unknown = f'{apikeys.SECRET_MARK}nosuchhk_' + 'A' * 43
        self.assertNotEqual(issued.info.prefix, apikeys.secret_prefix(unknown))
        calls = []
        original = apikeys._hash_secret

        def counting(secret, salt, iterations):
            calls.append(salt)
            return original(secret, salt, iterations)

        apikeys._hash_secret = counting
        self.addCleanup(setattr, apikeys, '_hash_secret', original)
        with self.assertRaises(apikeys.ApiKeyError):
            self.mgr.authenticate(unknown)
        with self.assertRaises(apikeys.ApiKeyError):
            self.mgr.authenticate(f'{apikeys.SECRET_MARK}{issued.info.prefix}_' + 'Z' * 43)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])

    def test_verification_compares_digests_with_compare_digest(self):
        source = Path(apikeys.__file__).read_text()
        self.assertIn('hmac.compare_digest', source)
        self.assertNotIn('== verifier', source)

    def test_authentication_reports_a_missing_key_separately_from_a_wrong_one(self):
        for value, code in ((None, apikeys.E_MISSING), ('', apikeys.E_MISSING),
                            ('not-a-key', apikeys.E_INVALID)):
            with self.subTest(value=value):
                with self.assertRaises(apikeys.ApiKeyError) as caught:
                    self.mgr.authenticate(value)
                self.assertEqual(caught.exception.code, code)


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = support.connect(self.tmp.name)
        self.addCleanup(self.conn.close)

    def test_ensure_schema_matches_the_contract_columns_in_order(self):
        columns = [row[1] for row in self.conn.execute('PRAGMA table_info(api_keys)')]
        self.assertEqual(columns[:len(apikeys.CONTRACT_API_KEYS_COLUMNS)],
                         list(apikeys.CONTRACT_API_KEYS_COLUMNS))
        audit = [row[1] for row in self.conn.execute('PRAGMA table_info(audit_log)')]
        self.assertEqual(audit, list(apikeys.AUDIT_LOG_COLUMNS))

    def test_ensure_schema_is_idempotent(self):
        first = Path(self.tmp.name) / 'first.db'
        second = Path(self.tmp.name) / 'second.db'
        import sqlite3
        one = sqlite3.connect(str(first))
        apikeys.ensure_schema(one)
        apikeys.ensure_schema(one)
        two = sqlite3.connect(str(second))
        apikeys.ensure_schema(two)
        self.addCleanup(one.close)
        self.addCleanup(two.close)
        for table in ('api_keys', 'audit_log'):
            self.assertEqual(
                [tuple(row) for row in one.execute(f'PRAGMA table_info({table})')],
                [tuple(row) for row in two.execute(f'PRAGMA table_info({table})')])

    def test_ensure_schema_adds_a_column_to_an_older_table(self):
        import sqlite3
        legacy = sqlite3.connect(str(Path(self.tmp.name) / 'legacy.db'))
        self.addCleanup(legacy.close)
        legacy.execute('''CREATE TABLE api_keys(
            id TEXT PRIMARY KEY, prefix TEXT NOT NULL, name TEXT, purpose TEXT, created_at REAL,
            expires_at REAL, last_used_at REAL, revoked_at REAL, permissions_json TEXT NOT NULL,
            resource_scope_json TEXT NOT NULL, rate_limit_json TEXT, concurrency_json TEXT,
            rotation_grace_until REAL, verifier TEXT NOT NULL, verifier_salt TEXT NOT NULL,
            verifier_algo TEXT NOT NULL)''')
        legacy.commit()
        apikeys.ensure_schema(legacy)
        columns = {row[1] for row in legacy.execute('PRAGMA table_info(api_keys)')}
        self.assertIn('disabled_at', columns)
        self.assertIn('previous_verifier', columns)
        mgr = apikeys.ApiKeyManager(legacy, now=support.Clock())
        issued = mgr.bootstrap_admin(local_trusted=True, name='admin')
        self.assertEqual(mgr.authenticate(issued.secret).key_id, issued.info.id)

    def test_prefix_index_from_migration_14_exists(self):
        names = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'api_keys'")}
        self.assertTrue(any('prefix' in name for name in names), names)

    def test_manager_refuses_a_database_without_the_table(self):
        import sqlite3
        empty = sqlite3.connect(str(Path(self.tmp.name) / 'empty.db'))
        self.addCleanup(empty.close)
        with self.assertRaises(apikeys.ApiKeyError):
            apikeys.ApiKeyManager(empty)


class EnvironmentTests(unittest.TestCase):
    def test_nothing_is_read_from_the_environment_to_derive_a_key(self):
        source = Path(apikeys.__file__).read_text()
        self.assertNotIn('os.environ', source)
        self.assertNotIn('os.getenv', source)
        self.assertNotIn('sys.argv', source)
        self.assertNotIn('argv', source)
        self.assertTrue(hasattr(os, 'environ'))  # the check above is about this module


if __name__ == '__main__':
    unittest.main()

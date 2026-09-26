"""Credential lifecycle across workbench connections and import transactions."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from proxy_workbench import db, proxytool, secrets


class RuntimeVaultTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(secrets.OsVault, 'available', return_value=False))
        self.workbench = proxytool.Workbench(self.home)
        self.addCleanup(self.workbench.close)
        self.endpoint = db.upsert_endpoint(self.workbench.conn, 'http://198.51.100.1:8080')
        self.workbench.conn.commit()

    def test_reopening_a_workbench_keeps_this_sessions_credentials(self):
        store = self.workbench.accesses()
        access = store.create(self.endpoint, 'http', username='local-test', password='test-canary')
        self.workbench.close()
        reopened = self.workbench.accesses()
        self.assertIsNot(reopened, store)
        with reopened.resolve(access.id) as resolved:
            self.assertEqual(resolved.password, 'test-canary')
        with proxytool.Workbench(self.home) as another:
            self.assertIs(another.accesses().vault, reopened.vault)

    def test_distinct_databases_do_not_share_a_session_vault(self):
        with proxytool.Workbench(self.home / 'other') as other:
            self.assertIsNot(other.accesses().vault, self.workbench.accesses().vault)

    def test_create_does_not_commit_the_callers_transaction(self):
        conn = self.workbench.conn
        vault = self.workbench.accesses().vault
        store = secrets.AccessStore(conn, vault, commit=False)
        conn.execute('BEGIN IMMEDIATE')
        created = store.create(self.endpoint, 'http', username='local-test', password='test-canary')
        self.assertTrue(conn.in_transaction)
        self.assertNotEqual(vault.state(created.secret_ref), secrets.STATE_READY)
        conn.rollback()
        store.rollback_pending()
        self.assertIsNone(store.get(created.id))
        self.assertIsNone(vault.state(created.secret_ref))

    def test_committed_create_can_be_finalized_and_resolved(self):
        conn = self.workbench.conn
        store = secrets.AccessStore(conn, self.workbench.accesses().vault, commit=False)
        conn.execute('BEGIN IMMEDIATE')
        created = store.create(self.endpoint, 'http', username='local-test', password='test-canary')
        conn.commit()
        store.finish_pending()
        with self.workbench.accesses().resolve(created.id) as resolved:
            self.assertEqual(resolved.password, 'test-canary')

    def test_anonymous_observation_uses_one_vault_without_committing(self):
        conn = self.workbench.conn
        before = len(secrets._SESSIONS)
        conn.execute('BEGIN IMMEDIATE')
        for _ in range(20):
            proxytool.anonymous_access(conn, self.endpoint, 'http')
        self.assertTrue(conn.in_transaction)
        self.assertLessEqual(len(secrets._SESSIONS) - before, 1)
        conn.rollback()

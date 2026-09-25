"""Access identities, credential rotation and the vault/SQLite staging protocol.

The schema comes from db.py, which owns the migrator (CONTRACTS.ru.md §3.3):
migration 1 creates `endpoints` and migration 3 creates `accesses`. The inline
mirror below is only a fallback for a tree where db.py has not landed yet, and
`SchemaContractTests` checks it against the declared column list either way.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import secrets as s

try:
    from proxy_workbench import db
except ImportError:  # db.py has not landed yet
    db = None

CANARY = 'canary-secret-3f7a'
CANARY_NEW = 'canary-secret-9c2d'

#: CONTRACTS.ru.md §3.3, migration 3, verbatim.
ACCESS_COLUMNS = ['id', 'endpoint_id', 'mode', 'secret_ref', 'access_revision', 'created_at', 'rotated_at']
CONTRACT_SCHEMA = """
CREATE TABLE endpoints (id TEXT PRIMARY KEY, canonical TEXT UNIQUE NOT NULL, host TEXT, port INTEGER,
                        scheme TEXT, ip_version INTEGER, country TEXT, country_source TEXT, country_at REAL,
                        asn INTEGER, provider TEXT, hosting INTEGER, cidr TEXT,
                        first_seen_at REAL, last_seen_at REAL);
CREATE TABLE accesses (id TEXT PRIMARY KEY, endpoint_id TEXT NOT NULL REFERENCES endpoints(id),
                       mode TEXT NOT NULL, secret_ref TEXT, access_revision INTEGER NOT NULL,
                       created_at REAL, rotated_at REAL);
"""


def open_contract_db(path):
    """Open a database with the contract schema, through db.py when it is there."""
    if db is None:
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(CONTRACT_SCHEMA)
        return conn
    conn, _report = db.open_db(path)
    return conn


class FailingConnection:
    """Wraps a connection and breaks one statement, for compensation tests."""

    def __init__(self, conn, *, fail_on):
        self._conn = conn
        self._fail_on = fail_on

    def execute(self, sql, *args):
        if self._fail_on in sql:
            raise sqlite3.OperationalError('disk I/O error (simulated)')
        return self._conn.execute(sql, *args)

    def commit(self):
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class StagedVault(s.MemoryVault):
    """A vault whose finalize step can be made to fail once."""

    def __init__(self, *, fail_finalize=0):
        super().__init__()
        self.fail_finalize = fail_finalize

    def mark_ready(self, ref):
        if self.fail_finalize > 0:
            self.fail_finalize -= 1
            raise s.SecretVaultLocked('keychain write failed (simulated)')
        return super().mark_ready(ref)


class AccessStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / 'db.sqlite3'
        self.conn = open_contract_db(self.path)
        self.addCleanup(self.conn.close)
        self.ep1 = 'ep-1'
        self.ep2 = 'ep-2'
        if db is not None:
            self.ep1 = db.upsert_endpoint(self.conn, 'http://proxy.example.com:8080')
            self.ep2 = db.upsert_endpoint(self.conn, 'socks5://10.0.0.5:1080')
        else:
            self.conn.execute('INSERT INTO endpoints (id, canonical) VALUES (?, ?)',
                              (self.ep1, 'http://proxy.example.com:8080'))
            self.conn.execute('INSERT INTO endpoints (id, canonical) VALUES (?, ?)',
                              (self.ep2, 'socks5://10.0.0.5:1080'))
            self.conn.commit()
        self.vault = s.MemoryVault()
        self.clock = {'now': 1000.0}
        self.store = s.AccessStore(self.conn, self.vault, now=lambda: self.clock['now'])

    def create(self, endpoint_id=None, scheme='http', **kwargs):
        kwargs.setdefault('username', 'alice')
        kwargs.setdefault('password', CANARY)
        return self.store.create(endpoint_id or self.ep1, scheme, **kwargs)

    def rows(self):
        return [dict(row) for row in self.conn.execute('SELECT %s FROM accesses' % s.COLUMNS)]


class RecordingConnection:
    """Keeps every statement, so the tests can check the SQL the module writes."""

    def __init__(self, conn):
        self._conn = conn
        self.statements = []

    def execute(self, sql, *args):
        self.statements.append(' '.join(sql.split()))
        return self._conn.execute(sql, *args)

    def commit(self):
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class SchemaContractTests(AccessStoreTestCase):
    def test_accesses_has_exactly_the_contract_columns(self):
        names = [row['name'] for row in self.conn.execute('PRAGMA table_info(accesses)')]
        self.assertEqual(names, ACCESS_COLUMNS)
        self.assertEqual(s.COLUMNS, ', '.join(ACCESS_COLUMNS))

    def test_the_database_file_never_holds_a_password(self):
        access = self.create(access_id='acc-1')
        self.store.rotate('acc-1', password=CANARY_NEW)
        self.conn.execute('INSERT INTO collections (id, name, kind, created_at) VALUES (?,?,?,?)',
                          ('col-1', 'Мои прокси', 'private', 1000.0))
        self.conn.commit()
        for path in Path(self.temp).glob('db.sqlite3*'):
            blob = path.read_bytes()
            self.assertNotIn(CANARY.encode(), blob, path.name)
            self.assertNotIn(CANARY_NEW.encode(), blob, path.name)
            self.assertNotIn(b'alice', blob, path.name)
        self.assertEqual(self.store.resolve(access.id).password, CANARY_NEW)

    def test_every_write_names_its_columns(self):
        recorder = RecordingConnection(self.conn)
        store = s.AccessStore(recorder, self.vault, now=lambda: 1000.0)
        store.create(self.ep1, 'http', username='alice', password=CANARY, access_id='acc-1')
        store.rotate('acc-1', password=CANARY_NEW)
        store.purge('acc-1')
        writes = [sql for sql in recorder.statements if 'accesses' in sql]
        self.assertTrue(writes)
        for sql in writes:
            if sql.startswith('INSERT'):
                self.assertIn('(id, endpoint_id, mode, secret_ref', sql)
            if sql.startswith('UPDATE'):
                self.assertIn('access_revision = ?', sql)

    def test_reads_ask_for_the_contract_columns(self):
        recorder = RecordingConnection(self.conn)
        store = s.AccessStore(recorder, self.vault, now=lambda: 1000.0)
        store.list_for_endpoint(self.ep1)
        store.get('acc-missing')
        store.reconcile()
        for sql in recorder.statements:
            if sql.startswith('SELECT'):
                self.assertIn(s.COLUMNS, sql)


class RebindContractTests(AccessStoreTestCase):
    """db.rebind_secrets moves references behind this module's back; both must hold."""

    def setUp(self):
        super().setUp()
        if db is None:
            self.skipTest('db.py is not in the tree')
        self.access = self.create(access_id='acc-1')

    def test_a_rebind_to_a_matching_revision_keeps_working(self):
        replacement = 'sec_rebound'
        self.vault.stage(replacement, s.SecretPayload('alice', CANARY_NEW,
                                                      s.make_verifier(CANARY_NEW, iterations=1000),
                                                      revision=self.access.access_revision))
        self.vault.mark_ready(replacement)
        db.rebind_secrets(self.conn, {self.access.secret_ref: replacement}, dry_run=False)
        self.assertEqual(self.store.get('acc-1').secret_ref, replacement)
        self.assertEqual(self.store.resolve('acc-1').password, CANARY_NEW)
        report = self.store.reconcile()
        self.assertEqual(report.removed_orphan_refs, (self.access.secret_ref,),
                         'the reference the row no longer owns must be cleaned up')
        self.assertEqual(self.store.resolve('acc-1').password, CANARY_NEW)

    def test_a_rebind_to_a_foreign_revision_is_refused_until_reconciled(self):
        replacement = 'sec_rebound'
        self.vault.stage(replacement, s.SecretPayload('alice', CANARY_NEW,
                                                      s.make_verifier(CANARY_NEW, iterations=1000),
                                                      revision=self.access.access_revision + 5))
        self.vault.mark_ready(replacement)
        db.rebind_secrets(self.conn, {self.access.secret_ref: replacement}, dry_run=False)
        with self.assertRaises(s.SecretConflictError) as caught:
            self.store.resolve('acc-1')
        self.assertEqual(caught.exception.code, s.E_CONFLICT)
        self.assertIn('reconcile()', caught.exception.action)


class CreateTests(AccessStoreTestCase):
    def test_create_stores_only_a_reference(self):
        access = self.create()
        self.assertEqual(access.mode, s.MODE_HTTP_BASIC)
        self.assertEqual(access.access_revision, 1)
        self.assertIsNone(access.rotated_at)
        self.assertTrue(access.secret_ref.startswith('sec_'))
        self.assertEqual(self.rows(), [dict(id=access.id, endpoint_id=self.ep1, mode=s.MODE_HTTP_BASIC,
                                            secret_ref=access.secret_ref, access_revision=1,
                                            created_at=1000.0, rotated_at=None)])

    def test_the_stored_row_holds_no_part_of_the_password(self):
        self.create(username='alice@corp', password=CANARY)
        blob = repr(self.rows()) + repr(self.conn.iterdump().__iter__().__next__())
        self.assertNotIn(CANARY, blob)
        self.assertEqual(self.vault.get(self.rows()[0]['secret_ref']).password, CANARY)

    def test_socks5_credentials_get_their_own_mode(self):
        access = self.create(endpoint_id=self.ep2, scheme='socks5')
        self.assertEqual(access.mode, s.MODE_SOCKS5)
        self.assertEqual(self.store.resolve(access.id).username, 'alice')

    def test_two_credentials_of_one_endpoint_are_two_accesses(self):
        first = self.create(username='alice', password=CANARY, access_id='acc-alice')
        second = self.create(username='bob', password=CANARY_NEW, access_id='acc-bob')
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.secret_ref, second.secret_ref)
        self.assertEqual([access.id for access in self.store.list_for_endpoint(self.ep1)],
                         ['acc-alice', 'acc-bob'])
        self.assertEqual(self.store.resolve('acc-alice').password, CANARY)
        self.assertEqual(self.store.resolve('acc-bob').password, CANARY_NEW)
        self.assertEqual(self.store.resolve('acc-bob').username, 'bob')

    def test_creating_the_same_username_twice_does_not_overwrite(self):
        self.create(access_id='acc-1')
        self.create(access_id='acc-2', password=CANARY_NEW)
        self.assertEqual(len(self.store.list_for_endpoint(self.ep1)), 2)
        self.assertEqual(self.store.resolve('acc-1').password, CANARY)
        self.assertEqual(self.store.resolve('acc-2').password, CANARY_NEW)

    def test_open_proxy_access_needs_no_reference(self):
        access = self.store.create(self.ep1, 'http', access_id='acc-open')
        self.assertEqual(access.mode, s.MODE_NONE)
        self.assertIsNone(access.secret_ref)
        self.assertEqual(self.store.state(access), s.STATE_NO_REF)
        self.assertTrue(self.store.usable(access))
        self.assertEqual(self.store.resolve('acc-open').password, '')

    def test_half_a_credential_is_refused(self):
        with self.assertRaises(s.SecretValidationError):
            self.store.create(self.ep1, 'http', username='alice')
        with self.assertRaises(s.SecretValidationError):
            self.store.create(self.ep1, 'http', password=CANARY)

    def test_unknown_endpoint_id_is_refused(self):
        with self.assertRaises(s.SecretValidationError):
            self.store.create('', 'http', username='a', password=CANARY)
        with self.assertRaises(s.SecretValidationError):
            self.store.create('   ', 'http', username='a', password=CANARY)

    def test_socks4_credentials_are_refused_explicitly(self):
        with self.assertRaises(s.SecretUnsupportedError) as caught:
            self.store.create(self.ep2, 'socks4', username='alice', password=CANARY)
        self.assertEqual(caught.exception.code, s.E_VALIDATION)
        self.assertIn('SOCKS4', caught.exception.message)

    def test_unsupported_scheme_is_refused(self):
        with self.assertRaises(s.SecretUnsupportedError):
            self.store.create(self.ep1, 'ntlm', username='alice', password=CANARY)

    def test_mode_and_scheme_must_agree(self):
        with self.assertRaises(s.SecretValidationError):
            self.store.create(self.ep1, 'http', mode=s.MODE_SOCKS5, username='a', password=CANARY)

    def test_foreign_endpoint_is_refused_by_the_database(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create('ep-unknown', 'http', username='a', password=CANARY)
        self.assertEqual(self.vault.refs(), [], 'a refused insert must not leave a staged secret')


class RotationTests(AccessStoreTestCase):
    def setUp(self):
        super().setUp()
        self.access = self.create(access_id='acc-1')

    def test_rotation_keeps_the_reference_and_bumps_the_revision(self):
        rotated = self.store.rotate('acc-1', password=CANARY_NEW)
        self.assertEqual(rotated.secret_ref, self.access.secret_ref)
        self.assertEqual(rotated.access_revision, 2)
        self.assertEqual(rotated.rotated_at, 1000.0)
        self.assertEqual(self.store.get('acc-1').access_revision, 2)
        self.assertEqual(self.store.resolve('acc-1').password, CANARY_NEW)

    def test_a_new_password_does_not_inherit_the_old_evidence(self):
        self.store.rotate('acc-1', password=CANARY_NEW)
        current = self.store.get('acc-1')
        self.assertTrue(s.is_superseded(current, 1))
        self.assertFalse(s.is_superseded(current, 2))
        self.assertEqual(s.admission_key(current, 1), (current.id, 1))
        self.assertNotEqual(s.admission_key(current, 1), s.admission_key(current, 2))
        with self.assertRaises(s.SecretConflictError) as caught:
            self.store.resolve('acc-1', access_revision=1)
        self.assertEqual(caught.exception.code, s.E_CONFLICT)
        self.assertIn('superseded', caught.exception.message)

    def test_rotation_changes_the_username_too(self):
        self.store.rotate('acc-1', username='alice2', password=CANARY_NEW)
        resolved = self.store.resolve('acc-1')
        self.assertEqual((resolved.username, resolved.password), ('alice2', CANARY_NEW))
        self.assertEqual(self.store.get('acc-1').access_revision, 2)

    def test_rotation_without_a_new_password_keeps_the_old_one(self):
        self.store.rotate('acc-1', username='alice2')
        self.assertEqual(self.store.resolve('acc-1').password, CANARY)

    def test_rotation_of_an_empty_username_is_refused(self):
        with self.assertRaises(s.SecretValidationError):
            self.store.rotate('acc-1', username='')

    def test_stale_expectation_is_a_conflict(self):
        self.store.rotate('acc-1', password=CANARY_NEW)
        with self.assertRaises(s.SecretConflictError) as caught:
            self.store.rotate('acc-1', password='another', expect_revision=1)
        self.assertEqual(caught.exception.code, s.E_CONFLICT)
        self.assertEqual(self.store.get('acc-1').access_revision, 2)
        self.assertEqual(self.store.resolve('acc-1').password, CANARY_NEW)

    def test_matching_expectation_is_accepted(self):
        self.store.rotate('acc-1', password=CANARY_NEW, expect_revision=1)
        self.assertEqual(self.store.get('acc-1').access_revision, 2)

    def test_rotation_of_an_open_access_without_a_credential_is_a_no_op(self):
        access = self.store.create(self.ep1, 'http', access_id='acc-open')
        self.assertEqual(self.store.rotate('acc-open'), access)

    def test_rotation_of_an_open_access_into_a_credential_is_refused(self):
        self.store.create(self.ep1, 'http', access_id='acc-open')
        with self.assertRaises(s.SecretValidationError):
            self.store.rotate('acc-open', password=CANARY)

    def test_rotation_of_an_unknown_access_is_refused_with_an_action(self):
        with self.assertRaises(s.SecretValidationError) as caught:
            self.store.rotate('acc-nope', password=CANARY_NEW)
        self.assertTrue(caught.exception.action)

    def test_previous_password_still_verifies_only_against_its_own_revision(self):
        self.store.rotate('acc-1', password=CANARY_NEW)
        self.assertTrue(self.store.verify_password('acc-1', CANARY_NEW))
        self.assertTrue(self.store.verify_password('acc-1', CANARY, access_revision=1))
        self.assertFalse(self.store.verify_password('acc-1', CANARY))
        self.assertFalse(self.store.verify_password('acc-1', 'wrong'))

    def test_rotation_uses_the_injected_clock(self):
        self.clock['now'] = 4321.0
        self.assertEqual(self.store.rotate('acc-1', password=CANARY_NEW).rotated_at, 4321.0)


class LockedVaultTests(AccessStoreTestCase):
    def setUp(self):
        super().setUp()
        self.access = self.create(access_id='acc-1')

    def test_a_locked_vault_is_reported_and_blocks_resolution(self):
        self.vault.lock()
        self.assertEqual(self.store.state(self.access), s.STATE_LOCKED)
        with self.assertRaises(s.SecretVaultLocked) as caught:
            self.store.resolve('acc-1')
        self.assertEqual(caught.exception.code, s.E_VAULT_LOCKED)
        self.assertTrue(caught.exception.action)

    def test_unlocking_makes_the_same_access_usable_again(self):
        self.vault.lock()
        with self.assertRaises(s.SecretVaultLocked):
            self.store.resolve('acc-1')
        self.vault.unlock()
        self.assertEqual(self.store.state(self.access), s.STATE_READY)
        self.assertEqual(self.store.resolve('acc-1').password, CANARY)

    def test_rotation_under_a_locked_vault_changes_nothing(self):
        self.vault.lock()
        with self.assertRaises(s.SecretVaultLocked):
            self.store.rotate('acc-1', password=CANARY_NEW)
        self.vault.unlock()
        self.assertEqual(self.store.get('acc-1').access_revision, 1)
        self.assertEqual(self.store.resolve('acc-1').password, CANARY)

    def test_create_under_a_locked_vault_writes_no_row(self):
        self.vault.lock()
        with self.assertRaises(s.SecretVaultLocked):
            self.create(access_id='acc-2')
        self.vault.unlock()
        self.assertEqual([access.id for access in self.store.list_for_endpoint(self.ep1)], ['acc-1'])


class StagingProtocolTests(AccessStoreTestCase):
    def test_a_failed_database_write_compensates_the_staged_secret(self):
        store = s.AccessStore(FailingConnection(self.conn, fail_on='INSERT INTO accesses'),
                              self.vault, now=lambda: 1000.0)
        with self.assertRaises(sqlite3.OperationalError):
            store.create(self.ep1, 'http', username='alice', password=CANARY, access_id='acc-1')
        self.assertEqual(self.vault.refs(), [], 'the staged reference must be removed again')
        self.assertEqual(self.rows(), [])

    def test_a_failed_finalize_leaves_no_ready_access_and_reconcile_finishes_it(self):
        vault = StagedVault(fail_finalize=1)
        store = s.AccessStore(self.conn, vault, now=lambda: 1000.0)
        with self.assertRaises(s.SecretVaultLocked) as caught:
            store.create(self.ep1, 'http', username='alice', password=CANARY, access_id='acc-1')
        self.assertEqual(caught.exception.code, s.E_VAULT_LOCKED)
        self.assertIn('reconcile()', caught.exception.detail['next'])
        self.assertEqual(vault.state(vault.refs()[0]), s.STATE_STAGED)
        access = store.get('acc-1')
        self.assertEqual(store.state(access), s.STATE_STAGED)
        self.assertFalse(store.usable(access))
        with self.assertRaises(s.SecretNotProvided) as refused:
            store.resolve('acc-1')
        self.assertEqual(refused.exception.code, s.E_NOT_PROVIDED)
        report = store.reconcile()
        self.assertEqual(report.finalized_refs, ('acc-1',))
        self.assertFalse(report.clean)
        self.assertTrue(store.usable(access))
        self.assertEqual(store.resolve('acc-1').password, CANARY)

    def test_a_failed_rotation_restores_the_previous_credential(self):
        store = s.AccessStore(FailingConnection(self.conn, fail_on='UPDATE accesses'),
                              self.vault, now=lambda: 1000.0)
        self.create(access_id='acc-1')
        with self.assertRaises(sqlite3.OperationalError):
            store.rotate('acc-1', password=CANARY_NEW)
        access = self.store.get('acc-1')
        self.assertEqual(access.access_revision, 1)
        self.assertIsNone(access.rotated_at)
        self.assertEqual(self.vault.state(access.secret_ref), s.STATE_READY)
        self.assertEqual(self.vault.get(access.secret_ref).password, CANARY)
        self.assertEqual(self.store.resolve('acc-1').password, CANARY)

    def test_a_failed_finalize_after_a_rotation_is_reported_for_reconciliation(self):
        vault = StagedVault()
        store = s.AccessStore(self.conn, vault, now=lambda: 1000.0)
        store.create(self.ep1, 'http', username='alice', password=CANARY, access_id='acc-1')
        vault.fail_finalize = 1
        with self.assertRaises(s.SecretVaultLocked) as caught:
            store.rotate('acc-1', password=CANARY_NEW)
        self.assertEqual(caught.exception.detail['access_revision'], 2)
        access = store.get('acc-1')
        self.assertEqual(access.access_revision, 2)
        self.assertEqual(store.state(access), s.STATE_STAGED)
        with self.assertRaises(s.SecretNotProvided):
            store.resolve('acc-1')
        report = store.reconcile()
        self.assertEqual(report.finalized_refs, ('acc-1',))
        self.assertEqual(store.resolve('acc-1').password, CANARY_NEW)

    def test_reconcile_removes_a_reference_no_row_owns(self):
        self.create(access_id='acc-1')
        self.vault.stage('sec_orphan', s.SecretPayload('ghost', 'ghost', 'x', 1))
        self.vault.mark_ready('sec_orphan')
        self.assertEqual(len(self.vault.refs()), 2)
        report = self.store.reconcile()
        self.assertEqual(report.removed_orphan_refs, ('sec_orphan',))
        self.assertEqual(len(self.vault.refs()), 1)

    def test_reconcile_reports_a_reference_that_vanished(self):
        self.create(access_id='acc-1')
        self.vault.delete(self.store.get('acc-1').secret_ref)
        report = self.store.reconcile()
        self.assertEqual(report.missing_refs, ('acc-1',))
        with self.assertRaises(s.SecretNotProvided):
            self.store.resolve('acc-1')

    def test_reconcile_is_idempotent(self):
        self.create(access_id='acc-1')
        self.vault.stage('sec_orphan', s.SecretPayload('ghost', 'ghost', 'x', 1))
        self.vault.mark_ready('sec_orphan')
        first = self.store.reconcile()
        second = self.store.reconcile()
        self.assertFalse(first.clean)
        self.assertTrue(second.clean)
        self.assertEqual(second.rows, 1)

    def test_reconcile_does_not_repair_anything_while_locked(self):
        self.create(access_id='acc-1')
        self.vault.stage('sec_orphan', s.SecretPayload('ghost', 'ghost', 'x', 1))
        self.vault.mark_ready('sec_orphan')
        self.vault.lock()
        report = self.store.reconcile()
        self.assertEqual(report.locked_refs, ('acc-1',))
        self.assertEqual(report.removed_orphan_refs, ())
        self.vault.unlock()
        self.assertEqual(len(self.vault.refs()), 2)

    def test_reconcile_refuses_to_finalize_a_revision_the_database_does_not_know(self):
        access = self.create(access_id='acc-1')
        self.vault.stage(access.secret_ref, s.SecretPayload('alice', CANARY_NEW,
                                                             s.make_verifier(CANARY_NEW, iterations=1000),
                                                             revision=7))
        report = self.store.reconcile()
        self.assertEqual(report.missing_refs, ('acc-1',))
        self.assertEqual(self.vault.state(access.secret_ref), s.STATE_STAGED)
        with self.assertRaises(s.SecretNotProvided) as caught:
            self.store.resolve('acc-1')
        self.assertEqual(caught.exception.code, s.E_NOT_PROVIDED)

    def test_a_ready_reference_with_a_foreign_revision_is_a_conflict(self):
        access = self.create(access_id='acc-1')
        self.vault.stage(access.secret_ref, s.SecretPayload('alice', CANARY_NEW,
                                                             s.make_verifier(CANARY_NEW, iterations=1000),
                                                             revision=7))
        self.vault.mark_ready(access.secret_ref)
        with self.assertRaises(s.SecretConflictError) as caught:
            self.store.resolve('acc-1')
        self.assertEqual(caught.exception.code, s.E_CONFLICT)

    def test_purge_removes_the_row_and_the_secret(self):
        access = self.create(access_id='acc-1')
        self.assertTrue(self.store.purge('acc-1'))
        self.assertFalse(self.store.purge('acc-1'))
        self.assertIsNone(self.store.get('acc-1'))
        self.assertEqual(self.vault.refs(), [])

    def test_purge_under_a_locked_vault_keeps_the_row_removal_only(self):
        access = self.create(access_id='acc-1')
        self.vault.lock()
        self.assertTrue(self.store.purge('acc-1'))
        self.vault.unlock()
        self.assertIsNone(self.store.get('acc-1'))
        self.assertEqual(self.vault.refs(), [access.secret_ref])


class ConflictChoiceTests(AccessStoreTestCase):
    def test_find_by_username_points_at_the_access_to_rotate(self):
        first = self.create(username='alice', password=CANARY, access_id='acc-alice')
        self.create(username='bob', password=CANARY_NEW, access_id='acc-bob')
        self.assertEqual(self.store.find_by_username(self.ep1, 'alice').id, first.id)
        self.assertEqual(self.store.find_by_username(self.ep1, 'bob').id, 'acc-bob')
        self.assertIsNone(self.store.find_by_username(self.ep1, 'carol'))
        self.assertIsNone(self.store.find_by_username(self.ep1, None))
        self.assertIsNone(self.store.find_by_username(self.ep2, 'alice'))

    def test_find_by_username_follows_a_rotated_username(self):
        self.create(username='alice', password=CANARY, access_id='acc-1')
        self.assertEqual(self.store.find_by_username(self.ep1, 'alice').id, 'acc-1')
        self.store.rotate('acc-1', username='alice2')
        self.assertIsNone(self.store.find_by_username(self.ep1, 'alice'))
        self.assertEqual(self.store.find_by_username(self.ep1, 'alice2').id, 'acc-1')

    def test_a_removed_access_is_not_offered_as_a_conflict(self):
        self.create(username='alice', password=CANARY, access_id='acc-1')
        self.store.purge('acc-1')
        self.assertIsNone(self.store.find_by_username(self.ep1, 'alice'))


if __name__ == '__main__':
    unittest.main()

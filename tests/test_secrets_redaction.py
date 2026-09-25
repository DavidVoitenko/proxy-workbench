"""Redaction: what may leave the module, and what may never appear anywhere else.

The canary below is a fake credential. It exists so the tests can prove it never
reaches the database, a JSON payload, a log line or a URL that a caller would print.
"""
import json
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

CANARY = 'canary-secret-6e4f'
SCHEMA = """
CREATE TABLE endpoints (id TEXT PRIMARY KEY, canonical TEXT UNIQUE NOT NULL);
CREATE TABLE accesses (id TEXT PRIMARY KEY, endpoint_id TEXT NOT NULL REFERENCES endpoints(id),
                       mode TEXT NOT NULL, secret_ref TEXT, access_revision INTEGER NOT NULL,
                       created_at REAL, rotated_at REAL);
"""


class RedactionTests(unittest.TestCase):
    def access(self, access_id='acc-1', revision=1, ref='sec_1'):
        return s.Access(access_id, 'ep-1', s.MODE_HTTP_BASIC, ref, revision, 1000.0, 1001.0)

    def test_describe_carries_identity_but_no_credential(self):
        payload = s.describe(self.access(), state=s.STATE_READY)
        self.assertEqual(sorted(payload), ['access_id', 'access_revision', 'endpoint_id', 'mode',
                                           'rotated_at', 'secret_ref', 'secret_state'])
        self.assertNotIn(CANARY, json.dumps(payload))
        self.assertEqual(payload['access_revision'], 1)
        self.assertEqual(payload['secret_state'], s.STATE_READY)

    def test_scrub_mapping_masks_secret_shaped_keys(self):
        clean = s.scrub_mapping({'username': 'alice', 'password': CANARY, 'proxy_password': CANARY,
                                 'api_key': 'k', 'Authorization': 'Basic x', 'nested': {'secret': CANARY},
                                 'access_id': 'acc-1', 'latency_ms': 12.5})
        self.assertEqual(clean['username'], 'alice')
        self.assertEqual(clean['access_id'], 'acc-1')
        self.assertEqual(clean['latency_ms'], 12.5)
        self.assertEqual(clean['password'], s.REDACTED)
        self.assertEqual(clean['proxy_password'], s.REDACTED)
        self.assertEqual(clean['api_key'], s.REDACTED)
        self.assertEqual(clean['Authorization'], s.REDACTED)
        self.assertEqual(clean['nested']['secret'], s.REDACTED)
        self.assertNotIn(CANARY, json.dumps(clean))
        self.assertEqual(s.scrub_mapping('not a mapping'), {})

    def test_redact_text_hides_a_known_value_including_inside_a_url(self):
        url = 'http://alice:%s@proxy.example.com:8080' % CANARY
        self.assertNotIn(CANARY, s.redact_text('failed to use %s' % url, [CANARY]))
        self.assertEqual(s.redact_text(None, [CANARY]), None)
        self.assertEqual(s.redact_text('nothing to hide', []), 'nothing to hide')

    def test_redact_proxy_url_strips_userinfo_even_without_the_value(self):
        url = 'http://alice:%s@proxy.example.com:8080' % CANARY
        self.assertEqual(s.redact_proxy_url(url), 'http://proxy.example.com:8080')
        self.assertEqual(s.redact_proxy_url('http://proxy.example.com:8080'), 'http://proxy.example.com:8080')
        self.assertEqual(s.redact_proxy_url('not a url'), 'not a url')

    def test_log_fields_scrub_what_the_caller_adds(self):
        fields = s.log_fields(self.access(), state=s.STATE_READY, proxy='http://alice:%s@host:8080' % CANARY,
                              password=CANARY, attempts=2)
        self.assertNotIn(CANARY, json.dumps(fields))
        self.assertEqual(fields['attempts'], 2)
        self.assertEqual(fields['access_id'], 'acc-1')

    def test_errors_carry_a_code_and_an_action_and_no_credential(self):
        try:
            s.parse_endpoint('http://alice:%s@proxy.example.com:8080' % CANARY)
        except s.SecretError as exc:
            described = exc.describe()
        self.assertEqual(described['code'], s.E_VALIDATION)
        self.assertTrue(described['action'])
        self.assertNotIn(CANARY, json.dumps(described))

    def test_a_resolved_secret_can_be_scrubbed_and_used_as_a_context(self):
        access = self.access()
        with s.ResolvedAccess(access, s.MODE_HTTP_BASIC, 'alice', CANARY) as resolved:
            self.assertEqual(resolved.auth, ('alice', CANARY))
        self.assertEqual(resolved.password, '')
        self.assertTrue(resolved._scrubbed)
        self.assertNotIn(CANARY, repr(resolved))
        self.assertNotIn(CANARY, json.dumps(resolved.describe()))

    def test_a_vault_error_never_quotes_the_password(self):
        vault = s.MemoryVault()
        vault.lock()
        try:
            vault.get('sec_1')
        except s.SecretError as exc:
            self.assertNotIn(CANARY, str(exc))
            self.assertEqual(exc.code, s.E_VAULT_LOCKED)


class CanaryLeakTests(unittest.TestCase):
    """The end-to-end check the F04 acceptance asks for."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'db.sqlite3'
        if db is not None:
            self.conn, _report = db.open_db(self.path)
        else:
            self.conn = sqlite3.connect(self.path, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute('PRAGMA foreign_keys=ON')
            self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)
        self.endpoint_id = 'ep-1'
        if db is not None:
            self.endpoint_id = db.upsert_endpoint(self.conn, 'http://proxy.example.com:8080')
        else:
            self.conn.execute('INSERT INTO endpoints (id, canonical) VALUES (?, ?)',
                              (self.endpoint_id, 'http://proxy.example.com:8080'))
        self.vault = s.MemoryVault()
        self.store = s.AccessStore(self.conn, self.vault, now=lambda: 1000.0)

    def test_nothing_the_module_produces_contains_the_canary(self):
        access = self.store.create(self.endpoint_id, 'http', username='alice', password=CANARY,
                                   access_id='acc-1')
        self.store.create(self.endpoint_id, 'http', username='bob', password=CANARY, access_id='acc-2')
        rotated = self.store.rotate('acc-1', password=CANARY)
        resolved = self.store.resolve('acc-1')
        endpoint = s.parse_endpoint('http://proxy.example.com:8080')

        exported = {
            'describe': json.dumps(s.describe(rotated, state=self.store.state(rotated))),
            'reconcile': json.dumps(self.store.reconcile().as_dict()),
            'error': json.dumps(s.SecretNotProvided('x', action='y').describe()),
            'log_fields': json.dumps(s.log_fields(rotated, state=s.STATE_READY, password=CANARY)),
            'row': json.dumps([dict(row) for row in self.conn.execute('SELECT %s FROM accesses' % s.COLUMNS)]),
        }
        for name, blob in exported.items():
            self.assertNotIn(CANARY, blob, name)

        # The transport form carries the credential, and only the transport form does.
        self.assertIn(CANARY, endpoint.url(resolved))
        self.assertNotIn(CANARY, s.redact_proxy_url(endpoint.url(resolved)))
        self.assertNotIn(CANARY, self.store.verify_password.__doc__ or '')
        resolved.scrub()

    def test_the_canary_is_absent_from_every_file_the_run_produces(self):
        self.store.create(self.endpoint_id, 'http', username='alice', password=CANARY, access_id='acc-1')
        self.store.rotate('acc-1', password=CANARY)
        for path in Path(self.temp.name).iterdir():
            with self.subTest(path=path.name):
                self.assertNotIn(CANARY.encode(), path.read_bytes())
        self.assertEqual(self.store.resolve('acc-1').password, CANARY)

    def test_the_worker_needs_a_reference_and_a_revision_but_not_a_password(self):
        access = self.store.create(self.endpoint_id, 'http', username='alice', password=CANARY,
                                   access_id='acc-1')
        worker_argv = ['proxy-workbench', 'run', '--access', access.id, '--ref', access.secret_ref,
                       '--access-revision', str(access.access_revision)]
        self.assertNotIn(CANARY, ' '.join(worker_argv))
        self.assertIn(access.secret_ref, ' '.join(worker_argv))


if __name__ == '__main__':
    unittest.main()

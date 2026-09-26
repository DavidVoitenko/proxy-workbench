"""A secret-bearing export through /v1: the path that was declared but unreachable.

`POST /v1/exports` accepts `include_secrets`, the route table names the sensitive
option, `openapi.json` advertises it, and the permission `export.secret` is checked
before the request ever reaches the engine.  The engine was then handed the mode
name `'include'`, which is not one of `exportsvc.CREDENTIALS_MODES`, so
`ExportOptions` refused every such request -- and the refusal came back as
`E_SERVICE_UNAVAILABLE` "the operation is not wired to the service layer" (F29
acceptance 4, F28).

What may appear in the artifact is a *reference* to the vault entry, never the
credential itself: the file says where the secret is, not what it is.
"""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import api, apiv1, core, exportsvc
from proxy_workbench import proxytool as engine
from tests.workbench_support import add_candidate, store_result

_NOW = time.time()
#: A documentation address (RFC 5737).  Nothing here can reach a real proxy.
PROXY = 'http://192.0.2.10:8080'
#: A canary credential.  It exists only in this file and must never reach the
#: database, an artifact, a log or an answer body.
CANARY_SECRET = 'CANARY-PROXY-PASSWORD-do-not-leak'


class SecretExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db = engine.open_db(self.home / 'proxies.sqlite3')
        config = json.dumps({'targets': [{'url': 'https://one.invalid/'}]})
        self.db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fx', config))
        row = {'proxy': PROXY, 'reliability': 1, 'min_target_reliability': 1, 'latency_ms': 120,
               'jitter_ms': 5, 'score': 80, 'successes': 3, 'requests': 3,
               'checked_at': _NOW, 'samples': []}
        add_candidate(self.db, PROXY)
        # The export is scoped to the credential-free public identity, so the row
        # is written under it; anything else would be refused for the access, not
        # for the credentials.
        store_result(self.db, ('fx', PROXY, row), access_id='default', access_revision=1,
                     profile_id='fx', checked_at=_NOW, valid_until=_NOW + 900)
        self.db.commit()

        self.manager = api.key_manager(self.home)
        self.service = api.WorkbenchService(self.home,
                                            exports=api.Exports(self.home / 'exports'),
                                            key_store=self.manager)
        self.control = apiv1.ApiV1(self.service, apiv1.ApiKeyStore(self.manager))
        self.counter = 0
        self.admin_id, self.admin = self._bootstrap()

    def tearDown(self):
        self.db.close()

    def _bootstrap(self):
        # The stock bootstrap key is read+admin; an export is a write, so the test
        # bootstraps with the full permission set the product defines.
        issued = self.manager.bootstrap_admin(local_trusted=True,
                                              permissions=sorted(apiv1.PERMISSIONS))
        return issued.key_id, issued.secret

    def _mint(self, name, permissions):
        """A narrow key, created the way an administrator creates one."""
        issued = self.manager.create(actor=self.admin_id, name=name,
                                     permissions=list(permissions))
        return issued.secret

    def call(self, method, path, body=None, key=None, query=''):
        self.counter += 1
        head = {'Host': '127.0.0.1', 'Authorization': f'Bearer {key or self.admin}'}
        if method != 'GET':
            head['Idempotency-Key'] = f'idem-{self.counter}'
            head['Content-Type'] = 'application/json'
        return self.control.handle(apiv1.Request(
            method, path, query=query, headers=head,
            body=json.dumps(body).encode() if body is not None else b''))

    # --- the defect --------------------------------------------------------

    def test_include_secrets_produces_an_artifact_and_not_a_500(self):
        plain = self.call('POST', '/v1/exports',
                          {'collection_id': 'public-base', 'kind': 'published'})
        self.assertEqual(plain.status_code, 202, plain.body)
        secret = self.call('POST', '/v1/exports',
                           {'collection_id': 'public-base', 'kind': 'published',
                            'include_secrets': True})
        self.assertEqual(secret.status_code, 202, secret.body)
        self.assertNotIn(b'E_SERVICE_UNAVAILABLE', secret.body)

    def test_the_mode_the_api_asks_for_is_one_the_engine_knows(self):
        """The literal the API passes must survive `ExportOptions` validation."""
        for wanted, valid in (('include', False), (exportsvc.CREDENTIALS_REFERENCE, True),
                              (exportsvc.CREDENTIALS_REDACT, True)):
            with self.subTest(mode=wanted):
                try:
                    exportsvc.ExportOptions(credentials=wanted)
                    accepted = True
                except exportsvc.ExportError:
                    accepted = False
                self.assertEqual(accepted, valid, f'credentials={wanted!r} accepted={accepted}')
        self.assertNotIn('include', exportsvc.CREDENTIALS_MODES)

    def test_without_the_permission_the_same_request_is_refused(self):
        weak = self._mint('reader', ['export.create'])
        response = self.call('POST', '/v1/exports',
                             {'collection_id': 'public-base', 'include_secrets': True}, key=weak)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error']['code'], 'E_AUTH_PERMISSION')

    def test_a_refusal_reports_its_own_code_and_not_a_broken_feature(self):
        """A domain refusal must not be dressed up as a missing service."""
        error = exportsvc.ExportError('E_EXPORT_CREDENTIALS_NOT_GRANTED', 'no grant')
        self.assertEqual(apiv1._domain_refusal(error).code, 'E_EXPORT_CREDENTIALS_NOT_GRANTED')
        self.assertEqual(apiv1._domain_refusal(error).status, 409)
        self.assertIsNone(apiv1._domain_refusal(RuntimeError('boom')))
        self.assertIsNone(apiv1._domain_refusal(ValueError('no code here')))

    def test_the_artifact_carries_a_reference_and_never_the_value(self):
        report = self.call('POST', '/v1/exports',
                           {'collection_id': 'public-base', 'include_secrets': True}).json()
        directory = Path(report['directory'])
        for name in report['files']:
            body = (directory / name).read_bytes()
            self.assertNotIn(CANARY_SECRET.encode(), body, name)
        ranked = json.loads((directory / 'ranked.json').read_text(encoding='utf-8'))
        self.assertEqual([row['proxy'] for row in ranked], [PROXY])
        # A row measured through the credential-free identity publishes no access
        # reference: a copy of the artifact must not look like a credential list.
        # A row measured through a *named* access gets `access:<id>@<revision>`,
        # never the value and never a direct-auth URI (F28, §5.2).
        self.assertIsNone(ranked[0].get('access_ref'))
        self.assertNotIn('access_id', ranked[0])
        self.assertIsNone(ranked[0].get('direct_auth_uri'))

    def test_a_credential_bearing_row_publishes_a_reference_not_a_value(self):
        row = {'proxy': PROXY, 'reliability': 1, 'min_target_reliability': 1, 'latency_ms': 120,
               'jitter_ms': 5, 'score': 80, 'successes': 3, 'requests': 3,
               'checked_at': _NOW, 'samples': [], 'access_id': 'acc-1', 'access_revision': 2}
        scope = exportsvc.ExportScope(
            identity=core.Scope('public-base', 'fx', 1, 'default'), protocol='all')
        artifact = exportsvc.write_snapshot(
            self.home / 'exports', [dict(row, admission='admitted')], scope=scope,
            options=exportsvc.ExportOptions(credentials=exportsvc.CREDENTIALS_REFERENCE),
            grant=exportsvc.SecretGrant(allowed=True, issued_by='local-cli'))
        ranked = json.loads((artifact.directory / 'ranked.json').read_text(encoding='utf-8'))
        self.assertEqual([item.get('access_ref') for item in ranked], ['access:acc-1@2'])
        self.assertIsNone(ranked[0].get('direct_auth_uri'))
        for name in artifact.files:
            self.assertNotIn(CANARY_SECRET.encode(),
                             (artifact.directory / name).read_bytes(), name)

    def test_the_canary_never_reaches_the_database(self):
        self.call('POST', '/v1/exports',
                  {'collection_id': 'public-base', 'include_secrets': True})
        stored = self.db.execute('SELECT payload FROM results').fetchone()[0]
        self.assertNotIn(CANARY_SECRET, stored)
        for path in (self.home / 'proxies.sqlite3', self.home / 'exports'):
            self.assertNotIn(CANARY_SECRET.encode(), _read(path), str(path))


def _read(path):
    if path.is_file():
        return path.read_bytes()
    if path.is_dir():
        return b''.join(item.read_bytes() for item in path.rglob('*') if item.is_file())
    return b''


if __name__ == '__main__':  # pragma: no cover
    unittest.main()

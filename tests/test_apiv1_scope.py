"""Object-level scope of /v1: a key that names collections must not see anything else.

The acceptance item behind this file is F29: "Key A does not get B through the list,
random, job id, SSE, export/download, batch or counts".  Two shapes of the same
defect are covered here:

1. a *read of one artifact* that never consulted the resource scope at all -- the
   route declared no ``scope=`` and the backstop guard only looked at the top level
   of the answer, while the service returns ``{"item": {...}}``;
2. a *list* whose filter asked ``_scope_values(principal, 'jobs')`` for a kind the
   principal does not have, so the function concluded the key was unrestricted.

Both were reachable with a key scoped to collection ``c1`` and an object belonging
to another collection.
"""
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import apiv1
from proxy_workbench.apiv1 import ApiV1, Request

ADMIN = 'admin-key-value'
SCOPED = 'scoped-key'

MINE = 'col-mine'
THEIRS = 'col-theirs'
#: A documentation address (RFC 5737).  Nothing in this file can reach a real proxy.
FOREIGN_PROXY = 'http://10.55.0.1:8080'
OWN_PROXY = 'http://10.0.0.1:8080'


class Keys(apiv1.KeyStore):
    """Two identities: an administrator and a key bound to one collection."""

    def __init__(self):
        self.audit_log = []

    def verify(self, secret):
        if secret == ADMIN:
            return apiv1.Principal(key_id='k-admin', kind='api_key',
                                   permissions=frozenset(apiv1.PERMISSIONS))
        if secret == SCOPED:
            return apiv1.Principal(
                key_id='k-scoped', kind='api_key', collections=(MINE,),
                permissions=frozenset({'read.results', 'read.results.detail', 'read.status',
                                       'read.export.artifact', 'jobs.read', 'jobs.submit',
                                       'pools.read', 'sources.read', 'profiles.read',
                                       'schedules.read', 'collections.read'}))
        return None

    def list_keys(self, principal, limit=None, **kwargs):
        return {'items': [], 'stream_id': 'keys', 'next_seq': None}

    def get_key(self, principal, key_id):
        return {'id': key_id}

    def read_audit(self, principal, **filters):
        return {'items': self.audit_log, 'stream_id': 'audit', 'next_seq': None}

    def audit(self, record):
        self.audit_log.append(record)


class Service(apiv1.Service):
    """One artifact and one job, both belonging to a collection the scoped key does not name."""

    def __init__(self):
        self.calls = []
        self.running = threading.Event()
        self.artifact = {'id': 'art-theirs', 'kind': 'published', 'collection_id': THEIRS,
                         'profile_id': 'p1', 'state': 'ready', 'generation': 'g-theirs'}
        self.job = {'id': 'job-theirs', 'kind': 'check', 'state': 'queued',
                    'collection_id': THEIRS, 'profile_id': 'unprofiled',
                    'profile_revision': 1, 'input_digest': 'deadbeef'}

    def queue_state(self):
        return {'depth': 0, 'capacity': 8}

    def invoke(self, operation, call):
        self.calls.append((operation, call))
        if operation == 'exports.status':
            return {'item': dict(self.artifact)}
        if operation == 'exports.compatibility':
            return {'item': dict(self.artifact), 'client': 'sing-box 1.11', 'unsupported': []}
        if operation == 'exports.download':
            return {'data': f'{FOREIGN_PROXY}\n'.encode(), 'filename': 'proxies.txt',
                    'content_type': 'text/plain; charset=utf-8',
                    'item': dict(self.artifact)}
        if operation == 'jobs.list':
            return {'items': [dict(self.job)], 'stream_id': 'jobs', 'next_seq': None}
        if operation == 'jobs.get':
            return dict(self.job)
        if operation == 'results.list':
            return {'items': [{'endpoint_id': 'e1', 'proxy': OWN_PROXY, 'collection_id': MINE}],
                    'stream_id': 'generation:g1', 'next_seq': None}
        if operation == 'profiles.list':
            return {'items': [{'id': 'p-theirs', 'name': 'theirs', 'digest': 'd1'}],
                    'stream_id': 'profiles', 'next_seq': None}
        if operation == 'sources.list':
            return {'items': [{'id': 'src-theirs', 'name': 'theirs'}],
                    'stream_id': 'sources', 'next_seq': None}
        if operation == 'schedules.list':
            return {'items': [{'id': 'sch-theirs', 'name': 'theirs', 'collection_id': THEIRS}],
                    'stream_id': 'schedules', 'next_seq': None}
        return {'operation': operation}


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.api = ApiV1(self.service, Keys())
        self.counter = 0

    def call(self, method, path, body=None, key=ADMIN, query='', **headers):
        self.counter += 1
        head = {'Host': '127.0.0.1', 'Authorization': f'Bearer {key}'}
        if method != 'GET':
            head['Idempotency-Key'] = f'idem-{self.counter}'
            head['Content-Type'] = 'application/json'
        head.update(headers)
        return self.api.handle(Request(method, path, query=query, headers=head,
                                       body=json.dumps(body).encode() if body is not None else b''))

    # --- the artifact reads that never asked the scope ---------------------

    def test_a_foreign_export_artifact_is_not_readable_by_a_scoped_key(self):
        for path in ('/v1/exports/art-theirs',
                     '/v1/exports/art-theirs/compatibility',
                     '/v1/exports/art-theirs/download/proxies.txt'):
            with self.subTest(path=path):
                response = self.call('GET', path, key=SCOPED)
                self.assertEqual(response.status_code, 404,
                                 f'{path} answered {response.status_code} for a key scoped to {MINE}')
                self.assertEqual(response.json()['error']['code'], 'E_STATE_NOT_FOUND')
                self.assertNotIn(FOREIGN_PROXY.encode(), response.body)

    def test_the_owning_key_still_reads_its_own_artifact(self):
        self.assertEqual(self.call('GET', '/v1/exports/art-theirs').status_code, 200)
        download = self.call('GET', '/v1/exports/art-theirs/download/proxies.txt')
        self.assertEqual(download.status_code, 200)
        self.assertIn(FOREIGN_PROXY.encode(), download.body)

    def test_a_download_is_redacted_before_it_leaves(self):
        """A redacted identity must not receive raw bytes, secret field or not."""
        self.service.artifact['kind'] = 'subscription'
        raw = self.call('GET', '/v1/exports/art-theirs/download/proxies.txt', key=SCOPED)
        self.assertEqual(raw.status_code, 404)
        self.assertNotIn(b'password', raw.body)
        # A file path is still refused: the manifest, not the URL, decides.
        self.assertEqual(
            self.call('GET', '/v1/exports/art-theirs/download/../../../proxies.sqlite3').status_code,
            404)

    # --- the lists that filtered by a kind the key does not have -----------

    def test_a_scoped_key_does_not_see_a_job_of_another_collection(self):
        response = self.call('GET', '/v1/jobs', key=SCOPED)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['items'], [],
                         'a key scoped to one collection must not receive another collection job')
        self.assertNotIn(THEIRS, response.body.decode())
        # the administrator still sees it
        self.assertEqual([item['id'] for item in
                          self.call('GET', '/v1/jobs').json()['items']], ['job-theirs'])

    def test_a_scoped_key_still_sees_the_jobs_of_its_own_collection(self):
        self.service.job['collection_id'] = MINE
        try:
            items = self.call('GET', '/v1/jobs', key=SCOPED).json()['items']
            self.assertEqual([item['id'] for item in items], ['job-theirs'])
        finally:
            self.service.job['collection_id'] = THEIRS

    def test_a_key_without_a_scope_still_sees_every_collection(self):
        """An unset scope keeps its documented meaning: everything its rights allow."""
        items = self.call('GET', '/v1/jobs').json()['items']
        self.assertEqual([item['id'] for item in items], ['job-theirs'])

    def test_the_list_filters_of_profiles_sources_and_schedules_are_reachable(self):
        """A kind the scope is not expressed in must not read as "unrestricted".

        `_scope_values` returning ``None`` means "this key is unrestricted".  For
        `jobs`, `profiles`, `sources` and `schedules` the principal carries no such
        attribute, so ``None`` used to be returned for a key that *was* restricted --
        and the whole list of another collection came back.
        """
        from proxy_workbench.api import _scope_values
        restricted = apiv1.Principal(key_id='k', kind='api_key', collections=(MINE,),
                                     permissions=frozenset({'read.results'}))
        for kind in ('jobs', 'profiles', 'sources', 'schedules'):
            with self.subTest(kind=kind):
                self.assertIsNotNone(
                    _scope_values(restricted, kind),
                    f'_scope_values({kind!r}) returns None: a scoped key looks unrestricted')
        # A key without a scope keeps its documented meaning for every kind.
        open_key = apiv1.Principal(key_id='k2', kind='api_key',
                                   permissions=frozenset({'read.results'}))
        for kind in ('jobs', 'profiles', 'sources', 'schedules', 'collections', 'pools'):
            with self.subTest(kind=kind, open=True):
                self.assertIsNone(_scope_values(open_key, kind))

    def test_a_scoped_key_gets_no_shared_configuration(self):
        """A source row carries the provider URL verbatim; a collection scope withholds it.

        This is the *service* layer's rule (`api.WorkbenchService._guard_objects`),
        so it is checked against the real service rather than the fake above.
        """
        from proxy_workbench import api as service_layer

        scoped = apiv1.Principal(key_id='k', kind='api_key', collections=(MINE,),
                                 permissions=frozenset({'read.results'}))
        allowed = service_layer._scope_values(scoped, 'sources')
        self.assertIsNotNone(allowed, 'a restricted key must not look unrestricted for sources')
        open_key = apiv1.Principal(key_id='k2', kind='api_key',
                                   permissions=frozenset({'read.results'}))
        self.assertIsNone(service_layer._scope_values(open_key, 'sources'))
        # A source row belongs to no collection, so a restricted key cannot claim it.
        self.assertIsNone(service_layer._collection_of({'id': 'src-1', 'url': 'http://u:p@h/'}))
        self.assertEqual(service_layer._collection_of({'collection_id': THEIRS}), THEIRS)
        self.assertEqual(
            service_layer._collection_of({'scope': {'collection_id': MINE}}), MINE)

    def test_the_backstop_drops_a_foreign_row_from_a_list(self):
        """A list keeps the rows the key may see; only the foreign ones disappear."""
        self.service.foreign_job = dict(self.service.job, id='job-mine', collection_id=MINE)
        original = self.service.invoke

        def invoke(operation, call):
            if operation == 'jobs.list':
                return {'items': [dict(self.service.foreign_job), dict(self.service.job)],
                        'stream_id': 'jobs', 'next_seq': None}
            return original(operation, call)

        self.service.invoke = invoke
        items = self.call('GET', '/v1/jobs', key=SCOPED).json()['items']
        self.assertEqual([item['id'] for item in items], ['job-mine'])

    def test_results_list_keeps_filtering_by_collection(self):
        items = self.call('GET', '/v1/results', key=SCOPED).json()['items']
        self.assertEqual([item['proxy'] for item in items], [OWN_PROXY])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()

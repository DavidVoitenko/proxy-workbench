"""The /v1 operations that promised work and did none: pools and result detail.

Two declared operations, two ways of answering "ok" without doing anything:

* `POST /v1/pools/{id}/refill` read nothing from `call.params['id']`; it queued a
  check of whatever collection the *body* named -- the public base by default --
  and never touched the pool, so a pool created with `--desired 5` stayed 0/5 for
  ever while the caller was told "job queued" (F14, §7.13).
* `GET /v1/results/{id}` compared the path segment with the full `proxy` URL, but
  `ID_PATTERN` cannot carry `://`, and `public_row` did not publish `endpoint_id`
  at all, so detail was always 404 and observations always an empty list (F29,
  mandatory operation "Results: detail/observations").

Everything here runs on documentation addresses (RFC 5737). No socket is opened.
"""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import api, apiv1, db
from proxy_workbench import proxytool as engine
from tests.workbench_support import add_candidate, store_result

_NOW = time.time()
MINE = 'col-mine'
THEIRS = 'col-theirs'
PROXY = 'http://198.51.100.7:8080'
SECOND = 'http://198.51.100.8:8080'


def _row(proxy, latency=120):
    return {'proxy': proxy, 'reliability': 1, 'min_target_reliability': 1, 'latency_ms': latency,
            'jitter_ms': 5, 'score': 80, 'successes': 3, 'requests': 3,
            'checked_at': _NOW, 'samples': []}


class PoolRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db = engine.open_db(self.home / 'proxies.sqlite3')
        config = json.dumps({'targets': [{'url': 'https://one.invalid/'}]})
        self.db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fx', config))
        for name, proxy in (('mine', PROXY), ('mine2', SECOND), ('theirs', 'http://203.0.113.9:3128')):
            collection = MINE if name.startswith('mine') else THEIRS
            if not self.db.execute('SELECT 1 FROM collections WHERE id=?',
                                   (collection,)).fetchone():
                db.create_collection(self.db, collection, kind='private',
                                    collection_id=collection, now=_NOW)
            add_candidate(self.db, proxy, collection_id=collection, origin='manual')
            store_result(self.db, ('fx', proxy, _row(proxy)), profile_id='fx',
                         collection_id=collection, checked_at=_NOW, valid_until=_NOW + 900)
        self.db.commit()
        self.manager = api.key_manager(self.home)
        self.service = api.WorkbenchService(self.home,
                                            exports=api.Exports(self.home / 'exports'),
                                            key_store=self.manager)
        self.control = apiv1.ApiV1(self.service, apiv1.ApiKeyStore(self.manager))
        self.counter = 0
        self.admin = self.manager.bootstrap_admin(
            local_trusted=True, permissions=sorted(apiv1.PERMISSIONS)).secret

    def tearDown(self):
        self.db.close()

    def call(self, method, path, body=None, key=None, query=''):
        self.counter += 1
        head = {'Host': '127.0.0.1', 'Authorization': f'Bearer {key or self.admin}'}
        if method != 'GET':
            head['Idempotency-Key'] = f'idem-{self.counter}'
            head['Content-Type'] = 'application/json'
        return self.control.handle(apiv1.Request(
            method, path, query=query, headers=head,
            body=json.dumps(body).encode() if body is not None else b''))

    def make_pool(self, pool_id='main', collection_id=MINE, desired=2):
        answer = self.call('POST', '/v1/pools',
                           {'name': pool_id, 'desired': desired, 'collection_id': collection_id,
                            'profile_id': 'fx'})
        self.assertEqual(answer.status_code, 200, answer.body)
        return answer.json()['id']

    # --- F14: the refill must touch the pool the path names ----------------

    def test_refill_fills_the_pool_the_path_names(self):
        pool_id = self.make_pool()
        before = self.call('GET', f'/v1/pools/{pool_id}').json()
        self.assertEqual(before.get('served', 0), 0)
        refill = self.call('POST', f'/v1/pools/{pool_id}/refill')
        self.assertEqual(refill.status_code, 200, refill.body)
        body = refill.json()
        self.assertEqual(body['pool_id'], pool_id)
        self.assertEqual(body['collection_id'], MINE)
        answer = self.call('GET', f'/v1/pools/{pool_id}/members')
        self.assertEqual(answer.status_code, 200, answer.body)
        members = answer.json()['items']
        self.assertEqual(sorted(item['endpoint_id'] for item in members),
                         sorted([db.endpoint_id(PROXY), db.endpoint_id(SECOND)]))
        self.assertGreater(body['served'], 0, 'a refill that admits nothing is not a refill')

    def test_refill_ignores_a_collection_in_the_body(self):
        """The body must not decide whose pool is refilled."""
        pool_id = self.make_pool(collection_id=MINE)
        self.call('POST', f'/v1/pools/{pool_id}/refill', {'collection_id': THEIRS})
        members = self.call('GET', f'/v1/pools/{pool_id}/members').json()['items']
        self.assertNotIn(db.endpoint_id('http://203.0.113.9:3128'),
                         [item['endpoint_id'] for item in members])

    def test_start_actually_starts_the_pool(self):
        pool_id = self.make_pool()
        started = self.call('POST', f'/v1/pools/{pool_id}/start')
        self.assertEqual(started.status_code, 200, started.body)
        self.assertGreaterEqual(started.json()['served'], 0)
        self.assertEqual(self.call('GET', f'/v1/pools/{pool_id}/status').json()['state'],
                         started.json()['state'])

    def test_recheck_queues_the_pools_own_collection(self):
        pool_id = self.make_pool(collection_id=MINE)
        answer = self.call('POST', f'/v1/pools/{pool_id}/recheck')
        self.assertEqual(answer.status_code, 202, answer.body)
        body = answer.json()
        self.assertEqual(body['pool_id'], pool_id)
        self.assertEqual(body['collection_id'], MINE)
        self.assertEqual(body['items'], 2)

    def test_the_body_cannot_redirect_a_recheck_to_another_collection(self):
        pool_id = self.make_pool(collection_id=MINE)
        answer = self.call('POST', f'/v1/pools/{pool_id}/recheck', {'collection_id': THEIRS})
        self.assertEqual(answer.status_code, 400)
        self.assertEqual(answer.json()['error']['code'], 'E_VALIDATION_UNKNOWN_FIELD')

    def test_an_unknown_pool_is_a_404_and_not_a_silent_success(self):
        answer = self.call('POST', '/v1/pools/no-such-pool/refill')
        self.assertEqual(answer.status_code, 404)
        self.assertEqual(answer.json()['error']['code'], 'E_STATE_NOT_FOUND')


class ResultDetailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db = engine.open_db(self.home / 'proxies.sqlite3')
        config = json.dumps({'targets': [{'url': 'https://one.invalid/'}]})
        self.db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fx', config))
        db.create_collection(self.db, THEIRS, kind='private', collection_id=THEIRS, now=_NOW)
        endpoint = add_candidate(self.db, 'http://11.0.0.2:8080', collection_id=THEIRS,
                                 origin='manual')
        # `observations.access_id` is a foreign key: a named access has to exist.
        self.db.execute(
            'INSERT OR IGNORE INTO accesses(id, endpoint_id, mode, access_revision, created_at)'
            " VALUES ('default', ?, 'public', 1, ?)", (endpoint, _NOW))
        store_result(self.db, ('fx', 'http://11.0.0.2:8080', _row('http://11.0.0.2:8080')),
                     profile_id='fx', collection_id=THEIRS, checked_at=_NOW, valid_until=_NOW + 900)
        self.db.execute(
            'INSERT INTO observations(id, job_id, endpoint_id, access_id, access_revision,'
            ' profile_id, profile_revision, started_at, finished_at, verdict)'
            " VALUES ('obs-1', 'job-1', ?, 'default', 1, 'fx', 1, ?, ?, '{\"reliability\": 1}')",
            (db.endpoint_id('http://11.0.0.2:8080'), _NOW - 30, _NOW - 25))
        self.db.commit()
        engine.export(self.db, 'fx', self.home / 'exports', collection_id=THEIRS,
                      min_success=1)

        self.manager = api.key_manager(self.home)
        self.service = api.WorkbenchService(self.home,
                                            exports=api.Exports(self.home / 'exports'),
                                            key_store=self.manager)
        self.control = apiv1.ApiV1(self.service, apiv1.ApiKeyStore(self.manager))
        self.counter = 0
        issued = self.manager.bootstrap_admin(local_trusted=True,
                                              permissions=sorted(apiv1.PERMISSIONS))
        self.admin = issued.secret
        self.scoped = self.manager.create(
            actor=issued.key_id, name='mine-only',
            permissions=['read.results', 'read.results.detail'],
            scope={'collections': [MINE]}).secret

    def tearDown(self):
        self.db.close()

    def call(self, method, path, body=None, key=None, query=''):
        self.counter += 1
        head = {'Host': '127.0.0.1', 'Authorization': f'Bearer {key or self.admin}'}
        if method != 'GET':
            head['Idempotency-Key'] = f'idem-{self.counter}'
            head['Content-Type'] = 'application/json'
        return self.control.handle(apiv1.Request(
            method, path, query=query, headers=head,
            body=json.dumps(body).encode() if body is not None else b''))

    def test_the_published_row_names_the_id_the_detail_route_takes(self):
        rows = self.call('GET', '/v1/results').json()['items']
        self.assertEqual([row['proxy'] for row in rows], ['http://11.0.0.2:8080'])
        self.assertEqual(rows[0]['endpoint_id'], db.endpoint_id('http://11.0.0.2:8080'))

    def test_detail_and_observations_answer_for_an_existing_row(self):
        detail = self.call('GET', '/v1/results/11.0.0.2:8080')
        self.assertEqual(detail.status_code, 200, detail.body)
        self.assertEqual(detail.json()['item']['proxy'], 'http://11.0.0.2:8080')
        observations = self.call('GET', '/v1/results/11.0.0.2:8080/observations')
        self.assertEqual(observations.status_code, 200)
        self.assertEqual([item['id'] for item in observations.json()['items']], ['obs-1'])

    def test_the_same_row_is_found_by_its_endpoint_id(self):
        endpoint = db.endpoint_id('http://11.0.0.2:8080')
        self.assertEqual(self.call('GET', f'/v1/results/{endpoint}').status_code, 200)

    def test_an_unknown_id_is_a_404(self):
        answer = self.call('GET', '/v1/results/11.0.0.9:8080')
        self.assertEqual(answer.status_code, 404)
        self.assertEqual(answer.json()['error']['code'], 'E_STATE_NOT_FOUND')

    def test_a_key_scoped_elsewhere_gets_404_not_the_row(self):
        detail = self.call('GET', '/v1/results', key=self.scoped)
        self.assertEqual(detail.json()['items'], [])
        answer = self.call('GET', '/v1/results/11.0.0.2:8080', key=self.scoped)
        self.assertEqual(answer.status_code, 404)
        self.assertEqual(answer.json()['error']['code'], 'E_STATE_NOT_FOUND')
        observations = self.call('GET', '/v1/results/11.0.0.2:8080/observations', key=self.scoped)
        self.assertEqual(observations.status_code, 404)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()

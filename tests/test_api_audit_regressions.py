"""Regressions found by calling every /v1 operation on a real loopback server.

Everything runs on 127.0.0.1 against a temporary data folder whose rows use the
RFC 5737 documentation ranges; nothing is fetched and no proxy is contacted.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import unittest
import uuid

import httpx

from proxy_workbench import api, apikeys, apiv1, exportsvc, proxytool
from tests import web_support as ws

ADDRESSES = ['192.0.2.%d' % i for i in range(1, 9)] + ['198.51.100.%d' % i for i in range(1, 5)]


def rows():
    now = time.time()
    protocols = ('http', 'socks5', 'https', 'socks4')
    countries = ('DE', 'US', 'NL', 'FR')
    return [ws.measurement(f'{protocols[i % 4]}://{address}:{8000 + i}', latency=50 + i * 30,
                           age=30 + i, now=now, country=countries[i % 4])
            for i, address in enumerate(ADDRESSES)]


def build(min_success=2 / 3):
    home = ws.build_data(rows(), publish=False)
    conn = proxytool.open_db(home / 'proxies.sqlite3')
    try:
        proxytool.export(conn, ws.profile_id(), home / 'exports', collection_id='public-base',
                         min_success=min_success, active_profile_path=home / 'last-profile.txt')
    finally:
        conn.close()
    return home


class LiveServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = build()
        manager = api.key_manager(cls.home)
        try:
            issued = manager.bootstrap_admin(local_trusted=True, name='admin',
                                             permissions=sorted(apikeys.PERMISSIONS))
            cls.admin = issued.secret
        finally:
            manager.conn.close()
        cls.server = api.make_api_server(cls.home, '127.0.0.1', 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.client = httpx.Client(base_url=cls.base, trust_env=False, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        shutil.rmtree(cls.home, ignore_errors=True)

    def call(self, method, path, body=None, token=None, params=None, headers=None):
        sent = {'Authorization': 'Bearer ' + (token or self.admin)}
        if method in ('POST', 'PATCH', 'DELETE'):
            sent['Idempotency-Key'] = uuid.uuid4().hex
        sent.update(headers or {})
        return self.client.request(method, path, json=body, params=params, headers=sent)

    def key(self, permissions, **extra):
        response = self.call('POST', '/v1/keys', {'name': 'k-' + uuid.uuid4().hex[:6],
                                                  'permissions': permissions, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['secret']


class ResultsTests(LiveServerCase):
    def proxies(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return [row['proxy'] for row in response.json()['items']]

    def test_following_the_cursor_walks_every_row_once(self):
        seen, cursor = [], None
        for _ in range(20):
            params = {'limit': 5, **({'cursor': cursor} if cursor else {})}
            body = self.call('GET', '/v1/results', params=params).json()
            seen += [row['proxy'] for row in body['items']]
            cursor = body.get('next_cursor')
            if not cursor:
                break
        everything = self.proxies(self.call('GET', '/v1/results'))
        self.assertEqual(seen, everything)
        self.assertEqual(len(set(seen)), len(ADDRESSES))

    def test_declared_filters_are_applied(self):
        fast = self.call('GET', '/v1/results', params={'max_latency_ms': 100}).json()['items']
        self.assertTrue(fast)
        self.assertTrue(all(row['latency_ms'] <= 100 for row in fast))
        self.assertEqual(self.proxies(self.call('GET', '/v1/results',
                                                params={'collection_id': 'nope'})), [])
        self.assertEqual(self.proxies(self.call('GET', '/v1/results',
                                                params={'anonymity': 'elite'})), [])
        self.assertEqual(self.call('GET', '/v1/results',
                                   params={'generation': '.generation-other'}).status_code, 404)

    def test_sort_orders_the_rows(self):
        rows = self.call('GET', '/v1/results', params={'sort': 'speed'}).json()['items']
        latencies = [row['latency_ms'] for row in rows]
        self.assertEqual(latencies, sorted(latencies))

    def test_count_is_honoured_by_random_and_top(self):
        self.assertEqual(len(self.call('GET', '/v1/results/random',
                                       params={'count': 4}).json()['items']), 4)
        self.assertEqual(len(self.call('GET', '/v1/results/top',
                                       params={'count': 2}).json()['items']), 2)

    def test_selection_accepts_host_and_port(self):
        first = self.proxies(self.call('GET', '/v1/results'))[0]
        picked = self.proxies(self.call('GET', '/v1/results/selection',
                                        params={'endpoint_ids': first.partition('://')[2]}))
        self.assertEqual(picked, [first])

    def test_country_filter_does_not_open_a_connection_per_row(self):
        opened = []
        original = api.WorkbenchService.workbench

        def counting(service):
            opened.append(1)
            return original(service)
        with unittest.mock.patch.object(api.WorkbenchService, 'workbench', counting):
            rows = self.call('GET', '/v1/results', params={'country': 'DE'}).json()['items']
        self.assertTrue(rows)
        self.assertLessEqual(len(opened), 1)


class TransportTests(LiveServerCase):
    def test_localhost_host_header_reaches_v1(self):
        response = self.call('GET', '/v1/service', headers={'Host': 'localhost'})
        self.assertEqual(response.status_code, 200, response.text)

    def test_unsupported_method_is_a_documented_405(self):
        response = self.client.put('/v1/keys', headers={'Authorization': 'Bearer ' + self.admin})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()['error']['code'], 'E_VALIDATION_METHOD')

    def test_legacy_reads_refuse_other_methods(self):
        for method in ('POST', 'DELETE', 'PATCH'):
            response = self.client.request(method, '/proxies', content=b'{}')
            self.assertEqual(response.status_code, 405, method)
            self.assertIn('GET', response.headers['Allow'])

    def test_event_streams_carry_frames(self):
        job = self.call('POST', '/v1/checks/collect', {'collection_id': 'public-base'}).json()
        store = api.WorkbenchService(self.home)._job_store()
        try:
            store[0].emit(job['job_id'], 'job.note', data={'n': 1})
            store[1].commit()
        finally:
            store[1].close()
        response = self.call('GET', f'/v1/jobs/{job["job_id"]}/events')
        self.assertEqual(response.status_code, 200)
        self.assertIn('event: job.note', response.text)
        system = self.call('GET', '/v1/events')
        self.assertEqual(system.status_code, 200)
        frames = [json.loads(line[6:]) for line in system.text.splitlines()
                  if line.startswith('data: ')]
        self.assertTrue(frames)
        self.assertNotIn('error', [frame['type'] for frame in frames])
        last_id = [line[4:] for line in system.text.splitlines() if line.startswith('id: ')][-1]
        resumed = self.call('GET', '/v1/events', params={'cursor': last_id})
        self.assertEqual(resumed.status_code, 200, resumed.text)


class ScopeAndKeyTests(LiveServerCase):
    def test_scoped_key_sees_the_members_of_its_own_collection(self):
        own = self.call('POST', '/v1/collections', {'name': 'own'}).json()['id']
        self.call('POST', f'/v1/collections/{own}/imports',
                  {'content': '192.0.2.200:8080', 'allow_private_endpoints': True})
        secret = self.key(['collections.read'], collections=[own])
        members = self.call('GET', f'/v1/collections/{own}/members', token=secret).json()
        self.assertEqual([item['canonical'] for item in members['items']],
                         ['http://192.0.2.200:8080'])

    def test_pool_scope_in_a_body_field_is_enforced(self):
        secret = self.key(['pools.read'], pools=['mine'])
        response = self.call('POST', '/v1/reservations/acquire',
                             {'pool_id': 'someone-else', 'ttl_s': 10}, token=secret)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['error']['code'], 'E_STATE_NOT_FOUND')

    def test_members_of_a_missing_pool_are_not_found(self):
        self.assertEqual(self.call('GET', '/v1/pools/no-such-pool/members').status_code, 404)

    def test_mutations_reach_the_audit_log(self):
        self.call('POST', '/v1/collections', {'name': 'audited'})
        entries = self.call('GET', '/v1/audit', params={'limit': 50}).json()['items']
        self.assertIn('collections.create', [entry['operation'] for entry in entries])

    def test_subscription_revoke_refuses_an_ordinary_key(self):
        created = self.call('POST', '/v1/keys', {'name': 'plain',
                                                  'permissions': ['read.status']}).json()
        response = self.call('POST', f'/v1/subscriptions/{created["id"]}/revoke')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.call('GET', f'/v1/keys/{created["id"]}').json()['state'], 'active')

    def test_half_a_rate_limit_is_refused(self):
        response = self.call('POST', '/v1/keys', {'name': 'half', 'permissions': ['read.status'],
                                                   'rate_limit_requests': 5})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['details']['field'], 'rate_limit_window_seconds')

    def test_profile_revision_is_addressable(self):
        created = self.call('POST', '/v1/profiles', {
            'name': 'rev-' + uuid.uuid4().hex[:6],
            'targets': [{'id': 't1', 'kind': 'required', 'min_success': 1.0}]}).json()
        response = self.call('GET', f'/v1/profiles/{created["id"]}@1')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['revision'], 1)


class ServiceErrorTests(LiveServerCase):
    def test_unknown_job_is_404(self):
        response = self.call('POST', '/v1/jobs/job-missing/cancel')
        self.assertEqual(response.status_code, 404)

    def test_source_refresh_answers_with_a_job(self):
        source = self.call('GET', '/v1/sources', params={'limit': 1}).json()['items'][0]['id']
        response = self.call('POST', f'/v1/sources/{source}/refresh', {})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()['job_id'])
        self.assertEqual(self.call('POST', '/v1/sources/nope/refresh/preview').status_code, 404)

    def test_an_engine_value_error_is_a_400(self):
        response = self.call('POST', '/v1/exports', {'endpoint_ids': 'http://192.0.2.1:8000'})
        self.assertEqual(response.status_code, 400, response.text)


class UnitTests(unittest.TestCase):
    def test_expired_idempotency_key_accepts_a_new_body(self):
        now = [0.0]
        store = apiv1.IdempotencyStore(10, 10, lambda: now[0])
        store.put('b', 'k', 'first', 'answer')
        with self.assertRaises(apiv1.ApiError):
            store.get('b', 'k', 'second')
        now[0] = 11
        self.assertIsNone(store.get('b', 'k', 'second'))

    def test_validation_and_missing_job_statuses(self):
        self.assertEqual(api._error_status('E_VALIDATION_FIELD'), 400)
        self.assertEqual(api._error_status('E_STATE_JOB_NOT_FOUND'), 404)
        self.assertEqual(api._error_status('E_STATE_JOB_TRANSITION'), 409)

    def test_a_snapshot_published_with_min_success_zero_is_readable(self):
        home = build(min_success=0)
        try:
            rows, status = api.Exports(home / 'exports').load()
            self.assertEqual(status['reader_state'], 'ok')
            self.assertTrue(rows)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_audit_adapter_writes_the_row(self):
        written = []

        class Manager:
            def record_audit(self, *args, **kwargs):
                written.append((args, kwargs))
        apiv1.ApiKeyStore(Manager()).audit({'key_id': 'k', 'operation': 'x.y',
                                            'collection_id': 'c', 'result': 'ok'})
        self.assertEqual(written[0][0], ('k', 'x.y'))
        self.assertEqual(written[0][1]['scope'], {'collection_id': 'c'})


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()

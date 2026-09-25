"""Control operations of /v1: collections, sources, profiles, jobs, results, pools,
gateway, schedules, exports and reservations."""
import json
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import apiv1
from proxy_workbench.apiv1 import ApiV1, Request

OPERATOR = 'operator-key-value'
SECRET_EXPORT = 'secret-export-key'


class Keys(apiv1.KeyStore):
    def __init__(self):
        self.audit_log = []

    def verify(self, secret):
        if secret == OPERATOR:
            return apiv1.Principal(key_id='k-op', kind='api_key',
                                   permissions=frozenset(apiv1.PERMISSIONS))
        if secret == SECRET_EXPORT:
            return apiv1.Principal(key_id='k-export', kind='api_key', permissions=frozenset({
                'read.results', 'read.status', 'read.export.artifact', 'export.create'}))
        if secret == 'scoped-key':
            return apiv1.Principal(key_id='k-scoped', kind='api_key', collections=('c1',),
                                   permissions=frozenset({'collections.read', 'collections.write',
                                                         'pools.read', 'jobs.submit'}))
        return None

    def list_keys(self, principal, limit=None, **kwargs):
        return {'items': [], 'stream_id': 'keys', 'next_seq': None}

    def get_key(self, principal, key_id):
        return {'id': key_id}

    def create_key(self, principal, spec):
        return {'id': 'k-new', 'name': spec.get('name')}

    def update_key(self, principal, key_id, patch):
        return {'id': key_id, **patch}

    def rotate_key(self, principal, key_id, grace_s=0.0, **kwargs):
        return {'id': key_id, 'rotation_grace_seconds': grace_s}

    def revoke_key(self, principal, key_id, **kwargs):
        return {'id': key_id, 'revoked': True}

    def disable_key(self, principal, key_id, **kwargs):
        return {'id': key_id, 'state': 'disabled'}

    def enable_key(self, principal, key_id, **kwargs):
        return {'id': key_id, 'state': 'active'}

    def delete_key(self, principal, key_id, **kwargs):
        return {'id': key_id, 'deleted': True}

    def read_audit(self, principal, **filters):
        return {'items': self.audit_log, 'stream_id': 'audit', 'next_seq': None}

    def audit(self, record):
        self.audit_log.append(record)


class Service(apiv1.Service):
    """Records every operation and answers with contract-shaped bodies."""

    def __init__(self):
        self.calls = []
        self.running = threading.Event()
        self.revisions = {'c1': 3, 'p1': 1}
        self.leases = {}

    def queue_state(self):
        return {'depth': 1, 'capacity': 8}

    def invoke(self, operation, call):
        self.calls.append((operation, call))
        if operation.startswith('checks.') or operation in ('pools.refill', 'pools.recheck',
                                                            'jobs.retry', 'exports.create',
                                                            'imports.commit', 'collections.merge',
                                                            'collections.replace',
                                                            'sources.refresh'):
            job = f'job-{len(self.calls)}'
            worker = threading.Thread(target=self._work, daemon=True)
            worker.start()
            return {'job_id': job, 'state': 'queued', 'kind': operation,
                    'scope': {'collection_id': call.body.get('collection_id') or 'c1'}}
        if operation == 'results.list':
            return {'items': [{'endpoint_id': 'e1', 'proxy': 'http://10.0.0.1:8080',
                               'admission_reason': 'time_ok', 'age_seconds': 12}],
                    'stream_id': 'generation:g1', 'next_seq': 2}
        if operation == 'results.random':
            return {'items': [{'endpoint_id': 'e1'}], 'stream_id': 'generation:g1'}
        if operation == 'results.top':
            return {'items': [{'endpoint_id': 'e1'}, {'endpoint_id': 'e2'}]}
        if operation == 'results.selection':
            return {'items': [{'endpoint_id': 'e1'}], 'selection_id': call.query.get('selection_id')}
        if operation == 'exports.download':
            return {'data': b'http://10.0.0.1:8080\n', 'content_type': 'text/plain; charset=utf-8',
                    'filename': 'proxies.txt'}
        if operation == 'reservations.acquire':
            lease = call.body['lease_id'] = f"lease-{len(self.leases) + 1}"
            self.leases[lease] = {'pool_id': call.body['pool_id'], 'ttl_s': call.body['ttl_s']}
            return {'lease_id': lease, 'members': [{'endpoint_id': 'e1'}],
                    'expires_at': 1700000000.0 + call.body['ttl_s']}
        if operation == 'reservations.lease':
            lease = self.leases.get(call.body['lease_id'])
            if lease is None:
                raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404)
            lease['ttl_s'] = call.body['ttl_s']
            return {'lease_id': call.body['lease_id'],
                    'expires_at': 1700000000.0 + call.body['ttl_s']}
        if operation == 'reservations.release':
            self.leases.pop(call.body['lease_id'], None)
            return {'lease_id': call.body['lease_id'], 'state': call.body.get('state', 'returned'),
                    'released': True}
        if operation == 'reservations.feedback':
            return {'recorded': True, 'reputation_updated': False, 'scope': 'pool'}
        if operation.endswith(('.list', '.members', '.observations', '.sessions')):
            return {'items': [{'id': 'x1', 'state': 'ok'}], 'stream_id': operation, 'next_seq': 2}
        if operation == 'gateway.config_get':
            return {'listen_host': '127.0.0.1', 'listen_port': 8890, 'max_per_proxy': 4}
        if operation == 'gateway.bindings':
            return {'items': [{'listener': 'l1', 'pool_id': 'p1', 'generation': 'g1'}],
                    'stream_id': 'bindings'}
        if operation == 'gateway.listeners':
            return {'items': [{'listener': 'l1', 'listen_host': '127.0.0.1', 'port': 8890}]}
        if operation == 'sources.catalog':
            return {'items': [{'id': 'src-1', 'support': 'supported'}, {'id': 'src-2',
                                                                        'support': 'needs_adapter'}]}
        if operation == 'profiles.presets':
            return {'items': [{'id': 'basic', 'version': 2, 'targets': ['https://example.invalid/']}]}
        if operation == 'collections.update' and call.expected_revision is not None:
            if call.expected_revision != self.revisions.get(call.params['id']):
                raise apiv1.ApiError('E_CONFLICT_REVISION', status=409,
                                     details={'expected': call.expected_revision,
                                             'current': self.revisions.get(call.params['id'])})
            self.revisions[call.params['id']] += 1
            return {'id': call.params['id'], 'revision': self.revisions[call.params['id']]}
        if operation == 'imports.preview':
            return {'valid': ['http://10.0.0.1:8080'], 'rejected': [{'line': 2, 'reason': 'scheme'}],
                    'duplicates': [], 'changes_nothing': True}
        if operation == 'exports.compatibility':
            return {'client': 'sing-box 1.11', 'unsupported': [{'proxy': 'https://10.0.0.2:443',
                                                                'reason': 'no https proxy type'}]}
        return {'operation': operation, 'id': call.params.get('id', 'c1'),
                'revision': self.revisions.get(call.params.get('id', 'c1'), 1)}

    def _work(self):
        self.running.set()
        time.sleep(0.4)
        self.running.clear()


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.api = ApiV1(self.service, Keys())
        self.counter = 0

    def call(self, method, path, body=None, key=OPERATOR, idem=None, query='', **headers):
        self.counter += 1
        head = {'Host': '127.0.0.1', 'Authorization': f'Bearer {key}'}
        if method != 'GET':
            head['Idempotency-Key'] = idem or f'idem-{self.counter}'
            head['Content-Type'] = 'application/json'
        head.update(headers)
        return self.api.handle(Request(method, path, query=query, headers=head,
                                       body=json.dumps(body).encode() if body is not None else b''))

    def test_long_operations_answer_202_and_do_not_hold_the_request(self):
        started = time.monotonic()
        response = self.call('POST', '/v1/checks/check',
                             {'collection_id': 'c1', 'want': 5, 'budget': {'max_seconds': 60}})
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 202)
        self.assertEqual(elapsed < 0.3, True, elapsed)
        self.assertTrue(response.json()['job_id'])
        self.assertEqual(dict(response.headers)['Location'],
                         f"/v1/jobs/{response.json()['job_id']}")
        self.assertTrue(self.service.running.is_set())
        # the API stays usable while the job runs
        self.assertEqual(self.call('GET', '/v1/jobs').status_code, 200)
        self.service.running.wait(2)

    def test_one_idempotency_key_starts_one_job(self):
        body = {'collection_id': 'c1', 'want': 5}
        first = self.call('POST', '/v1/checks/check', body, idem='job-once')
        second = self.call('POST', '/v1/checks/check', body, idem='job-once')
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()['job_id'], second.json()['job_id'])
        submits = [name for name, _ in self.service.calls if name == 'checks.check']
        self.assertEqual(len(submits), 1)
        other = self.call('POST', '/v1/checks/check', {'collection_id': 'c1', 'want': 6},
                          idem='job-once')
        self.assertEqual(other.status_code, 409)
        self.assertEqual(other.json()['error']['code'], 'E_CONFLICT_IDEMPOTENCY')

    def test_a_mutation_without_an_idempotency_key_is_refused(self):
        response = self.call('POST', '/v1/checks/check', {'collection_id': 'c1'},
                             **{'Idempotency-Key': ''})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error']['details']['field'], 'Idempotency-Key')

    def test_revision_conflict_is_reported(self):
        missing = self.call('PATCH', '/v1/collections/c1', {'name': 'new'})
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()['error']['details']['field'], 'If-Match')
        stale = self.call('PATCH', '/v1/collections/c1', {'name': 'new'}, **{'If-Match': '1'})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()['error']['code'], 'E_CONFLICT_REVISION')
        good = self.call('PATCH', '/v1/collections/c1', {'name': 'new'}, **{'If-Match': '3'})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json()['revision'], 4)
        mismatch = self.call('PATCH', '/v1/collections/c1', {'name': 'x', 'revision': 2},
                             **{'If-Match': '4'})
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(mismatch.json()['error']['details']['field'], 'revision')

    def test_unknown_parameters_are_never_ignored(self):
        body = self.call('POST', '/v1/collections', {'name': 'mine', 'silent_typo': 1})
        self.assertEqual(body.status_code, 400)
        self.assertEqual(body.json()['error']['code'], 'E_VALIDATION_UNKNOWN_FIELD')
        self.assertEqual(body.json()['error']['details']['fields'], ['silent_typo'])
        query = self.call('GET', '/v1/results', query='limit=5&mystery=1')
        self.assertEqual(query.status_code, 400)
        self.assertEqual(query.json()['error']['code'], 'E_VALIDATION_UNKNOWN_FIELD')
        empty = self.call('POST', '/v1/collections', {})
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()['error']['details']['field'], 'name')
        self.call('POST', '/v1/collections', {'name': 'mine'})

    def test_out_of_range_values_are_refused_with_a_reason(self):
        want = self.call('POST', '/v1/checks/check', {'collection_id': 'c1', 'want': 0})
        self.assertEqual(want.status_code, 400)
        self.assertEqual(want.json()['error']['details']['field'], 'want')
        sort = self.call('GET', '/v1/results', query='sort=whatever')
        self.assertEqual(sort.status_code, 400)
        limit = self.call('GET', '/v1/results', query='limit=100000')
        self.assertEqual(limit.status_code, 400)

    def test_collections_membership_and_import(self):
        self.assertEqual(self.call('GET', '/v1/collections').status_code, 200)
        self.assertEqual(self.call('POST', '/v1/collections', {'name': 'mine'}).status_code, 200)
        self.assertEqual(self.call('GET', '/v1/collections/c1').status_code, 200)
        added = self.call('POST', '/v1/collections/c1/members',
                          {'endpoint': 'http://10.0.0.1:8080'})
        self.assertEqual(added.status_code, 200)
        removed = self.call('DELETE', '/v1/collections/c1/members/e1')
        self.assertEqual(removed.status_code, 200)
        preview = self.call('POST', '/v1/collections/c1/imports/preview',
                            {'format': 'txt', 'content': 'http://10.0.0.1:8080\n'})
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.json()['changes_nothing'])
        commit = self.call('POST', '/v1/collections/c1/imports',
                           {'format': 'txt', 'content': 'http://10.0.0.1:8080\n', 'mode': 'merge'})
        self.assertEqual(commit.status_code, 202)
        merge = self.call('POST', '/v1/collections/c1/merge', {'from_collection_id': 'c2'})
        self.assertEqual(merge.status_code, 202)
        replace_call = self.call('POST', '/v1/collections/c1/replace',
                                 {'format': 'txt', 'content': 'http://10.0.0.1:8080\n'})
        self.assertEqual(replace_call.status_code, 202)
        # preview is not a mutation: it needs no key with write rights
        narrow = self.call('GET', '/v1/collections/c1', key='scoped-key')
        self.assertEqual(narrow.status_code, 200)

    def test_sources_catalog_definitions_and_refresh(self):
        catalog = self.call('GET', '/v1/sources/catalog').json()
        self.assertEqual(catalog['items'][1]['support'], 'needs_adapter')
        self.assertEqual(self.call('GET', '/v1/sources/s1').status_code, 200)
        created = self.call('POST', '/v1/sources',
                            {'url': 'https://example.invalid/list.txt', 'format': 'text',
                             'collection_id': 'c1'})
        self.assertEqual(created.status_code, 200)
        self.assertEqual(self.call('POST', '/v1/sources/s1/enable').status_code, 200)
        self.assertEqual(self.call('POST', '/v1/sources/s1/disable').status_code, 200)
        preview = self.call('POST', '/v1/sources/s1/refresh/preview')
        self.assertEqual(preview.status_code, 200)
        self.assertNotIn('sources.refresh', [name for name, _ in self.service.calls])
        refresh = self.call('POST', '/v1/sources/s1/refresh', {'collection_id': 'c1'})
        self.assertEqual(refresh.status_code, 202)
        status = self.call('GET', '/v1/sources/s1/refresh/job-1')
        self.assertEqual(status.status_code, 200)
        self.service.running.wait(2)

    def test_profiles_presets_versions_clone_archive_and_validation(self):
        self.assertEqual(self.call('GET', '/v1/profiles/presets').json()['items'][0]['id'], 'basic')
        created = self.call('POST', '/v1/profiles',
                            {'name': 'strict', 'targets': ['https://example.invalid/'],
                             'rule': 'all'})
        self.assertEqual(created.status_code, 200)
        self.assertEqual(self.call('GET', '/v1/profiles/p1').status_code, 200)
        version = self.call('POST', '/v1/profiles/p1/versions', {'max_age_seconds': 900})
        self.assertEqual(version.status_code, 200)
        self.assertEqual(self.call('POST', '/v1/profiles/p1/clone', {'name': 'copy'}).status_code, 200)
        self.assertEqual(self.call('POST', '/v1/profiles/p1/archive').status_code, 200)
        self.assertEqual(self.call('POST', '/v1/profiles/p1/validate').status_code, 200)
        empty = self.call('POST', '/v1/profiles', {'name': 'x', 'targets': []})
        self.assertEqual(empty.status_code, 400)

    def test_quick_test_is_a_job_under_the_same_policy(self):
        response = self.call('POST', '/v1/checks/quick-test',
                             {'collection_id': 'c1', 'endpoint': 'http://10.0.0.1:8080',
                              'max_seconds': 30})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()['job_id'])
        operation, call = self.service.calls[-1]
        self.assertEqual(operation, 'checks.quick_test')
        self.assertEqual(call.body['max_seconds'], 30)
        self.service.running.wait(2)

    def test_check_submit_carries_scope_profile_and_budget_to_the_service(self):
        self.call('POST', '/v1/checks/recheck',
                  {'collection_id': 'c1', 'profile_id': 'p-strict', 'profile_revision': 2,
                   'want': 10, 'budget': {'max_requests': 500, 'max_seconds': 120}})
        operation, call = self.service.calls[-1]
        self.assertEqual(operation, 'checks.recheck')
        self.assertEqual(call.body, {'collection_id': 'c1', 'profile_id': 'p-strict',
                                     'profile_revision': 2, 'want': 10,
                                     'budget': {'max_requests': 500, 'max_seconds': 120}})
        self.assertEqual(call.idempotency_key, 'idem-1')
        self.assertEqual(call.principal.key_id, 'k-op')
        self.assertEqual(call.deadline_s, 60.0)
        self.service.running.wait(2)

    def test_jobs_lifecycle_is_exposed_without_widening_scope(self):
        self.assertEqual(self.call('GET', '/v1/jobs').status_code, 200)
        self.assertEqual(self.call('GET', '/v1/jobs/j1').status_code, 200)
        self.assertEqual(self.call('POST', '/v1/jobs/j1/pause').status_code, 200)
        self.assertEqual(self.call('POST', '/v1/jobs/j1/resume').status_code, 200)
        self.assertEqual(self.call('POST', '/v1/jobs/j1/cancel').status_code, 200)
        retry = self.call('POST', '/v1/jobs/j1/retry')
        self.assertEqual(retry.status_code, 202)
        self.service.running.wait(2)

    def test_results_filters_sort_and_cursor(self):
        page = self.call('GET', '/v1/results',
                         query='collection_id=c1&sort=quality&max_age_seconds=900'
                               '&include_unknown=true&include_stale=1&limit=1')
        self.assertEqual(page.status_code, 200)
        body = page.json()
        self.assertEqual(body['items'][0]['admission_reason'], 'time_ok')
        self.assertIsNotNone(body['next_cursor'])
        operation, call = self.service.calls[-1]
        self.assertEqual(call.query, {'collection_id': 'c1', 'sort': 'quality',
                                      'max_age_seconds': 900, 'include_unknown': True,
                                      'include_stale': True, 'limit': 1})
        page_query = ('collection_id=c1&sort=quality&max_age_seconds=900&include_unknown=true'
                      '&include_stale=1&limit=1')
        again = self.call('GET', '/v1/results',
                          query=f'cursor={body["next_cursor"]}&{page_query}')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.service.calls[-1][1].query['cursor_seq'], 2)
        # a cursor is bound to its filter set, so a different filter is a conflict
        changed = self.call('GET', '/v1/results', query=f'cursor={body["next_cursor"]}&sort=speed')
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json()['error']['code'], 'E_CONFLICT_REVISION')
        foreign = apiv1.encode_cursor('generation:other', 5)
        refused = self.call('GET', '/v1/results', query=f'cursor={foreign}')
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()['error']['details']['field'], 'cursor')
        broken = self.call('GET', '/v1/results', query='cursor=not-a-cursor')
        self.assertEqual(broken.status_code, 400)

    def test_results_detail_observations_random_top_and_selection(self):
        detail = self.call('GET', '/v1/results/e1')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(self.call('GET', '/v1/results/e1/observations').status_code, 200)
        self.assertEqual(self.call('GET', '/v1/results/random', query='count=1').status_code, 200)
        self.assertEqual(self.call('GET', '/v1/results/top', query='count=2').status_code, 200)
        selection = self.call('GET', '/v1/results/selection', query='selection_id=s1')
        self.assertEqual(selection.json()['selection_id'], 's1')
        # a static path is never read as an identifier
        self.assertEqual(self.service.calls[-1][1].params.get('id'), None)

    def test_pools_target_reserve_policy_and_members(self):
        created = self.call('POST', '/v1/pools', {'name': 'main', 'desired': 5, 'reserve': 2,
                                                  'collection_id': 'c1'})
        self.assertEqual(created.status_code, 200)
        self.assertEqual(self.call('GET', '/v1/pools').status_code, 200)
        self.assertEqual(self.call('GET', '/v1/pools/p1').status_code, 200)
        updated = self.call('PATCH', '/v1/pools/p1', {'desired': 7}, **{'If-Match': '1'})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self.call('POST', '/v1/pools/p1/start').status_code, 200)
        self.assertEqual(self.call('POST', '/v1/pools/p1/pause').status_code, 200)
        refill = self.call('POST', '/v1/pools/p1/refill', {'budget': {'max_requests': 100}})
        self.assertEqual(refill.status_code, 202)
        self.assertEqual(self.call('POST', '/v1/pools/p1/recheck').status_code, 202)
        self.assertEqual(self.call('GET', '/v1/pools/p1/members').status_code, 200)
        self.assertEqual(self.call('GET', '/v1/pools/p1/status').status_code, 200)
        self.service.running.wait(2)

    def test_gateway_config_cannot_expose_a_network_by_accident(self):
        self.assertEqual(self.call('GET', '/v1/gateway/config').json()['listen_host'], '127.0.0.1')
        self.assertEqual(self.call('GET', '/v1/gateway/bindings').status_code, 200)
        self.assertEqual(self.call('GET', '/v1/gateway/listeners').status_code, 200)
        self.assertEqual(self.call('GET', '/v1/gateway/sessions').status_code, 200)
        bound = self.call('POST', '/v1/gateway/bindings',
                          {'listener': 'l1', 'pool_id': 'p1', 'revision': 1})
        self.assertEqual(bound.status_code, 200)
        refused = self.call('PATCH', '/v1/gateway/config', {'listen_host': '0.0.0.0',
                                                             'revision': 1})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()['error']['details']['field'], 'listen_host')
        allowed = self.call('PATCH', '/v1/gateway/config', {'listen_host': '10.0.0.7',
                                                            'revision': 1})
        self.assertEqual(allowed.status_code, 400)
        api = ApiV1(self.service, Keys(),
                    apiv1.ApiConfig(allowed_bind_hosts=('10.0.0.7',)))
        response = api.handle(Request('PATCH', '/v1/gateway/config',
                                      headers={'Host': '127.0.0.1',
                                               'Authorization': f'Bearer {OPERATOR}',
                                               'Idempotency-Key': 'g1', 'If-Match': '1',
                                               'Content-Type': 'application/json'},
                                      body=json.dumps({'listen_host': '10.0.0.7'}).encode()))
        self.assertEqual(response.status_code, 200)
        extra = self.call('PATCH', '/v1/gateway/config', {'expose': 'all', 'revision': 1})
        self.assertEqual(extra.status_code, 400)

    def test_schedules_crud_counters_and_next_run(self):
        created = self.call('POST', '/v1/schedules', {'name': 'nightly', 'kind': 'refill',
                                                      'interval_minutes': 30, 'pool_id': 'p1'})
        self.assertEqual(created.status_code, 200)
        self.assertEqual(self.call('GET', '/v1/schedules').status_code, 200)
        self.assertEqual(self.call('GET', '/v1/schedules/s1').status_code, 200)
        self.assertEqual(self.call('PATCH', '/v1/schedules/s1', {'interval_minutes': 15},
                                   **{'If-Match': '1'}).status_code, 200)
        self.assertEqual(self.call('POST', '/v1/schedules/s1/enable').status_code, 200)
        self.assertEqual(self.call('POST', '/v1/schedules/s1/disable').status_code, 200)
        self.assertEqual(self.call('GET', '/v1/schedules/s1/next-run').status_code, 200)
        self.assertEqual(self.call('GET', '/v1/schedules/s1/counters').status_code, 200)
        self.assertEqual(self.call('DELETE', '/v1/schedules/s1').status_code, 200)
        bad = self.call('POST', '/v1/schedules', {'name': 'x', 'kind': 'refill',
                                                  'interval_minutes': 0})
        self.assertEqual(bad.status_code, 400)

    def test_exports_artifact_status_compatibility_and_protected_download(self):
        created = self.call('POST', '/v1/exports', {'collection_id': 'c1', 'kind': 'selection',
                                                    'format': 'txt', 'endpoint_ids': 'e1'})
        self.assertEqual(created.status_code, 202)
        self.service.running.wait(2)
        self.assertEqual(self.call('GET', '/v1/exports/a1').status_code, 200)
        report = self.call('GET', '/v1/exports/a1/compatibility').json()
        self.assertEqual(report['unsupported'][0]['reason'], 'no https proxy type')
        download = self.call('GET', '/v1/exports/a1/download/proxies.txt')
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.body, b'http://10.0.0.1:8080\n')
        self.assertIn('attachment; filename="proxies.txt"', dict(download.headers)['Content-Disposition'])
        no_secret = self.call('GET', '/v1/exports/a1/download/proxies.txt', key=SECRET_EXPORT)
        self.assertEqual(no_secret.status_code, 200)
        with_secrets = self.call('GET', '/v1/exports/a1/download/proxies.txt',
                                 query='include_secrets=true', key=SECRET_EXPORT)
        self.assertEqual(with_secrets.status_code, 403)
        self.assertEqual(with_secrets.json()['error']['details']['required'], 'export.secret')
        # the sensitive option needs the right even when the artifact is created
        refused = self.call('POST', '/v1/exports', {'collection_id': 'c1', 'include_secrets': True},
                            key=SECRET_EXPORT)
        self.assertEqual(refused.status_code, 403)
        self.service.running.wait(2)

    def test_reservations_lease_ttl_and_bounded_feedback(self):
        acquire = self.call('POST', '/v1/reservations/acquire',
                            {'pool_id': 'p1', 'count': 2, 'ttl_s': 60})
        self.assertEqual(acquire.status_code, 200)
        self.assertEqual(acquire.json()['expires_at'], 1700000060.0)
        lease = self.call('POST', '/v1/reservations/lease',
                          {'lease_id': acquire.json()['lease_id'], 'pool_id': 'p1', 'ttl_s': 120})
        self.assertEqual(lease.json()['expires_at'], 1700000120.0)
        release = self.call('POST', '/v1/reservations/release',
                            {'lease_id': acquire.json()['lease_id'], 'pool_id': 'p1',
                             'state': 'returned'})
        self.assertEqual(release.json()['state'], 'returned')
        too_long = self.call('POST', '/v1/reservations/acquire', {'pool_id': 'p1', 'ttl_s': 999999})
        self.assertEqual(too_long.status_code, 400)
        feedback = self.call('POST', '/v1/reservations/feedback',
                             {'pool_id': 'p1', 'endpoint_id': 'e1', 'ok': False,
                              'target_id': 't1', 'error_code': 'HTTP_403'})
        self.assertEqual(feedback.status_code, 200)
        self.assertEqual(feedback.json()['reputation_updated'], False)
        wrong = self.call('POST', '/v1/reservations/feedback',
                          {'pool_id': 'p1', 'endpoint_id': 'e1', 'ok': True, 'listed': True})
        self.assertEqual(wrong.status_code, 400)

    def test_a_scoped_key_cannot_reach_another_collection(self):
        inside = self.call('GET', '/v1/collections/c1', key='scoped-key')
        self.assertEqual(inside.status_code, 200)
        outside = self.call('GET', '/v1/collections/c2', key='scoped-key')
        self.assertEqual(outside.status_code, 404)
        self.assertNotIn('c2', json.dumps(outside.json()))
        members = self.call('GET', '/v1/collections/c9/members', key='scoped-key')
        self.assertEqual(members.status_code, 404)


if __name__ == '__main__':
    unittest.main()

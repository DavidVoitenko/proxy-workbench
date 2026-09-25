"""Keys, permissions, resource scopes and the narrow identities of /v1."""
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
import secrets
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import apiv1
from proxy_workbench.apiv1 import ApiV1, Request

CANARY = 'CANARY-SECRET-7f3a1c-do-not-leak'
ADMIN_PERMISSIONS = sorted(apiv1.PERMISSIONS)


class MemoryKeys(apiv1.KeyStore):
    """A key store in the shape apikeys must have: one-way verifiers, one-time secrets."""

    def __init__(self):
        self.records = {}
        self.secrets = {}
        self.audit_log = []
        self.counter = 0

    def _digest(self, secret):
        return hashlib.sha256(secret.encode('utf-8')).hexdigest()

    def _add(self, principal, secret):
        self.records[principal.key_id] = principal
        self.secrets[self._digest(secret)] = principal
        return principal

    def bootstrap_admin(self, secret=CANARY, **kwargs):
        return self._add(apiv1.Principal(key_id='k-admin', kind='api_key', name='local bootstrap',
                                         permissions=frozenset(apiv1.PERMISSIONS)), secret)

    def verify(self, secret):
        return self.secrets.get(self._digest(secret))

    def list_keys(self, principal, **kwargs):
        return {'items': [self.public(record) for record in self.records.values()],
                'stream_id': 'keys'}

    def create_key(self, principal, spec):
        self.counter += 1
        secret = 'wbk_' + secrets.token_urlsafe(24)
        rate_limit = ((spec['rate_limit_requests'], spec['rate_limit_window_seconds'])
                      if spec.get('rate_limit_requests') else None)
        granted = frozenset(spec.get('permissions') or ())
        # a key that holds nothing but the read rights is a narrow subscription
        kind = ('subscription' if granted and granted <= apiv1.SUBSCRIPTION_PERMISSIONS
                else 'api_key')
        record = apiv1.Principal(
            key_id=f'k-{self.counter:03d}', kind=kind,
            name=spec.get('name'), permissions=granted,
            purpose=spec.get('purpose'),
            collections=tuple(spec.get('collections') or ()), pools=tuple(spec.get('pools') or ()),
            expires_at=spec.get('expires_at'), rate_limit=rate_limit,
            concurrency=spec.get('concurrency'))
        self._add(record, secret)
        return dict(self.public(record), secret=secret)

    def get_key(self, principal, key_id):
        return self.public(self.records[key_id])

    def rotate_key(self, principal, key_id, grace_s=0.0, **kwargs):
        secret = 'wbk_' + secrets.token_urlsafe(24)
        record = self.records[key_id]
        for digest, known in list(self.secrets.items()):
            if known.key_id == key_id:
                del self.secrets[digest]
        self.secrets[self._digest(secret)] = record
        return dict(self.public(record), secret=secret)

    def revoke_key(self, principal, key_id, **kwargs):
        record = replace(self.records[key_id], revoked_at=1700000000.0)
        self.records[key_id] = record
        for digest, known in list(self.secrets.items()):
            if known.key_id == key_id:
                self.secrets[digest] = record
        return self.public(record)

    def update_key(self, principal, key_id, patch):
        record = self.records[key_id]
        self.records[key_id] = replace(
            record,
            permissions=frozenset(patch.get('permissions', record.permissions)),
            name=patch.get('name', record.name),
            collections=tuple(patch.get('collections', record.collections)),
            pools=tuple(patch.get('pools', record.pools)))
        return self.public(self.records[key_id])

    def disable_key(self, principal, key_id, **kwargs):
        for digest, known in list(self.secrets.items()):
            if known.key_id == key_id:
                del self.secrets[digest]
        return self.public(self.records[key_id])

    def enable_key(self, principal, key_id, **kwargs):
        return self.public(self.records[key_id])

    def delete_key(self, principal, key_id, **kwargs):
        self.records.pop(key_id, None)
        return {'id': key_id, 'deleted': True}

    def read_audit(self, principal, **filters):
        return {'items': self.audit_log, 'stream_id': 'audit', 'next_seq': None}

    def audit(self, record):
        self.audit_log.append(record)

    def public(self, principal):
        return {'id': principal.key_id, 'kind': principal.kind, 'name': principal.name,
                'purpose': principal.purpose,
                'permissions': sorted(principal.permissions),
                'collections': list(principal.collections), 'pools': list(principal.pools),
                'expires_at': principal.expires_at, 'revoked_at': principal.revoked_at}


class FakeService(apiv1.Service):
    """Just enough domain work to show that the API layer does not invent any."""

    def __init__(self, keys):
        self.keys = keys
        self.calls = []
        self.rows = [{'endpoint_id': 'e1', 'proxy': 'http://10.0.0.1:8080'}]

    def queue_state(self):
        return {'depth': 0, 'capacity': 5}

    def invoke(self, operation, call):
        self.calls.append(operation)
        if operation == 'keys.list':
            return {'items': [self.keys.public(r) for r in self.keys.records.values()],
                    'stream_id': 'keys'}
        if operation == 'keys.get':
            return self.keys.public(self.keys.records[call.params['id']])
        if operation == 'keys.rotate':
            return self.keys.rotate_key(call.principal, call.params['id'])
        if operation == 'keys.revoke':
            return self.keys.revoke_key(call.principal, call.params['id'])
        if operation == 'keys.update':
            return self.keys.update_key(call.principal, call.params['id'], call.body)
        if operation == 'keys.create':
            return self.keys.create_key(call.principal, call.body)
        if operation == 'collections.get':
            if call.params['id'] not in self.keys.records and call.params['id'] != 'c1':
                raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404)
            return {'id': call.params['id'], 'name': 'mine'}
        if operation == 'results.list':
            return {'items': self.rows, 'stream_id': 'generation:g1', 'next_seq': 2}
        if operation == 'reservations.release':
            return {'lease_id': call.body['lease_id'], 'state': call.body.get('state', 'returned')}
        return {'operation': operation}


class KeyManagerTests(unittest.TestCase):
    def setUp(self):
        self.keys = MemoryKeys()
        self.keys.bootstrap_admin()
        self.service = FakeService(self.keys)
        self.api = ApiV1(self.service, self.keys)
        self.counter = 0

    def post(self, path, body=None, key=CANARY, idem=None, **headers):
        self.counter += 1
        return self.api.handle(Request('POST', path,
                                       headers={'Host': '127.0.0.1',
                                                'Authorization': f'Bearer {key}',
                                                'Idempotency-Key': idem or f'idem-{self.counter}',
                                                'Content-Type': 'application/json', **headers},
                                       body=json.dumps({} if body is None else body).encode()))

    def get(self, path, key=CANARY, **headers):
        return self.api.handle(Request('GET', path,
                                       headers={'Host': '127.0.0.1',
                                                'Authorization': f'Bearer {key}', **headers}))

    def test_a_key_is_required_and_the_secret_is_shown_once(self):
        anonymous = self.api.handle(Request('GET', '/v1/keys', headers={'Host': '127.0.0.1'}))
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(anonymous.json()['error']['code'], 'E_AUTH_MISSING')
        wrong = self.get('/v1/keys', key='wbk_not-a-real-secret')
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json()['error']['code'], 'E_AUTH_INVALID')

        created = self.post('/v1/keys', {'name': 'reader', 'permissions': ['read.results']})
        self.assertEqual(created.status_code, 200)
        key_id, secret = created.json()['id'], created.json()['secret']
        self.assertTrue(secret.startswith('wbk_'))
        listed = self.get('/v1/keys').json()['items']
        self.assertIn(key_id, [item['id'] for item in listed])
        self.assertNotIn(secret, json.dumps(listed))
        self.assertNotIn(secret, json.dumps(self.get(f'/v1/keys/{key_id}').json()))

    def test_created_key_works_and_cannot_administer_itself(self):
        secret = self.post('/v1/keys', {'name': 'reader', 'permissions': ['read.results']}).json()['secret']
        self.assertEqual(self.get('/v1/results', key=secret).status_code, 200)
        refused = self.post('/v1/keys', {'name': 'self', 'permissions': ADMIN_PERMISSIONS}, key=secret)
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json()['error']['code'], 'E_AUTH_PERMISSION')
        self.assertEqual(self.get('/v1/audit', key=secret).status_code, 403)

    def test_rotate_and_revoke_take_effect_immediately(self):
        key_id = self.post('/v1/keys', {'name': 'temp', 'permissions': ['read.results']}).json()['id']
        secret = self.post('/v1/keys', {'name': 'temp2', 'permissions': ['read.results']}).json()['secret']
        rotated = self.post(f'/v1/keys/{key_id}/rotate')
        self.assertEqual(rotated.status_code, 200)
        self.assertNotEqual(rotated.json()['secret'], secret)
        self.assertEqual(self.get('/v1/results', key=rotated.json()['secret']).status_code, 200)
        self.assertEqual(self.post(f'/v1/keys/{key_id}/revoke').status_code, 200)
        revoked = self.get('/v1/keys', key=rotated.json()['secret'])
        self.assertEqual(revoked.status_code, 401)
        self.assertEqual(revoked.json()['error']['code'], 'E_AUTH_REVOKED')

    def test_metadata_update_needs_the_admin_permission(self):
        key_id = self.post('/v1/keys', {'name': 'temp', 'permissions': ['read.results']}).json()['id']
        response = self.api.handle(Request(
            'PATCH', f'/v1/keys/{key_id}',
            headers={'Host': '127.0.0.1', 'Authorization': f'Bearer {CANARY}',
                     'Idempotency-Key': 'meta-1', 'If-Match': '1', 'Content-Type': 'application/json'},
            body=json.dumps({'name': 'renamed'}).encode()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['name'], 'renamed')
        self.assertEqual(self.get(f'/v1/keys/{key_id}').json()['name'], 'renamed')

    def test_expired_key_stops_working(self):
        self.keys._add(apiv1.Principal(key_id='k-old', permissions=frozenset({'read.results'}),
                                       expires_at=1.0), 'expired-secret')
        response = self.get('/v1/results', key='expired-secret')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'E_AUTH_EXPIRED')

    def test_resource_scope_hides_an_object_it_may_not_see(self):
        secret = self.post('/v1/keys', {'name': 'narrow', 'permissions': ['collections.read'],
                                         'collections': ['c1']}).json()['secret']
        self.assertEqual(self.get('/v1/collections/c1', key=secret).status_code, 200)
        outside = self.get('/v1/collections/c2', key=secret)
        missing = self.get('/v1/collections/nope', key=secret)
        self.assertEqual(outside.status_code, 404)
        self.assertEqual(outside.json()['error']['code'], 'E_STATE_NOT_FOUND')
        # the same code as a missing object, so the key learns nothing about c2
        self.assertEqual(outside.json()['error']['code'], missing.json()['error']['code'])
        self.assertNotIn('c2', json.dumps(outside.json()))

    def test_subscription_secret_is_read_only_narrow_and_redacted(self):
        created = self.post('/v1/subscriptions', {'name': 'phone', 'collection_id': 'c1'})
        self.assertEqual(created.status_code, 200)
        body = created.json()
        secret = body['secret']
        self.assertEqual(sorted(body['permissions']), sorted(apiv1.SUBSCRIPTION_PERMISSIONS))
        self.assertNotIn('admin.keys', body['permissions'])
        self.assertEqual(self.get('/v1/results', key=secret).status_code, 200)
        for path, payload in (('/v1/keys', {'name': 'x', 'permissions': ADMIN_PERMISSIONS}),
                              ('/v1/collections', {'name': 'x'}),
                              ('/v1/reservations/release', {'lease_id': 'l1', 'pool_id': 'p1'})):
            response = self.post(path, payload, key=secret)
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.json()['error']['details']['kind'], 'subscription')

    def test_subscription_answers_are_redacted(self):
        secret = self.post('/v1/subscriptions', {'name': 'phone'}).json()['secret']
        self.service.rows = [{'endpoint_id': 'e1', 'proxy': 'http://10.0.0.1:8080',
                              'access_id': 'a1', 'password': CANARY, 'nested': {'secret': CANARY}}]
        narrow = json.dumps(self.get('/v1/results', key=secret).json())
        self.assertNotIn(CANARY, narrow)
        self.assertNotIn('password', narrow)
        self.assertNotIn('secret', narrow)
        self.assertIn('endpoint_id', narrow)
        self.assertIn(CANARY, json.dumps(self.get('/v1/results', key=CANARY).json()))

    def test_legacy_query_token_is_a_deprecated_read_path(self):
        self.keys._add(apiv1.Principal(key_id='k-legacy', kind='legacy',
                                       permissions=frozenset(apiv1.LEGACY_READ_PERMISSIONS)),
                       'legacy-token-value')
        response = self.api.handle(Request('GET', '/v1/results',
                                           query='token=legacy-token-value&limit=5',
                                           headers={'Host': '127.0.0.1'}))
        self.assertEqual(response.status_code, 200)
        headers = dict(response.headers)
        self.assertEqual(headers['Deprecation'], 'true')
        self.assertIn('deprecated', headers['Warning'])
        self.assertIsNotNone(response.json()['next_cursor'])
        self.assertEqual(self.api.legacy_deprecations, 1)
        # the same token buys no admin and no private scope
        self.assertEqual(self.api.handle(Request('GET', '/v1/keys', query='token=legacy-token-value',
                                                 headers={'Host': '127.0.0.1'})).status_code, 401)
        denied = self.post('/v1/keys', {'name': 'x', 'permissions': ADMIN_PERMISSIONS},
                           key='legacy-token-value')
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()['error']['details']['kind'], 'legacy')

    def test_control_keys_never_travel_in_the_url(self):
        for path in ('/v1/keys', '/v1/collections', '/v1/pools', '/v1/jobs', '/v1/audit'):
            response = self.api.handle(Request('GET', path, query=f'token={CANARY}',
                                               headers={'Host': '127.0.0.1'}))
            self.assertEqual(response.status_code, 401, path)
        self.assertEqual(self.api.handle(Request('GET', '/v1/results', query=f'token={CANARY}',
                                                 headers={'Host': '127.0.0.1'})).status_code, 200)

    def test_canary_never_reaches_the_store_or_the_audit_log(self):
        self.post('/v1/keys', {'name': 'reader', 'permissions': ['read.results']})
        self.post('/v1/subscriptions', {'name': 'phone'})
        self.assertNotIn(CANARY, self.keys.secrets)
        self.assertIn(hashlib.sha256(CANARY.encode()).hexdigest(), self.keys.secrets)
        dumped = json.dumps({'records': {k: asdict(v) for k, v in self.keys.records.items()},
                             'audit': self.keys.audit_log}, default=str)
        self.assertNotIn(CANARY, dumped)
        self.assertTrue(self.keys.audit_log)
        for record in self.keys.audit_log:
            self.assertEqual(set(record), {'at', 'key_id', 'operation', 'object_kind', 'object_id',
                                           'collection_id', 'pool_id', 'result', 'error_code'})
            self.assertNotIn(CANARY, json.dumps(record, default=str))
        denied = self.post('/v1/keys', {'name': 'x', 'permissions': ADMIN_PERMISSIONS},
                           key='wbk_unknown')
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(self.keys.audit_log[-1]['error_code'], 'E_AUTH_INVALID')

    def test_audit_is_readable_only_by_an_admin_key(self):
        self.post('/v1/keys', {'name': 'reader', 'permissions': ['read.results']})
        log = self.get('/v1/audit').json()['items']
        self.assertEqual(log[-1]['operation'], 'keys.create')
        self.assertEqual(log[-1]['result'], 'ok')
        self.assertEqual(log[-1]['key_id'], 'k-admin')
        self.assertEqual(self.get('/v1/audit', key='wbk_nope').status_code, 401)


if __name__ == '__main__':
    unittest.main()

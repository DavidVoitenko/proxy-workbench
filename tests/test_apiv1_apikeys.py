"""The seam between /v1 and the real key manager: apikeys.ApiKeyManager behind the
KeyStore protocol of this module, on a temporary database and a fake clock."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import apikeys, apiv1
from proxy_workbench.apiv1 import ApiV1, Request

CANARY = 'CANARY-ADMIN-4b7d2e-do-not-leak'


class Service(apiv1.Service):
    def __init__(self):
        self.calls = []

    def queue_state(self):
        return {'depth': 0, 'capacity': 4}

    def invoke(self, operation, call):
        self.calls.append(operation)
        if operation == 'results.list':
            return {'items': [{'endpoint_id': 'e1', 'proxy': 'http://10.0.0.1:8080'}],
                    'stream_id': 'generation:g1', 'next_seq': None}
        if operation == 'collections.get':
            return {'id': call.params['id']}
        return {'operation': operation}


class ApiKeysIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now = [1700000000.0]
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        apikeys.ensure_schema(self.conn)
        self.manager = apikeys.ApiKeyManager(self.conn, now=lambda: self.now[0])
        self.admin = self.manager.bootstrap_admin(local_trusted=True, name='bootstrap')
        self.api = ApiV1(Service(), apiv1.ApiKeyStore(self.manager),
                         clock=lambda: self.now[0])
        self.counter = 0

    def tearDown(self):
        self.conn.close()

    def post(self, path, body=None, key=None, idem=None, **headers):
        self.counter += 1
        head = {'Host': '127.0.0.1',
                'Authorization': f'Bearer {key or self.admin.secret}',
                'Idempotency-Key': idem or f'i-{self.counter}',
                'Content-Type': 'application/json', **headers}
        return self.api.handle(Request('POST', path, headers=head,
                                       body=json.dumps({} if body is None else body).encode()))

    def get(self, path, key=None, **headers):
        return self.api.handle(Request('GET', path,
                                       headers={'Host': '127.0.0.1',
                                                'Authorization': f'Bearer {key or self.admin.secret}',
                                                **headers}))

    def test_the_bootstrap_admin_key_works_over_the_api(self):
        self.assertEqual(self.get('/v1/service').status_code, 200)
        self.assertEqual(self.get('/v1/service', key='pwk_wrong').status_code, 401)
        self.assertEqual(self.get('/v1/service', key='pwk_wrong').json()['error']['code'],
                         'E_AUTH_INVALID')
        listed = self.get('/v1/keys').json()['items']
        self.assertEqual([item['id'] for item in listed], [self.admin.key_id])
        self.assertEqual(listed[0]['name'], 'bootstrap')
        self.assertNotIn('secret', json.dumps(listed))

    def test_key_lifecycle_over_the_api(self):
        created = self.post('/v1/keys', {'name': 'reader', 'purpose': 'app',
                                         'permissions': ['read.results', 'collections.read'],
                                         'collections': ['c1']})
        self.assertEqual(created.status_code, 200)
        body = created.json()
        secret, key_id = body['secret'], body['id']
        self.assertTrue(secret.startswith(apikeys.SECRET_MARK))
        self.assertEqual(body['permissions'], ['collections.read', 'read.results'])
        self.assertEqual(body['scope'], {'collections': ['c1'], 'pools': []})
        # the reader reads, and it does not administer
        self.assertEqual(self.get('/v1/results', key=secret).status_code, 200)
        self.assertEqual(self.get('/v1/collections/c1', key=secret).status_code, 200)
        self.assertEqual(self.get('/v1/collections/c2', key=secret).status_code, 404)
        self.assertEqual(self.post('/v1/keys', {'name': 'x', 'permissions': ['admin.keys']},
                                   key=secret).status_code, 403)
        # metadata update, rotation, disable, enable, revoke
        renamed = self.api.handle(Request('PATCH', f'/v1/keys/{key_id}',
                                          headers={'Host': '127.0.0.1',
                                                   'Authorization': f'Bearer {self.admin.secret}',
                                                   'Idempotency-Key': 'u1', 'If-Match': '1',
                                                   'Content-Type': 'application/json'},
                                          body=json.dumps({'name': 'renamed'}).encode()))
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()['name'], 'renamed')
        rotated = self.post(f'/v1/keys/{key_id}/rotate').json()['secret']
        self.assertNotEqual(rotated, secret)
        self.assertEqual(self.get('/v1/results', key=rotated).status_code, 200)
        self.assertEqual(self.post(f'/v1/keys/{key_id}/disable').status_code, 200)
        self.assertEqual(self.get('/v1/results', key=rotated).status_code, 401)
        self.assertEqual(self.post(f'/v1/keys/{key_id}/enable').status_code, 200)
        self.assertEqual(self.get('/v1/results', key=rotated).status_code, 200)
        self.assertEqual(self.post(f'/v1/keys/{key_id}/revoke').status_code, 200)
        gone = self.get('/v1/results', key=rotated)
        self.assertEqual(gone.status_code, 401)
        self.assertEqual(gone.json()['error']['code'], 'E_AUTH_REVOKED')

    def test_expiry_and_rotation_grace_are_read_on_every_request(self):
        created = self.post('/v1/keys', {'name': 'short', 'permissions': ['read.results'],
                                         'expires_at': self.now[0] + 60}).json()
        self.assertEqual(self.get('/v1/results', key=created['secret']).status_code, 200)
        self.now[0] += 61
        expired = self.get('/v1/results', key=created['secret'])
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.json()['error']['code'], 'E_AUTH_EXPIRED')
        rotation = self.post('/v1/keys', {'name': 'rot', 'permissions': ['read.results']}).json()
        old_secret = rotation['secret']
        new_secret = self.post(f"/v1/keys/{rotation['id']}/rotate",
                               {'rotation_grace_seconds': 30}).json()['secret']
        self.assertEqual(self.get('/v1/results', key=new_secret).status_code, 200)
        self.now[0] += 10
        in_grace = self.get('/v1/results', key=old_secret)
        self.assertEqual(in_grace.status_code, 401)
        self.assertEqual(in_grace.json()['error']['code'], 'E_AUTH_ROTATION_GRACE')
        self.now[0] += 40
        expired_grace = self.get('/v1/results', key=old_secret)
        self.assertEqual(expired_grace.status_code, 401)
        self.assertEqual(expired_grace.json()['error']['code'], 'E_AUTH_INVALID')

    def test_subscription_secret_is_forced_narrow_and_listed_separately(self):
        created = self.post('/v1/subscriptions', {'name': 'phone', 'collection_id': 'c1'})
        self.assertEqual(created.status_code, 200)
        secret = created.json()['secret']
        self.assertEqual(sorted(created.json()['permissions']),
                         sorted(apiv1.SUBSCRIPTION_PERMISSIONS))
        self.assertEqual(self.get('/v1/results', key=secret).status_code, 200)
        self.assertEqual(self.post('/v1/keys', {'name': 'x', 'permissions': ['admin.keys']},
                                   key=secret).status_code, 403)
        self.assertEqual(self.get('/v1/subscriptions').json()['items'][0]['purpose'],
                         'subscription')
        self.assertEqual(len(self.get('/v1/subscriptions').json()['items']), 1)
        self.assertEqual(self.post(f"/v1/subscriptions/{created.json()['id']}/revoke").status_code,
                         200)
        self.assertEqual(self.get('/v1/results', key=secret).status_code, 401)

    def test_no_secret_reaches_the_database_or_the_audit_log(self):
        created = self.post('/v1/keys', {'name': 'reader', 'permissions': ['read.results']}).json()
        secret = created['secret']
        dump = '\n'.join(self.conn.iterdump())
        self.assertNotIn(secret, dump)
        self.assertNotIn(secret, json.dumps(self.get('/v1/keys').json()))
        self.assertNotIn(secret, json.dumps(self.get('/v1/audit').json()))
        row = self.conn.execute('SELECT verifier FROM api_keys WHERE id = ?',
                                (created['id'],)).fetchone()
        self.assertNotIn(secret, row[0])
        operations = [entry['operation'] for entry in self.get('/v1/audit').json()['items']]
        self.assertIn('key.create', operations)

    def test_an_unknown_key_is_a_404_and_a_denied_call_is_audited(self):
        missing = self.get('/v1/keys/pwk_nothing')
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()['error']['code'], 'E_STATE_NOT_FOUND')
        self.post('/v1/keys', {'name': 'reader', 'permissions': ['read.results']})
        denied = self.get('/v1/keys', key='pwk_wrong')
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(self.get('/v1/audit').json()['items'][0]['result'], 'denied')
        self.assertEqual(self.get('/v1/audit').json()['items'][0]['error_code'],
                         'E_AUTH_INVALID')

    def test_a_second_local_bootstrap_works_and_stays_out_of_the_database(self):
        issued = self.manager.bootstrap_admin(local_trusted=True, name='second')
        dump = '\n'.join(self.conn.iterdump())
        self.assertNotIn(issued.secret, dump)
        self.assertNotIn(issued.secret, json.dumps(self.get('/v1/keys').json()))
        self.assertEqual(self.get('/v1/service', key=issued.secret).status_code, 200)
        self.assertNotIn(issued.secret, self.get('/v1/audit').text)


if __name__ == '__main__':
    unittest.main()

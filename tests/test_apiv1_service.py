"""The service area of /v1: versioned prefix, network model, capabilities, OpenAPI."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import apiv1
from proxy_workbench.apiv1 import ApiV1, Request

ADMIN = apiv1.Principal(key_id='k-admin', kind='api_key',
                        permissions=frozenset(apiv1.PERMISSIONS))
READER = apiv1.Principal(key_id='k-reader', kind='api_key',
                         permissions=frozenset({'read.status', 'read.results'}))


class FakeKeys(apiv1.KeyStore):
    """A key store that knows nothing about keys: the service area only needs verify()."""

    def __init__(self, principals):
        self.principals = {p.key_id: p for p in principals}
        self.audit_log = []

    def verify(self, secret):
        return self.principals.get(secret)

    def list_keys(self, principal, limit=None, **kwargs):
        return {'items': [], 'stream_id': 'keys', 'next_seq': None}

    def get_key(self, principal, key_id):
        return {'id': key_id}

    def create_key(self, principal, spec):
        return {'id': 'k-new'}

    def update_key(self, principal, key_id, patch):
        return {'id': key_id}

    def rotate_key(self, principal, key_id, grace_s=0.0, **kwargs):
        return {'id': key_id}

    def revoke_key(self, principal, key_id, **kwargs):
        return {'id': key_id}

    def disable_key(self, principal, key_id, **kwargs):
        return {'id': key_id}

    def enable_key(self, principal, key_id, **kwargs):
        return {'id': key_id}

    def delete_key(self, principal, key_id, **kwargs):
        return {'id': key_id}

    def read_audit(self, principal, **filters):
        return {'items': [], 'stream_id': 'audit', 'next_seq': None}

    def audit(self, record):
        self.audit_log.append(record)


class FakeService(apiv1.Service):
    """Minimal service layer: enough to prove the API does not hide domain work."""

    def __init__(self):
        self.calls = []

    def queue_state(self):
        return {'depth': 0, 'capacity': 10}

    def invoke(self, operation, call):
        self.calls.append(operation)
        if operation == 'service.state':
            return {'version': '1.0.0', 'queue': {'depth': 0}, 'pools': [{'id': 'p1'}]}
        if operation == 'service.version':
            return {'product': 'Proxy Workbench', 'api_version': '1.0.0'}
        if operation == 'service.health':
            return {'state': 'ok'}
        if operation == 'service.capabilities':
            return {'permissions': sorted(apiv1.PERMISSIONS), 'network': {'bind': 'loopback'}}
        if operation == 'service.readiness':
            return {'ready': True}
        if operation == 'service.queue':
            return {'depth': 0, 'capacity': 10}
        if operation == 'results.list':
            return {'items': [{'endpoint_id': 'e1', 'country': 'DE', 'admission_reason': 'time_ok'}],
                    'stream_id': 'generation:g1', 'next_seq': 2}
        if operation == 'jobs.list':
            return {'items': [], 'stream_id': 'jobs'}
        return {'operation': operation, 'job_id': 'j1'}


def get(api, path, key=None, **kwargs):
    headers = {'Host': '127.0.0.1:8766'}
    if key:
        headers['Authorization'] = f'Bearer {key}'
    headers.update(kwargs.pop('headers', {}))
    return api.handle(Request('GET', path, headers=headers, **kwargs))


class ServiceAreaTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.keys = FakeKeys([ADMIN, READER])
        self.api = ApiV1(self.service, self.keys)

    def test_every_route_lives_under_the_versioned_prefix(self):
        self.assertTrue(apiv1.ROUTES)
        for route in apiv1.ROUTES:
            self.assertTrue(route.path.startswith('/v1/'), route.path)
        self.assertEqual(get(self.api, '/status').status_code, 404)
        self.assertEqual(get(self.api, '/v1/nowhere').status_code, 404)
        # a legacy read path is not silently promoted into the new API
        self.assertEqual(get(self.api, '/proxies', key='k-reader').status_code, 404)

    def test_version_and_health_need_no_key_on_loopback(self):
        for path in ('/v1/version', '/v1/health'):
            response = get(self.api, path)
            self.assertEqual(response.status_code, 200, path)
        self.assertEqual(self.service.calls, ['service.version', 'service.health'])
        # everything else needs a key even on loopback
        self.assertEqual(get(self.api, '/v1/service').status_code, 401)
        self.assertEqual(get(self.api, '/v1/service').json()['error']['code'], 'E_AUTH_MISSING')

    def test_aggregated_service_state_and_queue(self):
        response = get(self.api, '/v1/service', key='k-reader')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['pools'], [{'id': 'p1'}])
        self.assertEqual(body['queue'], {'depth': 0})
        self.assertEqual(get(self.api, '/v1/queue', key='k-reader').json()['capacity'], 10)
        self.assertEqual(get(self.api, '/v1/readiness', key='k-reader').json(), {'ready': True})

    def test_capabilities_publish_the_permission_canon(self):
        body = get(self.api, '/v1/capabilities', key='k-reader').json()
        self.assertEqual(body['permissions'], sorted(apiv1.PERMISSIONS))
        self.assertIn('admin.keys', body['permissions'])
        self.assertIn('export.secret', body['permissions'])

    def test_host_and_origin_follow_the_network_model(self):
        self.assertEqual(get(self.api, '/v1/health', headers={'Host': 'evil.example'}).status_code, 403)
        denied = get(self.api, '/v1/health', headers={'Host': 'evil.example'}).json()
        self.assertEqual(denied['error']['code'], 'E_AUTH_ORIGIN')
        self.assertEqual(get(self.api, '/v1/health', headers={'Host': '[::1]:8766'}).status_code, 200)
        self.assertEqual(get(self.api, '/v1/health', headers={'Host': 'localhost:8766'}).status_code, 200)
        self.assertEqual(get(self.api, '/v1/health', headers={'Origin': 'https://app.example'}).status_code, 403)

    def test_cors_is_opt_in_and_never_wildcard(self):
        config = apiv1.ApiConfig(allowed_origins=('https://app.example',),
                                 cors_origins=('https://app.example',))
        api = ApiV1(self.service, self.keys, config)
        preflight = api.handle(Request('OPTIONS', '/v1/results',
                                      headers={'Host': '127.0.0.1', 'Origin': 'https://app.example'}))
        self.assertEqual(preflight.status_code, 204)
        self.assertEqual(dict(preflight.headers)['Access-Control-Allow-Origin'],
                         'https://app.example')
        self.assertNotIn('*', dict(preflight.headers)['Access-Control-Allow-Origin'])
        self.assertIn('Authorization', dict(preflight.headers)['Access-Control-Allow-Headers'])
        denied = api.handle(Request('OPTIONS', '/v1/results',
                                    headers={'Host': '127.0.0.1', 'Origin': 'https://evil.example'}))
        self.assertEqual(denied.status_code, 403)
        plain = dict(get(self.api, '/v1/results', key='k-reader').headers)
        self.assertNotIn('Access-Control-Allow-Origin', plain)

    def test_remote_bind_requires_an_explicit_host_list(self):
        with self.assertRaises(ValueError):
            ApiV1(self.service, self.keys, apiv1.ApiConfig(host='0.0.0.0', allow_remote_bind=True))
        api = ApiV1(self.service, self.keys,
                    apiv1.ApiConfig(host='0.0.0.0', allow_remote_bind=True,
                                   allowed_hosts=('workbench.lan',)))
        allowed = {'Host': 'workbench.lan'}
        # off loopback nothing is anonymous, not even liveness
        self.assertEqual(get(api, '/v1/version', headers=allowed).status_code, 401)
        self.assertEqual(get(api, '/v1/version', headers=allowed, key='k-reader').status_code, 200)
        self.assertEqual(get(api, '/v1/version', headers=allowed, key='k-reader').status_code, 200)
        self.assertEqual(get(api, '/v1/version', headers={'Host': 'other.lan'}).status_code, 403)

    def test_wrong_method_reports_what_is_allowed(self):
        wrong = self.api.handle(Request('PUT', '/v1/keys',
                                        headers={'Host': '127.0.0.1',
                                                 'Authorization': 'Bearer k-admin'}))
        self.assertEqual(wrong.status_code, 405)
        self.assertEqual(wrong.json()['error']['code'], 'E_VALIDATION_METHOD')
        self.assertEqual(dict(wrong.headers)['Allow'], 'GET,POST')

    def test_get_never_starts_a_check(self):
        get(self.api, '/v1/results', key='k-reader')
        get(self.api, '/v1/jobs', key='k-admin')
        get(self.api, '/v1/service', key='k-reader')
        self.assertEqual(self.service.calls, ['results.list', 'jobs.list', 'service.state'])
        started = [name for name in self.service.calls if name.startswith(('checks.', 'pools.refill'))]
        self.assertEqual(started, [])

    def test_etag_and_not_modified(self):
        first = get(self.api, '/v1/results', key='k-reader')
        etag = dict(first.headers)['ETag']
        again = get(self.api, '/v1/results', key='k-reader', headers={'If-None-Match': etag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.body, b'')

    def test_tls_and_reverse_proxy_are_documented_and_wired(self):
        model = apiv1.openapi_document()['x-network-model']
        self.assertIn('tls', model)
        self.assertIn('reverse proxy', model['tls'])
        self.assertIn('not a secure channel', model['bearer_is_not_encryption'])
        self.assertIn('Authorization: Bearer only', model['key_transport'])
        with self.assertRaises(OSError):
            ApiV1(self.service, self.keys,
                  apiv1.ApiConfig(port=0, tls_cert='/nonexistent/cert.pem',
                                 tls_key='/nonexistent/key.pem')).make_server()
        plain = ApiV1(self.service, self.keys, apiv1.ApiConfig(port=0)).make_server()
        try:
            self.assertEqual(plain.socket.getsockname()[0], '127.0.0.1')
        finally:
            plain.server_close()

    def test_dependencies_are_checked_at_construction(self):
        class Incomplete:
            def invoke(self, operation, call):
                return {}

        with self.assertRaises(TypeError) as caught:
            ApiV1(Incomplete(), self.keys)
        self.assertIn('queue_state', str(caught.exception))
        with self.assertRaises(TypeError):
            ApiV1(self.service, object())

    def test_openapi_artifact_matches_the_route_table(self):
        artifact = Path(apiv1.openapi_path())
        self.assertTrue(artifact.is_file())
        stored = json.loads(artifact.read_text(encoding='utf-8'))
        self.assertEqual(stored, apiv1.openapi_document())
        self.assertEqual(stored['openapi'], '3.1.0')
        for route in apiv1.ROUTES:
            operation = stored['paths'][route.path][route.method.lower()]
            self.assertEqual(operation['operationId'], route.operation)
            if route.async_job:
                self.assertIn('202', operation['responses'], route.operation)
            if route.sse:
                self.assertIn('200', operation['responses'], route.operation)
        self.assertEqual(stored['x-permissions'], sorted(apiv1.PERMISSIONS))
        self.assertEqual(stored['x-error-codes'], sorted(apiv1.MESSAGES))

    def test_openapi_has_no_secret_shaped_examples(self):
        text = Path(apiv1.openapi_path()).read_text(encoding='utf-8')
        for marker in ('Bearer sk-', 'wbk_', 'CANARY', 'BEGIN PRIVATE KEY'):
            self.assertNotIn(marker, text, marker)
        # the document names the sensitive things, it never carries a value
        for field in ('"secret": "', '"password": "', '"token": "'):
            self.assertNotIn(field, text, field)
        self.assertIn('never a query parameter',
                      json.dumps(apiv1.openapi_document()['components']['securitySchemes']))


if __name__ == '__main__':
    unittest.main()

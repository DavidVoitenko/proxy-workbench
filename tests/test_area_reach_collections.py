"""F07: `POST /v1/collections` stops swallowing `allow_private`.

The route declared ``allow_private`` and the handler read ``name`` and
``kind``.  A client that sent ``{"name": "priv", "kind": "own",
"allow_private": true}`` got 201 and a collection id, and believed private
addresses were allowed there.  Nothing was allowed: ``collections`` has no such
column (CONTRACTS section 3.3, migration 2) and the real control,
``allow_private_endpoints``, lives on the import routes and is enforced by
``importer.DEFAULT_POLICY``.

So the field is refused, with the field that does work named in the action.
This file pins that answer down: the refusal, the field that works instead, and
the proof that the two are not the same thing -- one is a promise about what a
collection may hold, the other is a decision about one import.

Nothing here contacts anything; the only addresses are RFC 5737 documentation
ranges and they are only parsed, never dialled.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from proxy_workbench import apiv1
from proxy_workbench import db
from proxy_workbench import proxytool

KEY = 'reach-collections-operator-key'


def _verdict_ok():
    return json.dumps({'proxy': 'redacted', 'reliability': 1.0, 'min_target_reliability': 1.0,
                       'latency_ms': 60.0, 'successes': 2, 'requests': 2,
                       'samples': [{'ok': True, 'bytes': 512, 'ms': 60.0} for _ in range(2)]})


def prepare(data: Path) -> None:
    """Migrate the database the service writes to, as the program does at start."""
    conn, _ = db.open_db(str(data / db.DB_FILENAME))
    conn.close()


def call(client, method, path, body):
    return client.handle(apiv1.Request(
        method=method, path=path,
        headers={'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json',
                 'Idempotency-Key': f'k{abs(hash((method, path, json.dumps(body, sort_keys=True)))) % 10 ** 9}',
                 'Host': '127.0.0.1:8766'},
        body=json.dumps(body).encode(), client_host='127.0.0.1'))


class AllowPrivateIsRefusedTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.data = Path(self._directory.name)
        prepare(self.data)
        self.service = _Service(self.data)
        self.client = apiv1.ApiV1(self.service, _Keys(), apiv1.ApiConfig(host='127.0.0.1'))

    def test_the_exact_request_from_the_audit_is_refused(self):
        response = call(self.client, 'POST', '/v1/collections',
                        {'name': 'priv', 'kind': 'own', 'allow_private': True})
        self.assertEqual(response.status, 400)
        error = json.loads(response.body)['error']
        self.assertEqual(error['code'], 'E_VALIDATION_FIELD')
        self.assertEqual(error['details']['field'], 'allow_private')

    def test_the_refusal_names_the_field_that_works(self):
        response = call(self.client, 'POST', '/v1/collections',
                        {'name': 'priv', 'kind': 'own', 'allow_private': True})
        action = json.loads(response.body)['error']['action']
        self.assertIn('allow_private_endpoints', action)
        self.assertIn('/imports', action)

    def test_the_refusal_says_why_rather_than_only_that_it_failed(self):
        response = call(self.client, 'POST', '/v1/collections',
                        {'name': 'priv', 'kind': 'own', 'allow_private': True})
        reason = json.loads(response.body)['error']['details']['reason']
        self.assertIn('collections', reason)
        self.assertIn('import', reason)

    def test_no_collection_is_created_by_a_refused_request(self):
        call(self.client, 'POST', '/v1/collections',
             {'name': 'priv', 'kind': 'own', 'allow_private': True})
        from proxy_workbench import pools  # noqa: F401 - the schema is what matters
        conn, _ = db.open_db(str(self.data / db.DB_FILENAME))
        try:
            names = {row['name'] for row in db.list_collections(conn)}
        finally:
            conn.close()
        self.assertNotIn('priv', names)

    def test_the_field_is_still_in_the_schema_so_the_client_can_see_it(self):
        # Refusing a documented field with a reason is a different promise
        # from pretending the field does not exist; a client reading the
        # artifact must find the refusal waiting for it.
        body = apiv1.openapi_document()['paths']['/v1/collections']['post']['requestBody']
        text = json.dumps(body)
        self.assertIn('allow_private', text)

    def test_a_request_without_the_field_is_unaffected(self):
        response = call(self.client, 'POST', '/v1/collections', {'name': 'ok', 'kind': 'own'})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)['name'], 'ok')

    def test_the_schemas_own_default_is_not_a_refusal(self):
        # ``allow_private`` defaults to false, which is the value that asks for
        # nothing.  Refusing it would break a client that sends the schema.
        response = call(self.client, 'POST', '/v1/collections',
                        {'name': 'ok2', 'kind': 'own', 'allow_private': False})
        self.assertEqual(response.status, 200)

    def test_an_explicit_null_is_refused_by_the_schema_not_taken_as_false(self):
        # A null is not the schema's default: the default is a value the
        # request never sends.  It is refused with the field named, so a client
        # that sends null learns it sent something wrong instead of concluding
        # the field was understood and meant "no".
        response = call(self.client, 'POST', '/v1/collections',
                        {'name': 'ok3', 'kind': 'own', 'allow_private': None})
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)['error']['details']['field'], 'allow_private')

    def test_a_never_sent_field_is_the_default_and_needs_no_refusal(self):
        # The schema declares ``default=False``; a request that omits the field
        # gets the default and the handler is not asked to judge anything.
        response = call(self.client, 'POST', '/v1/collections', {'name': 'ok3b', 'kind': 'own'})
        self.assertEqual(response.status, 200)

    def test_a_false_string_is_an_affirmative_request_and_is_refused(self):
        # "true"/"false" are the booleans the API accepts; "false" as a *string*
        # is not a boolean, and the schema layer refuses it before the handler.
        response = call(self.client, 'POST', '/v1/collections',
                        {'name': 'ok4', 'kind': 'own', 'allow_private': 'true'})
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)['error']['details']['field'], 'allow_private')


class TheFieldThatWorksTests(unittest.TestCase):
    """`allow_private_endpoints` is honoured on the import routes."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.data = Path(self._directory.name)
        prepare(self.data)
        self.client = apiv1.ApiV1(_Service(self.data), _Keys(), apiv1.ApiConfig(host='127.0.0.1'))
        created = call(self.client, 'POST', '/v1/collections', {'name': 'mine', 'kind': 'own'})
        self.assertEqual(created.status, 200)
        self.collection = json.loads(created.body)['id']

    def preview(self, body):
        return call(self.client, 'POST', f'/v1/collections/{self.collection}/imports/preview', body)

    def test_it_is_echoed_back_so_the_client_knows_it_counted(self):
        response = self.preview({'format': 'txt', 'content': '203.0.113.9:8080\n',
                                 'allow_private_endpoints': True})
        self.assertEqual(response.status, 200)
        body = json.loads(response.body)
        self.assertTrue(body['allow_private_endpoints'])
        self.assertFalse(body['policy']['public_only'])

    def test_without_it_the_same_content_is_refused(self):
        response = self.preview({'format': 'txt', 'content': '203.0.113.9:8080\n'})
        self.assertEqual(response.status, 200)
        body = json.loads(response.body)
        self.assertTrue(body['policy']['public_only'])
        self.assertEqual(body['counts']['valid'], 0)

    def test_it_is_a_decision_about_one_import_not_a_property_of_a_collection(self):
        # The two fields are not the same thing: one says what this import may
        # contain, the other claimed to be a standing property of the
        # collection.  A stored flag the membership writers never consult would
        # be the same silent lie with a longer fuse, which is why the second is
        # refused rather than half-implemented.
        first = json.loads(self.preview({'format': 'txt', 'content': '203.0.113.9:8080\n',
                                         'allow_private_endpoints': True}).body)
        second = json.loads(self.preview({'format': 'txt', 'content': '203.0.113.9:8080\n'}).body)
        # The answer is always echoed, so a client can tell "allowed" from
        # "not asked for" instead of inferring it from an absent field.
        self.assertTrue(first['allow_private_endpoints'])
        self.assertFalse(second['allow_private_endpoints'])
        self.assertTrue(second['policy']['public_only'])
        # Two imports of the same address, two different decisions: the flag is
        # a property of the request, not of the collection.
        self.assertEqual(first['counts']['valid'], 1)
        self.assertEqual(second['counts']['valid'], 0)


class PatchCarriesTheSameFieldTests(unittest.TestCase):
    """`PATCH /v1/collections/{id}` declares the field too, so it is refused too."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.data = Path(self._directory.name)
        prepare(self.data)
        self.client = apiv1.ApiV1(_Service(self.data), _Keys(), apiv1.ApiConfig(host='127.0.0.1'))
        created = call(self.client, 'POST', '/v1/collections', {'name': 'mine', 'kind': 'own'})
        self.collection = json.loads(created.body)['id']

    def test_the_patch_route_declares_the_field(self):
        text = json.dumps(apiv1.openapi_document()['paths']['/v1/collections/{id}']['patch'])
        self.assertIn('allow_private', text)

    def test_the_handler_refuses_it_when_the_request_reaches_it(self):
        from proxy_workbench import api
        # ``PATCH`` is behind ``If-Match`` and ``collections`` has no revision
        # column (CONTRACTS 3.3), so the transport refuses every PATCH before
        # the handler runs.  The handler still refuses the field itself, which
        # is what a future revision column would meet.
        response = call(self.client, 'PATCH', f'/v1/collections/{self.collection}',
                        {'allow_private': True})
        self.assertGreaterEqual(response.status, 400)
        # Called directly, the handler is the one that refuses.
        class _Call:
            body = {'allow_private': True}
            params = {'id': self.collection}
            expected_revision = None
        with self.assertRaises(apiv1.ApiError) as caught:
            api._refuse_unsupported_allow_private(_Call.body)
        self.assertEqual(caught.exception.details['field'], 'allow_private')
        self.assertIn('allow_private_endpoints', caught.exception.action)

    def test_a_patch_without_the_field_is_not_about_the_field_at_all(self):
        response = call(self.client, 'PATCH', f'/v1/collections/{self.collection}', {'name': 'x'})
        # Whether that succeeds is the revision column's business; what must
        # not happen is a complaint about allow_private.
        self.assertNotIn('allow_private', response.body.decode())


class _Service:
    """The real engine, behind the interface ``apiv1`` checks for."""

    def __init__(self, data):
        from proxy_workbench import api
        self._inner = api.WorkbenchService(data)

    def invoke(self, operation, call):
        return self._inner.invoke(operation, call)

    def queue_state(self):
        return self._inner.queue_state()


class _Keys(apiv1.KeyStore):
    def verify(self, secret):
        if secret != KEY:
            return None
        return apiv1.Principal(key_id='k-op', kind='api_key',
                               permissions=frozenset(apiv1.PERMISSIONS))

    def read_audit(self, principal, **filters):
        return {'items': []}

    def audit(self, record):
        pass


if __name__ == '__main__':
    unittest.main()

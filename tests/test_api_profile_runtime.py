"""Real profile storage behind versioned API lifecycle routes."""
import json
import unittest

from proxy_workbench import api, apiv1
from tests.test_apiv1_service import FakeKeys
from tests.test_importer_fixtures import Schema


class ProfileApiRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema()
        self.addCleanup(self.schema.close)
        service = api.WorkbenchService(self.schema.path.parent, db_path=self.schema.path)
        principal = apiv1.Principal('admin', permissions=frozenset(apiv1.PERMISSIONS))
        self.client = apiv1.ApiV1(service, FakeKeys([principal]))
        self.sequence = 0

    def call(self, path, body=None, method='POST'):
        self.sequence += 1
        return self.client.handle(apiv1.Request(method, path, headers={
            'Host': '127.0.0.1', 'Authorization': 'Bearer admin',
            'Content-Type': 'application/json', 'Idempotency-Key': str(self.sequence)},
            body=json.dumps(body or {}).encode() if method == 'POST' else b''))

    def test_saved_profile_can_be_validated_cloned_updated_and_archived(self):
        created = self.call('/v1/profiles', {'name': 'original', 'targets': [
            {'id': 'one', 'kind': 'required'}, {'id': 'two', 'kind': 'optional'}],
            'rule': 'at_least_k', 'k': 1, 'attempts': 3})
        self.assertEqual(created.status_code, 200, created.json())
        profile_id = created.json()['id']
        valid = self.call(f'/v1/profiles/{profile_id}/validate')
        self.assertEqual(valid.status_code, 200, valid.json())
        self.assertTrue(valid.json()['valid'])
        copied = self.call(f'/v1/profiles/{profile_id}/clone', {'name': 'copy'})
        self.assertEqual(copied.status_code, 200, copied.json())
        clone_id = copied.json()['id']
        revised = self.call(f'/v1/profiles/{clone_id}/versions', {'attempts': 4})
        self.assertEqual(revised.status_code, 200, revised.json())
        self.assertEqual(revised.json()['revision'], 2)
        spec = self.call(f'/v1/profiles/{clone_id}/validate').json()['profile']['revisions'][0]['config']
        self.assertEqual(spec['attempts'], 4)
        self.assertEqual(len(spec['targets']), 2)
        self.assertEqual(spec['optional_rule'], {'mode': 'at_least', 'k': 1})
        original = self.call(f'/v1/profiles/{profile_id}/validate')
        self.assertEqual(original.status_code, 200, original.json())
        self.assertEqual(original.json()['profile']['revisions'][0]['config']['attempts'], 3)
        archived = self.call(f'/v1/profiles/{clone_id}/archive')
        self.assertEqual(archived.status_code, 200, archived.json())
        self.assertIsNotNone(self.call(f'/v1/profiles/{clone_id}', method='GET').json()['archived_at'])
        self.assertIsNone(self.call(f'/v1/profiles/{profile_id}', method='GET').json()['archived_at'])

    def test_unsupported_profile_options_are_refused_without_server_error(self):
        for field in ('max_age_seconds', 'connect_timeout_s', 'timeout_s', 'max_bytes', 'parent_id'):
            with self.subTest(field=field):
                payload = {'name': 'unsupported', 'targets': [{'id': 'one'}],
                           field: 'p_parent' if field == 'parent_id' else 30}
                response = self.call('/v1/profiles', payload)
                self.assertEqual(response.status_code, 400, response.json())
                self.assertEqual(response.json()['error']['code'], 'E_VALIDATION_FIELD')

    def test_missing_profile_validate_clone_archive_return_not_found(self):
        for suffix, body in (('validate', {}), ('clone', {'name': 'copy'}), ('archive', {})):
            with self.subTest(suffix=suffix):
                response = self.call('/v1/profiles/p_missing/' + suffix, body)
                self.assertEqual(response.status_code, 404, response.json())

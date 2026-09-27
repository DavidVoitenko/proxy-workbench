"""Regressions of the /v1 control operations that answered wrongly on a real server.

Same set-up as ``test_api_audit_regressions``: a loopback server on a temporary
data folder whose rows use the RFC 5737 documentation ranges.  Nothing is
fetched and no proxy is contacted.
"""
from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from proxy_workbench import api, apikeys, apiv1, db
from tests import web_support as ws
from tests.test_api_audit_regressions import LiveServerCase


def unique(prefix):
    return prefix + '-' + uuid.uuid4().hex[:8]


class CollectionTests(LiveServerCase):
    def create(self):
        response = self.call('POST', '/v1/collections', {'name': unique('col')})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['id']

    def members(self, collection):
        response = self.call('GET', f'/v1/collections/{collection}/members')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['items']

    def test_patch_renames_archives_and_restores_under_a_revision(self):
        collection = self.create()
        self.assertEqual(self.call('GET', f'/v1/collections/{collection}').json()['revision'], 1)
        renamed = self.call('PATCH', f'/v1/collections/{collection}', {'name': 'renamed'},
                            headers={'If-Match': '1'})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual((renamed.json()['name'], renamed.json()['revision']), ('renamed', 2))
        stale = self.call('PATCH', f'/v1/collections/{collection}', {'name': 'again'},
                          headers={'If-Match': '1'})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()['error']['code'], 'E_CONFLICT_REVISION')
        archived = self.call('PATCH', f'/v1/collections/{collection}',
                             {'archived': True, 'revision': 2})
        self.assertEqual(archived.status_code, 200, archived.text)
        self.assertTrue(archived.json()['archived'])
        self.assertIsNotNone(self.call('GET', f'/v1/collections/{collection}').json()['archived_at'])
        restored = self.call('PATCH', f'/v1/collections/{collection}',
                             {'archived': False, 'revision': 3})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertFalse(restored.json()['archived'])
        self.assertEqual(restored.json()['revision'], 4)

    def test_merge_copies_the_other_collection(self):
        target, source = self.create(), self.create()
        self.call('POST', f'/v1/collections/{source}/imports',
                  {'content': '192.0.2.201:8080', 'allow_private_endpoints': True})
        merged = self.call('POST', f'/v1/collections/{target}/merge',
                           {'from_collection_id': source})
        self.assertEqual(merged.status_code, 202, merged.text)
        self.assertTrue(merged.json()['job_id'])
        self.assertEqual(merged.json()['added'], 1)
        self.assertEqual([item['canonical'] for item in self.members(target)],
                         ['http://192.0.2.201:8080'])
        self.assertEqual(self.call('POST', f'/v1/collections/{target}/merge',
                                   {'from_collection_id': 'no-such'}).status_code, 404)

    def test_merge_source_must_be_in_the_key_scope(self):
        target, source = self.create(), self.create()
        secret = self.key(['collections.write', 'collections.read'], collections=[target])
        response = self.call('POST', f'/v1/collections/{target}/merge',
                             {'from_collection_id': source}, token=secret)
        self.assertEqual(response.status_code, 404)

    def test_replace_answers_with_a_job(self):
        collection = self.create()
        response = self.call('POST', f'/v1/collections/{collection}/replace',
                             {'content': 'http://192.0.2.202:8080\n',
                              'allow_private_endpoints': True})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()['job_id'])
        self.assertEqual([item['canonical'] for item in self.members(collection)],
                         ['http://192.0.2.202:8080'])

    def test_member_add_refuses_garbage_and_remove_takes_the_endpoint_id(self):
        collection = self.create()
        garbage = self.call('POST', f'/v1/collections/{collection}/members',
                            {'endpoint': 'not an endpoint'})
        self.assertEqual(garbage.status_code, 400, garbage.text)
        self.assertEqual(garbage.json()['error']['details']['field'], 'endpoint')
        self.assertEqual(self.members(collection), [])
        added = self.call('POST', f'/v1/collections/{collection}/members',
                          {'endpoint': 'socks5://192.0.2.203:1080'})
        self.assertEqual(added.status_code, 200, added.text)
        [member] = self.members(collection)
        removed = self.call('DELETE',
                            f'/v1/collections/{collection}/members/{member["endpoint_id"]}')
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(self.members(collection), [])
        again = self.call('DELETE', f'/v1/collections/{collection}/members/{member["endpoint_id"]}')
        self.assertEqual(again.status_code, 404)


class ScopeTests(LiveServerCase):
    def test_compare_refuses_a_collection_outside_the_key_scope(self):
        own = self.call('POST', '/v1/collections', {'name': unique('own')}).json()['id']
        secret = self.key(['sources.read'], collections=[own])
        body = {'sources': ['src-a']}
        for path, extra in (('/v1/sources/compare', {}),
                            ('/v1/sources/compare/suppliers', {'left': 'a', 'right': 'b'}),
                            ('/v1/sources/compare/cohorts',
                             {'cohorts': [{'label': 'x'}, {'label': 'y'}]})):
            outside = self.call('POST', path, {**body, **extra, 'collection_id': 'public-base'},
                                token=secret)
            self.assertEqual(outside.status_code, 404, (path, outside.text))
            nothing = self.call('POST', path, {**body, **extra}, token=secret)
            self.assertEqual(nothing.status_code, 400, (path, nothing.text))
        nested = self.call('POST', '/v1/sources/compare',
                           {**body, 'collection_id': own,
                            'cohort': {'collection_id': 'public-base'}}, token=secret)
        self.assertEqual(nested.status_code, 404, nested.text)


class PoolAndScheduleTests(LiveServerCase):
    def test_pool_profile_defaults_like_the_gui_and_must_exist(self):
        name = unique('pool')
        created = self.call('POST', '/v1/pools', {'name': name})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()['profile_id'], ws.profile_id())
        missing = self.call('POST', '/v1/pools', {'name': unique('pool'), 'profile_id': 'nope'})
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(missing.json()['error']['details']['field'], 'profile_id')

    def test_pool_update_does_not_drop_the_name(self):
        name = unique('pool')
        self.assertEqual(self.call('POST', '/v1/pools', {'name': name}).status_code, 200)
        renamed = self.call('PATCH', f'/v1/pools/{name}', {'name': 'other', 'revision': 1})
        self.assertEqual(renamed.status_code, 400, renamed.text)
        self.assertEqual(renamed.json()['error']['details']['field'], 'name')
        same = self.call('PATCH', f'/v1/pools/{name}', {'name': name, 'desired': 3,
                                                          'revision': 1})
        self.assertEqual(same.status_code, 200, same.text)
        self.assertEqual(same.json()['desired'], 3)

    def test_schedule_on_an_unknown_collection_is_refused(self):
        response = self.call('POST', '/v1/schedules', {'name': unique('s'), 'kind': 'check',
                                                        'interval_minutes': 30,
                                                        'collection_id': 'no-such'})
        self.assertEqual(response.status_code, 404, response.text)
        ok = self.call('POST', '/v1/schedules', {'name': unique('s'), 'kind': 'check',
                                                  'interval_minutes': 30,
                                                  'collection_id': 'public-base'})
        self.assertEqual(ok.status_code, 200, ok.text)


class GatewayTests(LiveServerCase):
    def test_config_is_stored_under_its_revision(self):
        current = self.call('GET', '/v1/gateway/config').json()
        revision = current['revision']
        unknown = self.call('PATCH', '/v1/gateway/config', {'transports': ['carrier-pigeon']},
                            headers={'If-Match': str(revision)})
        self.assertEqual(unknown.status_code, 400, unknown.text)
        self.assertEqual(unknown.json()['error']['details']['field'], 'transports')
        changed = self.call('PATCH', '/v1/gateway/config',
                            {'max_per_proxy': 3, 'transports': ['http', 'socks5h']},
                            headers={'If-Match': str(revision)})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertTrue(changed.json()['applied'])
        self.assertEqual(changed.json()['revision'], revision + 1)
        stale = self.call('PATCH', '/v1/gateway/config', {'max_per_proxy': 5},
                          headers={'If-Match': str(revision)})
        self.assertEqual(stale.status_code, 409, stale.text)
        stored = self.call('GET', '/v1/gateway/config').json()
        self.assertEqual((stored['max_per_proxy'], stored['transports'], stored['revision']),
                         (3, ['http', 'socks5h'], revision + 1))

    def test_a_binding_is_listed_and_revisioned(self):
        pool = unique('gw')
        self.assertEqual(self.call('POST', '/v1/pools', {'name': pool}).status_code, 200)
        listener = unique('l')
        bound = self.call('POST', '/v1/gateway/bindings',
                          {'listener': listener, 'pool_id': pool, 'revision': 1})
        self.assertEqual(bound.status_code, 200, bound.text)
        listed = self.call('GET', '/v1/gateway/bindings').json()['items']
        self.assertIn((listener, pool, 1),
                      [(item['listener'], item['pool_id'], item['revision']) for item in listed])
        again = self.call('POST', '/v1/gateway/bindings',
                          {'listener': listener, 'pool_id': pool, 'revision': 1})
        self.assertEqual(again.json()['revision'], 2)
        stale = self.call('POST', '/v1/gateway/bindings',
                          {'listener': listener, 'pool_id': pool, 'revision': 1})
        self.assertEqual(stale.status_code, 409, stale.text)


class KeyDeleteTests(LiveServerCase):
    def test_only_a_revoked_key_is_deleted(self):
        created = self.call('POST', '/v1/keys', {'name': unique('k'),
                                                  'permissions': ['read.status']}).json()
        refused = self.call('DELETE', f'/v1/keys/{created["id"]}')
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()['error']['code'], 'E_STATE_KEY_NOT_REVOKED')
        self.assertEqual(self.call('GET', f'/v1/keys/{created["id"]}').json()['state'], 'active')
        self.assertEqual(self.call('POST', f'/v1/keys/{created["id"]}/revoke').status_code, 200)
        deleted = self.call('DELETE', f'/v1/keys/{created["id"]}')
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.call('GET', f'/v1/keys/{created["id"]}').status_code, 404)


class _NoKeys(apiv1.KeyStore):
    def verify(self, secret):
        return None


class _NoService(apiv1.Service):
    def invoke(self, operation, call):
        return {}

    def queue_state(self):
        return {'depth': 0}


class HostHeaderTests(unittest.TestCase):
    def control(self, host):
        return apiv1.ApiV1(_NoService(), _NoKeys(),
                           apiv1.ApiConfig(host=host, allowed_hosts=api.accepted_host_names(host),
                                           allow_remote_bind=True))

    def code(self, control, host_header):
        response = control.handle(apiv1.Request('GET', '/v1/service',
                                                headers={'Host': host_header},
                                                client_host='127.0.0.1'))
        return json.loads(response.body)['error']['code']

    def test_a_wildcard_bind_accepts_the_machine_and_refuses_other_names(self):
        names = api.accepted_host_names('0.0.0.0')
        hostname = socket.gethostname().lower()
        self.assertIn('localhost', names)
        self.assertIn('127.0.0.1', names)
        self.assertIn(hostname, names)
        self.assertNotIn('0.0.0.0', names)
        control = self.control('0.0.0.0')
        for accepted in ('localhost:8765', '127.0.0.1:8765', '[::1]:8765', f'{hostname}:8765'):
            self.assertNotEqual(self.code(control, accepted), 'E_AUTH_ORIGIN', accepted)
        self.assertEqual(self.code(control, 'attacker.example:8765'), 'E_AUTH_ORIGIN')

    def test_a_named_bind_accepts_its_own_address(self):
        control = self.control('192.0.2.10')
        self.assertNotEqual(self.code(control, '192.0.2.10:8765'), 'E_AUTH_ORIGIN')
        self.assertEqual(self.code(control, 'rebound.example'), 'E_AUTH_ORIGIN')


class AuditPruningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        path = Path(self.tmp) / db.DB_FILENAME
        db.migrate(path)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.addCleanup(self.conn.close)

    def test_migration_20_adds_the_revision_and_the_index(self):
        self.assertEqual(db.SCHEMA_VERSION, 20)
        self.assertIn('revision', db.columns(self.conn, 'collections'))
        indexes = [row[1] for row in self.conn.execute('PRAGMA index_list(audit_log)')]
        self.assertIn('audit_log_at', indexes)

    def test_retention_is_exact_without_a_scan_per_insert(self):
        manager = apikeys.ApiKeyManager(self.conn, audit_retention=10)
        for index in range(5):
            manager._audit(None, 'warm-up')
        statements = []
        self.conn.set_trace_callback(statements.append)
        for index in range(40):
            manager._audit(None, 'probe')
        self.conn.set_trace_callback(None)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM audit_log').fetchone()[0], 10)
        joined = '\n'.join(statements).lower()
        self.assertNotIn('not in', joined)
        self.assertNotIn('count(*)', joined)
        plan = ' '.join(str(row[-1]) for row in self.conn.execute(
            'EXPLAIN QUERY PLAN SELECT rowid FROM audit_log ORDER BY at, rowid LIMIT 5'))
        self.assertIn('audit_log_at', plan)


if __name__ == '__main__':
    unittest.main()

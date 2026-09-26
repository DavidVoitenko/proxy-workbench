"""F29 key manager: the secret is shown once, and nothing else gives it back.

Acceptance 6 of F29 is the one that bites: "Canary secrets отсутствуют в БД API
verifiers как plaintext, logs, argv, обычных files/JSON/OpenAPI examples", and
the manager's own rule is that the full secret is shown **exactly once** --
afterwards only the id, the prefix and the metadata.

`apikeys` marks the body of an issuance with :class:`OneShotBody` and offers
:func:`carries_one_shot` / :func:`without_one_shot` for a caller that is about
to *keep* the answer.  The keeper that actually exists is the idempotency cache
in `apiv1.py`, and it stores the finished response -- bytes, with the marker
already gone -- so a repeated `POST /v1/keys` with the same Idempotency-Key used
to hand the same `pwk_...` back a second time.  The fix is one call at that call
site (:func:`without_one_shot_response`); these tests prove the function closes
the hole, by driving the real `ApiV1` with the patched store, so they keep
passing once `apiv1.py` adopts the call and document exactly what it must do.

Everything runs against a fake clock and a temporary database.  No network, no
real credential, no user data.
"""
import base64
import dataclasses
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import apikeys, apiv1
from proxy_workbench.apiv1 import ApiV1, Request

CANARY = 'CANARY-ADMIN-4b7d2e-do-not-leak'


class Service(apiv1.Service):
    def queue_state(self):
        return {'depth': 0, 'capacity': 4}

    def invoke(self, operation, call):
        return {'operation': operation}


class KeyManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.now = [1700000000.0]
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / 'keys.sqlite3'
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        apikeys.ensure_schema(self.conn)
        self.addCleanup(self.conn.close)
        self.manager = apikeys.ApiKeyManager(self.conn, now=lambda: self.now[0])
        self.admin = self.manager.bootstrap_admin(local_trusted=True, name='bootstrap')
        self.counter = 0

    def issue(self, name='k', permissions=('read.results',), **kwargs):
        return self.manager.create(actor=self.admin.info.id, name=name,
                                   permissions=list(permissions), **kwargs)

    def raw_rows(self, table='api_keys'):
        columns = [row[1] for row in self.conn.execute('PRAGMA table_info(%s)' % table)]
        return [dict(zip(columns, row))
                for row in self.conn.execute('SELECT * FROM %s' % table)]

    def row(self, key_id):
        return next(r for r in self.raw_rows() if r['id'] == key_id)


class OneShotDeliveryTests(KeyManagerTestCase):
    def test_the_full_secret_is_returned_once_and_only_the_prefix_afterwards(self):
        issued = self.issue()
        body = issued.as_json()
        self.assertTrue(apikeys.carries_one_shot(body))
        self.assertTrue(body['secret'].startswith(apikeys.SECRET_MARK))
        stored = self.row(issued.key_id)
        self.assertEqual(stored['prefix'], body['prefix'])
        self.assertNotEqual(stored['prefix'], body['secret'])
        listed = self.manager.list_keys(actor=self.admin.info.id)
        self.assertNotIn('secret', json.dumps([i.as_dict() for i in listed], default=str))
        self.assertNotIn(issued.secret, json.dumps([i.as_dict() for i in listed], default=str))

    def test_a_body_that_carries_nothing_is_not_treated_as_one_shot(self):
        body = self.admin.info.as_dict()
        self.assertFalse(apikeys.carries_one_shot(body))
        self.assertIs(apikeys.without_one_shot(body), body, 'the common path costs one pass')

    def test_the_cache_keeps_a_copy_without_the_value_and_says_so(self):
        issued = self.issue()
        kept = apikeys.without_one_shot(issued.as_json())
        self.assertNotIn('secret', kept)
        self.assertTrue(kept[apikeys.ONE_SHOT_NOTE_FIELD],
                        'a silent omission reads as "this key has no secret"')
        self.assertEqual(kept['id'], issued.key_id, 'the caller still learns which key it was')

    def test_an_export_shaped_secret_field_is_not_stripped(self):
        """`carries_one_shot` asks the issuer, not the field name.

        A downloadable artifact with `include_secrets` may be fetched again by
        its id, so treating its body as once-only would silently change that
        route.  Only a value shaped like a key this module issued is removed.
        """
        response = Response(json.dumps({'id': 'a1', 'secrets': ['sec_abc'],
                                         'secret': 'vault://ref/not-a-key'}).encode())
        self.assertIs(apikeys.without_one_shot_response(response), response)

    def test_the_response_scrubber_keeps_an_ordinary_answer_identical(self):
        ordinary = Response(json.dumps({'items': [], 'stream_id': 'keys'}).encode())
        self.assertIs(apikeys.without_one_shot_response(ordinary), ordinary)
        raw = Response(b'not json at all')
        self.assertIs(apikeys.without_one_shot_response(raw), raw)
        empty = Response(b'')
        self.assertIs(apikeys.without_one_shot_response(empty), empty)
        reference = Response(json.dumps({'id': 'a1', 'secrets': ['sec_abc'],
                                         'secret_ref': 'sec_abc'}).encode())
        self.assertIs(apikeys.without_one_shot_response(reference), reference,
                      'a secret *reference* is not an issued key')
        binary = Response(b'\x89PNG\r\n\x1a\nsecret: pwk_abcdef')
        self.assertIs(apikeys.without_one_shot_response(binary), binary,
                      'a non-JSON body is never rewritten')


class OneShotThroughTheApiTests(unittest.TestCase):
    """The real `/v1` pipeline with the one-line cache fix applied.

    `apiv1.IdempotencyStore.put` is wrapped here instead of edited, because
    `apiv1.py` is owned by another area.  The moment it adopts
    `without_one_shot_response` at its own call site, this test still passes and
    starts exercising the real method instead of the wrapper.
    """

    def setUp(self):
        self.now = [1700000000.0]
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        apikeys.ensure_schema(self.conn)
        self.addCleanup(self.conn.close)
        self.manager = apikeys.ApiKeyManager(self.conn, now=lambda: self.now[0])
        self.admin = self.manager.bootstrap_admin(local_trusted=True, name='bootstrap')
        self.api = ApiV1(Service(), apiv1.ApiKeyStore(self.manager), clock=lambda: self.now[0])

        original = apiv1.IdempotencyStore.put

        def put_with_one_shot_guard(store, bucket, key, digest, response):
            return original(store, bucket, key, digest, apikeys.without_one_shot_response(response))

        apiv1.IdempotencyStore.put = put_with_one_shot_guard
        self.addCleanup(setattr, apiv1.IdempotencyStore, 'put', original)
        self.counter = 0

    def call(self, method, path, body=None, *, key=None, idem=None):
        self.counter += 1
        headers = {'Host': '127.0.0.1',
                   'Authorization': 'Bearer %s' % (key or self.admin.secret),
                   'Content-Type': 'application/json'}
        if method != 'GET':
            headers['Idempotency-Key'] = idem or 'i-%d' % self.counter
        return self.api.handle(Request(method, path, headers=headers,
                                       body=json.dumps(body or {}).encode()))

    def test_repeating_a_key_creation_does_not_hand_out_the_secret_twice(self):
        body = {'name': 'reader', 'permissions': ['read.results']}
        first = self.call('POST', '/v1/keys', body, idem='IDEM-1')
        second = self.call('POST', '/v1/keys', body, idem='IDEM-1')
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()['secret'].startswith(apikeys.SECRET_MARK))
        self.assertEqual(second.status_code, 200)
        self.assertNotIn('secret', second.json())
        self.assertTrue(second.json()[apikeys.ONE_SHOT_NOTE_FIELD])
        self.assertEqual(first.json()['id'], second.json()['id'],
                         'one idempotency key still mints one key')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM api_keys').fetchone()[0], 2)

    def test_repeating_a_rotation_does_not_hand_out_the_secret_twice(self):
        key_id = self.call('POST', '/v1/keys',
                           {'name': 'r', 'permissions': ['read.results']}).json()['id']
        first = self.call('POST', '/v1/keys/%s/rotate' % key_id, {}, idem='R1')
        second = self.call('POST', '/v1/keys/%s/rotate' % key_id, {}, idem='R1')
        self.assertTrue(first.json()['secret'].startswith(apikeys.SECRET_MARK))
        self.assertNotIn('secret', second.json())
        self.assertTrue(second.json()[apikeys.ONE_SHOT_NOTE_FIELD])
        self.assertEqual(first.json()['id'], second.json()['id'])

    def test_nothing_the_cache_kept_carries_an_issued_key(self):
        self.call('POST', '/v1/keys', {'name': 'r', 'permissions': ['read.results']}, idem='K1')
        self.call('POST', '/v1/keys', {'name': 'q', 'permissions': ['read.results']}, idem='K2')
        self.call('GET', '/v1/keys')
        cached = b''.join(record['response'].body
                          for record in self.api.idempotency._records.values())
        self.assertNotIn(apikeys.SECRET_MARK.encode(), cached)
        self.assertNotIn(self.admin.secret.encode(), cached)
        self.assertTrue(self.api.idempotency._records, 'the guard must not disable the cache')

    def test_the_first_answer_is_still_complete(self):
        answer = self.call('POST', '/v1/keys', {'name': 'r', 'permissions': ['read.results']}).json()
        for field in ('id', 'prefix', 'name', 'permissions', 'scope', 'warnings', 'secret'):
            self.assertIn(field, answer)


class EntitlementTests(KeyManagerTestCase):
    def test_the_secret_is_cryptographically_random_with_room_to_spare(self):
        secrets = {self.issue(name='k%d' % index).secret for index in range(200)}
        self.assertEqual(len(secrets), 200, 'two issuances must never collide')
        bodies = {secret.split('_', 1)[1].split('_', 1)[1] for secret in secrets}
        self.assertEqual(len(bodies), 200)
        for body in bodies:
            decoded = base64.urlsafe_b64decode(body + '=' * (-len(body) % 4))
            self.assertEqual(len(decoded), apikeys.SECRET_BYTES,
                             'the secret must carry the full declared entropy')
        handles = {secret.split('_', 1)[1].split('_', 1)[0] for secret in secrets}
        self.assertEqual(len(handles), 200, 'the public handle is unique too')
        for handle in handles:
            self.assertEqual(len(handle), apikeys.PREFIX_LENGTH)

    def test_the_stored_verifier_is_a_hash_and_never_the_secret(self):
        issued = self.issue()
        row = self.raw_rows()[0]
        for column in ('verifier', 'verifier_salt', 'verifier_algo',
                       'previous_verifier', 'previous_verifier_salt'):
            self.assertNotIn(issued.secret, row[column] or '')
        self.assertTrue(row['verifier_algo'].startswith(apikeys.VERIFIER_ALGO))
        self.assertGreaterEqual(int(row['verifier_algo'].split('$')[1]), 100000)
        self.assertNotIn(issued.secret.encode(), self.path.read_bytes())

    def test_a_wrong_secret_of_the_right_shape_costs_the_same_as_a_right_one(self):
        """`authenticate` runs a full derivation for an unknown prefix too."""
        unknown = '%s%s_%s' % (apikeys.SECRET_MARK, 'f' * apikeys.PREFIX_LENGTH, 'x' * 40)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.manager.authenticate(unknown)
        self.assertEqual(caught.exception.code, apikeys.E_INVALID)
        with self.assertRaises(apikeys.ApiKeyError) as near:
            self.manager.authenticate(self.admin.secret[:-1] + 'Z')
        self.assertEqual(near.exception.code, apikeys.E_INVALID)
        self.assertEqual(caught.exception.detail, near.exception.detail,
                         'the two failures are indistinguishable to the caller')

    def test_a_garbage_presentation_is_refused_without_touching_the_table(self):
        for value in ('', None, 42, 'pwk_', 'pwk__x', 'pwk_nohandle',
                      '%s%s_%s' % (apikeys.SECRET_MARK, 'f' * apikeys.PREFIX_LENGTH, 'x' * 600)):
            with self.subTest(value=str(value)[:24]):
                with self.assertRaises(apikeys.ApiKeyError):
                    self.manager.authenticate(value)


@dataclasses.dataclass(frozen=True)
class Response:
    """The shape of ``apiv1.Response``: a frozen dataclass with a bytes body.

    Declared here rather than imported so the scrubber is tested against the
    contract it promises -- a frozen dataclass carrying a serialized body -- and
    not against one particular transport.
    """

    status: int = 200
    body: bytes = b''
    content_type: str = 'application/json'
    headers: tuple = ()
    stream: object = None


class LifecycleTests(KeyManagerTestCase):
    def test_list_rename_disable_enable_revoke_delete_and_metadata(self):
        issued = self.issue(name='before')
        key_id = issued.key_id
        info = self.manager.get_key(key_id, actor=self.admin.info.id)
        self.assertIsNone(info.expires_at)
        self.assertEqual(info.state, 'active')

        self.now[0] += 30
        self.manager.authenticate(issued.secret)
        self.assertEqual(self.manager.get_key(key_id, actor=self.admin.info.id).last_used_at,
                         self.now[0], 'last-used is recorded for the list screen')

        self.manager.update_metadata(key_id, actor=self.admin.info.id, name='after')
        self.assertEqual(self.manager.get_key(key_id, actor=self.admin.info.id).name, 'after')
        self.assertIsNotNone(self.manager.disable(key_id, actor=self.admin.info.id).disabled_at)
        self.assertEqual(self.manager.get_key(key_id, actor=self.admin.info.id).state, 'disabled')
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.manager.authenticate(issued.secret)
        self.assertEqual(caught.exception.code, apikeys.E_DISABLED)
        self.manager.enable(key_id, actor=self.admin.info.id)
        self.assertTrue(self.manager.authenticate(issued.secret))

        self.manager.revoke(key_id, actor=self.admin.info.id)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.manager.authenticate(issued.secret)
        self.assertEqual(caught.exception.code, apikeys.E_REVOKED)
        with self.assertRaises(apikeys.ApiKeyError):
            self.manager.update_metadata(key_id, actor=self.admin.info.id, name='x')
        self.manager.delete(key_id, actor=self.admin.info.id)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.manager.get_key(key_id)
        self.assertEqual(caught.exception.code, apikeys.E_FIELD)
        self.assertNotIn(key_id, [info.id for info in
                                  self.manager.list_keys(actor=self.admin.info.id)])

    def test_expiry_is_read_on_every_request_and_on_a_new_lease(self):
        issued = self.issue()
        self.now[0] += 5
        self.manager.update_metadata(issued.key_id, actor=self.admin.info.id, expires_at=self.now[0] + 10)
        self.assertTrue(self.manager.authenticate(issued.secret))
        self.now[0] += 11
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.manager.authenticate(issued.secret)
        self.assertEqual(caught.exception.code, apikeys.E_EXPIRED)
        with self.assertRaises(apikeys.ApiKeyError):
            self.manager.assert_active(issued.key_id)
        guard = apikeys.QuotaGuard(self.manager)
        with self.assertRaises(apikeys.ApiKeyError):
            guard.acquire(apikeys.Principal(self.manager.get_key(issued.key_id)))

    def test_only_a_local_bootstrap_may_mint_the_first_admin_key(self):
        fresh = sqlite3.connect(':memory:', check_same_thread=False)
        apikeys.ensure_schema(fresh)
        self.addCleanup(fresh.close)
        manager = apikeys.ApiKeyManager(fresh)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            manager.bootstrap_admin(local_trusted=False, name='from-the-network')
        self.assertEqual(caught.exception.code, apikeys.E_PERMISSION)
        self.assertEqual(caught.exception.state, 'not_local')
        self.assertEqual(fresh.execute('SELECT count(*) FROM api_keys').fetchone()[0], 0)

    def test_a_read_only_key_cannot_promote_itself(self):
        reader = self.issue(name='reader', permissions=['read.results'])
        principal = self.manager.authenticate(reader.secret)
        for call in (lambda: self.manager.create(actor=principal, name='self', permissions=['admin.keys']),
                     lambda: self.manager.revoke(self.admin.key_id, actor=principal),
                     lambda: self.manager.rotate(self.admin.key_id, actor=principal),
                     lambda: self.manager.read_audit(actor=principal),
                     lambda: self.manager.list_keys(actor=principal)):
            with self.subTest(call=call):
                with self.assertRaises(apikeys.ApiKeyError) as caught:
                    call()
                self.assertEqual(caught.exception.code, apikeys.E_PERMISSION)

    def test_the_bootstrap_grant_is_read_and_admin_only(self):
        self.assertEqual(set(self.admin.info.permissions), apikeys.BOOTSTRAP_PERMISSIONS)
        for permission in ('collections.write', 'jobs.submit', 'export.create', 'export.secret'):
            self.assertNotIn(permission, self.admin.info.permissions)


class RotationWindowTests(KeyManagerTestCase):
    def test_a_window_is_an_explicit_per_key_setting(self):
        plain = self.issue(name='plain', permissions=['read.status'])
        self.assertIsNone(self.manager.get_key(plain.key_id).rotation_grace_until,
                          'a window is never a default')
        self.manager.rotate(plain.key_id, actor=self.admin.info.id, grace_s=0.0)
        self.assertIsNone(self.manager.get_key(plain.key_id).rotation_grace_until,
                          'grace_s = 0 means no window, not an empty one')
        self.now[0] += 100
        rotated = self.manager.rotate(plain.key_id, actor=self.admin.info.id, grace_s=60.0)
        self.assertEqual(rotated.info.rotation_grace_until, self.now[0] + 60.0)
        self.assertIsNone(self.manager.get_key(plain.key_id).revoked_at)
        self.manager.rotate(plain.key_id, actor=self.admin.info.id, grace_s=5.0)
        self.assertEqual(self.manager.get_key(plain.key_id).rotation_grace_until,
                         self.now[0] + 5.0, 'the next rotation replaces the window, not extends it')

    def test_a_narrow_window_lets_the_superseded_secret_through_and_then_closes(self):
        reader = self.issue(name='r', permissions=['read.status'])
        self.now[0] += 100
        rotated = self.manager.rotate(reader.key_id, actor=self.admin.info.id, grace_s=60.0)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.manager.authenticate(reader.secret)
        self.assertEqual(caught.exception.code, apikeys.E_ROTATION_GRACE,
                         'inside the window the old secret is named, not "unrecognised"')
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.manager.authenticate(reader.secret, permission='read.status')
        self.assertEqual(caught.exception.code, apikeys.E_ROTATION_GRACE,
                         'a management call refuses the superseded secret even inside the window')
        inside = self.manager.authenticate(reader.secret, allow_grace=True)
        self.assertTrue(inside.in_grace)
        self.now[0] += 61
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.manager.authenticate(reader.secret, allow_grace=True)
        self.assertEqual(caught.exception.code, apikeys.E_INVALID)
        self.assertTrue(self.manager.authenticate(rotated.secret))

    def test_a_superseded_verifier_is_collected_without_an_explicit_call(self):
        """The gap this closes: the window closed, the hash was still in the table."""
        reader = self.issue(name='r', permissions=['read.status'])
        rotated = self.manager.rotate(reader.key_id, actor=self.admin.info.id, grace_s=30.0)
        row = next(r for r in self.raw_rows() if r['id'] == reader.key_id)
        self.assertTrue(row['previous_verifier'], 'the window really holds a second verifier')
        self.now[0] += 120
        self.manager.authenticate(rotated.secret, allow_grace=True)
        row = next(r for r in self.raw_rows() if r['id'] == reader.key_id)
        self.assertIsNone(row['previous_verifier'],
                          'one ordinary request past the window must collect the hash')
        self.assertIsNone(row['rotation_grace_until'])
        self.assertEqual(self.manager.purge_expired_grace(), 0, 'nothing left for the sweeper')

    def test_revoke_and_rotate_also_collect_closed_windows(self):
        """A rotation collects what already ran out, and then opens its own window."""
        first = self.issue(name='a', permissions=['read.status'])
        self.manager.rotate(first.key_id, actor=self.admin.info.id, grace_s=10.0)
        stale = self.row(first.key_id)['previous_verifier']
        self.assertTrue(stale)
        self.now[0] += 30
        self.manager.rotate(first.key_id, actor=self.admin.info.id, grace_s=10.0)
        row = self.row(first.key_id)
        self.assertNotEqual(row['previous_verifier'], stale,
                            'the window that ran out was collected on the way in')
        self.assertEqual(row['rotation_grace_until'], self.now[0] + 10.0)

    def test_the_collector_is_idempotent(self):
        reader = self.issue(name='r', permissions=['read.status'])
        rotated = self.manager.rotate(reader.key_id, actor=self.admin.info.id, grace_s=10.0)
        self.now[0] += 20
        self.assertEqual(self.manager.purge_expired_grace(), 1)
        self.assertEqual(self.manager.purge_expired_grace(), 0)
        self.manager.authenticate(rotated.secret)
        self.assertEqual(self.manager.purge_expired_grace(), 0)

    def test_a_negative_window_is_refused(self):
        for value in (-1, 'soon', True):
            with self.subTest(value=value):
                with self.assertRaises(apikeys.ApiKeyError):
                    self.manager.rotate(self.admin.key_id, actor=self.admin.info.id, grace_s=value)


class QuotaAndStreamTests(KeyManagerTestCase):
    def test_rate_and_concurrency_are_enforced_with_a_code_and_an_action(self):
        limited = self.manager.create(actor=self.admin.info.id, name='l',
                                      permissions=['read.status'],
                                      rate_limit={'requests': 2, 'window_s': 60})
        principal = self.manager.authenticate(limited.secret)
        guard = apikeys.QuotaGuard(self.manager)
        guard.check(principal)
        guard.check(principal)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            guard.check(principal)
        self.assertEqual(caught.exception.code, apikeys.E_RATE_LIMITED)
        self.assertIsNotNone(caught.exception.retry_after)
        self.assertEqual(caught.exception.http_status, 429)
        self.now[0] += 61
        self.assertIsNone(guard.check(principal))

        holder = self.manager.create(actor=self.admin.info.id, name='c',
                                     permissions=['read.status'], concurrency={'max_active': 1})
        other = self.manager.authenticate(holder.secret)
        lease = guard.acquire(other)
        self.assertEqual(guard.active_count(holder.key_id), 1)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            guard.acquire(other)
        self.assertEqual(caught.exception.code, apikeys.E_CONCURRENCY)
        lease.close()
        self.assertEqual(guard.active_count(holder.key_id), 0)

    def test_a_lease_loses_the_slot_when_the_key_is_revoked(self):
        guard = apikeys.QuotaGuard(self.manager)
        lease = guard.acquire(self.manager.authenticate(self.admin.secret))
        self.manager.revoke(self.admin.key_id, actor=self.admin.key_id)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            lease.revalidate()
        self.assertEqual(caught.exception.code, apikeys.E_REVOKED)
        lease.close()
        self.assertEqual(guard.active_count(self.admin.key_id), 0)

    def test_an_open_stream_is_terminated_by_a_revoke_and_by_an_expiry(self):
        sessions = apikeys.StreamSessions(self.manager, recheck_interval_s=30.0)
        first = sessions.open(self.manager.authenticate(self.admin.secret), 's-1')
        self.assertEqual(sessions.active_ids(), ['s-1'])
        self.assertEqual(sessions.due(), [], 'nothing is due before the interval')
        self.now[0] += 31
        self.assertEqual([s.stream_id for s in sessions.due()], ['s-1'])
        self.manager.revoke(self.admin.key_id, actor=self.admin.key_id)
        self.assertEqual(sessions.sweep(), [('s-1', apikeys.E_REVOKED)])
        self.assertEqual(sessions.active_ids(), [])
        self.assertEqual(first.close_code, apikeys.E_REVOKED)

        short = self.manager.bootstrap_admin(local_trusted=True, name='b', ttl_s=100)
        second = sessions.open(self.manager.authenticate(short.secret), 's-2')
        self.now[0] += 200
        self.assertEqual(sessions.sweep(), [('s-2', apikeys.E_EXPIRED)])
        self.assertTrue(second.closed)

    def test_a_stream_rechecks_before_every_event_and_after_close_refuses(self):
        sessions = apikeys.StreamSessions(self.manager, recheck_interval_s=1.0)
        session = sessions.open(self.manager.authenticate(self.admin.secret), 's-1')
        self.now[0] += 5
        info = session.recheck()
        self.assertEqual(info.id, self.admin.key_id)
        session.close()
        with self.assertRaises(apikeys.ApiKeyError):
            session.recheck()


class ObjectScopeTests(KeyManagerTestCase):
    def scoped(self, *collections, **kwargs):
        return self.manager.create(actor=self.admin.info.id, name='s',
                                   permissions=['read.results', 'export.create'],
                                   scope={'collections': list(collections)}, **kwargs)

    def test_a_filter_narrows_and_never_widens(self):
        key = self.scoped('col-a')
        principal = self.manager.authenticate(key.secret)
        self.assertEqual(apikeys.visible_collections(principal), ['col-a'])
        self.assertEqual(apikeys.effective_collections(principal, ['col-a']), ['col-a'])
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            apikeys.effective_collections(principal, ['col-b'])
        self.assertEqual(caught.exception.code, apikeys.E_SCOPE)

    def test_a_foreign_and_a_missing_object_answer_the_same_way(self):
        key = self.scoped('col-a')
        principal = self.manager.authenticate(key.secret)
        self.assertTrue(apikeys.object_visible(principal, 'result', 'r1', collection_id='col-a'))
        self.assertFalse(apikeys.object_visible(principal, 'result', 'r1', collection_id='col-b'))
        self.assertFalse(apikeys.object_visible(principal, 'result', 'missing', collection_id='col-b'))
        self.assertFalse(apikeys.object_visible(principal, 'artifact', 'a1'),
                         'an artifact that names no collection stays invisible')

    def test_an_unscoped_key_is_visible_everywhere_its_rights_allow(self):
        key = self.manager.create(actor=self.admin.info.id, name='u',
                                  permissions=['read.results'])
        principal = self.manager.authenticate(key.secret)
        self.assertIsNone(apikeys.visible_collections(principal))
        self.assertIsNone(apikeys.visible_pools(principal))
        self.assertTrue(apikeys.object_visible(principal, 'result', 'r1', collection_id='col-z'))

    def test_a_secret_bearing_export_is_a_second_sensitive_right(self):
        key = self.manager.create(actor=self.admin.info.id, name='e',
                                  permissions=['export.create'])
        principal = self.manager.authenticate(key.secret)
        self.assertTrue(principal.require('export.create'))
        denied = principal.require('export.create', include_secrets=True,
                                   collection_id='col-a')
        self.assertFalse(denied)
        self.assertEqual(denied.code, apikeys.E_PERMISSION)
        with self.assertRaises(apikeys.ApiKeyError):
            self.manager.authenticate(key.secret, permission='export.create', include_secrets=True)
        with self.assertRaises(apikeys.ApiKeyError):
            self.manager.authenticate(key.secret, permission='export.secret')

    def test_a_right_is_still_bound_by_the_scope(self):
        key = self.scoped('col-a')
        principal = self.manager.authenticate(key.secret)
        self.assertTrue(principal.require('read.results', collection_id='col-a'))
        self.assertFalse(principal.require('read.results', collection_id='col-b'))

    def test_an_unknown_permission_is_refused_by_name(self):
        principal = self.manager.authenticate(self.admin.secret)
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            principal.require('read.everything')
        self.assertEqual(caught.exception.code, apikeys.E_UNKNOWN_FIELD)

    def test_every_documented_right_exists_and_admin_is_separate(self):
        for right in ('read.status', 'read.results', 'collections.read', 'collections.write',
                      'import.commit', 'profiles.write', 'sources.write', 'jobs.submit',
                      'jobs.control', 'pools.write', 'gateway.write', 'schedules.write',
                      'export.create', 'export.secret', 'admin.settings', 'admin.keys',
                      'admin.audit'):
            self.assertIn(right, apikeys.PERMISSIONS)
        self.assertFalse(apikeys.READ_PERMISSIONS & apikeys.WRITE_PERMISSIONS)
        self.assertFalse(apikeys.WRITE_PERMISSIONS & apikeys.ADMIN_PERMISSIONS)
        self.assertFalse(apikeys.SENSITIVE_PERMISSIONS & apikeys.WRITE_PERMISSIONS)


class AuditLogTests(KeyManagerTestCase):
    def test_a_refusal_and_a_success_are_both_recorded_with_a_cause(self):
        reader = self.issue(name='r', permissions=['read.results'])
        self.manager.authenticate(reader.secret, permission='read.results')
        with self.assertRaises(apikeys.ApiKeyError):
            self.manager.authenticate(reader.secret, permission='admin.keys')
        rows = self.manager.read_audit(actor=self.admin.info.id, limit=100)
        outcomes = {row['result'] for row in rows}
        self.assertIn('ok', outcomes)
        self.assertIn('denied', outcomes)
        denied = [row for row in rows if row['result'] == 'denied']
        self.assertTrue(denied)
        for row in denied:
            self.assertTrue(row['error_code'])

    def test_the_log_carries_the_key_the_operation_the_object_and_the_moment(self):
        reader = self.issue(name='r', permissions=['read.results'], scope={'collections': ['col-a']})
        self.manager.revoke(reader.key_id, actor=self.admin.info.id)
        rows = self.manager.read_audit(actor=self.admin.info.id, operation='key.revoke')
        self.assertTrue(rows)
        row = rows[0]
        self.assertEqual(sorted(row), sorted(('at', 'key_id', 'operation', 'object_kind',
                                              'object_id', 'scope', 'result', 'error_code')))
        self.assertEqual(row['object_id'], reader.key_id)
        self.assertEqual(row['key_id'], self.admin.key_id)
        self.assertEqual(row['object_kind'], 'api_key')
        self.assertIsInstance(row['at'], float)

    def test_no_full_key_no_password_and_no_body_ever_reach_the_log(self):
        issued = self.issue(name='r', permissions=['read.results'])
        rotated = self.manager.rotate(issued.key_id, actor=self.admin.info.id, grace_s=30.0)
        self.manager.authenticate(rotated.secret)
        rows = self.manager.read_audit(actor=self.admin.key_id, limit=500)
        rendered = json.dumps(rows, default=str)
        for needle in (issued.secret, self.admin.secret, 'password', 'secret"', 'warnings'):
            self.assertNotIn(needle, rendered, '%s reached the audit log' % needle)
        self.assertNotIn(b'pwk_', self.path.read_bytes())

    def test_reading_the_audit_log_is_itself_an_admin_right(self):
        reader = self.issue(name='r', permissions=['read.results'])
        with self.assertRaises(apikeys.ApiKeyError) as caught:
            self.manager.read_audit(actor=self.manager.authenticate(reader.secret))
        self.assertEqual(caught.exception.code, apikeys.E_PERMISSION)

    def test_the_log_is_bounded(self):
        manager = apikeys.ApiKeyManager(self.conn, now=lambda: self.now[0], audit_retention=5)
        for index in range(20):
            manager._audit(None, 'probe', result='ok')
        self.assertLessEqual(len(manager.read_audit(actor=self.admin.info.id, limit=100)), 5)


if __name__ == '__main__':
    unittest.main()

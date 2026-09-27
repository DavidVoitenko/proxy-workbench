"""Regression tests for API validation, concurrency, streams and scoped reads."""
from concurrent.futures import ThreadPoolExecutor
import http.client
from itertools import count
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from proxy_workbench import api, apiv1, db, jobs


class Keys(apiv1.KeyStore):
    def __init__(self, *, collections=(), concurrency=None):
        self.principal = apiv1.Principal(
            key_id='reader', kind='api_key', collections=collections,
            concurrency=concurrency, permissions=frozenset(apiv1.PERMISSIONS))

    def verify(self, secret):
        return self.principal if secret == 'test-key' else None

    def audit(self, record):
        pass


class Service(apiv1.Service):
    def __init__(self):
        self.calls = 0
        self.version = 1
        self.events = [{'seq': 1, 'type': 'job.note', 'data': {}}]
        self.started = threading.Event()
        self.release = threading.Event()

    def queue_state(self):
        return {'depth': 0, 'capacity': 10}

    def invoke(self, operation, call):
        if operation == 'results.list':
            return {'items': [{'version': self.version}], 'stream_id': 'results',
                    'next_seq': None}
        if operation == 'collections.create':
            self.calls += 1
            self.started.set()
            self.release.wait(2)
            return {'id': 'one', 'revision': 1}
        if operation == 'events.system':
            return {'items': self.events, 'stream_id': 'system'}
        if operation == 'exports.download':
            return {'data': b'proxy-list\n', 'filename': 'proxies.txt',
                    'content_type': 'text/plain; charset=utf-8'}
        raise NotImplementedError(operation)


def request(method, path, *, query='', body=None, idem=None):
    headers = {'Host': '127.0.0.1', 'Authorization': 'Bearer test-key'}
    if idem is not None:
        headers['Idempotency-Key'] = idem
    if body is not None:
        headers['Content-Type'] = 'application/json'
    return apiv1.Request(method, path, query=query, headers=headers,
                         body=json.dumps(body).encode() if body is not None else b'')


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.control = apiv1.ApiV1(self.service, Keys())

    def test_nonfinite_numbers_and_invalid_cursor_return_validation_errors(self):
        for query in ('profile_revision=nan', 'max_latency_ms=inf',
                      'min_mbps=nan', 'cursor=____', 'cursor=%21%21%21%21'):
            with self.subTest(query=query):
                answer = self.control.handle(request('GET', '/v1/results', query=query))
                self.assertEqual(answer.status_code, 400, answer.body)
        answer = self.control.handle(request(
            'POST', '/v1/checks/quick-test', body={'endpoint': 'http://192.0.2.1:80',
                                                   'max_seconds': float('nan')}, idem='nan'))
        self.assertEqual(answer.status_code, 400, answer.body)

    def test_remote_bind_needs_explicit_network_configuration(self):
        with self.assertRaises(ValueError):
            apiv1.ApiV1(self.service, Keys(), apiv1.ApiConfig(
                host='0.0.0.0', allowed_hosts=('service.example',)))

    def test_read_is_not_cached_by_a_mutation_idempotency_header(self):
        first = self.control.handle(request('GET', '/v1/results', idem='read'))
        self.service.version = 2
        second = self.control.handle(request('GET', '/v1/results', idem='read'))
        self.assertEqual(first.json()['items'][0]['version'], 1)
        self.assertEqual(second.json()['items'][0]['version'], 2)

    def test_allowed_browser_origin_can_read_streams_and_artifacts(self):
        origin = 'https://app.example'
        self.control = apiv1.ApiV1(
            self.service, Keys(), apiv1.ApiConfig(allowed_origins=(origin,),
                                                   cors_origins=(origin,)))
        for path in ('/v1/events', '/v1/exports/example/download/proxies.txt'):
            with self.subTest(path=path):
                call = request('GET', path)
                call = apiv1.Request(call.method, call.path, call.query,
                                     {**call.headers, 'Origin': origin}, call.body)
                answer = self.control.handle(call)
                self.assertEqual(answer.status_code, 200, answer.body)
                self.assertEqual(dict(answer.headers)['Access-Control-Allow-Origin'], origin)
                if answer.stream is not None:
                    answer.stream.close()

    def test_malformed_service_event_cannot_break_or_inject_stream_frames(self):
        stream = apiv1.EventStream('system', [
            {'seq': 1, 'type': 'job.note\ndata: forged', 'at': float('nan')},
            {'seq': 'invalid', 'type': 'job.note'},
        ], lambda: 123.0)
        frames = list(stream)
        self.assertEqual(len(frames), 2)
        self.assertIn('event: job.notedata: forged\n', frames[0])
        self.assertNotIn('\ndata: forged\n', frames[0])
        self.assertIn('"at": 123.0', frames[0])
        self.assertIn('event: error\n', frames[1])

    def test_synthetic_stream_close_does_not_advance_resume_cursor(self):
        self.service.events = [
            {'seq': 1, 'type': 'job.note', 'data': {}},
            {'seq': 2, 'type': 'job.note', 'data': {}},
        ]
        response = self.control.handle(request('GET', '/v1/events', query='limit=1'))
        frames = list(response.stream)
        self.assertEqual(len(frames), 2)
        self.assertIn('id: ', frames[0])
        self.assertIn('event: stream.closed\n', frames[1])
        self.assertNotIn('id: ', frames[1])

    def test_concurrent_replay_executes_a_mutation_once(self):
        call = request('POST', '/v1/collections', body={'name': 'one'}, idem='same')
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.control.handle, call)
            self.assertTrue(self.service.started.wait(2))
            second = pool.submit(self.control.handle, call)
            deadline = time.monotonic() + 2
            while self.control.stats['requests'] < 2 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(self.control.stats['requests'], 2)
            time.sleep(0.05)
            self.service.release.set()
            answers = (first.result(3), second.result(3))
        self.assertEqual([answer.status_code for answer in answers], [200, 200])
        self.assertEqual(self.service.calls, 1)

    def test_unrelated_keys_do_not_share_a_hash_stripe(self):
        store = apiv1.IdempotencyStore(60, 100, time.monotonic)
        bucket = ('reader', 'POST', '/v1/collections')
        first = 'first'
        stripe = hash((bucket, first)) % 64
        second = next(f'other-{index}' for index in count()
                      if hash((bucket, f'other-{index}')) % 64 == stripe)
        acquired = threading.Event()

        def enter_second():
            with store.serialized(bucket, second):
                acquired.set()

        with store.serialized(bucket, first):
            thread = threading.Thread(target=enter_second)
            thread.start()
            self.assertTrue(acquired.wait(2), 'different keys must run independently')
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(store._flights)

    def test_head_event_stream_releases_key_slot_and_sends_no_body(self):
        control = apiv1.ApiV1(self.service, Keys(concurrency=1),
                              apiv1.ApiConfig(port=0))
        server = control.make_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            headers = {'Authorization': 'Bearer test-key'}
            conn.request('HEAD', '/v1/events', headers=headers)
            head = conn.getresponse()
            self.assertEqual(head.status, 200)
            self.assertEqual(head.read(), b'')
            deadline = time.monotonic() + 2
            while control.key_quota.active('reader') and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(control.key_quota.active('reader'), 0)
            conn.request('GET', '/v1/events', headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'event: job.note', response.read())
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


class ScopedJobReadsTests(unittest.TestCase):
    def test_retry_key_cannot_return_an_unrelated_job(self):
        conn = sqlite3.connect(':memory:')
        try:
            jobs.install_schema(conn)
            store = jobs.JobStore(conn)
            scope = jobs.Scope('mine', 'profile', 1)
            first = store.submit('scan', scope, [jobs.QueueItem('endpoint-one')])
            second = store.submit('scan', scope, [jobs.QueueItem('endpoint-two')])
            retried = store.retry(first.id, idempotency_key='retry-key')
            self.assertEqual(store.retry(first.id, idempotency_key='retry-key').id,
                             retried.id)
            with self.assertRaises(jobs.IdempotencyConflict):
                store.retry(second.id, idempotency_key='retry-key')
        finally:
            conn.close()

    def test_combined_server_head_event_stream_releases_key_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manager = api.key_manager(home)
            try:
                secret = manager.bootstrap_admin(
                    local_trusted=True, permissions=sorted(apiv1.PERMISSIONS),
                    concurrency={'max_active': 1}).secret
            finally:
                manager.conn.close()
            server = api.make_api_server(home, '127.0.0.1', 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                conn.request('HEAD', '/v1/events', headers={'Authorization': f'Bearer {secret}'})
                head = conn.getresponse()
                self.assertEqual(head.status, 200)
                self.assertEqual(head.read(), b'')
                conn.close()
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                conn.request('GET', '/v1/events', headers={'Authorization': f'Bearer {secret}'})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)

    def test_scope_applies_before_list_and_event_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            db.migrate(home)
            conn = db.connect(home)
            try:
                store = jobs.JobStore(conn)
                foreign = store.submit('scan', jobs.Scope('foreign', 'profile', 1))
                for number in range(25):
                    store.emit(foreign.id, 'job.note', data={'number': number})
                own = store.submit('scan', jobs.Scope('mine', 'profile', 1))
                another = store.submit('scan', jobs.Scope('mine', 'profile', 1))
            finally:
                conn.close()
            control = apiv1.ApiV1(api.WorkbenchService(home), Keys(collections=('mine',)))
            listed = control.handle(request('GET', '/v1/jobs', query='limit=1'))
            self.assertEqual(listed.status_code, 200, listed.body)
            first_ids = [item['id'] for item in listed.json()['items']]
            self.assertEqual(len(first_ids), 1)
            self.assertEqual(listed.json()['total'], 2)
            cursor = listed.json()['next_cursor']
            self.assertTrue(cursor)
            second_page = control.handle(request(
                'GET', '/v1/jobs', query=f'limit=1&cursor={cursor}'))
            self.assertEqual(second_page.status_code, 200, second_page.body)
            second_ids = [item['id'] for item in second_page.json()['items']]
            self.assertEqual(set(first_ids + second_ids), {own.id, another.id})
            self.assertIsNone(second_page.json()['next_cursor'])
            streamed = control.handle(request('GET', '/v1/events', query='limit=1'))
            self.assertEqual(streamed.status_code, 200, streamed.body)
            frames = list(streamed.stream)
            self.assertIn(own.id, ''.join(frames))
            self.assertNotIn(foreign.id, ''.join(frames))
            first_seq = json.loads(frames[0].split('data: ', 1)[1])['seq']
            event_cursor = frames[0].split('id: ', 1)[1].split('\n', 1)[0]
            resumed = control.handle(request(
                'GET', '/v1/events', query=f'limit=1&cursor={event_cursor}'))
            self.assertEqual(resumed.status_code, 200, resumed.body)
            resumed_frames = list(resumed.stream)
            next_seq = json.loads(resumed_frames[0].split('data: ', 1)[1])['seq']
            self.assertEqual(next_seq, first_seq + 1)


if __name__ == '__main__':
    unittest.main()

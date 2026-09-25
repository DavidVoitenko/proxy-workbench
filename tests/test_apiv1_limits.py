"""Limits, events and the bounded state of /v1: body, queue, concurrency, rate,
idempotency, event history and the event stream itself."""
import json
from pathlib import Path
import sys
import threading
import time
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import apiv1
from proxy_workbench.apiv1 import ApiV1, Request

KEY = 'limit-test-key'


class Keys(apiv1.KeyStore):
    def __init__(self, revoke_after=None):
        self.calls = 0
        self.revoke_after = revoke_after
        self.audit_log = []

    def verify(self, secret):
        if secret != KEY:
            return None
        self.calls += 1
        if self.revoke_after is not None and self.calls > self.revoke_after:
            return None
        return apiv1.Principal(key_id='k-1', permissions=frozenset(apiv1.PERMISSIONS))

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


class Service(apiv1.Service):
    def __init__(self, depth=0, capacity=10, events=None, block=None):
        self.depth = depth
        self.capacity = capacity
        self.events = events or []
        self.block = block
        self.submitted = []

    def queue_state(self):
        return {'depth': self.depth, 'capacity': self.capacity}

    def invoke(self, operation, call):
        if operation == 'checks.check':
            self.submitted.append(call)
            if self.block is not None:
                self.block.wait(3)
            return {'job_id': 'job-1', 'state': 'queued'}
        if operation == 'jobs.events':
            cursor_seq = call.query.get('cursor_seq') or 0
            return (f"job:{call.params['id']}",
                    [event for event in self.events if event['seq'] > cursor_seq])
        if operation == 'events.system':
            return ('system', list(self.events))
        return {'operation': operation, 'items': [], 'stream_id': operation}


def request(method, path, key=KEY, body=None, query='', **headers):
    head = {'Host': '127.0.0.1', 'Authorization': f'Bearer {key}'}
    if method != 'GET':
        head.setdefault('Idempotency-Key', 'i-1')
        head['Content-Type'] = 'application/json'
    head.update(headers)
    return Request(method, path, query=query, headers=head,
                   body=json.dumps(body).encode() if body is not None else b'')


def events(count, start=1):
    return [{'seq': seq, 'type': 'item.observation', 'job_id': 'job-1', 'item_id': f'i{seq}',
             'code': None, 'data': {'endpoint_id': f'e{seq}'}} for seq in range(start, start + count)]


class LimitTests(unittest.TestCase):
    def test_body_limit_is_enforced_before_the_service_is_called(self):
        service = Service()
        api = ApiV1(service, Keys(), apiv1.ApiConfig(max_body_bytes=64))
        small = api.handle(request('POST', '/v1/collections', body={'name': 'ok'}))
        self.assertEqual(small.status_code, 200)
        big = api.handle(request('POST', '/v1/collections', body={'name': 'x' * 500}))
        self.assertEqual(big.status_code, 413)
        self.assertEqual(big.json()['error']['code'], 'E_LIMIT_BODY')
        self.assertEqual(big.json()['error']['details']['max_body_bytes'], 64)

    def test_queue_limit_refuses_a_new_job_with_retry_after(self):
        service = Service(depth=10, capacity=10)
        api = ApiV1(service, Keys(), apiv1.ApiConfig(max_queue_depth=10))
        response = api.handle(request('POST', '/v1/checks/check', body={'collection_id': 'c1'}))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['error']['code'], 'E_LIMIT_QUEUE')
        self.assertEqual(dict(response.headers)['Retry-After'], '5')
        self.assertEqual(service.submitted, [])

    def test_concurrency_is_bounded_and_released(self):
        block = threading.Event()
        service = Service(block=block)
        api = ApiV1(service, Keys(), apiv1.ApiConfig(max_concurrency=1))
        answers = {}

        def run(name):
            answers[name] = api.handle(request('POST', '/v1/checks/check',
                                               body={'collection_id': 'c1'}, **{'Idempotency-Key': name}))

        first = threading.Thread(target=run, args=('a',))
        first.start()
        for _ in range(200):
            if api.slots._used:
                break
            time.sleep(0.005)
        second = api.handle(request('POST', '/v1/checks/check', body={'collection_id': 'c1'},
                                    **{'Idempotency-Key': 'b'}))
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()['error']['code'], 'E_LIMIT_CONCURRENCY')
        block.set()
        first.join(3)
        self.assertEqual(answers['a'].status_code, 202)
        self.assertEqual(api.slots._used, 0)
        self.assertEqual(api.handle(request('POST', '/v1/checks/check', body={'collection_id': 'c1'},
                                            **{'Idempotency-Key': 'c'})).status_code, 202)

    def test_rate_limit_gives_429_with_retry_after(self):
        api = ApiV1(Service(), Keys(), apiv1.ApiConfig(rate_limit=(2, 60)))
        for _ in range(2):
            self.assertEqual(api.handle(request('GET', '/v1/results')).status_code, 200)
        limited = api.handle(request('GET', '/v1/results'))
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()['error']['code'], 'E_AUTH_RATE_LIMITED')
        retry_after = dict(limited.headers)['Retry-After']
        self.assertGreaterEqual(int(retry_after), 1)
        self.assertLessEqual(int(retry_after), 60)
        # another route has its own budget, and the limit is per identity
        self.assertEqual(api.handle(request('GET', '/v1/service')).status_code, 200)

    def test_idempotency_records_are_bounded_and_expire(self):
        clock = [1000.0]
        store = apiv1.IdempotencyStore(ttl_s=10, maximum=2, clock=lambda: clock[0])
        store.put(('k', 'POST', '/x'), 'i-1', 'd1', 'answer-1')
        self.assertEqual(store.get(('k', 'POST', '/x'), 'i-1', 'd1'), 'answer-1')
        with self.assertRaises(apiv1.ApiError) as caught:
            store.get(('k', 'POST', '/x'), 'i-1', 'other-digest')
        self.assertEqual(caught.exception.code, 'E_CONFLICT_IDEMPOTENCY')
        store.put(('k', 'POST', '/x'), 'i-2', 'd2', 'answer-2')
        store.put(('k', 'POST', '/x'), 'i-3', 'd3', 'answer-3')
        self.assertEqual(len(store._records), 2)
        self.assertIsNone(store.get(('k', 'POST', '/x'), 'i-1', 'd1'))
        clock[0] += 100
        self.assertIsNone(store.get(('k', 'POST', '/x'), 'i-2', 'd2'))

    def test_a_broken_body_is_a_documented_error_not_a_traceback(self):
        api = ApiV1(Service(), Keys())
        broken = api.handle(Request('POST', '/v1/collections',
                                    headers={'Host': '127.0.0.1',
                                             'Authorization': f'Bearer {KEY}',
                                             'Idempotency-Key': 'x', 'Content-Type': 'application/json'},
                                    body=b'{not json'))
        self.assertEqual(broken.status_code, 400)
        self.assertEqual(broken.json()['error']['code'], 'E_VALIDATION_SCHEMA')
        wrong_type = api.handle(Request('POST', '/v1/collections',
                                        headers={'Host': '127.0.0.1',
                                                 'Authorization': f'Bearer {KEY}',
                                                 'Idempotency-Key': 'x',
                                                 'Content-Type': 'text/plain'},
                                        body=b'name=x'))
        self.assertEqual(wrong_type.status_code, 400)
        self.assertEqual(wrong_type.json()['error']['details']['content_type'], 'text/plain')
        not_an_object = api.handle(Request('POST', '/v1/collections',
                                           headers={'Host': '127.0.0.1',
                                                    'Authorization': f'Bearer {KEY}',
                                                    'Idempotency-Key': 'x',
                                                    'Content-Type': 'application/json'},
                                           body=b'[1,2]'))
        self.assertEqual(not_an_object.json()['error']['code'], 'E_VALIDATION_SCHEMA')


class EventStreamTests(unittest.TestCase):
    def test_events_carry_the_documented_frame_and_a_cursor(self):
        api = ApiV1(Service(events=events(3)), Keys())
        response = api.handle(request('GET', '/v1/jobs/job-1/events', query='limit=10'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, 'text/event-stream; charset=utf-8')
        frames = list(response.stream)
        self.assertEqual(len(frames), 3)
        self.assertTrue(frames[0].startswith('id: '))
        self.assertIn('event: item.observation', frames[0])
        event = json.loads(frames[0].split('data: ', 1)[1])
        self.assertEqual(set(event), {'stream', 'seq', 'at', 'type', 'job_id', 'item_id',
                                      'code', 'data'})
        self.assertEqual(event['stream'], 'job:job-1')
        self.assertEqual(event['data']['endpoint_id'], 'e1')

    def test_resume_yields_only_newer_events(self):
        api = ApiV1(Service(events=events(5)), Keys())
        first = api.handle(request('GET', '/v1/jobs/job-1/events'))
        frames = list(first.stream)
        cursor = frames[1].split('id: ', 1)[1].split('\n', 1)[0]
        again = api.handle(request('GET', '/v1/jobs/job-1/events', query=f'cursor={cursor}'))
        self.assertEqual(again.status_code, 200)
        resumed = list(again.stream)
        self.assertEqual([json.loads(f.split('data: ', 1)[1])['seq'] for f in resumed], [3, 4, 5])

    def test_history_is_bounded_and_an_evicted_cursor_is_refused(self):
        api = ApiV1(Service(events=events(5)), Keys(), apiv1.ApiConfig(max_event_history=2))
        frames = list(api.handle(request('GET', '/v1/jobs/job-1/events')).stream)
        first = frames[0].split('id: ', 1)[1].split('\n', 1)[0]
        kept = frames[-1].split('id: ', 1)[1].split('\n', 1)[0]
        evicted = api.handle(request('GET', '/v1/jobs/job-1/events', query=f'cursor={first}'))
        self.assertEqual(evicted.status_code, 409)
        self.assertEqual(evicted.json()['error']['code'], 'E_CONFLICT_REVISION')
        self.assertEqual(evicted.json()['error']['details']['max_event_history'], 2)
        self.assertEqual(api.handle(request('GET', '/v1/jobs/job-1/events',
                                            query=f'cursor={kept}')).status_code, 200)
        wrong_stream = api.handle(request('GET', '/v1/jobs/job-1/events',
                                          query=f'cursor={apiv1.encode_cursor("job:other", 2)}'))
        self.assertEqual(wrong_stream.status_code, 400)

    def test_stream_limit_closes_the_stream_instead_of_never_ending(self):
        api = ApiV1(Service(events=events(50)), Keys())
        frames = list(api.handle(request('GET', '/v1/jobs/job-1/events', query='limit=3')).stream)
        self.assertEqual(len(frames), 4)
        closing = json.loads(frames[-1].split('data: ', 1)[1])
        self.assertEqual(closing['type'], 'stream.closed')
        self.assertEqual(closing['code'], 'E_LIMIT_BUDGET')

    def test_a_revoked_key_closes_an_open_stream(self):
        keys = Keys(revoke_after=1)
        api = ApiV1(Service(events=events(4)), keys, apiv1.ApiConfig(sse_reverify_s=0.0))
        response = api.handle(request('GET', '/v1/jobs/job-1/events'))
        self.assertEqual(response.status_code, 200)
        frames = list(response.stream)
        self.assertEqual(len(frames), 1)
        closed = json.loads(frames[0].split('data: ', 1)[1])
        self.assertEqual(closed['type'], 'session.closed')
        self.assertEqual(closed['code'], 'E_AUTH_INVALID')

    def test_events_need_a_key(self):
        api = ApiV1(Service(events=events(2)), Keys())
        response = api.handle(Request('GET', '/v1/jobs/job-1/events', headers={'Host': '127.0.0.1'}))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'E_AUTH_MISSING')


class HttpServerTests(unittest.TestCase):
    """The same pipeline over a real socket, including the chunked event stream."""

    def setUp(self):
        self.api = ApiV1(Service(events=events(3)), Keys(),
                         apiv1.ApiConfig(host='127.0.0.1', port=0))
        self.server = self.api.make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.server.server_port}',
                                   trust_env=False, timeout=5,
                                   headers={'Authorization': f'Bearer {KEY}'})

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def test_an_oversized_body_is_refused_over_http_too(self):
        api = ApiV1(Service(), Keys(), apiv1.ApiConfig(port=0, max_body_bytes=200))
        server = api.make_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',
                              trust_env=False, timeout=5,
                              headers={'Authorization': f'Bearer {KEY}'}) as client:
                response = client.post('/v1/collections',
                                       content=b'{"name": "' + b'x' * 5000 + b'"}',
                                       headers={'Idempotency-Key': 'big-1',
                                                'Content-Type': 'application/json'})
                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.json()['error']['code'], 'E_LIMIT_BODY')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_plain_requests_and_headers(self):
        response = self.client.get('/v1/results')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Workbench-Api-Version'], '1.0.0')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.json()['operation'], 'results.list')
        etag = response.headers['ETag']
        self.assertEqual(self.client.get('/v1/results', headers={'If-None-Match': etag}).status_code, 304)
        created = self.client.post('/v1/collections', json={'name': 'via http'},
                                   headers={'Idempotency-Key': 'http-1'})
        self.assertEqual(created.status_code, 200)
        self.assertEqual(self.client.post('/v1/collections', json={'name': 'x'}).status_code, 400)
        self.assertEqual(self.client.get('/v1/version', headers={'Host': 'evil.example'}).status_code, 403)

    def test_event_stream_over_the_socket(self):
        with self.client.stream('GET', '/v1/jobs/job-1/events') as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers['content-type'].startswith('text/event-stream'))
            self.assertEqual(response.headers['transfer-encoding'], 'chunked')
            text = ''.join(response.iter_text())
        self.assertEqual(text.count('event: item.observation'), 3)
        seqs = [json.loads(chunk.split('data: ', 1)[1])['seq']
                for chunk in text.split('\n\n') if 'data: ' in chunk]
        self.assertEqual(seqs, [1, 2, 3])


if __name__ == '__main__':
    unittest.main()

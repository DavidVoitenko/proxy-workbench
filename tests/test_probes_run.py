"""Probe execution: attempts, redirects, assertions and stage attribution.

Everything runs against a local fake transport: no socket is opened and no
public proxy or target is contacted.
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import probes as pr

BODY = b'{"data": {"status": "ok", "items": [{"id": 7}]}, "host": "svc.invalid"}' + b' ' * 200


def transport(*responses):
    """Answers with the given responses in order; the last one repeats."""
    queue = list(responses)
    state = {'index': 0}
    calls = []

    async def send(request, *, options):
        calls.append(request)
        position = min(state['index'], len(queue) - 1)
        state['index'] += 1
        item = queue[position]
        if callable(item):
            item = item(request)
        return await item if hasattr(item, '__await__') else item

    return SimpleNamespace(send=send, calls=calls)


def routing(mapping, default=None):
    """A transport that picks the answer by request URL."""
    async def send(request, *, options):
        for fragment, response in mapping.items():
            if fragment in request.url:
                return response
        return default

    return SimpleNamespace(send=send, calls=[])


def ok(body=BODY, status=200, headers=(('content-type', 'application/json'),), url='https://svc.invalid/health'):
    return pr.ProbeResponse(status=status, headers=headers, body=body, url=url, ttfb_ms=11.0,
                            transfer_ms=9.0, total_ms=20.0, connect_ms=4.0, handshake_ms=3.0)


def target(**extra):
    base = {'id': 'svc', 'name': 'svc', 'url': 'https://svc.invalid/health', 'statuses': [200],
            'content_type': 'application/json', 'min_body_bytes': 8,
            'json_assertions': [{'path': 'data.status', 'op': 'equals', 'value': 'ok'}]}
    base.update(extra)
    return pr.validate_target(base)


def options(**extra):
    return pr.validate_options({'attempts': 1, 'backoff_s': 0, **extra})


class AssertionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_matching_answer_passes(self):
        result = await pr.run_probe(target(), options(), transport(ok()))
        self.assertTrue(result.ok)
        self.assertEqual(result.code, None)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.bytes, len(BODY))
        self.assertEqual(result.ttfb_ms, 11.0)
        self.assertEqual(result.transfer_ms, 9.0)

    async def test_each_assertion_has_its_own_code(self):
        cases = [
            (pr.ProbeResponse(status=503, body=BODY), f'HTTP_503'),
            (pr.ProbeResponse(status=200, body=BODY, headers=(('content-type', 'text/html'),)), pr.CONTENT_TYPE),
            (pr.ProbeResponse(status=200, body=b'x', headers=(('content-type', 'application/json'),)),
             pr.BODY_TOO_SMALL),
            (pr.ProbeResponse(status=200, body=BODY, headers=(('content-type', 'application/json'),)),
             pr.BODY_TOO_LARGE),
            (pr.ProbeResponse(status=200, body=BODY.replace(b'"ok"', b'"no"'),
                              headers=(('content-type', 'application/json'),)), pr.JSON_ASSERT),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected):
                probe = target(max_body_bytes=len(BODY) - 1 if expected == pr.BODY_TOO_LARGE else 1_000_000)
                result = await pr.run_probe(probe, options(), transport(response))
                self.assertFalse(result.ok)
                self.assertEqual(result.code, expected)

    async def test_contains_not_contains_and_hash(self):
        self.assertTrue((await pr.run_probe(target(contains='svc.invalid'),
                                             options(), transport(ok()))).ok)
        self.assertEqual((await pr.run_probe(target(contains='absent'), options(),
                                             transport(ok()))).code, pr.CONTENT_MISMATCH)
        self.assertEqual((await pr.run_probe(target(not_contains='svc.invalid'), options(),
                                             transport(ok()))).code, pr.CONTENT_MISMATCH)
        import hashlib
        good_hash = hashlib.sha256(BODY).hexdigest()
        self.assertTrue((await pr.run_probe(target(sha256=good_hash), options(), transport(ok()))).ok)
        self.assertEqual((await pr.run_probe(target(sha256='0' * 64), options(),
                                             transport(ok()))).code, pr.HASH_MISMATCH)

    async def test_json_assertion_operators(self):
        body = BODY
        cases = [
            ({'path': 'data.items.0.id', 'op': 'type', 'value': 'number'}, True),
            ({'path': 'data.items.0.id', 'op': 'min', 'value': 5}, True),
            ({'path': 'data.items.0.id', 'op': 'max', 'value': 5}, False),
            ({'path': 'data.items', 'op': 'contains', 'value': {'id': 7}}, True),
            ({'path': 'host', 'op': 'contains', 'value': 'svc.invalid'}, True),
            ({'path': 'data.items.0.id', 'op': 'contains', 'value': 7}, False),
            ({'path': 'data.missing', 'op': 'not_equals', 'value': 'x'}, True),
            ({'path': 'data.missing', 'op': 'exists'}, False),
        ]
        for assertion, expected in cases:
            with self.subTest(assertion=assertion):
                result = await pr.run_probe(target(json_assertions=[assertion]), options(), transport(ok(body)))
                self.assertEqual(result.ok, expected)

    async def test_a_non_json_body_fails_the_json_assertion(self):
        result = await pr.run_probe(target(), options(),
                                    transport(ok(b'<html>captcha</html>' + b' ' * 200)))
        self.assertEqual(result.code, pr.JSON_ASSERT)
        self.assertIn('JSON', result.detail)

    async def test_transport_failures_keep_their_stage(self):
        result = await pr.run_probe(target(), options(), transport(pr.ProbeResponse(code=pr.UNREACHABLE, stage='tcp')))
        self.assertEqual(result.code, pr.UNREACHABLE)
        self.assertEqual(result.stage, 'tcp')
        self.assertFalse(result.ok)

    async def test_an_unexpected_transport_exception_is_captured(self):
        def boom(request):
            raise ValueError('proxy exploded')

        result = await pr.run_probe(target(), options(), transport(boom))
        self.assertEqual(result.code, 'ValueError')
        self.assertIn('ValueError', result.detail)

    async def test_outcome_never_carries_a_body(self):
        result = await pr.run_probe(target(), options(), transport(ok()))
        text = json.dumps(result.to_public())
        self.assertNotIn('svc.invalid', text.split('"url"')[0])
        self.assertNotIn('"body"', text)


class RedirectTests(unittest.IsolatedAsyncioTestCase):
    def redirect(self, location, status=302):
        return pr.ProbeResponse(status=status, headers=(('location', location),), body=b'')

    async def test_redirects_are_followed_up_to_the_limit(self):
        probe = target(max_redirects=2)
        wire = transport(self.redirect('https://svc.invalid/next'), self.redirect('/final'), ok())
        result = await pr.run_probe(probe, options(), wire)
        self.assertTrue(result.ok)
        self.assertEqual([request.url for request in wire.calls],
                         ['https://svc.invalid/health', 'https://svc.invalid/next', 'https://svc.invalid/final'])

    async def test_a_redirect_loop_stops(self):
        probe = target(max_redirects=3)
        wire = transport(self.redirect('https://svc.invalid/health'))
        result = await pr.run_probe(probe, options(), wire)
        self.assertEqual(result.code, pr.REDIRECT_LOOP)

    async def test_too_many_redirects_stop(self):
        probe = target(max_redirects=1)
        wire = transport(self.redirect('/a'), self.redirect('/b'), ok())
        result = await pr.run_probe(probe, options(), wire)
        self.assertEqual(result.code, pr.REDIRECT_TOO_MANY)
        self.assertEqual(len(wire.calls), 2)

    async def test_a_redirect_is_not_followed_by_default(self):
        result = await pr.run_probe(target(), options(), transport(self.redirect('/other'), ok()))
        self.assertEqual(result.code, pr.REDIRECT_TOO_MANY)

    async def test_a_redirect_without_location_fails_the_assertion(self):
        result = await pr.run_probe(target(max_redirects=2),
                                    options(), transport(pr.ProbeResponse(status=302, body=b'')))
        self.assertEqual(result.code, pr.CONTENT_MISMATCH)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_transport_failure_is_retried(self):
        wire = transport(pr.ProbeResponse(code=pr.CONNECT_TIMEOUT, stage='tcp'), ok())
        result = await pr.run_probe(target(), options(attempts=3, backoff_s=0), wire)
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(wire.calls), 2)

    async def test_a_failed_assertion_is_not_retried(self):
        wire = transport(ok(b'short'))
        result = await pr.run_probe(target(), options(attempts=3, backoff_s=0), wire)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(wire.calls), 1)

    async def test_backoff_is_waited_between_attempts(self):
        async def slow_failure(request):
            if len(wire.calls) < 2:
                return pr.ProbeResponse(code=pr.CONNECT_TIMEOUT, stage='tcp')
            return ok()

        wire = transport(slow_failure)
        start = asyncio.get_running_loop().time()
        result = await pr.run_probe(target(), options(attempts=3, backoff_s=0.05, backoff_factor=2.0), wire)
        elapsed = asyncio.get_running_loop().time() - start
        self.assertTrue(result.ok)
        self.assertGreaterEqual(elapsed, 0.05)
        self.assertLess(elapsed, 0.5)

    async def test_the_whole_probe_deadline_covers_every_attempt(self):
        async def hang(request):
            await asyncio.sleep(5)
            return ok()

        wire = transport(hang)
        result = await pr.run_probe(target(),
                                    options(attempts=3, backoff_s=0.05, connect_timeout_s=0.1,
                                            handshake_timeout_s=0.1, read_timeout_s=0.1,
                                            whole_probe_timeout_s=0.5),
                                    wire)
        self.assertEqual(result.code, pr.WHOLE_PROBE_TIMEOUT)
        self.assertEqual(result.attempts, 1)

    async def test_backoff_that_does_not_fit_the_deadline_stops_early(self):
        wire = transport(pr.ProbeResponse(code=pr.CONNECT_TIMEOUT, stage='tcp'))
        result = await pr.run_probe(target(),
                                    options(attempts=3, backoff_s=5, backoff_factor=1, backoff_max_s=5,
                                            connect_timeout_s=0.1, handshake_timeout_s=0.1, read_timeout_s=0.1,
                                            whole_probe_timeout_s=0.5),
                                    wire)
        self.assertEqual(result.code, pr.WHOLE_PROBE_TIMEOUT)
        self.assertEqual(len(wire.calls), 1)


class StageTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_tcp_stage_reports_the_address_it_looked_at(self):
        wire = transport(pr.ProbeResponse())
        result = await pr.run_stage('tcp', 'http://11.0.0.1:8080', options(), wire)
        self.assertTrue(result.ok)
        self.assertEqual(result.url, 'http://11.0.0.1:8080')
        self.assertEqual(wire.calls[0].stage, 'tcp')
        self.assertEqual(wire.calls[0].max_bytes, 0)

    async def test_a_closed_port_fails_with_its_stage(self):
        wire = transport(pr.ProbeResponse(code=pr.UNREACHABLE, stage='tcp'))
        result = await pr.run_stage('handshake', 'socks5://11.0.0.1:1080', options(attempts=2, backoff_s=0), wire)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, 'tcp')
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(wire.calls), 2)

    async def test_a_target_stage_cannot_be_asked_for(self):
        with self.assertRaises(pr.ProbeError):
            await pr.run_stage('target', 'http://11.0.0.1:8080', options(), transport(ok()))


class PlanRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_fail_fast_stops_after_the_first_success(self):
        plan = pr.build_plan({'mode': 'basic', 'options': {'attempts': 1}})
        wire = transport(ok(b'Example Domain ' + b' ' * 200, headers=(('content-type', 'text/html'),)))
        outcome = await pr.run_plan(plan, wire, endpoint='http://11.0.0.1:8080')
        self.assertTrue(outcome.ok)
        self.assertEqual(len(wire.calls), 1)

    async def test_basic_falls_back_to_the_next_probe(self):
        plan = pr.build_plan({'mode': 'basic', 'options': {'attempts': 1}})
        first = pr.ProbeResponse(status=404, body=b'nope')
        wire = transport(first, pr.ProbeResponse(status=200, body=b'Example Domain ' + b' ' * 200,
                                                headers=(('content-type', 'text/html'),)))
        outcome = await pr.run_plan(plan, wire, endpoint='http://11.0.0.1:8080')
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.successes, 1)
        self.assertEqual(len(outcome.targets), 2)
        self.assertEqual(outcome.targets[0].code, 'HTTP_404')

    async def test_without_fail_fast_every_target_is_measured(self):
        plan = pr.build_plan({'mode': 'basic', 'options': {'attempts': 1}})
        html = (('content-type', 'text/html'),)
        wire = routing({'iana.org': pr.ProbeResponse(status=200, headers=html,
                                                     body=b'reserved domains ' + b' ' * 200),
                        'example.com': pr.ProbeResponse(status=200, headers=html,
                                                         body=b'Example Domain ' + b' ' * 200)})
        outcome = await pr.run_plan(plan, wire, endpoint='http://11.0.0.1:8080', fail_fast=False)
        self.assertEqual(len(outcome.targets), len(plan.targets))
        self.assertEqual(outcome.successes, len(plan.targets))
        self.assertTrue(all(item.ok for item in outcome.targets))

    async def test_public_view_is_serializable_and_complete(self):
        plan = pr.build_plan({'mode': 'basic', 'options': {'attempts': 1}})
        wire = transport(ok(b'Example Domain ' + b' ' * 200, headers=(('content-type', 'text/html'),)))
        outcome = await pr.run_plan(plan, wire, endpoint='http://11.0.0.1:8080')
        value = json.loads(json.dumps(outcome.to_public()))
        self.assertEqual(value['mode'], 'basic')
        self.assertEqual(value['evidence'], 'transfer_ok')
        self.assertGreaterEqual(value['duration_ms'], 0)
        self.assertEqual(value['whole_probe_timeout_s'], plan.options.whole_probe_timeout_s)
        self.assertTrue(value['targets'][0]['ok'])


if __name__ == '__main__':
    unittest.main()

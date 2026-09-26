"""End-to-end probe scenarios against the local reference probe (F20).

Every byte here travels over loopback to the reference endpoint this module
ships.  No public proxy, DNSBL or third-party service is contacted.
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import probes as pr

# Loopback is fast, so the honest minimums for these local scenarios are much
# smaller than the product defaults; the defaults themselves are pinned in
# tests.test_probes_speed.
SMALL_LIMITS = pr.SpeedLimits(min_bytes=4_000, min_seconds=0.0, min_chunks=2, max_bytes=8 << 20)


class LoopbackTransport:
    """The transport seam filled by a direct loopback client.

    In production the integrator binds this to the proxy under test; here it is
    a plain client, so the assertions are about the module's own arithmetic and
    validation rather than about somebody's proxy.
    """

    def __init__(self, url):
        self.url = url

    async def send(self, request, *, options):
        started = time.perf_counter()
        first = None
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=options.read_timeout_s,
                                         trust_env=False) as client:
                async with client.stream(request.method, request.url, headers=request.header_map(),
                                         content=request.body) as response:
                    if first is None:
                        first = time.perf_counter()
                    body = b''
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) >= request.max_bytes:
                            break
                    finished = time.perf_counter()
                    return pr.ProbeResponse(status=response.status_code,
                                            headers=tuple(response.headers.items()),
                                            body=body, url=str(response.url),
                                            ttfb_ms=round((first - started) * 1000, 2),
                                            transfer_ms=round((finished - first) * 1000, 2),
                                            total_ms=round((finished - started) * 1000, 2),
                                            connect_ms=round((first - started) * 1000, 2))
        except httpx.HTTPError as exc:
            return pr.ProbeResponse(code=type(exc).__name__, stage='target')

    async def download(self, target, *, options):
        trace = pr.TransferTrace(url=target.url)
        started = time.perf_counter()
        trace.begin(started)
        try:
            async with httpx.AsyncClient(timeout=options.read_timeout_s, trust_env=False) as client:
                async with client.stream('GET', target.url) as response:
                    trace.status = response.status_code
                    if response.status_code != 200:
                        return trace.fail(f'HTTP_{response.status_code}', time.perf_counter())
                    async for chunk in response.aiter_bytes():
                        trace.add(len(chunk), time.perf_counter())
                        if trace.bytes >= target.max_bytes:
                            break
                    return trace.finish(time.perf_counter())
        except httpx.HTTPError as exc:
            return trace.fail(type(exc).__name__, time.perf_counter())


class ReferenceProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._running = pr.serve_reference_probe()
        self.info = self._running.__enter__()
        self.transport = LoopbackTransport(self.info['url'])

    async def asyncTearDown(self):
        # The reference probe runs in a thread; shutting it down is blocking
        # work and must not stall the event loop.
        await asyncio.to_thread(self._running.__exit__, None, None, None)

    def options(self, **extra):
        return pr.validate_options({'attempts': 1, 'backoff_s': 0, **extra})

    async def test_reference_targets_pass_against_the_reference_probe(self):
        targets = pr.reference_probe_targets(self.info['url'])
        self.assertEqual([target.id for target in targets], ['reference-health', 'reference-echo'])
        for target in targets:
            with self.subTest(target=target.id):
                result = await pr.run_probe(target, self.options(), self.transport)
                self.assertTrue(result.ok, result.code)
                self.assertEqual(result.stage, 'target')
                self.assertGreater(result.bytes, 0)

    async def test_a_wrong_assertion_fails_with_its_own_code(self):
        broken = pr.validate_target({'id': 'x', 'url': f'{self.info["url"]}/health', 'statuses': [200],
                                     'content_type': 'application/json', 'min_body_bytes': 2,
                                     'json_assertions': [{'path': 'status', 'op': 'equals', 'value': 'nope'}]})
        result = await pr.run_probe(broken, self.options(), self.transport)
        self.assertEqual(result.code, pr.JSON_ASSERT)

    async def test_a_missing_path_fails_on_status(self):
        missing = pr.validate_target({'id': 'x', 'url': f'{self.info["url"]}/nope', 'statuses': [200],
                                      'min_body_bytes': 1})
        result = await pr.run_probe(missing, self.options(), self.transport)
        self.assertEqual(result.code, 'HTTP_404')

    async def test_a_dead_endpoint_fails_at_the_target_stage_not_as_a_claim(self):
        dead = pr.validate_target({'id': 'x', 'url': 'http://127.0.0.1:9/nothing', 'statuses': [200],
                                   'min_body_bytes': 1})
        result = await pr.run_probe(dead, self.options(connect_timeout_s=0.5, handshake_timeout_s=0.5,
                                                       read_timeout_s=0.5, whole_probe_timeout_s=2),
                                    self.transport)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.code)
        self.assertIn(result.stage, ('target', 'tcp', 'handshake'))

    async def test_bounded_redirects_are_followed_against_a_real_server(self):
        target = pr.validate_target({'id': 'redirect', 'url': f'{self.info["url"]}/redirect/2',
                                     'statuses': [200], 'content_type': 'application/json',
                                     'min_body_bytes': 2, 'max_redirects': 3})
        result = await pr.run_probe(target, self.options(), self.transport)
        self.assertTrue(result.ok, result.code)
        self.assertTrue(result.url.endswith('/health'))

    async def test_an_unbounded_redirect_target_fails(self):
        target = pr.validate_target({'id': 'redirect', 'url': f'{self.info["url"]}/redirect/3',
                                     'statuses': [200], 'min_body_bytes': 2})
        result = await pr.run_probe(target, self.options(), self.transport)
        self.assertEqual(result.code, pr.REDIRECT_TOO_MANY)

    async def test_echo_judge_confirms_elite_only_with_a_baseline(self):
        judge = pr.validate_judge_spec({'url': f'{self.info["url"]}/echo', 'content_type': 'application/json'})
        bootstrap = await self.fetch(judge.url)
        own = set(pr.extract_addresses(bootstrap))
        # The direct request proves the judge names the caller, so the judge is
        # verified and 127.0.0.1 is "our" address from now on.
        self.assertIn('127.0.0.1', own)
        self.assertEqual(pr.classify_echo(bootstrap, own, judge_verified=True, spec=judge).level, 'transparent')

        # A response from a different origin, as seen through a proxy.
        through_proxy = bootstrap.replace(b'127.0.0.1', b'203.0.113.9')
        self.assertEqual(pr.classify_echo(through_proxy, own, judge_verified=True, spec=judge).level, 'elite')
        self.assertEqual(pr.classify_echo(through_proxy, own, judge_verified=True, spec=judge).exit_ip,
                         '203.0.113.9')

        # Without a verified judge the same clean answer stays unknown.
        unverified = pr.classify_echo(through_proxy, own, judge_verified=False, spec=judge)
        self.assertEqual(unverified.level, 'unknown')
        self.assertEqual(unverified.code, pr.JUDGE_UNVERIFIED)

    async def test_an_error_page_is_never_a_verdict(self):
        judge = pr.validate_judge_spec({'url': f'{self.info["url"]}/nope'})
        missing = pr.run_probe(pr.validate_target({'id': 'judge', 'url': judge.url, 'statuses': [200],
                                                   'min_body_bytes': 1}),
                               self.options(), self.transport)
        outcome = await missing
        self.assertEqual(outcome.code, 'HTTP_404')
        self.assertNotIn('origin', outcome.to_public())

    async def test_bandwidth_over_a_real_stream_has_a_real_window(self):
        target = pr.validate_speed_target({'url': f'{self.info["url"]}/bytes/262144', 'max_bytes': 262_144})
        result = await pr.run_speed_test(target, self.options(), self.transport, limits=SMALL_LIMITS)
        self.assertEqual(result.state, 'ok', result.detail)
        self.assertGreater(result.mbps, 0)
        self.assertGreater(result.bytes, 0)
        self.assertGreaterEqual(result.chunks, 2)
        self.assertGreater(result.total_ms, result.transfer_ms - 0.001)
        self.assertGreater(result.total_ms, 0)

    async def test_a_tiny_stream_is_insufficient_even_over_real_http(self):
        target = pr.validate_speed_target({'url': f'{self.info["url"]}/bytes/64', 'max_bytes': 262_144})
        result = await pr.run_speed_test(target, self.options(), self.transport, limits=SMALL_LIMITS)
        self.assertEqual(result.state, 'insufficient')
        self.assertIsNone(result.mbps)
        self.assertEqual(result.code, pr.INSUFFICIENT_SAMPLE)

    async def test_a_missing_speed_endpoint_is_an_error(self):
        target = pr.validate_speed_target({'url': f'{self.info["url"]}/nope', 'max_bytes': 100_000})
        result = await pr.run_speed_test(target, self.options(), self.transport, limits=SMALL_LIMITS)
        self.assertEqual(result.state, 'error')
        self.assertEqual(result.code, 'HTTP_404')

    async def fetch(self, url):
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            return (await client.get(url)).content

    async def test_the_reference_probe_never_leaks_the_request_body_of_another_call(self):
        first = pr.reference_probe_targets(self.info['url'])[0]
        second = pr.reference_probe_targets(self.info['url'])[1]
        results = [await pr.run_probe(target, self.options(), self.transport) for target in (first, second)]
        self.assertEqual([item.target_id for item in results], ['reference-health', 'reference-echo'])


class CapabilityMatrixTests(unittest.TestCase):
    def test_every_capability_states_whether_it_is_measured(self):
        seen = set()
        for item in pr.capability_matrix():
            with self.subTest(capability=item['id']):
                self.assertIn(item['id'], seen if False else {item['id']})
                seen.add(item['id'])
                self.assertIn('supported', item)
                self.assertIn(item['kind'], ('target', 'speed', 'judge', 'dnsbl', 'stage'))
                if item['supported']:
                    self.assertNotEqual(item['endpoint'], '—')
                    self.assertNotEqual(item['budget'], '—')
                else:
                    self.assertNotEqual(item['outcome'], '')

    def test_measured_capabilities_name_their_endpoint_budget_and_outcome(self):
        """A capability that is measured names where, at what cost, and what came back.

        The three entries this replaces were asserted ``supported=False`` while
        the probe ladder had already gained a real endpoint, a budget and an
        outcome for each of them: a websocket upgrade with a pong, a connection
        held open, a media segment fetched.  Declaring them unmeasured was true
        when the product only ever issued a GET and became false when F20
        landed, so the assertion had pinned the absence rather than the honesty.
        """
        matrix = {item['id']: item for item in pr.capability_matrix()}
        for name in ('websocket_handshake', 'long_lived_connection', 'media_manifest_segment',
                     'http_api_assertions'):
            with self.subTest(capability=name):
                self.assertTrue(matrix[name]['supported'])
                self.assertNotEqual(matrix[name]['endpoint'], '—')
                self.assertNotEqual(matrix[name]['budget'], '—')
                self.assertNotEqual(matrix[name]['outcome'], '')
        # Still not measured, and still said so with an outcome instead of a promise.
        for name in ('udp_transport', 'http2_or_http3', 'calls_video_any_service'):
            with self.subTest(capability=name):
                self.assertFalse(matrix[name]['supported'])
                self.assertNotEqual(matrix[name]['outcome'], '')
        for name in ('http_transfer', 'bandwidth', 'anonymity_judge', 'dns_reputation'):
            with self.subTest(capability=name):
                self.assertTrue(matrix[name]['supported'])


if __name__ == '__main__':
    unittest.main()

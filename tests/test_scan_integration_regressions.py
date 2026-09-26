"""Scan regressions with an in-memory DB and bounded, observable fake I/O.

No listener, public network, user configuration, or persistent data is needed.
"""
import asyncio
import contextlib
import copy
import json
import sqlite3
import time
import unittest
from dataclasses import replace
from unittest import mock

from proxy_workbench import core, db, pipeline, probes, proxytool as p
from tests.workbench_support import add_candidate, store_result


CONFIG = {
    'targets': [{'url': 'http://origin.invalid/test', 'method': 'GET', 'headers': {},
                 'statuses': [200], 'contains': None, 'sha256': None}],
    'attempts': 1, 'timeout': 2, 'connect_timeout': 1, 'max_bytes': 1024,
    'reputation': {}, 'anonymity': {},
}


def good_row(proxy, *, exit_ip='203.0.113.9'):
    return {'proxy': proxy, 'successes': 1, 'requests': 1,
            'reliability': 1.0, 'min_target_reliability': 1.0,
            'checked_at': time.time(), 'latency_ms': 10, 'score': 90,
            'samples': [{'ok': True, 'bytes': 2, 'target': 0, 'attempt': 1}],
            'anonymity': {'level': 'elite', 'exit_ip': exit_ip}}


class MemoryHTTP:
    """Records requests, connection admission and bytes yielded to the reader."""

    def __init__(self, body=b'ok'):
        self.body = body
        self.requests = []
        self.bytes_read = 0
        self.connections = 0
        self.inflight = 0
        self.peak = 0
        self.read_caps = []

    @contextlib.asynccontextmanager
    async def client(self, *args, **kwargs):
        self.connections += 1
        yield self

    @contextlib.asynccontextmanager
    async def stream(self, method, url, **kwargs):
        self.requests.append(url)
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0)
            yield self
        finally:
            self.inflight -= 1

    @property
    def status_code(self):
        return 200

    @property
    def headers(self):
        return {'content-length': str(len(self.body))}

    async def aiter_bytes(self, chunk_size=None):
        self.read_caps.append(chunk_size)
        size = chunk_size or len(self.body)
        for offset in range(0, len(self.body), size):
            await asyncio.sleep(0)
            chunk = self.body[offset:offset + size]
            self.bytes_read += len(chunk)
            yield chunk


class ScanIntegrationRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        context = db._MigrationContext(now=time.time(), app_version='test', backup=None)
        for migration in db.MIGRATIONS:
            migration.apply(self.conn, context)
        self.conn.commit()
        self.addCleanup(self.conn.close)
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(p, 'open_fd_budget', return_value=96))
        self.patches.enter_context(mock.patch.object(p.random, 'shuffle', lambda values: None))
        self.network = self.patches.enter_context(
            mock.patch.object(p, 'snapshot_network', return_value='test-network'))
        self.patches.enter_context(mock.patch.object(
            asyncio, 'open_connection', side_effect=AssertionError('unexpected socket')))
        self.calls = []

    def seed(self, count=1, *, collection=None, start=1):
        proxies = [f'http://198.51.100.{i}:8080' for i in range(start, start + count)]
        for proxy in proxies:
            add_candidate(self.conn, proxy, collection_id=collection)
        self.conn.commit()
        return proxies

    async def probe(self, proxy, config, limiter):
        self.calls.append(proxy)
        return good_row(proxy)

    async def scan(self, config=None, **kwargs):
        state = {}
        await asyncio.wait_for(p.scan(
            self.conn, config or copy.deepcopy(CONFIG), workers=kwargs.pop('workers', 1),
            rate=0, progress=False, min_success=1, run_state=state,
            probe=kwargs.pop('probe', self.probe), **kwargs), 5)
        return state

    def saved(self):
        return [json.loads(payload) for (payload,) in self.conn.execute('SELECT payload FROM results')]

    def mock_http(self, body=b'ok'):
        http = MemoryHTTP(body)
        self.patches.enter_context(mock.patch.object(p, 'proxy_client', http.client))
        return http

    async def test_request_budget_stops_between_attempts_and_targets(self):
        self.seed()
        cfg = copy.deepcopy(CONFIG)
        cfg['attempts'] = 3
        cfg['targets'] *= 2
        http = self.mock_http()
        state = await self.scan(cfg, probe=p.check_proxy, max_requests=1)
        self.assertEqual((http.connections, len(http.requests)), (1, 1))
        self.assertEqual((state['requests'], state['bytes']), (1, 2))
        self.assertEqual(state['stop_reason'], 'E_LIMIT_BUDGET')
        self.assertEqual(state['passed'], 0)
        self.assertEqual(state['pending'], 1)
        self.assertEqual(self.saved(), [], 'an unfinished probe must remain resumable')

    async def test_byte_budget_bounds_the_read_itself(self):
        self.seed()
        cfg = copy.deepcopy(CONFIG)
        cfg['attempts'] = 3
        cfg['targets'] *= 2
        http = self.mock_http(b'x' * 66)
        state = await self.scan(cfg, probe=p.check_proxy, max_bytes=1)
        self.assertEqual((len(http.requests), http.bytes_read), (1, 1))
        self.assertEqual(http.read_caps, [1])
        self.assertEqual((state['requests'], state['bytes']), (1, 1))
        self.assertEqual(state['stop_reason'], 'E_LIMIT_BUDGET')

    async def test_zero_budget_opens_no_connection(self):
        self.seed()
        http = self.mock_http()
        for limits in ({'max_requests': 0}, {'max_bytes': 0}):
            with self.subTest(limits=limits):
                state = await self.scan(probe=p.check_proxy, **limits)
                self.assertEqual(state['stop_reason'], 'E_LIMIT_BUDGET')
        self.assertEqual(http.connections, 0)

    async def test_concurrent_attempts_share_request_reservations(self):
        self.seed(20)
        cfg = copy.deepcopy(CONFIG)
        cfg['attempts'] = 3
        cfg['targets'] *= 2
        http = self.mock_http()
        state = await self.scan(cfg, probe=p.check_proxy, workers=8, max_requests=5)
        self.assertGreater(http.peak, 1)
        self.assertEqual((http.connections, len(http.requests)), (5, 5))
        self.assertEqual(state['requests'], 5)
        self.assertEqual(state['bytes'], http.bytes_read)
        self.assertEqual(state['stop_reason'], 'E_LIMIT_BUDGET')

    async def test_concurrent_reads_share_byte_reservations(self):
        self.seed(20)
        cfg = copy.deepcopy(CONFIG)
        cfg['max_bytes'] = 2
        http = self.mock_http()
        state = await self.scan(cfg, probe=p.check_proxy, workers=8, max_bytes=5)
        self.assertGreater(http.peak, 1)
        self.assertEqual(http.bytes_read, 5)
        self.assertEqual(sorted(http.read_caps), [1, 2, 2])
        self.assertEqual((state['requests'], state['bytes']), (3, 5))
        self.assertEqual(http.inflight, 0)

    async def test_unused_byte_reservations_are_returned_to_waiters(self):
        self.seed(4)
        http = self.mock_http()
        state = await self.scan(probe=p.check_proxy, workers=4, max_bytes=8)
        self.assertEqual((state['passed'], state['bytes']), (4, 8))
        self.assertEqual((http.bytes_read, len(http.requests)), (8, 4))

    async def test_concurrent_reputation_rejections_spend_no_http_budget(self):
        self.seed(8)

        async def screen(proxy, config):
            await asyncio.sleep(0)
            return {'status': 'listed', 'dnsbl': []}

        state = await self.scan(screen=screen, workers=4, max_requests=1)
        self.assertEqual((state['checked'], state['requests']), (8, 0))
        self.assertEqual(state['stop_reason'], 'complete')
        self.assertEqual(self.calls, [])

    async def test_concurrent_tcp_rejections_spend_no_http_budget(self):
        self.seed(8)

        async def unreachable(proxy, timeout):
            await asyncio.sleep(0)
            return False

        with mock.patch.object(p, 'reachable', unreachable):
            state = await self.scan(prefilter=1, workers=4, max_requests=1)
        self.assertEqual((state['checked'], state['requests']), (8, 0))
        self.assertEqual(state['stop_reason'], 'complete')
        self.assertEqual(self.calls, [])

    async def test_httpx_mock_transport_also_obeys_request_budget(self):
        import httpx
        self.seed()
        cfg = copy.deepcopy(CONFIG)
        cfg['attempts'] = 3
        cfg['targets'] *= 2
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, content=b'ok')

        def client(*args):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)

        with mock.patch.object(p, 'proxy_client', client):
            state = await self.scan(cfg, probe=p.check_proxy, max_requests=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state['requests'], 1)

    async def test_judge_cannot_spend_a_second_request(self):
        self.seed()
        cfg = copy.deepcopy(CONFIG)
        cfg['anonymity'] = {'judge_url': 'http://judge.invalid/'}
        http = self.mock_http()

        async def probe(proxy, config, rate):
            return await p.check_proxy(proxy, config, rate, own_ips={'192.0.2.1'})

        state = await self.scan(cfg, probe=probe, max_requests=1)
        self.assertEqual(http.requests, [cfg['targets'][0]['url']])
        self.assertEqual(state['stop_reason'], 'E_LIMIT_BUDGET')
        self.assertEqual(state['passed'], 0)

    async def test_judge_read_gets_remaining_byte_budget(self):
        self.seed()
        cfg = copy.deepcopy(CONFIG)
        cfg['anonymity'] = {'judge_url': 'http://judge.invalid/'}
        http = self.mock_http()

        async def probe(proxy, config, rate):
            return await p.check_proxy(proxy, config, rate, own_ips={'192.0.2.1'})

        state = await self.scan(cfg, probe=probe, max_bytes=3)
        self.assertEqual(len(http.requests), 2)
        self.assertEqual((state['requests'], state['bytes'], http.bytes_read), (2, 3, 3))
        self.assertEqual(http.read_caps[-1], 1)

    async def test_expensive_stage_shares_budget_without_counting_basic_twice(self):
        self.seed()
        http = self.mock_http()

        async def expensive(proxy, config, rate, row):
            sample = await p.request_once(proxy, config['targets'][0], config, rate)
            return {**row, 'samples': row['samples'] + [sample], 'requests': 2}

        state = await self.scan(probe=p.check_proxy, expensive_probe=expensive, max_requests=2)
        self.assertEqual(len(http.requests), 2)
        self.assertEqual((state['requests'], state['bytes']), (2, 4))
        self.assertEqual(state['passed'], 1)

    async def test_speed_download_gets_remaining_bytes_before_transport(self):
        self.seed()
        cfg = copy.deepcopy(CONFIG)
        cfg['speedtest'] = {'url': 'http://speed.invalid/', 'max_bytes': 100_000}
        http = self.mock_http()
        caps = []

        async def download(transport, target, *, options, reuse=False):
            caps.append(target.max_bytes)
            trace = probes.TransferTrace(url=target.url)
            trace.begin(1.0)
            trace.status = 200
            trace.add(target.max_bytes, 1.5)
            return trace.finish(2.0)

        with mock.patch.object(p.Transport, 'download', download):
            state = await self.scan(cfg, probe=p.check_proxy, max_bytes=5)
        self.assertEqual(caps, [3])
        self.assertEqual((state['requests'], state['bytes']), (2, 5))
        self.assertEqual(len(http.requests), 1)

    async def test_speed_cannot_bypass_request_budget_via_caught_exception(self):
        self.seed()
        cfg = copy.deepcopy(CONFIG)
        cfg['speedtest'] = {'url': 'http://speed.invalid/', 'max_bytes': 100_000}
        self.mock_http()
        with mock.patch.object(p.Transport, 'download', new_callable=mock.AsyncMock) as download:
            state = await self.scan(cfg, probe=p.check_proxy, max_requests=1)
        download.assert_not_awaited()
        self.assertEqual(state['passed'], 0)
        self.assertEqual(state['stop_reason'], 'E_LIMIT_BUDGET')

    async def test_media_reserves_each_manifest_and_segment_request(self):
        self.seed()
        cfg = copy.deepcopy(CONFIG)
        cfg['capabilities'] = [{'kind': 'media', 'id': 'media', 'url': 'http://media.invalid/list.m3u8'}]
        self.mock_http()
        requests = []

        async def send(transport, request, *, options):
            requests.append(request)
            return probes.ProbeResponse(status=200, body=b'#EXTM3U\nsegment.ts\n', url=request.url)

        with mock.patch.object(p.Transport, 'send', send):
            state = await self.scan(cfg, probe=p.check_proxy, max_requests=2)
        self.assertEqual(len(requests), 1, 'no segment request may follow the second run request')
        self.assertEqual(state['requests'], 2)
        self.assertEqual(state['passed'], 0)
        self.assertEqual(state['stop_reason'], 'E_LIMIT_BUDGET')

    async def test_raw_capabilities_receive_remaining_byte_cap(self):
        self.seed()
        self.mock_http()
        for kind, method in (('websocket', 'websocket'), ('duration', 'hold')):
            with self.subTest(kind=kind):
                cfg = copy.deepcopy(CONFIG)
                cfg['capabilities'] = [{'kind': kind, 'id': kind, 'url': 'http://capability.invalid/'}]
                caps = []

                async def transfer(transport, request, *, options, spec):
                    key = 'max_frame_bytes' if kind == 'websocket' else 'max_bytes'
                    caps.append((request.max_bytes, spec.budget[key]))
                    if kind == 'websocket':
                        return probes.WsTrace(bytes_in=request.max_bytes)
                    return probes.HoldTrace(bytes=request.max_bytes)

                with mock.patch.object(p.Transport, method, transfer):
                    state = await self.scan(cfg, probe=p.check_proxy, max_bytes=5, recheck=True)
                self.assertEqual(caps, [(3, 3)])
                self.assertEqual((state['requests'], state['bytes']), (2, 5))

    async def test_resume_adds_only_the_missing_endpoint(self):
        self.seed(3)
        first = await self.scan(want=1)
        second = await self.scan(want=2)
        self.assertEqual((first['found'], first['passed']), (1, 1))
        self.assertEqual((second['found'], second['passed'], second['checked']), (2, 2, 2))
        self.assertEqual(len(self.calls), 2)
        third = await self.scan(want=2)
        self.assertEqual(third['found'], 2)
        self.assertEqual(len(self.calls), 2)

    async def test_resume_does_not_count_another_port_as_a_new_ip(self):
        for proxy in ('http://198.51.100.1:1000', 'http://198.51.100.1:2000',
                      'http://198.51.100.2:1000', 'http://198.51.100.3:1000'):
            add_candidate(self.conn, proxy)
        await self.scan(want=1, count_what='ip')
        state = await self.scan(want=2, count_what='ip')
        self.assertEqual((state['found'], state['unique_ips']), (2, 2))
        self.assertEqual(state['passed'], 3)
        self.assertEqual(len(self.calls), 3)

    async def test_resume_does_not_count_a_known_exit_again(self):
        self.seed(4)

        async def probe(proxy, config, rate):
            self.calls.append(proxy)
            exit_ip = '203.0.113.9' if len(self.calls) < 3 else '203.0.113.10'
            return good_row(proxy, exit_ip=exit_ip)

        await self.scan(probe=probe, want=1, count_what='exit')
        state = await self.scan(probe=probe, want=2, count_what='exit')
        self.assertEqual((state['found'], state['exit_ips'], state['passed']), (2, 2, 3))
        self.assertEqual(len(self.calls), 3)

    async def test_other_collection_cannot_seed_want(self):
        a = db.create_collection(self.conn, 'A', collection_id='a')
        b = db.create_collection(self.conn, 'B', collection_id='b')
        self.seed(collection=a)
        second = self.seed(collection=b, start=2)
        await self.scan(collection_id=a, want=1)
        state = await self.scan(collection_id=b, want=1)
        self.assertEqual(self.calls[-1:], second)
        self.assertEqual((state['checked'], state['passed'], state['found']), (1, 1, 1))

    async def test_shared_endpoint_still_needs_matching_collection_identity(self):
        a = db.create_collection(self.conn, 'A', collection_id='a')
        b = db.create_collection(self.conn, 'B', collection_id='b')
        self.seed(collection=a)
        self.seed(collection=b)
        await self.scan(collection_id=a)
        state = await self.scan(collection_id=b, want=1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(state['checked'], 1)
        self.assertEqual(self.saved()[0]['collection_id'], b)

    async def test_changed_profile_revision_is_remeasured_without_foreign_history(self):
        self.seed()
        await self.scan(profile_revision=1)
        state = await self.scan(profile_revision=2, want=1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(state['requests'], 1)
        self.assertEqual({row['profile_revision'] for row in self.saved()}, {1, 2})
        self.assertTrue(all(row['history']['checks'] == 1 for row in self.saved()))

    async def test_changed_access_id_or_revision_is_remeasured(self):
        self.seed()
        for access in (core.Access('a', 1), core.Access('b', 1), core.Access('b', 2)):
            state = await self.scan(access=access, want=1)
            self.assertEqual(state['requests'], 1)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(row['history']['checks'] == 1 for row in self.saved()))

    async def test_changed_network_is_remeasured(self):
        self.seed()
        await self.scan()
        self.network.return_value = 'another-network'
        state = await self.scan(want=1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(state['requests'], 1)
        self.assertEqual(self.saved()[0]['network_id'], 'another-network')

    async def test_identity_filter_precedes_newest_measurement_choice(self):
        proxy, = self.seed()
        first = await self.scan()
        row = {**good_row(proxy), 'checked_at': time.time() + 1}
        store_result(self.conn, (first['profile'], proxy, row), profile_revision=2,
                     network_id='test-network')
        state = await self.scan(profile_revision=1, want=1)
        self.assertEqual(state['requests'], 0)
        self.assertEqual(state['found'], 1)
        self.assertEqual(len(self.calls), 1)

    async def test_removed_membership_cannot_seed_want(self):
        proxies = self.seed(2)
        await self.scan(want=1)
        self.conn.execute('DELETE FROM membership WHERE endpoint_id=?', (db.endpoint_id(proxies[0]),))
        state = await self.scan(want=1)
        self.assertEqual(self.calls, proxies)
        self.assertEqual((state['scope_candidates'], state['checked'], state['found']), (1, 1, 1))

    async def test_exit_include_is_deferred_then_applied_with_default_unknown_policy(self):
        self.seed()
        resolve = lambda url: 'NL' if '203.0.113.9' in url else 'DE'
        state = await self.scan(countries=('NL',), country_basis='exit', country_of=resolve, want=1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((state['checked'], state['passed'], state['found']), (1, 1, 1))

    async def test_wrong_exit_does_not_satisfy_want_or_resume_seed(self):
        self.seed()
        for unknown in ('exclude', 'require_measurement'):
            with self.subTest(unknown=unknown):
                state = await self.scan(countries=('NL',), country_basis='exit',
                                        country_unknown=unknown, country_of=lambda _: 'DE', want=1)
                self.assertEqual((state['passed'], state['found']), (0, 0))
                self.assertEqual(state['stop_reason'], 'want_unreachable_endpoint')
        self.assertEqual(len(self.calls), 1, 'the measured mismatch can be reused, but never counted')

    async def test_exit_exclusion_is_applied_after_measurement(self):
        self.seed()
        state = await self.scan(country_exclude=('DE',), country_basis='exit',
                                country_of=lambda _: 'DE', want=1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(state['passed'], 0)

    async def test_either_country_basis_can_pass_through_the_exit(self):
        self.seed()
        state = await self.scan(countries=('NL',), country_basis='either', want=1,
                                country_of=lambda url: 'NL' if '203.0.113.9' in url else 'DE')
        self.assertEqual((len(self.calls), state['passed']), (1, 1))

    async def test_endpoint_country_still_filters_before_probe(self):
        self.seed()
        state = await self.scan(countries=('NL',), country_basis='endpoint', country_of=lambda _: 'DE')
        self.assertEqual(self.calls, [])
        self.assertEqual(state['scope_candidates'], 0)

    async def test_clean_reputation_reaches_verdict_storage_and_resume(self):
        self.seed()
        cfg = copy.deepcopy(CONFIG)
        cfg['reputation'] = {'strict': True}
        verdict = {'status': 'clean', 'checked_at': time.time(), 'dnsbl': []}
        screen = mock.AsyncMock(return_value=verdict)
        state = await self.scan(cfg, screen=screen, want=1)
        self.assertEqual((state['passed'], state['found']), (1, 1))
        self.assertEqual(self.saved()[0]['reputation'], verdict)
        state = await self.scan(cfg, screen=screen, want=1)
        self.assertEqual((state['passed'], state['found'], state['requests']), (1, 1, 0))
        self.assertEqual(screen.await_count, 1)

    async def test_unknown_screen_is_preserved_and_strict_screen_blocks(self):
        self.seed()
        verdict = {'status': 'unknown', 'dnsbl': []}
        await self.scan(screen=mock.AsyncMock(return_value=verdict))
        self.assertEqual(self.saved()[0]['reputation'], verdict)
        cfg = copy.deepcopy(CONFIG)
        cfg['reputation'] = {'strict': True}
        state = await self.scan(cfg, screen=mock.AsyncMock(return_value=verdict))
        self.assertEqual((len(self.calls), state['passed'], state['requests']), (1, 0, 0))


class PipelineSeedRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_same_pipeline_keeps_initial_met_across_runs(self):
        calls = []

        async def runner(item, *, stage, limit):
            calls.append(item.endpoint)
            return pipeline.StageOutcome(stage, True, requests=1)

        async def source():
            yield b'http://198.51.100.1:8080\nhttp://198.51.100.2:8080\n'

        config = pipeline.PipelineConfig(
            sources=(pipeline.SourceSpec('memory', fetch=source),), initial_met=1,
            find=pipeline.FindPolicy(n=2, what='endpoint'), runners=pipeline.Runners(basic=runner),
            run_cheap=False, run_expensive=False,
            normalize=pipeline.fixture_normalize,
            budgets=pipeline.Budgets(max_inflight=1, max_open_fds=3))
        engine = pipeline.Pipeline(config)
        first = await engine.run()
        self.assertEqual(first.find.met, 2)
        second = await engine.run()
        self.assertEqual(second.find.met, 2)
        self.assertEqual(len(calls), 1)
        engine.config = replace(config, find=pipeline.FindPolicy(n=3, what='endpoint'))
        third = await engine.run()
        self.assertEqual(third.find.met, 3)
        self.assertEqual(len(calls), 2)

    async def test_waiting_gate_acquire_rechecks_the_total_after_wakeup(self):
        gate = pipeline.ResourceGate(pipeline.Budgets(max_inflight=1, max_requests=2),
                                     pipeline.SystemClock())
        first = await gate.acquire(requests=1)
        pending = asyncio.create_task(gate.acquire(requests=1))
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        await gate.release(first, requests=2)
        with self.assertRaises(pipeline.BudgetExhausted):
            await asyncio.wait_for(pending, 1)
        self.assertEqual((gate.snapshot().requests, gate.snapshot().inflight), (2, 0))

    async def test_cancelled_request_settles_bytes_and_releases_reservation(self):
        gate = pipeline.ResourceGate(pipeline.Budgets(max_bytes=4), pipeline.SystemClock())
        slot = await gate.acquire(requests=1)
        budget = pipeline.RequestBudget(gate, slot)
        with budget:
            with self.assertRaises(asyncio.CancelledError):
                async with p._probe_io(4) as usage:
                    usage['bytes'] = 1
                    raise asyncio.CancelledError()
            async with p._probe_io(4) as usage:
                self.assertEqual(usage['max_bytes'], 3)
                usage['bytes'] = 3
        await gate.release(replace(slot, requests=0))
        self.assertEqual((gate.snapshot().requests, gate.snapshot().bytes), (2, 4))
        self.assertEqual(gate.snapshot().inflight, 0)
        self.assertIsNone(pipeline.CURRENT_REQUEST_BUDGET.get())


if __name__ == '__main__':
    unittest.main()

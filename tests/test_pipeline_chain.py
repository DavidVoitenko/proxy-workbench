"""F12: the chain itself — bounded fetch, parse, normalize/dedup, staged probes.

The tests below check behaviour, not implementation: what the counters say, what
comes out of ``stream()``, how many times a runner was asked about one endpoint.
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_pipeline_support as support  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import pipeline as pl  # noqa: E402


def run(coroutine):
    return asyncio.run(coroutine)


class SourceStageTests(unittest.TestCase):
    def test_lines_straddling_chunks_are_not_lost_or_doubled(self):
        # Chunk size 7 splits addresses and their newlines across reads; every
        # endpoint of the corpus must still be measured exactly once.
        endpoints = support.addresses(12)
        pulls = []
        calls = []
        source = support.chunk_source('s', endpoints, chunk=7, calls=pulls)
        cheap = support.recording_runner(ok=True, calls=calls)
        result = run(pl.run_pipeline(support.config(
            [source], pl.Runners(cheap=cheap, basic=None, expensive=None), run_basic=False)))
        self.assertEqual(result.state, 'complete', result.reason)
        self.assertEqual(result.counters.normalized, len(endpoints))
        self.assertEqual(result.counters.unique_endpoints, len(endpoints))
        self.assertEqual(len(calls), len(endpoints))
        self.assertEqual(len({item[1] for item in calls}), len(endpoints))
        self.assertGreater(len(pulls), 1)

    def test_source_byte_budget_truncates_instead_of_buffering(self):
        endpoints = support.addresses(40)
        source = support.chunk_source('s', endpoints, chunk=16)
        cheap = support.recording_runner(ok=True)
        result = run(pl.run_pipeline(support.config(
            [source], pl.Runners(cheap=cheap, basic=None, expensive=None), run_basic=False,
            budgets=pl.Budgets(max_source_bytes=64, max_requests=None, max_bytes=None))))
        self.assertEqual(result.counters.sources_truncated, 1)
        self.assertLessEqual(result.counters.source_bytes, 64)
        self.assertLess(result.counters.unique_endpoints, len(endpoints))
        self.assertEqual(result.state, 'budget', 'a truncated source is a budget stop, not a success')
        self.assertEqual(result.counters.stage_failures.get('stop'), pl.E_LIMIT_BODY)
        self.assertFalse(result.feed_complete)

    def test_per_source_byte_cap_wins_over_the_global_one(self):
        endpoints = support.addresses(20)
        source = support.chunk_source('s', endpoints, chunk=16)
        source = pl.SourceSpec(source_id='s', fetch=source.fetch, max_bytes=32)
        result = run(pl.run_pipeline(support.config(
            [source], pl.Runners(cheap=support.recording_runner(), basic=None, expensive=None),
            run_basic=False, budgets=pl.Budgets(max_source_bytes=4096, max_requests=None, max_bytes=None))))
        self.assertLessEqual(result.counters.source_bytes, 32)

    def test_a_line_that_does_not_normalise_is_rejected_and_counted(self):
        # One line is one address, like the existing collector, so a line with
        # trailing junk does not normalise.  It is counted, not dropped, and it
        # never reaches a probe.
        endpoints = support.addresses(4)
        calls = []
        source = support.chunk_source('s', endpoints, suffix='   garbage!!')
        result = run(pl.run_pipeline(support.config(
            [source], pl.Runners(cheap=support.recording_runner(calls=calls), basic=None,
                                 expensive=None), run_basic=False)))
        self.assertEqual(result.counters.parsed, 4)
        self.assertEqual(result.counters.rejected, 4)
        self.assertEqual(result.counters.normalized, 0)
        self.assertEqual(calls, [])
        self.assertEqual(result.state, 'complete')

    def test_a_good_line_next_to_a_bad_one_is_still_measured(self):
        endpoints = support.addresses(3)
        mixed = [endpoints[0], 'not an address at all', endpoints[1], endpoints[2]]
        calls = []
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', mixed)], pl.Runners(
                cheap=support.recording_runner(calls=calls), basic=None, expensive=None),
            run_basic=False)))
        self.assertEqual(result.counters.normalized, 3)
        self.assertEqual(result.counters.rejected, 1)
        self.assertEqual(len(calls), 3)

    def test_a_normaliser_that_raises_is_recorded_and_keeps_the_chain_alive(self):
        calls = []

        def normalize(value):
            raise ValueError('normaliser is broken')

        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', support.addresses(4))],
            pl.Runners(cheap=support.recording_runner(calls=calls), basic=None, expensive=None),
            run_basic=False, normalize=normalize)))
        self.assertEqual(result.state, 'complete')
        self.assertEqual(result.counters.rejected, 4)
        self.assertEqual(result.counters.stage_failures.get(pl.STAGE_NORMALIZE), 'ValueError')
        self.assertEqual(calls, [])

    def test_a_trailing_comment_is_dropped_not_parsed_as_an_address(self):
        endpoints = support.addresses(4)
        source = support.chunk_source('s', endpoints, suffix='   # 10.0.0.1:1')
        result = run(pl.run_pipeline(support.config(
            [source], pl.Runners(cheap=support.recording_runner(), basic=None, expensive=None),
            run_basic=False)))
        self.assertEqual(result.counters.unique_endpoints, len(endpoints))
        self.assertNotIn('http://10.0.0.1:1', [item.endpoint for item in result.results])

    def test_a_broken_source_stops_the_chain_and_says_so(self):
        async def fetch():
            raise RuntimeError('connection reset')
            yield b''  # pragma: no cover - makes this an async generator

        result = run(pl.run_pipeline(support.config(
            [pl.SourceSpec(source_id='s', fetch=fetch)],
            pl.Runners(cheap=support.recording_runner(), basic=None, expensive=None), run_basic=False)))
        self.assertEqual(result.state, 'partial')
        self.assertIn('source:RuntimeError', result.counters.stage_failures.get('stop', ''))
        self.assertEqual(result.counters.unique_endpoints, 0)

    def test_document_parser_gets_the_whole_bounded_buffer(self):
        endpoints = support.addresses(6)
        source = support.chunk_source('s', endpoints, chunk=5)
        seen = []

        def parse(data):
            seen.append(len(data))
            return pl.parse_lines(data)

        result = run(pl.run_pipeline(support.config(
            [source], pl.Runners(cheap=support.recording_runner(), basic=None, expensive=None),
            run_basic=False, parse=parse, parse_streaming=False)))
        self.assertEqual(result.counters.unique_endpoints, len(endpoints))
        self.assertEqual(len(seen), 1, 'a document parser must see the buffer once')

    def test_a_parser_that_raises_is_recorded_and_the_chain_continues(self):
        endpoints = support.addresses(4)

        def parse(data):
            raise ValueError('not json')

        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', endpoints)], pl.Runners(
                cheap=support.recording_runner(), basic=None, expensive=None),
            run_basic=False, parse=parse)))
        self.assertEqual(result.state, 'complete')
        self.assertEqual(result.counters.stage_failures.get(pl.STAGE_PARSE), 'ValueError')
        self.assertEqual(result.counters.normalized, 0)


class DedupTests(unittest.TestCase):
    def test_the_same_endpoint_twice_is_measured_once(self):
        endpoints = support.addresses(3)
        calls = []
        cheap = support.recording_runner(calls=calls)
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('a', endpoints), support.chunk_source('b', endpoints)],
            pl.Runners(cheap=cheap, basic=None, expensive=None), run_basic=False)))
        self.assertEqual(result.counters.unique_endpoints, 3)
        self.assertEqual(result.counters.duplicates, 3)
        self.assertEqual(result.counters.resume_skips, 0)
        self.assertEqual(len(calls), 3)

    def test_ledger_admits_once(self):
        ledger = pl.Ledger()
        self.assertTrue(ledger.admit('http://198.51.100.1:8080'))
        self.assertFalse(ledger.admit('http://198.51.100.1:8080'))
        self.assertEqual(ledger.to_public(), {'admitted': 1, 'finished': 0, 'released': 0, 'pending': 1})
        ledger.finish('http://198.51.100.1:8080', 'UNREACHABLE')
        self.assertEqual(ledger.pending, set())
        self.assertEqual(ledger.reason_codes['http://198.51.100.1:8080'], 'UNREACHABLE')

    def test_a_released_endpoint_is_pending_work_again(self):
        ledger = pl.Ledger()
        ledger.admit('http://198.51.100.1:8080')
        ledger.release('http://198.51.100.1:8080')
        self.assertEqual(ledger.to_public()['admitted'], 0)
        self.assertEqual(ledger.to_public()['released'], 1)
        self.assertEqual(ledger.pending, {'http://198.51.100.1:8080'})
        self.assertTrue(ledger.admit('http://198.51.100.1:8080'),
                        'a released endpoint must be admittable again')
        self.assertEqual(ledger.to_public()['released'], 0)


class StageGatingTests(unittest.TestCase):
    def test_cheap_failure_never_reaches_basic_or_expensive(self):
        calls = []
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', support.addresses(5))],
            pl.Runners(cheap=support.recording_runner(ok=False, calls=calls),
                       basic=support.recording_runner(ok=True, calls=calls),
                       expensive=support.recording_runner(ok=True, calls=calls)))))
        self.assertEqual([stage for stage, _, _ in calls], [pl.STAGE_CHEAP] * 5)
        self.assertEqual(result.counters.basic_checked, 0)
        self.assertEqual(result.counters.cheap_passed, 0)
        self.assertTrue(all(item.state in pl.EMITTED_ITEM_STATES for item in result.results))
        self.assertTrue(all(item.state == 'unreachable' for item in result.results))

    def test_basic_failure_reaches_no_expensive_probe(self):
        calls = []
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', support.addresses(4))],
            pl.Runners(cheap=support.recording_runner(ok=True, calls=calls),
                       basic=support.recording_runner(ok=False, calls=calls),
                       expensive=support.recording_runner(ok=True, calls=calls)))))
        self.assertEqual(result.counters.cheap_checked, 4)
        self.assertEqual(result.counters.basic_checked, 4)
        self.assertEqual(result.counters.expensive_checked, 0)
        self.assertEqual([stage for stage, _, _ in calls].count(pl.STAGE_EXPENSIVE), 0)

    def test_expensive_passes_are_all_passing_policy(self):
        calls = []
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', support.addresses(6))],
            pl.Runners(cheap=support.recording_runner(ok=True, calls=calls),
                       basic=support.recording_runner(ok=True, calls=calls),
                       expensive=support.recording_runner(ok=True, calls=calls)),
            find=pl.FindPolicy(n=0), expensive_policy=pl.EXPENSIVE_ALL_PASSING)))
        self.assertEqual(result.counters.expensive_checked, 6)
        self.assertEqual(result.state, 'complete')

    def test_a_runner_that_raises_fails_only_its_own_item(self):
        calls = []

        async def boom(item, *, stage, limit):
            calls.append((stage, item.endpoint))
            raise RuntimeError('transport exploded')

        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', support.addresses(4))],
            pl.Runners(cheap=boom, basic=support.recording_runner(ok=True), expensive=None))))
        self.assertEqual(result.state, 'complete')
        self.assertEqual(result.counters.cheap_checked, 4)
        self.assertEqual(result.counters.normalized, 4)
        self.assertTrue(all(item.reason_code == 'RuntimeError' for item in result.results))
        self.assertEqual(result.counters.basic_checked, 0)

    def test_runner_returning_junk_fails_the_item_and_says_what_is_expected(self):
        async def junk(item, *, stage, limit):
            return 'yes please'

        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', support.addresses(2))],
            pl.Runners(cheap=junk, basic=None, expensive=None), run_basic=False)))
        self.assertEqual(result.state, 'complete')
        self.assertTrue(all(item.reason_code == pl.E_VALIDATION_FIELD for item in result.results))

    def test_runner_may_return_a_bool_or_a_mapping(self):
        async def yes(item, *, stage, limit):
            return True

        async def mapping(item, *, stage, limit):
            return {'ok': True, 'bytes': 7, 'requests': 1, 'exit_ip': '203.0.113.9'}

        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', support.addresses(2))],
            pl.Runners(cheap=yes, basic=mapping, expensive=None))))
        self.assertEqual(result.counters.passed_endpoints, 2)
        self.assertEqual(result.counters.confirmed_exit_ips, 1,
                         'both endpoints reported the same exit address')
        self.assertEqual(result.counters.bytes, 14)


class StreamTests(unittest.TestCase):
    def test_results_arrive_before_the_run_ends(self):
        # Fewer workers than endpoints, and a runner that is slower than the
        # consumer, so leaving the stream early really does abandon the run.
        endpoints = support.addresses(40)

        async def runner(item, *, stage, limit):
            await asyncio.sleep(0.02)
            return pl.StageOutcome(stage, True, latency_ms=20.0, requests=1, bytes=8,
                                   exit_ip='203.0.113.7')

        async def scenario():
            pipeline = pl.Pipeline(support.config(
                [support.chunk_source('s', endpoints)],
                pl.Runners(cheap=runner, basic=None, expensive=None), run_basic=False,
                budgets=pl.Budgets(max_inflight=2, max_open_fds=6, max_ram_bytes=1 << 20)))
            seen = []
            async for item in pipeline.stream():
                seen.append(item.endpoint)
                if len(seen) == 2:
                    break
            return pipeline, seen

        pipeline, seen = run(scenario())
        self.assertEqual(len(seen), 2)
        self.assertIsNotNone(pipeline.last_result)
        self.assertNotEqual(pipeline.last_result.state, 'complete',
                            'a consumer that left early must not be reported as complete')
        self.assertTrue(pipeline.last_result.remaining,
                        'the endpoints nobody reached must still be reported as pending')

    def test_stream_and_run_report_the_same_summary(self):
        endpoints = support.addresses(20)
        seen = []

        async def scenario():
            pipeline = pl.Pipeline(support.config(
                [support.chunk_source('s', endpoints)],
                pl.Runners(cheap=support.recording_runner(ok=True), basic=None, expensive=None),
                run_basic=False))
            async for item in pipeline.stream():
                seen.append(item.endpoint)
            return pipeline.last_result

        streamed = run(scenario())
        direct = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', endpoints)],
            pl.Runners(cheap=support.recording_runner(ok=True), basic=None, expensive=None),
            run_basic=False)))
        self.assertEqual(sorted(seen), sorted(endpoints))
        self.assertEqual(streamed.state, direct.state)
        self.assertEqual(streamed.metrics.measured, direct.metrics.measured)
        self.assertEqual(streamed.counters.requests, direct.counters.requests)

    def test_on_result_hook_sees_every_item(self):
        seen = []
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', support.addresses(9))],
            pl.Runners(cheap=support.recording_runner(ok=True), basic=None, expensive=None),
            run_basic=False, on_result=seen.append)))
        self.assertEqual(len(seen), 9)
        self.assertEqual(len(result.results), 9)

    def test_a_hook_that_raises_does_not_stop_the_chain(self):
        def hook(item):
            raise RuntimeError('bad hook')

        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', support.addresses(5))],
            pl.Runners(cheap=support.recording_runner(ok=True), basic=None, expensive=None),
            run_basic=False, on_result=hook)))
        self.assertEqual(result.state, 'complete')
        self.assertEqual(result.metrics.measured, 5)


class ItemTests(unittest.TestCase):
    def test_canonical_addresses_split_into_scheme_host_port(self):
        item = pl.item_from_endpoint('socks5://[2001:db8::1]:1080')
        self.assertEqual((item.scheme, item.host, item.port), ('socks5', '2001:db8::1', 1080))
        self.assertEqual(item.address, '2001:db8::1')
        self.assertEqual(item.ip, '2001:db8::1')

    def test_a_hostname_has_an_address_but_no_ip(self):
        item = pl.item_from_endpoint('http://gate.example.invalid:3128')
        self.assertEqual(item.address, 'gate.example.invalid')
        self.assertIsNone(item.ip)

    def test_a_malformed_canonical_address_is_refused(self):
        for value in ('198.51.100.1:8080', 'http://198.51.100.1', 'http://:8080', '', 7, None):
            with self.subTest(value=value), self.assertRaises(pl.ValidationError) as caught:
                pl.item_from_endpoint(value)
            self.assertEqual(caught.exception.code, pl.E_VALIDATION_FIELD)

    def test_result_state_must_be_a_pipeline_state(self):
        with self.assertRaises(pl.ValidationError):
            pl.ItemResult(endpoint='http://198.51.100.1:8080', state='interesting', ok=True)

    def test_stage_outcome_kind_must_be_a_pipeline_stage(self):
        with self.assertRaises(pl.ValidationError):
            pl.StageOutcome(kind='speedtest', ok=True)


if __name__ == '__main__':
    unittest.main()

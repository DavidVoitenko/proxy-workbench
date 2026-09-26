"""F12: resource budgets, backpressure, adaptive concurrency, limits, control.

CPU, RAM, fds, bytes and requests are what the pipeline manages, and the worker
count is derived from them.  Pause and cancel must stop the whole chain, and a
resumed chain must not measure one item twice.
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


def corpus(count=24):
    return support.addresses(count)


class BudgetValidationTests(unittest.TestCase):
    def test_a_budget_without_a_single_slot_is_refused(self):
        with self.assertRaises(pl.ValidationError) as caught:
            pl.Budgets(max_open_fds=2, fds_per_request=3)
        self.assertIn('FD', caught.exception.message)
        with self.assertRaises(pl.ValidationError):
            pl.Budgets(max_ram_bytes=1024, ram_per_inflight_bytes=4096)

    def test_every_budget_is_range_checked(self):
        cases = {'max_inflight': 0, 'max_open_fds': 0, 'fds_per_request': 0, 'max_queue_items': 0,
                 'max_results_pending': 0, 'max_line_bytes': 0, 'max_stored_results': 0,
                 'max_requests': -1, 'max_bytes': -1, 'max_source_bytes': -1, 'max_items': -1,
                 'max_ram_bytes': 0, 'ram_per_inflight_bytes': 0, 'max_cpu_seconds': 0,
                 'deadline_s': -1.0, 'max_inflight': True}
        for name, value in cases.items():
            with self.subTest(name=name), self.assertRaises(pl.ValidationError):
                pl.Budgets(**{name: value})

    def test_the_worker_count_is_derived_from_fds_ram_and_inflight(self):
        self.assertEqual(pl.Budgets(max_inflight=100, max_open_fds=60, fds_per_request=3,
                                   max_ram_bytes=1 << 30, ram_per_inflight_bytes=1 << 18).worker_ceiling(), 20)
        self.assertEqual(pl.Budgets(max_inflight=5, max_open_fds=1000, fds_per_request=3,
                                   max_ram_bytes=1 << 30, ram_per_inflight_bytes=1 << 18).worker_ceiling(), 5)
        self.assertEqual(pl.Budgets(max_inflight=100, max_open_fds=6, fds_per_request=3,
                                   max_ram_bytes=1 << 30, ram_per_inflight_bytes=1 << 18).worker_ceiling(), 2)
        self.assertEqual(pl.Budgets(max_inflight=100, max_open_fds=1000, fds_per_request=3,
                                   max_ram_bytes=8 << 20, ram_per_inflight_bytes=1 << 20).worker_ceiling(), 8)

    def test_a_target_policy_is_range_checked(self):
        with self.assertRaises(pl.ValidationError):
            pl.TargetPolicy('t', max_inflight=0)
        with self.assertRaises(pl.ValidationError):
            pl.TargetPolicy('', max_inflight=1)
        with self.assertRaises(pl.ValidationError):
            pl.TargetPolicy('t', max_requests=-1)

    def test_limits_refuse_a_repeated_target(self):
        with self.assertRaises(pl.ValidationError):
            pl.Limits(targets=(pl.TargetPolicy('a'), pl.TargetPolicy('a')))


class TotalsTests(unittest.TestCase):
    def test_a_spent_request_budget_ends_the_run(self):
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(40))],
            pl.Runners(cheap=support.recording_runner(ok=True, requests=3), basic=None, expensive=None),
            run_basic=False, budgets=pl.Budgets(max_requests=9, max_bytes=None))))
        self.assertEqual(result.state, 'budget')
        self.assertEqual(result.counters.stage_failures.get('stop'), pl.E_LIMIT_BUDGET)
        self.assertGreaterEqual(result.counters.requests, 9)
        self.assertLess(result.metrics.measured, 40)

    def test_a_spent_byte_budget_ends_the_run(self):
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(40))],
            pl.Runners(cheap=support.recording_runner(ok=True, body_bytes=1024), basic=None,
                       expensive=None),
            run_basic=False, budgets=pl.Budgets(max_requests=None, max_bytes=4096))))
        self.assertEqual(result.state, 'budget')
        self.assertLessEqual(result.counters.bytes, 4096 + 1024)

    def test_an_item_budget_ends_the_run(self):
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(40))],
            pl.Runners(cheap=support.recording_runner(ok=True), basic=None, expensive=None),
            run_basic=False, budgets=pl.Budgets(max_items=5, max_requests=None, max_bytes=None))))
        self.assertEqual(result.state, 'budget')
        self.assertLessEqual(result.counters.unique_endpoints, 6)
        self.assertEqual(result.counters.stage_failures.get('stop'), pl.E_LIMIT_BUDGET)

    def test_a_cpu_budget_ends_the_run(self):
        clock = support.StepClock(step=0.0, cpu_step=1.0)
        result = run(pl.run_pipeline(
            support.config([support.chunk_source('s', corpus(40))],
                          pl.Runners(cheap=support.recording_runner(ok=True), basic=None,
                                     expensive=None),
                          run_basic=False,
                          budgets=pl.Budgets(max_cpu_seconds=3.0, max_requests=None, max_bytes=None)),
            clock=clock))
        self.assertEqual(result.state, 'budget')
        self.assertGreaterEqual(result.counters.cpu_s, 3.0)
        self.assertLess(result.metrics.measured, 40)

    def test_a_deadline_ends_the_run(self):
        started, released = [], []

        async def scenario():
            pending = asyncio.Event()

            async def blocked_runner(item, *, stage, limit):
                started.append(item.endpoint)
                try:
                    # A short sleep can finish early on the coarse Windows
                    # clock. Unfinished I/O must instead be cancelled by the
                    # pipeline deadline, regardless of timer resolution.
                    await pending.wait()
                finally:
                    released.append(item.endpoint)

            return await asyncio.wait_for(pl.run_pipeline(support.config(
                [support.chunk_source('s', corpus(200))],
                pl.Runners(cheap=blocked_runner, basic=None, expensive=None),
                run_basic=False, budgets=pl.Budgets(deadline_s=0.05, max_inflight=4, max_open_fds=12,
                                                   max_requests=None, max_bytes=None))), 1.0)

        result = run(scenario())
        self.assertEqual(result.state, 'budget')
        self.assertEqual(result.counters.stage_failures.get('stop'), pl.DEADLINE_EXCEEDED)
        self.assertLess(result.metrics.measured, 200)
        self.assertTrue(started)
        self.assertCountEqual(released, started)
        self.assertEqual(result.resources.inflight, 0)

    def test_the_deadline_is_handed_to_the_runner_as_remaining_time(self):
        seen = []
        run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(6))],
            pl.Runners(cheap=support.recording_runner(ok=True, calls=seen), basic=None, expensive=None),
            run_basic=False, budgets=pl.Budgets(deadline_s=30.0, max_requests=None, max_bytes=None))))
        self.assertTrue(seen)
        limits = [limit for _, _, limit in seen]
        self.assertTrue(all(0 < limit.remaining_s <= 30.0 for limit in limits))
        self.assertTrue(all(limit.deadline_s is not None for limit in limits))

    def test_the_request_cap_handed_to_a_runner_shrinks_as_the_budget_spends(self):
        seen = []
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(12))],
            pl.Runners(cheap=support.recording_runner(ok=True, calls=seen), basic=None, expensive=None),
            run_basic=False, budgets=pl.Budgets(max_requests=5, max_bytes=None, max_inflight=1,
                                               max_open_fds=3))))
        handed = [limit.remaining_requests for _, _, limit in seen]
        self.assertEqual(handed, [5, 4, 3, 2, 1],
                         'the runner must see the budget it may still spend, shrinking every call')
        self.assertEqual(result.counters.requests, 5)
        self.assertEqual(result.state, 'budget')

    def test_a_spent_budget_is_not_handed_to_a_runner_at_all(self):
        # The budget is checked before a measurement starts, so the runner is
        # never called with an empty budget and never charges for work that was
        # already impossible.
        seen = []
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(12))],
            pl.Runners(cheap=support.recording_runner(ok=True, calls=seen, budget=True), basic=None,
                       expensive=None),
            run_basic=False, budgets=pl.Budgets(max_requests=4, max_bytes=None, max_inflight=1,
                                               max_open_fds=3))))
        self.assertEqual(result.state, 'budget')
        self.assertEqual(result.counters.stage_failures.get('stop'), pl.E_LIMIT_BUDGET)
        self.assertEqual(result.counters.requests, 4)
        self.assertLessEqual(result.metrics.measured, 4)
        self.assertTrue(all(limit.remaining_requests > 0 for _, _, limit in seen))


class CeilingTests(unittest.TestCase):
    def test_the_fd_budget_bounds_what_runs_at_once(self):
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(40))],
            pl.Runners(cheap=support.recording_runner(ok=True, latency_s=0.002), basic=None,
                       expensive=None),
            run_basic=False, budgets=pl.Budgets(max_open_fds=9, fds_per_request=3, max_inflight=32,
                                               max_ram_bytes=1 << 30, max_requests=None,
                                               max_bytes=None))))
        self.assertLessEqual(result.metrics.peak_inflight, 3)
        self.assertLessEqual(result.metrics.peak_fds, 9)
        self.assertEqual(result.metrics.workers, 3)

    def test_the_ram_budget_bounds_what_runs_at_once(self):
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(40))],
            pl.Runners(cheap=support.recording_runner(ok=True, latency_s=0.002), basic=None,
                       expensive=None),
            run_basic=False, budgets=pl.Budgets(max_ram_bytes=1 << 20, ram_per_inflight_bytes=1 << 18,
                                               max_inflight=32, max_open_fds=1000, max_requests=None,
                                               max_bytes=None))))
        self.assertLessEqual(result.metrics.peak_inflight, 4)
        self.assertLessEqual(result.metrics.peak_ram_reserved_bytes, 1 << 20)

    def test_the_queue_bound_is_backpressure_not_a_memory_limit(self):
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(400))],
            pl.Runners(cheap=support.recording_runner(ok=True, latency_s=0.0005), basic=None,
                       expensive=None),
            run_basic=False, budgets=pl.Budgets(max_queue_items=2, max_inflight=2, max_open_fds=6,
                                               max_requests=None, max_bytes=None))))
        self.assertEqual(result.state, 'complete')
        self.assertLessEqual(result.metrics.queue_high_water, 2)
        self.assertEqual(result.counters.unique_endpoints, 400)
        self.assertEqual(result.metrics.measured, 400)

    def test_waiting_for_a_ceiling_is_counted_and_is_not_a_budget_stop(self):
        # Eight workers, one admissible slot: they must queue for it, and the
        # run must still finish as a complete run rather than a budget stop.
        result = run(pl.run_pipeline(
            support.config([support.chunk_source('s', corpus(40))],
                          pl.Runners(cheap=support.recording_runner(ok=True, latency_s=0.002),
                                     basic=None, expensive=None),
                          run_basic=False,
                          budgets=pl.Budgets(max_inflight=8, max_open_fds=300, max_queue_items=40,
                                             max_requests=None, max_bytes=None)),
            concurrency=pl.AdaptiveConcurrency(minimum=1, maximum=1, window=4)))
        self.assertEqual(result.state, 'complete')
        self.assertEqual(result.metrics.peak_inflight, 1)
        self.assertGreater(result.metrics.blocked_waits, 0)
        self.assertEqual(result.counters.requests, 40)
        self.assertEqual(result.counters.stage_failures.get('stop'), None)


class AdaptiveConcurrencyTests(unittest.TestCase):
    def test_the_limit_grows_on_success_and_shrinks_on_failure(self):
        controller = pl.AdaptiveConcurrency(minimum=2, maximum=20, window=8, increase=3,
                                            target_success=0.8, decrease_factor=0.5)
        start = controller.limit
        for _ in range(8):
            controller.observe(True, 0.01)
        self.assertEqual(controller.limit, min(20, start + 3))
        self.assertEqual(controller.snapshot().increases, 1)
        for _ in range(16):
            controller.observe(False, 0.5)
        self.assertEqual(controller.limit, 2, 'a dead target must drive the limit to its floor')
        self.assertGreaterEqual(controller.snapshot().decreases, 1)
        self.assertEqual(controller.snapshot().success_rate, 0.0)

    def test_a_mixed_window_holds_the_limit(self):
        controller = pl.AdaptiveConcurrency(minimum=1, maximum=16, window=10, target_success=0.8)
        before = controller.limit
        for index in range(10):
            controller.observe(index % 2 == 0, 0.01)
        self.assertEqual(controller.limit, before)
        self.assertEqual(controller.snapshot().window, 10)

    def test_the_limit_never_leaves_its_bounds(self):
        controller = pl.AdaptiveConcurrency(minimum=3, maximum=7, window=4, increase=5)
        for _ in range(50):
            controller.observe(True, 0.001)
        self.assertEqual(controller.limit, 7)
        controller = pl.AdaptiveConcurrency(minimum=3, maximum=7, window=4)
        for _ in range(50):
            controller.observe(False, 0.001)
        self.assertEqual(controller.limit, 3)

    def test_it_refuses_impossible_bounds(self):
        for kwargs in ({'minimum': 4, 'maximum': 2}, {'target_success': 0.0}, {'target_success': 1.5},
                       {'decrease_factor': 1.0}, {'window': 0}, {'increase': 0}):
            with self.subTest(**kwargs), self.assertRaises(pl.ValidationError):
                pl.AdaptiveConcurrency(**kwargs)

    def test_the_run_reports_its_own_adaptive_trace(self):
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(200))],
            pl.Runners(cheap=support.recording_runner(ok=True), basic=None, expensive=None),
            run_basic=False, budgets=pl.Budgets(max_inflight=32, max_open_fds=1000, max_requests=None,
                                               max_bytes=None))))
        trace = result.metrics.concurrency
        self.assertEqual(trace['window'], 16, 'the trace keeps a rolling window')
        self.assertEqual(trace['success_rate'], 1.0)
        self.assertGreater(trace['increases'], 0)
        self.assertEqual(trace['limit'], 32, 'the ceiling of the resource budget still wins')

    def test_the_adaptive_limit_shrinks_what_a_dead_endpoint_gets(self):
        slow = []

        async def runner(item, *, stage, limit):
            slow.append(len(slow))
            await asyncio.sleep(0.002)
            return pl.StageOutcome(stage, False, code='UNREACHABLE', failed_stage='tcp',
                                   latency_ms=2.0, requests=1)

        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(120))],
            pl.Runners(cheap=runner, basic=None, expensive=None), run_basic=False,
            budgets=pl.Budgets(max_inflight=64, max_open_fds=300, max_requests=None, max_bytes=None)),
            concurrency=pl.AdaptiveConcurrency(minimum=1, maximum=32, window=8, increase=4)))
        self.assertEqual(result.state, 'complete')
        self.assertLessEqual(result.metrics.peak_inflight, 32)
        self.assertLess(result.metrics.peak_inflight, 32,
                        'a dead endpoint must be walked down, not run flat out')


class HostLimitTests(unittest.TestCase):
    def test_one_measurement_per_host_at_a_time(self):
        concurrent = {'now': 0, 'peak': 0}

        async def runner(item, *, stage, limit):
            concurrent['now'] += 1
            concurrent['peak'] = max(concurrent['peak'], concurrent['now'])
            await asyncio.sleep(0.002)
            concurrent['now'] -= 1
            return pl.StageOutcome(stage, True, requests=1, bytes=1)

        endpoints = [f'http://198.51.100.1:{port}' for port in range(8010, 8030)]
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', endpoints)], pl.Runners(cheap=runner, basic=None,
                                                              expensive=None),
            run_basic=False, limits=pl.Limits(max_per_host=1))))
        self.assertEqual(result.metrics.measured, 20)
        self.assertEqual(concurrent['peak'], 1)
        self.assertEqual(result.metrics.host_waits, 19)

    def test_a_higher_per_host_limit_is_honoured(self):
        concurrent = {'now': 0, 'peak': 0}

        async def runner(item, *, stage, limit):
            concurrent['now'] += 1
            concurrent['peak'] = max(concurrent['peak'], concurrent['now'])
            await asyncio.sleep(0.002)
            concurrent['now'] -= 1
            return pl.StageOutcome(stage, True, requests=1, bytes=1)

        endpoints = [f'http://198.51.100.1:{port}' for port in range(8010, 8030)]
        run(pl.run_pipeline(support.config(
            [support.chunk_source('s', endpoints)], pl.Runners(cheap=runner, basic=None,
                                                              expensive=None),
            run_basic=False, limits=pl.Limits(max_per_host=4))))
        self.assertEqual(concurrent['peak'], 4)

    def test_a_minimum_interval_between_two_probes_of_one_host(self):
        clock = support.StepClock(step=0.5)
        limiter = pl.HostLimiter(max_per_host=1, min_interval_s=1.0, clock=clock)
        host = '198.51.100.1'

        async def scenario():
            await limiter.acquire(host)
            await limiter.release(host)
            self.assertFalse(limiter.free_now(host))
            clock.advance(1.0)
            self.assertTrue(limiter.free_now(host))
            await limiter.acquire(host)
            await limiter.release(host)

        run(scenario())


class TargetLimitTests(unittest.TestCase):
    def expensive_wide(self, seen):
        async def runner(item, *, stage, limit):
            seen.append(limit.target_id)
            await asyncio.sleep(0.001)
            return pl.StageOutcome(stage, True, requests=1, bytes=1, exit_ip='203.0.113.9')

        return runner

    def test_the_expensive_stage_is_pinned_to_its_target(self):
        seen = []
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(6))],
            pl.Runners(cheap=support.recording_runner(ok=True),
                       basic=support.recording_runner(ok=True, exit_ip='203.0.113.9'),
                       expensive=self.expensive_wide(seen)),
            limits=pl.Limits(targets=(pl.TargetPolicy('judge'),)))))
        self.assertEqual(set(seen), {'judge'})
        self.assertEqual(len(seen), result.counters.expensive_checked)

    def test_a_target_request_budget_stops_only_that_targets_stage(self):
        seen = []
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(12))],
            pl.Runners(cheap=support.recording_runner(ok=True),
                       basic=support.recording_runner(ok=True, exit_ip='203.0.113.9'),
                       expensive=self.expensive_wide(seen)),
            limits=pl.Limits(targets=(pl.TargetPolicy('judge', max_requests=3),)),
            budgets=pl.Budgets(max_queue_items=2))))
        self.assertEqual(len(seen), 3, 'the budget must be exact even under concurrency')
        self.assertEqual(result.state, 'complete', 'one target running out is not a chain failure')
        self.assertEqual(result.counters.expensive_checked, 12)
        self.assertEqual(result.counters.passed_endpoints, 3)
        codes = [item.reason_code for item in result.results if not item.ok]
        self.assertEqual(codes, [pl.E_LIMIT_BUDGET] * 9)

    def test_a_target_concurrency_limit_is_honoured(self):
        concurrent = {'now': 0, 'peak': 0}

        async def runner(item, *, stage, limit):
            concurrent['now'] += 1
            concurrent['peak'] = max(concurrent['peak'], concurrent['now'])
            await asyncio.sleep(0.002)
            concurrent['now'] -= 1
            return pl.StageOutcome(stage, True, requests=1, bytes=1)

        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(16))],
            pl.Runners(cheap=support.recording_runner(ok=True),
                       basic=support.recording_runner(ok=True, exit_ip='203.0.113.9'),
                       expensive=runner),
            limits=pl.Limits(targets=(pl.TargetPolicy('judge', max_inflight=2),)))))
        self.assertEqual(result.counters.expensive_checked, 16)
        self.assertLessEqual(concurrent['peak'], 2)


class ControlTests(unittest.TestCase):
    def scenario(self, *, endpoints=40, budgets=None):
        calls = []
        control = pl.PipelineControl()

        async def runner(item, *, stage, limit):
            calls.append(item.endpoint)
            return pl.StageOutcome(stage, True, requests=1, bytes=1, exit_ip='203.0.113.1')

        pipeline = pl.Pipeline(support.config(
            [support.chunk_source('s', support.addresses(endpoints))],
            pl.Runners(cheap=runner, basic=None, expensive=None), run_basic=False,
            budgets=budgets or pl.Budgets(max_inflight=2, max_open_fds=6, max_queue_items=4)),
            control=control)
        return pipeline, control, calls

    def test_cancel_ends_the_whole_chain(self):
        async def cancelling():
            pipeline, control, calls = self.scenario()
            control.cancel()
            return await pipeline.run(), calls

        result, calls = run(cancelling())
        self.assertEqual(result.state, 'cancelled')
        self.assertEqual(result.reason, 'cancelled')
        self.assertEqual(calls, [])

    def test_cancel_from_inside_a_measurement_stops_the_chain(self):
        async def cancelling():
            pipeline, control, calls = self.scenario()

            async def runner(item, *, stage, limit):
                calls.append(item.endpoint)
                control.cancel()
                return pl.StageOutcome(stage, True, requests=1, bytes=1)

            pipeline.config = support.config(
                [support.chunk_source('s', support.addresses(40))],
                pl.Runners(cheap=runner, basic=None, expensive=None), run_basic=False,
                budgets=pl.Budgets(max_inflight=2, max_open_fds=6, max_queue_items=4))
            return await pipeline.run(), calls

        result, calls = run(cancelling())
        self.assertEqual(result.state, 'cancelled')
        self.assertLess(len(calls), 40)
        self.assertTrue(result.remaining or result.metrics.measured < 40)

    def test_pause_stops_the_chain_and_reports_what_is_left(self):
        async def pausing():
            pipeline, control, calls = self.scenario()

            async def runner(item, *, stage, limit):
                calls.append(item.endpoint)
                control.pause()
                return pl.StageOutcome(stage, True, requests=1, bytes=1)

            pipeline.config = support.config(
                [support.chunk_source('s', support.addresses(40))],
                pl.Runners(cheap=runner, basic=None, expensive=None), run_basic=False,
                budgets=pl.Budgets(max_inflight=1, max_open_fds=3, max_queue_items=4))
            return await pipeline.run(), calls

        result, calls = run(pausing())
        self.assertEqual(result.state, 'paused')
        self.assertEqual(result.reason, 'paused')
        self.assertLess(len(calls), 40)
        self.assertFalse(result.feed_complete)
        self.assertTrue(result.remaining)

    def test_resume_measures_the_rest_and_never_repeats_an_item(self):
        async def pausing_then_resuming():
            pipeline, control, calls = self.scenario()
            seen = []

            async def runner(item, *, stage, limit):
                calls.append(item.endpoint)
                seen.append(item.endpoint)
                if len(seen) == 3:
                    control.pause()
                return pl.StageOutcome(stage, True, requests=1, bytes=1, exit_ip='203.0.113.1')

            pipeline.config = support.config(
                [support.chunk_source('s', support.addresses(20))],
                pl.Runners(cheap=runner, basic=None, expensive=None), run_basic=False,
                budgets=pl.Budgets(max_inflight=1, max_open_fds=3, max_queue_items=2))
            first = await pipeline.run()
            control.resume()
            second = await pipeline.run()
            return first, second, calls

        first, second, calls = run(pausing_then_resuming())
        self.assertEqual(first.state, 'paused')
        self.assertEqual(second.state, 'complete')
        self.assertEqual(len(calls), len(set(calls)), 'an endpoint was measured twice')
        self.assertEqual(len(set(calls)), 20)
        self.assertEqual(second.counters.resume_skips, first.metrics.measured)
        self.assertEqual(second.counters.duplicates, 0)

    def test_cancelling_the_task_propagates_and_leaves_the_ledger_consistent(self):
        async def cancelling_the_task():
            async def runner(item, *, stage, limit):
                await asyncio.sleep(0.05)
                return pl.StageOutcome(stage, True, requests=1, bytes=1)

            pipeline, control, calls = self.scenario()
            pipeline.config = support.config(
                [support.chunk_source('s', support.addresses(40))],
                pl.Runners(cheap=runner, basic=None, expensive=None), run_basic=False,
                budgets=pl.Budgets(max_inflight=2, max_open_fds=6, max_queue_items=4))
            task = asyncio.create_task(pipeline.run())
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return pipeline

        pipeline = run(cancelling_the_task())
        self.assertLessEqual(len(pipeline.ledger.finished), len(pipeline.ledger.admitted))
        self.assertEqual(pipeline.last_result.state, 'cancelled')
        self.assertTrue(pipeline.control.cancelled is False,
                        'a task cancellation is not a cooperative cancel request')

    def test_a_control_never_leaves_a_run_waiting_on_it(self):
        control = pl.PipelineControl()
        self.assertFalse(control.paused)
        self.assertFalse(control.cancelled)
        control.pause()
        self.assertTrue(control.paused)
        control.resume()
        self.assertFalse(control.paused)
        control.cancel()
        self.assertTrue(control.cancelled)
        control.reset()
        self.assertEqual(control.to_public(), {'paused': False, 'cancelled': False})


if __name__ == '__main__':
    unittest.main()

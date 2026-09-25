"""F12 acceptance: reproducible time-to-first, time-to-N and RAM on one fixture.

The fixture is the module's own corpus from the RFC 5737 / RFC 3849
documentation ranges and the measurement is a local stub, so nothing here
touches a network.  A synthetic number must never be readable as the speed of
live public proxies, and the tests below check both the numbers and that label.
"""
import asyncio
import sys
import tracemalloc
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_pipeline_support as support  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import pipeline as pl  # noqa: E402

ITEMS = 400
FIND_N = 10


class FixtureTests(unittest.TestCase):
    def test_the_fixture_is_deterministic_and_content_addressed(self):
        first = pl.synthetic_endpoints(ITEMS)
        second = pl.synthetic_endpoints(ITEMS)
        self.assertEqual(first, second)
        self.assertEqual(pl.fixture_digest(first), pl.fixture_digest(second))
        self.assertNotEqual(pl.fixture_digest(first), pl.fixture_digest(pl.synthetic_endpoints(ITEMS + 1)))
        self.assertEqual(len(set(first)), ITEMS)

    def test_the_fixture_is_a_prefix_of_itself(self):
        big = pl.synthetic_endpoints(1000)
        self.assertEqual(pl.synthetic_endpoints(200), big[:200])

    def test_no_fixture_address_is_globally_routable(self):
        for value in pl.synthetic_endpoints(300):
            item = pl.item_from_endpoint(value)
            self.assertIsNotNone(item.ip)
            import ipaddress
            self.assertFalse(item.ip and ipaddress.ip_address(item.ip).is_global,
                             f'{value} is routable, so a fixture could become real traffic')

    def test_the_fixture_normaliser_answers_like_the_projects_own(self):
        for value in ('', 'not an address', 'http://', 'http://host.example:port',
                      'ftp://198.51.100.1:21', 'gate.example.invalid:3128', None, 7):
            with self.subTest(value=value):
                self.assertIsNone(pl.fixture_normalize(value))
        self.assertEqual(pl.fixture_normalize('HTTP://198.51.100.1:8080'), 'http://198.51.100.1:8080')
        self.assertEqual(pl.fixture_normalize('198.51.100.1:8080'), 'http://198.51.100.1:8080')
        self.assertEqual(pl.fixture_normalize(' [2001:DB8::1]:1080 '), 'http://[2001:db8::1]:1080')

    def test_every_fixture_address_is_already_canonical(self):
        # The corpus must survive the normaliser unchanged, otherwise a digest
        # would describe a list the chain then reads differently.
        for value in pl.synthetic_endpoints(500):
            with self.subTest(value=value):
                self.assertEqual(pl.fixture_normalize(value), value)

    def test_the_public_normaliser_rejects_the_fixture_on_purpose(self):
        # The fixture lives in documentation space, so the project's own public
        # normaliser refuses it.  That is why the benchmark says which normaliser
        # it used instead of quietly swapping in a second one.
        from proxy_workbench import proxytool
        self.assertIsNone(proxytool.normalize(pl.synthetic_endpoints(1)[0]))

    def test_the_fixture_count_is_range_checked(self):
        for value in (0, -1, 1.5, True, None):
            with self.subTest(value=value), self.assertRaises(pl.ValidationError):
                pl.synthetic_endpoints(value)


class BenchmarkTests(unittest.TestCase):
    def test_time_to_first_and_time_to_n_are_measured(self):
        report = pl.benchmark(items=ITEMS, find_n=FIND_N, what='exit')
        self.assertEqual(report.state, 'want_reached')
        self.assertGreater(report.time_to_first_s, 0.0)
        self.assertIsNotNone(report.time_to_n_s)
        self.assertLessEqual(report.time_to_first_s, report.time_to_n_s)
        self.assertLessEqual(report.time_to_n_s, report.wall_s)
        self.assertGreater(report.requests, 0)
        self.assertGreater(report.bytes, 0)
        self.assertLess(report.time_to_n_s, report.wall_s,
                        'reaching the N must not take the whole corpus')

    def test_the_corpus_of_a_benchmark_is_stated_not_guessed(self):
        report = pl.benchmark(items=ITEMS, find_n=FIND_N)
        self.assertEqual(report.fixture, pl.BENCHMARK_FIXTURE)
        self.assertEqual(report.schema, pl.BENCHMARK_SCHEMA)
        self.assertEqual(report.digest, pl.fixture_digest(pl.synthetic_endpoints(ITEMS)))
        self.assertEqual(report.items, ITEMS)
        self.assertEqual(report.find_n, FIND_N)
        self.assertEqual(report.find_what, 'exit')

    def test_a_synthetic_number_is_never_readable_as_a_live_proxy_measurement(self):
        report = pl.benchmark(items=200, find_n=5)
        public = report.to_public()
        self.assertTrue(report.synthetic)
        self.assertEqual(public['synthetic'], True)
        self.assertEqual(public['notice'], pl.SYNTHETIC_NOTICE)
        self.assertIn('синтетическая', public['notice'])
        self.assertIn('не являются измерением скорости живых публичных прокси', public['notice'])

    def test_the_same_fixture_gives_the_same_decision_on_every_machine(self):
        first = pl.benchmark(items=ITEMS, find_n=FIND_N)
        second = pl.benchmark(items=ITEMS, find_n=FIND_N)
        for name in ('fixture', 'schema', 'digest', 'items', 'find_n', 'find_what', 'state',
                     'measured', 'requests', 'bytes', 'workers'):
            with self.subTest(name=name):
                self.assertEqual(getattr(first, name), getattr(second, name))

    def test_ram_follows_the_budget_and_not_the_corpus_size(self):
        small = pl.benchmark(items=2000, find_n=1, max_queue_items=16, measure_ram=True)
        large = pl.benchmark(items=20000, find_n=1, max_queue_items=16, measure_ram=True)
        self.assertGreater(small.peak_ram_bytes, 0)
        self.assertLess(large.peak_ram_bytes, 64 * 1024 * 1024,
                        'RAM must be bounded by the queue budget, not by the corpus')
        self.assertLess(large.peak_ram_bytes, small.peak_ram_bytes * 4,
                        'a corpus ten times larger must not cost ten times the memory')

    def test_a_tighter_queue_costs_less_memory_on_the_same_corpus(self):
        roomy = pl.benchmark(items=8000, find_n=1, max_queue_items=1024, measure_ram=True)
        tight = pl.benchmark(items=8000, find_n=1, max_queue_items=4, measure_ram=True)
        self.assertLessEqual(tight.peak_ram_bytes, roomy.peak_ram_bytes)
        self.assertEqual(roomy.state, tight.state)

    def test_peak_inflight_never_exceeds_the_derived_worker_count(self):
        report = pl.benchmark(items=ITEMS, find_n=FIND_N)
        self.assertGreater(report.workers, 0)
        self.assertLessEqual(report.peak_inflight, report.workers)

    def test_a_fixture_run_can_skip_the_ram_measurement(self):
        report = pl.benchmark(items=100, find_n=2, measure_ram=False)
        self.assertEqual(report.peak_ram_bytes, 0)
        self.assertEqual(report.state, 'want_reached')


class VirtualClockTests(unittest.TestCase):
    """With a clock a test controls, the timing is exact rather than plausible.

    A virtual clock never returns a real timeout, so the run is bounded by the
    pipeline's own boundary checks and the numbers below are the same on every
    machine.
    """

    def run_virtual(self, *, items=40, find_n=5, step=0.25):
        clock = support.StepClock(step=step)
        exits = [f'203.0.113.{index + 1}' for index in range(items)]
        runners = pl.Runners(cheap=support.exit_runner(exits), basic=None, expensive=None)
        config = support.config(
            [support.chunk_source('s', pl.synthetic_endpoints(items))], runners,
            find=pl.FindPolicy(n=find_n, what='exit'),
            budgets=pl.Budgets(max_inflight=1, max_open_fds=3, max_queue_items=1,
                               max_requests=None, max_bytes=None))
        return clock, asyncio.run(pl.run_pipeline(config, clock=clock))

    def test_a_virtual_run_is_exactly_repeatable(self):
        _, first = self.run_virtual()
        _, second = self.run_virtual()
        self.assertEqual(first.state, 'want_reached')
        self.assertEqual(first.metrics.time_to_first_s, second.metrics.time_to_first_s)
        self.assertEqual(first.metrics.time_to_n_s, second.metrics.time_to_n_s)
        self.assertEqual(first.metrics.wall_s, second.metrics.wall_s)

    def test_time_to_first_precedes_time_to_n_and_both_precede_the_wall(self):
        _, result = self.run_virtual()
        metrics = result.metrics
        self.assertGreater(metrics.time_to_first_s, 0.0)
        self.assertGreater(metrics.time_to_n_s, metrics.time_to_first_s)
        self.assertLessEqual(metrics.time_to_n_s, metrics.wall_s)
        self.assertGreater(metrics.items_per_s, 0.0)

    def test_a_tighter_queue_does_not_change_what_the_run_decides(self):
        def with_queue(size):
            clock = support.StepClock(step=0.25)
            exits = [f'203.0.113.{index + 1}' for index in range(40)]
            runners = pl.Runners(cheap=support.exit_runner(exits), basic=None, expensive=None)
            config = support.config(
                [support.chunk_source('s', pl.synthetic_endpoints(40))], runners,
                find=pl.FindPolicy(n=5, what='exit'),
                budgets=pl.Budgets(max_inflight=4, max_open_fds=12, max_queue_items=size,
                                   max_requests=None, max_bytes=None))
            return asyncio.run(pl.run_pipeline(config, clock=clock))

        tight, roomy = with_queue(1), with_queue(64)
        self.assertEqual(tight.state, roomy.state)
        self.assertEqual(tight.find.met, roomy.find.met)
        self.assertLessEqual(tight.metrics.queue_high_water, 1)
        self.assertLessEqual(roomy.metrics.queue_high_water, 64)

    def test_a_cpu_budget_is_enforced_in_virtual_time(self):
        clock = support.StepClock(step=0.0, cpu_step=0.5)
        result = asyncio.run(pl.run_pipeline(
            support.config([support.chunk_source('s', pl.synthetic_endpoints(40))],
                          pl.Runners(cheap=support.recording_runner(ok=True), basic=None,
                                     expensive=None),
                          run_basic=False,
                          budgets=pl.Budgets(max_cpu_seconds=2.0, max_requests=None,
                                             max_bytes=None)),
            clock=clock))
        self.assertEqual(result.state, 'budget')
        self.assertLess(result.metrics.measured, 40)


if __name__ == '__main__':
    unittest.main()

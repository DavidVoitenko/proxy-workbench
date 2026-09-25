"""F12: three different N, prior known-good with an age correction, find-N.

N endpoints, N unique IPs and N confirmed exit IPs are three numbers.  These
tests keep them apart, and check that a prior is carried while it is fresh and
re-probed as soon as it is not.
"""
import asyncio
import time
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_pipeline_support as support  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import pipeline as pl  # noqa: E402


def run(coroutine):
    return asyncio.run(coroutine)


def corpus(count=12):
    return support.addresses(count)


def three_units(exits, *, what, n, endpoints=None, limits=None, budgets=None):
    """A run whose every endpoint passes and reports an exit address from
    ``exits``, so the three units can be compared against each other."""
    endpoints = endpoints or corpus(6)
    seen = []
    runners = pl.Runners(cheap=support.exit_runner(['203.0.113.1'], calls=seen),
                         basic=support.exit_runner(exits, calls=seen), expensive=None)
    return run(pl.run_pipeline(support.config(
        [support.chunk_source('s', endpoints)], runners, find=pl.FindPolicy(n=n, what=what),
        limits=limits or pl.Limits(), budgets=budgets or pl.Budgets(max_queue_items=32))))


class ThreeNTests(unittest.TestCase):
    def test_n_endpoints_two_hosts_one_exit_is_three_different_numbers(self):
        endpoints = ['http://198.51.100.1:8080', 'http://203.0.113.1:8080',
                     'http://198.51.100.1:3128', 'http://[2001:db8::1]:8080']
        # every endpoint reports the same exit address
        result = three_units(['203.0.113.9'], what='endpoint', n=99, endpoints=endpoints)
        counters = result.counters
        self.assertEqual(counters.unique_endpoints, 4)
        self.assertEqual(counters.passed_endpoints, 4)
        self.assertEqual(counters.passed_exit_ips, 1)
        self.assertEqual(counters.passed_unique_ips, 3, 'the two ports of one host are one IP')
        self.assertEqual(result.find.met, 4)

    def test_find_by_exit_needs_confirmed_exit_addresses(self):
        # cheap passes but reports no exit address: nothing can be counted.
        seen = []
        runners = pl.Runners(cheap=support.exit_runner([None], calls=seen),
                             basic=support.exit_runner([None], calls=seen), expensive=None)
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(6))], runners,
            find=pl.FindPolicy(n=2, what='exit'))))
        self.assertEqual(result.counters.passed_endpoints, 6)
        self.assertEqual(result.counters.confirmed_exit_ips, 0)
        self.assertEqual(result.find.met, 0)
        self.assertFalse(result.find.satisfied)
        self.assertEqual(result.state, 'complete')

    def test_find_by_ip_never_counts_one_ip_twice(self):
        # six endpoints, one host, three ports, two hosts in total
        endpoints = [f'http://198.51.100.1:{port}' for port in (8080, 3128, 1080)]
        endpoints += [f'http://203.0.113.1:{port}' for port in (8080, 3128, 1080)]
        result = three_units(['203.0.113.9', '203.0.113.8'], what='ip', n=99, endpoints=endpoints)
        self.assertEqual(result.counters.passed_endpoints, 6)
        self.assertEqual(result.counters.passed_unique_ips, 2)
        self.assertEqual(result.find.met, 2)

    def test_find_met_always_equals_the_counter_it_advances(self):
        for what in pl.FIND_BY:
            with self.subTest(what=what):
                result = three_units(['203.0.113.1', '203.0.113.2', '203.0.113.1', '203.0.113.2'],
                                     what=what, n=99)
                self.assertEqual(result.find.met, result.counters.to_public()[pl.COUNT_FIELD[what]])

    def test_a_hostname_endpoint_is_never_counted_as_an_ip(self):
        def normalize(value):
            if 'gate' in value:
                return 'http://gate.example.invalid:3128'
            return support.normalize(value)

        seen = []
        runners = pl.Runners(cheap=support.exit_runner(['203.0.113.5'], calls=seen),
                             basic=support.exit_runner(['203.0.113.5'], calls=seen), expensive=None)
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', ['gate1', '198.51.100.1:8080'])], runners,
            find=pl.FindPolicy(n=1, what='ip'), normalize=normalize)))
        self.assertEqual(result.counters.unique_addresses, 2)
        self.assertEqual(result.counters.unique_ips, 1)
        self.assertEqual(result.counters.passed_endpoints, 2)
        self.assertEqual(result.counters.passed_unique_ips, 1)
        self.assertEqual(result.find.met, 1)

    def test_an_unknown_find_unit_is_refused(self):
        with self.assertRaises(pl.ValidationError) as caught:
            pl.FindPolicy(n=1, what='proxies')
        self.assertEqual(caught.exception.code, pl.E_VALIDATION_FIELD)
        self.assertIn('exit', caught.exception.message)


class FindNStopTests(unittest.TestCase):
    def test_satisfied_n_stops_the_chain_and_reports_what_it_left(self):
        seen = []
        runners = pl.Runners(cheap=support.exit_runner(['203.0.113.1'], calls=seen),
                             basic=support.exit_runner(['203.0.113.1', '203.0.113.2', '203.0.113.3'],
                                                       calls=seen),
                             expensive=support.exit_runner(['203.0.113.1', '203.0.113.2', '203.0.113.3'], calls=seen))
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(200))], runners,
            find=pl.FindPolicy(n=2, what='exit'), budgets=pl.Budgets(max_queue_items=1))))
        self.assertEqual(result.state, 'want_reached')
        self.assertEqual(result.reason, 'want_reached')
        self.assertTrue(result.find.satisfied)
        self.assertGreaterEqual(result.find.met, 2)
        self.assertLess(result.metrics.measured, 200)
        self.assertFalse(result.feed_complete, 'a satisfied N must stop the feed')
        self.assertLess(result.counters.unique_endpoints, 200)

    def test_time_to_n_is_measured_and_is_not_later_than_the_wall_time(self):
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(200))],
            pl.Runners(cheap=support.exit_runner(['203.0.113.1', '203.0.113.2', '203.0.113.3'],
                                                latency_s=0.002), basic=None, expensive=None),
            find=pl.FindPolicy(n=3, what='exit'), budgets=pl.Budgets(max_queue_items=1))))
        self.assertEqual(result.state, 'want_reached')
        self.assertIsNotNone(result.metrics.time_to_n_s)
        self.assertLessEqual(result.metrics.time_to_n_s, result.metrics.wall_s)
        self.assertIsNotNone(result.metrics.time_to_first_s)
        self.assertLessEqual(result.metrics.time_to_first_s, result.metrics.time_to_n_s)
        self.assertIsNotNone(result.metrics.time_to_first_pass_s)

    def test_an_unreachable_n_is_a_complete_run_with_an_unsatisfied_n(self):
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(6))],
            pl.Runners(cheap=support.recording_runner(ok=False), basic=None, expensive=None),
            find=pl.FindPolicy(n=2, what='endpoint'))))
        self.assertEqual(result.state, 'complete')
        self.assertFalse(result.find.satisfied)
        self.assertEqual(result.find.met, 0)
        self.assertEqual(result.find.remaining, 2)
        self.assertEqual(result.remaining, ())

    def test_n_zero_disables_the_search_entirely(self):
        result = three_units(['203.0.113.1'], what='exit', n=0)
        self.assertEqual(result.state, 'complete')
        self.assertEqual(result.find.met, 0)
        self.assertIsNone(pl.FindPolicy(n=0).to_public()['counted'])
        self.assertEqual(result.find.to_public()['target'], 0)


class ExpensivePolicyTests(unittest.TestCase):
    def sweep(self, policy, *, what='ip', n=60, hosts=('198.51.100.1', '203.0.113.1')):
        # Two hosts, many ports: 60 endpoints but only two unique IPs, so most
        # passing items cannot add a new unit of the N being counted.
        endpoints = [f'http://{host}:{port}' for host in hosts for port in range(8000, 8030)]
        seen = []
        runners = pl.Runners(cheap=support.recording_runner(ok=True, calls=seen),
                             basic=support.exit_runner(['203.0.113.9'], calls=seen),
                             expensive=support.exit_runner(['203.0.113.9'], calls=seen))
        return run(pl.run_pipeline(support.config(
            [support.chunk_source('s', endpoints)], runners,
            find=pl.FindPolicy(n=n, what=what), expensive_policy=policy,
            limits=pl.Limits(max_per_host=8), budgets=pl.Budgets(max_queue_items=4))))

    def test_until_n_spends_the_expensive_stage_only_where_it_can_advance_the_n(self):
        until = self.sweep(pl.EXPENSIVE_UNTIL_N)
        every = self.sweep(pl.EXPENSIVE_ALL_PASSING)
        self.assertEqual(until.state, 'complete')
        self.assertEqual(every.state, 'complete')
        self.assertEqual(until.counters.passed_endpoints, 60)
        self.assertEqual(until.counters.passed_unique_ips, 2)
        self.assertEqual(until.counters.expensive_checked, 2,
                         'only the first endpoint of each host can add a new IP')
        self.assertEqual(every.counters.expensive_checked, 60,
                         'all_passing charges every passing item')

    def test_a_full_sweep_without_an_n_keeps_paying_for_every_passing_item(self):
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(12))],
            pl.Runners(cheap=support.recording_runner(ok=True),
                       basic=support.recording_runner(ok=True, exit_ip='203.0.113.9'),
                       expensive=support.recording_runner(ok=True, exit_ip='203.0.113.9')),
            find=pl.FindPolicy(n=0), expensive_policy=pl.EXPENSIVE_UNTIL_N)))
        self.assertEqual(result.state, 'complete')
        self.assertEqual(result.counters.expensive_checked, 12)

    def test_expensive_none_never_runs(self):
        seen = []
        exits = ['203.0.113.1', '203.0.113.2', '203.0.113.3']
        runners = pl.Runners(cheap=support.exit_runner(exits, calls=seen),
                             basic=support.exit_runner(exits, calls=seen),
                             expensive=support.exit_runner(exits, calls=seen))
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', corpus(8))], runners, expensive_policy=pl.EXPENSIVE_NONE)))
        self.assertEqual(result.counters.expensive_checked, 0)
        self.assertEqual(result.state, 'complete')

    def test_an_unknown_expensive_policy_is_refused(self):
        with self.assertRaises(pl.ValidationError):
            support.config([], pl.Runners(cheap=support.recording_runner(), basic=None, expensive=None),
                           expensive_policy='whenever')


class PriorTests(unittest.TestCase):
    def prior(self, endpoints, *, now=None, ages=(0.0, 300.0, 1200.0), value=1.0, max_age_s=900.0):
        # The index's ``now`` and the run's clock must be the same time base, so
        # the fixture uses the wall clock and the pipeline does too.
        now = time.time() if now is None else now
        entries = [pl.PriorItem(endpoint, checked_at=now - age, value=value, exit_ip='203.0.113.7')
                   for endpoint, age in zip(endpoints, ages)]
        return pl.PriorIndex(entries, now=now, max_age_s=max_age_s)

    def test_decay_halves_the_weight_at_one_halflife(self):
        self.assertAlmostEqual(pl.decay_weight(1.0, 0.0), 1.0)
        self.assertAlmostEqual(pl.decay_weight(1.0, pl.AGE_HALFLIFE_S), 0.5)
        self.assertAlmostEqual(pl.decay_weight(1.0, 2 * pl.AGE_HALFLIFE_S), 0.25)
        self.assertAlmostEqual(pl.decay_weight(0.5, pl.AGE_HALFLIFE_S), 0.25)

    def test_decay_clamps_a_future_stamp_instead_of_promoting_it(self):
        self.assertAlmostEqual(pl.decay_weight(1.0, -500.0), 1.0)

    def test_decay_refuses_nonsense(self):
        for value, age in ((float('nan'), 1.0), (1.0, float('inf')), (True, 1.0)):
            with self.subTest(value=value, age=age), self.assertRaises(pl.ValidationError):
                pl.decay_weight(value, age)
        with self.assertRaises(pl.ValidationError):
            pl.decay_weight(1.0, 1.0, halflife_s=0)

    def test_priors_are_ordered_by_decayed_weight_not_by_staleness_alone(self):
        fresh, middling, stale = ('http://198.51.100.1:8080', 'http://198.51.100.2:8080',
                                  'http://198.51.100.3:8080')
        index = pl.PriorIndex(
            [pl.PriorItem(stale, checked_at=100.0, value=1.0),
             pl.PriorItem(middling, checked_at=700.0, value=1.0),
             pl.PriorItem(fresh, checked_at=1000.0, value=1.0)],
            now=1000.0, max_age_s=1000.0)
        self.assertEqual(index.ranked_endpoints(), (fresh, middling, stale))
        self.assertGreater(index.weight(fresh), index.weight(middling))
        self.assertGreater(index.weight(middling), index.weight(stale))

    def test_ties_are_broken_by_name_so_the_order_is_reproducible(self):
        endpoints = sorted(support.addresses(4))
        index = pl.PriorIndex([pl.PriorItem(item, checked_at=1000.0) for item in endpoints],
                              now=1000.0, max_age_s=900.0)
        self.assertEqual(index.ranked_endpoints(), tuple(endpoints))

    def test_a_stale_duplicate_prior_does_not_demote_the_newest_one(self):
        endpoint = support.addresses(1)[0]
        index = pl.PriorIndex(
            [pl.PriorItem(endpoint, checked_at=900.0, value=0.1),
             pl.PriorItem(endpoint, checked_at=1000.0, value=1.0)],
            now=1000.0, max_age_s=900.0)
        self.assertEqual(len(index), 1)
        self.assertAlmostEqual(index.weight(endpoint), 1.0)

    def test_a_future_stamp_is_reported_as_a_clock_anomaly(self):
        endpoint = support.addresses(1)[0]
        index = pl.PriorIndex([pl.PriorItem(endpoint, checked_at=1500.0)], now=1000.0, max_age_s=900.0)
        self.assertEqual(index.future_endpoints, (endpoint,))
        self.assertLess(index.age(endpoint), 0)

    def test_a_fresh_prior_is_carried_and_never_re_probed(self):
        endpoints = support.addresses(3)
        index = self.prior(endpoints, ages=(10.0, 20.0, 30.0))
        calls = []
        result = run(pl.run_pipeline(support.config(
            [], pl.Runners(cheap=support.recording_runner(calls=calls), basic=None, expensive=None),
            priors=index)))
        self.assertEqual(calls, [], 'a fresh prior must not be measured again')
        self.assertEqual(result.counters.carried, 3)
        self.assertEqual(result.counters.cheap_checked, 0)
        self.assertEqual(result.counters.passed_endpoints, 3)
        self.assertEqual(result.counters.confirmed_exit_ips, 1)
        self.assertTrue(all(item.carried for item in result.results))
        self.assertTrue(all(item.age_seconds is not None and item.age_seconds < 900
                            for item in result.results))
        self.assertTrue(all(item.checked_at is not None for item in result.results),
                        'a carried result must still say when it was measured')

    def test_an_expired_prior_is_re_probed(self):
        endpoints = support.addresses(3)
        index = self.prior(endpoints, ages=(10.0, 1000.0, 5000.0))
        calls = []
        result = run(pl.run_pipeline(support.config(
            [], pl.Runners(cheap=support.recording_runner(calls=calls), basic=None, expensive=None),
            priors=index)))
        self.assertEqual(result.counters.carried, 1)
        self.assertEqual(result.counters.cheap_checked, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sorted(item[1] for item in calls), sorted(endpoints[1:]))

    def test_carry_can_be_switched_off(self):
        endpoints = support.addresses(2)
        calls = []
        result = run(pl.run_pipeline(support.config(
            [], pl.Runners(cheap=support.recording_runner(calls=calls), basic=None, expensive=None),
            priors=self.prior(endpoints, ages=(10.0, 10.0)), carry_fresh_prior=False)))
        self.assertEqual(result.counters.carried, 0)
        self.assertEqual(len(calls), 2)

    def test_a_carried_prior_is_not_measured_again_when_the_source_repeats_it(self):
        endpoints = support.addresses(3)
        calls = []
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', endpoints)],
            pl.Runners(cheap=support.recording_runner(calls=calls), basic=None, expensive=None),
            priors=self.prior(endpoints, ages=(10.0, 10.0, 10.0)))))
        self.assertEqual(calls, [])
        self.assertEqual(result.counters.carried, 3)
        self.assertEqual(result.counters.duplicates, 3)
        self.assertEqual(result.counters.cheap_checked, 0)

    def test_priors_are_queued_before_the_corpus(self):
        # An endpoint that is not in the fixture, and a prior that is already
        # expired: it is re-probed, and it is the first thing offered.
        prior_endpoint = 'http://198.51.100.254:3128'
        calls = []

        async def runner(item, *, stage, limit):
            calls.append(item.endpoint)
            return pl.StageOutcome(stage, True, requests=1, bytes=1, exit_ip='203.0.113.1')

        now = time.time()
        index = pl.PriorIndex([pl.PriorItem(prior_endpoint, checked_at=now - 5000.0)], now=now,
                              max_age_s=60.0)
        self.assertFalse(index.is_fresh(prior_endpoint))
        result = run(pl.run_pipeline(support.config(
            [support.chunk_source('s', support.addresses(10))],
            pl.Runners(cheap=runner, basic=None, expensive=None), priors=index,
            find=pl.FindPolicy(n=1, what='exit'))))
        self.assertEqual(calls[0], prior_endpoint,
                         'a known-good endpoint must be offered before anything new')
        self.assertEqual(result.counters.carried, 0)
        self.assertEqual(result.state, 'want_reached')

    def test_prior_index_refuses_a_bad_clock_or_window(self):
        endpoint = support.addresses(1)[0]
        with self.assertRaises(pl.ValidationError):
            pl.PriorIndex([pl.PriorItem(endpoint, checked_at=1.0)], now='now', max_age_s=10)
        with self.assertRaises(pl.ValidationError):
            pl.PriorIndex([pl.PriorItem(endpoint, checked_at=1.0)], now=1.0, max_age_s=-1)
        with self.assertRaises(pl.ValidationError):
            pl.PriorIndex([pl.PriorItem(endpoint, checked_at=1.0)], now=1.0, max_age_s=10, halflife_s=0)

    def test_config_refuses_a_prior_index_of_the_wrong_type(self):
        with self.assertRaises(pl.ValidationError):
            support.config([], pl.Runners(cheap=support.recording_runner(), basic=None, expensive=None),
                           priors={'endpoint': 'http://198.51.100.1:8080'})


if __name__ == '__main__':
    unittest.main()

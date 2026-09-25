"""F14 acceptance scenarios: desired N, reserve, refill, budgets and recovery.

Every scenario is local: a temporary database, a fake source and a fake
measurement. No address is dialled and no external service is contacted.
"""
from __future__ import annotations

import unittest

from proxy_workbench import pools

from tests.pools_support import FakeSource, ManualClock, PoolTestCase


class AcceptanceScenarioTest(PoolTestCase):
    """The four scenarios MASTER-PROMPT F14 asks for, plus their neighbours."""

    def test_two_failures_are_replaced_from_the_reserve_back_to_five(self):
        self.create_pool(desired=5, minimum=3, reserve=2)
        source = FakeSource({
            pools.SOURCE_RESERVE: self.candidates(2, start=6, score=10),
            pools.SOURCE_SOURCES: self.candidates(5),
        })

        filled = pools.refill(self.store, 'main', source, now=self.now)
        self.assertEqual(filled.served, 5)
        self.assertEqual(filled.counts[pools.MEMBER_RESERVE], 2)
        self.assertEqual(filled.state, pools.STATE_COMPLETE)
        self.assertIsNone(filled.deficit_reason)
        self.assertTrue(filled.ready_for_clients)

        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 10)
        pools.report_health(self.store, 'main', 'ep-02', ok=False, now=self.now + 10)
        degraded = pools.refill(self.store, 'main', source, now=self.now + 20)

        self.assertEqual(degraded.served, 5, 'the reserve restores service without new candidates')
        self.assertEqual(degraded.promotions, 2)
        self.assertEqual(degraded.counts[pools.MEMBER_COOLDOWN], 2)
        self.assertEqual(degraded.state, pools.STATE_COMPLETE)
        self.assertEqual(self.states()['active'], 5)
        self.assertEqual(self.member_states(),
                         {'ep-01': pools.MEMBER_COOLDOWN, 'ep-02': pools.MEMBER_COOLDOWN,
                          'ep-03': pools.MEMBER_ACTIVE, 'ep-04': pools.MEMBER_ACTIVE,
                          'ep-05': pools.MEMBER_ACTIVE, 'ep-06': pools.MEMBER_ACTIVE,
                          'ep-07': pools.MEMBER_ACTIVE})

    def test_without_a_reserve_the_pool_reports_three_of_five(self):
        self.create_pool(desired=5, minimum=0, reserve=2)
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(5)})

        status = pools.refill(self.store, 'main', source, now=self.now)
        self.assertEqual(status.served, 5)
        self.assertEqual(status.counts[pools.MEMBER_RESERVE], 0)

        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 5)
        pools.report_health(self.store, 'main', 'ep-02', ok=False, now=self.now + 5)
        short = pools.refill(self.store, 'main', source, now=self.now + 10)

        self.assertEqual(short.served, 3)
        self.assertEqual(short.desired, 5)
        self.assertEqual(short.shortfall, 2)
        self.assertEqual(short.state, pools.STATE_DEGRADED)
        self.assertEqual(short.next_attempt_at, self.now + 10 + 60)
        self.assertEqual([(entry['code'], entry['count']) for entry in short.as_dict()['deficit_reasons']],
                         [(pools.REASON_COOLDOWN, 2)],
                         'the source had nothing but the two members that are resting')

    def test_a_source_with_nothing_to_offer_says_so(self):
        self.create_pool(desired=3, reserve=0)
        status = pools.refill(self.store, 'main', FakeSource(), now=self.now)

        self.assertEqual(status.served, 0)
        self.assertEqual(status.state, pools.STATE_EMPTY)
        self.assertEqual(status.deficit_reason, pools.REASON_NO_CANDIDATES)
        self.assertEqual(status.next_attempt_at, self.now + 60)
        self.assertFalse(status.ready_for_clients)
        self.assertIsNone(status.deficit_reason if status.state == pools.STATE_COMPLETE else None)

    def test_budget_zero_stops_the_refill(self):
        self.create_pool(desired=5, minimum=0, reserve=2, policy={'refill_budget': 0})
        source = FakeSource({
            pools.SOURCE_RESERVE: self.candidates(2, start=6, score=10),
            pools.SOURCE_SOURCES: self.candidates(5),
        })

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 0)
        self.assertEqual(status.budget_spent, 0)
        self.assertEqual(status.budget_limit, 0)
        self.assertEqual(status.deficit_reason, pools.REASON_BUDGET)
        self.assertEqual(status.state, pools.STATE_EMPTY)
        self.assertEqual(source.asked, [], 'a zero budget must not even ask the source')
        self.assertEqual(self.states()['active'], 0)

    def test_budget_zero_also_blocks_promotion_from_the_reserve(self):
        self.create_pool(desired=5, minimum=0, reserve=2)
        source = FakeSource({
            pools.SOURCE_RESERVE: self.candidates(2, start=6, score=10),
            pools.SOURCE_SOURCES: self.candidates(5),
        })
        pools.refill(self.store, 'main', source, now=self.now)
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 1)

        blocked = pools.refill(self.store, 'main', source, now=self.now + 2, budget=0)
        self.assertEqual(blocked.served, 4)
        self.assertEqual(blocked.promotions, 0)
        self.assertEqual(blocked.deficit_reason, pools.REASON_BUDGET)

        allowed = pools.refill(self.store, 'main', source, now=self.now + 3, budget=1)
        self.assertEqual(allowed.served, 5)
        self.assertEqual(allowed.promotions, 1)
        self.assertEqual(allowed.state, pools.STATE_COMPLETE)

    def test_a_crash_keeps_the_target_and_the_next_refill_finishes_it(self):
        self.create_pool(desired=7, minimum=3, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(20)})
        self.assertEqual(pools.refill(self.store, 'main', source, now=self.now).served, 7)
        for round_no, failing in enumerate((('ep-01', 'ep-02', 'ep-03'),
                                            ('ep-05', 'ep-06', 'ep-07')), start=1):
            for endpoint in failing:
                pools.report_health(self.store, 'main', endpoint, ok=False, now=self.now + round_no)
            self.assertEqual(pools.refill(self.store, 'main', source,
                                         now=self.now + round_no + 1).served, 7)
        for endpoint in ('ep-09', 'ep-10', 'ep-11'):
            pools.report_health(self.store, 'main', endpoint, ok=False, now=self.now + 3)

        crashing = _CrashingStore(self.store)
        with self.assertRaises(RuntimeError):
            pools.refill(crashing, 'main', source, now=self.now + 4)

        reopened = pools.PoolStore.open(self.path)
        self.addCleanup(reopened.close)
        spec = reopened.get('main')
        self.assertEqual((spec.desired, spec.minimum, spec.reserve), (7, 3, 0),
                         'the target state is not part of a refill transaction')
        self.assertEqual(len(reopened.members('main')), 13, 'no member of the failed refill survived')
        self.assertEqual(reopened.member('main', 'ep-01').state, pools.MEMBER_COOLDOWN)
        self.assertEqual(sum(1 for member in reopened.members('main')
                             if member.state == pools.MEMBER_ACTIVE), 4)

        recovered = pools.refill(reopened, 'main', source, now=self.now + 20)
        self.assertEqual(recovered.served, 7)
        self.assertEqual(recovered.state, pools.STATE_COMPLETE)


class _CrashingStore(pools.PoolStore):
    """The same database, but a process that dies in the middle of a refill.

    The first member write succeeds and the second raises, which is what a
    process killed during a refill looks like from the storage side.
    """

    def __init__(self, store: pools.PoolStore):
        super().__init__(store.conn)
        self.writes = 0

    def add_member(self, pool_id, endpoint_id, state, *, now):
        self.writes += 1
        if self.writes > 1:
            raise RuntimeError('simulated crash inside the refill transaction')
        super().add_member(pool_id, endpoint_id, state, now=now)


class RefillOrderTest(PoolTestCase):
    """The pool asks the cheap tiers first and never works past its budget."""

    def test_tiers_are_asked_in_reserve_known_sources_order(self):
        self.create_pool(desired=4, reserve=2)
        source = FakeSource({
            pools.SOURCE_RESERVE: self.candidates(2, start=7, score=1),
            pools.SOURCE_KNOWN: self.candidates(1, start=6, score=2),
            pools.SOURCE_SOURCES: self.candidates(4),
        })

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(source.asked, [pools.SOURCE_RESERVE, pools.SOURCE_KNOWN, pools.SOURCE_SOURCES])
        self.assertEqual(status.served, 4)
        self.assertEqual(status.counts[pools.MEMBER_RESERVE], 2)
        self.assertEqual(status.admissions, 6)

    def test_the_source_is_never_asked_beyond_the_budget(self):
        self.create_pool(desired=9, reserve=0, policy={'refill_budget': 2})
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(9)})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.budget_spent, 2)
        self.assertEqual(status.served, 2)
        self.assertEqual(status.deficit_reason, pools.REASON_BUDGET)
        for _kind, budget, _now in source.calls:
            self.assertLessEqual(budget, 2)

    def test_a_failing_source_is_reported_and_keeps_the_previous_membership(self):
        self.create_pool(desired=2, reserve=0)
        good = FakeSource({pools.SOURCE_SOURCES: self.candidates(2)})
        pools.refill(self.store, 'main', good, now=self.now)
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 1)
        pools.report_health(self.store, 'main', 'ep-02', ok=False, now=self.now + 1)

        status = pools.refill(self.store, 'main', FakeSource(error=OSError('source is unreachable')),
                              now=self.now + 2)

        self.assertEqual(status.served, 0)
        self.assertEqual(status.deficit_reason, pools.REASON_SOURCE_ERROR)
        self.assertEqual(status.source_errors, ('OSError',))
        self.assertEqual(len(self.store.members('main')), 2)
        self.assertEqual(self.states()[pools.MEMBER_COOLDOWN], 2)

    def test_scan_limit_bounds_the_work_of_one_refill(self):
        self.create_pool(desired=6, reserve=0, policy={'scan_limit': 3, 'refill_budget': 10})
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(3)})

        first = pools.refill(self.store, 'main', source, now=self.now)
        self.assertEqual(first.served, 3)
        self.assertEqual(first.deficit_reason, pools.REASON_BUDGET)
        for _kind, budget, _now in source.calls:
            self.assertLessEqual(budget, 3)

        second = pools.refill(self.store, 'main', FakeSource({pools.SOURCE_SOURCES: self.candidates(3, start=4)}),
                              now=self.now + 1)
        self.assertEqual(second.served, 6)
        self.assertEqual(second.state, pools.STATE_COMPLETE)


class MemberPhaseTest(PoolTestCase):
    """Cooldown, probation and re-admission: the pool recovers, it does not shrink."""

    def test_a_failed_member_returns_after_cooldown_and_a_new_measurement(self):
        self.create_pool(desired=1, minimum=1, reserve=0, policy={'cooldown_seconds': 60})
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(1)})
        pools.refill(self.store, 'main', source, now=self.now)

        failure = pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 1)
        self.assertEqual(failure.state, pools.MEMBER_COOLDOWN)
        self.assertEqual(failure.next_try_at, self.now + 61)

        too_early = pools.refill(self.store, 'main', source, now=self.now + 30)
        self.assertEqual(too_early.served, 0)
        self.assertEqual(too_early.state, pools.STATE_EMPTY)
        self.assertEqual(too_early.counts[pools.MEMBER_COOLDOWN], 1)

        resting = pools.refill(self.store, 'main', source, now=self.now + 61)
        self.assertEqual(resting.counts[pools.MEMBER_PROBATION], 1)
        self.assertEqual(resting.served, 0, 'a member in probation is not in service until it is measured')
        self.assertEqual(resting.recheck_due, ('ep-01',))

        measured = pools.refill(self.store, 'main', source, now=self.now + 62,
                                verify=lambda spec, member: True)
        self.assertEqual(measured.served, 1)
        self.assertEqual(measured.re_admissions, 1)
        self.assertEqual(measured.state, pools.STATE_COMPLETE)
        self.assertEqual(self.states()[pools.MEMBER_ACTIVE], 1)

    def test_a_failing_re_measurement_sends_the_member_back_to_cooldown(self):
        self.create_pool(desired=1, reserve=0, policy={'cooldown_seconds': 60})
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(1)})
        pools.refill(self.store, 'main', source, now=self.now)
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 1)

        status = pools.refill(self.store, 'main', source, now=self.now + 61,
                              verify=lambda spec, member: False)

        self.assertEqual(status.served, 0)
        self.assertEqual(status.counts[pools.MEMBER_COOLDOWN], 1)
        self.assertEqual(self.store.member('main', 'ep-01').released_at, self.now + 61)

    def test_a_measurement_that_broke_is_not_proof_of_a_dead_proxy(self):
        self.create_pool(desired=1, reserve=0, policy={'cooldown_seconds': 30})
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(1)})
        pools.refill(self.store, 'main', source, now=self.now)
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 1)

        def broken(spec, member):
            raise TimeoutError('measurement timed out')

        status = pools.refill(self.store, 'main', source, now=self.now + 31, verify=broken)

        self.assertEqual(status.served, 0)
        self.assertEqual(status.counts[pools.MEMBER_PROBATION], 1)
        self.assertEqual(status.source_errors, ('TimeoutError',))
        self.assertEqual(status.deficit_reason, pools.REASON_NO_CANDIDATES)

    def test_a_report_for_a_member_that_is_resting_is_not_counted_as_success(self):
        self.create_pool(desired=1, reserve=0, policy={'cooldown_seconds': 120})
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(1)})
        pools.refill(self.store, 'main', source, now=self.now)
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 1)

        result = pools.report_health(self.store, 'main', 'ep-01', ok=True, now=self.now + 2)

        self.assertFalse(result.applied)
        self.assertEqual(result.state, pools.MEMBER_COOLDOWN)
        self.assertEqual(result.next_try_at, self.now + 121)
        self.assertEqual(self.states()[pools.MEMBER_COOLDOWN], 1)

    def test_a_success_for_a_member_of_a_full_pool_waits_in_the_reserve(self):
        self.create_pool(desired=1, reserve=1)
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(2)})
        pools.refill(self.store, 'main', source, now=self.now)
        self.assertEqual(self.states()[pools.MEMBER_ACTIVE], 1)
        self.assertEqual(self.states()[pools.MEMBER_RESERVE], 1)

        result = pools.report_health(self.store, 'main', 'ep-02', ok=True, now=self.now + 1)

        self.assertTrue(result.applied)
        self.assertEqual(result.state, pools.MEMBER_RESERVE)
        self.assertEqual(self.states()[pools.MEMBER_ACTIVE], 1)

    def test_a_shrinking_target_moves_members_to_the_reserve_instead_of_dropping_them(self):
        self.create_pool(desired=4, reserve=2)
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(4)})
        pools.refill(self.store, 'main', source, now=self.now)

        self.store.set_target('main', desired=2, minimum=1)
        status = pools.refill(self.store, 'main', source, now=self.now + 1)

        self.assertEqual(status.served, 2)
        self.assertEqual(status.counts[pools.MEMBER_RESERVE], 2)
        self.assertEqual(len(self.store.members('main')), 4)

    def test_a_proof_older_than_the_max_age_stops_being_served(self):
        self.create_pool(desired=2, reserve=0, policy={'max_age_seconds': 120, 'probation_seconds': 60})
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(2)})
        pools.refill(self.store, 'main', source, now=self.now)

        stale = pools.refill(self.store, 'main', source, now=self.now + 300)

        self.assertEqual(stale.served, 0)
        self.assertEqual(stale.counts[pools.MEMBER_PROBATION], 2)
        self.assertEqual(stale.state, pools.STATE_EMPTY)
        self.assertEqual(len(self.store.members('main')), 2, 'the members are kept, they are not deleted')
        self.assertEqual(stale.probation_overdue, ('ep-01', 'ep-02'))
        self.assertEqual(stale.recheck_due, ('ep-01', 'ep-02'))

    def test_a_successful_report_refreshes_the_proof(self):
        self.create_pool(desired=1, reserve=0, policy={'max_age_seconds': 120})
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(1)})
        pools.refill(self.store, 'main', source, now=self.now)
        pools.report_health(self.store, 'main', 'ep-01', ok=True, now=self.now + 100)

        status = pools.refill(self.store, 'main', source, now=self.now + 200)

        self.assertEqual(status.served, 1)
        self.assertEqual(self.store.member('main', 'ep-01').admitted_at, self.now + 100)


class WatchTest(PoolTestCase):
    """The watch loop restores the pool instead of only re-checking the winners."""

    def test_watch_keeps_the_pool_at_its_target_over_ticks(self):
        self.create_pool(desired=3, minimum=2, reserve=1, policy={'cooldown_seconds': 60})
        source = FakeSource({
            pools.SOURCE_RESERVE: self.candidates(1, start=4, score=5),
            pools.SOURCE_SOURCES: self.candidates(3),
        })
        clock = ManualClock(self.now)
        seen = []

        def watch_one(status):
            seen.append(status.served)
            if len(seen) == 2:
                # Two of the three members die between the second and the third tick.
                for endpoint in ('ep-02', 'ep-03'):
                    pools.report_health(self.store, 'main', endpoint, ok=False, now=clock.now())
            return True

        statuses = pools.watch(self.store, 'main', source, ticks=4, clock=clock, on_status=watch_one,
                               verify=lambda spec, member: True)

        self.assertEqual(seen, [3, 3, 3, 3], 'the pool is back at its target after the failures')
        self.assertTrue(all(status.state == pools.STATE_COMPLETE for status in statuses))
        self.assertEqual(clock.slept, [300, 300, 300])

    def test_watch_does_not_look_at_a_pool_before_it_asks_to_be_looked_at(self):
        self.create_pool(desired=1, reserve=0, policy={'interval_seconds': 300, 'retry_interval_seconds': 600})
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(1)})
        clock = ManualClock(self.now)

        statuses = pools.watch(self.store, 'main', source, ticks=2, clock=clock, budget=0)

        self.assertEqual(statuses[0].served, 0)
        self.assertEqual(statuses[1].deferred, True, 'the second tick is earlier than next_attempt_at')
        self.assertEqual(statuses[1].budget_spent, 0)
        self.assertEqual(source.asked, [])

    def test_watch_stops_when_the_callback_says_so(self):
        self.create_pool(desired=1, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(1)})
        clock = ManualClock(self.now)

        statuses = pools.watch(self.store, 'main', source, ticks=5, clock=clock,
                               on_status=lambda status: False)

        self.assertEqual(len(statuses), 1)
        self.assertEqual(clock.slept, [])

    def test_a_watch_without_a_tick_count_runs_until_it_is_told_to_stop(self):
        self.create_pool(desired=2, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(2)})
        clock = ManualClock(self.now)
        seen = []

        statuses = pools.watch(self.store, 'main', source, clock=clock,
                               on_status=lambda status: seen.append(status.served) or len(seen) < 3)

        self.assertEqual(seen, [2, 2, 2])
        self.assertEqual(len(statuses), 3)
        self.assertEqual(clock.slept, [300, 300])


class MultiPoolTest(PoolTestCase):
    """Several named pools with their own target, limits and budget."""

    def test_pools_do_not_share_members_or_budget(self):
        self.add_collection('col-own', 'private')
        self.create_pool('de', desired=2, minimum=1, reserve=0)
        self.create_pool('own', collection_id='col-own', desired=2, minimum=1, reserve=0,
                         policy={'refill_budget': 1, 'countries': ['DE']})
        public = FakeSource({pools.SOURCE_SOURCES: self.candidates(2)})
        own = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(index, collection_id='col-own', origin_domain=pools.DOMAIN_OWN)
            for index in (1, 2)]})

        first = pools.refill(self.store, 'de', public, now=self.now)
        second = pools.refill(self.store, 'own', own, now=self.now)

        self.assertEqual((first.served, first.budget_spent), (2, 2))
        self.assertEqual((second.served, second.budget_spent), (1, 1))
        self.assertEqual(second.deficit_reason, pools.REASON_BUDGET)
        self.assertEqual([member.endpoint_id for member in self.store.members('own')], ['ep-01'])

    def test_refill_all_spends_one_shared_budget(self):
        self.create_pool('a', desired=3, reserve=0)
        self.create_pool('b', desired=3, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(3)})

        statuses = pools.refill_all(self.store, source, now=self.now, budget=4)

        self.assertEqual(statuses['a'].served, 3)
        self.assertEqual(statuses['b'].served, 1)
        self.assertEqual(statuses['b'].deficit_reason, pools.REASON_BUDGET)
        self.assertEqual(sum(status.budget_spent for status in statuses.values()), 4)

    def test_pools_are_listed_and_addressed_by_name(self):
        self.create_pool('alpha', desired=1, reserve=0)
        self.create_pool('beta', desired=1, reserve=0)

        self.assertEqual([spec.id for spec in self.store.list()], ['alpha', 'beta'])
        self.assertEqual(self.store.get('beta').desired, 1)
        with self.assertRaises(pools.PoolError) as caught:
            self.store.require('gamma')
        self.assertEqual(caught.exception.code, pools.REASON_UNKNOWN_POOL)


if __name__ == '__main__':
    unittest.main()

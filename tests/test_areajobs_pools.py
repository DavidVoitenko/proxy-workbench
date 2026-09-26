"""F14 acceptance and defect 12, run literally.

MASTER-PROMPT F14: «N=5 → два отказа → восстановление до 5; отсутствие резерва →
честное 3/5; budget=0 останавливает refill; после crash сохраняется целевое
состояние», plus the quotas with honest unknown, the degraded report and the
find-N unit the pool asks the measuring engine for.

Defect 12: «watch/refill восстанавливает пул, а не навсегда исключает
отвалившиеся адреса».

Nothing here touches the network. Addresses come from RFC 5737
(198.51.100.0/24) and RFC 5737 exit addresses, the source and the measurement
are local callables, and the database is a temporary file migrated by
``db.migrate()``.
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from proxy_workbench import db, pools
from proxy_workbench import pipeline as chain

from tests.pools_support import FakeSource, ManualClock, PoolTestCase  # noqa: E402

POLICY = {'cooldown_seconds': 300, 'probation_seconds': 120, 'retire_seconds': 86400,
          'retry_interval_seconds': 60, 'interval_seconds': 300, 'refill_budget': 20,
          'max_age_seconds': 7200}


class AcceptanceScenarioTest(PoolTestCase):
    """The four sentences of the F14 acceptance, each with the numbers around it."""

    def create(self, **kwargs) -> pools.PoolSpec:
        values = dict(POLICY)
        values.update(kwargs.pop('policy', {}))
        return self.store.create('main', collection_id=self.collection_id, profile_id='p1',
                                 profile_revision=2, policy=values, **kwargs)

    def test_n_five_two_failures_and_recovery_back_to_five(self):
        self.create(desired=5, minimum=3, reserve=2)
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(8)})

        filled = pools.refill(self.store, 'main', source, now=self.now)
        self.assertEqual((filled.served, filled.state), (5, pools.STATE_COMPLETE))
        self.assertEqual(filled.counts[pools.MEMBER_RESERVE], 2)

        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 10)
        pools.report_health(self.store, 'main', 'ep-02', ok=False, now=self.now + 11)
        degraded = self.store.status('main', now=self.now + 11)
        self.assertEqual(degraded.served, 3, 'two failures take two out of service at once')

        restored = pools.refill(self.store, 'main', source, now=self.now + 12)
        self.assertEqual(restored.served, 5, 'the reserve restores service with no new candidate')
        self.assertEqual(restored.promotions, 2)
        self.assertEqual(self.states()[pools.MEMBER_ACTIVE], 5)
        self.assertEqual(restored.state, pools.STATE_COMPLETE)

    def test_without_a_reserve_the_pool_says_three_of_five_and_why(self):
        self.create(desired=5, minimum=0, reserve=0)
        # the source knows five addresses and offers nothing else
        source = FakeSource({pools.SOURCE_KNOWN: self.candidates(5)})
        pools.refill(self.store, 'main', source, now=self.now)
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 5)
        pools.report_health(self.store, 'main', 'ep-02', ok=False, now=self.now + 6)

        status = pools.refill(self.store, 'main', source, now=self.now + 7)

        self.assertEqual(status.served, 3)
        self.assertEqual(status.shortfall, 2)
        self.assertEqual(status.state, pools.STATE_DEGRADED)
        self.assertEqual(status.deficit_reason, pools.REASON_COOLDOWN)
        self.assertEqual([(entry['code'], entry['count'])
                          for entry in status.as_dict()['deficit_reasons']],
                         [(pools.REASON_COOLDOWN, 2)])
        self.assertEqual(status.next_attempt_at, self.now + 7 + 60,
                         'the next attempt is a number the user can read')

    def test_budget_zero_stops_the_refill_before_it_asks_the_source(self):
        self.create(desired=5, minimum=3, reserve=2)
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(8)})

        status = pools.refill(self.store, 'main', source, now=self.now, budget=0)

        self.assertEqual(status.served, 0)
        self.assertEqual(status.budget_spent, 0)
        self.assertEqual(status.deficit_reason, pools.REASON_BUDGET)
        self.assertEqual(status.state, pools.STATE_EMPTY)
        self.assertEqual(source.asked, [], 'a zero budget does not even look at the source')
        self.assertEqual(self.states()[pools.MEMBER_ACTIVE], 0)
        self.assertEqual(self.store.get('main').state, pools.STATE_EMPTY,
                         'the target itself is untouched, only the work is')

    def test_budget_zero_also_blocks_a_promotion_from_the_reserve(self):
        self.create(desired=5, minimum=0, reserve=2)
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(8)})
        pools.refill(self.store, 'main', source, now=self.now)
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 1)

        blocked = pools.refill(self.store, 'main', source, now=self.now + 2, budget=0)
        self.assertEqual(blocked.promotions, 0)
        self.assertEqual(blocked.deficit_reason, pools.REASON_BUDGET)

        allowed = pools.refill(self.store, 'main', source, now=self.now + 3, budget=1)
        self.assertEqual(allowed.promotions, 1)
        self.assertEqual(allowed.served, 5)

    def test_the_target_state_survives_a_crash_and_the_next_refill_finishes_it(self):
        from contextlib import ExitStack
        import tempfile
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            path = Path(directory) / db.DB_FILENAME
            store = pools.PoolStore.open(path, migrate=db.migrate)
            cleanup.callback(store.close)
            store.create('main', collection_id=self.collection_id, profile_id='p1',
                         profile_revision=2, desired=5, minimum=3, reserve=2, policy=POLICY)
            source = FakeSource({pools.SOURCE_SOURCES: self.candidates(20)})
            pools.refill(store, 'main', source, now=self.now)
            before = store.status('main', now=self.now)
            members = {member.endpoint_id: member.state for member in store.members('main')}
            store.close()  # the process is gone; nothing was shut down gracefully

            reopened = pools.PoolStore.open(path, migrate=db.migrate)
            cleanup.callback(reopened.close)
            after = reopened.status('main', now=self.now)

            self.assertEqual(after.served, before.served)
            self.assertEqual({m.endpoint_id: m.state for m in reopened.members('main')}, members)
            spec = reopened.get('main')
            self.assertEqual((spec.desired, spec.minimum, spec.reserve), (5, 3, 2))

            # an unfinished target is completed by the next refill after the restart
            reopened.set_target('main', desired=7)
            reopened.close()
            third = pools.PoolStore.open(path, migrate=db.migrate)
            cleanup.callback(third.close)
            self.assertEqual(third.get('main').desired, 7)
            finished = pools.refill(third, 'main', source, now=self.now + 1)
            self.assertEqual(finished.served, 7)
            self.assertEqual(finished.state, pools.STATE_COMPLETE)


class DefectTwelveTest(PoolTestCase):
    """Defect 12: a failed address rests and comes back; it is never banned."""

    def setUp(self):
        super().setUp()
        self.store.create('main', collection_id=self.collection_id, profile_id='p1',
                          profile_revision=2, desired=3, minimum=3, reserve=1, policy=POLICY)
        self.working = {'ep-01', 'ep-02', 'ep-03', 'ep-04', 'ep-05'}
        self.rows = [self.candidate(index) for index in range(1, 8)]

    def source(self, spec, kind, budget, now):
        members = {member.endpoint_id for member in self.store.members(spec.id)}
        offered = []
        for row in self.rows:
            is_member = row.endpoint_id in members
            if kind == pools.SOURCE_RESERVE and not is_member:
                continue
            if kind != pools.SOURCE_RESERVE and is_member:
                continue
            # the measurement says which endpoints work right now; a fresh one each tick
            offered.append(pools.Candidate(
                endpoint_id=row.endpoint_id, canonical=row.canonical,
                collection_id=self.collection_id, origin_domain=pools.DOMAIN_OWN,
                protocol='http', country='DE', asn=64500 + int(row.endpoint_id[-2:]),
                exit_ip=f'203.0.113.{row.endpoint_id[-2:]}', checked_at=now - 30,
                valid_until=now + 1800, allowed=row.endpoint_id in self.working, score=50.0))
        return offered[:max(0, int(budget))]

    def verify(self, spec, member):
        return member.endpoint_id in self.working

    def test_a_failed_address_returns_after_the_cooldown_and_a_new_measurement(self):
        pools.refill(self.store, 'main', self.source, now=self.now, verify=self.verify)
        self.working.discard('ep-01')
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 10)
        self.assertEqual(self.store.member('main', 'ep-01').state, pools.MEMBER_COOLDOWN)

        self.working.add('ep-01')  # it works again
        status = pools.refill(self.store, 'main', self.source, now=self.now + 400,
                              verify=self.verify)

        # The reserve is promoted before members are re-measured, so a recovered
        # address lands wherever there is room — active, or the reserve when the
        # pool is already full.  Either way it is back, which is the requirement.
        self.assertIn(self.store.member('main', 'ep-01').state,
                      (pools.MEMBER_ACTIVE, pools.MEMBER_RESERVE),
                      'the address came back instead of being banned')
        self.assertEqual(status.re_admissions, 1, 'it took a new measurement, not a guess')
        self.assertEqual(status.served, 3, 'the target is met')

    def test_an_address_that_keeps_failing_stays_in_the_table_and_keeps_getting_measured(self):
        pools.refill(self.store, 'main', self.source, now=self.now, verify=self.verify)
        self.working.discard('ep-01')
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 10)
        for round_no in range(1, 4):
            pools.refill(self.store, 'main', self.source, now=self.now + 10 + round_no * 400,
                         verify=self.verify)
            self.assertIsNotNone(self.store.member('main', 'ep-01'),
                                 f'round {round_no}: the address was dropped from the pool')
        self.assertIn(self.store.member('main', 'ep-01').state,
                      (pools.MEMBER_COOLDOWN, pools.MEMBER_PROBATION, pools.MEMBER_RESERVE))

    def test_watch_holds_the_target_over_ticks_and_a_failure_recovers(self):
        pools.refill(self.store, 'main', self.source, now=self.now, verify=self.verify)
        self.working.difference_update({'ep-01', 'ep-03'})
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 1)
        pools.report_health(self.store, 'main', 'ep-03', ok=False, now=self.now + 1)
        self.working.update({'ep-01', 'ep-03'})

        statuses = pools.watch(self.store, 'main', self.source, ticks=4, verify=self.verify,
                               clock=ManualClock(self.now + 10))

        self.assertEqual(len(statuses), 4)
        self.assertGreaterEqual(statuses[-1].served, 3, 'watch restores the target')
        self.assertIn(self.store.member('main', 'ep-01').state,
                      (pools.MEMBER_ACTIVE, pools.MEMBER_RESERVE))

    def test_a_measurement_that_raised_is_not_taken_for_a_dead_address(self):
        calls = []

        def broken(spec, member):
            calls.append(member.endpoint_id)
            raise TimeoutError('the probe timed out')

        # a source that can only offer the pool's own members, so the only way
        # back into service is a measurement that actually answered
        members_only = lambda spec, kind, budget, now: [
            row for row in self.source(spec, kind, budget, now)
            if row.endpoint_id in {m.endpoint_id for m in self.store.members(spec.id)}]
        pools.refill(self.store, 'main', self.source, now=self.now)
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 1)
        pools.report_health(self.store, 'main', 'ep-02', ok=False, now=self.now + 1)

        status = pools.refill(self.store, 'main', members_only, now=self.now + 400, verify=broken)

        self.assertTrue(calls, 'the measurement was actually attempted')
        self.assertIn('TimeoutError', status.source_errors)
        self.assertEqual(self.store.member('main', 'ep-01').state, pools.MEMBER_PROBATION,
                         'a broken measurement is not a verdict of death')
        self.assertEqual(status.served, 2, 'a member with no measurement is not served')
        self.assertEqual(status.re_admissions, 0)
        self.assertEqual(sorted(status.recheck_due), ['ep-01', 'ep-02'],
                         'both are listed for whoever runs measurements')


class QuotaTest(PoolTestCase):
    """Country / protocol / ASN / unique exit-IP with honest unknown (F14)."""

    def create(self, **kwargs) -> pools.PoolSpec:
        values = dict(POLICY)
        values.update(kwargs.pop('policy', {}))
        return self.store.create('main', collection_id=self.collection_id, profile_id='p1',
                                 profile_revision=2, policy=values, **kwargs)

    def _refill(self, rows, **kwargs):
        return pools.refill(self.store, 'main',
                            lambda spec, kind, budget, now: rows[:max(0, int(budget))],
                            now=self.now, **kwargs)

    def test_the_exit_ip_quota_caps_members_per_exit_and_reports_the_rejects(self):
        self.create(desired=3, minimum=0, reserve=0, policy={'quota': {pools.QUOTA_EXIT_IP: 1}})
        rows = [self.candidate(1, exit_ip='203.0.113.9'), self.candidate(2, exit_ip='203.0.113.9'),
                self.candidate(3, exit_ip='203.0.113.7')]

        status = self._refill(rows)

        self.assertEqual(status.served, 2, 'two proxies behind one exit are one exit')
        self.assertIn((pools.REASON_QUOTA[3], 1),
                      [(entry['code'], entry['count'])
                       for entry in status.as_dict()['deficit_reasons']])

    def test_an_unknown_exit_is_reported_as_unknown_not_counted_as_a_value(self):
        self.create(desired=3, minimum=0, reserve=0,
                    policy={'quota': {pools.QUOTA_EXIT_IP: 1},
                            'quota_unknown': {pools.QUOTA_EXIT_IP: pools.UNKNOWN_IGNORE}})
        rows = [self.candidate(1, exit_ip='203.0.113.9'), self.candidate(2, exit_ip=None),
                self.candidate(3, exit_ip=None), self.candidate(4, exit_ip='203.0.113.7')]

        status = self._refill(rows)

        self.assertEqual(status.served, 3)
        self.assertEqual(status.quota_unknown.get(pools.QUOTA_EXIT_IP), 2,
                         'the pool says how many members it cannot describe')
        self.assertNotIn(pools.REASON_QUOTA_UNKNOWN[3],
                         [entry['code'] for entry in status.as_dict()['deficit_reasons']])

    def test_an_unknown_exit_fails_closed_when_the_user_asked_it_to(self):
        self.create(desired=3, minimum=0, reserve=0,
                    policy={'quota': {pools.QUOTA_EXIT_IP: 1},
                            'quota_unknown': {pools.QUOTA_EXIT_IP: pools.UNKNOWN_REJECT}})
        rows = [self.candidate(1, exit_ip='203.0.113.9'), self.candidate(2, exit_ip=None)]

        status = self._refill(rows)

        self.assertEqual(status.served, 1)
        self.assertIn(pools.REASON_QUOTA_UNKNOWN[3],
                      [entry['code'] for entry in status.as_dict()['deficit_reasons']])

    def test_the_country_protocol_and_asn_quotas_each_cap_their_dimension(self):
        for dimension, rows in (
                (pools.QUOTA_COUNTRY, [self.candidate(1, country='DE'),
                                       self.candidate(2, country='DE'),
                                       self.candidate(3, country='NL')]),
                (pools.QUOTA_PROTOCOL, [self.candidate(1, protocol='http'),
                                        self.candidate(2, protocol='http'),
                                        self.candidate(3, protocol='socks5')]),
                (pools.QUOTA_ASN, [self.candidate(1, asn=100), self.candidate(2, asn=100),
                                   self.candidate(3, asn=200)])):
            with self.subTest(dimension=dimension):
                self.setUp()
                self.create(desired=3, minimum=0, reserve=0, policy={'quota': {dimension: 1}})
                status = self._refill(rows)
                self.assertEqual(status.served, 2, f'{dimension}: the second value is refused')
                self.assertEqual(status.as_dict()['quota_conflicts'], [],
                                 f'{dimension}: no over-quota member is in service')
                self.addCleanup(self.store.close)

    def test_a_country_filter_is_never_relaxed_to_fill_the_pool(self):
        self.create(desired=2, minimum=0, reserve=0, policy={'countries': ['NL']})
        rows = [self.candidate(1, country='DE'), self.candidate(2, country='FR'),
                self.candidate(3, country=None)]

        status = self._refill(rows)

        self.assertEqual(status.served, 0)
        self.assertEqual(status.state, pools.STATE_EMPTY)
        self.assertEqual(status.deficit_reason, pools.REASON_COUNTRY_FILTER)
        self.assertFalse(status.ready_for_clients)
        self.assertEqual([m.endpoint_id for m in self.store.members('main')], [],
                         'not one address outside the filter was admitted')

    def test_a_public_discovery_endpoint_never_enters_a_private_collection(self):
        self.add_collection('col-private', 'private')
        self.store.create('main', collection_id='col-private', profile_id='p1',
                          profile_revision=2, desired=1, minimum=0, reserve=0, policy=POLICY)
        rows = [self.candidate(1, collection_id='col-private', origin_domain=pools.DOMAIN_PUBLIC),
                self.candidate(2, collection_id='col-private', origin_domain=pools.DOMAIN_OWN)]

        status = pools.refill(self.store, 'main',
                              lambda spec, kind, budget, now: rows[:max(0, int(budget))],
                              now=self.now)

        self.assertEqual(status.served, 1)
        self.assertEqual([m.endpoint_id for m in self.store.members('main')], ['ep-02'])
        self.assertIn(pools.REASON_PUBLIC_IN_PRIVATE,
                      [entry['code'] for entry in status.as_dict()['deficit_reasons']],
                      'the refusal is reported even though the pool reached its target')

    def test_an_empty_pool_reports_a_number_a_reason_and_a_next_attempt(self):
        self.create(desired=3, minimum=1, reserve=0)

        status = self._refill([])

        self.assertEqual(status.served, 0)
        self.assertEqual(status.state, pools.STATE_EMPTY)
        self.assertEqual(status.deficit_reason, pools.REASON_NO_CANDIDATES)
        self.assertEqual(status.next_attempt_at, self.now + 60)
        self.assertFalse(status.ready_for_clients,
                         'no hidden direct connection may stand in for an empty pool')
        self.assertTrue(status.below_minimum)


class FindUnitTest(PoolTestCase):
    """Which of the three N the pool asks the measuring engine for.

    HANDOFF/pipeline.md §1.5 asked for ``FindPolicy(n=pool.desired, what='exit')``.
    It is not what this module does, and the reason is in the schema: a member is
    a row of ``pool_member``, which holds no exit address, so a count of exits
    cannot survive the next restart.  The unit is therefore *derived* — exits when
    the policy caps members per exit, endpoints otherwise — and travels with the
    status so the caller never has to guess.
    """

    def _status(self, **policy):
        values = dict(POLICY)
        values.update(policy)
        self.store.create('main', collection_id=self.collection_id, profile_id='p1',
                          profile_revision=2, desired=5, minimum=3, reserve=0, policy=values)
        return pools.refill(self.store, 'main',
                            lambda spec, kind, budget, now: self.candidates(3)[:max(0, int(budget))],
                            now=self.now)

    def test_a_plain_pool_asks_for_endpoints(self):
        status = self._status()
        self.assertEqual(status.count_unit, 'endpoint')
        self.assertEqual(status.find, {'n': 2, 'what': 'endpoint'})
        self.assertEqual(status.find_policy(chain.FindPolicy).what, 'endpoint')

    def test_a_pool_that_caps_members_per_exit_asks_for_confirmed_exits(self):
        status = self._status(quota={pools.QUOTA_EXIT_IP: 1})
        self.assertEqual(status.count_unit, 'exit')
        self.assertEqual(status.find, {'n': 2, 'what': 'exit'})
        self.assertEqual(status.find_policy(chain.FindPolicy).what, 'exit')

    def test_the_unit_it_asks_for_is_one_the_engine_accepts(self):
        for policy in ({}, {'quota': {pools.QUOTA_EXIT_IP: 1}},
                       {'quota': {pools.QUOTA_COUNTRY: 2}}):
            with self.subTest(policy=policy):
                self.setUp()
                status = self._status(**policy)
                built = status.find_policy(chain.FindPolicy)
                self.assertIn(built.what, chain.FIND_BY,
                              'the engine refused the unit the pool asked in')
                self.assertEqual(built.n, status.shortfall)
                self.addCleanup(self.store.close)

    def test_the_three_units_are_the_same_vocabulary_the_cli_and_the_pipeline_use(self):
        self.assertEqual(pools.FIND_UNITS, chain.FIND_BY)

    def test_a_full_pool_asks_for_nothing(self):
        self.store.create('main', collection_id=self.collection_id, profile_id='p1',
                          profile_revision=2, desired=2, minimum=1, reserve=0, policy=POLICY)
        status = pools.refill(self.store, 'main',
                              lambda spec, kind, budget, now: self.candidates(5)[:max(0, int(budget))],
                              now=self.now)
        self.assertEqual(status.find, {'n': 0, 'what': 'endpoint'})
        self.assertFalse(status.find_policy(chain.FindPolicy).enabled)

    def test_ip_is_never_the_unit_of_a_pool(self):
        """The address of a proxy is not a thing a pool counts across a restart."""
        for pool_id in ('a', 'b'):
            self.store.create(pool_id, collection_id=self.collection_id, profile_id='p1',
                              profile_revision=2, desired=1, minimum=0, reserve=0,
                              policy={**POLICY, 'quota': {pools.QUOTA_EXIT_IP: 1}})
            self.assertNotEqual(self.store.get(pool_id).count_unit, 'ip')
            self.assertNotEqual(self.store.get(pool_id).count_unit, pools.QUOTA_EXIT_IP,
                                'the quota word is not the engine word')


class ConcurrentWriterTest(PoolTestCase):
    """A measurement is the caller's I/O and must not hold the database's write lock."""

    def test_another_writer_gets_in_while_the_pool_is_measuring(self):
        self.store.create('main', collection_id=self.collection_id, profile_id='p1',
                          profile_revision=2, desired=2, minimum=0, reserve=0, policy=POLICY)
        rows = self.candidates(4)
        pools.refill(self.store, 'main',
                     lambda spec, kind, budget, now: rows[:max(0, int(budget))], now=self.now)
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now)
        pools.report_health(self.store, 'main', 'ep-02', ok=False, now=self.now)

        observed = []

        def verify(spec, member):
            other = sqlite3.connect(str(self.path), isolation_level=None, timeout=0.05)
            try:
                other.execute('BEGIN IMMEDIATE')
                other.execute("INSERT OR REPLACE INTO pool_member"
                              " (pool_id, endpoint_id, state, admitted_at, released_at)"
                              " VALUES ('probe', 'x', 'active', 0, NULL)")
                other.commit()
                observed.append('free')
            except sqlite3.Error:
                observed.append('locked')
            finally:
                other.close()
            return True

        status = pools.refill(self.store, 'main',
                              lambda spec, kind, budget, now: rows[:max(0, int(budget))],
                              now=self.now + 400, verify=verify)

        self.assertEqual(set(observed), {'free'},
                         'the write lock is held across a network round trip')
        self.assertEqual(status.re_admissions, 2)
        self.assertEqual(status.served, 2)

    def test_a_crash_between_the_phase_changes_and_the_admissions_leaves_a_pool_a_refill_finishes(self):
        from contextlib import ExitStack
        import tempfile
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            path = Path(directory) / db.DB_FILENAME
            store = pools.PoolStore.open(path, migrate=db.migrate)
            cleanup.callback(store.close)
            store.create('main', collection_id=self.collection_id, profile_id='p1',
                         profile_revision=2, desired=5, minimum=3, reserve=2, policy=POLICY)
            rows = self.candidates(10)
            pools.refill(store, 'main',
                         lambda spec, kind, budget, now: rows[:max(0, int(budget))], now=self.now)
            pools.report_health(store, 'main', 'ep-01', ok=False, now=self.now + 1)
            pools.report_health(store, 'main', 'ep-02', ok=False, now=self.now + 1)

            # a process that dies inside the measurement: phases committed, verdicts not
            store.close()
            reopened = pools.PoolStore.open(path, migrate=db.migrate)
            cleanup.callback(reopened.close)
            members = {m.endpoint_id: m.state for m in reopened.members('main')}
            self.assertEqual(len(members), 7, 'no member is lost by a crash mid-measurement')
            self.assertEqual(sorted(members), ['ep-01', 'ep-02', 'ep-03', 'ep-04',
                                              'ep-05', 'ep-06', 'ep-07'])
            self.assertEqual(members['ep-01'], pools.MEMBER_COOLDOWN,
                             'the two that failed are still resting, not deleted')
            self.assertEqual(reopened.get('main').desired, 5, 'the target survived')

            finished = pools.refill(reopened, 'main',
                                    lambda spec, kind, budget, now: rows[:max(0, int(budget))],
                                    now=self.now + 400, verify=lambda spec, member: True)
            self.assertEqual(finished.served, 5, 'the next refill completes the target')


class NamedPoolsTest(PoolTestCase):
    """Several named pools after one engine, each with its own budget."""

    def test_two_pools_keep_their_own_members_and_share_a_budget(self):
        self.store.create('alpha', collection_id=self.collection_id, profile_id='p1',
                          profile_revision=2, desired=2, minimum=0, reserve=0, policy=POLICY)
        self.store.create('beta', collection_id=self.collection_id, profile_id='p1',
                          profile_revision=2, desired=3, minimum=0, reserve=0, policy=POLICY)
        alpha_rows = self.candidates(2, start=1)
        beta_rows = self.candidates(3, start=20)
        source = lambda spec, kind, budget, now: (alpha_rows if spec.id == 'alpha' else beta_rows)[:max(0, int(budget))]

        first = pools.refill(self.store, 'alpha', source, now=self.now)
        second = pools.refill(self.store, 'beta', source, now=self.now)

        self.assertEqual((first.served, second.served), (2, 3))
        self.assertEqual({m.endpoint_id for m in self.store.members('alpha')},
                         {'ep-01', 'ep-02'})
        self.assertEqual(len({m.endpoint_id for m in self.store.members('beta')}), 3)

        shared = pools.refill_all(self.store, source, now=self.now, budget=1)
        self.assertEqual(sum(status.budget_spent for status in shared.values()) <= 1, True,
                         'one shared budget caps the whole background pass')


if __name__ == '__main__':
    unittest.main()

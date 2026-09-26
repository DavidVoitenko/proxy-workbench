"""F21: honest comparison of sources and suppliers.

The fixture is deliberately *distinguishable*: ``pub-A`` and ``pub-B`` hand out
byte-identical address sets, exactly like the copy pairs the source research
found, and ``pub-C`` adds addresses nobody else has.  If the comparison is
honest, the copy is visible as a copy and ``pub-B`` adds nothing.

Documentation ranges only -- no test ever points at a real proxy.
"""
import itertools
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import db
from proxy_workbench import source_catalog
from proxy_workbench import sourcedesk as sd

T0 = 1_757_000_000.0
HOUR = 3600.0
WINDOWS = [(T0 + index * HOUR, T0 + (index + 1) * HOUR) for index in range(4)]

SHARED = [f'198.51.100.{n}' for n in range(1, 31)]     # pub-A and pub-B, identical
EXCLUSIVE = [f'203.0.113.{n}' for n in range(1, 13)]   # pub-C only


def verdict(reliability, *, ms=120.0, byte_count=2048, requests=2):
    return json.dumps({'proxy': 'redacted', 'reliability': reliability,
                       'min_target_reliability': reliability, 'latency_ms': ms,
                       'successes': int(round(reliability * requests)), 'requests': requests,
                       'samples': [{'ok': True, 'bytes': byte_count, 'ms': ms}
                                   for _ in range(requests)]})


def build(path):
    """A real migrated database with observations written through the real schema."""
    conn, _report = db.open_db(str(path))
    for canonical in SHARED + EXCLUSIVE:
        conn.execute('INSERT OR IGNORE INTO endpoints(id, canonical, country, country_source, '
                     'asn, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)',
                     (db.endpoint_id(canonical), canonical,
                      'NL' if canonical in SHARED else 'DE',
                      'geoip' if canonical in SHARED else 'source', 64500, T0, T0 + 4 * HOUR))
    conn.execute('INSERT OR IGNORE INTO accesses(id, endpoint_id, mode, access_revision, created_at) '
                 'VALUES (?,?,?,?,?)',
                 ('acc-1', db.endpoint_id(SHARED[0]), 'none', 1, T0))
    for proxy in SHARED:
        for source in ('pub-A', 'pub-B'):
            conn.execute('INSERT OR IGNORE INTO candidate_seen(proxy, source, endpoint_id) '
                         'VALUES (?,?,?)', (proxy, source, db.endpoint_id(proxy)))
    for proxy in EXCLUSIVE:
        conn.execute('INSERT OR IGNORE INTO candidate_seen(proxy, source, endpoint_id) VALUES (?,?,?)',
                     (proxy, 'pub-C', db.endpoint_id(proxy)))
    conn.execute("INSERT INTO job(id, kind, state, scope_json, profile_id, profile_revision, "
                 "collection_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 ('job-1', 'check', 'want_reached',
                  json.dumps({'collection_id': 'public', 'profile_id': 'p1', 'profile_revision': 1,
                              'filters': {'want': 40, 'count_what': 'endpoint', 'countries': ['NL']},
                              'budgets': {'max_requests': 500}}),
                  'p1', 1, 'public', T0))

    def observe(canonical, at, payload, error=None, stage=None):
        conn.execute('INSERT INTO observations(id, job_id, endpoint_id, access_id, access_revision, '
                     'profile_id, profile_revision, started_at, finished_at, verdict, error_code, '
                     'error_stage) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                     (f'obs-{db.endpoint_id(canonical)}-{int(at)}', 'job-1',
                      db.endpoint_id(canonical), 'acc-1', 1, 'p1', 1, at, at + 2.0,
                      payload, error, stage))

    for index, (start, _end) in enumerate(WINDOWS):
        at = start + 60
        for number, proxy in enumerate(SHARED):
            if number < 20 or (number < 26 and index < 2):
                observe(proxy, at, verdict(1.0))
            else:
                observe(proxy, at, '', 'UNREACHABLE', 'tcp')
        for number, proxy in enumerate(EXCLUSIVE):
            if number < 4:
                observe(proxy, at, verdict(1.0))
            elif number < 6:
                # The target was down.  This says nothing about the address and
                # must not become a zero -- and must not become a pass either.
                observe(proxy, at, verdict(0.0), 'TARGET_UNAVAILABLE', 'target')
            else:
                observe(proxy, at, '', 'CONNECT_TIMEOUT', 'tcp')
    conn.commit()
    return conn


class CompareBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        cls.conn = build(Path(cls._tmp.name) / 'f21.sqlite3')

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls._tmp.cleanup()

    def cohort(self, index=0, **kwargs):
        start, end = WINDOWS[index]
        return sd.Cohort(start, end, profile_id='p1', profile_revision=1,
                         collection_id='public', **kwargs)

    def compare(self, sources=('pub-A', 'pub-B', 'pub-C'), index=0, **kwargs):
        return sd.compare_sources(self.conn, sources=list(sources), cohort=self.cohort(index), **kwargs)


class CopyDetectionTests(CompareBase):
    """The central acceptance: copies are not counted as independent publishers."""

    def test_two_publishers_with_identical_sets_are_reported_as_identical(self):
        pair = next(p for p in self.compare().overlaps
                    if {p.left, p.right} == {'pub-A', 'pub-B'})
        self.assertTrue(pair.identical)
        self.assertEqual(pair.shared, len(SHARED))
        self.assertEqual(pair.jaccard, 1.0)
        self.assertEqual((pair.left_only, pair.right_only), (0, 0))

    def test_the_copy_contributes_nothing_unique(self):
        report = self.compare()
        copy = report.row('pub-B')
        self.assertEqual(copy.offered, len(SHARED))
        self.assertEqual(copy.unique_offered, 0)
        self.assertEqual(copy.unique_admitted, 0)

    def test_identical_publishers_land_in_one_family(self):
        family = next(f for f in self.compare().families if len(f.members) > 1)
        self.assertEqual(family.members, ('pub-A', 'pub-B'))
        self.assertTrue(family.identical_group)

    def test_a_source_with_its_own_addresses_is_credited_with_them(self):
        row = self.compare().row('pub-C')
        self.assertEqual(row.unique_offered, len(EXCLUSIVE))
        self.assertEqual(row.unique_admitted, 4)

    def test_a_shared_address_is_credited_to_but_counted_once(self):
        family = next(f for f in self.compare().families if len(f.members) > 1)
        # 30 shared addresses, offered by two publishers, stored as one family.
        self.assertEqual(family.endpoints, len(SHARED))

    def test_the_copy_is_called_out_as_shared_cost(self):
        codes = [note.code for note in self.compare().biases]
        self.assertIn(sd.BIAS_SHARED_COST, codes)
        self.assertIn(sd.BIAS_ORDER, codes)


class OrderIndependenceTests(CompareBase):
    def test_permuting_the_sources_changes_nothing(self):
        sources = ['pub-A', 'pub-B', 'pub-C']
        baseline = self.compare(sources).as_dict()
        for permutation in itertools.permutations(sources):
            with self.subTest(order=permutation):
                self.assertEqual(self.compare(list(permutation)).as_dict(), baseline)

    def test_duplicates_in_the_request_do_not_inflate_anything(self):
        once = self.compare(['pub-A', 'pub-B']).as_dict()
        twice = self.compare(['pub-A', 'pub-B', 'pub-A', 'pub-B']).as_dict()
        self.assertEqual(once, twice)


class UnknownIsNotZeroTests(CompareBase):
    def test_an_unconcluded_measurement_leaves_the_denominator(self):
        row = self.compare().row('pub-C')
        # 4 pass, 6 measured failures, 2 measurements that concluded nothing.
        self.assertEqual((row.passed, row.failed, row.unknown), (4, 6, 2))
        self.assertAlmostEqual(row.reliability, 4 / 10)
        self.assertNotIn(0.0, (row.reliability,))

    def test_a_target_side_failure_is_not_the_address_fault(self):
        state, _reason = sd.classify_measurement({'min_target_reliability': 0.0},
                                                 'TARGET_UNAVAILABLE')
        self.assertEqual(state, sd.OUTCOME_UNKNOWN)

    def test_a_transport_failure_is_a_failure_even_without_a_payload(self):
        state, _reason = sd.classify_measurement(None, 'UNREACHABLE')
        self.assertEqual(state, sd.OUTCOME_FAIL)

    def test_an_unreadable_verdict_is_unknown(self):
        self.assertEqual(sd.classify_measurement(None)[0], sd.OUTCOME_UNKNOWN)
        self.assertEqual(sd.classify_measurement('{not json')[0], sd.OUTCOME_UNKNOWN)

    def test_a_verdict_without_a_measurement_is_unknown_not_zero(self):
        state, _reason = sd.classify_measurement({'proxy': 'x', 'samples': []})
        self.assertEqual(state, sd.OUTCOME_UNKNOWN)

    def test_unknown_is_named_as_a_bias(self):
        self.assertIn(sd.BIAS_UNKNOWN, [note.code for note in self.compare().biases])


class NoDataTests(CompareBase):
    def test_a_window_without_observations_says_no_data(self):
        empty = sd.Cohort(T0 + 99 * HOUR, T0 + 100 * HOUR, profile_id='p1', profile_revision=1)
        report = sd.compare_sources(self.conn, sources=['pub-A', 'pub-C'], cohort=empty)
        self.assertFalse(report.has_data)
        for row in report.rows:
            self.assertEqual(row.status, sd.STATUS_NO_DATA)
            self.assertIsNone(row.reliability)
            self.assertIsNone(row.reliability_low)
            self.assertIsNone(row.reliability_high)
        self.assertTrue(any('неизвестен, а не нулевой' in w for w in report.warnings))

    def test_no_observations_means_no_price_not_a_zero_price(self):
        empty = sd.Cohort(T0 + 99 * HOUR, T0 + 100 * HOUR, profile_id='p1', profile_revision=1)
        report = sd.compare_sources(self.conn, sources=['pub-C'], cohort=empty)
        self.assertIsNone(report.cost[0].seconds)
        self.assertIsNone(report.cost[0].bytes)
        self.assertEqual(report.cost[0].admitted, 0)
        self.assertTrue(report.cost[0].reason)

    def test_a_source_that_was_never_collected_is_not_a_dead_source(self):
        row = self.compare(sources=['pub-A', 'paid-brightdata']).row('paid-brightdata')
        self.assertEqual(row.status, sd.STATUS_NOT_COLLECTED)
        self.assertIsNone(row.reliability)
        self.assertEqual(row.offered, 0)

    def test_a_filter_that_removed_everything_does_not_declare_a_source_dead(self):
        # The same addresses, measured under a different profile revision, are
        # absent from that cohort.  The source is still offered, still credited,
        # and the report says it has no measurements there.
        other = sd.Cohort(*WINDOWS[0], profile_id='p1', profile_revision=7)
        report = sd.compare_sources(self.conn, sources=['pub-C'], cohort=other)
        row = report.row('pub-C')
        self.assertEqual(row.status, sd.STATUS_NO_DATA)
        self.assertEqual(row.offered, len(EXCLUSIVE))
        self.assertGreater(row.unique_offered, 0)

    def test_wilson_interval_is_absent_without_trials(self):
        self.assertEqual(sd.wilson_interval(0, 0), (None, None))
        low, high = sd.wilson_interval(3, 10)
        self.assertLess(low, 0.3)
        self.assertGreater(high, 0.6)


class CostTests(CompareBase):
    def test_cost_per_admitted_counts_time_bytes_and_attempts(self):
        row = self.compare().row('pub-C')
        cost = next(c for c in self.compare().cost if c.admitted == row.admitted)
        self.assertEqual(cost.admitted, 4)
        self.assertEqual(cost.seconds, row.seconds)
        self.assertEqual(cost.bytes, row.bytes)
        self.assertEqual(cost.attempts, row.attempts)
        self.assertEqual(cost.seconds / cost.admitted, row.seconds / 4)

    def test_an_unmeasured_source_spends_nothing_we_can_attribute(self):
        row = self.compare().row('pub-A')
        # 4 dead addresses cost nothing in bytes, and the pass rate only counts
        # the ones that were actually judged.
        self.assertEqual(row.bytes, (26 * 2 * 2048))

    def test_a_small_sample_is_flagged_rather_than_asserted(self):
        self.assertFalse(self.compare().row('pub-C').sample_sufficient)
        self.assertIn(sd.BIAS_SAMPLE, [note.code for note in self.compare().biases])


class SurvivalTests(CompareBase):
    def steps(self):
        return sd.survival_across_windows(self.conn, sources=['pub-A', 'pub-B', 'pub-C'],
                                          profile_id='p1', profile_revision=1, windows=WINDOWS)

    def test_addresses_that_die_stop_being_counted_as_alive(self):
        steps = self.steps()
        self.assertEqual(len(steps), 4)
        self.assertEqual((steps[0].entered, steps[0].alive), (30, 30))
        self.assertEqual(steps[2].dead, 6)
        self.assertEqual(steps[3].entered, 24)

    def test_a_window_without_measurements_censors_instead_of_killing(self):
        steps = sd.survival_across_windows(
            self.conn, sources=['pub-C'], profile_id='p1', profile_revision=1,
            windows=[WINDOWS[0], (T0 + 50 * HOUR, T0 + 51 * HOUR)])
        self.assertEqual(steps[1].dead, 0)
        self.assertEqual(steps[1].censored, 4)
        self.assertIsNone(steps[1].rate)

    def test_survival_needs_more_than_one_window(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.survival_across_windows(self.conn, sources=['pub-C'], windows=[WINDOWS[0]])


class SupplierTests(CompareBase):
    def test_two_suppliers_are_compared_on_the_same_cohort(self):
        result = sd.compare_suppliers(self.conn, 'pub-A', 'pub-C', cohort=self.cohort())
        self.assertEqual(result.cohort, self.cohort())
        self.assertIsNotNone(result.comparison)
        self.assertTrue(result.overlap or result.overlap is None)
        self.assertEqual(result.left.source_id, 'pub-A')
        self.assertEqual(result.right.source_id, 'pub-C')

    def test_two_identical_suppliers_are_saying_about_equal_conditions(self):
        result = sd.compare_suppliers(self.conn, 'pub-A', 'pub-B', cohort=self.cohort())
        self.assertTrue(result.overlap.identical)
        self.assertEqual(result.right.unique_offered, 0)
        self.assertTrue(any('один и тот же список' in w for w in result.warnings))

    def test_a_supplier_that_was_never_collected_is_not_equal_terms(self):
        result = sd.compare_suppliers(self.conn, 'pub-A', 'paid-brightdata', cohort=self.cohort())
        self.assertFalse(result.equal_terms)
        self.assertTrue(any('не собирался' in w for w in result.warnings))

    def test_a_supplier_cannot_be_compared_with_itself(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.compare_suppliers(self.conn, 'pub-A', 'pub-A', cohort=self.cohort())


class CohortComparabilityTests(CompareBase):
    def test_different_profiles_and_disjoint_windows_are_flagged(self):
        first = sd.Cohort(0.0, 100.0, profile_id='p1', profile_revision=1)
        second = sd.Cohort(200.0, 300.0, profile_id='p2', profile_revision=2, min_success=0.5)
        _breakdowns, warnings = sd.compare_cohorts(self.conn, sources=['pub-C'],
                                                    cohorts=(first, second))
        joined = ' '.join(warnings)
        self.assertIn('разной ревизией профиля', joined)
        self.assertIn('разных профилей', joined)
        self.assertIn('разный порог приёмки', joined)
        self.assertIn('окна не пересекаются', joined)

    def test_comparable_cohorts_raise_no_warning(self):
        first = sd.Cohort(0.0, 100.0, profile_id='p1', profile_revision=1)
        second = sd.Cohort(50.0, 150.0, profile_id='p1', profile_revision=1)
        _breakdowns, warnings = sd.compare_cohorts(self.conn, sources=['pub-C'],
                                                    cohorts=(first, second))
        self.assertEqual(warnings, ())

    def test_a_window_must_be_a_real_interval(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.Cohort(100.0, 100.0)


class BiasTests(CompareBase):
    def test_find_n_and_filters_are_named_from_the_job_that_ran(self):
        codes = [note.code for note in self.compare().biases]
        self.assertIn(sd.BIAS_FIND_N, codes)
        self.assertIn(sd.BIAS_FILTERS, codes)

    def test_a_publisher_declared_country_is_not_our_measurement(self):
        row = self.compare().row('pub-C')
        self.assertEqual(row.publisher_country_claims, len(EXCLUSIVE))
        self.assertEqual(row.measured_countries, 0)
        self.assertIn(sd.BIAS_PUBLISHER_GEO, [note.code for note in self.compare().biases])

    def test_different_countries_are_not_compared_as_if_they_were_the_same(self):
        note = next(n for n in self.compare().biases if n.code == sd.BIAS_GEOGRAPHY)
        self.assertIn('pub-A', note.sources)
        self.assertIn('pub-C', note.sources)

    def test_every_bias_code_is_one_the_module_declares(self):
        for note in self.compare().biases:
            self.assertIn(note.code, sd.BIAS_CODES)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.catalog = source_catalog.load_bundled()

    def test_commercial_and_trial_providers_are_visible_but_inert(self):
        notes = {note.source_id: note for note in sd.provider_inventory(self.catalog)}
        for source_id in ('new-001', 'new-002', 'new-003', 'new-004', 'new-005', 'new-006', 'new-007'):
            with self.subTest(source=source_id):
                self.assertIn(source_id, notes)
                self.assertFalse(notes[source_id].collectable)
                self.assertEqual(notes[source_id].status, sd.STATUS_NOT_COLLECTED)

    def test_no_price_is_invented_for_a_plan_we_never_bought(self):
        for note in sd.provider_inventory(self.catalog):
            with self.subTest(source=note.source_id):
                self.assertNotIn('price', note.as_dict())
                self.assertNotIn('cost', note.as_dict())
                self.assertIn('не оценивалась', note.note)

    def test_collectable_sources_are_not_listed_as_inert(self):
        listed = {note.source_id for note in sd.provider_inventory(self.catalog)}
        collectable = {s['id'] for s in self.catalog['sources'] if s.get('collection_allowed', True)}
        # A collectable record may still be absent from the DB and thus listed,
        # but one that was never collected must not claim to be collectable.
        for note in sd.provider_inventory(self.catalog):
            if note.source_id in collectable:
                self.assertNotEqual(note.status, 'measured')


class FetchStateTests(unittest.TestCase):
    """A 200 with a valid ETag and an empty body is not a successful fetch."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.conn, _report = db.open_db(str(Path(self._tmp.name) / 'feed.sqlite3'))
        for statement in sd.REQUESTED_DDL:
            self.conn.execute(statement)
        self.desk = sd.SourceDesk(self.conn)
        self.source = sd.user_source(binding_id='b1', url='https://list.invalid/free-proxy-list/')
        self.desk.bind(self.source, collection_id='public')

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _state(self):
        self.conn.commit()
        return sd._fetch_states(self.conn, [self.source.id])[self.source.id]

    def test_two_hundred_with_an_etag_and_no_body_is_its_own_outcome(self):
        self.conn.execute('UPDATE source_feed SET last_outcome=?, etag=?, last_success_at=? '
                          'WHERE source_id=?', ('empty', 'W/"abc"', T0, self.source.id))
        state = self._state()
        self.assertTrue(state.delivered_nothing)
        self.assertEqual(state.last_outcome, 'empty')
        self.assertEqual(state.etag, 'W/"abc"')
        self.assertIsNotNone(state.last_success_at)

    def test_a_feed_that_delivered_addresses_is_not_delivered_nothing(self):
        self.conn.execute('UPDATE source_feed SET last_outcome=? WHERE source_id=?',
                          ('ok', self.source.id))
        self.assertFalse(self._state().delivered_nothing)


class ValidationTests(unittest.TestCase):
    def test_a_comparison_needs_a_cohort_and_a_source(self):
        conn = db.connect(':memory:')
        with self.assertRaises(sd.SourceDeskError):
            sd.compare_sources(conn, sources=['a'], cohort=object())
        with self.assertRaises(sd.SourceDeskError):
            sd.compare_sources(conn, sources=[], cohort=sd.Cohort(0.0, 1.0))
        with self.assertRaises(sd.SourceDeskError):
            sd.compare_sources(conn, sources=['a'], cohort=sd.Cohort(0.0, 1.0),
                               family_jaccard=2.0)
        conn.close()

    def test_the_api_is_exported(self):
        for name in ('compare_sources', 'compare_suppliers', 'compare_cohorts', 'Cohort',
                     'survival_across_windows', 'provider_inventory', 'wilson_interval',
                     'classify_measurement', 'SourceComparison', 'SupplierComparison'):
            self.assertIn(name, sd.__all__, name)


if __name__ == '__main__':
    unittest.main()

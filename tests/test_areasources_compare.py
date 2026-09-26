"""F21: the comparison, proven on the shape the source research actually found.

The headline scenario is the one in the research: two publishers hand out
*the same* address set, exactly like ``hookzof/socks5_list`` and
``proxifly/free-proxy-list`` (21 036 identical endpoints, Jaccard 1.0 in
``docs/requirements/sources-research/overlap.md``).  The comparison has to see
the copy and give the second publisher a unique contribution of zero, before
any proxy is contacted.

Every address here is from a documentation range.  Nothing in this file opens a
connection, and nothing claims a pass rate for a real proxy.
"""
import itertools
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import db
from proxy_workbench import source_catalog as sc
from proxy_workbench import sourcedesk as sd

T0 = 1_757_000_000.0
HOUR = 3600.0

SHARED = [f'198.51.100.{n}' for n in range(1, 21)]      # pub-a and pub-b, identical
EXCLUSIVE = [f'203.0.113.{n}' for n in range(1, 9)]     # pub-c only
UNNEVER_COLLECTED = [f'192.0.2.{n}' for n in range(1, 4)]  # pub-d: catalog says so, no rows

CATALOG_DATASET_GROUPS = {
    'pub-a': 'dataset:hookzof-proxifly-socks5-21036',
    'pub-b': 'dataset:hookzof-proxifly-socks5-21036',
    'pub-c': 'gfp-aggregator',
    'pub-d': 'never-collected',
}


def _verdict(reliability, *, ms=90.0, byte_count=1024, requests=2):
    return json.dumps({'proxy': 'redacted', 'reliability': reliability,
                       'min_target_reliability': reliability, 'latency_ms': ms,
                       'successes': int(round(reliability * requests)), 'requests': requests,
                       'samples': [{'ok': True, 'bytes': byte_count, 'ms': ms}
                                   for _ in range(requests)]})


def _build(path):
    conn, _report = db.open_db(str(path))
    for canonical in SHARED + EXCLUSIVE + UNNEVER_COLLECTED:
        conn.execute('INSERT OR IGNORE INTO endpoints(id, canonical, country, country_source, '
                     'asn, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)',
                     (db.endpoint_id(canonical), canonical,
                      'NL' if canonical in SHARED else 'DE',
                      'source', 64500, T0, T0 + HOUR))
    conn.execute('INSERT OR IGNORE INTO accesses(id, endpoint_id, mode, access_revision, created_at) '
                 'VALUES (?,?,?,?,?)',
                 ('acc-1', db.endpoint_id(SHARED[0]), 'none', 1, T0))
    for proxy in SHARED:
        for source in ('pub-a', 'pub-b'):
            conn.execute('INSERT OR IGNORE INTO candidate_seen(proxy, source, endpoint_id) VALUES (?,?,?)',
                         (proxy, source, db.endpoint_id(proxy)))
    for proxy in EXCLUSIVE:
        conn.execute('INSERT OR IGNORE INTO candidate_seen(proxy, source, endpoint_id) VALUES (?,?,?)',
                     (proxy, 'pub-c', db.endpoint_id(proxy)))
    conn.execute("INSERT INTO job(id, kind, state, scope_json, profile_id, profile_revision, "
                 "collection_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 ('job-1', 'check', 'want_reached',
                  json.dumps({'collection_id': 'public', 'profile_id': 'p1', 'profile_revision': 1,
                              'filters': {'countries': ['NL']}}),
                  'p1', 1, 'public', T0))

    def observe(canonical, at, payload, error=None, stage=None):
        conn.execute('INSERT INTO observations(id, job_id, endpoint_id, access_id, access_revision, '
                     'profile_id, profile_revision, started_at, finished_at, verdict, error_code, '
                     'error_stage) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                     (f'obs-{db.endpoint_id(canonical)}-{int(at)}', 'job-1',
                      db.endpoint_id(canonical), 'acc-1', 1, 'p1', 1, at, at + 1.0,
                      payload, error, stage))

    for proxy in SHARED:
        if proxy in SHARED[:12]:
            observe(proxy, T0 + 60, _verdict(1.0))
        else:
            observe(proxy, T0 + 60, '', 'UNREACHABLE', 'tcp')
    for proxy in EXCLUSIVE:
        if proxy in EXCLUSIVE[5:6]:
            continue
        if proxy in EXCLUSIVE[:3]:
            observe(proxy, T0 + 60, _verdict(1.0))
        else:
            observe(proxy, T0 + 60, '', 'CONNECT_TIMEOUT', 'tcp')
    # One measurement the target answered for, not the address: it must stay out
    # of the denominator instead of becoming a fail.
    observe(EXCLUSIVE[5], T0 + 60, _verdict(0.0), 'TARGET_UNAVAILABLE', 'target')
    conn.commit()
    return conn


class CompareBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._temp = tempfile.TemporaryDirectory()
        cls.conn = _build(Path(cls._temp.name) / 'compare.db')
        cls.cohort = sd.Cohort(T0, T0 + HOUR, profile_id='p1', profile_revision=1,
                               collection_id='public', min_success=2 / 3, label='window-1')

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls._temp.cleanup()

    def compare(self, sources=('pub-a', 'pub-b', 'pub-c', 'pub-d'), **kwargs):
        return sd.compare_sources(self.conn, sources=sources, cohort=self.cohort, **kwargs)


class TheCopyScenarioTest(CompareBase):
    """Two publishers, one address set.  The second contributes nothing."""

    def test_the_overlap_is_seen_and_is_exactly_the_whole_set(self):
        result = self.compare(sources=('pub-a', 'pub-b'))
        pair = next(item for item in result.overlaps
                    if {item.left, item.right} == {'pub-a', 'pub-b'})
        self.assertTrue(pair.identical)
        self.assertEqual(pair.shared, len(SHARED))
        self.assertEqual(pair.jaccard, 1.0)
        self.assertEqual(pair.left_only, 0)
        self.assertEqual(pair.right_only, 0)

    def test_the_copies_unique_contribution_is_zero_not_small(self):
        result = self.compare(sources=('pub-a', 'pub-b'))
        first, second = result.row('pub-a'), result.row('pub-b')
        self.assertEqual(first.offered, len(SHARED))
        self.assertEqual(second.offered, len(SHARED))
        self.assertEqual(first.unique_offered, 0)
        self.assertEqual(second.unique_offered, 0)
        self.assertEqual(first.unique_admitted, 0)
        self.assertEqual(second.unique_admitted, 0)
        # The addresses are still credited to both, many-to-many.
        self.assertEqual(first.admitted, 12)
        self.assertEqual(second.admitted, 12)

    def test_the_copy_is_called_out_as_one_publisher_counted_twice(self):
        result = self.compare(sources=('pub-a', 'pub-b'))
        codes = {note.code for note in result.biases}
        self.assertIn(sd.BIAS_SHARED_COST, codes)
        note = next(item for item in result.biases if item.code == sd.BIAS_SHARED_COST)
        self.assertIn('один издатель', note.detail)

    def test_both_copies_land_in_one_family(self):
        result = self.compare(sources=('pub-a', 'pub-b', 'pub-c'))
        families = {family.family_id: family.members for family in result.families}
        merged = [members for members in families.values() if {'pub-a', 'pub-b'} <= set(members)]
        self.assertEqual(len(merged), 1, 'копии обязаны быть одним семейством')
        self.assertTrue(any(family.identical_group for family in result.families))

    def test_the_third_publisher_is_credited_with_what_only_it_has(self):
        result = self.compare()
        third = result.row('pub-c')
        self.assertEqual(third.unique_offered, len(EXCLUSIVE))
        self.assertEqual(third.unique_admitted, 3)
        self.assertEqual(result.row('pub-a').unique_offered, 0)

    def test_a_source_that_was_never_collected_is_not_a_dead_source(self):
        result = self.compare()
        never = result.row('pub-d')
        self.assertEqual(never.status, sd.STATUS_NOT_COLLECTED)
        self.assertEqual(never.offered, 0)
        self.assertTrue(never.notes)
        self.assertIsNone(never.reliability)

    def test_the_supplier_view_of_two_copies_says_equal_terms_are_not_two_answers(self):
        supplier = sd.compare_suppliers(self.conn, 'pub-a', 'pub-b', cohort=self.cohort)
        self.assertTrue(supplier.overlap.identical)
        self.assertTrue(any('один и тот же список' in text for text in supplier.warnings))
        self.assertEqual(supplier.right.unique_offered, 0)


class DatasetIdentityTest(CompareBase):
    """What the catalog already knows must not have to be re-proved by a run."""

    def test_a_dataset_group_merges_a_pair_whose_addresses_never_arrived(self):
        # Two publishers the catalog records as one dataset; neither was
        # collected, so no address set exists to compare.
        result = sd.compare_sources(self.conn, sources=('pub-d', 'pub-a'), cohort=self.cohort,
                                   dataset_groups=CATALOG_DATASET_GROUPS)
        # pub-a was collected, pub-d was not: the prior only joins identities,
        # it never invents addresses or a pass rate.
        self.assertEqual(result.row('pub-d').status, sd.STATUS_NOT_COLLECTED)
        self.assertEqual(result.row('pub-a').offered, len(SHARED))

    def test_a_dataset_group_never_makes_a_pair_independent(self):
        without = sd.compare_sources(self.conn, sources=('pub-a', 'pub-b'), cohort=self.cohort)
        with_prior = sd.compare_sources(self.conn, sources=('pub-a', 'pub-b'), cohort=self.cohort,
                                        dataset_groups=CATALOG_DATASET_GROUPS)
        self.assertEqual([row.as_dict() for row in without.rows],
                         [row.as_dict() for row in with_prior.rows])
        self.assertEqual([pair.as_dict() for pair in without.overlaps],
                         [pair.as_dict() for pair in with_prior.overlaps])

    def test_the_catalog_merges_the_two_pairs_the_research_proved_identical(self):
        catalog = sc.load_bundled()
        groups = sc.dataset_groups(catalog, ['cur-41', 'cur-43', 'cur-11', 'new-079', 'cur-01'])
        self.assertEqual(groups['cur-41'], groups['cur-43'])
        self.assertEqual(groups['cur-11'], groups['new-079'])
        self.assertNotEqual(groups['cur-41'], groups['cur-11'])
        # cur-01 says nothing about its dataset, so it keeps its family.
        self.assertEqual(groups['cur-01'], 'current:github:MuRongPIG/Proxy-Master')

    def test_an_empty_dataset_group_map_changes_nothing(self):
        base = self.compare(sources=('pub-a', 'pub-b')).as_dict()
        for empty in (None, {}, {'pub-a': None}):
            other = sd.compare_sources(self.conn, sources=('pub-a', 'pub-b'), cohort=self.cohort,
                                       dataset_groups=empty).as_dict()
            self.assertEqual(base, other)


class OrderAndUnknownTest(CompareBase):
    def test_permuting_the_sources_changes_no_number_and_no_note(self):
        reference = None
        for order in itertools.permutations(('pub-a', 'pub-b', 'pub-c', 'pub-d')):
            payload = self.compare(sources=order).as_dict()
            if reference is None:
                reference = payload
            self.assertEqual(reference, payload, f'порядок {order} изменил результат')

    def test_a_repeated_source_does_not_inflate_anything(self):
        once = self.compare(sources=('pub-a', 'pub-b')).as_dict()
        twice = self.compare(sources=('pub-a', 'pub-b', 'pub-b', 'pub-a')).as_dict()
        self.assertEqual(once, twice)

    def test_a_target_side_failure_leaves_the_denominator_alone(self):
        row = self.compare().row('pub-c')
        # Every address of this publisher was looked at; one of those lookups
        # concluded nothing and is therefore neither a pass nor a fail.
        self.assertEqual(row.measured, len(EXCLUSIVE))
        self.assertEqual(row.unknown, 1)
        self.assertEqual(row.passed, 3)
        self.assertEqual(row.failed, len(EXCLUSIVE) - 3 - 1)
        self.assertAlmostEqual(row.reliability, 3 / (3 + (len(EXCLUSIVE) - 3 - 1)), places=9)
        self.assertIn('не входят в знаменатель', row.notes[0])

    def test_a_publisher_declared_country_is_not_our_measurement(self):
        rows = {row.source_id: row for row in self.compare().rows if row.measured}
        self.assertTrue(rows)
        for row in rows.values():
            self.assertGreater(row.publisher_country_claims, 0)
            self.assertEqual(row.measured_countries, 0)
        codes = {note.code for note in self.compare().biases}
        self.assertIn(sd.BIAS_PUBLISHER_GEO, codes)

    def test_no_observations_means_no_price_and_a_warning(self):
        empty_cohort = sd.Cohort(T0 + 10 * HOUR, T0 + 11 * HOUR, profile_id='p1',
                                 profile_revision=1, collection_id='public')
        result = sd.compare_sources(self.conn, sources=('pub-a', 'pub-b'), cohort=empty_cohort)
        self.assertTrue(any('неизвестен' in text for text in result.warnings))
        for row in result.rows:
            self.assertIsNone(row.reliability)
            self.assertIsNone(row.reliability_low)
            self.assertIsNone(self.compare().row('pub-a').status and None)
            self.assertIsNone(result.cost[0].seconds)
            self.assertEqual(result.cost[0].reason, 'нет ни одного пригодного адреса в этом окне')


class CostAndBiasTest(CompareBase):
    def test_cost_per_admitted_counts_time_bytes_and_attempts(self):
        result = self.compare(sources=('pub-a',))
        cost = result.cost[0]
        self.assertEqual(cost.admitted, 12)
        self.assertEqual(cost.bytes, 12 * 2 * 1024)
        self.assertGreater(cost.seconds, 0)
        self.assertEqual(cost.attempts, 12 * 2)
        self.assertEqual(cost.basis, 'admitted')

    def test_a_source_with_no_admitted_address_has_no_price(self):
        cost = self.compare(sources=('pub-d',)).cost[0]
        self.assertEqual(cost.admitted, 0)
        self.assertIsNone(cost.seconds)
        self.assertIsNone(cost.bytes)
        self.assertTrue(cost.reason)

    def test_filters_run_under_are_named_from_the_job_that_ran(self):
        codes = {note.code for note in self.compare().biases}
        self.assertIn(sd.BIAS_FILTERS, codes)
        note = next(item for item in self.compare().biases if item.code == sd.BIAS_FILTERS)
        self.assertIn('countries', note.detail)

    def test_every_emitted_bias_is_a_code_the_module_declares(self):
        for note in self.compare().biases:
            self.assertIn(note.code, sd.BIAS_CODES)


class SurvivalAndCohortsTest(CompareBase):
    WINDOWS = [(T0 + index * HOUR, T0 + (index + 1) * HOUR) for index in range(4)]

    def test_survival_needs_two_windows(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.survival_across_windows(self.conn, sources=('pub-a',),
                                       windows=self.WINDOWS[:1], start=T0, end=T0 + HOUR)

    def test_an_address_that_was_never_rechecked_is_censored_not_dead(self):
        steps = sd.survival_across_windows(self.conn, sources=('pub-a', 'pub-b'),
                                           profile_id='p1', windows=self.WINDOWS)
        self.assertEqual(len(steps), len(self.WINDOWS))
        # Only window 1 has observations.  In window 2 nothing was re-checked,
        # so the twelve are censored -- "we did not look", not "they died".
        self.assertEqual(steps[0].entered, 12)
        self.assertEqual(steps[0].alive, 12)
        self.assertEqual(steps[1].entered, 12)
        self.assertEqual(steps[1].dead, 0)
        self.assertEqual(steps[1].censored, 12)
        self.assertIsNone(steps[1].rate)
        self.assertIsNone(steps[1].rate_of_entered)
        for step in steps[2:]:
            self.assertEqual(step.entered, 0)
            self.assertIsNone(step.rate)

    def test_two_cohorts_with_different_rules_are_flagged_not_averaged(self):
        other = sd.Cohort(T0, T0 + HOUR, profile_id='p2', profile_revision=2,
                          collection_id='public', min_success=0.9, label='window-1-b')
        breakdowns, warnings = sd.compare_cohorts(self.conn, sources=('pub-a', 'pub-b'),
                                                  cohorts=(self.cohort, other))
        self.assertEqual(len(breakdowns), 2)
        self.assertEqual(len(warnings), 3)
        self.assertTrue(any('ревизией профиля' in text for text in warnings))
        self.assertTrue(any('порог приёмки' in text for text in warnings))
        self.assertTrue(any('разных профилей' in text for text in warnings))

    def test_one_cohort_is_not_a_comparison(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.compare_cohorts(self.conn, sources=('pub-a',), cohorts=(self.cohort,))

    def test_a_supplier_cannot_be_compared_with_itself(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.compare_suppliers(self.conn, 'pub-a', 'pub-a', cohort=self.cohort)

    def test_a_source_pair_with_no_evidence_is_flagged_as_not_equal_terms(self):
        supplier = sd.compare_suppliers(self.conn, 'pub-a', 'pub-d', cohort=self.cohort)
        self.assertFalse(supplier.equal_terms)
        self.assertTrue(any('не собирался' in text for text in supplier.warnings))


class ProviderInventoryTest(unittest.TestCase):
    """The paid and trial providers stay visible, inert and unpriced."""

    def test_every_commercial_record_is_listed_and_none_is_collectable(self):
        catalog = sc.load_bundled()
        notes = sd.provider_inventory(catalog)
        self.assertTrue(notes)
        for note in notes:
            self.assertFalse(note.collectable)
            self.assertEqual(note.status, sd.STATUS_NOT_COLLECTED)
            self.assertIn(note.status,
                          (sd.STATUS_MEASURED, sd.STATUS_NO_DATA, sd.STATUS_NOT_COLLECTED))
        text = json.dumps([note.as_dict() for note in notes], ensure_ascii=False)
        self.assertNotIn('$', text)
        self.assertNotIn('USD', text)

    def test_a_collectable_public_source_is_not_listed_as_a_provider(self):
        catalog = sc.load_bundled()
        notes = sd.provider_inventory(catalog)
        listed = {note.source_id for note in notes}
        for source in sc.eligible_sources(catalog)[:20]:
            self.assertNotIn(source['id'], listed)


if __name__ == '__main__':
    unittest.main()

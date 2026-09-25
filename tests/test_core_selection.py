import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import core

NOW = 1_700_000_000.0
SCOPE = core.Scope(collection_id='public', profile_id='p1', profile_revision=3)
ACCESS = core.Access(access_id='acc-1', access_revision=2)
POLICY = core.Policy(max_age_seconds=600.0, min_success=0.5)


def row(index, checked_at=NOW - 30, max_age=600.0, **extra):
    data = {
        'proxy': f'http://11.0.0.{index}:80',
        'endpoint_id': f'ep-{index}',
        'collection_id': 'public',
        'profile_id': 'p1',
        'profile_revision': 3,
        'network_id': 'default',
        'access_id': 'acc-1',
        'access_revision': 2,
        'min_target_reliability': 1.0,
        'latency_ms': 100.0,
        'protocol': 'http',
        'country': 'NL',
    }
    if checked_at is not None:
        data['checked_at'] = checked_at
        data['valid_until'] = checked_at + max_age
    data.update(extra)
    return data


def reasons(selection):
    return {item.endpoint_id: (item.reason_code or 'OK') for item in selection.admissions}


class MixedAgeTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row(1, checked_at=NOW - 10),                    # fresh
                     row(2, checked_at=NOW - 5000, max_age=600.0),    # expired
                     row(3, checked_at=NOW + 120),                   # future
                     row(4, checked_at=NOW - 10, country='DE')]      # fresh, filtered out

    def test_rows_are_filtered_independently(self):
        policy = core.Policy(max_age_seconds=600.0, min_success=0.5, countries=frozenset({'NL'}))
        selection = core.select(self.rows, SCOPE, ACCESS, policy, NOW)
        self.assertEqual([item['proxy'] for item in selection.admitted], [self.rows[0]['proxy']])
        self.assertEqual(reasons(selection), {
            'ep-1': 'OK',
            'ep-2': 'E_TIME_TTL_EXPIRED',
            'ep-3': 'E_TIME_FUTURE',
            'ep-4': 'E_SCOPE_COUNTRY',
        })
        self.assertEqual(selection.counts, {'E_TIME_TTL_EXPIRED': 1, 'E_TIME_FUTURE': 1,
                                             'E_SCOPE_COUNTRY': 1})
        self.assertEqual((selection.state, selection.state_detail), ('partial', 'rejected'))

    def test_expiring_member_does_not_empty_the_set(self):
        fresh_two = [row(1, checked_at=NOW - 10), row(2, checked_at=NOW - 20)]
        selection = core.select(fresh_two + [row(3, checked_at=NOW - 5000)], SCOPE, ACCESS,
                                POLICY, NOW)
        self.assertEqual(len(selection.admitted), 2)
        self.assertEqual(selection.expires_at, max(item['valid_until'] for item in fresh_two))
        self.assertGreater(selection.expires_at, self.rows[1]['valid_until'])
        self.assertEqual((selection.state, selection.state_detail), ('partial', 'rejected'))

    def test_set_keeps_its_newest_member_not_its_oldest(self):
        rows = [row(1, checked_at=NOW - 590), row(2, checked_at=NOW - 10)]
        first = core.select(rows, SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual(len(first.admitted), 2)
        # Only the oldest row expires; the set must not collapse to zero.
        later = core.select(rows, SCOPE, ACCESS, POLICY, NOW + 300)
        self.assertEqual(len(later.admitted), 1)
        self.assertEqual(later.state, 'partial')
        self.assertIsNotNone(later.expires_at)

    def test_all_expired_is_stale_not_empty(self):
        rows = [row(1, checked_at=NOW - 5000), row(2, checked_at=NOW - 9000)]
        selection = core.select(rows, SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual((selection.state, selection.state_detail), ('stale', 'all_expired'))
        self.assertIsNone(selection.expires_at)
        self.assertEqual(selection.admitted, ())

    def test_nothing_in_scope_and_nothing_measured_are_different(self):
        empty = core.select([], SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual((empty.state, empty.state_detail), ('empty', 'nothing_in_scope'))
        bare = row(1, checked_at=None, min_target_reliability=None)
        unmeasured = core.select([bare], SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual(unmeasured.counts, {'E_STATE_NO_OBSERVATION': 1})
        self.assertEqual((unmeasured.state, unmeasured.state_detail), ('empty', 'empty_no_match'))

    def test_all_failed_has_its_own_detail(self):
        rows = [row(1, error='UNREACHABLE', min_target_reliability=0.0),
                row(2, min_target_reliability=0.0)]
        selection = core.select(rows, SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual((selection.state, selection.state_detail), ('empty', 'all_failed'))

    def test_rows_the_clock_cannot_trust_are_not_called_expired(self):
        rows = [row(1, checked_at=NOW + 30), row(2, checked_at=None)]
        selection = core.select(rows, SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual((selection.state, selection.state_detail), ('stale', 'all_untrusted'))
        self.assertEqual(selection.counts, {'E_TIME_FUTURE': 1, 'E_TIME_UNKNOWN': 1})
        self.assertIn(core.DETAIL_ALL_UNTRUSTED, core.STATE_DETAILS)

    def test_complete_set_when_nothing_is_rejected(self):
        selection = core.select([row(1), row(2)], SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual((selection.state, selection.state_detail), ('complete', 'ok'))
        self.assertEqual(selection.counts, {})


class TimeMovementTests(unittest.TestCase):
    def test_time_moving_forward_drops_rows_one_by_one(self):
        rows = [row(1, checked_at=NOW - 100), row(2, checked_at=NOW - 300)]
        seen = []
        for offset in (0, 350, 600):
            selection = core.select(rows, SCOPE, ACCESS, POLICY, NOW + offset)
            seen.append((offset, len(selection.admitted), selection.state))
        self.assertEqual(seen, [(0, 2, 'complete'), (350, 1, 'partial'), (600, 0, 'stale')])

    def test_time_moving_backwards_turns_rows_into_future_not_fresh(self):
        rows = [row(1, checked_at=NOW - 10)]
        self.assertEqual(len(core.select(rows, SCOPE, ACCESS, POLICY, NOW).admitted), 1)
        backwards = core.select(rows, SCOPE, ACCESS, POLICY, NOW - 600)
        self.assertEqual(backwards.counts, {'E_TIME_FUTURE': 1})
        self.assertEqual(backwards.admissions[0].time_state, core.TIME_FUTURE)

    def test_backwards_movement_never_resurrects_an_expired_row(self):
        rows = [row(1, checked_at=NOW - 5000)]
        self.assertEqual(core.select(rows, SCOPE, ACCESS, POLICY, NOW).counts,
                         {'E_TIME_TTL_EXPIRED': 1})
        self.assertEqual(core.select(rows, SCOPE, ACCESS, POLICY, NOW - 600).counts,
                         {'E_TIME_TTL_EXPIRED': 1})


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row(1, checked_at=NOW - 10), row(2, checked_at=NOW - 5000)]
        self.first = core.Generation('.generation-aaa', 2, published_at=NOW)
        self.second = core.Generation('.generation-bbb', 2, published_at=NOW + 3600)

    def test_republish_changes_nothing_but_the_pointer(self):
        before = core.select(self.rows, SCOPE, ACCESS, POLICY, NOW,
                             published_at=NOW, generation=self.first.name)
        after = core.select(self.rows, SCOPE, ACCESS, POLICY, NOW,
                            published_at=NOW + 3600, generation=self.second.name)
        self.assertEqual(before.content_digest, after.content_digest)
        self.assertEqual(before.parity_pairs, after.parity_pairs)
        self.assertEqual(before.generation, self.first.name)
        self.assertEqual(after.generation, self.second.name)
        self.assertEqual(reasons(before), reasons(after))
        self.assertEqual([item.valid_until for item in before.admissions],
                         [item.valid_until for item in after.admissions])
        self.assertEqual(after.published_at, NOW + 3600)

    def test_consumer_stays_pinned_to_its_generation(self):
        available = [self.first, self.second]
        pinned = core.pin_generation(available, self.first.name, supported_schema_versions={2})
        self.assertEqual(pinned.name, self.first.name)
        with self.assertRaises(core.AdmissionError) as caught:
            core.pin_generation(available, '.generation-missing', supported_schema_versions={2})
        self.assertEqual(caught.exception.code, 'E_STATE_NO_SNAPSHOT')
        with self.assertRaises(core.AdmissionError) as caught:
            core.pin_generation(available, self.first.name, supported_schema_versions={1})
        self.assertEqual(caught.exception.code, 'E_STATE_SNAPSHOT_SCHEMA')

    def test_generation_name_is_never_a_freshness_input(self):
        renamed = core.Generation('.generation-zzz', 2, published_at=NOW - 99_999)
        self.assertEqual(
            core.select(self.rows, SCOPE, ACCESS, POLICY, NOW, generation=renamed.name).content_digest,
            core.select(self.rows, SCOPE, ACCESS, POLICY, NOW, generation=self.first.name).content_digest)

    def test_generation_name_is_validated(self):
        for name in ('', '.', '..', 'a/b', 'a\\b'):
            with self.subTest(name=name):
                with self.assertRaises(core.AdmissionError):
                    core.Generation(name, 2)


class ParityTests(unittest.TestCase):
    def test_same_composition_in_any_order_is_one_digest(self):
        rows = [row(1), row(2), row(3, checked_at=NOW - 5000)]
        first = core.select(rows, SCOPE, ACCESS, POLICY, NOW)
        second = core.select(list(reversed(rows)), SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual(first.content_digest, second.content_digest)
        self.assertEqual(first.parity_pairs,
                         (('ep-1', 'OK'), ('ep-2', 'OK'), ('ep-3', 'E_TIME_TTL_EXPIRED')))

    def test_one_changed_row_changes_the_digest(self):
        rows = [row(1), row(2, checked_at=NOW - 5000)]
        remeasured = dict(rows[1], checked_at=NOW - 10, valid_until=NOW + 590)
        before = core.select(rows, SCOPE, ACCESS, POLICY, NOW)
        after = core.select([rows[0], remeasured], SCOPE, ACCESS, POLICY, NOW)
        self.assertNotEqual(before.content_digest, after.content_digest)
        self.assertEqual(after.parity_pairs, (('ep-1', 'OK'), ('ep-2', 'OK')))

    def test_rejected_row_reason_is_visible_per_row(self):
        rows = [row(1), row(2, country='DE')]
        policy = core.Policy(max_age_seconds=600.0, countries=frozenset({'NL'}))
        selection = core.select(rows, SCOPE, ACCESS, policy, NOW)
        self.assertEqual(selection.parity_pairs,
                         (('ep-1', 'OK'), ('ep-2', 'E_SCOPE_COUNTRY')))


class ChangePropagationTests(unittest.TestCase):
    """Network change, access revision and revoke reach every surface the same way."""

    def setUp(self):
        self.rows = [row(1), row(2)]

    def test_network_change_filters_the_whole_set_the_same_way(self):
        on_default = core.Scope('public', 'p1', 3, network_id='default')
        on_eth = core.Scope('public', 'p1', 3, network_id='eth0')
        self.assertEqual(len(core.select(self.rows, on_default, ACCESS, POLICY, NOW).admitted), 2)
        moved = core.select(self.rows, on_eth, ACCESS, POLICY, NOW)
        self.assertEqual(moved.counts, {'E_SCOPE_NETWORK': 2})
        self.assertEqual(moved.state, 'empty')

    def test_access_revision_change_filters_the_whole_set_the_same_way(self):
        rotated = core.Access('acc-1', 3)
        revoked = core.select(self.rows, SCOPE, rotated, POLICY, NOW)
        self.assertEqual(revoked.counts, {'E_CONFLICT_ACCESS_REVISION': 2})
        self.assertEqual(revoked.state, 'empty')

    def test_revoke_after_a_selection_removes_the_endpoint_on_the_next_read(self):
        first = core.select(self.rows, SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual(len(first.admitted), 2)
        revoked = core.Policy(max_age_seconds=600.0, denied=frozenset({self.rows[0]['proxy']}))
        second = core.select(self.rows, SCOPE, ACCESS, revoked, NOW)
        self.assertEqual(len(second.admitted), 1)
        self.assertEqual(second.counts, {'E_SCOPE_DENYLIST': 1})
        self.assertNotEqual(first.content_digest, second.content_digest)


class StatusTests(unittest.TestCase):
    def test_status_reports_policy_expiry_and_reasons(self):
        rows = [row(1, checked_at=NOW - 10), row(2, checked_at=NOW - 5000)]
        selection = core.select(rows, SCOPE, ACCESS, POLICY, NOW, published_at=NOW,
                                generation='.generation-aaa')
        status = selection.as_status()
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(status['max_age_seconds'], 600.0)
        self.assertEqual(status['expires_at'], selection.admissions[0].valid_until)
        self.assertEqual(status['admission_counts'], {'E_TIME_TTL_EXPIRED': 1})
        self.assertEqual((status['admitted'], status['rejected'], status['checked']), (1, 1, 1))
        self.assertEqual(status['generation'], '.generation-aaa')
        self.assertTrue(status['static_ttl_enforced'])
        self.assertIsNone(status['static_ttl_notice'])

    def test_static_publication_says_it_cannot_revoke_itself(self):
        selection = core.select([row(1)], SCOPE, ACCESS, POLICY, NOW, static=True)
        status = selection.as_status()
        self.assertFalse(status['static_ttl_enforced'])
        self.assertEqual(status['static_ttl_notice'], core.STATIC_TTL_NOTICE)

    def test_admission_as_dict_is_the_public_row_shape(self):
        result = core.admit(row(1), SCOPE, ACCESS, POLICY, NOW, published_at=NOW)
        data = result.as_dict()
        self.assertEqual(set(data), {'admitted', 'admission_reason', 'time_state',
                                     'observation_state', 'age_seconds', 'checked_at',
                                     'valid_until', 'published_at', 'max_age_seconds',
                                     'ttl_backfilled', 'detail'})
        self.assertIsNone(data['admission_reason'])
        self.assertEqual(data['published_at'], NOW)


if __name__ == '__main__':
    unittest.main()

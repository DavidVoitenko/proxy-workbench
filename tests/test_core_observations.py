import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import core

NOW = 1_700_000_000.0
SCOPE = core.Scope(collection_id='public', profile_id='p1', profile_revision=3)
ACCESS = core.Access(access_id='acc-1', access_revision=2)
POLICY = core.Policy(max_age_seconds=600.0, min_success=0.5)

VERDICT = {'min_target_reliability': 1.0, 'reliability': 1.0, 'latency_ms': 120.0,
           'protocol': 'http', 'country': 'NL', 'reputation': {'status': 'clean'}}


def good(checked_at, **extra):
    extra.setdefault('observation_id', 'obs-1')
    extra.setdefault('verdict', dict(VERDICT))
    return core.Measurement(endpoint_id='ep-1', checked_at=checked_at,
                            job_id='job-1', network_id='default', **extra)


def failed(checked_at, **extra):
    return core.Measurement(
        endpoint_id='ep-1', checked_at=checked_at, job_id='job-2',
        verdict={'min_target_reliability': 0.0, 'reliability': 0.0, 'latency_ms': None,
                 'protocol': 'http', 'country': 'NL'},
        error={'code': 'UNREACHABLE', 'stage': 'tcp'}, network_id='default', **extra)


def measured(measurement, previous=None, policy=POLICY):
    """A measurement folded into a row under the identity the scope asks for."""
    return core.apply_measurement(previous, measurement, policy, access=ACCESS,
                                  collection_id='public', profile=('p1', 3))


class DeadlineTests(unittest.TestCase):
    def test_deadline_is_written_once_at_measurement_time(self):
        row = measured(good(NOW - 100))
        self.assertEqual(row['checked_at'], NOW - 100)
        self.assertEqual(row['valid_until'], NOW + 500)
        self.assertEqual(row['last_observation_state'], 'measured')

    def test_another_policy_does_not_move_a_written_deadline(self):
        row = measured(good(NOW - 100))
        other = core.Policy(max_age_seconds=86_400.0, min_success=0.5)
        decision = core.admit(row, SCOPE, ACCESS, other, NOW)
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.valid_until, NOW + 500)
        self.assertEqual(decision.max_age_seconds, 86_400.0)

    def test_a_new_measurement_uses_the_policy_in_force_then(self):
        row = measured(good(NOW - 100))
        shorter = core.Policy(max_age_seconds=120.0, min_success=0.5)
        row = measured(good(NOW - 10, observation_id='obs-2'), row, shorter)
        self.assertEqual(row['valid_until'], NOW + 110)
        self.assertEqual(row['observation_id'], 'obs-2')

    def test_measurement_stamps_the_identity_it_was_made_under(self):
        row = measured(good(NOW - 10))
        for field, value in (('access_id', 'acc-1'), ('access_revision', 2),
                             ('collection_id', 'public'), ('profile_id', 'p1'),
                             ('profile_revision', 3), ('network_id', 'default')):
            self.assertEqual(row[field], value, field)
        self.assertTrue(core.admit(row, SCOPE, ACCESS, POLICY, NOW).admitted)

    def test_network_is_not_invented_when_the_writer_does_not_know_it(self):
        blind = core.Measurement('ep-1', checked_at=NOW - 10, verdict=dict(VERDICT))
        row = measured(blind)
        self.assertNotIn('network_id', row)
        self.assertEqual(core.admit(row, SCOPE, ACCESS, POLICY, NOW).reason_code, 'E_SCOPE_NETWORK')

    def test_verdict_cannot_overwrite_the_measurement_time(self):
        liar = good(NOW - 10, verdict={'min_target_reliability': 1.0, 'checked_at': NOW + 9999,
                                       'valid_until': NOW + 99999})
        row = measured(liar)
        self.assertEqual(row['checked_at'], NOW - 10)
        self.assertEqual(row['valid_until'], NOW + 590)

    def test_a_measured_row_is_admissible_by_the_same_contract(self):
        row = measured(good(NOW - 10))
        decision = core.admit(row, SCOPE, ACCESS, POLICY, NOW)
        self.assertTrue(decision.admitted)
        self.assertAlmostEqual(decision.age_seconds, 10.0)


class UnfinishedMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.row = measured(good(NOW - 10))

    def test_cancelled_recheck_keeps_the_last_completed_result(self):
        cancelled = core.Measurement(endpoint_id='ep-1', job_id='job-2', completed=False)
        after = measured(cancelled, self.row)
        self.assertEqual(after['checked_at'], self.row['checked_at'])
        self.assertEqual(after['valid_until'], self.row['valid_until'])
        self.assertEqual(after['history'], self.row['history'])
        self.assertEqual(after['last_observation_state'], 'preserved')
        self.assertTrue(core.admit(after, SCOPE, ACCESS, POLICY, NOW).admitted)

    def test_measurement_without_a_timestamp_is_not_a_result(self):
        in_flight = core.Measurement(endpoint_id='ep-1', verdict=dict(VERDICT), checked_at=None)
        after = measured(in_flight, self.row)
        self.assertEqual(after['last_observation_state'], 'preserved')
        self.assertEqual(after['valid_until'], self.row['valid_until'])
        self.assertEqual(after['history'], self.row['history'])

    def test_unfinished_measurement_does_not_count_as_a_check(self):
        after = measured(core.Measurement('ep-1', completed=False), self.row)
        self.assertEqual(after['history']['checks'], 1)
        self.assertEqual(after['history']['passes'], 1)

    def test_first_ever_cancelled_measurement_produces_no_row(self):
        after = measured(core.Measurement('ep-1', completed=False))
        self.assertEqual(after['last_observation_state'], 'preserved')
        self.assertNotIn('valid_until', after)
        self.assertEqual(core.admit(after, SCOPE, ACCESS, POLICY, NOW).reason_code,
                         'E_STATE_NO_OBSERVATION')


class HistoryAndFailureTests(unittest.TestCase):
    def test_history_accumulates_and_never_resets(self):
        row = None
        for index, checked_at in enumerate((NOW - 300, NOW - 200, NOW - 100), 1):
            row = measured(good(checked_at, observation_id=f'obs-{index}'), row)
        self.assertEqual(row['history'], {'checks': 3, 'passes': 3,
                                          'first_checked': NOW - 300, 'last_ok': NOW - 100})

    def test_fresh_failure_replaces_the_old_success_and_keeps_history(self):
        row = measured(good(NOW - 200))
        row = measured(failed(NOW - 10), row)
        self.assertEqual(row['error_code'], 'UNREACHABLE')
        self.assertEqual(row['min_target_reliability'], 0.0)
        self.assertEqual(row['history']['checks'], 2)
        self.assertEqual(row['history']['passes'], 1)
        self.assertEqual(row['history']['last_ok'], NOW - 200)
        decision = core.admit(row, SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual(decision.reason_code, 'E_STATE_MEASUREMENT_FAILED')

    def test_a_later_success_clears_the_previous_error(self):
        row = measured(failed(NOW - 200))
        self.assertEqual(core.admit(row, SCOPE, ACCESS, POLICY, NOW).reason_code,
                         'E_STATE_MEASUREMENT_FAILED')
        row = measured(good(NOW - 10, observation_id='obs-2'), row)
        self.assertNotIn('error', row)
        self.assertNotIn('error_code', row)
        self.assertTrue(core.admit(row, SCOPE, ACCESS, POLICY, NOW).admitted)

    def test_history_of_a_legacy_row_starts_at_one(self):
        legacy = {'checked_at': NOW - 100, 'min_target_reliability': 1.0}
        history = core.history_of(legacy, True, NOW - 100)
        self.assertEqual((history['checks'], history['passes']), (1, 1))
        self.assertEqual(history['first_checked'], NOW - 100)
        self.assertEqual(core.history_of(None, False, NOW)['passes'], 0)

    def test_success_is_derived_from_the_measurement_not_the_policy(self):
        ok = core.Measurement('ep-1', checked_at=NOW, verdict={'min_target_reliability': 1.0})
        bad = core.Measurement('ep-1', checked_at=NOW, verdict={'min_target_reliability': 0.0})
        broken = core.Measurement('ep-1', checked_at=NOW, verdict={'min_target_reliability': 1.0},
                                  error={'code': 'UNREACHABLE'})
        self.assertEqual((ok.succeeded, bad.succeeded, broken.succeeded), (True, False, False))
        self.assertTrue(core.Measurement('ep-1', checked_at=NOW, ok=True).succeeded)
        self.assertFalse(core.Measurement('ep-1', checked_at=NOW, completed=False,
                                          verdict={'min_target_reliability': 1.0}).succeeded)

    def test_history_counts_measurements_while_admission_counts_the_policy(self):
        strict = core.Policy(max_age_seconds=600.0, min_anonymity='elite')
        row = measured(good(NOW - 10))
        history_before = dict(row['history'])
        second = measured(good(NOW - 5, observation_id='obs-2'), row, strict)
        # The measurement is a real success even though this policy refuses to serve it.
        self.assertEqual(row['history'], history_before)
        self.assertEqual(second['history'], {'checks': 2, 'passes': 2,
                                             'first_checked': NOW - 10, 'last_ok': NOW - 5})
        self.assertEqual(core.admit(second, SCOPE, ACCESS, strict, NOW).reason_code,
                         'E_STATE_ANONYMITY')
        self.assertTrue(core.admit(second, SCOPE, ACCESS, POLICY, NOW).admitted)

    def test_apply_measurement_does_not_mutate_the_previous_row(self):
        row = measured(good(NOW - 10))
        snapshot = {key: (dict(value) if isinstance(value, dict) else value)
                    for key, value in row.items()}
        measured(failed(NOW), row)
        self.assertEqual(row, snapshot)


if __name__ == '__main__':
    unittest.main()

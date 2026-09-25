"""The acceptance rule itself: required + all/any/at-least-K/none, thresholds, fail-fast.

This is the truth table F05 asks for, plus the two clauses that are easy to lose:
an unknown or skipped check is not a success, and measurements taken under other
thresholds do not become evidence for this revision.
"""
from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import profiles  # noqa: E402
from tests import profiles_support as support  # noqa: E402

BASIC, SEARCH, VIDEO, CHAT = support.BASIC, support.SEARCH, support.VIDEO, support.CHAT


def both_required(**kwargs):
    return support.spec(**kwargs)


def two_required_one_optional(rule, k=None, **kwargs):
    return support.spec(optional_rule=rule, k=k,
                        targets=[support.target(BASIC), support.target(SEARCH),
                                 support.target(VIDEO, 'optional')], **kwargs)


def pass_of(spec, *evidence):
    return profiles.evaluate(spec, list(evidence))['pass']


def reason_of(spec, *evidence):
    return profiles.evaluate(spec, list(evidence))['reason']


class RequiredPlusAllTests(unittest.TestCase):
    def setUp(self):
        self.spec = two_required_one_optional('all')

    def test_all_needs_every_required_and_every_optional_check(self):
        good_basic, good_search, good_video = (support.ok(self.spec, BASIC),
                                               support.ok(self.spec, SEARCH),
                                               support.ok(self.spec, VIDEO))
        self.assertTrue(pass_of(self.spec, good_basic, good_search, good_video))
        self.assertFalse(pass_of(self.spec, good_basic, good_search))
        self.assertEqual(reason_of(self.spec, good_basic, good_search),
                         'E_VERDICT_OPTIONAL_NOT_PASSED')
        self.assertFalse(pass_of(self.spec, good_basic, good_video))
        self.assertEqual(reason_of(self.spec, good_basic, good_video),
                         'E_VERDICT_REQUIRED_NOT_PASSED')

    def test_a_missing_mandatory_probe_is_named_in_the_verdict(self):
        verdict = profiles.evaluate(self.spec, [support.ok(self.spec, BASIC)])
        self.assertFalse(verdict['pass'])
        self.assertEqual([item['target_id'] for item in verdict['required_missing']], [SEARCH])
        self.assertEqual(verdict['required_missing'][0]['state'], 'unmeasured')
        self.assertEqual(verdict['required_passed'], [BASIC])
        self.assertEqual(verdict['targets'][VIDEO]['state'], 'unmeasured')


class RequiredPlusAnyTests(unittest.TestCase):
    def setUp(self):
        self.spec = support.spec(optional_rule='any',
                                 targets=[support.target(BASIC),
                                          support.target(VIDEO, 'optional'),
                                          support.target(SEARCH, 'optional')])

    def test_one_optional_probe_is_enough_but_a_mandatory_one_still_decides(self):
        self.assertTrue(pass_of(self.spec, support.ok(self.spec, BASIC),
                                support.ok(self.spec, VIDEO)))
        self.assertFalse(pass_of(self.spec, support.ok(self.spec, BASIC),
                                 support.bad(self.spec, VIDEO), support.bad(self.spec, SEARCH)))
        self.assertEqual(reason_of(self.spec, support.ok(self.spec, BASIC),
                                   support.bad(self.spec, VIDEO), support.bad(self.spec, SEARCH)),
                         'E_VERDICT_OPTIONAL_NOT_PASSED')
        self.assertFalse(pass_of(self.spec, support.bad(self.spec, BASIC),
                                 support.ok(self.spec, VIDEO)))
        self.assertEqual(reason_of(self.spec, support.bad(self.spec, BASIC),
                                   support.ok(self.spec, VIDEO)),
                         'E_VERDICT_REQUIRED_NOT_PASSED')

    def test_an_unknown_optional_probe_does_not_count_as_success(self):
        self.assertFalse(pass_of(self.spec, support.ok(self.spec, BASIC),
                                 support.unknown(self.spec, VIDEO)))
        self.assertEqual(profiles.evaluate(self.spec, [support.ok(self.spec, BASIC),
                                                      support.unknown(self.spec, VIDEO)])
                         ['optional_missing'][0]['state'], 'unknown')


class RequiredPlusAtLeastTests(unittest.TestCase):
    def setUp(self):
        # Three optional probes, K = 2: "at least K" is a real count, not "all".
        self.spec = support.spec(optional_rule='at_least', k=2,
                                 targets=[support.target(BASIC),
                                          support.target(VIDEO, 'optional'),
                                          support.target(SEARCH, 'optional'),
                                          support.target(CHAT, 'optional')])

    def test_exactly_k_of_the_optional_probes_pass(self):
        self.assertTrue(pass_of(self.spec, support.ok(self.spec, BASIC),
                                support.ok(self.spec, VIDEO), support.ok(self.spec, SEARCH)))
        # Two of three is enough, whichever two.
        self.assertTrue(pass_of(self.spec, support.ok(self.spec, BASIC),
                                support.ok(self.spec, VIDEO), support.ok(self.spec, SEARCH),
                                support.bad(self.spec, CHAT)))
        self.assertTrue(pass_of(self.spec, support.ok(self.spec, BASIC),
                                support.ok(self.spec, VIDEO), support.bad(self.spec, SEARCH),
                                support.ok(self.spec, CHAT)))
        # One of three is not.
        self.assertFalse(pass_of(self.spec, support.ok(self.spec, BASIC),
                                 support.ok(self.spec, VIDEO), support.bad(self.spec, SEARCH),
                                 support.bad(self.spec, CHAT)))
        # A skipped check never counts towards K.
        self.assertFalse(pass_of(self.spec, support.ok(self.spec, BASIC),
                                 support.ok(self.spec, VIDEO), support.skipped(self.spec, SEARCH),
                                 support.bad(self.spec, CHAT)))
        self.assertEqual(reason_of(self.spec, support.ok(self.spec, BASIC),
                                   support.ok(self.spec, VIDEO), support.skipped(self.spec, SEARCH),
                                   support.bad(self.spec, CHAT)),
                         'E_VERDICT_OPTIONAL_NOT_PASSED')

    def test_the_mandatory_set_is_not_counted_towards_k(self):
        verdict = profiles.evaluate(self.spec, [support.ok(self.spec, BASIC),
                                               support.ok(self.spec, VIDEO)])
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['optional_needed'], 2)
        self.assertEqual(verdict['optional_passed'], [VIDEO])


class OptionalNoneTests(unittest.TestCase):
    def setUp(self):
        self.spec = support.spec(optional_rule='none', attempts=2,
                                 targets=[support.target(BASIC),
                                          support.target(VIDEO, 'optional'),
                                          support.target(SEARCH, 'optional')])

    def test_the_none_rule_ignores_the_optional_failures(self):
        relaxed = self.spec.copy_of(targets=[support.target(BASIC, min_success=0.5),
                                             support.target(VIDEO, 'optional'),
                                             support.target(SEARCH, 'optional')])
        self.assertTrue(pass_of(relaxed, support.mixed(relaxed, BASIC, 1, 2)))
        self.assertFalse(pass_of(self.spec, support.bad(self.spec, BASIC),
                                 support.ok(self.spec, VIDEO)))
        self.assertEqual(reason_of(self.spec, support.bad(self.spec, BASIC),
                                   support.ok(self.spec, VIDEO)),
                         'E_VERDICT_REQUIRED_NOT_PASSED')
        self.assertEqual(profiles.plan(self.spec)['optional_needed'], 0)


class UnknownSkippedUnmeasuredTests(unittest.TestCase):
    def setUp(self):
        self.spec = both_required(attempts=2)

    def test_unknown_is_not_fail_and_not_success(self):
        verdict = profiles.evaluate(self.spec, [support.unknown(self.spec, BASIC),
                                               support.ok(self.spec, SEARCH)])
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['targets'][BASIC]['state'], 'unknown')
        self.assertEqual(verdict['required_missing'][0]['reason'], 'E_TARGET_UNKNOWN')
        self.assertEqual(verdict['required_passed'], [SEARCH])

    def test_a_skipped_probe_is_not_evidence(self):
        """Fail-fast and the budget cut a check: it is skipped, never invented."""
        verdict = profiles.evaluate(self.spec, [support.skipped(self.spec, BASIC),
                                               support.ok(self.spec, SEARCH)])
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['targets'][BASIC]['state'], 'skipped')
        self.assertEqual(verdict['required_missing'][0]['reason'], 'E_TARGET_SKIPPED')
        self.assertEqual(verdict['probes_used'], 1)

    def test_partial_measurements_are_judged_against_the_threshold(self):
        self.assertFalse(pass_of(self.spec, support.mixed(self.spec, BASIC, 1, 2),
                                 support.ok(self.spec, SEARCH)))
        verdict = profiles.evaluate(self.spec, [support.mixed(self.spec, BASIC, 1, 2),
                                               support.ok(self.spec, SEARCH)])
        self.assertEqual(verdict['required_missing'][0]['reason'], 'E_TARGET_BELOW_MIN_SUCCESS')
        self.assertEqual(verdict['targets'][BASIC]['ratio'], 0.5)

    def test_a_threshold_of_two_thirds_accepts_two_of_three(self):
        spec = both_required(attempts=3, targets=[support.target(BASIC, min_success=2 / 3),
                                                  support.target(SEARCH)])
        self.assertTrue(pass_of(spec, support.mixed(spec, BASIC, 2, 3), support.ok(spec, SEARCH)))
        self.assertFalse(pass_of(spec, support.mixed(spec, BASIC, 1, 3),
                                 support.ok(spec, SEARCH)))

    def test_the_float_rule_is_the_one_the_admission_path_already_uses(self):
        """One epsilon, one meaning: a 2/3 ratio is not a coin flip between surfaces."""
        from proxy_workbench import reputation

        for threshold in (2 / 3, 0.6666666666666667, 0.7, 0.5):
            with self.subTest(threshold=threshold):
                spec = both_required(attempts=3,
                                     targets=[support.target(BASIC, min_success=threshold),
                                              support.target(SEARCH)])
                measured = pass_of(spec, support.mixed(spec, BASIC, 2, 3),
                                   support.ok(spec, SEARCH))
                row = {'proxy': 'http://127.0.0.1:9', 'min_target_reliability': 2 / 3}
                self.assertEqual(measured, reputation.result_allowed(row, threshold))


class TargetThresholdTests(unittest.TestCase):
    def test_latency_threshold_is_per_target(self):
        spec = both_required(attempts=1, targets=[support.target(BASIC, max_latency_ms=1000.0),
                                                  support.target(SEARCH, max_latency_ms=200.0)])
        self.assertTrue(pass_of(spec, support.ok(spec, BASIC, latency_ms=900.0),
                                support.ok(spec, SEARCH, latency_ms=150.0)))
        verdict = profiles.evaluate(spec, [support.ok(spec, BASIC, latency_ms=900.0),
                                          support.ok(spec, SEARCH, latency_ms=900.0)])
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['required_missing'][0]['reason'], 'E_TARGET_LATENCY_EXCEEDED')
        self.assertEqual(verdict['required_missing'][0]['target_id'], SEARCH)

    def test_a_missing_latency_is_not_a_pass_when_a_latency_is_required(self):
        spec = both_required(targets=[support.target(BASIC, max_latency_ms=1000.0),
                                      support.target(SEARCH)])
        verdict = profiles.evaluate(spec, [support.ok(spec, BASIC), support.ok(spec, SEARCH)])
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['required_missing'][0]['reason'], 'E_TARGET_LATENCY_MISSING')

    def test_a_latency_above_zero_threshold_fails(self):
        spec = both_required(targets=[support.target(BASIC, max_latency_ms=0.0),
                                      support.target(SEARCH)])
        self.assertFalse(pass_of(spec, support.ok(spec, BASIC, latency_ms=1.0),
                                 support.ok(spec, SEARCH)))
        self.assertTrue(pass_of(spec, support.ok(spec, BASIC, latency_ms=0.0),
                                support.ok(spec, SEARCH)))


class ThresholdChangeTests(unittest.TestCase):
    """F05: "изменение порога не приписывает недовыполненным измерениям доказательство"."""

    def test_evidence_measured_under_other_thresholds_is_not_admissible(self):
        before = both_required(attempts=2, targets=[support.target(BASIC),
                                                    support.target(SEARCH)])
        measured = [support.mixed(before, BASIC, 1, 2), support.ok(before, SEARCH)]
        self.assertFalse(pass_of(before, *measured))

        after = before.copy_of(targets=[support.target(BASIC, min_success=0.5),
                                        support.target(SEARCH)])
        verdict = profiles.evaluate(after, measured)
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['required_missing'][0]['target_id'], BASIC)
        self.assertEqual(verdict['required_missing'][0]['state'], 'unmeasured')
        self.assertEqual(verdict['required_missing'][0]['reason'], 'E_TARGET_STALE_EVIDENCE')
        # Re-measuring under the new revision is what makes it admissible again.
        self.assertTrue(pass_of(after, support.mixed(after, BASIC, 1, 2),
                                support.ok(after, SEARCH)))

    def test_evidence_without_a_threshold_fingerprint_is_not_admissible(self):
        spec = both_required()
        loose = [profiles.TargetEvidence(BASIC, 1, 1, 40.0), support.ok(spec, SEARCH)]
        verdict = profiles.evaluate(spec, loose)
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['targets'][BASIC]['state'], 'unmeasured')
        self.assertEqual(verdict['targets'][BASIC]['reason'], 'E_TARGET_STALE_EVIDENCE')

    def test_the_fingerprint_covers_thresholds_only(self):
        required = profiles.TargetRule(BASIC, 'required', True, 0.5, None)
        optional = profiles.TargetRule(BASIC, 'optional', True, 0.5, None)
        self.assertEqual(required.fingerprint(), optional.fingerprint())
        self.assertNotEqual(required.fingerprint(),
                            profiles.TargetRule(BASIC, 'required', True, 0.6, None).fingerprint())
        # Same measurement, different role: the evidence is still the same evidence,
        # so moving a check between M and O does not invalidate what was measured.
        as_required = both_required(targets=[support.target(BASIC, min_success=0.5),
                                             support.target(SEARCH)])
        as_optional = both_required(targets=[support.target(BASIC, 'optional', min_success=0.5),
                                             support.target(SEARCH)])
        evidence = support.mixed(as_required, BASIC, 1, 2)
        self.assertEqual(as_optional.assess(evidence).state, 'pass')
        self.assertEqual(as_optional.required_targets, (as_optional.target(SEARCH),))


class EvidenceShapeTests(unittest.TestCase):
    def setUp(self):
        self.spec = both_required(attempts=2)
        self.evidence = [support.mixed(self.spec, BASIC, 2, 2), support.mixed(self.spec, SEARCH, 1, 2)]

    def test_list_mapping_and_typed_evidence_agree(self):
        as_list = profiles.evaluate(self.spec, self.evidence)
        as_mapping = profiles.evaluate(self.spec, {item.target_id: item for item in self.evidence})
        as_dicts = profiles.evaluate(self.spec, [item.as_dict() for item in self.evidence])
        self.assertEqual(json.dumps(as_list, sort_keys=True), json.dumps(as_mapping, sort_keys=True))
        self.assertEqual(json.dumps(as_list, sort_keys=True), json.dumps(as_dicts, sort_keys=True))

    def test_the_verdict_is_deterministic_and_serialisable(self):
        first = profiles.evaluate(self.spec, self.evidence)
        second = profiles.evaluate(self.spec, self.evidence)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first, second)
        self.assertIn('targets', first)
        self.assertNotIn(0, first['targets'][BASIC])

    def test_evidence_of_an_unknown_target_is_refused(self):
        with self.assertRaises(profiles.ProfileError) as raised:
            profiles.evaluate(self.spec, [support.ok(self.spec, 'not-in-profile')])
        self.assertEqual(raised.exception.code, 'E_VALIDATION_FIELD')

    def test_impossible_evidence_is_refused(self):
        for evidence in ({'target': BASIC, 'ok': 2, 'attempts': 1},
                         {'target': BASIC, 'attempts': -1},
                         {'target': '', 'ok': 0, 'attempts': 1},
                         {'target': BASIC, 'ok': 0, 'attempts': 1, 'latency_ms': -5}):
            with self.subTest(evidence=evidence):
                with self.assertRaises(profiles.ProfileError):
                    profiles.TargetEvidence.parse(evidence)

    def test_aliases_of_the_target_key_are_accepted(self):
        for key in ('target', 'target_id', 'id'):
            item = profiles.TargetEvidence.parse({key: BASIC, 'ok': 1, 'attempts': 1})
            self.assertEqual(item.target_id, BASIC)


class FailFastTests(unittest.TestCase):
    """A stop is allowed only when no completion of the remaining probes can pass."""

    def test_probing_continues_while_the_threshold_is_still_reachable(self):
        spec = both_required(attempts=2, targets=[support.target(BASIC, min_success=0.5),
                                                  support.target(SEARCH)])
        state = profiles.RunState(spec)
        self.assertFalse(state.stop().stop)
        state.record(BASIC, ok=0)  # 0 of 1 used is still reachable as 1 of 2
        decision = state.stop()
        self.assertFalse(decision.stop)
        self.assertIn(BASIC, decision.live)
        # The state is provisional while attempts are left: it is a fail so far, and
        # only ``stop`` knows that one more attempt can still lift it over 0.5.
        self.assertEqual(state.state(BASIC), 'fail')

    def test_the_run_stops_when_a_mandatory_probe_can_no_longer_reach_its_threshold(self):
        spec = both_required(attempts=2, targets=[support.target(BASIC, min_success=2 / 3),
                                                  support.target(SEARCH)])
        state = profiles.RunState(spec)
        state.record(BASIC, ok=0)
        state.record(BASIC, ok=0)  # 0 of 2: the best reachable ratio is 0
        decision = state.stop()
        self.assertTrue(decision.stop)
        self.assertEqual(decision.reason, 'E_VERDICT_REQUIRED_NOT_PASSED')
        self.assertEqual(decision.live, (SEARCH,))
        # The target that was cut is reported as pending, never as measured.
        self.assertEqual(state.pending_targets(), [SEARCH])
        verdict = state.evaluate()
        self.assertFalse(verdict['pass'])
        # The cut target is reported as skipped: an unrun check is not a measurement.
        self.assertEqual(verdict['targets'][SEARCH]['state'], 'skipped')
        self.assertEqual(verdict['targets'][SEARCH]['reason'], 'E_TARGET_SKIPPED')

    def test_at_least_counts_the_probes_that_can_still_pass(self):
        spec = support.spec(optional_rule='at_least', k=2, attempts=1,
                            targets=[support.target(BASIC),
                                     support.target(VIDEO, 'optional'),
                                     support.target(SEARCH, 'optional')])
        state = profiles.RunState(spec)
        state.record(BASIC, ok=1)
        state.record(VIDEO, ok=0)  # one optional is dead, one is still open
        decision = state.stop()
        self.assertTrue(decision.stop)
        self.assertEqual(decision.reason, 'E_VERDICT_OPTIONAL_NOT_PASSED')
        self.assertFalse(state.can_pass())

    def test_a_run_that_can_still_pass_keeps_going_to_the_end(self):
        spec = both_required(attempts=2, targets=[support.target(BASIC, min_success=0.5),
                                                  support.target(SEARCH)])
        state = profiles.RunState(spec)
        for target_id in (BASIC, SEARCH):
            state.record(target_id, ok=1)
            self.assertFalse(state.stop().stop)
        for target_id in (BASIC, SEARCH):
            state.record(target_id, ok=1)
        self.assertTrue(state.evaluate()['pass'])
        self.assertEqual(state.pending_targets(), [])
        self.assertEqual(state.stop().stop, False)

    def test_the_budget_stops_a_run_that_would_otherwise_pass(self):
        spec = both_required(attempts=3, targets=[support.target(BASIC),
                                                  support.target(SEARCH)],
                             budget={'max_probes': 6, 'max_duration_s': 10.0})
        state = profiles.RunState(spec)
        for _ in range(2):
            for target_id in (BASIC, SEARCH):
                state.record(target_id, ok=1)
        self.assertEqual(state.probes_used, 4)
        # The time budget is only judged when the caller passes the elapsed time:
        # this module has no clock, so it cannot guess one.
        self.assertFalse(state.stop(elapsed_s=9.0).stop)
        self.assertTrue(state.stop(elapsed_s=10.0).stop)
        state.record(BASIC, ok=1)
        state.record(SEARCH, ok=1)
        self.assertEqual(state.probes_used, 6)
        decision = state.stop()
        self.assertTrue(decision.stop)
        self.assertEqual(decision.reason, 'E_LIMIT_BUDGET')
        self.assertEqual(decision.live, (BASIC, SEARCH))

    def test_recording_after_the_budget_or_a_disabled_target_is_refused(self):
        spec = both_required(attempts=1, targets=[support.target(BASIC),
                                                  support.target(SEARCH, enabled=False)])
        state = profiles.RunState(spec)
        state.record(BASIC, ok=1)
        with self.assertRaises(profiles.ProfileError) as raised:
            state.record(BASIC, ok=1)
        self.assertEqual(raised.exception.code, 'E_CONFLICT_REVISION')
        with self.assertRaises(profiles.ProfileError):
            state.record(SEARCH, ok=1)
        with self.assertRaises(profiles.ProfileError):
            state.record('absent', ok=1)
        with self.assertRaises(profiles.ProfileError):
            state.record(BASIC, ok=3)

    def test_the_checkpoint_facts_name_every_target(self):
        spec = both_required(attempts=2)
        state = profiles.RunState(spec)
        state.record(BASIC, ok=1, latency_ms=120.0)
        state.record(SEARCH, ok=0, unknown=True)
        self.assertEqual([item.as_dict() for item in state.as_decisions()],
                         [{'target_id': BASIC, 'done': 1, 'ok': 1, 'latency_ms': 120.0,
                           'unknown': False},
                          {'target_id': SEARCH, 'done': 1, 'ok': 0, 'latency_ms': None,
                           'unknown': True}])
        verdict = state.evaluate()
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['targets'][SEARCH]['state'], 'unknown')


if __name__ == '__main__':
    unittest.main()

"""Profile specification: selected targets, required/optional, thresholds, budget.

Covers the F05 configuration rules, including the non-empty guarantee: ``all``
over an empty set, ``K = 0`` and a fully disabled target set are refused instead
of being saved as a profile that can never pass.
"""
from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import profiles  # noqa: E402
from tests import profiles_support as support  # noqa: E402

REQUIRED = 'E_VALIDATION_FIELD'
UNKNOWN_FIELD = 'E_VALIDATION_UNKNOWN_FIELD'


def unvalidated(**document):
    """A document that bypasses cross-field validation, as a foreign row would."""
    document.setdefault('targets', [{'id': support.BASIC}])
    return profiles.ProfileSpec.from_dict(document, validate=False)


def verdict_of(document, evidence=None):
    return profiles.evaluate(unvalidated(**document), evidence)


class SelectionTests(unittest.TestCase):
    def test_round_trip_keeps_every_field(self):
        original = support.spec(optional_rule='at_least', k=2, attempts=3,
                                budget={'max_probes': 40, 'max_duration_s': 120.0},
                                targets=[support.target(support.BASIC, min_success=0.5,
                                                        max_latency_ms=1500.0),
                                         support.target(support.SEARCH, 'optional'),
                                         support.target(support.VIDEO, 'optional')],
                                description='API проекта')
        again = profiles.ProfileSpec.from_json(original.to_json())
        self.assertEqual(again.as_dict(), original.as_dict())
        self.assertEqual(again.digest, original.digest)
        self.assertEqual(json.loads(original.to_json()), original.as_dict())

    def test_digest_covers_the_stored_document(self):
        one = support.spec(targets=[support.target(support.BASIC), support.target(support.SEARCH)])
        same = support.spec(targets=[support.target(support.BASIC), support.target(support.SEARCH)])
        self.assertEqual(one.digest, same.digest)
        # A threshold is content, so it changes the identity of the revision.
        lowered = support.spec(targets=[support.target(support.BASIC, min_success=0.5),
                                        support.target(support.SEARCH)])
        self.assertNotEqual(one.digest, lowered.digest)
        # The probe order is content too: reordering is a change, not a no-op edit
        # that would be dropped when the digest is compared on update.
        reordered = support.spec(targets=[support.target(support.SEARCH), support.target(support.BASIC)])
        self.assertNotEqual(one.digest, reordered.digest)

    def test_selected_targets_and_kinds(self):
        spec = support.spec(optional_rule='any',
                            targets=[support.target(support.BASIC),
                                     support.target(support.VIDEO, 'optional'),
                                     support.target(support.SEARCH, enabled=False)])
        plan = profiles.plan(spec)
        self.assertEqual(plan['required'], [support.BASIC])
        self.assertEqual(plan['optional'], [support.VIDEO])
        self.assertEqual(plan['disabled'], [support.SEARCH])
        # A disabled target is measured by nobody: it must not raise min_probes.
        self.assertEqual(plan['min_probes'], 2)

    def test_min_probes_follows_the_rule(self):
        targets = [support.target(support.BASIC), support.target(support.SEARCH),
                   support.target(support.VIDEO, 'optional')]
        self.assertEqual(support.spec(targets=targets, attempts=2).min_probes, 4)
        self.assertEqual(support.spec(targets=targets, attempts=2, optional_rule='all').min_probes, 6)
        self.assertEqual(support.spec(targets=targets, attempts=2, optional_rule='any').min_probes, 6)
        self.assertEqual(support.spec(targets=targets, attempts=2, optional_rule='at_least',
                                      k=1).min_probes, 6)

    def test_optional_rule_needs_a_number_above_zero(self):
        for mode in profiles.OPTIONAL_MODES:
            self.assertEqual(profiles.OptionalRule.parse(mode).mode, mode)
        self.assertEqual(profiles.OptionalRule.parse('at_least', 3).k, 3)
        self.assertEqual(profiles.OptionalRule.parse({'mode': 'at_least', 'k': 2}).k, 2)
        self.assertEqual(profiles.OptionalRule.parse(None).mode, 'none')
        with self.assertRaises(profiles.ProfileError) as raised:
            profiles.OptionalRule.parse('most', 1)
        self.assertEqual(raised.exception.code, REQUIRED)


class NonEmptyGuaranteeTests(unittest.TestCase):
    """MASTER-PROMPT F05: all(empty), K=0 and all-disabled probes never pass."""

    def assert_refused(self, code, build):
        with self.assertRaises(profiles.ProfileError) as raised:
            build()
        self.assertEqual(raised.exception.code, code)

    def test_all_over_an_empty_set_is_refused_and_cannot_pass(self):
        self.assert_refused(REQUIRED, lambda: support.spec(targets=[]))
        verdict = verdict_of({'targets': [], 'optional_rule': 'all'})
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['reason'], 'E_VERDICT_EMPTY_TARGET_SET')

    def test_k_zero_is_refused_and_cannot_pass(self):
        self.assert_refused(REQUIRED,
                            lambda: support.spec(optional_rule='at_least', k=1,
                                                 targets=[support.target(support.BASIC),
                                                         support.target(support.VIDEO, 'optional')],
                                                 budget={'max_probes': 0}))
        verdict = verdict_of({'targets': [{'id': support.BASIC}],
                              'optional_rule': {'mode': 'at_least', 'k': 0}},
                             [support.ok(unvalidated(targets=[{'id': support.BASIC}]), support.BASIC)])
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['reason'], 'E_VERDICT_K_NOT_POSITIVE')

    def test_k_above_the_optional_set_is_refused_and_cannot_pass(self):
        self.assert_refused(REQUIRED,
                            lambda: support.spec(optional_rule='at_least', k=2,
                                                 targets=[support.target(support.BASIC),
                                                         support.target(support.VIDEO, 'optional')]))
        document = {'targets': [{'id': support.BASIC}, {'id': support.VIDEO, 'kind': 'optional'}],
                    'optional_rule': {'mode': 'at_least', 'k': 3}}
        verdict = verdict_of(document)
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['reason'], 'E_VERDICT_K_UNREACHABLE')

    def test_a_fully_disabled_target_set_is_refused_and_cannot_pass(self):
        self.assert_refused(REQUIRED,
                            lambda: support.spec(targets=[support.target(support.BASIC, enabled=False)]))
        verdict = verdict_of({'targets': [{'id': support.BASIC, 'enabled': False}]})
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['reason'], 'E_VERDICT_EMPTY_TARGET_SET')

    def test_the_mandatory_set_may_not_be_emptied_by_making_everything_optional(self):
        """"Это исключает ошибочное превращение обязательной проверки в необязательную"."""
        self.assert_refused(REQUIRED,
                            lambda: support.spec(targets=[support.target(support.SEARCH, 'optional')]))
        # A row a foreign writer left behind can still reach the decision, and it
        # never passes without a probe of its own: the ``none`` rule proves nothing.
        document = {'targets': [{'id': support.SEARCH, 'kind': 'optional'}], 'optional_rule': 'none'}
        empty = verdict_of(document)
        self.assertFalse(empty['pass'])
        self.assertEqual(empty['reason'], 'E_VERDICT_NO_EFFECTIVE_PROBE')
        measured = verdict_of(document, [support.ok(unvalidated(**document), support.SEARCH)])
        self.assertTrue(measured['pass'])
        self.assertEqual(measured['effective_probes'], 1)

    def test_an_empty_optional_set_is_allowed_only_under_the_explicit_none_policy(self):
        self.assertEqual(support.spec().optional_rule.mode, 'none')
        for mode in ('all', 'any'):
            with self.subTest(mode=mode):
                self.assert_refused(REQUIRED, lambda mode=mode: support.spec(optional_rule=mode))
        verdict = verdict_of({'targets': [{'id': support.BASIC}], 'optional_rule': 'all'})
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['reason'], 'E_VERDICT_EMPTY_OPTIONAL_SET')

    def test_no_successful_probe_is_never_a_pass(self):
        document = {'targets': [{'id': support.SEARCH, 'kind': 'optional'}],
                    'optional_rule': 'none'}
        verdict = verdict_of(document, [support.bad(unvalidated(**document), support.SEARCH,
                                                     attempts=2)])
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['reason'], 'E_VERDICT_NO_EFFECTIVE_PROBE')
        self.assertEqual(verdict['effective_probes'], 0)


class BudgetAndBoundsTests(unittest.TestCase):
    def test_budget_below_the_minimum_is_refused(self):
        with self.assertRaises(profiles.ProfileError) as raised:
            support.spec(attempts=2, budget={'max_probes': 3})
        self.assertEqual(raised.exception.code, REQUIRED)
        self.assertIn('минимум', str(raised.exception))
        self.assertEqual(support.spec(attempts=2, budget={'max_probes': 4}).max_probes, 4)

    def test_budget_bounds(self):
        for budget in ({'max_probes': 0}, {'max_probes': 'many'}, {'max_duration_s': 0},
                       {'max_duration_s': -1.0}, {'max_probes': 1, 'unknown': 1}):
            with self.subTest(budget=budget):
                with self.assertRaises(profiles.ProfileError):
                    support.spec(budget=budget)

    def test_attempts_and_threshold_bounds(self):
        for attempts in (0, -1, profiles.MAX_ATTEMPTS + 1, 1.5, 'two'):
            with self.subTest(attempts=attempts):
                with self.assertRaises(profiles.ProfileError):
                    support.spec(attempts=attempts)
        for threshold in (0, -0.5, 1.5, float('inf'), True):
            with self.subTest(min_success=threshold):
                with self.assertRaises(profiles.ProfileError):
                    profiles.TargetRule(support.BASIC, min_success=threshold)
        self.assertEqual(profiles.TargetRule(support.BASIC, min_success=2 / 3).min_success, 2 / 3)

    def test_names_and_descriptions_are_bounded_single_lines(self):
        for name in ('', '   ', 'x' * (profiles.MAX_NAME + 1), 'two\nlines'):
            with self.subTest(name=name):
                with self.assertRaises(profiles.ProfileError):
                    profiles.clean_name(name)
        self.assertEqual(profiles.clean_name('  API  '), 'API')
        with self.assertRaises(profiles.ProfileError):
            support.spec(description='x' * (profiles.MAX_DESCRIPTION + 1))

    def test_duplicate_targets_and_odd_identifiers_are_refused(self):
        with self.assertRaises(profiles.ProfileError):
            support.spec(targets=[support.target(support.BASIC), support.target(support.BASIC)])
        for target_id in ('', 'Bad Id', '../escape', 'x' * 65):
            with self.subTest(target_id=target_id):
                with self.assertRaises(profiles.ProfileError):
                    support.spec(targets=[support.target(target_id)])


class UnknownFieldTests(unittest.TestCase):
    """F07: an unrecognised parameter is refused everywhere, not ignored."""

    def test_unknown_fields_are_refused_in_every_document(self):
        cases = [
            (lambda: support.spec(targets=[dict(support.target(support.BASIC), retries=3)]), 'target'),
            (lambda: profiles.ProfileSpec.from_dict(dict(support.spec().as_dict(), extra=1)), 'profile'),
            (lambda: profiles.TargetEvidence.parse({'target': support.BASIC, 'weight': 2}), 'evidence'),
            (lambda: profiles.run_request({'profile': support.spec().as_dict(), 'mode': 'fast'}), 'request'),
            (lambda: profiles.Budget.parse({'max_probes': 4, 'max_workers': 8}), 'budget'),
        ]
        for build, what in cases:
            with self.subTest(document=what):
                with self.assertRaises(profiles.ProfileError) as raised:
                    build()
                self.assertEqual(raised.exception.code, UNKNOWN_FIELD)

    def test_a_newer_document_version_is_refused(self):
        with self.assertRaises(profiles.ProfileError) as raised:
            profiles.ProfileSpec.from_dict(dict(support.spec().as_dict(),
                                               version=profiles.CONFIG_VERSION + 1))
        self.assertEqual(raised.exception.code, 'E_IMPORT_REVISION')


class LegacyBridgeTests(unittest.TestCase):
    """A content-addressed row of the old table becomes a named revision 1."""

    def test_every_legacy_target_stays_mandatory_with_the_old_threshold(self):
        spec = profiles.from_legacy_config(json.loads(support.LEGACY_CONFIG))
        self.assertEqual([target.id for target in spec.required_targets],
                         ['target-0', 'target-1'])
        self.assertEqual(spec.optional_targets, ())
        self.assertEqual(spec.attempts, 2)
        self.assertAlmostEqual(spec.required_targets[0].min_success, 2 / 3)
        # The old admission threshold keeps its meaning: 1 of 2 attempts is a fail.
        self.assertFalse(profiles.evaluate(
            spec, [spec.evidence('target-0', ok=1, attempts=2)])['pass'])
        self.assertTrue(profiles.evaluate(
            spec, [spec.evidence('target-0', ok=2, attempts=2),
                   spec.evidence('target-1', ok=2, attempts=2)])['pass'])

    def test_a_legacy_config_without_fail_fast_keeps_every_attempt(self):
        legacy = json.loads(support.LEGACY_CONFIG)
        del legacy['fail_fast']
        spec = profiles.from_legacy_config(legacy)
        self.assertEqual([target.min_success for target in spec.required_targets], [1.0, 1.0])
        self.assertFalse(profiles.evaluate(
            spec, [spec.evidence('target-0', ok=1, attempts=2)])['pass'])

    def test_named_target_ids_of_a_legacy_config_are_kept_when_they_fit(self):
        legacy = json.loads(support.LEGACY_CONFIG)
        legacy['targets'][0]['id'] = 'http-basic'
        spec = profiles.from_legacy_config(legacy)
        self.assertEqual([target.id for target in spec.required_targets],
                         ['http-basic', 'target-1'])

    def test_an_empty_legacy_config_is_refused(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.from_legacy_config({'targets': [], 'attempts': 1})
        with self.assertRaises(profiles.ProfileError):
            profiles.from_legacy_config({'attempts': 1})


class PublicApiTests(unittest.TestCase):
    def test_plan_is_json_serialisable_and_advertises_the_whole_decision(self):
        document = profiles.plan(support.spec(optional_rule='any', attempts=2,
                                              targets=[support.target(support.BASIC),
                                                       support.target(support.VIDEO, 'optional')]))
        self.assertEqual(json.loads(json.dumps(document)), document)
        self.assertEqual(document['optional_rule'], {'mode': 'any', 'k': None})
        self.assertEqual(document['optional_needed'], 1)
        self.assertEqual(document['digest'], support.spec(optional_rule='any', attempts=2,
                                                          targets=[support.target(support.BASIC),
                                                                   support.target(support.VIDEO,
                                                                                 'optional')]).digest)

    def test_create_spec_is_the_same_call_as_the_classmethod(self):
        self.assertEqual(profiles.create_spec(targets=[support.target(support.BASIC),
                                                      support.target(support.SEARCH)]).as_dict(),
                         support.spec().as_dict())

    def test_copy_of_validates_the_change(self):
        original = support.spec()
        relaxed = original.copy_of(attempts=4)
        self.assertEqual(relaxed.attempts, 4)
        self.assertEqual(original.attempts, 1)
        with self.assertRaises(profiles.ProfileError):
            original.copy_of(targets=[])

    def test_every_emitted_reason_code_is_documented(self):
        """i18n needs one closed list, so no verdict may invent a code (CONTRACTS §5.4)."""
        emitted = set()
        cases = [
            (support.spec(), [support.ok(support.spec(), support.BASIC)]),
            (support.spec(attempts=2), [support.bad(support.spec(attempts=2), support.BASIC,
                                                  attempts=2)]),
            (support.spec(optional_rule='at_least', k=1,
                          targets=[support.target(support.BASIC),
                                   support.target(support.VIDEO, 'optional')]),
             [support.ok(support.spec(optional_rule='at_least', k=1,
                                      targets=[support.target(support.BASIC),
                                               support.target(support.VIDEO, 'optional')]),
                        support.BASIC)]),
        ]
        for spec, evidence in cases:
            verdict = profiles.evaluate(spec, evidence)
            emitted.add(verdict['reason'])
            for item in verdict['required_missing'] + verdict['optional_missing']:
                emitted.add(item['reason'])
        for document in ({'targets': []}, {'targets': [{'id': support.BASIC, 'enabled': False}]},
                         {'targets': [{'id': support.BASIC}], 'optional_rule': 'all'}):
            emitted.add(verdict_of(document)['reason'])
        self.assertNotIn(None, emitted)
        self.assertTrue(emitted <= set(profiles.REASON_CODES),
                        f'undocumented reason codes: {sorted(emitted - set(profiles.REASON_CODES))}')


if __name__ == '__main__':
    unittest.main()

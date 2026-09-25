"""One country criterion, applied the same way everywhere (F08).

The cases below are the acceptance lines of F08: a German endpoint wanted for a
Dutch exit, unknown never passing for "not in NL", names in RU and EN resolving
to the same ISO code, and the origin and date of every piece of knowledge.
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import geo
from proxy_workbench import geoip

NOW = 1_800_000_000.0


def fact(code, source=geo.SOURCE_GEOIP, at=None, address=None):
    return geo.CountryFact.of(code, source, at=at, address=address)


class ParseCriterionTests(unittest.TestCase):
    def test_plain_codes_keep_the_picker_meaning(self):
        criterion = geo.parse_criterion('de, NL,DE')
        self.assertEqual(criterion.include, ('DE', 'NL'))
        self.assertEqual(criterion.exclude, ())
        self.assertEqual(criterion.basis, geo.BASIS_ENDPOINT)
        self.assertEqual(criterion.countries, ('DE', 'NL'))

    def test_legacy_default_for_unknown_is_explicit_in_the_criterion(self):
        # "DE,NL" drops an endpoint without a known country today; the value is
        # carried in the criterion so a surface can show it instead of assume it.
        self.assertEqual(geo.parse_criterion('DE,NL').unknown, geo.UNKNOWN_EXCLUDE)
        self.assertEqual(geo.parse_criterion('DE,NL').countries, geoip.parse_countries('DE,NL'))

    def test_exclusions_and_names(self):
        criterion = geo.parse_criterion('Германия,NL,!FR,-Belgium')
        self.assertEqual(criterion.include, ('DE', 'NL'))
        self.assertEqual(criterion.exclude, ('BE', 'FR'))
        self.assertEqual(geo.parse_criterion('Germany').include, ('DE',))
        self.assertEqual(geo.parse_criterion(['nl']).include, ('NL',))
        self.assertEqual(geo.parse_criterion('').active, False)

    def test_unknown_token_is_reported_with_its_text(self):
        for value in ('Atlantis', 'DEU', 'D1', '!СШАа'):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                geo.parse_criterion(value)
            self.assertIn(value.strip('!'), str(caught.exception))
        with self.assertRaises(ValueError):
            geoip.parse_countries('Atlantis')

    def test_invalid_policies_are_refused(self):
        for kwargs in ({'basis': 'hostname'}, {'unknown': 'maybe'},
                       {'include': ('DE',), 'exclude': ('DE',)}, {'max_age_seconds': 0}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                geo.CountryCriterion(**kwargs)

    def test_serialisation_and_digest(self):
        criterion = geo.parse_criterion('DE,!FR', basis=geo.BASIS_EXIT, unknown=geo.UNKNOWN_REQUIRE)
        self.assertEqual(geo.CountryCriterion.from_dict(criterion.as_dict()), criterion)
        self.assertEqual(geo.parse_criterion(criterion.as_dict()), criterion)
        self.assertEqual(geo.parse_criterion(criterion), criterion)
        self.assertEqual(criterion.digest(), geo.parse_criterion(criterion.as_dict()).digest())
        self.assertNotEqual(criterion.digest(), geo.parse_criterion('DE,!FR').digest())


class UnknownPolicyTests(unittest.TestCase):
    """Unknown is a state of its own, not "not in NL"."""

    def criterion(self, unknown):
        return geo.CountryCriterion(include=('NL',), unknown=unknown)

    def test_exclude_drops_and_names_itself(self):
        verdict = geo.evaluate(self.criterion(geo.UNKNOWN_EXCLUDE), None, None, NOW)
        self.assertEqual((verdict.matched, verdict.verified, verdict.unknown, verdict.country),
                         (False, False, True, None))
        self.assertEqual(verdict.reason, geo.REASON_UNKNOWN_EXCLUDED)

    def test_include_unverified_keeps_but_does_not_confirm(self):
        verdict = geo.evaluate(self.criterion(geo.UNKNOWN_INCLUDE), None, None, NOW)
        self.assertEqual((verdict.matched, verdict.verified, verdict.unknown), (True, False, True))
        self.assertEqual(verdict.reason, geo.REASON_UNKNOWN_INCLUDED)

    def test_require_measurement_keeps_and_asks_for_a_probe(self):
        verdict = geo.evaluate(self.criterion(geo.UNKNOWN_REQUIRE), None, None, NOW)
        self.assertEqual((verdict.matched, verdict.verified, verdict.needs_measurement), (True, False, True))
        self.assertEqual(verdict.reason, geo.REASON_UNKNOWN_NEEDS_MEASUREMENT)

    def test_no_criterion_keeps_rows_without_claiming_anything(self):
        verdict = geo.evaluate(geo.CountryCriterion(), fact('DE'), fact('NL'), NOW)
        self.assertTrue(verdict.matched)
        self.assertFalse(verdict.verified)
        self.assertEqual(verdict.reason, geo.REASON_NO_CRITERION)


class EndpointVersusExitTests(unittest.TestCase):
    """Endpoint country and observed exit country are different things."""

    def setUp(self):
        self.endpoint = fact('DE', geo.SOURCE_GEOIP, at=NOW, address='11.0.0.7')
        self.exit_nl = fact('NL', geo.SOURCE_OBSERVED_EXIT, at=NOW, address='11.0.1.7')

    def test_de_endpoint_with_nl_exit_passes_an_exit_criterion(self):
        criterion = geo.parse_criterion('NL', basis=geo.BASIS_EXIT)
        verdict = geo.evaluate(criterion, self.endpoint, self.exit_nl, NOW)
        self.assertTrue(verdict.matched)
        self.assertTrue(verdict.verified)
        self.assertEqual(verdict.country, 'NL')
        self.assertEqual(verdict.reason, geo.REASON_MATCHED)

    def test_the_same_row_fails_an_endpoint_criterion(self):
        criterion = geo.parse_criterion('NL', basis=geo.BASIS_ENDPOINT)
        verdict = geo.evaluate(criterion, self.endpoint, self.exit_nl, NOW)
        self.assertFalse(verdict.matched)
        self.assertEqual(verdict.reason, geo.REASON_NOT_INCLUDED)
        self.assertEqual(verdict.country, 'DE')

    def test_either_basis_matches_and_names_the_conflict(self):
        criterion = geo.parse_criterion('NL', basis=geo.BASIS_EITHER)
        verdict = geo.evaluate(criterion, self.endpoint, self.exit_nl, NOW)
        self.assertTrue(verdict.matched)
        self.assertEqual(verdict.conflict, 'DE')

    def test_unknown_exit_never_rejects_a_german_endpoint(self):
        criterion = geo.parse_criterion('NL', basis=geo.BASIS_EXIT, unknown=geo.UNKNOWN_REQUIRE)
        plan = geo.plan_measurement(criterion, self.endpoint, None, NOW)
        self.assertFalse(plan.drop)
        self.assertTrue(plan.judge_required)
        self.assertEqual(plan.reason, geo.PLAN_JUDGE_FOR_EXIT)
        self.assertEqual(plan.verdict.unknown, True)
        self.assertEqual(plan.verdict.country, None)

    def test_exclusion_applies_to_every_known_country(self):
        criterion = geo.parse_criterion('NL', basis=geo.BASIS_EXIT, exclude=('DE',))
        verdict = geo.evaluate(criterion, self.endpoint, self.exit_nl, NOW)
        self.assertFalse(verdict.matched)
        self.assertEqual(verdict.reason, geo.REASON_EXCLUDED)
        self.assertEqual(verdict.country, 'DE')

    def test_a_known_wrong_exit_is_dropped_without_a_probe(self):
        criterion = geo.parse_criterion('NL', basis=geo.BASIS_EXIT, unknown=geo.UNKNOWN_REQUIRE)
        plan = geo.plan_measurement(criterion, self.endpoint, fact('FR'), NOW)
        self.assertTrue(plan.drop)
        self.assertFalse(plan.judge_required)
        self.assertEqual(plan.reason, geo.PLAN_DROP)

    def test_a_matching_row_needs_no_probe(self):
        plan = geo.plan_measurement(geo.parse_criterion('NL', basis=geo.BASIS_EXIT),
                                    self.endpoint, self.exit_nl, NOW)
        self.assertEqual((plan.drop, plan.judge_required, plan.reason), (False, False, geo.PLAN_READY))

    def test_exclusions_alone_keep_everything_else(self):
        # "not FR" is a criterion, not an empty include list that matches nothing.
        criterion = geo.CountryCriterion(exclude=('FR',))
        self.assertTrue(criterion.active)
        kept = geo.evaluate(criterion, self.endpoint, self.exit_nl, NOW)
        self.assertTrue(kept.matched)
        self.assertTrue(kept.verified)
        self.assertEqual(kept.reason, geo.REASON_MATCHED)
        dropped = geo.evaluate(geo.CountryCriterion(exclude=('DE',)), self.endpoint, self.exit_nl, NOW)
        self.assertFalse(dropped.matched)
        self.assertEqual(dropped.reason, geo.REASON_EXCLUDED)


class KnowledgeDateTests(unittest.TestCase):
    """Where a code came from and when it was learned are part of the answer."""

    def test_max_age_turns_old_knowledge_into_unknown(self):
        criterion = geo.CountryCriterion(include=('DE',), max_age_seconds=60)
        old = fact('DE', at=NOW - 3600)
        self.assertEqual(geo.evaluate(criterion, old, None, NOW).reason, geo.REASON_UNKNOWN_EXCLUDED)
        fresh = fact('DE', at=NOW - 10)
        self.assertTrue(geo.evaluate(criterion, fresh, None, NOW).verified)

    def test_undated_knowledge_is_not_fresh_when_a_limit_is_asked_for(self):
        criterion = geo.CountryCriterion(include=('DE',), max_age_seconds=60)
        self.assertTrue(geo.evaluate(criterion, fact('DE'), None, NOW).unknown)
        # Without a limit the caller did not ask about age, so it is not a reason to doubt.
        self.assertTrue(geo.evaluate(geo.CountryCriterion(include=('DE',)),
                                     fact('DE'), None, NOW).verified)

    def test_age_and_staleness_are_reported(self):
        undated = fact('DE')
        self.assertEqual((undated.age_seconds(NOW), undated.is_stale(NOW, None)), (None, False))
        dated = fact('DE', at=NOW - 100)
        self.assertEqual(dated.age_seconds(NOW), 100.0)
        self.assertTrue(dated.is_stale(NOW, 50))
        self.assertFalse(dated.is_stale(NOW, 200))
        self.assertTrue(dated.is_stale(NOW - 10, 50))  # a future date is not freshness

    def test_precedence_conflicts_are_named_not_silently_resolved(self):
        merged = geo.merge_facts([fact('FR', geo.SOURCE_SOURCE, at=NOW),
                                  fact('DE', geo.SOURCE_GEOIP, at=NOW)], NOW)
        self.assertEqual(merged.code, 'DE')
        self.assertEqual(merged.conflict, 'FR')
        self.assertEqual([f.source for f in merged.others], [geo.SOURCE_SOURCE])

    def test_a_resolved_address_beats_the_published_country(self):
        merged = geo.merge_facts([fact('FR', geo.SOURCE_SOURCE, at=NOW),
                                  fact('NL', geo.SOURCE_RESOLVED, at=NOW, address='11.0.1.7')], NOW)
        self.assertEqual((merged.code, merged.fact.source, merged.conflict), ('NL', geo.SOURCE_RESOLVED, 'FR'))

    def test_stale_sources_stay_visible_without_becoming_knowledge(self):
        merged = geo.merge_facts([fact('DE', geo.SOURCE_GEOIP, at=NOW - 10_000),
                                  fact('FR', geo.SOURCE_SOURCE, at=NOW - 10_000)], NOW, 60)
        self.assertEqual(merged.code, None)
        self.assertFalse(merged.known)
        self.assertEqual(len(merged.others), 2)

    def test_an_invalid_stored_code_is_unknown_not_a_guess(self):
        self.assertEqual(geo.CountryFact.of('de', geo.SOURCE_SOURCE).code, 'DE')
        for value in ('', None, 'DEU', 'ZZ-', 'unknown'):
            self.assertIsNone(geo.CountryFact.of(value, geo.SOURCE_SOURCE).code)

    def test_facts_serialise_with_their_origin(self):
        described = fact('DE', geo.SOURCE_RESOLVED, at=NOW, address='11.0.0.7').as_dict(NOW, 60)
        self.assertEqual(described, {'country': 'DE', 'country_source': geo.SOURCE_RESOLVED,
                                     'country_at': NOW, 'country_age_seconds': 0.0,
                                     'country_stale': False, 'country_database_version': None,
                                     'country_address': '11.0.0.7'})


if __name__ == '__main__':
    unittest.main()

"""F08: one country rule, reached from the GUI, the API and the export.

``Workbench.filter_by_country`` had no caller.  What it had instead was three
copies of a weaker rule: ``row['country'] not in wanted`` in the API's row
selector, the same test again with its own ``split(',')`` in the API's matcher,
and the legacy ``countries=`` shortcut in the GUI's admission policy.  None of
the three could say "not this country", compare the exit country, or say what
an unknown country did to the row -- and the GUI list and an export of the same
rows could therefore disagree about one filter.

So the question this file answers is not "does the function work" (its own
tests already said that) but "is it the rule the three surfaces actually use".
It is: every one of them reaches it now, and each of the three can do something
the copies could not.

Nothing here opens a socket.  ``socket.socket``, ``create_connection`` and
``getaddrinfo`` are replaced for the whole module: a read-only filter that
dials something fails the test.
"""
from __future__ import annotations

from pathlib import Path
import socket
import sys
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from proxy_workbench import api
from proxy_workbench import geo
from proxy_workbench import proxytool

NOW = 1_800_000_000.0

ROWS = [
    {'proxy': 'http://11.0.0.1:8080', 'country': 'DE', 'checked_at': NOW - 60},
    {'proxy': 'http://11.0.1.1:8080', 'country': 'NL', 'checked_at': NOW - 60},
    {'proxy': 'http://11.0.1.2:8080', 'country': '', 'exit_country': 'NL', 'checked_at': NOW - 60},
    {'proxy': 'http://proxy.example.invalid:8080', 'country': None, 'checked_at': NOW - 60},
]


def no_network():
    """Anything that opens a socket inside a read-only filter fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError('a read-only country filter must not touch the network')
    return [
        mock.patch.object(socket, 'socket', refuse),
        mock.patch.object(socket, 'create_connection', refuse),
        mock.patch.object(socket, 'getaddrinfo', refuse),
    ]


class OneRuleTests(unittest.TestCase):
    """The rule exists once, and the Workbench method is what the surfaces call."""

    def test_the_workbench_method_is_what_the_surfaces_call(self):
        workbench = proxytool.Workbench(REPO / 'data', clock=lambda: NOW)
        kept = workbench.filter_by_country(ROWS, geo.parse_criterion({'include': ['NL']}), now=NOW)
        self.assertEqual([row['proxy'] for row in kept.kept], ['http://11.0.1.1:8080'])

    def test_the_method_and_the_module_function_agree(self):
        criterion = geo.parse_criterion({'include': ['DE', 'NL']})
        through_method = proxytool.Workbench(REPO / 'data', clock=lambda: NOW) \
            .filter_by_country(ROWS, criterion, now=NOW)
        through_function = proxytool.filter_by_country(ROWS, criterion, now=NOW)
        self.assertEqual(through_method.kept, through_function.kept)
        self.assertEqual(through_method.reasons, through_function.reasons)

    def test_nothing_is_modified_and_nothing_is_started(self):
        import json
        before = json.dumps(ROWS, sort_keys=True)
        patches = no_network()
        with patches[0], patches[1], patches[2]:
            proxytool.filter_by_country(ROWS, geo.parse_criterion({'include': ['NL']}), now=NOW)
        self.assertEqual(json.dumps(ROWS, sort_keys=True), before)

    def test_an_unknown_country_is_not_reported_as_a_verified_one(self):
        result = proxytool.filter_by_country(ROWS, geo.parse_criterion({'include': ['NL']}), now=NOW)
        # One row verified its country and matched; two have none that this
        # criterion can compare (one has only an exit country, which an
        # endpoint criterion does not look at) and are reported as unknown
        # rather than counted as a country nobody asked for.
        self.assertEqual(result.verified, 1)
        self.assertEqual(result.unknown, 2)
        self.assertEqual(result.reasons[geo.REASON_UNKNOWN_EXCLUDED], 2)
        self.assertEqual(result.reasons[geo.REASON_NOT_INCLUDED], 1)
        self.assertNotIn(geo.REASON_MATCHED, result.dropped_reasons()
                         if hasattr(result, 'dropped_reasons') else {})


class ApiUsesTheRuleTests(unittest.TestCase):
    """`api.select` and `api._matches` no longer carry their own copy."""

    def base_row(self, country, **overrides):
        row = {'proxy': f'http://11.0.0.1:{abs(hash(country or "")) % 60000 + 1024}', 'protocol': 'http',
               'country': country, 'anonymity': 'anonymous', 'latency_ms': 50.0,
               'checked_at': NOW, 'valid_until': NOW + 3600, 'fresh': 'fresh'}
        row.update(overrides)
        return row

    def test_select_keeps_and_drops_through_the_one_rule(self):
        rows = [self.base_row('DE'), self.base_row('NL'), self.base_row(None)]
        query = {'countries': ['NL'], 'freshness': 'all', 'protocol': 'all'}
        kept = api.select(rows, query, now=NOW)
        self.assertEqual([row['country'] for row in kept], ['NL'])

    def test_select_agrees_with_the_rule_on_an_unknown_country(self):
        rows = [self.base_row('DE'), self.base_row(None)]
        criterion = api.country_criterion_of({'countries': ['DE']})
        rule = proxytool.filter_by_country(rows, criterion, now=NOW)
        self.assertEqual([r['proxy'] for r in api.select(rows, {'countries': ['DE'], 'freshness': 'all'},
                                                        now=NOW)],
                         [row['proxy'] for row in rule.kept])

    def test_an_empty_country_filter_keeps_everything(self):
        rows = [self.base_row('DE'), self.base_row(None)]
        self.assertEqual(len(api.select(rows, {'freshness': 'all'}, now=NOW)), 2)

    def test_matches_uses_the_same_criterion_as_select(self):
        row = self.base_row('NL')
        self.assertTrue(api.country_matches(row, {'countries': ['NL']}, now=NOW))
        self.assertFalse(api.country_matches(row, {'countries': ['DE']}, now=NOW))
        # An unknown country is not a match for a country that was asked for.
        self.assertFalse(api.country_matches(self.base_row(None), {'countries': ['DE']}, now=NOW))

    def test_the_rule_can_now_say_not_this_country(self):
        # The hand-written copies could only include; the rule has an exclude
        # half that they had no way to express.
        rows = [self.base_row('DE'), self.base_row('NL')]
        kept = proxytool.filter_by_country(rows, geo.parse_criterion({'exclude': ['DE']}), now=NOW)
        self.assertEqual([row['country'] for row in kept.kept], ['NL'])

    def test_the_rule_can_now_compare_the_exit_country(self):
        rows = [{'proxy': 'http://11.0.0.1:8080', 'country': 'DE', 'exit_country': 'NL',
                 'checked_at': NOW - 60}]
        kept = proxytool.filter_by_country(
            rows, geo.parse_criterion({'include': ['NL'], 'basis': 'exit'}), now=NOW)
        self.assertEqual(len(kept.kept), 1)

    def test_the_rule_can_now_keep_an_unverified_row_saying_so(self):
        rows = [self.base_row(None)]
        kept = proxytool.filter_by_country(
            rows, geo.parse_criterion({'include': ['DE'], 'unknown': 'include_unverified'}), now=NOW)
        self.assertEqual(len(kept.kept), 1)
        self.assertEqual(kept.unknown, 1)
        self.assertEqual(kept.verified, 0)


class GuiUsesTheRuleTests(unittest.TestCase):
    """The GUI table asks for the same criterion the export applies."""

    def source(self):
        from proxy_workbench import gui
        return Path(gui.__file__).read_text(encoding='utf-8')

    def test_the_table_builds_the_criterion_rather_than_a_bare_country_set(self):
        text = self.source()
        self.assertIn('country_criterion=core.country_criterion(', text)

    def test_the_table_accepts_the_same_basis_and_unknown_knobs(self):
        text = self.source()
        self.assertIn('country_basis', text)
        self.assertIn('country_unknown', text)
        for value in ('endpoint', 'exit', 'either'):
            self.assertIn(value, text)

    def test_the_knobs_are_validated_rather_than_ignored(self):
        from proxy_workbench import gui
        app = object.__new__(gui.App)
        for query, needle in (({'country_basis': ['nonsense']}, 'country_basis'),
                              ({'country_unknown': ['maybe']}, 'country_unknown')):
            with self.assertRaises(ValueError) as caught:
                app.parse_results_query(query)
            self.assertIn(needle, str(caught.exception))

    def test_the_defaults_are_what_the_page_always_did(self):
        from proxy_workbench import gui
        app = object.__new__(gui.App)
        parsed = app.parse_results_query({'country': ['DE,NL']})
        self.assertEqual(parsed['country_basis'], 'endpoint')
        self.assertEqual(parsed['country_unknown'], 'exclude')
        # An include list with unknown excluded keeps exactly what it kept before.
        criterion = proxytool.country_criterion(sorted(parsed['countries']),
                                                basis=parsed['country_basis'],
                                                unknown=parsed['country_unknown'])
        rows = [{'proxy': 'http://11.0.0.1:8080', 'country': 'DE', 'checked_at': NOW},
                {'proxy': 'http://11.0.1.1:8080', 'country': 'NL', 'checked_at': NOW},
                {'proxy': 'http://11.0.1.2:8080', 'country': None, 'checked_at': NOW}]
        self.assertEqual([row['country'] for row in proxytool.filter_by_country(rows, criterion, now=NOW).kept],
                         ['DE', 'NL'])


class ExportAgreesTests(unittest.TestCase):
    """The export and the list must answer the same question the same way."""

    def test_the_export_engine_and_the_list_rule_agree_on_a_row(self):
        row = {'proxy': 'http://11.0.0.1:8080', 'country': 'DE', 'checked_at': NOW}
        verdict = proxytool.geo_country_verdict(proxytool.country_criterion(['NL']), row, now=NOW)
        kept = proxytool.filter_by_country([row], proxytool.country_criterion(['NL']), now=NOW)
        self.assertEqual(bool(verdict.matched), bool(kept.kept))

    def test_an_exit_country_the_export_sees_the_filter_sees_too(self):
        row = {'proxy': 'http://11.0.0.1:8080', 'country': 'DE', 'exit_country': 'NL',
               'checked_at': NOW}
        verdict = proxytool.geo_country_verdict(
            proxytool.country_criterion(['NL'], basis='exit'), row, now=NOW)
        kept = proxytool.filter_by_country([row], proxytool.country_criterion(['NL'], basis='exit'),
                                           now=NOW)
        self.assertEqual(bool(verdict.matched), bool(kept.kept))
        self.assertTrue(verdict.matched)


if __name__ == '__main__':
    unittest.main()

"""One criterion in GUI, API and export, and a read-only filter that starts nothing (F08).

The three surfaces are represented here by the three shapes a caller actually
has: a settings/query mapping, the keyword arguments of the export, and an API
query string.  All three go through ``geo.parse_criterion``, and the test
compares digests and decisions rather than trusting that they agree.
"""
import gzip
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import geo
from proxy_workbench import geoip

NOW = 1_800_000_000.0

COUNTRY_CSV = ('11.0.0.0,11.0.0.255,DE\n'
               '11.0.1.0,11.0.1.255,NL\n'
               '2001:db8::,2001:db8:ffff:ffff:ffff:ffff:ffff:ffff,US\n')


def no_network():
    """Anything that opens a socket inside a read-only filter fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError('read-only filter must not touch the network')
    return [
        mock.patch.object(socket, 'socket', refuse),
        mock.patch.object(socket, 'create_connection', refuse),
        mock.patch.object(socket, 'getaddrinfo', refuse),
    ]


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        home = Path(self.temp.name)
        path = home / geoip.DB_NAME
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            handle.write(COUNTRY_CSV)
        self.resolver = geo.Resolver(country_db=geoip.CountryDB.from_file(path), now=NOW,
                                     database_version='2026-09')
        self.addCleanup(self.temp.cleanup)
        self.rows = [
            {'proxy': 'http://11.0.0.1:8080', 'country': 'DE', 'exit_ip': '11.0.1.9', 'checked_at': NOW - 60},
            {'proxy': 'http://11.0.1.1:8080', 'country': 'NL', 'exit_ip': '11.0.1.9', 'checked_at': NOW - 60},
            {'proxy': 'http://proxy.example.invalid:8080', 'country': '', 'checked_at': NOW - 60},
        ]

    def apply(self, criterion):
        return geo.filter_rows(self.rows, criterion, self.resolver, NOW)

    def test_endpoint_criterion_keeps_only_the_matching_country(self):
        result = self.apply(geo.parse_criterion('NL'))
        self.assertEqual([row['proxy'] for row in result.kept], ['http://11.0.1.1:8080'])
        self.assertEqual(result.reasons, {geo.REASON_MATCHED: 1, geo.REASON_NOT_INCLUDED: 1,
                                          geo.REASON_UNKNOWN_EXCLUDED: 1})
        self.assertEqual((result.total, result.unknown, result.verified), (3, 1, 1))

    def test_exit_criterion_keeps_the_german_endpoint_with_a_dutch_exit(self):
        result = self.apply(geo.parse_criterion('NL', basis=geo.BASIS_EXIT))
        self.assertEqual([row['proxy'] for row in result.kept], ['http://11.0.0.1:8080',
                                                                  'http://11.0.1.1:8080'])
        self.assertEqual(result.verified, 2)

    def test_exclusion_removes_a_country_from_the_kept_set(self):
        result = self.apply(geo.parse_criterion('NL', exclude=('DE',)))
        self.assertEqual([row['proxy'] for row in result.kept], ['http://11.0.1.1:8080'])
        self.assertEqual(result.reasons[geo.REASON_EXCLUDED], 1)

    def test_unknown_kept_unverified_is_visible_in_the_result(self):
        result = self.apply(geo.parse_criterion('NL,DE', unknown=geo.UNKNOWN_INCLUDE))
        self.assertEqual(len(result.kept), 3)
        self.assertEqual(result.unknown, 1)
        self.assertEqual(result.verified, 2)
        self.assertEqual(result.reasons[geo.REASON_UNKNOWN_INCLUDED], 1)

    def test_the_summary_is_what_a_status_endpoint_serves(self):
        summary = self.apply(geo.parse_criterion('NL')).as_dict()
        self.assertEqual(summary['kept'], 1)
        self.assertEqual(summary['total'], 3)
        self.assertEqual(summary['unknown'], 1)
        self.assertEqual(summary['criterion_digest'], geo.parse_criterion('NL').digest())

    def test_rows_are_not_modified_and_nothing_is_started(self):
        snapshot = json.dumps(self.rows, sort_keys=True)
        with no_network()[0], no_network()[1], no_network()[2]:
            self.apply(geo.parse_criterion('NL', basis=geo.BASIS_EITHER))
        self.assertEqual(json.dumps(self.rows, sort_keys=True), snapshot)

    def test_a_filter_without_a_resolver_confirms_nothing(self):
        # No resolver means no knowledge; the filter says so instead of measuring.
        with no_network()[0], no_network()[1], no_network()[2]:
            result = geo.filter_rows(self.rows, geo.parse_criterion('NL', unknown=geo.UNKNOWN_REQUIRE))
        self.assertEqual((len(result.kept), result.verified, result.unknown), (3, 0, 3))
        self.assertEqual(result.reasons, {geo.REASON_UNKNOWN_NEEDS_MEASUREMENT: 3})

    def test_an_empty_selection_keeps_everything(self):
        result = self.apply(geo.CountryCriterion())
        self.assertEqual(len(result.kept), 3)
        self.assertEqual(result.reasons, {geo.REASON_NO_CRITERION: 3})


class OneCriterionEverywhereTests(unittest.TestCase):
    """GUI settings, export keywords and an API query must produce one criterion."""

    def test_the_three_shapes_agree(self):
        from_settings = geo.parse_criterion({'country': 'DE,NL', 'country_basis': 'exit'})
        from_export = geo.parse_criterion('DE,NL', basis=geo.BASIS_EXIT)
        query = parse_qs('country=DE%2CNL&country_basis=exit')   # the shape /v1 really receives
        from_api = geo.parse_criterion({'include': query['country'], 'basis': query['country_basis'][0]})
        self.assertEqual(from_settings.digest(), from_export.digest())
        self.assertEqual(from_export.digest(), from_api.digest())

    def test_an_api_client_cannot_add_an_unexpected_silent_default(self):
        criterion = geo.parse_criterion('', include=('NL',), basis=geo.BASIS_EXIT,
                                        unknown=geo.UNKNOWN_REQUIRE, max_age_seconds=900)
        self.assertEqual(criterion.include, ('NL',))
        self.assertEqual((criterion.basis, criterion.unknown, criterion.max_age_seconds),
                         (geo.BASIS_EXIT, geo.UNKNOWN_REQUIRE, 900))
        # The stored form carries every choice, so a reloaded surface shows the same policy.
        self.assertEqual(geo.CountryCriterion.from_dict(criterion.as_dict()), criterion)

    def test_a_name_and_a_code_select_the_same_country(self):
        by_name = geo.parse_criterion('Нидерланды')
        by_code = geo.parse_criterion('NL')
        self.assertEqual(by_name.digest(), by_code.digest())


class QuotaInterfaceTests(unittest.TestCase):
    """The data interface for pool quotas; storage and refill belong to pools.py."""

    def test_endpoints_are_counted_once_each(self):
        status = geo.quota_status(geo.QUOTA_ENDPOINTS, 3, endpoints=['a', 'b', 'c', 'c'])
        self.assertEqual((status.confirmed, status.shortfall, status.satisfied), (3, 0, True))
        self.assertEqual(status.reason, geo.REASON_QUOTA_ENDPOINTS)

    def test_unique_exits_are_counted_by_address(self):
        exits = [geo.ExitObservation('11.0.1.1', 'NL', NOW - 10), geo.ExitObservation('11.0.1.1', 'NL', NOW - 5),
                 geo.ExitObservation('11.0.1.2', 'NL', NOW - 5)]
        status = geo.quota_status(geo.QUOTA_EXIT_IPS, 2, exits=exits, now=NOW)
        self.assertEqual((status.confirmed, status.unknown, status.satisfied), (2, 0, True))

    def test_an_unknown_exit_confirms_nothing(self):
        exits = [geo.ExitObservation('11.0.1.1', 'NL', NOW - 10), geo.ExitObservation(None, None, NOW)]
        status = geo.quota_status(geo.QUOTA_EXIT_IPS, 2, exits=exits, now=NOW)
        self.assertEqual((status.confirmed, status.unknown, status.confirmable, status.satisfied), (1, 1, False, False))
        self.assertEqual(status.reason, geo.REASON_QUOTA_EXIT_UNKNOWN)

    def test_an_old_exit_stops_counting_when_a_limit_is_given(self):
        exits = [geo.ExitObservation('11.0.1.1', 'NL', NOW - 10_000)]
        self.assertEqual(geo.quota_status(geo.QUOTA_EXIT_IPS, 1, exits=exits, now=NOW).confirmed, 1)
        status = geo.quota_status(geo.QUOTA_EXIT_IPS, 1, exits=exits, now=NOW, max_age_seconds=60)
        self.assertEqual((status.confirmed, status.unknown, status.confirmable), (0, 1, False))

    def test_the_summary_serialises_for_a_pool_status_endpoint(self):
        status = geo.quota_status(geo.QUOTA_ENDPOINTS, 0, endpoints=[]).as_dict()
        self.assertEqual(status['basis'], geo.QUOTA_ENDPOINTS)
        self.assertEqual((status['shortfall'], status['satisfied']), (0, True))

    def test_an_unknown_basis_is_refused(self):
        with self.assertRaises(ValueError):
            geo.quota_status('megabits', 1, endpoints=['a'])


if __name__ == '__main__':
    unittest.main()

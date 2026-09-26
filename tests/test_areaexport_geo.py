"""F08: one country criterion, endpoint and exit kept apart, unknown kept unknown.

What the code did before this file existed, proven by running it: the export
engine (``proxytool.matches_selection``) and the geo filter disagreed about the
same rows.  A row carrying ``country='NL'`` whose address is not in the GeoIP
database was matched by the export engine and dropped by
``geo.filter_rows(rows, criterion, resolver)``, because ``geo.Resolver``
never read the row's own ``country``.  Two answers for one filter is the parity
break F08 requires, and it is the case a read-only API page and a GUI list hit
on every deployment without a GeoIP database.

These tests run both decisions side by side instead of trusting that they agree.
"""
import gzip
import ipaddress
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import geo, geoip
from proxy_workbench import proxytool as p

NOW = 1_800_000_000.0
COUNTRY_CSV = ('11.0.0.0,11.0.0.255,DE\n'
               '11.0.1.0,11.0.1.255,NL\n'
               '2001:db8::,2001:db8:ffff:ffff:ffff:ffff:ffff:ffff,US\n')


def no_network():
    def refuse(*args, **kwargs):
        raise AssertionError('a read-only filter must not touch the network')
    return [mock.patch.object(socket, 'socket', refuse),
            mock.patch.object(socket, 'create_connection', refuse),
            mock.patch.object(socket, 'getaddrinfo', refuse)]


class Fixture(unittest.TestCase):
    """Rows, a real on-disk GeoIP database and a real ASN index."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        path = home / geoip.DB_NAME
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            handle.write(COUNTRY_CSV)
        self.database = geoip.CountryDB.from_file(path)
        self.resolver = geo.Resolver(country_db=self.database, now=NOW, database_version='2026-09')
        self.empty = geo.Resolver(country_db=geoip.CountryDB([]), now=NOW)
        self.rows = [
            {'proxy': 'http://11.0.0.1:8080', 'country': 'DE', 'exit_ip': '11.0.1.9', 'checked_at': NOW - 60},
            {'proxy': 'http://11.0.1.1:8080', 'country': 'NL', 'exit_ip': '11.0.1.9', 'checked_at': NOW - 60},
            {'proxy': 'http://11.0.2.1:8080', 'country': '', 'checked_at': NOW - 60},
            {'proxy': 'http://proxy.example.invalid:8080', 'country': '', 'checked_at': NOW - 60},
        ]

    def both(self, criterion, rows=None, country_of=None):
        """The geo filter's answer next to the export engine's answer."""
        rows = self.rows if rows is None else rows
        mine = {row['proxy'] for row in
                geo.filter_rows(rows, criterion, self.resolver, NOW).kept}
        theirs = {row['proxy'] for row in rows
                  if p.matches_selection(row, countries=criterion.countries,
                                         country_of=country_of if country_of is not None
                                         else self.database.country_of)}
        return mine, theirs


class ParityWithTheExportEngineTests(Fixture):
    def test_the_two_surfaces_agree_on_every_country_list(self):
        for codes in ('DE', 'NL', 'DE,NL', 'US', 'FR'):
            with self.subTest(countries=codes):
                mine, theirs = self.both(geo.parse_criterion(codes))
                self.assertEqual(mine, theirs)

    def test_a_row_that_states_its_country_answers_even_with_no_database(self):
        # The defect: the export engine read row['country'], the resolver did
        # not, so one row matched in one surface and was dropped in the other.
        rows = [{'proxy': 'http://11.0.0.1:8080', 'country': 'DE', 'checked_at': NOW - 60},
                {'proxy': 'http://11.0.0.2:8080', 'country': 'NL', 'checked_at': NOW - 60},
                {'proxy': 'http://11.0.0.3:8080', 'checked_at': NOW - 60}]
        criterion = geo.parse_criterion('NL')
        mine = {row['proxy'] for row in geo.filter_rows(rows, criterion, self.empty, NOW).kept}
        theirs = {row['proxy'] for row in
                  rows if p.matches_selection(row, countries=criterion.countries, country_of=lambda _p: None)}
        self.assertEqual(mine, theirs)
        self.assertEqual(mine, {'http://11.0.0.2:8080'})

    def test_the_row_country_is_a_claim_named_as_such(self):
        endpoint, _exit, _provider = self.empty.from_row(
            {'proxy': 'http://11.0.0.2:8080', 'country': 'NL', 'checked_at': NOW - 60})
        self.assertEqual((endpoint.code, endpoint.source), ('NL', geo.SOURCE_SOURCE))
        # ...and it is dated by the measurement that carried it
        self.assertEqual(endpoint.at, NOW - 60)

    def test_the_database_outranks_the_claim_when_they_disagree(self):
        endpoint, _exit, _provider = self.resolver.from_row(
            {'proxy': 'http://11.0.0.1:8080', 'country': 'FR', 'checked_at': NOW - 60})
        self.assertEqual((endpoint.code, endpoint.source), ('DE', geo.SOURCE_GEOIP))

    def test_an_explicit_declared_map_supplies_what_the_database_cannot(self):
        # The map matters exactly where the database has no answer; where the
        # database has one, the database wins (SOURCE_PRECEDENCE).
        resolver = geo.Resolver(country_db=self.database, now=NOW,
                                declared={'http://198.51.100.7:8080': 'FR'})
        endpoint, _exit, _provider = resolver.from_row({'proxy': 'http://198.51.100.7:8080',
                                                        'country': 'DE', 'checked_at': NOW - 60})
        self.assertEqual((endpoint.code, endpoint.source), ('FR', geo.SOURCE_SOURCE))

    def test_a_filter_without_a_resolver_still_confirms_nothing(self):
        # The unchanged contract: no resolver means no knowledge, and the filter
        # says so instead of measuring.  Only the resolver path reads rows.
        with no_network()[0], no_network()[1], no_network()[2]:
            result = geo.filter_rows(self.rows, geo.parse_criterion('NL', unknown=geo.UNKNOWN_REQUIRE))
        self.assertEqual((len(result.kept), result.verified, result.unknown), (4, 0, 4))


class EndpointAndExitAreDifferentThingsTests(unittest.TestCase):
    def test_a_german_endpoint_is_not_dropped_for_a_wanted_dutch_exit(self):
        endpoint = geo.CountryFact.of('DE', geo.SOURCE_GEOIP, at=NOW, address='11.0.0.1')
        exit_nl = geo.CountryFact.of('NL', geo.SOURCE_OBSERVED_EXIT, at=NOW, address='81.2.3.4')
        criterion = geo.CountryCriterion(include=('NL',), basis=geo.BASIS_EXIT)
        verdict = geo.evaluate(criterion, endpoint, exit_nl, NOW)
        self.assertTrue(verdict.matched)
        self.assertTrue(verdict.verified)
        self.assertEqual(verdict.country, 'NL')
        self.assertEqual(verdict.basis, geo.BASIS_EXIT)

        plan = geo.plan_measurement(criterion, endpoint, exit_nl, NOW)
        self.assertFalse(plan.drop)
        self.assertFalse(plan.judge_required)

    def test_an_undetermined_exit_survives_as_something_to_measure(self):
        endpoint = geo.CountryFact.of('DE', geo.SOURCE_GEOIP, at=NOW, address='11.0.0.1')
        criterion = geo.CountryCriterion(include=('NL',), basis=geo.BASIS_EXIT)
        plan = geo.plan_measurement(criterion, endpoint, None, NOW)
        self.assertFalse(plan.drop)
        self.assertTrue(plan.judge_required)
        self.assertTrue(plan.suggest_job)
        self.assertEqual(plan.reason, geo.PLAN_JUDGE_FOR_EXIT)

    def test_the_three_bases_read_the_same_row_three_different_ways(self):
        endpoint = geo.CountryFact.of('DE', geo.SOURCE_GEOIP, at=NOW, address='11.0.0.1')
        exit_nl = geo.CountryFact.of('NL', geo.SOURCE_OBSERVED_EXIT, at=NOW, address='81.2.3.4')
        answers = {basis: geo.evaluate(geo.CountryCriterion(include=('NL',), basis=basis),
                                       endpoint, exit_nl, NOW).matched
                   for basis in geo.BASES}
        self.assertEqual(answers, {geo.BASIS_ENDPOINT: False, geo.BASIS_EXIT: True, geo.BASIS_EITHER: True})
        # no exit observed at all: only the endpoint-based criteria can answer
        answers = {basis: geo.evaluate(geo.CountryCriterion(include=('NL',), basis=basis),
                                       endpoint, None, NOW).matched
                   for basis in geo.BASES}
        self.assertEqual(answers, {geo.BASIS_ENDPOINT: False, geo.BASIS_EXIT: False, geo.BASIS_EITHER: False})

    def test_exclusion_applies_to_every_country_it_knows_including_the_exit(self):
        endpoint = geo.CountryFact.of('DE', geo.SOURCE_GEOIP, at=NOW, address='11.0.0.1')
        exit_nl = geo.CountryFact.of('NL', geo.SOURCE_OBSERVED_EXIT, at=NOW, address='81.2.3.4')
        criterion = geo.CountryCriterion(include=('DE',), exclude=('NL',), basis=geo.BASIS_EITHER)
        # the endpoint is in the wanted list and the row is still refused: the
        # exit it carries is on the "never here" list
        verdict = geo.evaluate(criterion, endpoint, exit_nl, NOW)
        self.assertFalse(verdict.matched)
        self.assertEqual(verdict.reason, geo.REASON_EXCLUDED)
        self.assertTrue(geo.evaluate(geo.CountryCriterion(include=('DE', 'NL'), basis=geo.BASIS_EITHER),
                                    endpoint, exit_nl, NOW).matched)

    def test_a_country_cannot_be_wanted_and_forbidden_at_once(self):
        with self.assertRaises(ValueError):
            geo.CountryCriterion(include=('NL',), exclude=('NL',))


class UnknownIsNeverVerifiedTests(unittest.TestCase):
    UNKNOWN = geo.CountryFact.unknown(geo.SOURCE_UNRESOLVED, address='proxy.example.net')

    def verdict(self, policy):
        criterion = geo.CountryCriterion(include=('NL',), basis=geo.BASIS_ENDPOINT, unknown=policy)
        return geo.evaluate(criterion, self.UNKNOWN, None, NOW)

    def test_every_policy_is_a_value_and_none_of_them_claims_a_measurement(self):
        expected = {geo.UNKNOWN_EXCLUDE: (False, geo.REASON_UNKNOWN_EXCLUDED),
                    geo.UNKNOWN_INCLUDE: (True, geo.REASON_UNKNOWN_INCLUDED),
                    geo.UNKNOWN_REQUIRE: (True, geo.REASON_UNKNOWN_NEEDS_MEASUREMENT)}
        for policy, (matched, reason) in expected.items():
            with self.subTest(policy=policy):
                verdict = self.verdict(policy)
                self.assertEqual((verdict.matched, verdict.reason), (matched, reason))
                self.assertTrue(verdict.unknown)
                self.assertFalse(verdict.verified)
                self.assertIsNone(verdict.country)

    def test_a_kept_unknown_row_is_still_marked_as_needing_measurement(self):
        plan = geo.plan_measurement(
            geo.CountryCriterion(include=('NL',), unknown=geo.UNKNOWN_REQUIRE), self.UNKNOWN, None, NOW)
        self.assertTrue(plan.judge_required)
        self.assertTrue(plan.suggest_job)
        self.assertEqual(plan.reason, geo.PLAN_JUDGE_FOR_UNKNOWN)

    def test_knowledge_older_than_the_limit_is_not_used(self):
        stale = geo.CountryFact.of('NL', geo.SOURCE_GEOIP, at=NOW - 4000, address='11.0.1.1')
        criterion = geo.CountryCriterion(include=('NL',), max_age_seconds=600)
        self.assertFalse(geo.evaluate(criterion, stale, None, NOW).matched)
        self.assertTrue(geo.evaluate(criterion, stale, None, NOW).unknown)

    def test_a_hostname_without_a_recorded_resolution_has_no_geography(self):
        resolver = geo.Resolver(country_db=geoip.CountryDB([]), now=NOW)
        fact = resolver.endpoint_fact('http://proxy.example.net:8080')
        self.assertIsNone(fact.code)
        self.assertEqual(fact.source, geo.SOURCE_UNRESOLVED)
        self.assertEqual(fact.address, 'proxy.example.net')
        # the same hostname with the address that was really used, and when:
        # the database still cannot classify that address, so the country stays
        # unknown - but the address and the moment it was used are recorded.
        resolved = resolver.endpoint_fact('http://proxy.example.net:8080',
                                          resolved_ip='198.51.100.7', resolved_at=NOW - 30)
        self.assertIsNone(resolved.code)
        self.assertEqual((resolved.source, resolved.at, resolved.address),
                         (geo.SOURCE_UNRESOLVED, NOW - 30, '198.51.100.7'))
        # with a database that knows the address, the resolved code is the fact
        known = geo.Resolver(country_db=geoip.CountryDB(
            [(ipaddress.ip_address('11.0.1.0'), ipaddress.ip_address('11.0.1.255'), 'NL')]), now=NOW)
        self.assertEqual(known.endpoint_fact('http://proxy.example.net:8080',
                                             resolved_ip='11.0.1.1', resolved_at=NOW - 30).code, 'NL')

    def test_the_three_surfaces_build_one_criterion(self):
        forms = (geo.parse_criterion('de,NL,!FR'),
                 geo.parse_criterion({'include': ['DE', 'NL'], 'exclude': ['FR']}),
                 geo.parse_criterion({'country': 'DE,NL', 'country_exclude': 'FR'}),
                 geo.CountryCriterion(include=('NL', 'DE'), exclude=('FR',)))
        self.assertEqual(len({form.digest() for form in forms}), 1)
        self.assertEqual(forms[0].include, ('DE', 'NL'))
        self.assertEqual(forms[0].exclude, ('FR',))


class HostingIsAHeuristicTests(unittest.TestCase):
    def test_no_organisation_means_no_claim_at_all(self):
        claims = geo.ProviderFact.of(None, asn=64500, address='11.0.0.1').claims()
        self.assertIsNone(claims['hosting'])
        self.assertEqual(claims['hosting_basis'], geo.HOSTING_BASIS_NONE)

    def test_a_name_match_never_becomes_a_residential_or_mobile_claim(self):
        for organisation in ('Amazon.com, Inc.', 'Hetzner Online GmbH', 'Acme Broadband'):
            with self.subTest(organisation=organisation):
                claims = geo.ProviderFact.of(organisation, asn=64500,
                                             address='11.0.0.1').claims()
                self.assertIsNone(claims['residential'])
                self.assertIsNone(claims['mobile'])
                self.assertFalse(claims['verified'])
                self.assertEqual(claims['hosting_basis'], geo.HOSTING_BASIS_ORG_NAME)
                self.assertEqual(claims['note'], geo.HOSTING_CLAIM_NOT_RESIDENTIAL)

    def test_the_serialized_fact_carries_the_basis_next_to_the_flag(self):
        fact = geo.ProviderFact.of('Acme Hosting', asn=64500, address='11.0.0.1', cidr='11.0.0.0/16')
        payload = fact.as_dict()
        self.assertEqual(payload['hosting_basis'], geo.HOSTING_BASIS_ORG_NAME)
        self.assertEqual(payload['cidr'], '11.0.0.0/16')
        self.assertNotIn('residential', payload)
        self.assertNotIn('mobile', payload)

    def test_no_serialized_surface_offers_a_residential_or_mobile_value(self):
        # ``claims()`` names the two fields a consumer may want and gives them
        # None, so a surface that renders it has nothing to show; the shapes a
        # table or a status endpoint serves do not carry them at all.
        claims = geo.ProviderFact.of('Amazon', asn=1, address='11.0.0.1').claims()
        self.assertIsNone(claims['residential'])
        self.assertIsNone(claims['mobile'])
        for payload in (geo.ProviderFact.of('Amazon', asn=1, address='11.0.0.1').as_dict(),
                        geo.ProviderIndex([(ipaddress.ip_address('11.0.0.0'),
                                            ipaddress.ip_address('11.0.0.255'), 1, 'Amazon')]
                                          ).provider_of('http://11.0.0.1:8080').as_dict()):
            self.assertNotIn('residential', payload)
            self.assertNotIn('mobile', payload)


class DatabaseAndEndpointFactsTests(unittest.TestCase):
    def test_the_database_status_names_its_version_and_its_absence(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / 'country-lite.csv'
            absent = geo.database_status(path, now=NOW)
            self.assertFalse(absent.as_dict()['available'])
            self.assertEqual(absent.version_label(), 'absent')
            geo.install_database(path, b'11.0.0.0,11.0.0.255,DE\n', geo.COUNTRY_DATABASE,
                                 '2026-09-01', lambda body: None, at=NOW)
            current = geo.database_status(path, now=NOW).as_dict()
            self.assertEqual((current['available'], current['version'], current['stale']), (True, '2026-09-01', False))
            self.assertTrue(geo.database_status(path, now=NOW + geo.DEFAULT_DATABASE_MAX_AGE + 1).as_dict()['stale'])

    def test_a_failed_update_keeps_the_working_database_and_its_version(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / 'country-lite.csv'
            geo.install_database(path, b'11.0.0.0,11.0.0.255,DE\n', geo.COUNTRY_DATABASE,
                                 '2026-09-01', lambda body: None, at=NOW)

            def reject(body):
                raise ValueError('truncated file')

            with self.assertRaises(ValueError):
                geo.install_database(path, b'broken', geo.COUNTRY_DATABASE, '2026-10-01', reject, at=NOW)
            self.assertEqual(path.read_bytes(), b'11.0.0.0,11.0.0.255,DE\n')
            self.assertEqual(geo.database_status(path, now=NOW).version, '2026-09-01')

    def test_both_families_are_recognised_and_a_lookalike_is_not(self):
        self.assertEqual(geo.endpoint_ip_version('http://11.0.0.1:8080'), 4)
        self.assertEqual(geo.endpoint_ip_version('socks5://[2001:4860:4860::8888]:1080'), 6)
        self.assertIsNone(geo.endpoint_ip_version('http://proxy.example.net:8080'))
        self.assertTrue(geo.classify_host('http://proxy.example.net:8080').is_hostname)

    def test_a_target_capability_is_none_until_it_was_measured(self):
        self.assertEqual(geo.TargetCapabilities.from_samples([]).as_dict(),
                         {'ipv4_destination': None, 'ipv6_destination': None, 'at': None,
                          'source': 'unmeasured'})
        measured = geo.TargetCapabilities.from_samples([{'ip_version': 4, 'ok': True}], at=NOW)
        self.assertEqual(measured.as_dict(),
                         {'ipv4_destination': True, 'ipv6_destination': None, 'at': NOW, 'source': 'measured'})

    def test_an_unknown_exit_cannot_confirm_a_quota(self):
        exits = [geo.ExitObservation('11.0.1.1', 'NL', NOW - 10), geo.ExitObservation(None, None, NOW)]
        status = geo.quota_status(geo.QUOTA_EXIT_IPS, 2, exits=exits, now=NOW, max_age_seconds=600)
        self.assertEqual((status.confirmed, status.unknown, status.confirmable, status.satisfied),
                         (1, 1, False, False))
        self.assertEqual(status.reason, geo.REASON_QUOTA_EXIT_UNKNOWN)


if __name__ == '__main__':
    unittest.main()

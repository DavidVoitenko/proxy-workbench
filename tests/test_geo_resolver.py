"""Geographical knowledge and endpoint characteristics, from local files only (F08).

Every case runs against synthetic CSV databases written to a temporary folder -
no proxy list, no public GeoIP service, no DNS.  What is checked here is the
meaning of the values: a hostname without a recorded resolution has no country,
hosting is a heuristic with its basis attached, and an IPv6 endpoint says
nothing about which destination families it can reach.
"""
import gzip
import ipaddress
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import geo
from proxy_workbench import geoip

NOW = 1_800_000_000.0

# 11.0.0.0/24 DE, 11.0.1.0/24 NL, 2001:db8::/32 US; 11.0.2.0/24 is ZZ and dropped.
COUNTRY_CSV = ('11.0.0.0,11.0.0.255,DE\n'
               '11.0.1.0,11.0.1.255,NL\n'
               '11.0.2.0,11.0.2.255,ZZ\n'
               '2001:db8::,2001:db8:ffff:ffff:ffff:ffff:ffff:ffff,US\n')

ASN_CSV = ('11.0.0.0,11.0.0.255,64500,Example Home Broadband\n'
           '11.0.1.0,11.0.1.255,64501,Hetzner Online GmbH\n'
           '2001:db8::,2001:db8:ffff:ffff:ffff:ffff:ffff:ffff,64502,"Cloud Servers, Inc."\n')


def row(proxy, **extra):
    base = {'proxy': proxy, 'checked_at': NOW - 60, 'country': '', 'exit_ip': '', 'exit_country': ''}
    base.update(extra)
    return base


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.country = self._database(geoip.DB_NAME, COUNTRY_CSV, geoip.CountryDB)
        self.providers = self._database(geoip.ASN_NAME, ASN_CSV, geoip.AsnDB)
        self.resolver = geo.Resolver(country_db=self.country, provider_db=self.providers, now=NOW,
                                     database_version='2026-09')

    def tearDown(self):
        self.temp.cleanup()

    def _database(self, name, body, loader):
        path = self.home / name
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            handle.write(body)
        return loader.from_file(path)

    def test_ip_literal_endpoints_get_their_country_from_the_database(self):
        fact = self.resolver.endpoint_fact('http://11.0.0.7:8080')
        self.assertEqual((fact.code, fact.source, fact.address), ('DE', geo.SOURCE_GEOIP, '11.0.0.7'))
        self.assertEqual(fact.at, NOW)
        self.assertEqual(fact.database_version, '2026-09')
        self.assertEqual(self.resolver.endpoint_fact('socks5://[2001:db8::5]:1080').code, 'US')

    def test_a_hostname_without_a_recorded_resolution_is_unknown(self):
        fact = self.resolver.endpoint_fact('http://proxy.example.invalid:8080')
        self.assertIsNone(fact.code)
        self.assertFalse(fact.known)
        self.assertEqual(fact.source, geo.SOURCE_UNRESOLVED)
        self.assertEqual(fact.address, 'proxy.example.invalid')

    def test_a_hostname_uses_the_address_that_was_really_used_and_when(self):
        fact = self.resolver.endpoint_fact('http://proxy.example.invalid:8080',
                                           resolved_ip='11.0.1.7', resolved_at=NOW - 30)
        self.assertEqual((fact.code, fact.source, fact.address, fact.at), ('NL', geo.SOURCE_RESOLVED,
                                                                          '11.0.1.7', NOW - 30))

    def test_a_declared_country_is_a_claim_and_can_conflict(self):
        resolver = geo.Resolver(country_db=self.country, now=NOW, declared={'http://11.0.0.7:8080': 'FR'})
        merged = geo.merge_facts([resolver.endpoint_fact('http://11.0.0.7:8080'),
                                  geo.CountryFact.of('FR', geo.SOURCE_SOURCE, at=NOW)], NOW)
        self.assertEqual(merged.code, 'DE')
        self.assertEqual(merged.conflict, 'FR')

    def test_a_claim_survives_a_database_that_knows_nothing(self):
        resolver = geo.Resolver(now=NOW, declared={'http://198.51.100.7:8080': 'FR'})
        fact = resolver.endpoint_fact('http://198.51.100.7:8080')
        self.assertEqual((fact.code, fact.source), ('FR', geo.SOURCE_SOURCE))

    def test_a_claim_about_a_hostname_is_kept_but_cannot_be_called_fresh(self):
        declared = {'http://proxy.example.invalid:8080': 'FR'}
        undated = geo.Resolver(now=NOW, declared=declared).endpoint_fact('http://proxy.example.invalid:8080')
        self.assertEqual((undated.code, undated.source, undated.at), ('FR', geo.SOURCE_SOURCE, None))
        # Without a limit the claim is what the list says; with one it is not knowledge yet.
        self.assertTrue(geo.evaluate(geo.CountryCriterion(include=('FR',)), undated, None, NOW).verified)
        limited = geo.Resolver(now=NOW, declared=declared, max_age_seconds=3600)
        verdict = geo.evaluate(geo.CountryCriterion(include=('FR',), max_age_seconds=3600),
                               limited.endpoint_fact('http://proxy.example.invalid:8080'), None, NOW)
        self.assertTrue(verdict.unknown)
        self.assertEqual(verdict.reason, geo.REASON_UNKNOWN_EXCLUDED)

    def test_exit_country_comes_from_the_address_the_judge_saw(self):
        endpoint, exit_fact, _provider = self.resolver.from_row(
            row('http://11.0.0.7:8080', exit_ip='11.0.1.9', country='DE'))
        self.assertEqual(endpoint.code, 'DE')
        self.assertEqual((exit_fact.code, exit_fact.source, exit_fact.at),
                         ('NL', geo.SOURCE_OBSERVED_EXIT, NOW - 60))
        self.assertNotEqual(exit_fact.code, endpoint.code)

    def test_exit_ip_inside_the_anonymity_block_is_used_too(self):
        _endpoint, exit_fact, _provider = self.resolver.from_row(
            row('http://11.0.0.7:8080', anonymity={'level': 'elite', 'exit_ip': '11.0.1.9'}))
        self.assertEqual(exit_fact.code, 'NL')

    def test_without_an_exit_ip_there_is_no_exit_country(self):
        _endpoint, exit_fact, _provider = self.resolver.from_row(row('http://11.0.0.7:8080', country='DE'))
        self.assertIsNone(exit_fact)

    def test_ipv4_and_ipv6_endpoints_are_told_apart(self):
        self.assertEqual(geo.endpoint_ip_version('http://11.0.0.7:8080'), 4)
        self.assertEqual(geo.endpoint_ip_version('socks5://[2001:db8::5]:1080'), 6)
        self.assertIsNone(geo.endpoint_ip_version('http://proxy.example.invalid:8080'))
        self.assertEqual(geo.endpoint_ip_version(''), None)
        self.assertEqual(geo.classify_host('[2001:db8::5]:1080').address, '2001:db8::5')

    def test_provider_characteristics_with_asn_organisation_and_range(self):
        fact = self.resolver.provider_fact('http://11.0.1.7:8080')
        self.assertEqual((fact.asn, fact.organization, fact.hosting), (64501, 'Hetzner Online GmbH', True))
        self.assertEqual(fact.hosting_basis, geo.HOSTING_BASIS_ORG_NAME)
        self.assertEqual(fact.ip_version, 4)
        self.assertIsNone(fact.cidr)          # AsnDB reports no prefix; see test_index_carries_cidr
        home = self.resolver.provider_fact('http://11.0.0.7:8080')
        self.assertEqual((home.asn, home.hosting), (64500, False))

    def test_an_unknown_address_reports_no_provider(self):
        fact = self.resolver.provider_fact('http://198.51.100.7:8080')
        self.assertEqual((fact.asn, fact.organization, fact.hosting), (None, None, None))
        self.assertEqual(fact.hosting_basis, geo.HOSTING_BASIS_NONE)

    def test_hosting_is_a_heuristic_and_residential_stays_unmeasured(self):
        claims = self.resolver.provider_fact('http://11.0.1.7:8080').claims()
        self.assertIs(claims['hosting'], True)
        self.assertEqual(claims['hosting_basis'], geo.HOSTING_BASIS_ORG_NAME)
        self.assertIs(claims['verified'], False)
        self.assertIsNone(claims['residential'])
        self.assertIsNone(claims['mobile'])
        self.assertEqual(claims['note'], geo.HOSTING_CLAIM_NOT_RESIDENTIAL)

    def test_without_a_provider_database_nothing_is_claimed(self):
        fact = geo.Resolver(now=NOW).provider_fact('http://11.0.1.7:8080')
        self.assertIsNone(fact.hosting)
        self.assertEqual(fact.hosting_basis, geo.HOSTING_BASIS_NONE)


class ProviderIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _index(self, body, version='2026-09'):
        path = self.home / geoip.ASN_NAME
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            handle.write(body)
        return geo.ProviderIndex.from_file(path, database_version=version)

    def test_index_agrees_with_the_existing_database_and_adds_the_range(self):
        path = self.home / geoip.ASN_NAME
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            handle.write(ASN_CSV)
        index = geo.ProviderIndex.from_file(path, database_version='2026-09')
        for address in ('11.0.0.7', '11.0.1.7', '2001:db8::5'):
            with self.subTest(address=address):
                mine = index.lookup(address)
                theirs = geoip.AsnDB.from_file(path).lookup(address)
                self.assertEqual((mine.asn, mine.organization, mine.hosting),
                                 (theirs['asn'], theirs['org'], theirs['hosting']))
        self.assertEqual(index.lookup('11.0.1.7').cidr, '11.0.1.0/24')
        self.assertEqual(index.lookup('2001:db8::5').cidr, '2001:db8::/32')
        self.assertEqual(index.lookup('2001:db8::5').database_version, '2026-09')
        self.assertIsNone(index.lookup('198.51.100.7'))
        self.assertIsNone(index.lookup('not an ip'))

    def test_index_works_as_a_resolver_provider(self):
        resolver = geo.Resolver(provider_db=self._index(ASN_CSV), now=NOW)
        fact = resolver.provider_fact('http://11.0.1.7:8080')
        self.assertEqual((fact.asn, fact.cidr, fact.hosting_basis), (64501, '11.0.1.0/24',
                                                                    geo.HOSTING_BASIS_ORG_NAME))

    def test_the_smallest_covering_network_is_reported_as_a_range(self):
        net = ipaddress.ip_network
        self.assertEqual(geo.range_cidr(net('11.0.0.0'), net('11.0.0.255')), '11.0.0.0/24')
        self.assertEqual(geo.range_cidr(net('11.0.0.7'), net('11.0.0.7')), '11.0.0.7/32')
        self.assertEqual(geo.range_cidr(net('2001:db8::'), net('2001:db8:ffff::')), '2001:db8::/32')
        self.assertEqual(geo.range_cidr(net('0.0.0.0'), net('255.255.255.255')), '0.0.0.0/0')


class CapabilityTests(unittest.TestCase):
    def test_destination_families_come_from_what_was_measured(self):
        capabilities = geo.TargetCapabilities.from_samples(
            [{'ok': True, 'ip_version': 4}, {'ok': False, 'ip_version': 6}], at=NOW)
        self.assertEqual((capabilities.ipv4_destination, capabilities.ipv6_destination), (True, False))
        self.assertEqual(capabilities.source, 'measured')
        self.assertEqual(capabilities.at, NOW)

    def test_without_samples_nothing_is_known(self):
        capabilities = geo.TargetCapabilities.from_samples((), at=NOW)
        self.assertEqual(capabilities.as_dict(), {'ipv4_destination': None, 'ipv6_destination': None,
                                                  'at': NOW, 'source': geo.UNMEASURED})

    def test_an_ipv6_endpoint_does_not_imply_an_ipv6_destination(self):
        resolver = geo.Resolver(now=NOW)
        described = resolver.describe(row('socks5://[2001:db8::5]:1080', samples=[{'ok': True, 'ip_version': 4}]))
        self.assertEqual(described['ip_version'], 6)
        self.assertIs(described['target_capabilities']['ipv4_destination'], True)
        self.assertIsNone(described['target_capabilities']['ipv6_destination'])

    def test_one_failure_does_not_hide_an_earlier_success(self):
        capabilities = geo.TargetCapabilities.from_samples(
            [{'ok': False, 'ip_version': 4}, {'ok': True, 'ip_version': 4}])
        self.assertIs(capabilities.ipv4_destination, True)


class DescribeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        home = Path(self.temp.name)
        country = home / geoip.DB_NAME
        with gzip.open(country, 'wt', encoding='utf-8') as handle:
            handle.write(COUNTRY_CSV)
        providers = home / geoip.ASN_NAME
        with gzip.open(providers, 'wt', encoding='utf-8') as handle:
            handle.write(ASN_CSV)
        self.resolver = geo.Resolver(country_db=geoip.CountryDB.from_file(country),
                                     provider_db=geoip.AsnDB.from_file(providers), now=NOW,
                                     database_version='2026-09', max_age_seconds=3600)
        self.addCleanup(self.temp.cleanup)

    def test_row_projection_keeps_the_published_fields_and_adds_their_origin(self):
        described = self.resolver.describe(
            row('http://11.0.0.7:8080', exit_ip='11.0.1.9', country='DE'),
            criterion=geo.parse_criterion('NL', basis=geo.BASIS_EXIT))
        for field in ('proxy', 'country', 'exit_ip', 'exit_country', 'asn', 'provider', 'hosting', 'cidr',
                      'ip_version', 'target_capabilities'):
            self.assertIn(field, described)
        self.assertEqual(described['country'], 'DE')
        self.assertEqual(described['exit_country'], 'NL')
        self.assertEqual(described['country_source'], geo.SOURCE_GEOIP)
        self.assertEqual(described['country_at'], NOW - 60)
        self.assertEqual(described['country_stale'], False)
        self.assertEqual(described['country_database_version'], '2026-09')
        self.assertEqual(described['exit_country_source'], geo.SOURCE_OBSERVED_EXIT)
        self.assertEqual(described['asn'], 64500)
        self.assertEqual(described['provider'], 'Example Home Broadband')
        self.assertEqual(described['hosting_basis'], geo.HOSTING_BASIS_ORG_NAME)
        self.assertTrue(described['country_matched'])
        self.assertTrue(described['country_verified'])
        self.assertEqual(described['country_reason'], geo.REASON_MATCHED)

    def test_a_row_the_criterion_rejects_says_why(self):
        described = self.resolver.describe(row('http://11.0.0.7:8080', country='DE'),
                                           criterion=geo.parse_criterion('NL'))
        self.assertFalse(described['country_matched'])
        self.assertEqual(described['country_reason'], geo.REASON_NOT_INCLUDED)

    def test_a_row_without_a_criterion_gets_no_claim(self):
        described = self.resolver.describe(row('http://11.0.0.7:8080', country='DE'))
        self.assertNotIn('country_matched', described)
        self.assertEqual(described['country'], 'DE')

    def test_stale_knowledge_is_visible_in_the_row(self):
        old = geo.Resolver(country_db=self.resolver.country_db, now=NOW, max_age_seconds=10)
        described = old.describe(row('http://11.0.0.7:8080', country='DE', checked_at=NOW - 5000))
        self.assertEqual(described['country'], 'DE')
        self.assertTrue(described['country_stale'])
        self.assertEqual(described['country_age_seconds'], 5000.0)

    def test_rows_without_json_are_projected_as_unknown(self):
        for empty in (None, {}):
            described = self.resolver.describe(empty)
            self.assertIsNone(described['country'])
            self.assertIsNone(described['exit_country'])
            self.assertIsNone(described['ip_version'])
            self.assertEqual(described['country_source'], geo.SOURCE_UNRESOLVED)


if __name__ == '__main__':
    unittest.main()

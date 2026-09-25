"""The new criterion does not change what the existing picker selects (F08).

F08 adds semantics on top of the country picker, it does not replace it.  The
only way to claim that is to run both decisions over the same rows and compare:
``geo.evaluate`` for the new contract, ``proxytool.matches_selection`` for the
behaviour the product has today.  If the integrator changes the signature of
that function, this file skips and says so rather than failing for a rename.
"""
import gzip
import inspect
import ipaddress
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import geo
from proxy_workbench import geoip
from proxy_workbench import proxytool as p

NOW = 1_800_000_000.0
COUNTRY_CSV = ('11.0.0.0,11.0.0.255,DE\n'
               '11.0.1.0,11.0.1.255,NL\n'
               '2001:db8::,2001:db8:ffff:ffff:ffff:ffff:ffff:ffff,US\n')


class PickerCompatTests(unittest.TestCase):
    def setUp(self):
        if not hasattr(p, 'matches_selection'):
            self.skipTest('proxytool.matches_selection is gone; the existing picker is being rewritten')
        missing = {'countries', 'country_of'} - set(inspect.signature(p.matches_selection).parameters)
        if missing:
            self.skipTest('proxytool.matches_selection no longer takes %s' % ', '.join(sorted(missing)))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / geoip.DB_NAME
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            handle.write(COUNTRY_CSV)
        self.database = geoip.CountryDB.from_file(path)
        self.resolver = geo.Resolver(country_db=self.database, now=NOW)
        self.rows = [
            {'proxy': 'http://11.0.0.1:8080', 'country': 'DE'},
            {'proxy': 'socks5://11.0.1.1:1080', 'country': 'NL'},
            {'proxy': 'http://11.0.2.1:8080', 'country': ''},            # in the file, not in the list
            {'proxy': 'http://proxy.example.invalid:8080', 'country': ''},  # no database entry
        ]

    def test_plain_country_selection_matches_the_existing_behaviour(self):
        for codes in ('DE', 'NL', 'DE,NL', 'US', 'FR'):
            with self.subTest(countries=codes):
                criterion = geo.parse_criterion(codes)
                mine = {row['proxy'] for row in
                        geo.filter_rows(self.rows, criterion, self.resolver, NOW).kept}
                theirs = {row['proxy'] for row in self.rows
                          if p.matches_selection(row, countries=criterion.countries,
                                                 country_of=self.database.country_of)}
                self.assertEqual(mine, theirs)

    def test_the_resolver_reads_a_country_the_same_way_for_every_endpoint_form(self):
        for proxy, expected in (('http://11.0.0.1:8080', 'DE'), ('socks5://11.0.1.2:1080', 'NL'),
                                ('socks5://[2001:db8::9]:1080', 'US'), ('http://proxy.example:8080', None)):
            with self.subTest(proxy=proxy):
                self.assertEqual(self.resolver.endpoint_fact(proxy).code, expected)
                self.assertEqual(self.database.country_of(proxy), expected)

    def test_a_declared_country_reaches_the_new_criterion_the_way_it_reaches_the_old_one(self):
        # candidate_meta is what proxytool.country_resolver reads; the resolver takes
        # the same mapping and the selection stays the same.
        declared = {'http://198.51.100.7:8080': 'FR'}
        resolver = geo.Resolver(country_db=self.database, now=NOW, declared=declared)
        rows = [{'proxy': 'http://198.51.100.7:8080'}]
        old = p.country_resolver(_MetaDatabase(declared), self.database)
        self.assertEqual(old(rows[0]['proxy']), 'FR')
        criterion = geo.parse_criterion('FR')
        self.assertTrue(geo.filter_rows(rows, criterion, resolver, NOW).verified == 1)
        self.assertTrue(p.matches_selection(rows[0], countries=criterion.countries, country_of=old))

    def test_a_source_country_outside_the_database_is_kept(self):
        rows = [(ipaddress.ip_address('11.0.0.0'), ipaddress.ip_address('11.0.0.255'), 'DE')]
        self.assertEqual(geo.Resolver(country_db=geoip.CountryDB(rows), now=NOW)
                         .endpoint_fact('http://11.0.0.9:8080').code, 'DE')


class _MetaDatabase:
    """The one method proxytool.country_resolver reads from a connection."""

    def __init__(self, declared):
        self.declared = declared

    def execute(self, _query):
        return iter([(proxy, code) for proxy, code in self.declared.items()])


if __name__ == '__main__':
    unittest.main()

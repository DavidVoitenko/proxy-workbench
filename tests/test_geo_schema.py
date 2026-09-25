"""The geographical columns of migration 1 are the ones this module fills (F08).

``geo.py`` owns no table and writes no DDL, so the contract it depends on is the
one ``db.py`` creates.  These cases run the real migrator on a temporary
database and write a resolved endpoint through the names both sides use, which
is the check that the two agree.

The columns this module asks for and does not have yet are listed in
``docs/integration/HANDOFF/geo.md`` §1.3.  While they are missing, the test that
looks for them skips and says so; it does not pretend they exist and it does not
fail for a column the owner has not added yet.
"""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import geo
from proxy_workbench import db

NOW = 1_800_000_000.0
# Columns migration 1 declares for endpoints, filled from Resolver.describe() / facts.
ENDPOINT_COLUMNS = {
    'ip_version': 'ip_version',
    'country': 'country',
    'country_source': 'country_source',
    'country_at': 'country_at',
    'asn': 'asn',
    'provider': 'provider',
    'hosting': 'hosting',
    'cidr': 'cidr',
}
# Requested in HANDOFF geo.md §1.3, not declared by the migrator yet.
REQUESTED = (
    ('endpoints', 'hosting_basis'),
    ('observations', 'exit_ip'),
    ('observations', 'exit_country'),
    ('observations', 'exit_country_source'),
    ('observations', 'exit_country_at'),
    ('pools', 'quota_basis'),
)


class SchemaTests(unittest.TestCase):
    def setUp(self):
        if not hasattr(db, 'migrate') or not hasattr(db, 'columns'):
            self.skipTest('db.migrate/db.columns are not available yet')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'proxies.sqlite3'
        self.report = db.migrate(self.path, now=NOW)
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)

    def columns(self, table):
        return set(db.columns(self.conn, table))

    def test_the_migrator_reached_the_declared_schema_version(self):
        self.assertEqual(self.conn.execute('PRAGMA user_version').fetchone()[0], db.SCHEMA_VERSION)

    def test_every_endpoint_column_this_module_fills_exists(self):
        present = self.columns('endpoints')
        missing = sorted(column for column in ENDPOINT_COLUMNS if column not in present)
        self.assertEqual(missing, [], 'endpoints is missing columns geo.py writes: %s' % ', '.join(missing))

    def test_a_resolved_endpoint_round_trips_through_the_migrated_schema(self):
        fact = geo.CountryFact.of('DE', geo.SOURCE_GEOIP, at=NOW, database_version='2026-09',
                                  address='11.0.0.7')
        provider = geo.ProviderFact.of('Example Home Broadband', asn=64500, at=NOW, address='11.0.0.7',
                                       cidr='11.0.0.0/24')
        row = {'id': db.endpoint_id('http://11.0.0.7:8080'),
               'canonical': 'http://11.0.0.7:8080',
               'ip_version': geo.endpoint_ip_version('http://11.0.0.7:8080'),
               'country': fact.code, 'country_source': fact.source, 'country_at': fact.at,
               'asn': provider.asn, 'provider': provider.organization,
               'hosting': int(bool(provider.hosting)), 'cidr': provider.cidr}
        names = ', '.join(row)
        marks = ', '.join('?' * len(row))
        self.conn.execute('INSERT INTO endpoints(%s) VALUES (%s)' % (names, marks), tuple(row.values()))
        self.conn.commit()
        stored = dict(zip(row, self.conn.execute(
            'SELECT %s FROM endpoints WHERE id = ?' % names, (row['id'],)).fetchone()))
        self.assertEqual(stored, row)
        # The knowledge read back still says where it came from and when.
        read = geo.CountryFact.of(stored['country'], stored['country_source'], at=stored['country_at'])
        self.assertEqual((read.code, read.source, read.age_seconds(NOW)), ('DE', geo.SOURCE_GEOIP, 0.0))

    def test_an_unknown_endpoint_is_stored_as_null_not_as_a_guess(self):
        self.conn.execute('INSERT INTO endpoints(id, canonical, country, country_source) VALUES (?,?,?,?)',
                          (db.endpoint_id('http://proxy.example.invalid:8080'),
                           'http://proxy.example.invalid:8080', None, geo.SOURCE_UNRESOLVED))
        self.conn.commit()
        country, source = self.conn.execute('SELECT country, country_source FROM endpoints').fetchone()
        self.assertIsNone(country)
        self.assertEqual(source, geo.SOURCE_UNRESOLVED)

    def test_the_requested_columns_are_reported_until_the_owner_adds_them(self):
        missing = ['%s.%s' % (table, column) for table, column in REQUESTED
                   if column not in self.columns(table)]
        if missing:
            self.skipTest('not in the schema yet, requested in HANDOFF geo.md §1.3: %s'
                          % ', '.join(missing))
        for table, column in REQUESTED:
            self.assertIn(column, self.columns(table))

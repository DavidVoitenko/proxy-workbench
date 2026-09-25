"""GeoIP database status, version and installation (F08).

The databases are fixtures written to a temporary folder.  The point of these
cases is the bookkeeping the picker never had: which version is installed, how
old it is, and that a damaged update leaves the last working database in place
instead of deleting it.
"""
import gzip
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import geo
from proxy_workbench import geoip

NOW = 1_800_000_000.0
BODY = ('11.0.0.0,11.0.0.255,DE\n'
        '11.0.1.0,11.0.1.255,NL\n'
        '2001:db8::,2001:db8:ffff:ffff:ffff:ffff:ffff:ffff,US\n')


class DatabaseStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.path = geoip.default_path(self.home)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(self.path, 'wt', encoding='utf-8') as handle:
            handle.write(BODY)
        self.addCleanup(self.temp.cleanup)

    def test_an_absent_database_says_so(self):
        status = geo.database_status(self.home / 'missing.csv.gz', geo.COUNTRY_DATABASE, NOW)
        self.assertFalse(status.present)
        self.assertEqual(status.as_dict()['available'], False)
        self.assertEqual(status.error, 'GEOIP_ABSENT')
        self.assertIsNone(status.version)
        self.assertEqual(status.version_label(), 'absent')
        self.assertEqual(status.attribution, geoip.ATTRIBUTION)

    def test_an_installed_database_reports_size_and_ranges(self):
        status = geo.database_status(self.path, geo.COUNTRY_DATABASE, NOW, geoip.CountryDB.from_file(self.path))
        self.assertTrue(status.present)
        self.assertEqual(status.as_dict()['available'], True)
        self.assertEqual(status.ranges, 3)
        self.assertEqual(status.bytes, self.path.stat().st_size)
        self.assertEqual(status.as_dict()['kind'], geo.COUNTRY_DATABASE)

    def test_a_file_without_a_recorded_version_is_not_called_current(self):
        status = geo.database_status(self.path, geo.COUNTRY_DATABASE, NOW)
        self.assertTrue(status.present)
        self.assertIsNone(status.version)
        self.assertTrue(status.stale)
        self.assertEqual(status.version_label(), 'unknown-version')
        # Age falls back to the file time, so a status line can still show one.
        self.assertIsNotNone(status.age_seconds)

    def test_version_and_date_come_from_the_sidecar(self):
        geo.write_meta(self.path, geo.COUNTRY_DATABASE, '2026-09', at=NOW - 3600)
        status = geo.database_status(self.path, geo.COUNTRY_DATABASE, NOW)
        self.assertEqual((status.version, status.installed_at, status.age_seconds), ('2026-09', NOW - 3600, 3600.0))
        self.assertFalse(status.stale)
        self.assertEqual(status.version_label(), '2026-09')

    def test_an_old_database_is_reported_as_stale(self):
        geo.write_meta(self.path, geo.COUNTRY_DATABASE, '2026-01', at=NOW - 90 * 24 * 3600)
        self.assertTrue(geo.database_status(self.path, geo.COUNTRY_DATABASE, NOW).stale)
        self.assertTrue(geo.database_status(self.path, geo.COUNTRY_DATABASE, NOW,
                                           max_age_seconds=10).stale)
        geo.write_meta(self.path, geo.COUNTRY_DATABASE, '2026-09', at=NOW - 3600)
        self.assertFalse(geo.database_status(self.path, geo.COUNTRY_DATABASE, NOW).stale)

    def test_a_damaged_sidecar_is_ignored_instead_of_failing_the_status(self):
        geo.meta_path(self.path).write_text('{not json', encoding='utf-8')
        self.assertEqual(geo.read_meta(self.path), {})
        self.assertTrue(geo.database_status(self.path, geo.COUNTRY_DATABASE, NOW).present)
        self.assertEqual(geo.read_meta(self.home / 'nothing-here.csv.gz'), {})

    def test_status_of_both_databases_at_once(self):
        status = geo.geo_status(self.home, NOW, geoip.CountryDB.from_file(self.path))
        self.assertTrue(status['country']['available'])
        self.assertFalse(status['asn']['available'])
        self.assertEqual(status['asn']['kind'], geo.ASN_DATABASE)
        self.assertEqual(status['attribution'], geoip.ATTRIBUTION)
        self.assertEqual(status['country']['path'], str(geoip.default_path(self.home)))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.path = self.home / geoip.DB_NAME
        self.body = gzip.compress(BODY.encode('utf-8'))
        self.addCleanup(self.temp.cleanup)

    def test_a_valid_body_is_installed_with_its_version(self):
        version = geo.install_database(self.path, self.body, geo.COUNTRY_DATABASE, '2026-09',
                                       geoip.validate_download, at=NOW)
        self.assertEqual(version, '2026-09')
        self.assertEqual(geoip.CountryDB.from_file(self.path).lookup('11.0.1.1'), 'NL')
        status = geo.database_status(self.path, geo.COUNTRY_DATABASE, NOW)
        self.assertEqual((status.version, status.stale), ('2026-09', False))
        self.assertFalse(self.path.with_name(self.path.name + '.tmp').exists())

    def test_a_damaged_update_keeps_the_last_working_database(self):
        geo.install_database(self.path, self.body, geo.COUNTRY_DATABASE, '2026-09', geoip.validate_download, at=NOW)
        with self.assertRaises(ValueError):
            geo.install_database(self.path, gzip.compress(b'<html>404</html>'), geo.COUNTRY_DATABASE, '2026-10',
                                geoip.validate_download, at=NOW)
        self.assertEqual(geoip.CountryDB.from_file(self.path).lookup('11.0.0.1'), 'DE')
        self.assertEqual(geo.database_status(self.path, geo.COUNTRY_DATABASE, NOW).version, '2026-09')
        self.assertFalse(self.path.with_name(self.path.name + '.tmp').exists())

    def test_the_first_update_that_fails_leaves_nothing_behind(self):
        with self.assertRaises(ValueError):
            geo.install_database(self.path, b'not gzip at all', geo.COUNTRY_DATABASE, '2026-09',
                                geoip.validate_download, at=NOW)
        self.assertFalse(self.path.exists())
        self.assertEqual(geo.read_meta(self.path), {})
        self.assertFalse(geo.meta_path(self.path).exists())

    def test_the_provider_database_uses_the_same_install_path(self):
        path = self.home / geoip.ASN_NAME
        body = gzip.compress(b'11.0.0.0,11.0.0.255,64500,Example Home Broadband\n')
        geo.install_database(path, body, geo.ASN_DATABASE, '2026-09', geoip.validate_asn_download, at=NOW)
        database = geoip.AsnDB.from_file(path)
        self.assertEqual(database.lookup('11.0.0.7')['asn'], 64500)
        self.assertEqual(geo.database_status(path, geo.ASN_DATABASE, NOW, database).kind, geo.ASN_DATABASE)


if __name__ == '__main__':
    unittest.main()

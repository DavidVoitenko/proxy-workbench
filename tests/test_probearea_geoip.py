"""F08 geography and characteristics, exercised against a real database file.

The point of F08's acceptance is that a read-only country filter never starts
a network request and that "unknown" is never dressed up as "checked".  Both
are proved here by building actual DB-IP-shaped CSV files on disk, loading
them through the real loader, and calling the real lookups.
"""
import datetime
import gzip
import io
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from proxy_workbench import geoip

COUNTRY_ROWS = [
    ('5.9.0.0', '5.9.255.255', 'DE'),
    ('2.16.0.0', '2.16.255.255', 'NL'),
    ('2001:db8::', '2001:db8:0:1::', 'DE'),
    ('203.0.113.0', '203.0.113.255', 'ZZ'),        # reserved marker, must be dropped
    ('bad', 'row', 'DE'),                          # unparsable
    ('8.8.8.0', '8.8.4.0', 'US'),                  # reversed range, must be dropped
]
ASN_ROWS = [
    ('5.9.0.0', '5.9.255.255', '24940', 'Hetzner Online GmbH'),
    ('2.16.0.0', '2.16.255.255', '6079', 'Leidos Netherlands B.V.'),
    ('1.1.1.0', '1.1.1.255', '13335', 'Cloudflare, Inc.'),
    ('4.0.0.0', '3.0.0.0', '64500', 'Reversed Range Ltd'),
]


def _csv(rows):
    buffer = io.StringIO()
    for row in rows:
        buffer.write(','.join(row) + '\n')
    return buffer.getvalue()


class GeographyLookups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._dir = tempfile.TemporaryDirectory()
        root = Path(cls._dir.name)
        (root / 'geoip').mkdir(parents=True, exist_ok=True)
        cls.country_path = root / 'geoip' / 'dbip-country-lite.csv.gz'
        with gzip.open(cls.country_path, 'wt', encoding='utf-8') as handle:
            handle.write(_csv(COUNTRY_ROWS))
        cls.asn_path = root / 'geoip' / 'dbip-asn-lite.csv.gz'
        with gzip.open(cls.asn_path, 'wt', encoding='utf-8') as handle:
            handle.write(_csv(ASN_ROWS))
        cls.db = geoip.CountryDB.from_file(cls.country_path)
        cls.asn = geoip.AsnDB.from_file(cls.asn_path)

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def test_a_lookup_finds_the_country_of_a_real_range(self):
        self.assertEqual(self.db.lookup('5.9.1.2'), 'DE')
        self.assertEqual(self.db.lookup('2.16.255.255'), 'NL')
        self.assertEqual(self.db.lookup('2001:db8::1'), 'DE')

    def test_addresses_outside_every_range_stay_unknown(self):
        for address in ('9.9.9.9', '1.2.3.4', '2001:db8:0:2::1'):
            with self.subTest(address=address):
                self.assertIsNone(self.db.lookup(address))

    def test_unknown_is_none_not_a_default_country(self):
        self.assertIsNone(self.db.lookup('9.9.9.9'))
        self.assertNotEqual(self.db.lookup('9.9.9.9'), '')

    def test_a_malformed_address_is_unknown_rather_than_an_error(self):
        for value in ('not-an-ip', '', '999.1.1.1', None, '5.9.1.2/24'):
            with self.subTest(value=value):
                self.assertIsNone(self.db.lookup(value))

    def test_the_malformed_and_reserved_rows_were_not_loaded(self):
        # ZZ is DB-IP's "no data" marker, the reversed range is unusable, and
        # the unparsable line must be skipped: 3 usable ranges, nothing else.
        self.assertEqual(self.db.size, 3)
        self.assertIsNone(self.db.lookup('8.8.8.8'))
        self.assertIsNone(self.db.lookup('203.0.113.1'))

    def test_proxy_urls_and_bare_hosts_resolve_the_same_way(self):
        for proxy in ('http://5.9.1.2:8080', 'socks5://5.9.1.2:1080', 'https://5.9.1.2:443',
                      '5.9.1.2:8080', '5.9.1.2'):
            with self.subTest(proxy=proxy):
                self.assertEqual(self.db.country_of(proxy), 'DE')
        self.assertEqual(self.db.country_of('socks5h://[2001:db8::1]:1080'), 'DE')

    def test_the_country_picker_normalizes_and_rejects(self):
        self.assertEqual(geoip.parse_countries('de, NL'), ('DE', 'NL'))
        self.assertEqual(geoip.parse_countries(['nl', 'DE', 'de']), ('DE', 'NL'))
        self.assertEqual(geoip.parse_countries(''), ())
        self.assertEqual(geoip.parse_countries(None), ())
        for bad in ('Germany', 'DEU', 'D', 12, {'DE': 1}):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    geoip.parse_countries(bad)

    def test_a_read_only_filter_never_opens_a_socket(self):
        """F08 acceptance: the filter itself must not start network activity."""
        original = socket.socket

        def forbidden(*args, **kwargs):
            raise AssertionError('просмотр базы не должен открывать сокет')

        with mock.patch.object(socket, 'socket', forbidden):
            self.assertEqual(self.db.country_of('http://5.9.1.2:8080'), 'DE')
            self.assertIsNone(self.db.lookup('9.9.9.9'))
            self.assertEqual(geoip.parse_countries('de,NL'), ('DE', 'NL'))
            self.assertEqual(geoip.asn_path('/tmp/data'), Path('/tmp/data/geoip/dbip-asn-lite.csv.gz'))
            self.assertEqual(geoip.default_path('/tmp/data'),
                             Path('/tmp/data/geoip/dbip-country-lite.csv.gz'))

    def test_a_missing_database_is_absent_not_guessed(self):
        self.assertIsNone(geoip.CountryDB.load_optional(Path(self._dir.name) / 'nope.csv.gz'))
        self.assertIsNone(geoip.AsnDB.load_optional(Path(self._dir.name) / 'nope.csv.gz'))

    def test_endpoint_country_is_not_the_same_claim_as_an_exit_country(self):
        """F08: an endpoint's country says nothing about where traffic exits."""
        self.assertEqual(self.db.country_of('http://5.9.1.2:8080'), 'DE')
        # There is no exit-country lookup in this module at all, and adding a
        # pretend one would be the "unknown as checked" failure the spec names.
        self.assertFalse(hasattr(geoip, 'exit_country'))
        self.assertFalse(hasattr(self.db, 'exit_country'))


class ProviderAndHosting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._dir = tempfile.TemporaryDirectory()
        path = Path(cls._dir.name) / 'asn.csv.gz'
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            handle.write(_csv(ASN_ROWS))
        cls.asn = geoip.AsnDB.from_file(path)

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def test_asn_and_organization_come_from_the_real_range(self):
        self.assertEqual(self.asn.lookup('5.9.1.2'),
                         {'asn': 24940, 'org': 'Hetzner Online GmbH', 'hosting': True})
        self.assertEqual(self.asn.lookup('1.1.1.1')['asn'], 13335)
        self.assertIsNone(self.asn.lookup('9.9.9.9'))

    def test_the_reversed_range_row_was_dropped(self):
        self.assertEqual(self.asn.size, 3)
        self.assertIsNone(self.asn.lookup('3.5.5.5'))

    def test_hosting_is_a_heuristic_not_a_residential_claim(self):
        self.assertTrue(geoip.is_hosting('Hetzner Online GmbH'))
        self.assertTrue(geoip.is_hosting('Cloudflare, Inc.'))
        self.assertFalse(geoip.is_hosting('Leidos Netherlands B.V.'))
        self.assertFalse(geoip.is_hosting(''))
        self.assertFalse(geoip.is_hosting(None))
        # The flag is a hint and says so: there is no "residential" verdict.
        self.assertNotIn('residential', str(geoip.AsnDB.lookup.__doc__ or ''))
        row = self.asn.lookup('2.16.0.1')
        self.assertEqual(set(row), {'asn', 'org', 'hosting'})


class DatabaseIntake(unittest.TestCase):
    def test_a_good_download_validates(self):
        body = gzip.compress(_csv(COUNTRY_ROWS).encode())
        geoip.validate_download(body)

    def test_broken_and_oversized_downloads_are_refused(self):
        for body, code in ((b'not gzip at all', 'GEOIP_INVALID'),
                           (gzip.compress(b'nothing,useful,here\n'), 'GEOIP_INVALID'),
                           (b'\x00' * (geoip.MAX_DOWNLOAD_BYTES + 1), 'GEOIP_TOO_LARGE')):
            with self.subTest(code=code):
                with self.assertRaises(ValueError) as caught:
                    geoip.validate_download(body)
                self.assertEqual(str(caught.exception), code)

    def test_asn_download_validates_separately(self):
        geoip.validate_asn_download(gzip.compress(_csv(ASN_ROWS).encode()))
        with self.assertRaises(ValueError) as caught:
            geoip.validate_asn_download(gzip.compress(b'a,b\n'))
        self.assertEqual(str(caught.exception), 'GEOIP_INVALID')

    def test_month_candidates_are_the_current_and_the_previous_one(self):
        today = datetime.date(2026, 9, 26)
        self.assertEqual(geoip.candidate_months(today), ['2026-09', '2026-08'])
        self.assertEqual(geoip.candidate_months(datetime.date(2026, 1, 5)),
                         ['2026-01', '2025-12'])
        self.assertIn('db-ip.com', geoip.ATTRIBUTION)


def live_report():
    import tempfile
    print('\n--- F08: география по настоящему файлу базы (сеть не используется) ' + '-' * 4)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'geoip').mkdir(parents=True, exist_ok=True)
        country_path = root / 'geoip' / 'dbip-country-lite.csv.gz'
        with gzip.open(country_path, 'wt', encoding='utf-8') as handle:
            handle.write(_csv(COUNTRY_ROWS))
        asn_path = root / 'geoip' / 'dbip-asn-lite.csv.gz'
        with gzip.open(asn_path, 'wt', encoding='utf-8') as handle:
            handle.write(_csv(ASN_ROWS))

        db = geoip.CountryDB.from_file(country_path)
        asn = geoip.AsnDB.from_file(asn_path)
        print(f'  загружено диапазонов: страны={db.size} провайдеры={asn.size} '
              f'(ZZ, битая строка и развёрнутый диапазон отброшены)')
        for address in ('5.9.1.2', '2.16.255.255', '2001:db8::1', '9.9.9.9',
                        '203.0.113.1', '8.8.8.8', 'мусор'):
            value = db.lookup(address)
            row = asn.lookup(address)
            detail = '-' if row is None else f"asn={row['asn']} org={row['org']!r} hosting={row['hosting']}"
            print(f'  {address:16} страна={str(value):6} провайдер: {detail}')
        print('  один критерий страны для всех форм адреса:',
              {form: db.country_of(form) for form in
               ('http://5.9.1.2:8080', 'socks5://5.9.1.2:1080', 'socks5h://[2001:db8::1]:1080',
                '5.9.1.2:8080')})
        print('  picker:', geoip.parse_countries('de, NL'), '| отказ:',
              end=' ')
        try:
            geoip.parse_countries('Germany')
        except ValueError as exc:
            print(str(exc)[:70])

        original = socket.socket

        def forbidden(*args, **kwargs):
            raise AssertionError('фильтр сам запустил сеть')

        with mock.patch.object(socket, 'socket', forbidden):
            db.country_of('http://5.9.1.2:8080')
            db.lookup('9.9.9.9')
            geoip.parse_countries('de,NL')
        print('  read-only фильтр ни одного сокета не открыл: да')

        print('  скачивание БД при intake:')
        for body, label in ((gzip.compress(_csv(COUNTRY_ROWS).encode()), 'корректный gz+CSV'),
                            ('не gzip'.encode('utf-8'), 'битый ответ'),
                            (gzip.compress(b'a,b\n'), 'CSV без диапазонов')):
            try:
                geoip.validate_download(body)
                print(f'    {label:22} -> принят')
            except ValueError as exc:
                print(f'    {label:22} -> отказ {exc}')
        print('  неизвестное не выдаётся за проверенное: lookup() возвращает None, '
              'а не страну по умолчанию')
        print('  hosting — эвристика, а не доказательство residential/mobile:',
              f"is_hosting('Hetzner Online GmbH')={geoip.is_hosting('Hetzner Online GmbH')}, "
              f"is_hosting('Leidos Netherlands B.V.')={geoip.is_hosting('Leidos Netherlands B.V.')}")


if __name__ == '__main__':
    live_report()

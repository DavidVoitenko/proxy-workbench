import asyncio
import contextlib
import datetime
import gzip
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geoip
import gui
import proxytool as p

# Synthetic ranges: 11.0.0.0/24 → DE, 11.0.1.0/24 → NL, 2001:db8::/32 → US.
CSV = ('11.0.0.0,11.0.0.255,DE\n'
       '11.0.1.0,11.0.1.255,NL\n'
       '2001:db8::,2001:db8:ffff:ffff:ffff:ffff:ffff:ffff,US\n'
       '11.0.2.0,11.0.2.255,ZZ\n'
       'broken line\n')


def config():
    return dict(version=2, targets=[dict(url='http://service.invalid/', method='GET', statuses=[200],
                                         headers={}, contains=None, sha256=None)],
                attempts=1, timeout=0.3, max_bytes=1024)


def good(proxy, cfg, ms=10):
    samples = [dict(ok=True, ms=ms, target=0, attempt=1, status=200, error=None, bytes=1)]
    return p.summarize(proxy, samples, cfg)


class CountryDBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / geoip.DB_NAME
        with gzip.open(self.path, 'wt', encoding='utf-8') as handle:
            handle.write(CSV)

    def tearDown(self):
        self.temp.cleanup()

    def test_lookup_ipv4_ipv6_and_proxies(self):
        db = geoip.CountryDB.from_file(self.path)
        self.assertEqual(db.size, 3)
        self.assertEqual(db.lookup('11.0.0.7'), 'DE')
        self.assertEqual(db.lookup('11.0.1.255'), 'NL')
        self.assertIsNone(db.lookup('11.0.2.1'))
        self.assertIsNone(db.lookup('10.9.9.9'))
        self.assertEqual(db.country_of('socks5://[2001:db8::5]:1080'), 'US')
        self.assertEqual(db.country_of('http://11.0.0.1:8080'), 'DE')
        self.assertIsNone(db.lookup('not an ip'))
        self.assertIsNone(geoip.CountryDB.load_optional(Path(self.temp.name) / 'missing.csv.gz'))

    def test_parse_countries(self):
        self.assertEqual(geoip.parse_countries(' de, nl,DE '), ('DE', 'NL'))
        self.assertEqual(geoip.parse_countries(''), ())
        self.assertEqual(geoip.parse_countries(['us']), ('US',))
        for bad in ('DEU', 'D1', 42):
            with self.assertRaises(ValueError):
                geoip.parse_countries(bad)

    def test_download_validation_and_months(self):
        geoip.validate_download(self.path.read_bytes())
        for bad in (b'not gzip', gzip.compress(b'<html>error</html>')):
            with self.assertRaises(ValueError):
                geoip.validate_download(bad)
        self.assertEqual(geoip.candidate_months(datetime.date(2026, 1, 15)), ['2026-01', '2025-12'])


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_falls_back_to_previous_month(self):
        body = gzip.compress(CSV.encode())
        requested = []

        async def handler(reader, writer):
            try:
                line = (await reader.readuntil(b'\r\n\r\n')).split(b'\r\n')[0].decode()
                requested.append(line)
                month = geoip.candidate_months()[1]
                if month in line:
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
                else:
                    writer.write(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                await writer.drain()
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/{{month}}.csv.gz'
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(geoip, 'DOWNLOAD_URL', url):
            target = Path(temp) / 'geoip' / geoip.DB_NAME
            async with server:
                month = await p.download_geoip(target, timeout=5)
            self.assertEqual(month, geoip.candidate_months()[1])
            self.assertEqual(len(requested), 2)
            self.assertEqual(geoip.CountryDB.from_file(target).lookup('11.0.1.1'), 'NL')


class ScanSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db = p.open_db(self.home / 'test.sqlite3')
        proxies = [f'http://11.0.0.{i}:80' for i in range(10)] + [f'socks5://11.0.1.{i}:1080' for i in range(10)]
        self.db.executemany('INSERT INTO candidates VALUES (?)', ((proxy,) for proxy in proxies))
        self.db.commit()
        rows = [(geoip.ipaddress.ip_address('11.0.0.0'), geoip.ipaddress.ip_address('11.0.0.255'), 'DE'),
                (geoip.ipaddress.ip_address('11.0.1.0'), geoip.ipaddress.ip_address('11.0.1.255'), 'NL')]
        self.country_of = p.country_resolver(self.db, geoip.CountryDB(rows))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    async def run_scan(self, **options):
        seen = []

        async def probe(proxy, cfg, rate):
            seen.append(proxy)
            await asyncio.sleep(0)
            return good(proxy, cfg)
        await p.scan(self.db, config(), workers=2, rate=0, probe=probe, progress=False, min_success=1,
                     country_of=self.country_of, **options)
        return seen

    async def test_country_and_protocol_narrow_checks_without_new_profile(self):
        seen = await self.run_scan(countries=('NL',))
        self.assertEqual(sorted(seen), sorted(f'socks5://11.0.1.{i}:1080' for i in range(10)))
        stored = json.loads(self.db.execute('SELECT payload FROM results LIMIT 1').fetchone()[0])
        self.assertEqual(stored['country'], 'NL')
        # A wider run reuses the same profile and only checks what is left.
        seen = await self.run_scan(protocol='http')
        self.assertEqual(len(seen), 10)
        self.assertTrue(all(proxy.startswith('http://') for proxy in seen))
        self.assertEqual(self.db.execute('SELECT count(DISTINCT profile) FROM results').fetchone()[0], 1)

    async def test_want_stops_early_and_resume_continues(self):
        progress = []
        seen = []

        async def probe(proxy, cfg, rate):
            seen.append(proxy)
            await asyncio.sleep(0)
            return good(proxy, cfg)
        await p.scan(self.db, config(), workers=1, rate=0, probe=probe, progress=False, min_success=1,
                     want=3, on_progress=progress.append)
        self.assertEqual(len(seen), 3)
        self.assertEqual(progress[-1]['passed'], 3)
        seen.clear()
        await p.scan(self.db, config(), workers=1, rate=0, probe=probe, progress=False, min_success=1, want=3)
        self.assertEqual(seen, [])  # already have enough
        await p.scan(self.db, config(), workers=1, rate=0, probe=probe, progress=False, min_success=1)
        self.assertEqual(len(seen), 17)

    async def test_previously_working_proxies_are_checked_first(self):
        other = dict(config(), attempts=2)
        for proxy in ('http://11.0.0.9:80', 'socks5://11.0.1.9:1080'):
            row = good(proxy, other)
            self.db.execute('INSERT INTO results VALUES (?,?,?)', ('older', proxy, json.dumps(row)))
        self.db.commit()
        seen = await self.run_scan(want=2)
        self.assertEqual(sorted(seen[:2]), ['http://11.0.0.9:80', 'socks5://11.0.1.9:1080'])

    async def test_full_sweep_keeps_key_order_for_fast_inserts(self):
        seen = []

        async def probe(proxy, cfg, rate):
            seen.append(proxy)
            return good(proxy, cfg)
        await p.scan(self.db, config(), workers=1, rate=0, probe=probe, progress=False, min_success=1)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(seen), 20)

    def test_export_by_country(self):
        cfg = config()
        self.db.execute('INSERT INTO profiles VALUES (?,?)', ('fx', json.dumps(cfg)))
        for proxy in ('http://11.0.0.1:80', 'socks5://11.0.1.1:1080'):
            self.db.execute('INSERT INTO results VALUES (?,?,?)', ('fx', proxy, json.dumps(good(proxy, cfg))))
        self.db.execute("INSERT INTO candidate_meta(proxy, country) VALUES ('http://11.0.0.1:80', 'FR')")
        self.db.commit()
        country_of = p.country_resolver(self.db, None)
        out = self.home / 'out'
        report = p.export(self.db, 'fx', out, min_success=1, countries=('FR',), country_of=country_of)
        self.assertEqual(report['exported'], 1)
        self.assertEqual((out / 'proxies.txt').read_text(encoding='utf-8').split(), ['http://11.0.0.1:80'])
        self.assertIn(',FR', (out / 'ranked.csv').read_text(encoding='utf-8'))


class GuiGeoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        db = p.open_db(self.home / 'proxies.sqlite3')
        cfg = config()
        db.execute('INSERT INTO profiles VALUES (?,?)', ('fx', json.dumps(cfg)))
        for proxy in ('http://11.0.0.1:80', 'socks5://11.0.1.1:1080'):
            db.execute('INSERT INTO candidates VALUES (?)', (proxy,))
            db.execute('INSERT INTO results VALUES (?,?,?)', ('fx', proxy, json.dumps(good(proxy, cfg))))
        db.commit()
        db.close()
        (self.home / 'last-profile.txt').write_text('fx')
        self.server = gui.make_server(self.home)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.server.server_port}', trust_env=False,
                                   headers={'X-Workbench-Token': self.server.app.token}, timeout=5)

    def tearDown(self):
        self.server.app.close()
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_status_column_and_filter(self):
        self.assertFalse(self.client.get('/api/geoip').json()['available'])
        rows = self.client.get('/api/results?min_success=1').json()['rows']
        self.assertEqual({row['country'] for row in rows}, {None})
        path = geoip.default_path(self.home)
        path.parent.mkdir(parents=True)
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            handle.write(CSV)
        status = self.client.get('/api/geoip').json()
        self.assertEqual((status['available'], status['ranges']), (True, 3))
        rows = self.client.get('/api/results?min_success=1&country=nl').json()['rows']
        self.assertEqual([(row['proxy'], row['country']) for row in rows], [('socks5://11.0.1.1:1080', 'NL')])
        self.assertEqual(self.client.get('/api/results?country=DEU').status_code, 400)

    def test_settings_pass_country_and_want_to_the_scanner(self):
        settings = gui.validate(dict(gui.defaults(), countries='nl,de', want=5))
        self.assertEqual((settings['countries'], settings['want']), ('DE,NL', 5))
        with self.assertRaises(ValueError):
            gui.validate(dict(gui.defaults(), countries='Germany'))


if __name__ == '__main__':
    unittest.main()

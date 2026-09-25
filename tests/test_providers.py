import asyncio
import gzip
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

import httpx
from tests.workbench_support import add_candidate, add_candidates, store_result  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import api, geoip, gui
from proxy_workbench import proxytool as p

#: A result fixture describes a measurement that just happened; the
#: admission contract has no "fresh forever" state (CONTRACTS §2.4).
_NOW = time.time()

ASN_CSV = ('11.0.0.0,11.0.0.255,64500,Example Home Broadband\n'
           '11.0.1.0,11.0.1.255,64501,Hetzner Online GmbH\n'
           '2001:db8::,2001:db8:ffff:ffff:ffff:ffff:ffff:ffff,64502,"Cloud Servers, Inc."\n'
           'bad,line\n')


def result_row(proxy):
    return dict(proxy=proxy, reliability=1, min_target_reliability=1, latency_ms=100, jitter_ms=1, score=90,
                successes=1, requests=1, checked_at=_NOW, samples=[])


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        path = geoip.asn_path(self.home)
        path.parent.mkdir(parents=True)
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            handle.write(ASN_CSV)
        self.asn = geoip.AsnDB.from_file(path)

    def tearDown(self):
        self.temp.cleanup()

    def test_lookup_and_hosting(self):
        self.assertEqual(self.asn.size, 3)
        self.assertEqual(self.asn.provider_of('http://11.0.0.9:80'),
                         {'asn': 64500, 'org': 'Example Home Broadband', 'hosting': False})
        self.assertTrue(self.asn.provider_of('socks5://11.0.1.1:1080')['hosting'])
        self.assertEqual(self.asn.provider_of('http://[2001:db8::1]:80')['org'], 'Cloud Servers, Inc.')
        self.assertIsNone(self.asn.lookup('12.0.0.1'))
        for name, hosting in (('Amazon.com, Inc.', True), ('DigitalOcean, LLC', True), ('Rostelecom', False),
                              ('Comcast Cable Communications', False), ('OVH SAS', True)):
            self.assertEqual(geoip.is_hosting(name), hosting, name)
        geoip.validate_asn_download(gzip.compress(ASN_CSV.encode()))
        with self.assertRaises(ValueError):
            geoip.validate_asn_download(gzip.compress(b'1.1.1.1,1.1.1.2,DE\n'))

    def test_scan_skips_hosting_and_export_reports_providers(self):
        db = p.open_db(self.home / 'proxies.sqlite3')
        home, hosted = 'http://11.0.0.1:80', 'http://11.0.1.1:80'
        add_candidates(db, ((home,), (hosted,)))
        db.commit()
        probed = []

        async def probe(proxy, cfg, rate):
            probed.append(proxy)
            return p.summarize(proxy, [dict(ok=True, ms=5, target=0, attempt=1)], cfg)
        cfg = dict(version=2, targets=[dict(url='http://service.invalid/', method='GET', statuses=[200], headers={},
                                            contains=None, sha256=None)], attempts=1, timeout=1, max_bytes=1024)
        provider_of = p.provider_resolver(self.asn)
        profile = asyncio.run(p.scan(db, cfg, workers=1, rate=0, probe=probe, progress=False, min_success=1,
                                     exclude_hosting=True, provider_of=provider_of))
        self.assertEqual(probed, [home])
        asyncio.run(p.scan(db, cfg, workers=1, rate=0, probe=probe, progress=False, min_success=1))
        self.assertEqual(sorted(probed), [home, hosted])
        report = p.export(db, profile, self.home / 'exports', min_success=1, provider_of=provider_of)
        self.assertEqual(report['exported'], 2)
        csv_text = (self.home / 'exports' / 'ranked.csv').read_text()
        self.assertIn('64501,Hetzner Online GmbH,True', csv_text)
        report = p.export(db, profile, self.home / 'exports', min_success=1, provider_of=provider_of, exclude_hosting=True)
        self.assertEqual((report['exported'], report['exclude_hosting']), (1, True))
        rows, _ = api.Exports(self.home / 'exports').load()
        self.assertEqual(rows[0]['provider'], 'Example Home Broadband')
        p.export(db, profile, self.home / 'exports', min_success=1, provider_of=provider_of)
        rows, _ = api.Exports(self.home / 'exports').load()
        self.assertEqual([row['proxy'] for row in api.select(rows, api.parse_query('hosting=0'))], [home])
        self.assertEqual([row['proxy'] for row in api.select(rows, api.parse_query('hosting=1'))], [hosted])
        with self.assertRaises(ValueError):
            api.parse_query('hosting=maybe')
        db.close()

    def test_gui_results_column_and_filter(self):
        db = p.open_db(self.home / 'proxies.sqlite3')
        db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fx', json.dumps(dict(targets=[dict(url='https://one.invalid/')]))))
        for proxy in ('http://11.0.0.1:80', 'http://11.0.1.1:80'):
            add_candidate(db, (proxy))
            store_result(db, ('fx', proxy, json.dumps(result_row(proxy))))
        db.commit()
        p.export(db, 'fx', self.home / 'exports', min_success=1, provider_of=p.provider_resolver(self.asn))
        db.close()
        # The table, the API and the engine only agree on which rows exist once a
        # snapshot is published: the collection and the profile come from the
        # published status, not from a guess (CONTRACTS §1.2 rule 2).
        (self.home / 'last-profile.txt').write_text('fx')
        server = gui.make_server(self.home)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False,
                                  headers={'X-Workbench-Token': server.app.token}, timeout=5)
            rows = client.get('/api/results?min_success=1').json()['rows']
            self.assertEqual({row['provider']['org'] for row in rows}, {'Example Home Broadband', 'Hetzner Online GmbH'})
            rows = client.get('/api/results?min_success=1&hosting=hide').json()['rows']
            self.assertEqual([row['proxy'] for row in rows], ['http://11.0.0.1:80'])
            status = client.get('/api/geoip').json()
            self.assertEqual((status['providers'], status['provider_ranges']), (True, 3))
            client.close()
        finally:
            server.app.close()
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertTrue(gui.validate(dict(gui.defaults(), exclude_hosting=True))['exclude_hosting'])


if __name__ == '__main__':
    unittest.main()

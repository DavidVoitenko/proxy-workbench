"""Every artifact of one generation: TXT, CSV, JSON, protocol files, PAC, Clash, proxychains.

F28: the existing formats are preserved and checked, a static list says what it
is, and an empty or unsupported set never becomes a direct connection.
"""
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import core, exportsvc as es, i18n

NOW = 1_700_000_000.0
ROWS = [
    dict(proxy='http://203.0.113.7:8080', country='DE', latency_ms=120.0, jitter_ms=3.0,
         reliability=1.0, min_target_reliability=1.0, successes=3, requests=3, score=88.0,
         checked_at=NOW, valid_until=NOW + 600, listed_in=1, source_keys=['alpha', 'beta'],
         reputation={'status': 'clean'}, speed={'mbps': 12.5, 'bytes': 1024, 'ms': 900, 'state': 'ok'},
         anonymity={'level': 'elite', 'signals': ['no-leak']}, history={'checks': 4, 'passes': 3}),
    dict(proxy='socks5://198.51.100.9:1080', country='NL', latency_ms=90.0, jitter_ms=1.0,
         reliability=1.0, min_target_reliability=1.0, successes=3, requests=3, score=91.0,
         checked_at=NOW, valid_until=NOW + 600, listed_in=1, source_keys=['gamma'],
         reputation={'status': 'clean'}),
    dict(proxy='socks4://192.0.2.44:1080', country='US', latency_ms=300.0, jitter_ms=20.0,
         reliability=1.0, min_target_reliability=1.0, successes=3, requests=3, score=40.0,
         checked_at=NOW, valid_until=NOW + 600, listed_in=1, source_keys=[],
         reputation={'status': 'clean'}),
    dict(proxy='socks5h://198.51.100.10:1080', country='NL', latency_ms=95.0, jitter_ms=2.0,
         reliability=1.0, min_target_reliability=1.0, successes=3, requests=3, score=90.0,
         checked_at=NOW, valid_until=NOW + 600, listed_in=1, source_keys=['gamma'],
         reputation={'status': 'clean'}),
]
IPV6 = dict(proxy='socks5://[2001:db8::1]:1080', country='FI', latency_ms=140.0, jitter_ms=4.0,
            reliability=1.0, min_target_reliability=1.0, successes=3, requests=3, score=80.0,
            checked_at=NOW, valid_until=NOW + 600, reputation={'status': 'clean'})


def scope(**overrides):
    base = dict(identity=core.Scope('col-1', 'prof-1', 1))
    base.update(overrides)
    return es.ExportScope(**base)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)
        self.artifact = es.write_snapshot(self.home, ROWS, scope=scope(),
                                          options=es.ExportOptions(published_at=NOW, client_target='1.10.0'),
                                          policy=core.Policy(max_age_seconds=900), now=NOW)

    def tearDown(self):
        self.temp.cleanup()

    def read(self, name):
        return (self.artifact.directory / name).read_text(encoding='utf-8')

    def test_every_contracted_artifact_is_written(self):
        expected = {'proxies.txt', 'snapshot.txt', 'hostport.txt', 'ranked.json', 'ranked.csv',
                    'http.txt', 'https.txt', 'socks4.txt', 'socks5.txt', 'proxychains.txt',
                    'proxy.pac', 'clash.yaml', 'singbox.json', 'status.json'}
        self.assertEqual(expected, {path.name for path in self.artifact.directory.iterdir()})
        self.assertEqual(expected, set(self.artifact.files))
        # status.json carries the manifest of the rest, so it cannot be inside it
        self.assertEqual(expected - {'status.json'}, set(self.artifact.manifest))
        self.assertEqual(self.artifact.manifest, json.loads(self.read('status.json'))['manifest'])

    def test_proxies_txt_stays_bare_so_existing_tools_keep_working(self):
        self.assertEqual(self.read('proxies.txt'),
                         ''.join(f"{row['proxy']}\n" for row in ROWS))
        self.assertNotIn('#', self.read('proxies.txt'))

    def test_snapshot_txt_says_what_it_is_and_cannot_expire_itself(self):
        text = self.read('snapshot.txt')
        header = text.split('\n', 1)[0]
        self.assertTrue(header.startswith('# Proxy Workbench snapshot.'))
        for line in text.splitlines():
            if line.startswith('#'):
                continue
            self.assertIn(line, [row['proxy'] for row in ROWS])
        self.assertIn(f"# generation: {self.artifact.generation}", text)
        self.assertIn(f"# scope_digest: {self.artifact.status.digest}", text)
        self.assertIn(f"# expires_at: {NOW + 900}", text)
        self.assertIn('# rows: 4', text)
        self.assertIn(i18n.tr('Этот статический файл не может отозвать сам себя. Проверяйте истёкшие строки заново:',
                               'This static file cannot revoke itself. Re-check rows past expires_at.'), text)
        self.assertEqual(core.STATIC_TTL_NOTICE, 'E_STATE_SNAPSHOT_STATIC_TTL')

    def test_protocol_files_hold_only_their_own_protocol(self):
        self.assertEqual(self.read('http.txt'), '203.0.113.7:8080\n')
        self.assertEqual(self.read('socks5.txt'), '198.51.100.9:1080\n198.51.100.10:1080\n')
        self.assertEqual(self.read('socks4.txt'), '192.0.2.44:1080\n')
        self.assertEqual(self.read('https.txt'), '')

    def test_hostport_and_proxychains_cover_what_they_can(self):
        self.assertEqual(self.read('hostport.txt').splitlines(),
                         ['203.0.113.7:8080', '198.51.100.9:1080', '192.0.2.44:1080',
                          '198.51.100.10:1080'])
        chains = self.read('proxychains.txt')
        self.assertIn('http 203.0.113.7 8080', chains)
        self.assertIn('socks5 198.51.100.9 1080', chains)
        self.assertIn('socks4 192.0.2.44 1080', chains)
        self.assertNotIn('198.51.100.10 1080', chains.splitlines()[1])  # alias folds into socks5

    def test_ranked_csv_keeps_legacy_columns_and_adds_the_new_ones(self):
        table = list(csv.DictReader(io.StringIO(self.read('ranked.csv'))))
        self.assertEqual(len(table), 4)
        for name in ('proxy', 'score', 'latency_ms', 'reliability', 'min_target_reliability',
                     'successes', 'requests', 'checked_at', 'valid_until', 'reputation_status',
                     'reputation_sources', 'anonymity', 'anonymity_signals', 'country', 'exit_ip',
                     'exit_country', 'asn', 'provider', 'hosting', 'mbps', 'listed_in',
                     'source_keys', 'recommended', 'checks', 'passes', 'protocol', 'uptime',
                     'age_seconds', 'admission_reason', 'access_id'):
            self.assertIn(name, table[0], name)
        self.assertEqual(table[0]['anonymity'], 'elite')
        self.assertEqual(table[0]['anonymity_signals'], 'no-leak')
        self.assertEqual(table[0]['mbps'], '12.5')
        self.assertEqual(table[0]['checks'], '4')
        self.assertEqual(table[0]['passes'], '3')
        self.assertEqual(table[0]['source_keys'], 'alpha,beta')
        self.assertEqual(table[0]['reputation_status'], 'clean')

    def test_ranked_json_round_trips(self):
        rows = json.loads(self.read('ranked.json'))
        self.assertEqual([row['proxy'] for row in rows], [row['proxy'] for row in ROWS])
        self.assertEqual(rows[0]['speed'], ROWS[0]['speed'])
        self.assertEqual(rows[0]['history'], {'checks': 4, 'passes': 3})
        self.assertIsNone(rows[0]['admission_reason'])
        self.assertIsNone(rows[0]['age_seconds'])

    def test_pac_lists_browsers_can_use(self):
        pac = self.read('proxy.pac')
        self.assertIn('function FindProxyForURL(url, host)', pac)
        self.assertIn('PROXY 203.0.113.7:8080', pac)
        self.assertIn('SOCKS5 198.51.100.9:1080', pac)
        self.assertIn('SOCKS 192.0.2.44:1080', pac)
        self.assertNotIn('DIRECT', pac)

    def test_clash_keeps_its_shape(self):
        clash = self.read('clash.yaml')
        self.assertIn('mixed-port: 7890', clash)
        self.assertIn('"type": "http"', clash)
        self.assertIn('"type": "socks5"', clash)
        self.assertIn('- MATCH,auto', clash)
        self.assertNotIn('DIRECT', clash)
        self.assertNotIn('socks4', clash)

    def test_ipv6_is_bracketed_everywhere_it_appears(self):
        artifact = es.write_snapshot(self.home, [IPV6], scope=scope(),
                                     options=es.ExportOptions(published_at=NOW, client_target='1.10.0'),
                                     now=NOW)
        text = (artifact.directory / 'proxies.txt').read_text(encoding='utf-8')
        self.assertEqual(text.strip(), 'socks5://[2001:db8::1]:1080')
        chains = (artifact.directory / 'proxychains.txt').read_text(encoding='utf-8')
        self.assertIn('socks5 2001:db8::1 1080', chains)
        pac = (artifact.directory / 'proxy.pac').read_text(encoding='utf-8')
        self.assertIn('SOCKS5 [2001:db8::1]:1080', pac)
        clash = (artifact.directory / 'clash.yaml').read_text(encoding='utf-8')
        members = [json.loads(line.strip()[2:]) for line in clash.splitlines()
                   if line.startswith('  - {')]
        self.assertEqual(members[0]['server'], '2001:db8::1')
        self.assertEqual(members[0]['port'], 1080)


class FailClosedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_an_empty_set_never_becomes_a_direct_connection(self):
        artifact = es.write_snapshot(self.home, [], scope=scope(),
                                     options=es.ExportOptions(published_at=NOW, client_target='1.10.0'),
                                     now=NOW)
        clash = (artifact.directory / 'clash.yaml').read_text(encoding='utf-8')
        pac = (artifact.directory / 'proxy.pac').read_text(encoding='utf-8')
        singbox = json.loads((artifact.directory / 'singbox.json').read_text(encoding='utf-8'))
        self.assertIn('- MATCH,REJECT', clash)
        self.assertNotIn('DIRECT', clash)
        self.assertNotIn('DIRECT', pac)
        self.assertEqual([item['type'] for item in singbox['outbounds']], ['block'])
        self.assertEqual(singbox['route']['final'], 'blocked')
        self.assertEqual((artifact.directory / 'proxies.txt').read_text(encoding='utf-8'), '')
        self.assertEqual(json.loads((artifact.directory / 'ranked.json').read_text(encoding='utf-8')), [])
        status = json.loads((artifact.directory / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['state'], 'empty')
        self.assertTrue(status['empty_export'])
        self.assertIsNone(status['expires_at'])

    def test_an_unsupported_only_set_fails_closed_and_says_why(self):
        only_https = [dict(proxy='https://203.0.113.7:443', country='DE', checked_at=NOW,
                           valid_until=NOW + 600)]
        artifact = es.write_snapshot(self.home, only_https, scope=scope(),
                                     options=es.ExportOptions(published_at=NOW, client_target='1.10.0'),
                                     now=NOW)
        clash = (artifact.directory / 'clash.yaml').read_text(encoding='utf-8')
        singbox = json.loads((artifact.directory / 'singbox.json').read_text(encoding='utf-8'))
        self.assertIn('- MATCH,REJECT', clash)
        self.assertEqual([item['type'] for item in singbox['outbounds']], ['block'])
        status = json.loads((artifact.directory / 'status.json').read_text(encoding='utf-8'))
        self.assertTrue(status['compat']['files']['clash.yaml']['fail_closed'])
        self.assertEqual(status['compat']['files']['clash.yaml']['state_detail'], 'empty_no_match')
        reasons = {item['format']: item['reasons'] for item in status['compat']['unsupported']}
        self.assertEqual(reasons['clash'], ['E_EXPORT_TLS_UNSUPPORTED'])
        self.assertEqual(reasons['singbox'], ['E_EXPORT_TLS_UNSUPPORTED'])
        # the formats that can carry it are untouched: a dropped row is not a lost row
        self.assertEqual(status['compat']['files']['proxies.txt']['supported'], 1)
        self.assertEqual(status['exported'], 1)

    def test_the_empty_policy_may_demand_a_generation_error_instead(self):
        with self.assertRaises(es.ExportError) as caught:
            es.write_snapshot(self.home, [], scope=scope(),
                              options=es.ExportOptions(published_at=NOW, empty_policy='error'), now=NOW)
        self.assertEqual(caught.exception.code, 'E_STATE_NO_PROXIES')
        self.assertEqual(list((self.home / 'generations').iterdir()), [])


if __name__ == '__main__':
    unittest.main()

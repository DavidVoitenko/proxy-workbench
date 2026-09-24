import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxytool as p


def config():
    return dict(version=2, targets=[dict(url='http://service.invalid/', method='GET', statuses=[200],
                                         headers={}, contains=None, sha256=None)],
                attempts=1, timeout=0.3, max_bytes=1024)


def measured(proxy, cfg, ok=True, ms=10):
    samples = [dict(ok=ok, ms=ms, target=0, attempt=1, status=200 if ok else 503, error=None, bytes=1)]
    return p.summarize(proxy, samples, cfg)


class FreshnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db = p.open_db(self.home / 'test.sqlite3')
        self.proxies = [f'http://11.0.0.{i}:80' for i in range(6)]
        self.db.executemany('INSERT INTO candidates VALUES (?)', ((proxy,) for proxy in self.proxies))
        self.db.commit()
        self.alive = set(self.proxies[:3])

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    async def scan(self, **options):
        seen = []

        async def probe(proxy, cfg, rate):
            seen.append(proxy)
            return measured(proxy, cfg, ok=proxy in self.alive)
        profile = await p.scan(self.db, config(), workers=2, rate=0, probe=probe, progress=False, min_success=1, **options)
        return profile, seen

    def histories(self, profile):
        return {proxy: json.loads(payload)['history']
                for proxy, payload in self.db.execute('SELECT proxy, payload FROM results WHERE profile=?', (profile,))}

    async def test_recheck_passing_touches_only_matching_proxies_and_keeps_history(self):
        profile, seen = await self.scan()
        self.assertEqual(len(seen), 6)
        self.alive = {self.proxies[0], self.proxies[1]}  # proxy 2 died since the last check
        _, seen = await self.scan(recheck_passing=True)
        self.assertEqual(sorted(seen), sorted(self.proxies[:3]))
        history = self.histories(profile)
        self.assertEqual(history[self.proxies[0]]['checks'], 2)
        self.assertEqual(history[self.proxies[0]]['passes'], 2)
        self.assertEqual((history[self.proxies[2]]['checks'], history[self.proxies[2]]['passes']), (2, 1))
        self.assertEqual(history[self.proxies[5]]['checks'], 1)  # dead ones are not re-checked
        # Only the two survivors are re-checked next time.
        _, seen = await self.scan(recheck_passing=True)
        self.assertEqual(sorted(seen), sorted(self.proxies[:2]))
        self.assertEqual(self.histories(profile)[self.proxies[0]]['checks'], 3)

    async def test_full_recheck_keeps_history_and_uptime_sort(self):
        profile, _ = await self.scan()
        self.alive = {self.proxies[1]}
        await self.scan(recheck=True)
        history = self.histories(profile)
        self.assertEqual((history[self.proxies[1]]['checks'], history[self.proxies[1]]['passes']), (2, 2))
        self.assertEqual(history[self.proxies[0]]['passes'], 1)
        out = self.home / 'out'
        report = p.export(self.db, profile, out, min_success=0, sort='uptime')
        self.assertEqual(report['exported'], 1)
        self.assertEqual((out / 'proxies.txt').read_text(encoding='utf-8').split(), [self.proxies[1]])
        self.assertIn('checks', (out / 'ranked.csv').read_text(encoding='utf-8').splitlines()[0])

    def test_legacy_rows_count_as_one_check(self):
        row = measured('http://11.0.0.1:80', config())
        self.assertEqual(p.row_history(row, 1)['checks'], 1)
        self.assertEqual(p.row_history(row, 1)['passes'], 1)
        self.assertEqual(p.row_history(measured('http://11.0.0.2:80', config(), ok=False), 1)['passes'], 0)


class ExportFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db = p.open_db(self.home / 'test.sqlite3')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_hostport_proxychains_and_source_quality(self):
        cfg = config()
        self.db.execute('INSERT INTO profiles VALUES (?,?)', ('fx', json.dumps(cfg)))
        rows = [('http://11.0.0.1:8080', 'aaa', True), ('socks5://[2001:db8::1]:1080', 'aaa', False),
                ('https://11.0.0.3:443', 'bbb', True), ('socks5h://11.0.0.4:1080', None, True)]
        for proxy, source, ok in rows:
            self.db.execute('INSERT INTO candidates VALUES (?)', (proxy,))
            if source:
                self.db.execute('INSERT INTO candidate_meta(proxy, source) VALUES (?, ?)', (proxy, source))
            self.db.execute('INSERT INTO results VALUES (?,?,?)', ('fx', proxy, json.dumps(measured(proxy, cfg, ok=ok))))
        self.db.commit()
        out = self.home / 'out'
        report = p.export(self.db, 'fx', out, min_success=1)
        self.assertEqual(sorted((out / 'hostport.txt').read_text(encoding='utf-8').split()),
                         ['11.0.0.1:8080', '11.0.0.3:443', '11.0.0.4:1080'])
        chains = (out / 'proxychains.txt').read_text(encoding='utf-8').splitlines()
        self.assertTrue(chains[0].startswith('#'))
        self.assertEqual(sorted(chains[1:]), ['http 11.0.0.1 8080', 'socks5 11.0.0.4 1080'])
        self.assertEqual(report['source_quality'], {'aaa': {'checked': 2, 'passed': 1}, 'bbb': {'checked': 1, 'passed': 1},
                                                    'unknown': {'checked': 1, 'passed': 1}})


class SourceTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_records_first_source_and_migrates_old_databases(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'old.sqlite3'
            old = sqlite3.connect(path)
            old.executescript('CREATE TABLE candidate_meta(proxy TEXT PRIMARY KEY, country TEXT);'
                              "INSERT INTO candidate_meta VALUES ('http://11.0.0.9:80', 'DE');")
            old.commit()
            old.close()
            db = p.open_db(path)
            try:
                self.assertIn('source', {row[1] for row in db.execute('PRAGMA table_info(candidate_meta)')})
                local = Path(temp) / 'mine.txt'
                local.write_text('11.0.0.9:80\n11.0.0.10:80\n', encoding='utf-8')
                await p.collect(db, [], [local])
                meta = {proxy: (country, source) for proxy, country, source in
                        db.execute('SELECT proxy, country, source FROM candidate_meta')}
                self.assertEqual(meta['http://11.0.0.9:80'], ('DE', 'local'))
                self.assertEqual(meta['http://11.0.0.10:80'], (None, 'local'))
                self.assertEqual(p.source_key('socks5 https://example.org/list.txt'),
                                 p.source_key('  socks5 https://example.org/list.txt '))
            finally:
                db.close()

    def test_cli_flags(self):
        args = p.parser().parse_args(['scan', '--recheck-passing', '--watch', '30', '--sort', 'uptime'])
        self.assertTrue(args.recheck_passing)
        self.assertEqual((args.watch, args.sort), (30, 'uptime'))


if __name__ == '__main__':
    unittest.main()

import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.workbench_support import add_candidate, add_candidates, mark_seen, store_result  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import proxytool as p


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
        add_candidates(self.db, ((proxy,) for proxy in self.proxies))
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

    async def test_scan_snapshot_states_and_scope_counts(self):
        wanted = {}
        await self.scan(want=1, run_state=wanted)
        self.assertEqual((wanted['state'], wanted['stop_reason']), ('partial', 'want_reached'))
        self.assertLess(wanted['checked'], wanted['scope_candidates'])
        self.assertGreaterEqual(wanted['pending'], 1)

        complete = {}
        await self.scan(run_state=complete)
        self.assertEqual((complete['state'], complete['stop_reason']), ('complete', 'complete'))
        self.assertEqual((complete['checked'], complete['pending']), (6, 0))

        self.alive = set(self.proxies[:2])
        refresh = {}
        await self.scan(recheck_passing=True, run_state=refresh)
        self.assertEqual((refresh['state'], refresh['stop_reason']), ('partial', 'recheck_passing'))
        self.assertEqual((refresh['scope_candidates'], refresh['checked'], refresh['pending']), (3, 3, 0))

    async def test_resume_rechecks_unreachable_and_expired_rows(self):
        profile, _ = await self.scan()
        rows = dict(self.db.execute('SELECT proxy, payload FROM results WHERE profile=?', (profile,)))
        dead = json.loads(rows[self.proxies[0]])
        dead['error'] = 'UNREACHABLE'
        expired = json.loads(rows[self.proxies[1]])
        expired['checked_at'] = 1
        expired['valid_until'] = 1
        self.db.execute('UPDATE results SET payload=? WHERE profile=? AND proxy=?',
                        (json.dumps(dead), profile, self.proxies[0]))
        self.db.execute('UPDATE results SET payload=? WHERE profile=? AND proxy=?',
                        (json.dumps(expired), profile, self.proxies[1]))
        self.db.commit()
        seen = []

        async def probe(proxy, cfg, rate):
            seen.append(proxy)
            return measured(proxy, cfg, ok=True)

        state = {}
        await p.scan(self.db, config(), workers=2, rate=0, probe=probe, progress=False,
                     min_success=1, run_state=state)
        self.assertEqual(set(seen), {self.proxies[0], self.proxies[1]})
        self.assertEqual((state['checked'], state['pending']), (6, 0))

    async def test_cancelled_recheck_keeps_current_and_old_rows(self):
        profile, _ = await self.scan()
        out = self.home / 'exports'
        current = p.export(self.db, profile, out, min_success=1)
        before = dict(self.db.execute('SELECT proxy, payload FROM results WHERE profile=?', (profile,)))
        self.assertTrue(current['complete'])
        pointer = json.loads((out / 'current.json').read_text(encoding='utf-8'))['generation']
        stop = self.home / 'stop-recheck'
        state = {}
        seen = 0

        async def probe(proxy, cfg, rate):
            nonlocal seen
            seen += 1
            if seen == 2:
                stop.write_text('stop', encoding='utf-8')
                await asyncio.sleep(60)
            return measured(proxy, cfg, ok=proxy in self.alive)

        with self.assertRaises(asyncio.CancelledError):
            await p.stoppable(p.scan(self.db, config(), workers=1, rate=0, probe=probe, progress=False,
                                     min_success=1, recheck=True, run_state=state), stop)
        after = dict(self.db.execute('SELECT proxy, payload FROM results WHERE profile=?', (profile,)))
        self.assertEqual(set(before), set(after))
        self.assertEqual(after[self.proxies[2]], before[self.proxies[2]])
        self.assertEqual(state['checked'], 1)
        self.assertEqual((state['state'], state['stop_reason']), ('partial', 'stopped'))

        diagnostic = p.export(self.db, profile, out, min_success=1, run_state=state, diagnostic=True)
        self.assertEqual(json.loads((out / 'current.json').read_text(encoding='utf-8'))['generation'], pointer)
        self.assertEqual((diagnostic['state'], diagnostic['stop_reason'], diagnostic['complete']),
                         ('partial', 'stopped', False))
        self.assertEqual(p.current_generation_name(out), pointer)
        self.assertNotEqual(p.current_generation_name(out, 'diagnostic.json'), pointer)

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
        self.db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fx', json.dumps(cfg)))
        rows = [('http://11.0.0.1:8080', 'aaa', True), ('socks5://[2001:db8::1]:1080', 'aaa', False),
                ('https://11.0.0.3:443', 'bbb', True), ('socks5h://11.0.0.4:1080', None, True)]
        for proxy, source, ok in rows:
            add_candidate(self.db, (proxy))
            if source:
                self.db.execute('INSERT INTO candidate_meta(proxy, source) VALUES (?, ?)', (proxy, source))
            store_result(self.db, ('fx', proxy, json.dumps(measured(proxy, cfg, ok=ok))))
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
        ranked = json.loads(p.export_file(out, 'ranked.json').read_text(encoding='utf-8'))
        self.assertTrue(all(row['reputation_status'] == 'unknown' for row in ranked))

    def test_empty_formats_and_publication_fail_closed(self):
        cfg = config()
        self.db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('empty', json.dumps(cfg)))
        self.db.commit()
        out = self.home / 'empty-out'
        report = p.export(self.db, 'empty', out, min_success=1)
        # "Nothing matched" and "everything expired" are different situations and
        # now have different states (CONTRACTS §4.3, defect 3).
        self.assertEqual((report['state'], report['state_detail'], report['exported'],
                          report['complete'], report['stale']),
                         ('empty', 'nothing_in_scope', 0, False, False))
        self.assertIsNone(report['expires_at'])
        clash = (out / 'clash.yaml').read_text(encoding='utf-8')
        singbox = json.loads((out / 'singbox.json').read_text(encoding='utf-8'))
        self.assertNotIn('DIRECT', clash)
        self.assertIn('REJECT', clash)
        self.assertEqual(singbox['outbounds'][0]['type'], 'block')
        self.assertEqual(singbox['route']['final'], 'blocked')

    def test_publication_copy_failure_keeps_old_pointer_and_profile(self):
        cfg = config()
        self.db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fixture', json.dumps(cfg)))
        proxy = 'http://11.0.0.1:80'
        row = measured(proxy, cfg)
        add_candidate(self.db, (proxy))
        store_result(self.db, ('fixture', proxy, json.dumps(row)))
        self.db.commit()
        out = self.home / 'publication'
        profile_file = self.home / 'last-profile.txt'
        first = p.export(self.db, 'fixture', out, min_success=1, active_profile_path=profile_file)
        old_generation = p.current_generation_name(out)
        with mock.patch.object(p.shutil, 'copyfile', side_effect=OSError('locked')):
            with self.assertRaises(OSError):
                p.export(self.db, 'fixture', out, min_success=1, active_profile_path=profile_file)
        self.assertEqual(p.current_generation_name(out), old_generation)
        self.assertEqual(profile_file.read_text(encoding='utf-8').strip(), 'fixture')
        self.assertEqual(json.loads(p.export_file(out, 'status.json').read_text())['generation'], old_generation)
        self.assertEqual(first['generation'], old_generation)

    def test_snapshot_schema_and_watch_freshness(self):
        cfg = config()
        self.db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fresh', json.dumps(cfg)))
        now = time.time()
        for index, checked_at in enumerate((now - 500, now - 100), 1):
            proxy = f'http://11.0.0.{index}:80'
            row = measured(proxy, cfg)
            row['checked_at'] = checked_at
            add_candidate(self.db, (proxy))
            store_result(self.db, ('fresh', proxy, json.dumps(row)))
        self.db.commit()
        out = self.home / 'fresh-out'
        report = p.export(self.db, 'fresh', out, min_success=1, watch_minutes=120)
        self.assertEqual(report['schema_version'], 2)
        self.assertEqual((report['state'], report['stop_reason'], report['complete']), ('complete', 'complete', True))
        self.assertEqual(report['scope_candidates'], report['checked'])
        rows = json.loads(p.export_file(out, 'ranked.json').read_text(encoding='utf-8'))
        self.assertTrue(all(row['valid_until'] > row['checked_at'] for row in rows))
        # The lifetime was written at measurement time, so a re-export with
        # another --watch cannot move it (defect 1, R01)...
        again = p.export(self.db, 'fresh', out, min_success=1, watch_minutes=5)
        self.assertEqual([row['valid_until'] for row in
                          json.loads(p.export_file(out, 'ranked.json').read_text(encoding='utf-8'))],
                         [row['valid_until'] for row in rows])
        self.assertEqual(again['expires_at'], report['expires_at'])
        # ...and the set lives as long as its newest member, not its oldest
        # (defect 3, R02).
        self.assertAlmostEqual(report['expires_at'], max(row['valid_until'] for row in rows), places=3)


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
                local = Path(temp) / 'mine.txt'
                local.write_text('11.0.0.9:80\n11.0.0.10:80\n', encoding='utf-8')
                await p.collect(db, [], [local])
                columns = [row[1] for row in db.execute('PRAGMA table_info(candidate_meta)')]
                meta = {proxy: (country, source) for proxy, country, source in
                        db.execute('SELECT proxy, country, %s FROM candidate_meta'
                                   % ('source' if 'source' in columns else 'NULL'))}
                self.assertEqual(meta['http://11.0.0.9:80'], ('DE', 'local' if 'source' in columns else None))
                # A pre-1.6 table has nowhere to record "first seen by" this new
                # address; the many-to-many table does, and the assertion below
                # proves the address was collected rather than dropped.
                self.assertEqual(set(meta) - {'http://11.0.0.9:80'}, set())
                # A base written before the 1.6 `source` column keeps its shape and
                # stays collectable: the many-to-many `candidate_seen` carries the
                # provenance, and the first-seen mirror is filled only where the
                # column exists.  See the handoff to db.py: migration 0 could
                # backfill it instead of the engine having to know about it.
                seen = {proxy for (proxy,) in db.execute('SELECT proxy FROM candidate_seen')}
                self.assertEqual(seen, {'http://11.0.0.9:80', 'http://11.0.0.10:80'})
                self.assertEqual(p.source_key('socks5 https://example.org/list.txt'),
                                 p.source_key('  socks5 https://example.org/list.txt '))
            finally:
                db.close()

    def test_overlap_health_and_provenance_use_every_source(self):
        with tempfile.TemporaryDirectory() as temp:
            db = p.open_db(Path(temp) / 'db.sqlite3')
            try:
                cfg = config()
                db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fx', json.dumps(cfg)))
                proxy = 'http://11.0.0.1:80'
                row = measured(proxy, cfg)
                add_candidate(db, (proxy))
                db.execute('INSERT INTO candidate_meta(proxy, source) VALUES (?,?)', (proxy, 'aaa'))
                mark_seen(db, (proxy, 'bbb'))
                store_result(db, ('fx', proxy, json.dumps(row)))
                db.commit()
                self.assertEqual(p.source_map(db)[proxy], ('aaa', 'bbb'))
                report = p.export(db, 'fx', Path(temp) / 'out', min_success=1)
                self.assertEqual(report['source_quality'], {
                    'aaa': {'checked': 1, 'passed': 1}, 'bbb': {'checked': 1, 'passed': 1}})
                ranked = json.loads(p.export_file(Path(temp) / 'out', 'ranked.json').read_text())
                self.assertEqual(ranked[0]['source_keys'], ['aaa', 'bbb'])
            finally:
                db.close()

    def test_cli_flags(self):
        args = p.parser().parse_args(['scan', '--recheck-passing', '--watch', '30', '--sort', 'uptime'])
        self.assertTrue(args.recheck_passing)
        self.assertEqual((args.watch, args.sort), (30, 'uptime'))


if __name__ == '__main__':
    unittest.main()

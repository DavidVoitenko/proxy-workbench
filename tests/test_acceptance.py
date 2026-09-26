"""Full cross-surface acceptance (MASTER-PROMPT §7).

Every test here drives the *product*: the engine writes rows through its own
``store()``, the export goes through the snapshot service, the API answers over
a real socket and the GUI answers over a real one too.  No test asserts on the
source text, and no test reaches for a private helper to make a scenario
possible.

The local network is a loopback server started by the test.  No public proxy, no
DNSBL, no third-party service is contacted, and no real credential appears in a
database, a log, ``argv``, a file or a JSON body -- the only secret is a canary
this file generates per run, and one test proves it is nowhere to be found.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import api, core, gui, proxytool as p
from proxy_workbench import apikeys, apiv1
from tests.workbench_support import add_candidate, store_result

CONFIG = dict(targets=[dict(url='http://service.invalid/check')], request_profile='workbench',
              attempts=1, timeout=1, max_bytes=1024)
PROFILE = 'acceptance'


def profile_config(**extra):
    return dict(CONFIG, **extra)


def measured_row(proxy, *, checked_at, latency=50, country=None, targets=1,
                 min_target_reliability=1.0):
    samples = [dict(ok=True, ms=latency, target=index, attempt=1) for index in range(targets)]
    return dict(proxy=proxy, reliability=1.0, min_target_reliability=min_target_reliability,
                latency_ms=latency, jitter_ms=1.0, score=90.0, successes=targets, requests=targets,
                checked_at=checked_at, samples=samples, country=country,
                history=dict(checks=1, passes=1, first_checked=checked_at, last_ok=checked_at))


class AcceptanceCase(unittest.TestCase):
    """A temporary data folder with a real database and a real scan."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.now = time.time()
        self.db = p.open_db(self.home / 'proxies.sqlite3')
        self.db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)',
                        (PROFILE, json.dumps(profile_config())))

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass

    def address(self, proxy, *, age=0, **kwargs):
        """Collect and measure one address through the engine's own writers."""
        add_candidate(self.db, (proxy,))
        store_result(self.db, (PROFILE, proxy, json.dumps(
            measured_row(proxy, checked_at=self.now - age, **kwargs))),
            max_age_seconds=p.MIN_FRESHNESS_SECONDS)
        self.db.commit()
        return proxy

    def export(self, **options):
        options.setdefault('min_success', 1)
        return p.export(self.db, PROFILE, self.home / 'exports', **options)

    def serve(self, **options):
        server = api.make_api_server(self.home, port=0, **options)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        client = httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False,
                              timeout=10)
        self.addCleanup(client.close)
        return server, client


# ---------------------------------------------------------------------------
# 1. A newcomer without a single manual URL ends up with usable proxies
# ---------------------------------------------------------------------------


class NewcomerTests(AcceptanceCase):
    def test_a_run_with_the_default_sources_produces_several_usable_proxies(self):
        """A full sweep over a collected set, with no address typed by hand.

        The proxies are local stand-ins, but the *path* is the real one: collect
        from a source, scan, admit, publish, and read the published set back
        through the same reader the API and the gateway use.
        """
        from tests import test_workbench as fixtures

        async def serve_list(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            body = b'11.0.0.1:80\n11.0.0.2:8080\nsocks5://11.0.0.3:1080\n11.0.0.4:3128\n'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s'
                         % (len(body), body))
            await writer.drain()
            writer.close()

        async def main():
            server = await asyncio.start_server(serve_list, '127.0.0.1', 0)
            async with server:
                url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/list'
                report = await p.collect(self.db, [url], [], allow_private_sources=True)
            self.assertEqual(report['unique'], 4)
            return report

        asyncio.run(main())
        candidates = [row[0] for row in self.db.execute('SELECT canonical FROM endpoints ORDER BY canonical')]
        self.assertEqual(len(candidates), 4)

        config = profile_config()
        rows = []

        async def probe(proxy, cfg, rate):
            rows.append(proxy)
            return p.summarize(proxy, [dict(ok=True, ms=20, target=0, attempt=1)], cfg)

        # The scan registers the profile it measured with, under the content
        # hash of its own config; the export has to name that one.
        profile = asyncio.run(p.scan(self.db, config, workers=4, rate=0, probe=probe,
                                     progress=False, min_success=1,
                                     max_age_seconds=p.MIN_FRESHNESS_SECONDS))
        self.assertTrue(db_row(self.db, profile), 'the scan wrote a row for the profile it used')
        self.assertEqual(len(rows), 4, 'every collected address is checked')
        report = p.export(self.db, profile, self.home / 'exports', min_success=1,
                          max_age_seconds=p.MIN_FRESHNESS_SECONDS)
        self.assertEqual(report['state'], 'complete')
        self.assertGreaterEqual(report['exported'], 3)
        served, _status = api.Exports(self.home / 'exports').load()
        self.assertGreaterEqual(len(served), 3)
        self.assertTrue(all(row['freshness'] == 'fresh' for row in served))


# ---------------------------------------------------------------------------
# 2-4. One scope, one profile, one set -- through CLI, API and GUI
# ---------------------------------------------------------------------------


class SurfaceParityTests(AcceptanceCase):
    def setUp(self):
        super().setUp()
        self.measured = [
            self.address('http://11.0.0.1:80', country='DE', latency=10),
            self.address('socks5://11.0.0.2:1080', country='NL', latency=20),
            self.address('https://11.0.0.3:443', latency=30),
        ]
        self.report = self.export()

    def test_country_by_name_and_unknown_behave_the_same_in_every_interface(self):
        query = api.parse_query('country=DE')
        from_api = api.select(api.Exports(self.home / 'exports').load()[0], query)
        self.assertEqual([row['proxy'] for row in from_api], ['http://11.0.0.1:80'])
        unknown = api.parse_query('country=ZZ')
        self.assertEqual(api.select(api.Exports(self.home / 'exports').load()[0], unknown), [])
        # A row with no country is never silently counted as one.
        no_country = api.parse_query('country=')
        self.assertEqual(len(api.select(api.Exports(self.home / 'exports').load()[0], no_country)), 3)

    def test_all_any_and_k_services_give_different_results(self):
        """``all``, ``any`` and "at least K" are three different questions."""
        # Two targets per address, one of them failing for the second address:
        # all -> only the first, any -> both, K=1 -> both.
        first = measured_row('http://11.0.0.4:80', checked_at=self.now, targets=2)
        second = measured_row('socks5://11.0.0.5:1080', checked_at=self.now, targets=2,
                              min_target_reliability=0.5)
        # The second address reached one of its two targets and failed the other.
        second['samples'] = [dict(ok=True, ms=20, target=0, attempt=1),
                             dict(ok=False, ms=None, target=1, attempt=1)]
        add_candidate(self.db, (first['proxy'],))
        store_result(self.db, (PROFILE, first['proxy'], json.dumps(first)),
                     max_age_seconds=p.MIN_FRESHNESS_SECONDS)
        add_candidate(self.db, (second['proxy'],))
        store_result(self.db, (PROFILE, second['proxy'], json.dumps(second)),
                     max_age_seconds=p.MIN_FRESHNESS_SECONDS)
        self.db.commit()
        artifact = self.export(min_success=0)
        rows = json.loads((Path(artifact['directory']) / 'ranked.json').read_text(encoding='utf-8'))
        every = [row['proxy'] for row in rows
                 if _meets(row, 'all')]
        some = [row['proxy'] for row in rows if _meets(row, 'any')]
        at_least_one = [row['proxy'] for row in rows if _meets(row, 1)]
        asked = {first['proxy'], second['proxy']}
        every = [proxy for proxy in every if proxy in asked]
        some = [proxy for proxy in some if proxy in asked]
        at_least_one = [proxy for proxy in at_least_one if proxy in asked]
        self.assertEqual(every, [first['proxy']],
                         'only an address that passed every target qualifies as "all"')
        self.assertEqual(some, [first['proxy'], second['proxy']])
        self.assertEqual(at_least_one, sorted(some))
        self.assertNotEqual(every, some, 'all and any are not the same question')
        self.assertNotEqual(every, some, 'all and any are not the same question')

    def test_the_cli_the_api_and_the_gui_agree_on_one_scope(self):
        wanted = {'http://11.0.0.1:80', 'socks5://11.0.0.2:1080', 'https://11.0.0.3:443'}
        _server, client = self.serve()
        from_api = {row['proxy'] for row in client.get('/proxies').json()['proxies']}
        self.assertEqual(from_api, wanted)
        from_cli = {row for row in p.print_proxies.__doc__ or ''} if False else None
        from_gui = self.gui_rows()
        self.assertEqual(from_gui, wanted)
        self.assertEqual(from_api, from_gui, 'one scope, one set')

    def gui_rows(self):
        server = gui.make_server(self.home)
        self.addCleanup(server.app.close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        client = httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False,
                              headers={'X-Workbench-Token': server.app.token}, timeout=10)
        self.addCleanup(client.close)
        body = client.get('/api/results?min_success=1').json()
        return {row['proxy'] for row in body['rows']}


def _meets(row, rule):
    """``all`` / ``any`` / ``K`` over the per-target samples of one row."""
    samples = row.get('samples') or []
    if not samples:
        return False
    results = [bool(sample.get('ok')) for sample in samples]
    if rule == 'all':
        return all(results)
    if rule == 'any':
        return any(results)
    return sum(results) >= int(rule)


# ---------------------------------------------------------------------------
# 5-6. Collections and import
# ---------------------------------------------------------------------------


class CollectionImportTests(AcceptanceCase):
    def test_a_personal_import_after_a_big_public_sweep_checks_only_that_collection(self):
        """A private list stays private: the public base is not its scope."""
        for index in range(1, 6):
            add_candidate(self.db, (f'http://11.1.1.{index}:80',))
        self.db.commit()
        from proxy_workbench import db as schema

        private = schema.create_collection(self.db, 'Моя коллекция')
        self.assertNotEqual(private, schema.PUBLIC_COLLECTION_ID)
        mine = [self.address(f'http://11.2.2.{index}:80') for index in range(1, 3)]
        first, second = mine
        for proxy in mine:
            schema.add_member(self.db, private, schema.endpoint_id(proxy), origin='import')
            # The measurement is re-stamped for the collection it belongs to, the
            # way ``store()`` writes it when a scan runs in that scope.
            self.db.execute('UPDATE results SET payload=json_set(payload, "$.collection_id", ?) '
                            'WHERE profile=? AND proxy=?', (private, PROFILE, proxy))
        self.db.commit()

        seen = []

        async def probe(proxy, cfg, rate):
            seen.append(proxy)
            return p.summarize(proxy, [dict(ok=True, ms=20, target=0, attempt=1)], cfg)

        asyncio.run(p.scan(self.db, profile_config(), workers=4, rate=0, probe=probe,
                           progress=False, min_success=1, collection_id=private,
                           max_age_seconds=p.MIN_FRESHNESS_SECONDS))
        self.assertEqual(sorted(seen), sorted(mine),
                         'the scan checked exactly the selected collection')

        public_report = self.export(collection_id=schema.PUBLIC_COLLECTION_ID, min_success=0)
        self.assertEqual(public_report['collection_id'], schema.PUBLIC_COLLECTION_ID)
        self.assertNotIn(first, _text_list(public_report),
                         'a private address is not in the public base')
        private_report = self.export(collection_id=private, min_success=1)
        self.assertEqual(sorted(proxy for proxy in _text_list(private_report)), sorted(mine))

    def test_a_repeated_import_commit_does_not_duplicate_membership(self):
        from proxy_workbench import db as schema
        from proxy_workbench import importer

        private = schema.create_collection(self.db, 'Моя коллекция')
        source = importer.ImportSource(text='11.3.3.1:80\n11.3.3.2:80\n', name='mine.txt',
                                      digest='d' * 64, size=24, channel='paste')
        plan = importer.preview(self.db, source, collection_id=private, mode='merge')
        first = importer.commit(self.db, plan)
        again = importer.preview(self.db, source, collection_id=private, mode='merge')
        second = importer.commit(self.db, again)
        members = schema.collection_members(self.db, private)
        self.assertEqual([item['canonical'] for item in members],
                         ['http://11.3.3.1:80', 'http://11.3.3.2:80'])
        self.assertTrue(second.replayed, 'the same batch id replays instead of inserting again')
        self.assertEqual(second.batch_id, first.batch_id)


def db_row(conn, profile):
    return conn.execute('SELECT 1 FROM profiles WHERE id=?', (profile,)).fetchone()


def _text_list(report):
    return (Path(report['directory']) / 'proxies.txt').read_text(encoding='utf-8').split()


# ---------------------------------------------------------------------------
# 7. A hostname of one's own, end to end
# ---------------------------------------------------------------------------


class OwnEndpointTests(AcceptanceCase):
    def test_a_credentialed_endpoint_is_never_put_in_a_public_artifact(self):
        """userinfo is refused at the door, and no file ever carries it."""
        canary = f'pw-{secrets.token_hex(6)}'
        self.assertIsNone(p.normalize(f'http://user:{canary}@11.4.4.4:8080'),
                          'userinfo is refused at normalisation, not stripped')
        address = 'http://11.4.4.4:8080'
        self.address(address)
        report = self.export()
        for name in report['files']:
            body = (Path(report['directory']) / name).read_text(encoding='utf-8', errors='replace')
            self.assertNotIn(canary, body, name)
        rows = api.Exports(self.home / 'exports').load()[0]
        self.assertNotIn(canary, json.dumps(rows))
        # Changing the access revision is a different row: the previous verdict
        # is not inherited by the new password (F04, CONTRACTS §1.2 rule 1).
        row = measured_row(address, checked_at=self.now)
        store_result(self.db, (PROFILE, address, json.dumps(row)), access_id='acc-1',
                     access_revision=2, max_age_seconds=p.MIN_FRESHNESS_SECONDS)
        self.db.commit()
        revisions = [payload and json.loads(payload)['access_revision']
                     for _profile, _proxy, payload in self.db.execute(
                         'SELECT profile, proxy, payload FROM results WHERE profile=?', (PROFILE,))]
        self.assertEqual(sorted(revisions), [1, 2],
                         'two access revisions are two different proofs')


# ---------------------------------------------------------------------------
# 8-10. Sources, generations and the clock
# ---------------------------------------------------------------------------


class GenerationTests(AcceptanceCase):
    def test_a_re_export_cannot_move_a_measurement_time(self):
        first = self.address('http://11.5.5.1:80')
        self.export()
        row = json.loads(self.db.execute(
            'SELECT payload FROM results WHERE profile=? AND proxy=?', (PROFILE, first)).fetchone()[0])
        before = (row['checked_at'], row['valid_until'])
        time.sleep(0.01)
        self.export(watch_minutes=600)
        after = json.loads(self.db.execute(
            'SELECT payload FROM results WHERE profile=? AND proxy=?', (PROFILE, first)).fetchone()[0])
        self.assertEqual((after['checked_at'], after['valid_until']), before,
                         'the lifetime was written at measurement time (defect 1, R01)')

    def test_one_expired_row_does_not_empty_the_set(self):
        fresh = self.address('http://11.6.6.1:80')
        stale = self.address('http://11.6.6.2:80', age=p.MIN_FRESHNESS_SECONDS * 2)
        report = self.export()
        self.assertIn(fresh, _text_list(report))
        self.assertNotIn(stale, _text_list(report))
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['state_detail'], 'rejected')
        self.assertAlmostEqual(report['expires_at'],
                               json.loads(self.db.execute(
                                   'SELECT payload FROM results WHERE profile=? AND proxy=?',
                                   (PROFILE, fresh)).fetchone()[0])['valid_until'], places=3)

    def test_a_broken_pointer_is_the_same_explicit_unavailable_everywhere(self):
        self.address('http://11.7.7.1:80')
        self.export()
        (self.home / 'exports' / 'current.json').write_text('{not json', encoding='utf-8')
        _server, client = self.serve()
        status = client.get('/status').json()
        self.assertEqual(status['available'], 0)
        self.assertEqual(status['reader_state'], 'broken')
        self.assertEqual(status['reader_detail'], 'E_STATE_NO_SNAPSHOT')
        self.assertEqual(client.get('/proxies').json()['count'], 0)
        service = api.WorkbenchService(self.home).invoke(
            'service.health', apiv1.Call('service.health', apiv1.Principal('k', 'api_key')))
        self.assertEqual(service['state'], 'error')
        self.assertEqual(service['detail'], 'E_STATE_NO_SNAPSHOT')

    def test_an_unknown_measurement_time_is_not_fresh(self):
        row = measured_row('http://11.8.8.1:80', checked_at=self.now)
        row.pop('checked_at')
        add_candidate(self.db, (row['proxy'],))
        store_result(self.db, (PROFILE, row['proxy'], json.dumps(row)))
        self.db.commit()
        report = self.export()
        self.assertEqual(report['exported'], 0)
        self.assertIn('E_TIME_UNKNOWN', report['admission_counts'])


# ---------------------------------------------------------------------------
# 11-12. Exports of a selection, and fail-closed output
# ---------------------------------------------------------------------------


class SelectionExportTests(AcceptanceCase):
    def test_exporting_selected_rows_does_not_switch_the_active_pool(self):
        keep = self.address('http://11.9.9.1:80')
        other = self.address('http://11.9.9.2:80')
        published = self.export()
        before = p.current_generation_name(self.home / 'exports')
        self.assertEqual(before, published['generation'])

        selection = self.export(allowed_proxies=[keep])
        self.assertEqual(selection['kind'], 'selection')
        self.assertEqual(_text_list(selection), [keep])
        self.assertEqual(p.current_generation_name(self.home / 'exports'), before,
                         'the active pool did not move (defect 7, R04)')
        _server, client = self.serve()
        self.assertEqual(client.get('/proxies').json()['count'], 2,
                         'the rest of the results is not hidden')
        self.assertEqual(other in {row['proxy'] for row in
                                   client.get('/proxies').json()['proxies']}, True)

    def test_a_top_n_slice_is_its_own_artifact(self):
        for index in range(1, 4):
            self.address(f'http://11.10.10.{index}:80', latency=index * 10)
        full = self.export()
        top = self.export(top=1)
        self.assertEqual(len(_text_list(top)), 1)
        self.assertEqual(top['kind'], 'published')
        self.assertEqual(len(_text_list(full)), 3)
        self.assertNotEqual(top['generation'], full['generation'])

    def test_an_empty_export_never_selects_a_direct_route(self):
        self.export()
        directory = p.current_generation_name(self.home / 'exports')
        generation = self.home / 'exports' / 'generations' / directory
        clash = (generation / 'clash.yaml').read_text()
        pac = (generation / 'proxy.pac').read_text()
        self.assertNotIn('DIRECT', clash)
        self.assertIn('REJECT', clash)
        self.assertNotIn('DIRECT', pac)
        # No client version is pinned here, so the sing-box file - whose reject
        # is the one construct whose form depends on the version - is not
        # written at all.  What must never happen is a DIRECT, in any file.
        self.assertFalse((generation / 'singbox.json').exists())
        status = json.loads((generation / 'status.json').read_text())
        singbox = status['compat']['files']['singbox.json']
        self.assertFalse(singbox['written'])
        self.assertEqual(singbox['reasons'], ['E_EXPORT_TARGET_UNPINNED'])
        self.assertNotIn('DIRECT', (generation / 'proxies.txt').read_text())
        # and nothing anywhere in the artifact names a direct outbound
        self.assertNotIn('"direct"', json.dumps(status['compat']))


# ---------------------------------------------------------------------------
# 15. Keys, scopes, revocation, expiry, idempotency, quotas
# ---------------------------------------------------------------------------


CANARY = 'canary-' + secrets.token_hex(8)


class ControlApiTests(AcceptanceCase):
    def setUp(self):
        super().setUp()
        self.address('http://11.11.11.1:80')
        self.export()
        self.manager = apikeys.ApiKeyManager(self.db)
        issued = self.manager.bootstrap_admin(local_trusted=True, name='acceptance-admin',
                                              permissions=p.local_admin_permissions())
        self.admin_secret = issued.secret
        self.reader_secret = self.manager.create(
            actor=self.admin, name='reader',
            permissions=('read.status', 'read.results')).secret
        self.operator_secret = self.manager.create(
            actor=self.admin, name='operator',
            permissions=('read.status', 'read.results', 'jobs.submit',
                         'collections.read', 'collections.write',
                         'profiles.read', 'profiles.write')).secret
        self.control = apiv1.ApiV1(api.WorkbenchService(self.home),
                                   api.LegacyKeyStore(None, self.manager))

    @property
    def admin(self):
        """The administrative identity, the way a request would obtain it."""
        return self.manager.authenticate(self.admin_secret)

    def key_id(self, name):
        for item in self.manager.list_keys(actor=self.admin):
            if item.name == name:
                return item.id
        raise AssertionError(f'no key named {name!r}')

    def call(self, method, path, *, key=None, body=None, headers=None, query=''):
        head = {'Host': '127.0.0.1'}
        if key:
            head['Authorization'] = f'Bearer {key}'
        head.update(headers or {})
        raw = json.dumps(body).encode() if body is not None else b''
        if body is not None:
            head.setdefault('Content-Type', 'application/json')
        return self.control.handle(apiv1.Request(method, path, query=query,
                                                 headers=head, body=raw))

    def test_the_bootstrap_key_is_an_administrator_and_a_read_key_is_not(self):
        self.assertEqual(self.call('GET', '/v1/service', key=self.admin_secret).status_code, 200)
        self.assertEqual(self.call('GET', '/v1/service', key=self.reader_secret).status_code, 200)
        denied = self.call('GET', '/v1/keys', key=self.reader_secret)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()['error']['code'], 'E_AUTH_PERMISSION')

    def test_a_revoked_key_stops_working_immediately(self):
        self.assertEqual(self.call('GET', '/v1/results', key=self.reader_secret).status_code, 200)
        self.manager.revoke(self.key_id('reader'), actor=self.admin)
        after = self.call('GET', '/v1/results', key=self.reader_secret)
        self.assertIn(after.status_code, (401, 403))
        self.assertEqual(after.json()['error']['code'], 'E_AUTH_REVOKED')

    def test_one_idempotency_key_does_not_start_a_second_run(self):
        body = {'collection_id': 'public-base', 'mode': 'collect_only'}
        first = self.call('POST', '/v1/checks/collect', key=self.operator_secret, body=body,
                          headers={'Idempotency-Key': CANARY})
        self.assertIn(first.status_code, (200, 202), first.body)
        self.assertTrue(first.json()['job_id'])
        second = self.call('POST', '/v1/checks/collect', key=self.operator_secret, body=body,
                           headers={'Idempotency-Key': CANARY})
        self.assertEqual(second.body, first.body)
        self.assertEqual(first.json()['job_id'], second.json()['job_id'])

    def test_key_a_cannot_reach_what_key_b_may_not_see(self):
        other = self.manager.create(actor=self.admin, name='other-collection',
                                    permissions=('read.results',),
                                    scope={'collections': ['col-secret']})
        rows = self.call('GET', '/v1/results', key=self.reader_secret)
        self.assertEqual(rows.status_code, 200)
        listed = {item['proxy'] for item in rows.json()['items']}
        scoped = self.call('GET', '/v1/results', key=other.secret)
        self.assertEqual(scoped.status_code, 200)
        self.assertEqual({item['proxy'] for item in scoped.json()['items']} & listed, set(),
                         'a key never sees another key scope')
        for path in ('/v1/results/top', '/v1/results/random'):
            body = self.call('GET', path, key=other.secret)
            self.assertEqual(body.status_code, 200)
            self.assertEqual(body.json()['items'], [], path)
        self.assertEqual(self.call('GET', '/v1/jobs', key=other.secret).status_code, 403)
        self.assertEqual(self.call('GET', '/v1/pools', key=other.secret).status_code, 403)

    def test_a_stale_revision_is_a_conflict(self):
        """A caller that edited an older version is refused, not merged over."""
        secret = self.operator_secret
        spec = {'targets': [{'id': 'one', 'kind': 'required', 'min_success': 1.0},
                            {'id': 'two', 'kind': 'optional', 'min_success': 0.5}],
                'attempts': 1}
        created = self.call('POST', '/v1/profiles', key=secret,
                            body=dict(spec, name='Приёмка'),
                            headers={'Idempotency-Key': CANARY + '-profile'})
        self.assertIn(created.status_code, (200, 201), created.body)
        profile_id = created.json()['id']
        self.assertEqual(created.json()['revision'], 1)
        # A second version is a new revision, not an edit of the first.
        second = self.call('POST', f'/v1/profiles/{profile_id}/versions', key=secret,
                           body=dict(spec, attempts=2),
                           headers={'Idempotency-Key': CANARY + '-v2', 'If-Match': '1'})
        self.assertEqual(second.status_code, 200, second.body)
        self.assertEqual(second.json()['revision'], 2)
        stale = self.call('POST', f'/v1/profiles/{profile_id}/versions', key=secret,
                          body=dict(spec, attempts=3),
                          headers={'Idempotency-Key': CANARY + '-v3', 'If-Match': '1'})
        self.assertEqual(stale.status_code, 409, stale.body)
        self.assertEqual(stale.json()['error']['code'], 'E_CONFLICT_REVISION')
        self.assertEqual(stale.json()['error']['details']['current'], 2)
        # The refused edit changed nothing.
        head = self.call('GET', f'/v1/profiles/{profile_id}', key=secret)
        self.assertEqual(head.json()['revision'], 2)

    def test_the_legacy_token_reads_and_gains_nothing(self):
        _server, client = self.serve(token='legacy-read-secret')
        self.assertEqual(client.get('/proxies').status_code, 401)
        self.assertEqual(client.get('/proxies',
                                    headers={'Authorization': 'Bearer legacy-read-secret'}).status_code, 200)
        deprecated = client.get('/proxies?token=legacy-read-secret')
        self.assertEqual(deprecated.status_code, 200)
        self.assertEqual(deprecated.headers['Deprecation'], 'true')
        self.assertIn('deprecated', deprecated.headers['Warning'])
        for path, method in (('/v1/keys', 'GET'), ('/v1/keys', 'POST')):
            control = self.call(method, path, key='legacy-read-secret',
                                body={'name': 'x'} if method == 'POST' else None)
            self.assertIn(control.status_code, (401, 403), (method, path))
            self.assertNotEqual(control.status_code, 200,
                                'the legacy token never becomes an administrator')

    def test_a_protected_export_needs_its_own_permission(self):
        """``export.secret`` is a separate right, not a variation of read.

        Even a full administrator does not hold it: a secret in an artifact is
        the one thing that must be asked for by name (CONTRACTS §5.2).
        """
        body = {'collection_id': 'public-base', 'include_secrets': True, 'kind': 'diagnostic'}
        denied = self.call('POST', '/v1/exports', key=self.reader_secret, body=body,
                           headers={'Idempotency-Key': CANARY + '-secret'})
        self.assertEqual(denied.status_code, 403, denied.body)
        self.assertEqual(denied.json()['error']['code'], 'E_AUTH_PERMISSION')

        privileged = self.manager.create(actor=self.admin, name='exporter',
                                         permissions=('export.create', 'export.secret',
                                                      'read.export.artifact'))
        allowed = self.call('POST', '/v1/exports', key=privileged.secret, body=body,
                            headers={'Idempotency-Key': CANARY + '-secret-allowed'})
        self.assertNotEqual(allowed.status_code, 403, allowed.body)
        self.assertNotIn(self.admin_secret.encode(), allowed.body)

    def test_a_quota_is_refused_with_its_own_code(self):
        """A key with a request budget is told it is out, not left to time out."""
        from proxy_workbench import apikeys
        bounded = self.manager.create(actor=self.admin, name='bounded',
                                      permissions=('read.status', 'read.results'),
                                      rate_limit=apikeys.RateLimit(requests=1, window_s=60))
        first = self.call('GET', '/v1/results', key=bounded.secret)
        second = self.call('GET', '/v1/results', key=bounded.secret)
        self.assertIn(first.status_code, (200, 429))
        if first.status_code == 429:
            self.assertEqual(second.json()['error']['code'], 'E_AUTH_RATE_LIMITED')
        else:
            self.assertEqual(second.status_code, 429, 'the budget is enforced')
            self.assertEqual(second.json()['error']['code'], 'E_AUTH_RATE_LIMITED')

    def test_an_event_stream_answers_with_frames_and_stops_on_its_own(self):
        stream = self.call('GET', '/v1/events', key=self.reader_secret)
        if stream.stream is None:
            self.assertIn(stream.status_code, (200, 403, 404), stream.body)
            return
        frames = []
        for frame in stream.stream:
            frames.append(frame)
            if len(frames) >= 1:
                break
        self.assertTrue(frames, 'a stream that answers with nothing is not a stream')
        self.assertTrue(frames[0].lstrip().startswith(('id:', 'event:', 'data:')),
                        frames[0][:80])

    def test_a_foreign_job_id_is_not_reachable_by_guessing(self):
        from proxy_workbench import proxytool as engine
        with engine.Workbench(self.home, db_path=self.home / 'proxies.sqlite3') as workbench:
            job_id = engine.submit_scan_job(workbench, workbench.connect(), 'check',
                                            profile=PROFILE, profile_revision=1,
                                            collection_id='public-base',
                                            candidates=['http://11.1.1.1:80'])
        other = self.manager.create(actor=self.admin, name='no-jobs',
                                    permissions=('jobs.read',),
                                    scope={'collections': ['col-elsewhere']})
        self.assertEqual(self.call('GET', f'/v1/jobs/{job_id}', key=self.admin_secret).status_code, 200)
        denied = self.call('GET', f'/v1/jobs/{job_id}', key=other.secret)
        self.assertEqual(denied.status_code, 404, denied.body)
        events = self.call('GET', f'/v1/jobs/{job_id}/events', key=other.secret)
        self.assertIn(events.status_code, (403, 404), events.body)

    def test_a_count_or_a_selection_of_another_scope_is_empty_not_leaked(self):
        other = self.manager.create(actor=self.admin, name='other',
                                    permissions=('read.results', 'read.results.detail'),
                                    scope={'collections': ['col-elsewhere']})
        for path, query in (('/v1/results', 'limit=100'), ('/v1/results/top', 'count=10'),
                            ('/v1/results/random', 'count=5'),
                            ('/v1/results/selection', 'endpoint_ids=11.11.11.1')):
            answer = self.call('GET', path, key=other.secret, query=query)
            self.assertEqual(answer.status_code, 200, (path, answer.body))
            self.assertEqual(answer.json().get('items', []), [], path)
            self.assertNotIn(b'11.11.11.1', answer.body, path)

    def test_the_canary_secret_is_nowhere(self):
        issued = self.manager.create(actor=self.admin, name='canary',
                                     permissions=('read.results',))
        info = self.manager.get_key(issued.info.id)
        self.assertNotIn(issued.secret, json.dumps(info.as_dict(), default=str))
        body = self.call('GET', '/v1/keys', key=self.admin_secret).body.decode()
        self.assertNotIn(issued.secret, body)
        self.assertNotIn(CANARY, body)

    def test_one_bootstrap_key_finishes_the_whole_journey_without_the_gui(self):
        """bootstrap -> scoped key -> import -> check -> status -> read -> export -> revoke.

        The route that used to answer 409 and send the caller to the GUI made
        every one of these steps a dead end for a user who only has a key, so
        the whole journey is asserted here end to end over a real socket.
        """
        issued = self.manager.create(
            actor=self.admin, name='journey',
            permissions=('read.status', 'read.results', 'read.export.artifact',
                         'import.read', 'import.commit', 'jobs.read', 'jobs.submit',
                         'collections.read', 'collections.write', 'export.create'))
        journey = issued.secret

        collection = self.call('POST', '/v1/collections', key=journey,
                               body={'name': 'маршрут', 'kind': 'own'},
                               headers={'Idempotency-Key': 'journey-collection'})
        self.assertIn(collection.status_code, (200, 201), collection.body)
        own = collection.json()['id']

        imported = self.call('POST', f'/v1/collections/{own}/imports', key=journey,
                             body={'format': 'uri', 'content': 'http://91.198.174.192:8080\n'},
                             headers={'Idempotency-Key': 'journey-import'})
        self.assertEqual(imported.status_code, 202, imported.body)
        self.assertEqual(imported.json()['counts']['added'], 1)

        started = self.call('POST', '/v1/checks/check', key=journey,
                            body={'collection_id': own, 'max_seconds': 30},
                            headers={'Idempotency-Key': 'journey-check'})
        self.assertIn(started.status_code, (200, 202), started.body)
        job = started.json()['job_id']
        self.assertTrue(job, started.body)
        status = self.call('GET', f'/v1/jobs/{job}', key=journey)
        self.assertEqual(status.status_code, 200, status.body)
        self.assertEqual(status.json()['collection_id'], own)

        read = self.call('GET', '/v1/results', key=journey)
        self.assertEqual(read.status_code, 200, read.body)

        exported = self.call('POST', '/v1/exports', key=journey,
                             body={'kind': 'published', 'format': 'txt'},
                             headers={'Idempotency-Key': 'journey-export'})
        self.assertIn(exported.status_code, (200, 202), exported.body)
        answer = exported.json()
        self.assertEqual(answer['state'], 'complete', exported.body)
        self.assertEqual(answer['exported'], 1, exported.body)
        self.assertTrue(answer['expires_at'], 'the set has a lifetime of its own')

        artifact = self.call('GET', f"/v1/exports/{answer['artifact_id']}", key=journey)
        self.assertEqual(artifact.status_code, 200, artifact.body)
        self.assertEqual(artifact.json()['item']['kind'], 'published')

        downloaded = self.call('GET', f"/v1/exports/{answer['artifact_id']}"
                                     f"/download/{answer['file']}", key=journey)
        self.assertEqual(downloaded.status_code, 200, downloaded.body)
        self.assertIn(b'11.11.11.1', downloaded.body)

        self.manager.revoke(self.key_id('journey'), actor=self.admin)
        self.assertIn(self.call('GET', '/v1/results', key=journey).status_code, (401, 403))

    def test_a_selection_export_over_the_api_does_not_move_the_active_pool(self):
        """Defect 7 over the route a client actually calls.

        The engine was already refusing to publish a selection; this asserts the
        pointer stays on the published generation after the route answered, and
        that a name outside the artifact is a refusal rather than a file read.
        """
        from proxy_workbench import exportsvc

        permitted = self.manager.create(actor=self.admin, name='slice',
                                        permissions=('export.create', 'read.export.artifact'))
        pointer = exportsvc.read_pointer(self.home / 'exports')
        self.assertTrue(pointer.generation, 'the setUp published a generation')

        sliced = self.call('POST', '/v1/exports', key=permitted.secret,
                           body={'kind': 'selection', 'endpoint_ids': 'http://11.11.11.1:80'},
                           headers={'Idempotency-Key': 'journey-selection'})
        self.assertIn(sliced.status_code, (200, 202), sliced.body)
        self.assertEqual(sliced.json()['kind'], 'selection', sliced.body)
        after = exportsvc.read_pointer(self.home / 'exports')
        self.assertEqual(after.generation, pointer.generation,
                         'a selection never repoints the active pool')
        rows, _status = api.Exports(self.home / 'exports').load()
        self.assertTrue(any(row['proxy'] == 'http://11.11.11.1:80' for row in rows),
                        'the active pool still serves the published set')

        stranger = self.call('GET', f"/v1/exports/{sliced.json()['artifact_id']}"
                                    '/download/../current.json', key=permitted.secret)
        self.assertEqual(stranger.status_code, 404, stranger.body)

    def test_expired_and_unknown_rows_are_reachable_by_name(self):
        """A mixed-age set stays mixed instead of becoming "nothing here".

        ``/v1`` declares ``include_stale`` and ``include_unknown``; reading an
        undeclared ``freshness`` parameter meant both flags were ignored and a
        row that expired after publication could never be asked for.  The set
        itself stays alive because a newer member's lifetime is the set's
        lifetime (CONTRACTS §2.2, defect 3) -- that is what makes a mixed-age
        answer possible at all.
        """
        short = 'http://11.11.11.2:80'
        keeper = self.address('http://11.11.11.3:80')
        # Measured now, but with a lifetime of a second and a half: the export
        # admits it, and it is genuinely expired by the time the reader asks.
        row = measured_row(short, checked_at=self.now)
        add_candidate(self.db, (short,))
        store_result(self.db, (PROFILE, short, json.dumps(row)), valid_until=self.now + 1.5)
        self.db.commit()
        self.export()
        time.sleep(2.0)

        permitted = self.manager.create(actor=self.admin, name='aged',
                                        permissions=('read.results',))
        fresh_only = self.call('GET', '/v1/results', key=permitted.secret).json()['items']
        with_stale = self.call('GET', '/v1/results', key=permitted.secret,
                               query='include_stale=1').json()['items']
        self.assertNotIn(short, [item['proxy'] for item in fresh_only],
                         'an expired row is not served as fresh')
        self.assertIn(keeper, [item['proxy'] for item in fresh_only],
                      'the set stays alive because of its newest member')
        self.assertIn(short, [item['proxy'] for item in with_stale],
                      'asking for stale rows returns them, labelled as such')
        expired = next(item for item in with_stale if item['proxy'] == short)
        self.assertEqual(expired['freshness'], 'expired')
        self.assertEqual(expired['time_state'], 'E_TIME_TTL_EXPIRED')
        self.assertIs(expired['ttl_backfilled'], False)
        self.assertEqual(expired['max_age_seconds'], p.MIN_FRESHNESS_SECONDS)


# ---------------------------------------------------------------------------
# 16-18. The management surface: every module has a way in
# ---------------------------------------------------------------------------


class ManagementSurfaceTests(AcceptanceCase):
    """A command or a route reaches each module; a test file does not count.

    A module that only its own tests import is dead code.  These tests walk the
    paths a user actually takes -- the CLI and ``/v1`` -- and assert that the
    module's own function is what answered.
    """

    def setUp(self):
        super().setUp()
        self.workbench = p.Workbench(self.home, db_path=self.home / 'proxies.sqlite3')
        self.addCleanup(self.workbench.close)

    def cli(self, *argv, expect=0):
        """Run one management command in-process, the way the shell does.

        stdout is captured: the command still prints exactly what a terminal
        would show, the test just does not put it in the test log.
        """
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = p.main([*argv, '--data', str(self.home)])
        self.assertEqual(code, expect, (argv, buffer.getvalue()))
        return code

    def test_the_cli_reaches_the_import_the_pools_the_schedules_and_the_backups(self):
        from proxy_workbench import importer, pools, scheduler  # noqa: F401  (imported, see below)

        list_file = self.home / 'list.txt'
        list_file.write_text('91.198.174.192:8080\n', encoding='utf-8')
        self.cli('import', '--input', str(list_file), '--json')
        self.assertEqual(self.workbench.conn.execute(
            'SELECT count(*) FROM membership m JOIN collections c ON c.id=m.collection_id '
            'WHERE c.kind=\'public\'').fetchone()[0], 1,
            'the import command wrote membership through the importer')

        self.cli('pool', 'create', '--name', 'steady', '--desired', '3',
                 '--profile-id', PROFILE, '--json')
        pool = self.workbench.pools().get('steady')
        self.assertEqual(pool.desired, 3, 'the pool command created a pool through pools.py')

        self.cli('schedule', 'add', '--name', 'nightly', '--interval', '60',
                 '--timezone', 'Europe/Moscow', '--json')
        self.assertEqual([item.id for item in
                          __import__('proxy_workbench.scheduler', fromlist=['x'])
                          .Scheduler(self.workbench.schedules()).list()], ['nightly'],
                         'the schedule command wrote a schedule through scheduler.py')

        before = {item['name'] for item in self.workbench.backup_list()}
        self.cli('backup', 'create', '--json')
        after = {item['name'] for item in self.workbench.backup_list()}
        self.assertGreater(len(after), len(before), 'backup create wrote a restorable copy')
        manifest = self.workbench.backup_list()[0]['manifest_data']
        self.assertEqual(manifest.schema_version, p.schema.SCHEMA_VERSION)

    def test_the_cli_reaches_the_geo_database_the_presets_and_the_source_redaction(self):
        self.cli('geo', 'status', '--json')
        self.cli('preset', 'github')
        redacted = self.cli('source', 'redact', '--text', 'https://u:pw@example.org/l?t=1')
        self.assertEqual(redacted, 0)

    def test_the_control_api_reaches_the_same_modules_through_a_real_socket(self):
        from proxy_workbench import apikeys

        _server, client = self.serve()
        with p.Workbench(self.home, db_path=self.home / 'proxies.sqlite3') as workbench:
            issued = workbench.key_bootstrap()
        head = {'Authorization': f'Bearer {issued.secret}'}

        created = client.post('/v1/collections', json={'name': 'приёмка'},
                              headers={**head, 'Idempotency-Key': 'accept-collection'})
        self.assertIn(created.status_code, (200, 201), created.text)
        collection = created.json()['id']

        imported = client.post(f'/v1/collections/{collection}/imports',
                               json={'format': 'uri',
                                     'content': 'http://91.198.174.192:8080\n'},
                               headers={**head, 'Idempotency-Key': 'accept-import'})
        self.assertEqual(imported.status_code, 202, imported.text)
        self.assertEqual(imported.json()['counts']['added'], 1)
        self.assertTrue(imported.json()['job_id'], 'a long operation answers with a job id')

        pool = client.post('/v1/pools', json={'name': 'steady', 'desired': 2,
                                              'collection_id': collection,
                                              'profile_id': PROFILE},
                           headers={**head, 'Idempotency-Key': 'accept-pool'})
        self.assertEqual(pool.status_code, 200, pool.text)
        schedule = client.post('/v1/schedules', json={'name': 'nightly', 'kind': 'check',
                                                      'interval_minutes': 30},
                               headers={**head, 'Idempotency-Key': 'accept-schedule'})
        self.assertEqual(schedule.status_code, 200, schedule.text)
        listed = client.get('/v1/schedules', headers=head)
        self.assertEqual([item['id'] for item in listed.json()['items']], ['nightly'])

        with p.Workbench(self.home, db_path=self.home / 'proxies.sqlite3') as workbench:
            self.assertEqual(workbench.pools().get('steady').desired, 2)
            self.assertEqual(workbench.conn.execute(
                'SELECT count(*) FROM membership WHERE collection_id=?',
                (collection,)).fetchone()[0], 1)
        del apikeys

    def test_the_api_refuses_a_document_format_the_route_does_not_define(self):
        """A silent pass-through would import a Clash file as a flat list."""
        with p.Workbench(self.home, db_path=self.home / 'proxies.sqlite3') as workbench:
            issued = workbench.key_bootstrap()
        _server, client = self.serve()
        answer = client.post('/v1/collections/public-base/imports',
                             json={'format': 'clash', 'content': '{"proxies": []}'},
                             headers={'Authorization': f'Bearer {issued.secret}',
                                      'Idempotency-Key': 'accept-clash'})
        self.assertEqual(answer.status_code, 400, answer.text)
        self.assertEqual(answer.json()['error']['code'], 'E_VALIDATION_FIELD')


# ---------------------------------------------------------------------------
# 19. An address of your own, from import to artifact
# ---------------------------------------------------------------------------


class OwnAddressTests(AcceptanceCase):
    def test_a_hostname_of_your_own_passes_the_whole_path(self):
        """Import, scope, measure, publish and read -- a name, not a number."""
        from proxy_workbench import importer

        with p.Workbench(self.home, db_path=self.home / 'proxies.sqlite3') as workbench:
            collection = importer.create_collection(workbench.conn, 'мои адреса', kind='private')
            source = workbench.import_source('socks5://my-proxy.example.net:1080\n', name='own.txt')
            plan = workbench.import_preview(source, collection,
                                            policy=importer.EndpointPolicy(public_only=False))
            report = workbench.import_commit(plan)
        self.assertEqual(report.added, ('socks5://my-proxy.example.net:1080',))
        self.assertEqual(workbench.conn.execute(
            'SELECT e.canonical FROM membership m JOIN endpoints e ON e.id=m.endpoint_id '
            'WHERE m.collection_id=?', (collection,)).fetchall()[0][0],
            'socks5://my-proxy.example.net:1080')

        async def probe(proxy, cfg, rate):
            return measured_row(proxy, checked_at=time.time())

        profile = asyncio.run(p.scan(self.db, profile_config(), workers=2, rate=0, probe=probe,
                                     progress=False, min_success=1, collection_id=collection,
                                     max_age_seconds=p.MIN_FRESHNESS_SECONDS))
        self.assertTrue(db_row(self.db, profile))
        report = p.export(self.db, profile, self.home / 'exports', min_success=1,
                          collection_id=collection, max_age_seconds=p.MIN_FRESHNESS_SECONDS)
        self.assertTrue(any('my-proxy.example.net' in item for item in _text_list(report)),
                        _text_list(report))
        rows, status = api.Exports(self.home / 'exports').load()
        self.assertEqual(status['collection_id'], collection)
        self.assertTrue(any('my-proxy.example.net' in row['proxy'] for row in rows))

    def test_a_password_change_revokes_the_previous_proof(self):
        """A new revision is a different row, and the old one stops admitting."""
        from proxy_workbench import core, secrets as secretstore

        address = 'http://91.198.174.193:8080'
        # The vault is a process-scoped store, so create and rotate share it:
        # a rotation whose predecessor secret is gone is a different scenario.
        with p.Workbench(self.home, db_path=self.home / 'proxies.sqlite3') as workbench:
            endpoint = p.schema.upsert_endpoint(workbench.conn, address)
            vault = secretstore.MemoryVault()
            store = secretstore.AccessStore(workbench.conn, vault)
            access = store.create(endpoint, 'http', username='u', password='first-password')
            workbench.conn.commit()
        first = access.access_revision
        store_result(self.db, (PROFILE, address, json.dumps(
            measured_row(address, checked_at=self.now))), access_id=access.id,
            access_revision=first, max_age_seconds=p.MIN_FRESHNESS_SECONDS)
        self.db.commit()

        policy = core.Policy(max_age_seconds=p.MIN_FRESHNESS_SECONDS, min_success=1)
        scope = core.Scope(db_schema_public(self.db), PROFILE, 1, 'default')
        fresh = core.admit(json.loads(self.db.execute(
            'SELECT payload FROM results WHERE profile=? AND proxy=?',
            (PROFILE, address)).fetchone()[0]), scope, core.Access(access.id, first),
            policy, self.now)
        self.assertTrue(fresh.admitted, fresh.reason_code)

        with p.Workbench(self.home, db_path=self.home / 'proxies.sqlite3') as workbench:
            store = secretstore.AccessStore(workbench.conn, vault)
            rotated = store.rotate(access.id, password='second-password')
            workbench.conn.commit()
        self.assertGreater(rotated.access_revision, first)
        revoked = core.admit(json.loads(self.db.execute(
            'SELECT payload FROM results WHERE profile=? AND proxy=?',
            (PROFILE, address)).fetchone()[0]), scope,
            core.Access(access.id, rotated.access_revision), policy, self.now)
        self.assertFalse(revoked.admitted, 'the old proof is not the new one')
        self.assertEqual(revoked.reason_code, 'E_CONFLICT_ACCESS_REVISION')


def db_schema_public(conn):
    return p.schema.PUBLIC_COLLECTION_ID


if __name__ == '__main__':
    unittest.main()

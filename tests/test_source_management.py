import asyncio
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import api
from proxy_workbench import apiv1
from proxy_workbench import gui
from proxy_workbench import i18n
from proxy_workbench import proxytool as p
from proxy_workbench import source_catalog
from proxy_workbench import source_management as sm


CATALOG = source_catalog.load_bundled()

#: A source a later catalog adds; it exists in no packaged catalog, so anything
#: that re-validates a selection against the bundled file alone loses it.
REMOTE_ONLY = {'id': 'new-999', 'name': 'Remote-only list', 'publisher': 'Remote Publisher',
               'data_urls': ['https://remote.example/rows.txt'], 'protocols': ['http'],
               'adapter': {'kind': 'line', 'profile': 'line-v1', 'config': {}},
               'access': 'public', 'category': 'list', 'collection_allowed': True}


def accepted_catalog(extra=(REMOTE_ONLY,), revision=None):
    """The bundled catalog plus entries only the accepted revision lists."""
    raw = json.loads(source_catalog.bundled_path().read_text(encoding='utf-8'))
    sources = [dict(item) for item in raw['sources']] + [dict(item) for item in extra]
    return source_catalog.validate_catalog(
        dict(raw, sources=sources, sets=[],
             revision=revision or CATALOG['revision'] + 1),
        allow_research=True, allow_unsafe=True)


def settings_with(ids, custom=(), disabled=(), catalog=None):
    catalog = catalog or CATALOG
    selection = dict(schema_version=1, catalog_revision=catalog['revision'],
                     selected_ids=list(ids), download_disabled_ids=list(disabled),
                     sets=[], custom_sources=list(custom))
    return source_catalog.migrate_settings({'sources': [], 'source_selection': selection}, catalog)


class CatalogViewTests(unittest.TestCase):
    """The catalog view must stay descriptive, never a quality ranking."""

    def test_five_evidence_statuses_stay_separate_and_liveness_is_never_set(self):
        view = sm.build_view(CATALOG, settings_with(['cur-01']), {}, {'q': 'cur-01'})
        row = view['sources'][0]
        self.assertEqual(sorted(row['evidence']), sorted(source_catalog.EVIDENCE_STATES))
        self.assertEqual(row['evidence']['proxy_liveness']['state'], 'not_run')
        self.assertEqual(row['evidence']['url_reachable']['state'], 'http_2xx_nonempty')
        self.assertNotEqual(row['evidence']['format_confirmed']['state'],
                            row['evidence']['url_reachable']['state'])
        text = json.dumps(view, ensure_ascii=False).lower()
        for forbidden in ('working source', 'живой источник', 'рабочий источник', 'качественный источник'):
            self.assertNotIn(forbidden, text)

    def test_tor_mtproto_and_subscription_formats_are_visible_but_never_collected(self):
        view = sm.build_view(CATALOG, {}, {}, {'q': 'tor', 'limit': 50})
        identifiers = {row['id'] for row in view['sources']}
        self.assertIn('new-073', identifiers)
        for source_id in ('new-073', 'new-087', 'new-088'):
            row = next(item for item in sm.build_view(CATALOG, {}, {}, {'limit': 200})['sources']
                       if item['id'] == source_id)
            self.assertFalse(row['collectable'])
            self.assertTrue(row['not_proxy_source'])
            self.assertIn(row['state'], ('not_proxy_source', 'needs_access'))
        selected = settings_with(['new-073', 'new-088', 'cur-01'])
        self.assertEqual(selected['sources'],
                         [next(source['legacy_specs'][0] for source in CATALOG['sources']
                               if source['id'] == 'cur-01')])

    def test_provider_conditions_are_separate_groups_with_checked_dates_and_terms(self):
        view = sm.build_view(CATALOG, {}, {}, {'limit': 1})
        groups = {group['id']: group for group in view['access_groups']}
        self.assertGreater(groups['public_free']['count'], 0)
        self.assertGreater(groups['trial']['count'], 0)
        self.assertGreater(groups['paid']['count'], 0)
        self.assertGreater(groups['permanent_free_quota']['count'], 0)
        self.assertEqual(groups['free_with_key']['count'], 1)
        for group in groups.values():
            if group['count'] and group['id'] not in ('own_infrastructure', 'unknown'):
                self.assertTrue(group['checked_at'], group)
        trial = sm.build_view(CATALOG, {}, {}, {'access': 'trial', 'limit': 50})['sources']
        self.assertTrue(trial)
        for row in trial:
            self.assertEqual(row['access'], 'temporary_trial')
            self.assertNotEqual(row['access'], 'permanent_free_quota')
            self.assertTrue(row['terms_url'] or row['access_note'])

    def test_filters_narrow_the_catalog_without_changing_the_selection(self):
        base = settings_with(['cur-01', 'cur-02'], disabled=['cur-02'])
        catalog_ids = {source['id'] for source in CATALOG['sources']}
        http_sources = {source['id'] for source in CATALOG['sources'] if 'http' in source['protocols']}
        quick = set(next(definition['members'] for definition in CATALOG['sets']
                         if definition['id'] == 'quick'))
        cases = {
            'protocol': (http_sources, {'protocol': 'http'}),
            'set': (quick, {'set': 'quick'}),
        }
        for name, (expected, query) in cases.items():
            with self.subTest(filter=name):
                view = sm.build_view(CATALOG, base, {}, {**query, 'limit': 200})
                self.assertTrue(view['sources'])
                self.assertTrue(expected.issuperset({row['id'] for row in view['sources']}))
        by_state = sm.build_view(CATALOG, base, {}, {'state': 'disabled', 'limit': 200})
        self.assertEqual([row['id'] for row in by_state['sources']], ['cur-02'])
        self.assertEqual(by_state['sources'][0]['selection_state'], 'disabled')
        self.assertTrue(by_state['sources'][0]['selected'])
        with self.assertRaises(ValueError):
            sm.build_view(CATALOG, base, {}, {'state': 'not-a-state'})
        text = sm.build_view(CATALOG, base, {}, {'q': 'raw.githubusercontent.com', 'limit': 5})
        self.assertTrue(text['sources'])
        self.assertTrue(all('githubusercontent.com' in endpoint['url']
                            for row in text['sources'] for endpoint in row['endpoints']))
        self.assertEqual(base['source_selection']['selected_ids'], ['cur-01', 'cur-02'])
        category = CATALOG['sources'][0]['category']
        narrowed = sm.build_view(CATALOG, base, {}, {'category': category})
        self.assertTrue(narrowed['sources'])
        self.assertTrue(all(row['category'] == category for row in narrowed['sources']))

    def test_sets_report_new_members_without_adding_them(self):
        settings = settings_with(['cur-01'])
        settings['source_selection']['sets'] = [{'id': 'quick', 'members': ['cur-02']}]
        view = sm.build_view(CATALOG, settings, {}, {'limit': 1})
        quick = next(item for item in view['sets'] if item['id'] == 'quick')
        self.assertTrue(quick['applied'])
        self.assertEqual(quick['applied_members'], 1)
        self.assertIn('cur-04', quick['new_members'])
        self.assertNotIn('cur-04', settings['source_selection']['selected_ids'])

    def test_the_custom_set_and_the_custom_state_are_the_same_own_sources(self):
        # The user's own lists are the "custom" set: it cannot be filled from a
        # catalog, so its members come from the selection.  Otherwise the set
        # filter, the set card and the "custom" state answer three different
        # questions about the same rows.
        own = [source_catalog.custom_source('https://mine.example/one.txt', 'http'),
               source_catalog.custom_source('https://mine.example/two.txt', 'socks5')]
        settings = settings_with([item['id'] for item in own], custom=own)
        by_set = sm.build_view(CATALOG, settings, {}, {'set': 'custom', 'limit': 50})
        by_state = sm.build_view(CATALOG, settings, {}, {'state': 'custom', 'limit': 50})
        self.assertEqual([row['id'] for row in by_set['sources']], [item['id'] for item in own])
        self.assertEqual([row['id'] for row in by_state['sources']], [row['id'] for row in by_set['sources']])
        self.assertEqual(by_set['total'], by_state['total'])
        self.assertEqual(by_set['custom_count'], 2)
        self.assertEqual(sm.build_view(CATALOG, settings_with([]), {}, {'set': 'custom'})['total'], 0)
        custom_set = next(item for item in by_set['sets'] if item['id'] == 'custom')
        self.assertEqual(custom_set['members'], 2)
        self.assertEqual(custom_set['member_ids'], [item['id'] for item in own])
        self.assertEqual(custom_set['selected_members'], 2)
        # Applying the set keeps exactly the own sources and snapshots them, so
        # the card does not immediately claim the same sources are new.
        applied = sm.apply_set(settings, 'custom', CATALOG)
        self.assertEqual(applied['source_selection']['selected_ids'], [item['id'] for item in own])
        snapshot = next(item for item in sm.sets_view(CATALOG, applied) if item['id'] == 'custom')
        self.assertTrue(snapshot['applied'])
        self.assertEqual(snapshot['new_members'], [])
        self.assertEqual(snapshot['retired_members'], [])
        self.assertEqual(sm.build_view(CATALOG, applied, {}, {'set': 'quick'})['total'],
                         len(source_catalog.DEFAULT_SETS['quick']['members']))

    def test_catalog_refresh_reports_changes_and_never_selects(self):
        settings = settings_with(['cur-01'])
        changed = {**next(source for source in CATALOG['sources'] if source['id'] == 'cur-01'),
                   'name': 'Renamed by the publisher'}
        extra = {**next(source for source in CATALOG['sources'] if source['id'] == 'cur-02'),
                 'id': 'cur-99', 'name': 'New entry'}
        incoming = dict(CATALOG, sets=[], sources=[changed, extra] + [s for s in CATALOG['sources']
                                                                      if s['id'] not in ('cur-01', 'cur-02')])
        diff = source_catalog.catalog_diff(CATALOG, source_catalog.validate_catalog(incoming, allow_unsafe=True))
        self.assertEqual(diff['added'], ['cur-99'])
        self.assertIn('cur-01', diff['changed'])
        self.assertIn('cur-02', diff['retired'])
        self.assertEqual(settings['source_selection']['selected_ids'], ['cur-01'])
        self.assertEqual(settings['source_selection']['custom_sources'], [])

    def test_a_source_retired_by_a_catalog_update_stays_visible_and_removable(self):
        settings = settings_with(['cur-01', 'cur-02'])
        reduced = dict(CATALOG, sets=[],
                       sources=[item for item in CATALOG['sources'] if item['id'] != 'cur-01'])
        catalog = source_catalog.validate_catalog(reduced, allow_unsafe=True)
        view = sm.build_view(catalog, settings, {}, {'limit': 200})
        self.assertEqual(view['selection']['materialized'], 2)
        self.assertEqual(view['selection']['retired'], ['cur-01'])
        retired = next(item for item in view['sources'] if item['id'] == 'cur-01')
        self.assertTrue(retired['retired'])
        self.assertTrue(retired['selected'])
        self.assertEqual(retired['state'], 'retired')
        self.assertEqual(sm.build_view(catalog, settings, {}, {'q': 'cur-01'})['total'], 1)
        self.assertEqual(sm.build_view(catalog, settings, {}, {'state': 'retired'})['total'], 1)
        detail = sm.detail_view(catalog, settings, 'cur-01', {})
        self.assertTrue(detail['retired'])
        # The choice is the user's: it survives the update and can be undone.
        after = sm.remove_sources(settings, ['cur-01'])
        self.assertEqual(after['source_selection']['selected_ids'], ['cur-02'])
        self.assertNotIn('cur-01', after['sources'])
        # A source nobody selected is not invented.
        self.assertEqual(sm.build_view(catalog, settings_with(['cur-02']), {}, {'state': 'retired'})['total'], 0)

    def test_migration_keeps_the_format_the_user_typed_for_a_catalog_url(self):
        # cur-41 is a socks5 list today; a bare URL in the old text area meant
        # "an HTTP list", and the catalog must not re-type that choice.
        url = 'https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt'
        catalog_item = source_catalog.source_by_id(CATALOG, 'cur-41')
        self.assertEqual(catalog_item['legacy_specs'], [f'socks5 {url}'])
        migrated = source_catalog.migrate_settings({'sources': [url]})
        self.assertEqual(migrated['source_selection']['selected_ids'], ['cur-41'])
        self.assertEqual(migrated['sources'], [url])
        self.assertEqual(migrated['source_selection']['specs'], {'cur-41': url})
        plans = source_catalog.materialize_selection(migrated)
        self.assertEqual([plan['adapter']['config']['legacy_kind'] for plan in plans], ['http'])
        # Re-reading the stored settings keeps the same reading.
        self.assertEqual(source_catalog.migrate_settings(migrated)['sources'], [url])
        self.assertEqual(gui.validate(migrated)['source_selection']['specs'], {'cur-41': url})
        # An explicitly typed format, and a source added fresh from the catalog
        # screen, keep the catalog's own declaration.
        explicit = source_catalog.migrate_settings({'sources': [f'socks5 {url}']})
        self.assertEqual(explicit['sources'], [f'socks5 {url}'])
        fresh = settings_with(['cur-41'])
        self.assertEqual(fresh['sources'], [f'socks5 {url}'])
        # Removing the source drops the recorded format with it.
        self.assertEqual(sm.remove_sources(migrated, ['cur-41'])['source_selection']['specs'], {})

    def test_every_bare_url_of_a_legacy_kind_keeps_its_own_format(self):
        aliases = source_catalog.legacy_aliases(CATALOG)
        retyped = []
        for source in CATALOG['sources']:
            for endpoint in source['endpoints']:
                url = endpoint['url']
                if aliases.get(url) != source['id'] or not source['legacy_specs']:
                    continue
                if source['legacy_specs'][0] == url:
                    continue
                migrated = source_catalog.migrate_settings({'sources': [url]})
                if migrated['sources'] != [url]:
                    retyped.append(source['id'])
        self.assertEqual(retyped, [])


class MockSourceService:
    """Loopback list server: the only kind of endpoint these tests point at."""

    def __init__(self, body=b'[{"ip":"11.7.7.7","port":80,"protocol":"http"}]', status=200):
        self.body = body
        self.status = status
        self.server = None
        self.thread = None
        self.base = ''

    def __enter__(self):
        import http.server

        body, status = self.body, self.status

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class SourceGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.server = gui.make_server(self.home)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.server.server_port}', trust_env=False,
                                   headers={'X-Workbench-Token': self.server.app.token}, timeout=20)

    def tearDown(self):
        self.server.app.close()
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def stored(self):
        return json.loads((self.home / 'gui-settings.json').read_text(encoding='utf-8'))

    def accept_catalog(self, catalog=None):
        """Store an accepted catalog, as ``sources update`` does."""
        catalog = catalog or accepted_catalog()
        (self.home / 'source-catalog.json').write_text(json.dumps(catalog), encoding='utf-8')
        return catalog

    def test_catalog_control_plane_requires_the_gui_token(self):
        for path in ('/api/source-catalog', '/api/source-catalog/cur-01', '/api/sources/scope'):
            self.assertEqual(self.client.get(path, headers={'X-Workbench-Token': ''}).status_code, 403)
            self.assertEqual(self.client.get(path, headers={'Host': 'untrusted.invalid'}).status_code, 403)
            self.assertEqual(self.client.get(path, headers={'Origin': 'http://untrusted.invalid'}).status_code, 403)
        self.assertEqual(self.client.post('/api/sources/toggle', json={'id': 'cur-01', 'disabled': True},
                                          headers={'X-Workbench-Token': ''}).status_code, 403)
        body = self.client.get('/api/source-catalog?limit=3').json()
        self.assertEqual(body['total'], len(CATALOG['sources']))
        self.assertEqual(len(body['sources']), 3)
        self.assertEqual(body['sources'][0]['evidence']['proxy_liveness']['state'], 'not_run')

    def test_pause_and_remove_are_two_different_actions(self):
        self.server.app.save(settings_with(['cur-01', 'cur-02']))
        paused = self.client.post('/api/sources/toggle', json={'id': 'cur-01', 'disabled': True}).json()
        self.assertTrue(paused['download_disabled'])
        self.assertIn('cur-01', paused['settings']['source_selection']['selected_ids'])
        self.assertNotIn([value for value in paused['settings']['sources']
                          if 'MuRongPIG' in value], [])
        removed = self.client.post('/api/sources/remove', json={'id': 'cur-02'}).json()
        self.assertNotIn('cur-02', removed['settings']['source_selection']['selected_ids'])
        self.assertIn('cur-01', removed['settings']['source_selection']['selected_ids'])
        self.assertIn('cur-01', removed['settings']['source_selection']['download_disabled_ids'])
        self.assertNotIn('cur-02', removed['settings']['sources'])
        self.assertEqual(self.client.post('/api/sources/toggle', json={'id': 'cur-01'}).status_code, 400)
        self.assertEqual(self.client.post('/api/sources/toggle',
                                          json={'id': 'cur-01', 'disabled': 'yes'}).status_code, 400)
        self.assertEqual(self.client.post('/api/sources/remove', json={'id': 'nope'}).status_code, 400)

    def test_include_puts_a_catalog_source_into_the_set_again(self):
        # "Add to the set" is the only way back after a removal, so it has to
        # work for a source the catalog owns, not only for an own URL.
        self.server.app.save(settings_with(['cur-02']))
        self.assertNotIn('cur-04', self.stored()['source_selection']['selected_ids'])
        response = self.client.post('/api/sources/select', json={'id': 'cur-04', 'selected': True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn('cur-04', body['settings']['source_selection']['selected_ids'])
        self.assertTrue(body['in_set'])
        self.assertIn(source_catalog.source_by_id(CATALOG, 'cur-04')['legacy_specs'][0],
                      body['settings']['sources'])
        self.assertIn('https://proxyspace.pro/http.txt', body['settings']['sources'])
        # A paused source is taken back into the download as well.
        self.client.post('/api/sources/toggle', json={'id': 'cur-04', 'disabled': True})
        again = self.client.post('/api/sources/select', json={'id': 'cur-04', 'selected': True}).json()
        self.assertIn('cur-04', again['settings']['source_selection']['selected_ids'])
        self.assertNotIn('cur-04', again['settings']['source_selection']['download_disabled_ids'])
        # An id that is neither in the catalog nor stored is still refused.
        self.assertEqual(self.client.post('/api/sources/select',
                                          json={'id': 'nope-999', 'selected': True}).status_code, 400)

    def test_a_source_only_the_accepted_catalog_lists_reaches_the_collector(self):
        # ``sources`` is the one list the collector reads, so every selection
        # change has to be re-materialized against the catalog it validated,
        # never against the packaged one: a source an accepted catalog adds is
        # otherwise selected on screen and silently never downloaded.
        catalog = self.accept_catalog()
        url = REMOTE_ONLY['data_urls'][0]
        picked = self.client.post('/api/sources/select', json={'id': 'new-999', 'selected': True})
        self.assertEqual(picked.status_code, 200)
        self.assertTrue(picked.json()['in_set'])
        self.assertIn('new-999', picked.json()['settings']['source_selection']['selected_ids'])
        self.assertIn(url, picked.json()['settings']['sources'])
        # Pausing takes the source out of the download and resuming puts the
        # very same spec back, without the packaged catalog knowing the id.
        paused = self.client.post('/api/sources/toggle', json={'id': 'new-999', 'disabled': True}).json()
        self.assertIn('new-999', paused['settings']['source_selection']['download_disabled_ids'])
        self.assertIn('new-999', paused['settings']['source_selection']['selected_ids'])
        self.assertNotIn(url, paused['settings']['sources'])
        resumed = self.client.post('/api/sources/toggle', json={'id': 'new-999', 'disabled': False}).json()
        self.assertIn(url, resumed['settings']['sources'])
        # Removing a different source keeps the spec of the accepted-catalog one.
        removed = self.client.post('/api/sources/remove', json={'id': 'cur-02'}).json()
        self.assertIn(url, removed['settings']['sources'])
        # A set replaces the selection by design, but taking the source back
        # afterwards must materialize it again.
        self.assertEqual(self.client.post('/api/sources/set', json={'set': 'quick'}).status_code, 200)
        self.assertNotIn('new-999', self.stored()['source_selection']['selected_ids'])
        again = self.client.post('/api/sources/select', json={'id': 'new-999', 'selected': True}).json()
        self.assertIn('new-999', again['settings']['source_selection']['selected_ids'])
        self.assertIn(url, again['settings']['sources'])
        # Adding an own URL re-materializes the whole selection, so the
        # accepted-catalog source has to survive it as well.
        added = self.client.post('/api/sources/add', json={'url': 'https://mine.example/list.txt',
                                                           'kind': 'socks5'}).json()
        self.assertIn(url, added['settings']['sources'])
        # A restart reads the same list, and the collector resolves the source.
        stored = gui.read_settings(self.home)
        self.assertIn(url, stored['sources'])
        plans = source_catalog.materialize_selection(stored, catalog)
        self.assertIn('new-999', [plan['id'] for plan in plans])
        self.assertIn('new-999', [item['id'] for item in
                                  self.client.get('/api/source-catalog', params={'q': 'new-999'}).json()['sources']])

    def test_pause_of_a_source_the_catalog_does_not_list_is_refused(self):
        # Every other selection action refuses an unknown id; a pause that
        # reported success for one would write a setting nothing can show,
        # undo or explain.
        self.server.app.save(settings_with(['cur-01']))
        response = self.client.post('/api/sources/toggle', json={'id': 'totally-unknown', 'disabled': True})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('totally-unknown', self.stored()['source_selection']['download_disabled_ids'])
        self.assertEqual(self.client.get('/api/source-catalog',
                                         params={'state': 'disabled'}).json()['total'], 0)

    def test_prune_sees_a_source_only_the_accepted_catalog_lists(self):
        catalog = self.accept_catalog()
        url = REMOTE_ONLY['data_urls'][0]
        self.server.app.save(settings_with(['new-999', 'cur-01'], catalog=catalog))
        self.assertIn(url, self.stored()['sources'])
        (self.home / 'exports').mkdir(exist_ok=True)
        (self.home / 'exports' / 'status.json').write_text(
            json.dumps({'source_quality': {p.source_key(url): {'checked': 500, 'passed': 0}}}), encoding='utf-8')
        body = self.client.post('/api/sources/prune', json=self.stored()).json()
        self.assertEqual(body['removed'], [gui.public_source(REMOTE_ONLY['data_urls'][0])])
        self.assertNotIn('new-999', body['settings']['source_selection']['selected_ids'])
        self.assertNotIn('new-999', self.stored()['source_selection']['selected_ids'])
        self.assertNotIn(url, self.stored()['sources'])

    def test_paused_source_offers_resume_and_not_pause(self):
        self.server.app.save(settings_with(['cur-01']))
        self.client.post('/api/sources/toggle', json={'id': 'cur-01', 'disabled': True})
        row = next(item for item in self.client.get('/api/source-catalog', params={'q': 'cur-01'}).json()['sources']
                   if item['id'] == 'cur-01')
        self.assertTrue(row['download_disabled'])
        self.assertTrue(row['selected'])
        self.assertEqual(row['selection_state'], 'disabled')
        source = (Path(__file__).resolve().parents[1] / 'proxy_workbench' / 'ui' / 'app.js').read_text(encoding='utf-8')
        row_html = source[source.index('function catalogRowHtml'):source.index('function renderCatalog(view)')]
        self.assertIn('row.download_disabled', row_html)
        self.assertIn('cat.action.resume', row_html)
        self.assertNotIn('data-disabled', row_html)
        handlers = source[source.index('function bindCatalogRows'):source.index('function catalogToggle')]
        self.assertIn("'catalog-resume': id => catalogToggle(id, false)", handlers)
        self.assertIn("'catalog-toggle': id => catalogToggle(id, true)", handlers)
        for language in ('cat.action.resume', 'cat.state.retired'):
            self.assertEqual(source.count(f"'{language}':"), 2)

    def test_own_url_keeps_its_chosen_format_and_refuses_private_targets(self):
        added = self.client.post('/api/sources/add', json={'url': 'https://mine.example/rows.csv',
                                                           'kind': 'fields'}).json()
        self.assertEqual(added['adapter'], 'fields')
        self.assertIn('fields https://mine.example/rows.csv', added['settings']['sources'])
        self.assertIn(added['id'], added['settings']['source_selection']['selected_ids'])
        socks = self.client.post('/api/sources/add', json={'url': 'https://mine.example/list.txt',
                                                          'kind': 'socks5'}).json()
        self.assertIn('socks5 https://mine.example/list.txt', socks['settings']['sources'])
        for url in ('http://127.0.0.1:9/list', 'http://169.254.169.254/latest/meta-data/', 'http://[::1]/list'):
            with self.subTest(url=url):
                response = self.client.post('/api/sources/add', json={'url': url, 'kind': 'http'})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post('/api/sources/add',
                                          json={'url': 'https://mine.example/x', 'kind': 'yaml'}).status_code, 400)
        self.assertEqual(self.client.post('/api/sources/add',
                                          json={'url': 'https://user:pw@mine.example/x', 'kind': 'http'}).status_code, 400)

    def test_the_set_filter_and_the_custom_state_agree_on_own_sources(self):
        first = self.client.post('/api/sources/add', json={'url': 'https://mine.example/one.txt',
                                                           'kind': 'http'}).json()
        second = self.client.post('/api/sources/add', json={'url': 'https://mine.example/two.txt',
                                                            'kind': 'socks5'}).json()
        by_set = self.client.get('/api/source-catalog', params={'set': 'custom'}).json()
        by_state = self.client.get('/api/source-catalog', params={'state': 'custom'}).json()
        self.assertEqual([row['id'] for row in by_set['sources']], [first['id'], second['id']])
        self.assertEqual([row['id'] for row in by_state['sources']], [row['id'] for row in by_set['sources']])
        self.assertEqual(next(item for item in by_set['sets'] if item['id'] == 'custom')['members'], 2)
        self.assertTrue(self.client.get('/api/source-catalog', params={'set': 'quick'}).json()['total'])

    def test_preview_reports_counts_and_reasons_without_touching_the_database(self):
        body = json.dumps([{'ip': '11.7.7.7', 'port': 80, 'protocol': 'http'},
                           {'ip': '11.7.7.8', 'port': 'not-a-port', 'protocol': 'http'},
                           {'proxy': 'http://user:secret@11.7.7.9:80', 'protocol': 'http'}]).encode()
        with MockSourceService(body) as service:
            preview = self.client.post('/api/sources/preview', json={'url': service.base + '/list.json',
                                                                      'kind': 'json-records',
                                                                      'allow_private': True}).json()
        self.assertTrue(preview['preview'])
        # The adapter drops the record with credentials before counting it; the
        # unusable port is recognized and rejected later by the common barrier.
        self.assertEqual(preview['recognized'], 2)
        self.assertEqual(preview['accepted'], 1)
        self.assertEqual(preview['rejected'], 2)
        self.assertEqual(preview['reject_reasons']['credentials_present'], 1)
        self.assertEqual(preview['reject_reasons']['not_a_public_proxy'], 1)
        self.assertEqual(preview['proxies_checked'], 0)
        self.assertEqual(preview['sample'], ['http://11.7.7.7:80'])
        self.assertFalse((self.home / 'proxies.sqlite3').exists())
        # A private destination stays blocked unless the person ticks the box.
        with MockSourceService(body) as service:
            blocked = self.client.post('/api/sources/preview', json={'url': service.base + '/list.json',
                                                                      'kind': 'json-records'})
        self.assertEqual(blocked.status_code, 400)

    def test_check_one_source_is_a_preview_and_refuses_unsupported_formats(self):
        body = json.dumps([{'ip': '11.5.5.5', 'port': 8080, 'protocol': 'socks5'}]).encode()
        with MockSourceService(body) as service:
            preview = self.client.post('/api/sources/check', json={'url': service.base + '/socks.json',
                                                                    'kind': 'json-records',
                                                                    'allow_private': True}).json()
        self.assertEqual(preview['accepted'], 1)
        self.assertEqual(preview['http_state'], 'http_2xx_nonempty')
        self.assertEqual(preview['parse_state'], 'complete')
        self.assertEqual(preview['proxies_checked'], 0)
        # One source already in the set: the button reports a reason, not a verdict.
        descriptor = source_catalog.custom_source('https://offline.invalid/list.txt', 'http')
        gui.save_settings(self.home, settings_with([descriptor['id']],
                                                   custom=[{'id': descriptor['id'], 'url': descriptor['url'],
                                                            'adapter': descriptor['adapter']}]))
        with mock.patch.object(p, '_validate_source_destination',
                               side_effect=p.SourceFetchError('SOURCE_DNS_ERROR')):
            reported = self.client.post('/api/sources/check', json={'id': descriptor['id']}).json()
        self.assertTrue(reported['error'])
        self.assertEqual(reported['accepted'], 0)
        response = self.client.post('/api/sources/check', json={'id': 'new-073', 'allow_private': True})
        self.assertEqual(response.status_code, 400)
        self.assertIn('не список прокси-адресов', response.json()['error'])

    def test_private_custom_source_requires_boolean_opt_in_for_every_preview(self):
        with MockSourceService(b'11.3.3.3:8080\n127.0.0.1:80\n') as service:
            url = service.base + '/private-source'
            for opt_in in (None, False, 'true'):
                payload = {'url': url, 'kind': 'http', 'allow_private': opt_in}
                self.assertEqual(self.client.post('/api/sources/add', json=payload).status_code, 400)
            added = self.client.post('/api/sources/add', json={
                'url': url, 'kind': 'http', 'allow_private': True})
            self.assertEqual(added.status_code, 200, added.text)
            source_id = added.json()['id']
            refused = self.client.post('/api/sources/check', json={'id': source_id}).json()
            self.assertEqual(refused['accepted'], 0)
            self.assertIsNotNone(refused['error'])
            checked = self.client.post('/api/sources/check', json={
                'id': source_id, 'allow_private': True}).json()
            self.assertEqual(checked['accepted'], 1)
            self.assertEqual(checked['sample'], ['http://11.3.3.3:8080'])
            self.assertEqual(checked['proxies_checked'], 0)

    def test_exclude_scope_keeps_the_data_and_can_be_undone(self):
        db = p.open_db(self.home / 'proxies.sqlite3')
        try:
            for proxy, source in (('http://11.1.1.1:80', 'cur-01'), ('http://11.1.1.2:80', 'cur-01'),
                                  ('http://11.1.1.3:80', 'cur-01'), ('http://11.1.1.3:80', 'cur-02')):
                db.execute('INSERT OR IGNORE INTO candidates(proxy) VALUES (?)', (proxy,))
                db.execute('INSERT INTO candidate_seen(proxy,source) VALUES (?,?)', (proxy, source))
            db.execute('INSERT INTO source_identity(source_id,family_id,publisher_id) VALUES (?,?,?)',
                       ('cur-01', 'family-a', 'p'))
            db.execute('INSERT INTO source_identity(source_id,family_id,publisher_id) VALUES (?,?,?)',
                       ('cur-02', 'family-b', 'p'))
            db.commit()
        finally:
            db.close()
        result = self.client.post('/api/sources/exclude-scope', json={'id': 'cur-01', 'confirm': True}).json()
        self.assertEqual(result['excluded'], 2)
        self.assertEqual(result['delivered'], 3)
        self.assertEqual(result['shared'], 1)
        scope = self.client.get('/api/sources/scope').json()
        self.assertEqual([row['proxy'] for row in scope['proxies']], ['http://11.1.1.1:80', 'http://11.1.1.2:80'])
        db = p.open_db(self.home / 'proxies.sqlite3')
        try:
            self.assertEqual(db.execute('SELECT count(*) FROM candidates').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM candidate_seen').fetchone()[0], 4)
        finally:
            db.close()
        both = self.client.post('/api/sources/exclude-scope',
                                json={'id': 'cur-01', 'confirm': True, 'include_shared': True}).json()
        self.assertEqual(both['excluded'], 1)
        self.assertEqual(both['already_excluded'], 2)
        self.assertEqual(self.client.post('/api/sources/scope/clear', json={}).json()['removed'], 3)
        self.assertEqual(self.client.get('/api/sources/scope').json()['count'], 0)

    def test_recover_clears_the_pause_but_not_the_stored_data(self):
        db = p.open_db(self.home / 'proxies.sqlite3')
        try:
            db.execute('INSERT INTO source_state(source_id,endpoint_id,consecutive_failures,backoff_until,'
                       'quarantine_until,last_error) VALUES (?,?,?,?,?,?)',
                       ('cur-01', 'primary', 5, time.time() + 3600, time.time() + 3600, 'SOURCE_TIMEOUT'))
            db.execute("INSERT INTO source_generation(source_id,state,active,last_good,created_at,record_count)"
                       " VALUES ('cur-01','complete',1,1,?,3)", (time.time(),))
            db.commit()
        finally:
            db.close()
        row = self.client.get('/api/source-catalog/cur-01').json()
        self.assertEqual(row['state'], 'quarantined')
        self.assertEqual(row['runtime']['error'], 'SOURCE_TIMEOUT')
        self.assertEqual(row['cache']['last_good']['record_count'], 3)
        with mock.patch.object(self.server.app, 'preview_source', return_value={
                'accepted': 0, 'complete': False, 'error': 'SOURCE_TIMEOUT'}):
            self.assertEqual(self.client.post('/api/sources/recover', json={'id': 'cur-01'}).json()['cleared'], 1)
        row = self.client.get('/api/source-catalog/cur-01').json()
        self.assertNotEqual(row['state'], 'quarantined')
        self.assertEqual(row['cache']['last_good']['record_count'], 3)
        unknown = self.client.post('/api/sources/recover', json={'id': 'nope'})
        self.assertEqual(unknown.status_code, 200)
        self.assertFalse(unknown.json()['known'])
        self.assertEqual(unknown.json()['cleared'], 0)

    def test_update_reports_progress_and_never_selects_anything(self):
        incoming = dict(source_catalog.validate_catalog(dict(CATALOG), allow_unsafe=True), revision=CATALOG['revision'] + 1)
        added = {**next(source for source in incoming['sources'] if source['id'] == 'cur-01'),
                 'id': 'cur-77', 'name': 'Published after this build'}
        incoming['sources'] = [added] + [s for s in incoming['sources'] if s['id'] != 'cur-01']
        self.server.app.save(settings_with(['cur-01']))
        result = asyncio.run(  # the job runs in a thread; drive the same coroutine directly
            self._refresh_with(incoming))
        self.assertEqual(result['added'], ['cur-77'])
        self.assertEqual(result['running'], False)
        stored = self.stored()
        self.assertEqual(stored['source_selection']['selected_ids'], ['cur-01'])
        self.assertNotIn('cur-77', stored['sources'])
        view = self.client.get('/api/source-catalog?q=cur-77').json()
        self.assertEqual(view['total'], 1)
        self.assertFalse(view['sources'][0]['selected'])

    async def _refresh_with(self, incoming):
        async def fake_fetch(*args, **kwargs):
            return {'state': 'available', 'catalog': incoming}
        with mock.patch.object(p, 'fetch_catalog', fake_fetch):
            self.server.app.refresh_catalog({})
            for _ in range(50):
                job = self.server.app.catalog_update_status()
                if not job['running']:
                    return job
                await asyncio.sleep(0.02)
        self.fail('catalog refresh did not finish')

    def test_update_failure_keeps_the_local_catalog(self):
        with mock.patch.object(p, 'fetch_catalog', side_effect=RuntimeError('boom')):
            self.server.app.refresh_catalog({})
        for _ in range(50):
            job = self.server.app.catalog_update_status()
            if not job['running']:
                break
            time.sleep(0.02)
        self.assertEqual(job['stage'], 'error')
        self.assertFalse((self.home / 'source-catalog.json').exists())
        self.assertEqual(self.client.get('/api/source-catalog').json()['revision'], CATALOG['revision'])


class SourceCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db = p.open_db(self.home / 'proxies.sqlite3')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = p.main(['--data', str(self.home)] + argv)
        return code, out.getvalue(), err.getvalue()

    def stored(self):
        return json.loads((self.home / 'gui-settings.json').read_text(encoding='utf-8'))

    def test_list_and_show_print_catalog_data_in_both_languages(self):
        code, out, _ = self.run_cli(['sources', 'list', '--source-limit', '3', '--format', 'json'])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload['total'], len(CATALOG['sources']))
        self.assertEqual(len(payload['sources']), 3)
        with mock.patch.object(i18n, 'LANG', 'ru'):
            code, out, _ = self.run_cli(['sources', 'show', 'new-073'])
        self.assertEqual(code, 0)
        self.assertIn('прокси проверены: нет', out)
        self.assertIn('proxy_liveness: not_run', out)
        self.assertIn('первоисточник условий', out)
        with mock.patch.object(i18n, 'LANG', 'en'):
            code, out, _ = self.run_cli(['sources', 'show', 'new-073'])
        self.assertIn('proxies verified: no', out)
        self.assertIn('terms:', out)

    def test_list_filters_and_exit_codes(self):
        with mock.patch.object(i18n, 'LANG', 'ru'):
            code, out, _ = self.run_cli(['sources', 'list', '--source-state', 'needs_access',
                                         '--source-limit', '1'])
        self.assertEqual(code, 0)
        self.assertIn('не список прокси-адресов', out)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli(['sources', 'list', '--source-state', 'nope'])
        code, _, _ = self.run_cli(['sources', 'list', '--source-query', 'zzz-no-such-source',
                                   '--source-limit', '1'])
        self.assertEqual(code, 1)
        self.assertEqual(self.run_cli(['sources', 'frobnicate'])[0], 2)
        self.assertEqual(self.run_cli(['sources', 'list', '--format', 'hostport'])[0], 2)

    def test_set_enable_disable_remove_and_add_change_one_selection(self):
        self.db.close()
        gui.save_settings(self.home, settings_with(['cur-01']))
        self.db = p.open_db(self.home / 'proxies.sqlite3')
        code, out, _ = self.run_cli(['sources', 'set', 'quick'])
        self.assertEqual(code, 0)
        self.assertIn('Nothing was added implicitly', out)
        self.assertEqual(self.stored()['source_selection']['selected_ids'][:2], ['cur-02', 'cur-04'])
        code, _, _ = self.run_cli(['sources', 'disable', 'cur-02'])
        self.assertEqual(code, 0)
        self.assertEqual(self.stored()['source_selection']['download_disabled_ids'], ['cur-02'])
        self.assertIn('cur-02', self.stored()['source_selection']['selected_ids'])
        code, out, _ = self.run_cli(['sources', 'enable', 'cur-02'])
        self.assertIn('Download enabled', out)
        self.assertEqual(self.stored()['source_selection']['download_disabled_ids'], [])
        code, out, _ = self.run_cli(['sources', 'remove', 'cur-04'])
        self.assertIn('Removed from the set', out)
        self.assertNotIn('cur-04', self.stored()['source_selection']['selected_ids'])
        self.assertNotIn('cur-04', self.stored()['sources'])
        code, out, _ = self.run_cli(['sources', 'add', 'https://mine.example/rows.csv',
                                     '--source-format', 'page-json'])
        self.assertEqual(code, 0)
        self.assertIn('page-json https://mine.example/rows.csv', self.stored()['sources'])
        self.assertEqual(self.run_cli(['sources', 'disable', 'not-a-source'])[0], 2)
        self.assertEqual(self.stored()['sources'][-1], 'page-json https://mine.example/rows.csv')

    def test_cli_changes_keep_a_source_only_the_accepted_catalog_lists(self):
        catalog = accepted_catalog()
        (self.home / 'source-catalog.json').write_text(json.dumps(catalog), encoding='utf-8')
        self.db.close()
        gui.save_settings(self.home, settings_with(['new-999'], catalog=catalog))
        self.db = p.open_db(self.home / 'proxies.sqlite3')
        url = REMOTE_ONLY['data_urls'][0]
        for argv in (['sources', 'disable', 'cur-02'], ['sources', 'enable', 'cur-02'],
                     ['sources', 'remove', 'cur-04'],
                     ['sources', 'add', 'https://mine.example/rows.csv', '--source-format', 'line']):
            code, _, _ = self.run_cli(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn(url, self.stored()['sources'], argv)
            self.assertIn('new-999', self.stored()['source_selection']['selected_ids'], argv)

    def test_check_uses_a_local_service_and_writes_no_candidates(self):
        body = json.dumps([{'ip': '11.3.3.3', 'port': 1080, 'protocol': 'socks5'},
                           {'ip': '11.3.3.4', 'port': 1080, 'protocol': 'smtp'}]).encode()
        with MockSourceService(body) as service:
            url = service.base + '/socks.json'
            self.db.close()
            gui.save_settings(self.home, gui.defaults())
            code, out, _ = self.run_cli(['sources', 'add', url, '--source-format', 'json-records',
                                         '--allow-private-sources'])
            self.assertEqual(code, 0)
            source_id = [item['id'] for item in self.stored()['source_selection']['custom_sources']][-1]
            self.db = p.open_db(self.home / 'proxies.sqlite3')
            code, out, _ = self.run_cli(['sources', 'check', source_id, '--allow-private-sources'])
        self.assertEqual(code, 0)
        self.assertIn('recognized 1', out)
        self.assertIn('accepted 1', out)
        self.assertIn('rejected 1', out)
        self.assertIn('unsupported_protocol=1', out)
        self.assertIn('no proxy was checked', out)
        self.assertEqual(self.db.execute('SELECT count(*) FROM candidates').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_observation').fetchone()[0], 0)
        code, out, _ = self.run_cli(['sources', 'check', 'new-088'])
        self.assertIn('not a list of proxy addresses', out)

    def test_legacy_commands_and_flags_still_parse(self):
        with self.assertRaises(SystemExit):
            self.run_cli(['--version'])
        code, _, _ = self.run_cli(['get', '--format', 'hostport'])
        self.assertEqual(code, 1)
        code, out, _ = self.run_cli(['sources', 'sets', '--format', 'json'])
        self.assertEqual(code, 0)
        self.assertEqual([item['id'] for item in json.loads(out)][0], 'quick')

    def test_json_preview_is_one_document_and_add_keeps_the_name(self):
        with MockSourceService(b'11.4.4.4:8080\n') as service:
            code, out, err = self.run_cli([
                'source', 'add', service.base + '/list', '--source-name', 'Local fixture',
                '--source-format', 'socks5', '--allow-private-sources', '--format', 'json'])
            self.assertEqual(code, 0, err)
            source_id = json.loads(out)['id']
            custom = next(row for row in self.stored()['source_selection']['custom_sources']
                          if row['id'] == source_id)
            self.assertEqual(custom['name'], 'Local fixture')
            code, out, err = self.run_cli(['source', 'check', '--id', source_id,
                                         '--allow-private-sources', '--format', 'json'])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)['sample'], ['socks5://11.4.4.4:8080'])

    def test_update_accepts_a_new_revision_and_refuses_same_revision_changes(self):
        raw = json.loads(source_catalog.bundled_path().read_text(encoding='utf-8'))
        raw['sources'] = [row for row in raw['sources'] if row['id'] != 'new-082']
        raw['sets'] = []
        raw['revision'] = CATALOG['revision'] + 1
        incoming = self.home / 'incoming.json'
        incoming.write_text(json.dumps(raw), encoding='utf-8')
        gui.save_settings(self.home, settings_with(['cur-01']))
        selected = self.stored()['source_selection']['selected_ids']
        code, out, err = self.run_cli(['source', 'update', str(incoming), '--json'])
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)['accepted'])
        target = self.home / 'source-catalog.json'
        accepted = target.read_bytes()
        self.assertEqual(json.loads(accepted)['revision'], raw['revision'])
        self.assertEqual(self.stored()['source_selection']['selected_ids'], selected)
        raw['sources'][0]['name'] = 'Changed without a new revision'
        incoming.write_text(json.dumps(raw), encoding='utf-8')
        self.assertEqual(self.run_cli(['source', 'update', '--path', str(incoming)])[0], 2)
        self.assertEqual(target.read_bytes(), accepted)

    def test_recover_clears_every_endpoint_and_preserves_cached_state(self):
        for endpoint in ('primary', 'mirror'):
            self.db.execute('INSERT INTO source_state(source_id,endpoint_id,etag,consecutive_failures,'
                            'quarantine_until,backoff_until) VALUES (?,?,?,?,?,?)',
                            ('cur-01', endpoint, 'saved-etag', 3, time.time() + 300, time.time() + 300))
        self.db.commit()
        code, out, err = self.run_cli(['source', 'recover', '--id', 'cur-01', '--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['cleared'], 2)
        rows = self.db.execute('SELECT etag,consecutive_failures,quarantine_until,backoff_until '
                               'FROM source_state WHERE source_id=?', ('cur-01',)).fetchall()
        self.assertEqual([tuple(row) for row in rows], [('saved-etag', 0, None, None)] * 2)
        unknown = json.loads(self.run_cli(['source', 'recover', 'unknown', '--json'])[1])
        self.assertFalse(unknown['known'])
        self.assertEqual(unknown['cleared'], 0)


class SourceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        db = p.open_db(self.home / 'proxies.sqlite3')
        db.execute('INSERT INTO candidates(proxy) VALUES (?)', ('http://11.4.4.4:80',))
        db.execute('INSERT INTO candidate_seen(proxy,source) VALUES (?,?)', ('http://11.4.4.4:80', 'cur-01'))
        db.execute('INSERT INTO source_identity(source_id,family_id,publisher_id) VALUES (?,?,?)',
                   ('cur-01', 'family-a', 'p'))
        db.execute("INSERT INTO source_generation(source_id,state,active,last_good,created_at,record_count)"
                   " VALUES ('cur-01','complete',1,1,?,1)", (time.time(),))
        generation = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        db.execute('INSERT INTO source_generation_entry(generation_id,endpoint_id) VALUES (?,?)',
                   (generation, p.schema.endpoint_id('http://11.4.4.4:80')))
        db.execute("INSERT INTO source_observation(run_id,source_id,endpoint_id,started_at,http_state,parse_state,"
                   "cache_state,outcome,recognized,accepted,rejected) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   ('run-1', 'cur-01', 'primary', time.time(), 'http_2xx_nonempty', 'confirmed',
                    'last_good', 'available', 5, 4, 1))
        db.commit()
        db.close()
        gui.save_settings(self.home, settings_with(['cur-01']))
        self.server = api.make_api_server(self.home, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.server.server_port}',
                                   trust_env=False, timeout=10)

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_sources_endpoints_are_read_only_and_filterable(self):
        body = self.client.get('/sources?limit=2').json()
        self.assertEqual(body['total'], len(CATALOG['sources']))
        self.assertEqual(len(body['sources']), 2)
        self.assertIn('/sources', self.client.get('/status').json()['endpoints'])
        filtered = self.client.get('/sources?state=has_data&limit=50').json()
        self.assertEqual([row['id'] for row in filtered['sources']], ['cur-01'])
        self.assertEqual(self.client.get('/sources?set=quick&limit=50').json()['total'], 8)
        self.assertEqual(self.client.get('/sources?limit=0').status_code, 400)
        self.assertEqual(self.client.get('/sources?state=bogus').status_code, 400)
        self.assertEqual(self.client.get('/sources/not-a-source').status_code, 404)
        self.assertEqual(self.client.get('/sources/a/b').status_code, 404)
        self.assertEqual(self.client.get('/source-sets').json()['sets'][0]['id'], 'quick')

    def test_detail_reports_runtime_but_never_endpoint_paths_or_queries(self):
        detail = self.client.get('/sources/cur-01').json()
        self.assertEqual(detail['id'], 'cur-01')
        self.assertEqual(detail['state'], 'last_good')
        self.assertEqual(detail['selection_state'], 'selected')
        self.assertEqual(detail['runtime']['contribution']['exclusive'], 1)
        self.assertEqual(detail['runtime']['recognized'], 5)
        self.assertEqual(detail['evidence']['proxy_liveness']['state'], 'not_run')
        for endpoint in detail['endpoints']:
            self.assertNotIn('?', endpoint['url'])
            self.assertTrue(endpoint['url'].startswith('https://raw.githubusercontent.com/'))
        self.assertNotIn('MuRongPIG/Proxy-Master/main/http.txt', json.dumps(detail))

    def test_public_api_refuses_every_management_operation(self):
        for path in ('/sources/toggle', '/sources/set', '/sources/preview', '/sources/remove',
                     '/sources/add', '/sources/update', '/sources/exclude-scope', '/sources/recover'):
            with self.subTest(path=path):
                self.assertIn(self.client.post(path, json={'id': 'cur-01'}).status_code, (404, 405, 501))
        self.assertTrue((self.home / 'gui-settings.json').is_file())
        self.assertEqual(json.loads((self.home / 'gui-settings.json').read_text(encoding='utf-8'))
                         ['source_selection']['selected_ids'], ['cur-01'])


class SourceV1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        p.open_db(self.home / 'proxies.sqlite3').close()

        class Keys(apiv1.KeyStore):
            def verify(self, secret):
                if secret == 'local-source-test':
                    return apiv1.Principal(key_id='source-test', kind='api_key',
                                           permissions=frozenset(apiv1.PERMISSIONS))

            def audit(self, record):
                pass

        self.control = apiv1.ApiV1(api.WorkbenchService(self.home), Keys(),
                                   apiv1.ApiConfig(host='127.0.0.1'))
        self.request_number = 0

    def tearDown(self):
        self.temp.cleanup()

    def request(self, method, path, query='', body=None):
        self.request_number += 1
        return self.control.handle(apiv1.Request(
            method=method, path=path, query=query,
            headers={'Host': '127.0.0.1', 'Authorization': 'Bearer local-source-test',
                     'Content-Type': 'application/json',
                     'Idempotency-Key': f'source-test-{self.request_number}'},
            body=json.dumps(body).encode() if body is not None else b'', client_host='127.0.0.1'))

    def test_filter_pagination_and_accepted_catalog_are_shared_with_the_cli(self):
        catalog = accepted_catalog()
        (self.home / 'source-catalog.json').write_text(json.dumps(catalog), encoding='utf-8')
        gui.save_settings(self.home, settings_with(['new-999'], catalog=catalog))
        original = (self.home / 'gui-settings.json').read_bytes()
        found = self.request('GET', '/v1/sources/catalog', 'q=new-999&limit=1')
        self.assertEqual(found.status, 200, found.text)
        self.assertEqual(found.json()['revision'], catalog['revision'])
        self.assertEqual([row['id'] for row in found.json()['items']], ['new-999'])
        first = self.request('GET', '/v1/sources', 'limit=2')
        self.assertEqual(first.status, 200, first.text)
        self.assertEqual(len(first.json()['items']), 2)
        cursor = first.json()['next_cursor']
        self.assertTrue(cursor)
        from urllib.parse import urlencode
        second = self.request('GET', '/v1/sources', urlencode({'limit': 2, 'cursor': cursor}))
        self.assertEqual(second.status, 200, second.text)
        self.assertEqual(len(second.json()['items']), 2)
        self.assertTrue({row['id'] for row in first.json()['items']}.isdisjoint(
            row['id'] for row in second.json()['items']))
        self.assertEqual((self.home / 'gui-settings.json').read_bytes(), original)
        self.assertEqual(self.request('GET', '/v1/sources', 'limit=0').status, 400)

    def test_custom_source_format_survives_create_update_and_pause(self):
        created = self.request('POST', '/v1/sources', body={
            'url': 'https://mine.example/rows.json', 'format': 'json'})
        self.assertIn(created.status, (200, 201), created.text)
        source_id = created.json()['id']
        custom = self.request('GET', '/v1/sources/' + source_id)
        self.assertEqual(custom.status, 200, custom.text)
        self.assertEqual(custom.json()['adapter'], 'json-records')
        paused = self.request('POST', '/v1/sources/' + source_id + '/disable', body={})
        self.assertEqual(paused.status, 200, paused.text)
        updated = self.request('PATCH', '/v1/sources/' + source_id,
                               body={'url': 'https://mine.example/updated.json', 'revision': 1})
        self.assertEqual(updated.status, 200, updated.text)
        replacement_id = updated.json()['id']
        stored = json.loads((self.home / 'gui-settings.json').read_text(encoding='utf-8'))
        self.assertIn(replacement_id, stored['source_selection']['download_disabled_ids'])
        self.assertNotIn(source_id, stored['source_selection']['download_disabled_ids'])
        item = self.request('GET', '/v1/sources/' + replacement_id).json()
        self.assertEqual(item['adapter'], 'json-records')
        self.assertTrue(item['download_disabled'])

    def test_unsupported_source_overrides_are_refused_without_changing_settings(self):
        for field, value in (('interval_minutes', 10), ('max_bytes', 1024)):
            denied = self.request('POST', '/v1/sources', body={
                'url': 'https://mine.example/rows.json', 'format': 'json', field: value})
            self.assertEqual(denied.status, 400, denied.text)
            self.assertEqual(denied.json()['error']['details']['field'], field)
            self.assertFalse((self.home / 'gui-settings.json').exists())
        created = self.request('POST', '/v1/sources', body={
            'url': 'https://mine.example/rows.json', 'format': 'json'})
        source_id = created.json()['id']
        original = (self.home / 'gui-settings.json').read_bytes()
        for field, value in (('interval_minutes', 10), ('max_bytes', 1024)):
            denied = self.request('PATCH', '/v1/sources/' + source_id,
                                  body={'revision': 1, field: value})
            self.assertEqual(denied.status, 400, denied.text)
            self.assertEqual(denied.json()['error']['details']['field'], field)
            self.assertEqual((self.home / 'gui-settings.json').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()

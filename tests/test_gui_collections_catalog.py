"""F02 and F06 in the interface: choosing a collection and the service catalog.

Both functions were written as modules and were unreachable from the product:
the interface had no collection control and no service catalog at all, so the
functional acceptance reported them as missing.  These tests drive the running
loopback interface over HTTP, the way a user does, and check three things:

* the user can *reach* the function (the control exists, the server answers,
  the address survives the round trip);
* the answer is right on local fixtures (two personal collections and the public
  base do not mix, a set writes the targets of its own services, a definition
  diff is a real diff);
* a second surface reads the same truth, so a set applied in the interface is
  the one the next check measures.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.workbench_support import add_candidate, store_result  # noqa: E402
from proxy_workbench import db, gui, proxytool as p  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
UI = REPO/'proxy_workbench'/'ui'


class InterfaceCase(unittest.TestCase):
    """A real interface on loopback, with a data folder of its own."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.server = gui.make_server(self.home)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.server.server_port}',
                                   trust_env=False, headers={'X-Workbench-Token': self.server.app.token},
                                   timeout=10)

    def tearDown(self):
        self.server.app.close()
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def seed(self, rows, *, scope=None):
        """One profile, published, with a measured row per (address, collection).

        ``scope`` is the collection the publication is about, the same argument a
        check gets from the interface.  Without it the scope is the public base.
        """
        conn = p.open_db(self.home/'proxies.sqlite3')
        cfg = dict(targets=[dict(url='https://one.invalid/')], min_success=1)
        conn.execute('INSERT OR IGNORE INTO profiles(id, config) VALUES (?, ?)',
                     ('fixture', json.dumps(cfg)))
        for proxy, membership in rows:
            add_candidate(conn, proxy, collection_id=membership)
            row = p.summarize(proxy, [dict(target=0, attempt=1, ok=True, ms=10, bytes=1,
                                           status=200, error=None)], cfg)
            store_result(conn, ('fixture', proxy, json.dumps(row)), collection_id=membership or None)
        conn.commit()
        # The publication is what the results table reads, exactly as a check leaves it.
        p.export(conn, 'fixture', self.home/'exports', min_success=1, collection_id=scope)
        conn.close()
        (self.home/'last-profile.txt').write_text('fixture', encoding='utf-8')
        return cfg


class CollectionScopeTests(InterfaceCase):
    """F02: the user chooses which collection a check and the table are about."""

    def test_the_interface_offers_the_collections_that_exist(self):
        self.seed([('http://198.51.100.7:8080', None)])
        response = self.client.get('/api/collections')
        self.assertEqual(response.status_code, 200, response.text)
        items = {item['id']: item for item in response.json()['collections']}
        self.assertIn(db.PUBLIC_COLLECTION_ID, items)
        self.assertIn(db.LEGACY_COLLECTION_ID, items)
        self.assertEqual(items[db.PUBLIC_COLLECTION_ID]['members'], 1)
        self.assertEqual(response.json()['default_id'], db.PUBLIC_COLLECTION_ID)

    def test_a_personal_collection_starts_empty_and_stays_separate(self):
        self.seed([('http://198.51.100.7:8080', None)])
        created = self.client.post('/api/collections', json={'name': 'Мои прокси'})
        self.assertEqual(created.status_code, 200, created.text)
        mine = created.json()['collection']
        items = {item['id']: item for item in self.client.get('/api/collections').json()['collections']}
        self.assertEqual(items[mine]['members'], 0)
        self.assertEqual(items[mine]['kind'], 'private')
        self.assertEqual(items[db.PUBLIC_COLLECTION_ID]['members'], 1)
        members = self.client.get(f'/api/collections/members?collection={mine}').json()
        self.assertEqual(members['total'], 0, 'a personal list must not inherit public addresses')
        added = self.client.post('/api/collections/members', json={
            'collection': mine,
            'proxies': '198.51.100.7:8080\nплохая строка\nsocks5://198.51.100.9:1080'})
        self.assertEqual(added.status_code, 200, added.text)
        body = added.json()
        self.assertEqual((body['added'], body['already'], body['rejected_total']), (2, 0, 1))
        self.assertEqual([item['proxy'] for item in body['members']['members']],
                         ['http://198.51.100.7:8080', 'socks5://198.51.100.9:1080'])
        # The same address now lives in two lists, which is the point of F02.
        public = self.client.get(f'/api/collections/members?collection={db.PUBLIC_COLLECTION_ID}').json()
        self.assertIn('http://198.51.100.7:8080', [item['proxy'] for item in public['members']])

    def test_removing_a_member_keeps_the_address_in_every_other_list(self):
        self.seed([('http://198.51.100.7:8080', None)])
        mine = self.client.post('/api/collections', json={'name': 'Работа'}).json()['collection']
        self.client.post('/api/collections/members',
                         json={'collection': mine, 'proxies': '198.51.100.7:8080'})
        removed = self.client.post('/api/collections/member-remove',
                                   json={'collection': mine, 'proxy': '198.51.100.7:8080'})
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(removed.json()['members']['total'], 0)
        public = self.client.get(f'/api/collections/members?collection={db.PUBLIC_COLLECTION_ID}').json()
        self.assertEqual([item['proxy'] for item in public['members']], ['http://198.51.100.7:8080'])
        again = self.client.post('/api/collections/member-remove',
                                 json={'collection': mine, 'proxy': '198.51.100.7:8080'})
        self.assertEqual(again.status_code, 400)
        self.assertIn('нет в коллекции', again.json()['error'])

    def test_the_results_table_is_about_the_chosen_collection_only(self):
        conn = p.open_db(self.home/'proxies.sqlite3')
        mine = db.create_collection(conn, 'Дом', kind='private')
        add_candidate(conn, 'http://203.0.113.4:3128', collection_id=mine)
        conn.commit()
        conn.close()
        rows = [('http://198.51.100.7:8080', None), ('http://203.0.113.4:3128', mine)]
        # A publication about the public base says nothing about the personal list.
        self.seed(rows)
        public_scope = self.client.get(
            f'/api/results?min_success=0&collection={db.PUBLIC_COLLECTION_ID}').json()
        self.assertEqual([row['proxy'] for row in public_scope['rows']], ['http://198.51.100.7:8080'])
        everything = self.client.get('/api/results?min_success=0').json()
        self.assertEqual(everything['total'], 1)
        self.assertEqual(everything['measured_collection'], db.PUBLIC_COLLECTION_ID)
        # The same check, published about the personal list: the two never mix.
        self.seed(rows, scope=mine)
        scoped = self.client.get(f'/api/results?min_success=0&collection={mine}').json()
        self.assertEqual([row['proxy'] for row in scoped['rows']], ['http://203.0.113.4:3128'])
        self.assertEqual(scoped['collection'], mine)
        self.assertEqual(scoped['measured_collection'], mine)
        self.assertEqual(self.client.get('/api/results?min_success=0').json()['total'], 1)
        empty = self.client.get('/api/results?min_success=0&collection=legacy-collected').json()
        self.assertEqual(empty['total'], 0, 'a collection with no members shows nothing')
        # A different scope is a different scope: the digest has to say so, or a
        # selection made in one collection would be applied to another.
        self.assertNotEqual(scoped['scope_digest'], empty['scope_digest'])
        self.assertNotEqual(scoped['scope_digest'], everything['scope_digest'])

    def test_an_unknown_collection_is_refused_instead_of_answered_as_empty(self):
        self.seed([('http://198.51.100.7:8080', None)])
        missing = self.client.get('/api/results?collection=col-does-not-exist')
        self.assertEqual(missing.status_code, 400)
        self.assertIn('Коллекция не найдена', missing.json()['error'])
        members = self.client.get('/api/collections/members?collection=col-does-not-exist')
        self.assertEqual(members.status_code, 400)

    def test_the_saved_scope_reaches_the_worker_of_a_check(self):
        self.seed([('http://198.51.100.7:8080', None)])
        conn = p.open_db(self.home/'proxies.sqlite3')
        mine = db.create_collection(conn, 'Дом', kind='private')
        add_candidate(conn, 'http://203.0.113.4:3128', collection_id=mine)
        conn.commit()
        conn.close()
        settings = gui.defaults()
        settings['collection'] = mine
        settings['sources'] = []
        settings['proxies'] = '198.51.100.7:8080'
        saved = self.client.post('/api/settings', json=settings)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['collection'], mine)
        recorded = {}

        class Process:
            def poll(self):
                return None
        with mock.patch('subprocess.Popen') as popen:
            popen.return_value = Process()
            popen.return_value.wait = lambda timeout=None: 0
            self.client.post('/api/start', json=dict(action='scan', settings=settings))
            recorded['command'] = popen.call_args[0][0]
        command = recorded['command']
        self.assertIn('--collection', command, command)
        self.assertEqual(command[command.index('--collection') + 1], mine)
        self.assertIn('scan', command)
        # The job the interface reports names the same scope.
        self.assertEqual(json.loads((self.home/'gui-job.json').read_text(encoding='utf-8'))['collection'],
                         mine)

    def test_a_refused_scope_stops_the_check_before_the_worker_starts(self):
        self.seed([('http://198.51.100.7:8080', None)])
        settings = gui.defaults()
        settings['collection'] = 'col-not-in-the-database'
        settings['sources'] = []
        settings['proxies'] = '198.51.100.7:8080'
        with mock.patch('subprocess.Popen') as popen:
            response = self.client.post('/api/start', json=dict(action='scan', settings=settings))
        self.assertEqual(response.status_code, 400)
        self.assertIn('Коллекция не найдена', response.json()['error'])
        popen.assert_not_called()

    def test_the_membership_editor_refuses_credentials_and_never_writes_them(self):
        self.seed([('http://198.51.100.7:8080', None)])
        mine = self.client.post('/api/collections', json={'name': 'Работа'}).json()['collection']
        canary = 'canary7f3a91c4'
        refused = self.client.post('/api/collections/members', json={
            'collection': mine, 'proxies': f'http://user:{canary}@198.51.100.7:8080'})
        self.assertEqual(refused.status_code, 200, refused.text)
        self.assertEqual(refused.json()['added'], 0)
        self.assertEqual(refused.json()['rejected_total'], 1)
        members = self.client.get(f'/api/collections/members?collection={mine}').json()
        self.assertEqual(members['total'], 0)
        self.assertNotIn(canary.encode(), (self.home/'proxies.sqlite3').read_bytes())
        self.assertNotIn(canary.encode(), (self.home/'gui-run.log').read_bytes()
                         if (self.home/'gui-run.log').is_file() else b'')


class ServiceCatalogTests(InterfaceCase):
    """F06: the catalog of versioned service definitions and sets of them."""

    def test_the_catalog_is_readable_and_states_what_each_service_proves(self):
        view = self.client.get('/api/service-catalog').json()
        self.assertGreaterEqual(view['total'], 9, 'the nine presets the interface had must be here')
        by_id = {preset['id']: preset for preset in view['presets']}
        for wanted in ('google-204', 'telegram-home', 'discord-gateway', 'github-home'):
            self.assertIn(wanted, by_id, sorted(by_id))
        telegram = by_id['telegram-home']
        self.assertEqual(telegram['version'], 1)
        self.assertTrue(telegram['maintainer'])
        self.assertEqual(len(telegram['probes']), 1)
        probe = telegram['probes'][0]
        self.assertEqual(probe['url'], 'https://telegram.org/')
        self.assertIn('200', probe['pass_condition'])
        self.assertTrue(telegram['not_proved_ru'], 'a service must say what it does not prove')
        self.assertIn(telegram['verification'], ('documented', 'legacy_definition', 'unverified_live'))
        self.assertTrue(telegram['definition_checked_on'])
        self.assertTrue(telegram['capability_title'])
        # The names homepage / API / WebSocket / media must stay distinguishable.
        capabilities = {item['id'] for item in view['catalog']['capabilities']}
        self.assertTrue({'homepage', 'api', 'websocket', 'media', 'long_connection'} <= capabilities)

    def test_search_and_the_category_filter_narrow_the_list(self):
        every = self.client.get('/api/service-catalog').json()
        self.assertEqual(self.client.get('/api/service-catalog?q=telegram').json()['total'], 1)
        self.assertEqual(self.client.get('/api/service-catalog?category=messengers').json()['total'], 3)
        self.assertEqual(self.client.get('/api/service-catalog?capability=status_endpoint').json()['total'], 3)
        both = self.client.get('/api/service-catalog?category=messengers&q=signal').json()
        self.assertEqual([preset['id'] for preset in both['presets']], ['signal-home'])
        unknown = self.client.get('/api/service-catalog?category=nope')
        self.assertEqual(unknown.status_code, 400)
        self.assertTrue(self.client.get('/api/service-catalog').json()['total'] < every['total'] + 1)

    def test_every_shipped_set_names_its_rule_and_its_services(self):
        view = self.client.get('/api/service-catalog').json()
        by_id = {item['id']: item for item in view['sets']}
        for wanted in ('basic', 'search', 'messengers', 'social', 'video', 'development', 'api'):
            self.assertIn(wanted, by_id, sorted(by_id))
            item = by_id[wanted]
            self.assertIn(item['combination'], ('all', 'any', 'at_least', 'none'))
            self.assertTrue(item['required'], f'{wanted} has no mandatory service')
            self.assertGreater(item['probes'], 0)
            self.assertTrue(item['digest'])
        self.assertEqual(by_id['video']['combination'], 'none')
        self.assertEqual(by_id['search']['combination'], 'at_least')
        self.assertEqual(by_id['search']['min_passes'], 1)

    def test_applying_a_set_writes_its_targets_and_its_whole_field_set(self):
        applied = self.client.post('/api/service-catalog/apply', json={'set': 'messengers'})
        self.assertEqual(applied.status_code, 200, applied.text)
        body = applied.json()
        settings = body['settings']
        urls = [target['url'] for target in settings['targets']]
        self.assertEqual(urls, ['https://telegram.org/', 'https://discord.com/api/v10/gateway',
                                'https://signal.org/'])
        self.assertEqual(settings['protocol'], 'socks5', 'the set owns the protocol field')
        self.assertTrue(body['report']['set'], 'applying a set must report what it changed')
        self.assertIn('workers', body['report']['carried_over'],
                      'a user field is carried over and reported, never set by a set')
        self.assertEqual(body['cost']['presets'], 3)
        # The set fills the form; the next check measures exactly those targets.
        saved = self.client.post('/api/settings', json=settings)
        self.assertEqual(saved.status_code, 200, saved.text)
        stored = self.client.get('/api/settings').json()
        self.assertEqual([target['url'] for target in stored['targets']], urls)
        self.assertEqual(stored['protocol'], 'socks5')
        self.assertTrue((self.home/'gui-service-sets.json').is_file())
        self.assertEqual(json.loads((self.home/'gui-service-sets.json').read_text(
            encoding='utf-8'))['pinned']['set_id'], 'messengers')

    def test_a_saved_set_survives_a_restart_and_a_catalog_rename_does_not_touch_it(self):
        saved = self.client.post('/api/service-catalog/save', json={
            'id': 'my-set', 'title': 'Мои сервисы',
            'preset_ids': ['telegram-home', 'signal-home'], 'combination': 'any'})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['set']['origin'], 'user')
        self.assertEqual(saved.json()['set']['required'], ['telegram-home'])
        listed = {item['id'] for item in self.client.get('/api/service-catalog').json()['sets']}
        self.assertIn('my-set', listed)
        self.server.app.close()
        self.server = gui.make_server(self.home)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client.close()
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.server.server_port}',
                                   trust_env=False, headers={'X-Workbench-Token': self.server.app.token},
                                   timeout=10)
        again = self.client.get('/api/service-catalog').json()
        self.assertIn('my-set', {item['id'] for item in again['sets']})
        detail = self.client.get('/api/service-catalog/set?set=my-set').json()
        self.assertEqual(detail['set']['combination'], 'any')
        self.assertEqual(len(detail['presets']), 2)
        self.assertEqual(self.client.post('/api/service-catalog/delete',
                                          json={'set': 'my-set'}).status_code, 200)
        self.assertNotIn('my-set', {item['id'] for item in
                                    self.client.get('/api/service-catalog').json()['sets']})
        self.assertEqual(self.client.post('/api/service-catalog/delete',
                                          json={'set': 'my-set'}).status_code, 400)

    def test_a_selection_without_a_name_is_a_set_the_user_can_measure(self):
        applied = self.client.post('/api/service-catalog/apply', json={
            'set': 'selection', 'title': 'Моя проверка',
            'preset_ids': ['google-204', 'cloudflare-trace'],
            'combination': 'any', 'min_passes': 1})
        self.assertEqual(applied.status_code, 200, applied.text)
        urls = [target['url'] for target in applied.json()['settings']['targets']]
        self.assertEqual(urls, ['https://www.google.com/generate_204',
                                'https://www.cloudflare.com/cdn-cgi/trace'])
        self.assertEqual(applied.json()['set']['id'], 'selection')
        for bad in ({'set': 'selection', 'preset_ids': []},
                    {'set': 'selection', 'preset_ids': ['no-such-service']},
                    {'set': 'selection', 'preset_ids': ['google-204'], 'combination': 'sideways'}):
            self.assertEqual(self.client.post('/api/service-catalog/apply', json=bad).status_code, 400, bad)

    def test_a_set_definition_is_never_changed_behind_the_users_back(self):
        self.assertEqual(self.client.post('/api/service-catalog/apply', json={'set': 'basic'}).status_code, 200)
        view = self.client.post('/api/service-catalog/update', json={'set': 'basic'}).json()
        self.assertEqual(view['set_id'], 'basic')
        self.assertEqual(view['update']['set_state'], 'current')
        self.assertTrue(view['changes'], 'the diff names every service of the set')
        for change in view['changes']:
            self.assertEqual(change['state'], 'unchanged',
                             'the shipped manifest and itself cannot differ')
            self.assertIn('from_digest', change)
            self.assertIn('to_digest', change)
        applied = self.client.post('/api/service-catalog/update', json={'set': 'basic', 'apply': True})
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()['applied'], 0)
        # A definition that does change is reported as such, with both digests.
        stored = json.loads((self.home/'gui-service-sets.json').read_text(encoding='utf-8'))
        snapshot = stored['sets']['basic']
        snapshot['presets']['google-204']['probes'][0]['url'] = 'https://www.google.com/generate_204x'
        snapshot['presets']['google-204']['version'] = 2
        stored['sets']['basic'] = snapshot
        stored['pinned'] = snapshot
        (self.home/'gui-service-sets.json').write_text(json.dumps(stored), encoding='utf-8')
        self.server.app.service_sets.read = lambda: stored
        self.server.app.service_sets.write = lambda payload: stored.update(payload)
        changed = self.client.post('/api/service-catalog/update', json={'set': 'basic'}).json()
        states = {change['preset_id']: change for change in changed['changes']}
        self.assertEqual(states['google-204']['state'], 'updated')
        self.assertEqual(states['google-204']['from_version'], 2)
        self.assertEqual(states['google-204']['to_version'], 1)
        self.assertNotEqual(states['google-204']['from_digest'], states['google-204']['to_digest'])
        self.assertTrue(states['google-204']['breaking'], 'a changed probe can move a verdict')
        self.assertFalse(states['cloudflare-trace']['breaking'])
        self.assertEqual(states['cloudflare-trace']['state'], 'unchanged')
        self.assertTrue(states['google-204']['changed_fields'],
                        'the diff names the fields that differ')
        self.assertEqual(self.client.post('/api/service-catalog/update',
                                          json={'set': 'basic', 'apply': True}).status_code, 200)
        healed = self.client.post('/api/service-catalog/update', json={'set': 'basic'}).json()
        self.assertTrue(all(change['state'] == 'unchanged' for change in healed['changes']))

    def test_a_saved_set_needs_a_mandatory_service_and_a_known_rule(self):
        for bad in ({'id': 'Bad Id', 'title': 'x', 'preset_ids': ['google-204']},
                    {'id': 'ok-id', 'title': '  ', 'preset_ids': ['google-204']},
                    {'id': 'ok-id', 'title': 'x', 'preset_ids': []},
                    {'id': 'ok-id', 'title': 'x', 'preset_ids': ['google-204'], 'combination': 'maybe'}):
            response = self.client.post('/api/service-catalog/save', json=bad)
            self.assertEqual(response.status_code, 400, bad)
        stored = self.home/'gui-service-sets.json'
        self.assertFalse(stored.is_file() and json.loads(stored.read_text(
            encoding='utf-8')).get('sets'), 'a refused set must not be written')


class ServedPageTests(InterfaceCase):
    """What the browser downloads: the controls exist and the script calls the server."""

    def test_the_served_page_carries_both_controls_and_their_calls(self):
        page = self.client.get('/').text
        for marker in ('id="collection-scope"', 'id="collection-list"', 'id="collection-members"',
                       'id="result-collection"', 'id="service-catalog"', 'id="svc-list"'):
            self.assertIn(marker, page, marker)
        script = self.client.get('/app.js').text
        self.assertIn("api('/api/collections'", script)
        self.assertIn("api('/api/collections/members'", script)
        self.assertIn("'/api/service-catalog?'", script)
        self.assertIn("copy.collection = val('collection-scope'", script)
        self.assertIn("collection: $('result-collection')", script)

    def test_every_control_the_new_code_reads_exists_in_the_served_page(self):
        page = self.client.get('/').text
        ids = set(re.findall(r'\bid="([^"]+)"', page))
        script = self.client.get('/app.js').text
        wanted = {name for name in re.findall(r"\$\('([a-zA-Z0-9_-]+)'\)", script)
                  if name.startswith(('svc-', 'collection-', 'col-')) or name == 'result-collection'}
        self.assertTrue(wanted, 'the new code reads at least one control')
        self.assertEqual(wanted - ids, set(),
                         'a control the code reads but the page does not have is an unreachable path')

    def test_the_stylesheet_carries_the_new_rows(self):
        style = self.client.get('/style.css').text
        for marker in ('.svc-row', '.svc-selection', '.collection-row', '.collection-member'):
            self.assertIn(marker, style, marker)


class InterfaceMarkupTests(unittest.TestCase):
    """The controls exist in the page and the page reads what the server sends."""

    @classmethod
    def setUpClass(cls):
        cls.page = (UI/'index.html').read_text(encoding='utf-8')
        cls.script = (UI/'app.js').read_text(encoding='utf-8')
        cls.style = (UI/'style.css').read_text(encoding='utf-8')

    def test_the_collection_control_is_on_the_page_and_bound_to_the_settings(self):
        self.assertIn('id="collection-scope"', self.page)
        self.assertIn('id="collection-list"', self.page)
        self.assertIn('id="collection-members"', self.page)
        self.assertIn('id="result-collection"', self.page)
        self.assertIn("copy.collection = val('collection-scope'", self.script)
        self.assertIn("api('/api/collections'", self.script)
        self.assertIn("api('/api/collections/members'", self.script)
        self.assertIn("collection: $('result-collection')", self.script)
        self.assertIn('Коллекция', self.script)
        # The word the acceptance looks for has to be the user's own word.
        self.assertRegex(self.script.lower(), r'коллекц')

    def test_the_service_catalog_is_on_the_page_with_sets_probes_and_a_diff(self):
        for marker in ('id="service-catalog"', 'id="svc-sets"', 'id="svc-list"', 'id="svc-detail"',
                       'id="svc-rule"', 'id="svc-save-form"'):
            self.assertIn(marker, self.page, marker)
        self.assertIn("'/api/service-catalog?'", self.script)
        for call in ("api('/api/service-catalog/apply'", "api('/api/service-catalog/save'",
                     "api('/api/service-catalog/update'", "api('/api/service-catalog/delete'"):
            self.assertIn(call, self.script, call)
        self.assertRegex(self.script, r'набор сервисов|service.?set|serviceSet', )
        self.assertIn('.svc-row', self.style)
        self.assertIn('.collection-row', self.style)

    def test_every_new_key_is_translated_in_both_languages(self):
        block = self.script[self.script.index('const messages = {'):self.script.index('\n};\n')]
        english, russian = block.split('\n  ru: {')
        keys = lambda text: set(re.findall(r"^    '([\w.]+)': '((?:[^'\\]|\\.)*)',?$", text, re.M))
        found = {key for key, _ in keys(english)} & {key for key, _ in keys(russian)}
        for group in ('svc.', 'col.'):
            for key in sorted(key for key in found if key.startswith(group)):
                self.assertIn(key, found, key)
        self.assertTrue({key for key in found if key.startswith('svc.')})
        self.assertTrue({key for key in found if key.startswith('col.')})

    def test_the_collection_is_a_known_word_in_the_page_title_of_the_card(self):
        self.assertRegex(self.page, r'data-i18n="col.title"')


if __name__ == '__main__':
    unittest.main()

"""F19: the result list as a working surface.

Views (fresh / stale / failed / unknown), page / selected / all-matching
scopes, bulk recheck, export, copy, tag, exclude, denylist, favourites, notes,
saved views, columns, matrix and undoable history — and the two rules that
matter most: a selection never silently moves to another scope, and a
favourite never bypasses freshness.
"""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import web_support as ws
from proxy_workbench import gui


class ResultListTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.rows = []
        for index in range(4):
            self.rows.append(ws.measurement(f'http://11.0.0.{20 + index}:8080', age=20 + index,
                                            now=self.now, country='NL' if index % 2 == 0 else 'DE'))
        self.expired = ws.measurement('http://11.0.0.40:8080', age=7200, valid_for=900, now=self.now)
        self.failed = ws.measurement('http://11.0.0.41:8080', age=40, error='UNREACHABLE', now=self.now)
        self.unknown = ws.measurement('http://11.0.0.42:8080', age=40, now=self.now)
        self.unknown.pop('checked_at')
        self.unknown.pop('valid_until')
        self.home = ws.build_data(self.rows + [self.expired, self.failed, self.unknown], now=self.now)
        self.fixture = ws.ServerFixture(self.home)
        self.proxy = self.rows[0]['proxy']
        self.other = self.rows[1]['proxy']

    def tearDown(self):
        self.fixture.close()

    def results(self, **params):
        params.setdefault('min_success', 0)
        response = self.fixture.client.get('/api/results', params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def bulk(self, body, expected=200):
        response = self.fixture.client.post('/api/results/bulk', json=body)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    # -- views ------------------------------------------------------------

    def test_each_view_shows_its_own_rows_and_reports_the_counts(self):
        counts = self.results(view='fresh')['counts']
        self.assertEqual(counts['fresh'], 4)
        self.assertEqual(counts['stale'], 1)
        self.assertEqual(counts['failed'], 1)
        self.assertEqual(counts['unknown'], 1)
        self.assertEqual(counts['all'], 7)
        self.assertEqual([row['proxy'] for row in self.results(view='stale')['rows']], [self.expired['proxy']])
        self.assertEqual([row['proxy'] for row in self.results(view='failed')['rows']], [self.failed['proxy']])
        self.assertEqual([row['proxy'] for row in self.results(view='unknown')['rows']], [self.unknown['proxy']])

    def test_rows_carry_their_freshness_and_admission_reason(self):
        row = {item['proxy']: item for item in self.results(view='all')['rows']}[self.expired['proxy']]
        self.assertEqual(row['freshness'], 'stale')
        self.assertEqual(row['admission_reason'], 'E_TIME_TTL_EXPIRED')
        fresh = {item['proxy']: item for item in self.results(view='fresh')['rows']}[self.proxy]
        self.assertEqual(fresh['freshness'], 'fresh')
        self.assertTrue(fresh['checked_at'] and fresh['valid_until'])

    def test_an_unknown_view_name_is_rejected_and_never_ignored(self):
        response = self.fixture.client.get('/api/results', params={'view': 'everything', 'min_success': 0})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Неверные параметры', response.json()['error'])

    # -- scopes -----------------------------------------------------------

    def test_page_scope_is_the_page_the_table_shows(self):
        first = self.results(view='fresh', limit=2, offset=0)
        self.assertEqual([row['proxy'] for row in first['rows']], [row['proxy'] for row in self.rows[:2]])
        copied = self.bulk({'op': 'copy', 'scope': 'page', 'query': {'view': 'fresh', 'limit': '2', 'offset': '0'}})
        self.assertEqual(copied['count'], 2)
        self.assertEqual(copied['text'].split(), [row['proxy'] for row in self.rows[:2]])

    def test_selected_scope_uses_the_addresses_the_user_marked(self):
        copied = self.bulk({'op': 'copy', 'scope': 'selected', 'proxies': [self.proxy, self.other]})
        self.assertEqual(copied['count'], 2)
        self.assertEqual(sorted(copied['text'].split()), sorted([self.proxy, self.other]))

    def test_all_matching_runs_on_the_server_without_loading_the_rows(self):
        bulk = self.bulk({'op': 'tag', 'scope': 'all_matching', 'tag': 'eu', 'query': {'view': 'fresh'}})
        self.assertEqual(bulk['count'], 4)
        self.assertNotIn('rows', bulk)
        entries = self.fixture.client.get('/api/annotations').json()['entries']
        self.assertEqual(sorted(entries), sorted(row['proxy'] for row in self.rows))

    def test_a_stale_scope_digest_refuses_the_action(self):
        refused = self.bulk({'op': 'tag', 'scope': 'all_matching', 'tag': 'eu',
                             'query': {'view': 'fresh'}, 'scope_digest': 'not-the-current-scope'}, expected=400)
        self.assertIn('Область изменилась', refused['error'])
        self.assertEqual(self.fixture.client.get('/api/annotations').json()['entries'], {})

    def test_the_scope_digest_changes_with_the_filters_and_the_generation(self):
        first = self.results(view='fresh')['scope_digest']
        second = self.results(view='stale')['scope_digest']
        self.assertNotEqual(first, second)
        self.assertEqual(first, self.results(view='fresh')['scope_digest'])

    # -- tags, favourites, notes, exclude, denylist ----------------------

    def test_tags_notes_and_favourites_survive_a_reload(self):
        self.bulk({'op': 'tag', 'scope': 'selected', 'proxies': [self.proxy], 'tag': 'fast'})
        self.bulk({'op': 'note', 'scope': 'selected', 'proxies': [self.proxy], 'note': 'from the office'})
        self.bulk({'op': 'favorite', 'scope': 'selected', 'proxies': [self.proxy]})
        row = {item['proxy']: item for item in self.results(view='fresh')['rows']}[self.proxy]
        self.assertEqual(row['tags'], ['fast'])
        self.assertEqual(row['note'], 'from the office')
        self.assertTrue(row['favorite'])
        detail = self.fixture.client.get('/api/result-detail', params={'proxy': self.proxy}).json()
        self.assertEqual(detail['tags'], ['fast'])
        self.assertEqual(detail['note'], 'from the office')

    def test_a_favourite_does_not_bypass_freshness(self):
        self.bulk({'op': 'favorite', 'scope': 'selected', 'proxies': [self.expired['proxy']]})
        fresh = {row['proxy'] for row in self.results(view='fresh')['rows']}
        self.assertNotIn(self.expired['proxy'], fresh)
        row = {item['proxy']: item for item in self.results(view='all')['rows']}[self.expired['proxy']]
        self.assertTrue(row['favorite'])
        self.assertEqual(row['freshness'], 'stale')
        # and the default table still hides it
        self.assertNotIn(self.expired['proxy'], {row['proxy'] for row in self.results()['rows']})

    def test_exclude_hides_a_row_from_my_list_and_include_brings_it_back(self):
        self.bulk({'op': 'exclude', 'scope': 'selected', 'proxies': [self.proxy]})
        entries = self.fixture.client.get('/api/annotations').json()['entries']
        self.assertTrue(entries[self.proxy].get('excluded'))
        self.bulk({'op': 'include', 'scope': 'selected', 'proxies': [self.proxy]})
        entries = self.fixture.client.get('/api/annotations').json()['entries']
        self.assertFalse(entries[self.proxy].get('excluded'))

    def test_denylist_writes_the_local_list_and_undo_puts_it_back(self):
        answer = self.bulk({'op': 'denylist', 'scope': 'selected', 'proxies': [self.proxy]})
        self.assertEqual(answer['count'], 1)
        self.assertIn(self.proxy, (self.home / 'denylist.txt').read_text(encoding='utf-8'))
        # A later action must not become the target of an explicit undo.
        self.bulk({'op': 'favorite', 'scope': 'selected', 'proxies': [self.other]})
        undone = self.fixture.client.post('/api/history/undo', json={'id': answer['history']['id']})
        self.assertEqual(undone.status_code, 200, undone.text)
        self.assertNotIn(self.proxy, (self.home / 'denylist.txt').read_text(encoding='utf-8'))

    def test_a_denylisted_row_leaves_the_table(self):
        self.bulk({'op': 'denylist', 'scope': 'selected', 'proxies': [self.proxy]})
        self.assertNotIn(self.proxy, {row['proxy'] for row in self.results(view='fresh')['rows']})

    # -- history ----------------------------------------------------------

    def test_history_records_recoverable_actions_and_undo_reverses_them(self):
        self.bulk({'op': 'tag', 'scope': 'selected', 'proxies': [self.proxy], 'tag': 'temp'})
        entries = self.fixture.client.get('/api/history').json()['entries']
        self.assertTrue(entries)
        self.assertTrue(entries[0]['recoverable'])
        self.assertNotIn('undo', entries[0])
        undone = self.fixture.client.post('/api/history/undo', json={'id': entries[0]['id']})
        self.assertEqual(undone.status_code, 200, undone.text)
        self.assertEqual(self.fixture.client.get('/api/annotations').json()['entries'], {})

    def test_a_second_undo_of_the_same_action_is_refused(self):
        self.bulk({'op': 'tag', 'scope': 'selected', 'proxies': [self.proxy], 'tag': 'temp'})
        first = self.fixture.client.post('/api/history/undo', json={})
        self.assertEqual(first.status_code, 200)
        again = self.fixture.client.post('/api/history/undo', json={'id': first.json()['undone']})
        self.assertEqual(again.status_code, 400)

    def test_an_unknown_operation_is_refused(self):
        answer = self.bulk({'op': 'launch', 'scope': 'selected', 'proxies': [self.proxy]}, expected=400)
        self.assertIn('Неизвестная массовая операция', answer['error'])

    # -- saved views, columns, matrix -------------------------------------

    def test_saved_views_store_the_query_and_come_back(self):
        view = self.fixture.client.post('/api/views', json={'name': 'NL fresh', 'query': {'view': 'fresh', 'country': 'NL'},
                                                             'columns': ['proxy', 'latency']}).json()
        self.assertEqual(view['name'], 'NL fresh')
        self.assertEqual(view['columns'], ['proxy', 'latency'])
        views = self.fixture.client.get('/api/views').json()['views']
        self.assertEqual([item['id'] for item in views], [view['id']])
        removed = self.fixture.client.post('/api/views/delete', json={'id': view['id']})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.fixture.client.get('/api/views').json()['views'], [])

    def test_a_saved_view_with_a_broken_query_is_refused(self):
        answer = self.fixture.client.post('/api/views', json={'name': 'bad', 'query': {'view': 'everything'}})
        self.assertEqual(answer.status_code, 400)

    def test_the_default_columns_are_the_compact_set(self):
        payload = self.results(view='fresh')
        self.assertEqual(payload['columns'], list(gui.COMPACT_COLUMNS))
        self.assertNotIn('jitter', payload['columns'])
        self.assertIn('jitter', gui.ALL_COLUMNS)

    def test_the_matrix_answers_with_a_cell_per_target(self):
        matrix = self.fixture.client.get('/api/results/matrix', params={'view': 'fresh', 'min_success': 0}).json()
        self.assertEqual([target['name'] for target in matrix['targets']],
                         ['Example service', 'Second service'])
        row = matrix['rows'][0]
        self.assertEqual(sorted(cell['target'] for cell in row['cells']), [0, 1])
        self.assertTrue(all(cell['ok'] for cell in row['cells']))

    def test_page_size_is_bounded(self):
        self.assertEqual(self.results(view='fresh', limit=9999)['limit'], gui.PAGE_SIZE_MAX)
        self.assertEqual(self.results(view='fresh', limit=0)['limit'], 1)

    # -- recheck ----------------------------------------------------------

    def test_bulk_recheck_starts_a_job_for_the_whole_scope(self):
        with mock.patch.object(type(self.fixture.app), 'start', autospec=True) as start:
            start.return_value = {'id': 'job1', 'action': 'recheck'}
            answer = self.bulk({'op': 'recheck', 'scope': 'all_matching', 'query': {'view': 'fresh'}})
        self.assertEqual(answer['count'], 4)
        self.assertEqual(answer['job']['action'], 'recheck')
        payload = start.call_args.args[1]
        self.assertEqual(payload['action'], 'recheck')
        self.assertEqual(len(payload['selection']), 4)
        self.assertGreater(gui.MAX_BULK_SELECTION, gui.MAX_SELECTION)


if __name__ == '__main__':
    unittest.main()

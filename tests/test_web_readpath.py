"""Defect 5 and defect 3 on the read-only path of the interface.

Defect 5: the table, the row details and the download must answer while a scan
or a watch holds the exclusive data lock.
Defect 3: one expired member of a mixed-age set must not hide the fresh ones,
and "nothing matched" must not be reported as "expired".
"""
import json
import sys
import threading
import time
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import web_support as ws
from proxy_workbench.maintenance import exclusive_lock


class ReadPathTests(unittest.TestCase):
    def setUp(self):
        now = time.time()
        self.fresh = ws.measurement('http://11.0.0.10:8080', age=30, now=now, country='NL')
        self.expired = ws.measurement('http://11.0.0.11:8080', age=7200, valid_for=900, now=now, country='DE')
        self.failed = ws.measurement('http://11.0.0.12:8080', age=60, error='UNREACHABLE', now=now)
        self.home = ws.build_data([self.fresh, self.expired, self.failed], now=now)
        self.fixture = ws.ServerFixture(self.home)

    def tearDown(self):
        self.fixture.close()

    def get(self, path, **params):
        response = self.fixture.client.get(path, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # -- defect 5 ---------------------------------------------------------

    def test_table_details_and_download_answer_while_the_worker_holds_the_lock(self):
        with exclusive_lock(self.home / 'workbench.lock'):
            rows = self.get('/api/results', min_success=0, view='all')
            self.assertEqual([row['proxy'] for row in rows['rows']],
                             [self.fresh['proxy'], self.expired['proxy'], self.failed['proxy']])
            detail = self.get('/api/result-detail', proxy=self.fresh['proxy'])
            self.assertEqual(detail['proxy'], self.fresh['proxy'])
            download = self.fixture.client.get('/api/download/proxies.txt')
            self.assertEqual(download.status_code, 200)
            self.assertIn(self.fresh['proxy'], download.text)
            self.assertEqual(self.get('/api/results/matrix')['profile'], self.fixture.app.job.get('profile') or
                             self.get('/api/results/matrix')['profile'])
            events = self.get('/api/events')
            self.assertTrue(any(event['item_id'] == self.fresh['proxy'] for event in events['events']))

    def test_reads_answer_while_another_holder_keeps_the_writer_lock(self):
        holding = threading.Event()
        released = threading.Event()

        def holder():
            with exclusive_lock(self.home / 'workbench.lock'):
                holding.set()
                released.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(holding.wait(5), 'the fixture never took the writer lock')
        try:
            started = time.monotonic()
            rows = self.get('/api/results', min_success=0, view='all')
            elapsed = time.monotonic() - started
            self.assertEqual(len(rows['rows']), 3)
            self.assertLess(elapsed, 0.5, 'the read path waited for the writer lock')
            # A second exclusive holder is refused, which proves the lock really
            # was taken and the read did not simply run after it was released.
            with self.assertRaises(RuntimeError):
                with exclusive_lock(self.home / 'workbench.lock'):
                    pass
        finally:
            released.set()
            thread.join()

    def test_detail_reports_age_instead_of_claiming_the_row_is_missing(self):
        detail = self.get('/api/result-detail', proxy=self.expired['proxy'])
        self.assertEqual(detail['freshness'], 'stale')
        self.assertEqual(detail['admission_reason'], 'E_TIME_TTL_EXPIRED')
        self.assertIsNotNone(detail['age_seconds'])

    # -- defect 3 ---------------------------------------------------------

    def test_an_expired_member_does_not_hide_the_fresh_ones(self):
        status = self.get('/api/state')['export']
        self.assertEqual(status['state'], 'partial')
        self.assertFalse(status['stale'])
        self.assertEqual(status['expired_count'], 1)
        fresh_view = self.get('/api/results', min_success=0, view='fresh')
        self.assertEqual([row['proxy'] for row in fresh_view['rows']], [self.fresh['proxy']])
        stale_view = self.get('/api/results', min_success=0, view='stale')
        self.assertEqual([row['proxy'] for row in stale_view['rows']], [self.expired['proxy']])

    def test_nothing_matched_is_not_reported_as_expired(self):
        # A filter that matches no row is an empty result, not an expired
        # snapshot: the published set itself stays fresh and non-stale.
        status = self.get('/api/state')['export']
        self.assertFalse(status['stale'])
        self.assertEqual(status['available'], 1)
        filtered = self.fixture.client.get('/api/results', params={'q': 'nothing-matches-this'}).json()
        self.assertEqual(filtered['rows'], [])
        self.assertEqual(filtered['total'], 0)
        after = self.get('/api/state')['export']
        self.assertEqual(after['stale'], False)
        self.assertEqual(after['available'], 1)
        self.assertNotEqual(after['state_detail'], 'all_expired')

    def test_an_empty_publication_is_empty_and_not_expired(self):
        now = time.time()
        home = ws.build_data([ws.measurement('http://11.0.1.6:8080', age=5, now=now)], now=now)
        ws.publish_generation(home, [], ws.profile_id(), now=now, name='.generation-empty01', exported=0,
                              extra={'state': 'complete'})
        fixture = ws.ServerFixture(home)
        try:
            status = fixture.client.get('/api/state').json()['export']
            self.assertEqual(status['available'], 0)
            self.assertTrue(status['empty_export'])
            self.assertFalse(status['stale'])
            # "Nothing was published" and "everything expired" are different
            # situations and must not share a code.
            self.assertEqual(status['state'], 'empty')
            self.assertIn(status['state_detail'], ('empty_no_match', 'nothing_in_scope'))
            self.assertNotEqual(status['state_detail'], 'all_expired')
        finally:
            fixture.close()

    def test_a_broken_pointer_is_reported_broken_and_never_falls_back_to_rows(self):
        (self.home / 'exports' / 'current.json').write_text('{not json', encoding='utf-8')
        state = self.fixture.client.get('/api/state').json()
        self.assertEqual(state['export']['state'], 'error')
        self.assertEqual(state['export']['reader_state'], 'broken')
        rows = self.get('/api/results', min_success=0, view='all')
        self.assertEqual(rows['rows'], [])
        self.assertEqual(rows['snapshot_state'], 'broken')
        download = self.fixture.client.get('/api/download/proxies.txt')
        self.assertEqual(download.status_code, 409)

    def test_the_reason_travels_to_the_page_in_words_and_as_a_code(self):
        """CONTRACTS §5.4: ``state_detail`` is translated, the code stays."""
        from proxy_workbench import i18n
        answer = self.get('/api/results', min_success=0, view='all')
        self.assertIn(answer['state_detail'], i18n.STATE_DETAILS)
        self.assertEqual(answer['state_detail_label'], i18n.state_detail_text(answer['state_detail']))

        # A publication where everything expired, and one where nothing matched,
        # must be readable as two different situations.
        now = time.time()
        # One generation where every row left the set through its deadline, and
        # one where the row is inside the deadline but fails the policy.  Those
        # are the two situations a user must be able to tell apart.
        expired_row = ws.measurement('http://11.2.0.1:8080', age=7200, valid_for=900, now=now)
        weak_row = ws.measurement('http://11.2.0.2:8080', age=5, reliability=0.1, now=now)
        cases = (
            ('.generation-allgone', [expired_row], 'all_expired'),
            ('.generation-nomatch', [weak_row], 'empty_no_match'),
        )
        labels = {}
        for name, rows, expected in cases:
            home = ws.build_data(rows, now=now)
            ws.publish_generation(home, rows, ws.profile_id(), now=now, name=name,
                                  extra={'state': 'complete', 'min_success': 0.5})
            fixture = ws.ServerFixture(home)
            try:
                answer = fixture.client.get('/api/results', params={'view': 'all', 'min_success': 0.5}).json()
                self.assertEqual(answer['state_detail'], expected, answer['state_detail'])
                self.assertNotEqual(answer['state_detail_label'], expected)
                self.assertTrue(answer['state_detail_label'].strip())
                labels[expected] = answer['state_detail_label']
            finally:
                fixture.close()
        self.assertNotEqual(labels['all_expired'], labels['empty_no_match'],
                            'expired and empty must not read the same to a user')


if __name__ == '__main__':
    unittest.main()

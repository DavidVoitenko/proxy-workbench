"""Defect 25: the live feed is a stream of real measurement events.

The previous implementation scanned the aggregated scan log for OK/PASS/FAIL
and presented those lines as a per-proxy stream.  Here the source is the
measurement store: one event per finished observation, with the address, the
measured latency and a machine code, and a cursor that only moves forward.
"""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import web_support as ws


class EventStreamTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.good = ws.measurement('http://11.0.0.10:8080', latency=140.0, age=20, now=self.now)
        self.dead = ws.measurement('http://11.0.0.11:8080', age=30, error='UNREACHABLE', now=self.now)
        self.home = ws.build_data([self.good, self.dead], now=self.now)
        self.fixture = ws.ServerFixture(self.home)
        self.app = self.fixture.app

    def tearDown(self):
        self.fixture.close()

    def events(self, **params):
        response = self.fixture.client.get('/api/events', params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_one_event_per_finished_measurement(self):
        payload = self.events()
        self.assertEqual(payload['source'], 'measurements')
        by_item = {event['item_id']: event for event in payload['events']}
        self.assertEqual(sorted(by_item), sorted([self.good['proxy'], self.dead['proxy']]))
        self.assertEqual(by_item[self.good['proxy']]['type'], 'item.observation')
        self.assertEqual(by_item[self.good['proxy']]['code'], 'OK')
        self.assertEqual(by_item[self.good['proxy']]['data']['latency_ms'], 140.0)
        self.assertEqual(by_item[self.good['proxy']]['data']['reliability'], 1.0)
        self.assertEqual(by_item[self.good['proxy']]['data']['proxy'], self.good['proxy'])
        self.assertEqual(by_item[self.dead['proxy']]['code'], 'UNREACHABLE')
        self.assertEqual(by_item[self.dead['proxy']]['data']['error'], 'UNREACHABLE')

    def test_the_stream_reports_the_proxy_not_a_log_line(self):
        for event in self.events()['events']:
            if event['type'] == 'item.observation':
                self.assertTrue(event['item_id'].startswith('http://'))
                self.assertIn('proxy', event['data'])
                self.assertNotIn('OK', str(event['data'].get('error') or ''))

    def test_the_cursor_only_moves_forward(self):
        first = self.events()
        cursor = first['cursor']
        self.assertTrue(cursor)
        second = self.events(after=cursor)
        self.assertEqual(second['events'], [])
        replay = self.events(after='0')
        self.assertEqual([event['seq'] for event in replay['events']],
                         sorted(event['seq'] for event in replay['events']))
        self.assertEqual(len(replay['events']), len(first['events']))

    def test_a_new_measurement_appears_once_and_only_once(self):
        before = {event['item_id'] for event in self.events()['events']}
        self.assertIn(self.good['proxy'], before)
        later = ws.measurement('http://11.0.0.12:8080', latency=90.0, age=5, now=time.time())
        from proxy_workbench import proxytool as core
        conn = core.open_db(self.home / 'proxies.sqlite3')
        with conn:
            conn.execute('INSERT OR REPLACE INTO results (profile, proxy, payload) VALUES (?,?,?)',
                         (ws.profile_id(), later['proxy'], json.dumps(later)))
        conn.close()
        self.app.event_backfilled = False
        after = self.events()
        items = [event['item_id'] for event in after['events']]
        self.assertIn(later['proxy'], items)
        self.assertEqual(len(items), len(set(items)))
        again = self.events(after=after['cursor'])
        self.assertEqual(again['events'], [], 'the same measurement was reported twice')

    def test_the_events_carry_no_secret_and_no_credentials(self):
        raw = self.fixture.client.get('/api/events').text
        self.assertNotIn(self.app.token, raw)
        self.assertNotIn(self.app.gateway_token, raw)
        for line in (self.home / 'gui-events.jsonl').read_text(encoding='utf-8').splitlines():
            event = json.loads(line)
            self.assertNotIn('password', json.dumps(event).lower())
            self.assertNotIn('token', json.dumps(event).lower())

    def test_a_row_without_a_measurement_time_gets_no_invented_event(self):
        from proxy_workbench import proxytool as core
        row = ws.measurement('http://11.0.0.13:8080', age=10, now=self.now)
        row.pop('checked_at')
        conn = core.open_db(self.home / 'proxies.sqlite3')
        with conn:
            conn.execute('INSERT OR REPLACE INTO results (profile, proxy, payload) VALUES (?,?,?)',
                         (ws.profile_id(), row['proxy'], json.dumps(row)))
        conn.close()
        items = [event['item_id'] for event in self.events()['events']]
        self.assertNotIn(row['proxy'], items)

    def test_the_feed_survives_a_data_lock_held_by_a_scan(self):
        from proxy_workbench.maintenance import exclusive_lock
        with exclusive_lock(self.home / 'workbench.lock'):
            payload = self.events()
        self.assertEqual(len(payload['events']), 2)


if __name__ == '__main__':
    unittest.main()

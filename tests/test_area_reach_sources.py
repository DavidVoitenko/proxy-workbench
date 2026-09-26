"""F21 is reachable: `sourcedesk.compare_*` has a CLI, a `/v1` route and a page.

The defect this file closes is not "the function is wrong" -- ``sourcedesk``
computes overlap, unique contribution, cost and bias correctly, and its own
tests prove that.  The defect is that nothing in the tree *called* it: no
subcommand, no route, no page.  A module with green tests and no door is a
function the user does not have.

So every test here goes the way a person goes: through the argument parser,
through a live HTTP request, and through the page the GUI serves.  The
headline is the research fact from
``docs/requirements/sources-research/overlap.md``: two publishers hand out the
same 21 036 addresses, the second's unique contribution is zero, and the report
says so on every surface.

Nothing here contacts a proxy.  Every address is from a documentation range
(RFC 5737) and every "measurement" is a row written into a temporary database.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from proxy_workbench import apiv1
from proxy_workbench import db
from proxy_workbench import gui
from proxy_workbench import proxytool
from proxy_workbench import sourcedesk as sd

T0 = 1_757_000_000.0
HOUR = 3600.0

#: Two publishers with byte-identical payloads under different ids -- the shape
#: of ``hookzof/socks5_list`` and ``proxifly/free-proxy-list`` in the research.
SHARED = [f'198.51.100.{n}' for n in range(1, 21)]
#: A third publisher that adds addresses of its own.
EXCLUSIVE = [f'203.0.113.{n}' for n in range(1, 9)]


def _verdict(reliability, *, ms=90.0, byte_count=1024, requests=2):
    return json.dumps({'proxy': 'redacted', 'reliability': reliability,
                       'min_target_reliability': reliability, 'latency_ms': ms,
                       'successes': int(round(reliability * requests)), 'requests': requests,
                       'samples': [{'ok': True, 'bytes': byte_count, 'ms': ms}
                                   for _ in range(requests)]})


def build_database(directory: Path) -> Path:
    """A migrated database with the copy scenario already measured."""
    conn, _report = db.open_db(str(directory / db.DB_FILENAME))
    for canonical in SHARED + EXCLUSIVE:
        conn.execute('INSERT OR IGNORE INTO endpoints(id, canonical, country, country_source, '
                     'asn, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)',
                     (db.endpoint_id(canonical), canonical,
                      'NL' if canonical in SHARED else 'DE', 'source', 64500, T0, T0 + HOUR))
    conn.execute('INSERT OR IGNORE INTO accesses(id, endpoint_id, mode, access_revision, created_at) '
                 'VALUES (?,?,?,?,?)',
                 ('acc-1', db.endpoint_id(SHARED[0]), 'none', 1, T0))
    for proxy in SHARED:
        for source in ('pub-a', 'pub-b'):
            conn.execute('INSERT OR IGNORE INTO candidate_seen(proxy, source, endpoint_id) VALUES (?,?,?)',
                         (proxy, source, db.endpoint_id(proxy)))
    for proxy in EXCLUSIVE:
        conn.execute('INSERT OR IGNORE INTO candidate_seen(proxy, source, endpoint_id) VALUES (?,?,?)',
                     (proxy, 'pub-c', db.endpoint_id(proxy)))
    conn.execute("INSERT INTO job(id, kind, state, scope_json, profile_id, profile_revision, "
                 "collection_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 ('job-1', 'check', 'want_reached',
                  json.dumps({'collection_id': 'public', 'profile_id': 'p1', 'profile_revision': 1,
                              'filters': {'countries': ['NL'], 'want': 12, 'count_what': 'endpoint'}}),
                  'p1', 1, 'public', T0))

    def observe(canonical, payload, error=None, stage=None):
        conn.execute('INSERT INTO observations(id, job_id, endpoint_id, access_id, access_revision, '
                     'profile_id, profile_revision, started_at, finished_at, verdict, error_code, '
                     'error_stage) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                     (f'obs-{db.endpoint_id(canonical)}', 'job-1', db.endpoint_id(canonical),
                      'acc-1', 1, 'p1', 1, T0 + 60, T0 + 61.0, payload, error, stage))

    for proxy in SHARED:
        observe(proxy, _verdict(1.0) if proxy in SHARED[:12] else '',
                None if proxy in SHARED[:12] else 'UNREACHABLE',
                None if proxy in SHARED[:12] else 'tcp')
    for proxy in EXCLUSIVE:
        if proxy in EXCLUSIVE[:3]:
            observe(proxy, _verdict(1.0))
        elif proxy in EXCLUSIVE[5:6]:
            # A target that could not be reached is not a verdict about the
            # address, and must not become one in a comparison either.
            observe(proxy, _verdict(0.0), 'TARGET_UNAVAILABLE', 'target')
        else:
            observe(proxy, '', 'CONNECT_TIMEOUT', 'tcp')
    conn.commit()
    conn.close()
    return directory


class CliReachesTheComparison(unittest.TestCase):
    """`proxy-workbench source compare` -- the command a person types."""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.data = build_database(Path(cls._directory.name))

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def cli(self, *args, lang='ru'):
        # The language is pinned per call: the assertions below are about what
        # the command reports, not about whichever locale another test in the
        # same process happened to select.
        environment = dict(os.environ, PROXY_WORKBENCH_LANG=lang)
        return subprocess.run(
            [sys.executable, '-m', 'proxy_workbench', '--data', str(self.data), 'source', 'compare', *args],
            capture_output=True, text=True, timeout=180, cwd=str(REPO), env=environment)

    def test_the_subcommand_exists_and_is_advertised(self):
        self.assertIn('compare', proxytool.SOURCE_SUBCOMMANDS)
        listed = subprocess.run(
            [sys.executable, '-m', 'proxy_workbench', 'source', 'nonsense'],
            capture_output=True, text=True, timeout=180, cwd=str(REPO),
            env=dict(os.environ, PROXY_WORKBENCH_LANG='ru'))
        self.assertIn('compare', listed.stderr)

    def test_the_copy_is_visible_from_the_command_line(self):
        done = self.cli('pub-a', 'pub-b', 'pub-c')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('ОДИНАКОВЫЕ НАБОРЫ', done.stdout)
        self.assertIn('jaccard 100.0%', done.stdout)
        # The unique contribution of the second publisher is zero, and the text
        # says so next to the pass rate rather than leaving it to be inferred.
        for line in done.stdout.splitlines():
            if line.startswith('pub-b'):
                self.assertRegex(line, r'своих\s+0')

    def test_json_carries_overlap_families_cost_and_bias(self):
        done = self.cli('pub-a', 'pub-b', 'pub-c', '--json')
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        pair = next(p for p in report['overlaps'] if {p['left'], p['right']} == {'pub-a', 'pub-b'})
        self.assertTrue(pair['identical'])
        self.assertEqual(pair['shared'], len(SHARED))
        self.assertEqual(pair['jaccard'], 1.0)
        self.assertEqual({r['source_id']: r['unique_offered'] for r in report['rows']}['pub-b'], 0)
        family = next(f for f in report['families'] if f['family_id'] == 'pub-a')
        self.assertEqual(family['members'], ['pub-a', 'pub-b'])
        self.assertTrue(family['identical_group'])
        codes = {bias['code'] for bias in report['biases']}
        # find-N, the country filter and the geography difference are all named.
        self.assertIn('find_n', codes)
        self.assertIn('filters', codes)
        self.assertIn('geography', codes)
        self.assertIn('shared_cost', codes)
        # Cost per admitted address, with the fields a reader needs.
        self.assertEqual(len(report['cost']), len(report['rows']))
        self.assertTrue(all('seconds' in item and 'bytes' in item and 'attempts' in item
                            for item in report['cost']))

    def test_unknown_measurements_are_not_a_zero_reliability(self):
        done = self.cli('pub-c', '--json')
        self.assertEqual(done.returncode, 0, done.stderr)
        row = json.loads(done.stdout)['rows'][0]
        self.assertEqual(row['unknown'], 1)
        # 3 passed of 7 decided, not of 8: the target that could not be
        # reached stays out of the denominator instead of becoming a failure.
        self.assertAlmostEqual(row['reliability'], 3 / 7)
        self.assertIn('unknown_measurements', {b['code'] for b in json.loads(done.stdout)['biases']})

    def test_permuting_the_sources_changes_nothing(self):
        first = json.loads(self.cli('pub-a', 'pub-b', 'pub-c', '--json').stdout)
        second = json.loads(self.cli('pub-c', 'pub-a', 'pub-b', '--json').stdout)
        self.assertEqual(first['rows'], second['rows'])
        self.assertEqual(first['overlaps'], second['overlaps'])
        self.assertEqual(first['families'], second['families'])
        self.assertEqual(first['cost'], second['cost'])
        self.assertEqual(first['biases'], second['biases'])

    def test_two_suppliers_on_identical_terms(self):
        done = self.cli('--suppliers', 'pub-a,pub-b')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('одинаковые условия: да', done.stdout)
        self.assertIn('уникальный вклад второго равен нулю', done.stdout)
        supplier = json.loads(self.cli('--suppliers', 'pub-a,pub-b', '--json').stdout)
        self.assertTrue(supplier['equal_terms'])
        self.assertTrue(supplier['overlap']['identical'])

    def test_two_suppliers_must_be_two(self):
        done = self.cli('--suppliers', 'pub-a')
        self.assertEqual(done.returncode, 2)
        self.assertIn('E_VALIDATION_FIELD', done.stderr)

    def test_several_windows_and_why_they_are_not_one_number(self):
        done = self.cli('--name', 'pub-a,pub-c', '--json', '--cohorts',
                        f'{int(T0) - 1000}:{int(T0)},{int(T0)}:{int(T0) + 1000}')
        self.assertEqual(done.returncode, 0, done.stderr)
        body = json.loads(done.stdout)
        self.assertEqual(len(body['cohorts']), 2)
        self.assertTrue(body['warnings'])
        # The window with no measurement says so; it is not a pass rate of zero.
        self.assertEqual([r['status'] for r in body['cohorts'][0]['rows']],
                         ['no_data', 'no_data'])
        self.assertIsNone(body['cohorts'][0]['rows'][0]['reliability'])

    def test_one_window_is_refused_because_there_is_nothing_to_compare(self):
        done = self.cli('--name', 'pub-a', '--cohorts', '1:2')
        self.assertEqual(done.returncode, 2)
        self.assertIn('E_VALIDATION_FIELD', done.stderr)

    def test_survival_keeps_censoring_visible(self):
        report = json.loads(self.cli('pub-a', '--json', '--survival', '--survival-windows', '3').stdout)
        steps = report['survival']
        self.assertEqual(len(steps), 3)
        for step in steps:
            self.assertIn('censored', step)
            # An address that was simply not looked at is not a dead one.
            self.assertEqual(step['dead'] + step['alive'], step['entered'] - step['censored'])

    def test_no_sources_is_refused_rather_than_compared_against_nothing(self):
        done = self.cli()
        self.assertEqual(done.returncode, 2)
        self.assertIn('E_VALIDATION_FIELD', done.stderr)

    def test_a_database_without_measurements_says_so(self):
        with tempfile.TemporaryDirectory() as empty:
            done = subprocess.run(
                [sys.executable, '-m', 'proxy_workbench', '--data', empty, 'source', 'compare', 'x', 'y'],
                capture_output=True, text=True, timeout=180, cwd=str(REPO),
                env=dict(os.environ, PROXY_WORKBENCH_LANG='ru'))
            self.assertEqual(done.returncode, 2)
            self.assertIn('E_STATE_NO_SNAPSHOT', done.stderr)


class RouteReachesTheComparison(unittest.TestCase):
    """`POST /v1/sources/compare*` -- the route a client calls."""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.data = build_database(Path(cls._directory.name))

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def setUp(self):
        self._keys = _OperatorKeys()
        self._service = _Service(self.data)
        self.client = apiv1.ApiV1(self._service, self._keys, apiv1.ApiConfig(host='127.0.0.1'))

    def call(self, path, body):
        return self.client.handle(apiv1.Request(
            method='POST', path=path, headers={'Authorization': f'Bearer {KEY}',
                                              'Content-Type': 'application/json',
                                              'Idempotency-Key': 'k', 'Host': '127.0.0.1:8766'},
            body=json.dumps(body).encode(), client_host='127.0.0.1'))

    def test_the_three_routes_exist_in_the_route_table(self):
        paths = apiv1.openapi_document()['paths']
        for path in ('/v1/sources/compare', '/v1/sources/compare/suppliers',
                     '/v1/sources/compare/cohorts'):
            self.assertIn(path, paths)
        for path in ('/v1/sources/compare', '/v1/sources/compare/suppliers',
                     '/v1/sources/compare/cohorts'):
            self.assertIn('post', paths[path], path)

    def test_the_route_answers_200_with_the_copy_reported(self):
        response = self.call('/v1/sources/compare', {'sources': ['pub-a', 'pub-b', 'pub-c']})
        self.assertEqual(response.status, 200)
        body = json.loads(response.body)
        pair = next(p for p in body['overlaps'] if {p['left'], p['right']} == {'pub-a', 'pub-b'})
        self.assertTrue(pair['identical'])
        self.assertEqual(pair['shared'], len(SHARED))
        self.assertEqual({r['source_id']: r['unique_offered'] for r in body['rows']}['pub-b'], 0)

    def test_the_route_reports_the_cohort_it_used(self):
        body = json.loads(self.call('/v1/sources/compare', {'sources': ['pub-a']}).body)
        self.assertEqual(body['cohort']['profile_revision'], 1)
        self.assertGreater(body['cohort']['end'], body['cohort']['start'])

    def test_the_route_accepts_an_explicit_window(self):
        body = json.loads(self.call('/v1/sources/compare',
                                    {'sources': ['pub-a'], 'start': T0, 'end': T0 + HOUR}).body)
        self.assertEqual((body['cohort']['start'], body['cohort']['end']), (T0, T0 + HOUR))

    def test_suppliers_route_reports_equal_terms_and_the_copy(self):
        body = json.loads(self.call('/v1/sources/compare/suppliers',
                                    {'sources': ['pub-a', 'pub-b'],
                                     'left': 'pub-a', 'right': 'pub-b'}).body)
        self.assertTrue(body['equal_terms'])
        self.assertTrue(body['overlap']['identical'])
        self.assertTrue(any('уникальный вклад второго равен нулю' in w for w in body['warnings']))

    def test_cohorts_route_returns_both_breakdowns_and_the_warning(self):
        body = json.loads(self.call('/v1/sources/compare/cohorts',
                                    {'sources': ['pub-a', 'pub-c'],
                                     'cohorts': [{'start': int(T0) - 1000, 'end': int(T0)},
                                                 {'start': int(T0), 'end': int(T0) + 1000}]}).body)
        self.assertEqual(len(body['cohorts']), 2)
        self.assertTrue(any('окна не пересекаются' in w for w in body['warnings']))

    def test_survival_on_the_route_keeps_censoring(self):
        body = json.loads(self.call('/v1/sources/compare',
                                    {'sources': ['pub-a'], 'survival': True,
                                     'survival_windows': 3}).body)
        self.assertEqual(len(body['survival']), 3)
        self.assertIn('censored', body['survival'][0])

    def test_an_empty_source_list_is_refused(self):
        response = self.call('/v1/sources/compare', {'sources': []})
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)['error']['code'], 'E_VALIDATION_FIELD')

    def test_an_unknown_parameter_is_refused_and_never_ignored(self):
        response = self.call('/v1/sources/compare', {'sources': ['pub-a'], 'jaccard': 0.5})
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)['error']['code'], 'E_VALIDATION_UNKNOWN_FIELD')

    def test_a_database_without_measurements_is_refused_not_answered_empty(self):
        with tempfile.TemporaryDirectory() as empty:
            service = _Service(Path(empty))
            client = apiv1.ApiV1(service, _OperatorKeys(), apiv1.ApiConfig(host='127.0.0.1'))
            response = client.handle(apiv1.Request(
                method='POST', path='/v1/sources/compare',
                headers={'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json',
                         'Idempotency-Key': 'k'},
                body=json.dumps({'sources': ['x']}).encode(), client_host='127.0.0.1'))
            self.assertGreaterEqual(response.status, 400)


class PageReachesTheComparison(unittest.TestCase):
    """The sources page of the interface carries the section and the endpoint."""

    def setUp(self):
        self.page = (REPO / 'proxy_workbench' / 'ui' / 'index.html').read_text(encoding='utf-8')
        self.served = gui.append_source_compare_script(gui.inject_source_compare(self.page))

    def test_the_sources_page_gains_the_section(self):
        self.assertNotIn('id="source-compare-card"', self.page)
        served = self.served
        self.assertIn('id="source-compare-card"', served)
        # It belongs to the sources page, not somewhere else on the screen.
        self.assertLess(served.index('id="page-sources"'), served.index('id="source-compare-card"'))
        self.assertLess(served.index('id="source-compare-card"'), served.index('id="page-pools"'))

    def test_the_section_uses_the_designs_own_classes(self):
        start = self.served.rfind('<section', 0, self.served.index('id="source-compare-card"'))
        section = self.served[start:][:4000]
        for token in ('class="card"', 'class="card-title"', 'class="badge subtle"',
                      'class="table-wrap"', 'class="field-grid"', 'class="button primary chip"'):
            self.assertIn(token, section, token)

    def test_the_section_carries_the_controls_the_comparison_needs(self):
        for element in ('source-compare-input', 'source-compare-min', 'source-compare-windows',
                        'source-compare-run', 'source-compare-rows', 'source-compare-status',
                        'source-compare-detail'):
            self.assertIn(f'id="{element}"', self.served, element)

    def test_the_driver_calls_the_endpoint_with_the_page_token(self):
        self.assertIn('/api/sources/compare', self.served)
        # The token is read from the page's own meta tag, which the server
        # already substitutes; the section must not carry a second copy of it
        # and must not invent one.
        self.assertIn('meta[name="workbench-token"]', self.served)
        self.assertEqual(self.served.count(gui.SOURCE_COMPARE_MARKER), 1)
        script = self.served[self.served.index(gui.SOURCE_COMPARE_MARKER):]
        self.assertNotIn('__TOKEN__', script)

    def test_injection_is_idempotent_and_survives_a_reshuffled_page(self):
        again = gui.append_source_compare_script(gui.inject_source_compare(self.served))
        self.assertEqual(again.count('id="source-compare-card"'), 1)
        self.assertEqual(again.count('id="source-compare-run"'), 1)
        self.assertEqual(again, self.served)
        # A page whose anchor is gone is served unchanged rather than broken.
        without_anchor = self.page.replace(gui.SOURCE_COMPARE_ANCHOR, '</section>')
        self.assertNotIn(gui.SOURCE_COMPARE_ANCHOR, without_anchor)
        self.assertEqual(gui.append_source_compare_script(gui.inject_source_compare(without_anchor)),
                         without_anchor)

    def test_the_section_shows_the_copy_verbatim_when_the_page_renders_it(self):
        # The same verdict strings the API returns, checked against the text the
        # page writes, so a report cannot change and the page keep the old words.
        self.assertIn('одинаковые наборы — уникальный вклад второго равен нулю', self.served)
        self.assertIn('Смещения, из-за которых число не вся правда', self.served)
        self.assertIn('Выживание по окнам', self.served)

    def test_the_endpoint_is_routed_on_the_same_server_that_serves_the_page(self):
        source = Path(gui.__file__).read_text(encoding='utf-8')
        self.assertIn("'/api/sources/compare'", source)
        self.assertRegex(source, r'def source_comparison\(self, payload\)')


class _OperatorKeys(apiv1.KeyStore):
    def verify(self, secret):
        if secret != KEY:
            return None
        return apiv1.Principal(key_id='k-op', kind='api_key',
                               permissions=frozenset(apiv1.PERMISSIONS))

    def read_audit(self, principal, **filters):
        return {'items': []}

    def audit(self, record):
        pass


class _Service:
    """The real engine behind `/v1`, so the route is measured, not mocked."""

    def __init__(self, data):
        from proxy_workbench import api
        self._inner = api.WorkbenchService(data)

    def __getattr__(self, name):
        return getattr(self._inner, name)


KEY = 'reach-sources-operator-key'


if __name__ == '__main__':
    unittest.main()

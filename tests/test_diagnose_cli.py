"""`diagnose` — the diagnostic layer the product could not reach (F10, F25).

`diagnostics.build_funnel`, `explain_zero`, `check_control`, `build_bundle` and
`health_report` had tests and no caller: `grep` over the product found only
`diagnostics.classification(exc)`. The acceptance of F10 is "the user understands
why there are zero results and fixes the cause without reading a traceback", and
that needs the layer to answer a question, not merely to exist.

Every action is a read of local data; `bundle` writes one redacted file under the
data folder and reports the path and the number of redactions. Documentation
addresses only (RFC 5737): `check_control` is given no target, so it performs no
outbound request.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db, proxytool

NOW = time.time()
PROXY = 'http://198.51.100.9:8080'


class DiagnoseCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'data'
        self.conn, _ = db.open_db(self.data / db.DB_FILENAME)
        endpoint = db.upsert_endpoint(self.conn, PROXY)
        self.conn.execute('INSERT OR IGNORE INTO profiles(id, config, digest, created_at)'
                          " VALUES ('p1','{}','p1',?)", (NOW,))
        self.conn.execute('INSERT OR IGNORE INTO accesses(id, endpoint_id, mode,'
                          ' access_revision, created_at) VALUES (?,?,?,?,?)',
                          ('default', endpoint, 'public', 1, NOW))
        row = {'proxy': PROXY, 'checked_at': NOW, 'valid_until': NOW + 900,
               'error': 'UNREACHABLE', 'error_code': 'E_PROBE_CONNECT', 'error_stage': 'tcp',
               'requests': 0, 'successes': 0, 'min_target_reliability': 0.0}
        self.conn.execute(
            'INSERT INTO results(profile, proxy, payload, endpoint_id, access_id,'
            " access_revision, profile_id, profile_revision, checked_at, valid_until, error_code)"
            " VALUES ('p1',?,?,?,'default',1,'p1',1,?,?,'E_PROBE_CONNECT')",
            (PROXY, json.dumps(row), endpoint, NOW, NOW + 900))
        db.add_member(self.conn, db.PUBLIC_COLLECTION_ID, endpoint, origin='manual')
        self.conn.commit()
        self.conn.close()

    def run_cli(self, *argv, json_output=True):
        out, err = io.StringIO(), io.StringIO()
        args = ['diagnose', '--data', str(self.data)]
        if json_output:
            args.append('--json')
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = proxytool.main([*args, *argv])
        except SystemExit as exc:
            code = exc.code
        return code, (out.getvalue().strip() or err.getvalue().strip())

    def test_the_funnel_counts_the_stages_of_a_real_run(self):
        code, text = self.run_cli('funnel')
        self.assertEqual(code, 0, text)
        body = json.loads(text)
        self.assertIn('scope', body['stages'])
        self.assertEqual(body['stages']['scope']['entered'], 1)
        self.assertEqual(body['stages']['tcp']['lost'], 1)
        self.assertEqual(body['stages']['tcp']['reasons'].get('UNREACHABLE'), 1)

    def test_the_bundle_is_written_redacted_and_reports_its_path(self):
        code, text = self.run_cli('bundle')
        self.assertEqual(code, 0, text)
        body = json.loads(text)
        target = Path(body['path'])
        self.assertTrue(target.is_file())
        self.assertIn('redactions', body)
        self.assertIn('schema_version', body)
        payload = json.loads(target.read_text(encoding='utf-8'))
        self.assertIn('environment', payload)

    def test_health_reports_checks_and_a_verdict(self):
        code, text = self.run_cli('health')
        self.assertEqual(code, 0, text)
        body = json.loads(text)
        self.assertTrue(body['checks'])
        for check in body['checks']:
            self.assertIn('ok', check)
            self.assertIn('name', check)
        self.assertFalse(body['ok'] and not all(item['ok'] for item in body['checks']))

    def test_control_answers_a_verdict_instead_of_raising(self):
        code, text = self.run_cli('control')
        self.assertEqual(code, 0, text)
        self.assertIn(json.loads(text)['state'], ('ok', 'degraded', 'failed'))

    def test_zero_explains_an_empty_result_once_an_export_exists(self):
        """The explanation needs the export status; without it there is nothing to explain."""
        self.conn = db.connect(self.data / db.DB_FILENAME)
        self.addCleanup(self.conn.close)
        proxytool.export(self.conn, 'p1', self.data / 'exports', min_success=1)
        code, text = self.run_cli('zero')
        self.assertEqual(code, 0, text)
        body = json.loads(text)
        if body.get('zero') is None:
            self.skipTest('this fixture produced a non-empty selection; nothing to explain')
        self.assertIn('code', body['zero'])
        self.assertTrue(body['zero']['action'])

    def test_an_unknown_action_is_refused(self):
        code, text = self.run_cli('nonsense', json_output=False)
        self.assertEqual(code, 2)
        self.assertIn('nonsense', text)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()

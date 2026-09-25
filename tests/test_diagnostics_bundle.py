"""Redaction, the local bundle, health and the reproduction recipe (F25).

The canary below is a fake secret: it must not survive into the bundle payload,
the preview, or the file on disk.  Every test here is local; the one subprocess
test proves that importing and using the module does not touch the network.
"""
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import branding
from proxy_workbench import diagnostics as d

ROOT = Path(__file__).resolve().parents[1]
CANARY = 'canary-PW-4f2b91c7d0e3'
CANARY_URL = f'http://user:{CANARY}@proxy.invalid:8080'
NOW = 1_700_000_000.0


def row(proxy='http://10.0.0.1:8080', **extra):
    payload = dict(proxy=proxy, country='DE', checked_at=NOW - 30, valid_until=NOW + 3600,
                   reliability=1.0, min_target_reliability=1.0, successes=1, requests=1, score=0.9,
                   reputation={'status': 'clean', 'dnsbl': []}, anonymity={'level': 'basic'},
                   samples=[dict(ok=True, ms=12, status=200, bytes=8, error=None, target=0, attempt=1)])
    payload.update(extra)
    return payload


def status(**overrides):
    base = dict(state='complete', stop_reason='complete', candidates=2, scope_candidates=2,
                checked=2, pending=0, passed=2, exported=2, local_filtered=0,
                profile='abc123', generation='.generation-abcdefgh', collection_id='col-1')
    base.update(overrides)
    return base


class RedactionTests(unittest.TestCase):
    def test_credentials_in_a_url_are_replaced(self):
        redacted, notes = d.redact_with_notes({'proxy': CANARY_URL})
        self.assertNotIn(CANARY, redacted['proxy'])
        self.assertTrue(redacted['proxy'].startswith('http://'))
        self.assertIn('proxy.invalid:8080', redacted['proxy'])
        self.assertEqual([note.kind for note in notes], ['credentials'])

    def test_authorization_headers_and_token_fields_are_replaced(self):
        payload = {'headers': {'Authorization': f'Bearer {CANARY}', 'accept': '*/*'},
                   'access': {'password': CANARY, 'api_key': CANARY, 'login': 'user'},
                   'url': f'https://source.invalid/list?token={CANARY}&page=2'}
        redacted, notes = d.redact_with_notes(payload)
        self.assertEqual(redacted['headers']['Authorization'], '***')
        self.assertEqual(redacted['headers']['accept'], '*/*')
        self.assertEqual(redacted['access']['password'], '***')
        self.assertEqual(redacted['access']['api_key'], '***')
        self.assertEqual(redacted['access']['login'], 'user')
        self.assertNotIn(CANARY, redacted['url'])
        self.assertIn('page=2', redacted['url'])
        kinds = {note.kind for note in notes}
        self.assertEqual(kinds, {'header', 'secret', 'query'})

    def test_a_bearer_token_in_free_text_is_replaced(self):
        redacted, _notes = d.redact_with_notes({'log': f'GET / HTTP/1.1 Authorization: Bearer {CANARY}'})
        self.assertNotIn(CANARY, redacted['log'])

    def test_notes_never_carry_the_value_they_removed(self):
        _redacted, notes = d.redact_with_notes({'password': CANARY, 'url': CANARY_URL})
        self.assertTrue(notes)
        for note in notes:
            self.assertNotIn(CANARY, repr(note))
            self.assertIn(note.kind, ('credentials', 'header', 'query', 'secret', 'host'))

    def test_hosts_are_kept_by_default_and_replaced_on_request(self):
        kept = d.redact_value({'proxy': 'http://10.0.0.1:8080'})
        self.assertEqual(kept['proxy'], 'http://10.0.0.1:8080')
        hidden = d.redact_value({'proxy': 'http://10.0.0.1:8080'}, d.Redaction(hosts=True))
        self.assertNotIn('10.0.0.1', hidden['proxy'])
        self.assertIn('8080', hidden['proxy'])

    def test_a_policy_without_credentials_keeps_the_userinfo(self):
        redacted = d.redact_value({'proxy': CANARY_URL}, d.Redaction(credentials=False))
        self.assertIn(CANARY, redacted['proxy'])

    def test_a_policy_needs_a_placeholder(self):
        with self.assertRaises(ValueError):
            d.Redaction(placeholder='')


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make(self, **overrides):
        kwargs = dict(rows=[row()], status=status(), job={'id': 'job-1', 'state': 'succeeded'})
        kwargs.update(overrides)
        return d.build_bundle(now=NOW, **kwargs)

    def test_the_canary_is_absent_from_payload_preview_and_file(self):
        rows = [row(proxy=CANARY_URL,
                    samples=[dict(ok=False, ms=1, status=407, error='HTTP_407', target=0, attempt=1,
                                  headers={'Proxy-Authorization': f'Basic {CANARY}'})])]
        bundle = self.make(rows=rows, status=status(exported=0, passed=0),
                           zero=d.ZeroResult(code='AUTH_FAILED', stage='auth',
                                            summary='Прокси отклонил учётные данные',
                                            action='Проверьте логин и пароль.'))
        self.assertNotIn(CANARY, bundle.to_json())
        self.assertNotIn(CANARY, bundle.preview())
        written = bundle.save(self.root / 'bundle.json')
        self.assertNotIn(CANARY.encode(), written.read_bytes())

    def test_nothing_is_written_before_save_is_called(self):
        target = self.root / 'nested' / 'bundle.json'
        self.make()
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_save_refuses_to_clobber_an_existing_file(self):
        target = self.root / 'bundle.json'
        self.make().save(target)
        with self.assertRaises(FileExistsError):
            self.make().save(target)
        self.make().save(target, overwrite=True)
        self.assertTrue(target.exists())

    def test_the_bundle_carries_versions_scope_job_and_the_funnel(self):
        bundle = self.make()
        payload = bundle.payload
        self.assertEqual(payload['schema_version'], d.BUNDLE_SCHEMA_VERSION)
        self.assertEqual(payload['environment']['product_version'], branding.PRODUCT_VERSION)
        self.assertEqual(payload['status']['collection_id'], 'col-1')
        self.assertEqual(payload['job']['id'], 'job-1')
        self.assertIn('stages', payload['funnel'])
        self.assertEqual(payload['rows']['total'], 1)
        self.assertEqual(json.loads(bundle.to_json())['status']['generation'], '.generation-abcdefgh')

    def test_the_row_sample_is_capped_and_the_truncation_is_visible(self):
        bundle = self.make(rows=[row(proxy=f'http://10.0.0.{index}:80') for index in range(20)], sample=5)
        self.assertEqual(bundle.sample_size, 5)
        self.assertEqual(bundle.total_size, 20)
        self.assertTrue(bundle.truncated)
        self.assertEqual(len(bundle.payload['rows']['items']), 5)

    def test_the_preview_is_bounded_and_shows_the_counts(self):
        bundle = self.make(rows=[row(proxy=f'http://10.0.0.{index}:80') for index in range(200)])
        preview = bundle.preview(limit=400)
        self.assertIn('schema=', preview)
        self.assertLess(len(preview), 900)
        self.assertLess(len(preview), len(bundle.preview()))

    def test_the_bundle_describes_itself_as_local(self):
        description = self.make().describe('en')
        self.assertIn('sent nowhere', description)

    def test_an_existing_funnel_is_reused_instead_of_recomputed(self):
        unreachable = row('http://10.0.0.2:80', error='UNREACHABLE', samples=[])
        funnel = d.build_funnel([row(), unreachable], status=status(exported=1, passed=1), now=NOW)
        bundle = self.make(funnel=funnel, rows=[])
        self.assertEqual(bundle.payload['funnel']['stages']['tcp']['reasons'], {'UNREACHABLE': 1})
        self.assertEqual(bundle.total_size, 0)

    def test_building_and_saving_uses_no_socket(self):
        def boom(*args, **kwargs):
            raise AssertionError('the diagnostics layer must not open a socket')

        originals = (socket.socket, socket.create_connection, socket.getaddrinfo)
        socket.socket = socket.create_connection = socket.getaddrinfo = boom
        try:
            bundle = self.make()
            written = bundle.save(self.root / 'offline.json')
            verdict = d.check_control(limits=d.ControlLimits(total_requests=0),
                                      checker=lambda probe, limits: {'ok': True})
        finally:
            socket.socket, socket.create_connection, socket.getaddrinfo = originals
        self.assertTrue(written.exists())
        self.assertEqual(verdict.state, 'skipped')

    def test_importing_the_module_opens_no_socket(self):
        script = (
            'import socket\n'
            'def boom(*a, **k):\n'
            '    raise AssertionError("network access")\n'
            'socket.socket = socket.create_connection = socket.getaddrinfo = boom\n'
            'import json, pathlib, tempfile\n'
            'from proxy_workbench import diagnostics as d\n'
            'with tempfile.TemporaryDirectory() as tmp:\n'
            '    bundle = d.build_bundle(rows=[{"proxy": "http://10.0.0.1:80", "password": "canary"}])\n'
            '    path = bundle.save(pathlib.Path(tmp) / "b.json")\n'
            '    assert "canary" not in path.read_text()\n'
            'print(len(d.CODES))\n'
        )
        done = subprocess.run([sys.executable, '-c', script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), str(len(d.CODES)))


class HealthTests(unittest.TestCase):
    def test_a_healthy_environment_reports_no_problems(self):
        report = d.health_report(version=branding.PRODUCT_VERSION, schema_version=14,
                                 scope={'collection_id': 'col-1', 'profile_id': 'p1', 'profile_revision': 2},
                                 job={'state': 'running'}, max_age_seconds=7200)
        self.assertTrue(report.ok, report.render('en'))
        self.assertEqual(report.failures, ())

    def test_an_unpinned_scope_is_named_with_a_code_and_an_action(self):
        report = d.health_report(scope={'collection_id': 'col-1'})
        check = report.check('scope')
        self.assertFalse(report.ok)
        self.assertEqual(check.code, 'SCOPE_UNSPECIFIED')
        self.assertEqual(check.params['field'], 'profile_id')
        self.assertTrue(check.action)
        self.assertIn('SCOPE_UNSPECIFIED', report.render('en'))

    def test_a_missing_version_and_schema_are_separate_problems(self):
        report = d.health_report(scope={'collection_id': 'c', 'profile_id': 'p', 'profile_revision': 1})
        self.assertEqual({check.name for check in report.failures}, {'version', 'schema'})
        self.assertEqual(report.check('version').code, 'VERSION_UNKNOWN')
        self.assertEqual(report.check('schema').code, 'SCHEMA_UNKNOWN')

    def test_an_unknown_job_state_is_not_treated_as_healthy(self):
        report = d.health_report(job={'state': 'half-done'})
        self.assertEqual(report.check('job').code, 'JOB_FAILED')
        self.assertEqual(report.check('job').params['stop_reason'], 'half-done')

    def test_a_broken_device_shows_up_in_the_health_report(self):
        verdict = d.ControlVerdict(state='device_network', checked=2, limit=3, code='DEVICE_NETWORK_DOWN')
        report = d.health_report(control=verdict)
        self.assertEqual(report.check('control').code, 'DEVICE_NETWORK_DOWN')
        self.assertFalse(report.ok)

    def test_a_max_age_left_unset_is_visible(self):
        report = d.health_report(profile={'id': 'p1', 'revision': 1}, max_age_seconds=0)
        self.assertEqual(report.check('freshness').code, 'TTL_EXPIRED')

    def test_the_report_is_serializable(self):
        report = d.health_report(version=branding.PRODUCT_VERSION, schema_version=14,
                                 scope={'collection_id': 'c', 'profile_id': 'p', 'profile_revision': 1})
        self.assertEqual(json.loads(json.dumps(report.to_dict()))['ok'], True)


class RecipeTests(unittest.TestCase):
    config = {'targets': [{'url': 'https://target.invalid/check', 'method': 'GET', 'statuses': [200],
                           'contains': 'ok', 'sha256': None, 'headers': {'accept': '*/*'}}],
              'attempts': 3, 'timeout': 7.5, 'connect_timeout': 2.5, 'request_profile': 'standard'}

    def test_a_complete_recipe_reproduces_the_measurement(self):
        recipe = d.fixture_recipe(row(), self.config)
        self.assertTrue(recipe.complete)
        self.assertIsNone(recipe.code)
        self.assertEqual(recipe.attempts, 3)
        self.assertEqual(recipe.timeout_s, 7.5)
        self.assertEqual(recipe.connect_timeout_s, 2.5)
        self.assertEqual(recipe.request_profile, 'standard')
        self.assertEqual([target['url'] for target in recipe.targets], ['https://target.invalid/check'])
        self.assertEqual([target['contains'] for target in recipe.targets], ['ok'])
        config = json.loads(recipe.config_json())
        self.assertEqual(config['targets'][0]['statuses'], [200])
        self.assertEqual(config['attempts'], 3)
        self.assertEqual(len(recipe.samples), 1)

    def test_the_recipe_json_is_a_profile_configuration(self):
        recipe = d.fixture_recipe(row(), self.config)
        config = json.loads(recipe.config_json())
        self.assertEqual(sorted(config), ['attempts', 'connect_timeout', 'request_profile', 'targets', 'timeout'])

    def test_a_recipe_without_a_profile_says_it_is_incomplete(self):
        recipe = d.fixture_recipe(row(), None)
        self.assertFalse(recipe.complete)
        self.assertEqual(recipe.code, 'RECIPE_NO_PROFILE')
        rendered = recipe.render('en')
        self.assertIn('RECIPE_NO_PROFILE', rendered)
        self.assertIn('Action:', rendered)

    def test_a_recipe_without_samples_refuses_to_guess(self):
        empty = row()
        empty.pop('samples')
        recipe = d.fixture_recipe(empty, self.config)
        self.assertFalse(recipe.complete)
        self.assertEqual(recipe.code, 'RECIPE_NO_SAMPLES')

    def test_a_recipe_never_carries_credentials(self):
        config = dict(self.config, targets=[dict(self.config['targets'][0],
                                                url=f'https://user:{CANARY}@target.invalid/check')])
        recipe = d.fixture_recipe(row(), config)
        self.assertNotIn(CANARY, recipe.config_json())
        self.assertNotIn(CANARY, recipe.render('ru'))


if __name__ == '__main__':
    unittest.main()

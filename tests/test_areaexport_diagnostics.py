"""F10 / F25: why there are zero results, and a diagnostic bundle that leaks nothing.

What the code did before this file existed, proven by running it:

* ``Funnel.zero_result`` required ``lost_total() == 0`` when the run had no
  ``exported`` total.  A run in which *every* address was lost therefore
  reported "not a zero-result run" and ``explain_zero`` returned ``None``.  Six
  of the twelve ways a run can come out empty produced no explanation at all:
  unreachable sources, empty sources, unknown measurement time, the device
  being offline, the target being down, an exhausted budget.  That is exactly
  the run F10 says a user must be able to read without a traceback.
* ``FixtureRecipe.to_dict()`` - the shape an in-app help page or an API serves
  for F25 - carried ``source.proxy`` and the sample errors verbatim, so
  ``http://user:<password>@host:port`` came back in the reproduction recipe even
  though every other diagnostic surface redacts it.
"""
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import diagnostics as dg

NOW = 1_700_000_000.0
CANARY = 'C4NARY-9f3a2b7c-PASSWORD'


def good(proxy):
    return {'proxy': proxy, 'checked_at': NOW, 'valid_until': NOW + 600, 'requests': 3,
            'successes': 3, 'samples': [{'target': 'http://localhost/health', 'ok': True, 'status': 200}]}


def lost(proxy, error, **extra):
    row = {'proxy': proxy, 'checked_at': NOW, 'valid_until': NOW + 600, 'requests': 1,
           'samples': [{'target': 'http://localhost/health', 'ok': False, 'error': error}]}
    row.update(extra)
    return row


def control(state, kind_code, bad_kind):
    def checker(probe, limits):
        bad = probe.kind == bad_kind
        return {'kind': probe.kind, 'target': probe.target, 'ok': not bad,
                'code': kind_code if bad else None}
    return dg.check_control(checker=checker, limits=dg.ControlLimits(total_requests=3),
                            targets=({'url': 'http://localhost/health'},))


# ---------------------------------------------------------------------------
# F10 acceptance: every way to get zero results names a cause and an action
# ---------------------------------------------------------------------------

class ZeroResultExplanationTests(unittest.TestCase):
    def explain(self, **kwargs):
        now = kwargs.pop('now', NOW)
        funnel = dg.build_funnel(now=now, **kwargs)
        self.assertTrue(funnel.zero_result(), 'the run produced nothing and must be explained')
        zero = dg.explain_zero(funnel, lang='en')
        self.assertIsNotNone(zero, 'a zero result with no explanation is the defect')
        self.assertTrue(zero.code)
        self.assertTrue(zero.action)
        self.assertIn(zero.code, dg.CODES)
        rendered = zero.render('en')
        self.assertNotIn('Traceback', rendered)
        self.assertNotIn('Error', rendered)
        self.assertIn(zero.code, rendered)
        return zero

    def test_every_source_failed_to_download(self):
        zero = self.explain(sources=[{'source': 's1', 'rows': 0, 'complete': False, 'error': 'ConnectionRefusedError'},
                                     {'source': 's2', 'rows': 0, 'complete': False, 'error': 'ConnectTimeout'}])
        self.assertEqual(zero.code, 'SOURCE_UNAVAILABLE')
        self.assertEqual(zero.params['count'], 2)
        self.assertIn('last good source data is kept', zero.action)

    def test_the_sources_answered_and_nothing_parsed(self):
        self.assertEqual(self.explain(sources=[{'source': 's1', 'rows': 0, 'complete': True}]).code,
                         'SOURCE_EMPTY')

    def test_nothing_was_collected_at_all(self):
        self.assertEqual(self.explain().code, 'NO_SOURCES')

    def test_the_scope_filter_dropped_every_address(self):
        zero = self.explain(status={'candidates': 120, 'scope_candidates': 0, 'exported': 0})
        self.assertEqual(zero.code, 'SCOPE_EMPTY')
        self.assertIn('SCOPE_FILTERED', zero.related)

    def test_every_row_expired(self):
        zero = self.explain(rows=[{'proxy': f'http://11.0.0.{i}:8080', 'checked_at': NOW - 9000,
                                   'valid_until': NOW - 3600, 'requests': 3,
                                   'samples': [{'ok': True, 'status': 200}]} for i in (1, 2, 3)])
        self.assertEqual(zero.code, 'E_TIME_TTL_EXPIRED')
        self.assertIn('recheck', zero.action.lower())

    def test_no_row_carries_a_measurement_time(self):
        zero = self.explain(rows=[{'proxy': 'http://11.0.0.1:8080', 'requests': 3,
                                   'samples': [{'ok': True, 'status': 200}]}])
        self.assertEqual(zero.code, 'E_TIME_UNKNOWN')

    def test_the_device_had_no_network(self):
        verdict = control('device_network', 'DEVICE_NETWORK_DOWN', 'connect')
        self.assertEqual(verdict.state, 'device_network')
        zero = self.explain(rows=[lost(f'http://11.0.0.{i}:8080', 'ConnectError') for i in (1, 2)],
                            control=verdict)
        self.assertEqual(zero.code, 'DEVICE_NETWORK_DOWN')
        self.assertIn('not there', zero.action)

    def test_the_target_was_down_for_everybody(self):
        verdict = control('target_outage', 'TARGET_OUTAGE', 'http')
        self.assertEqual(verdict.state, 'target_outage')
        zero = self.explain(rows=[lost(f'http://11.0.0.{i}:8080', 'HTTP_503') for i in (1, 2, 3, 4)],
                            control=verdict)
        self.assertEqual(zero.code, 'TARGET_OUTAGE')
        self.assertIn('not an address error', zero.action)

    def test_the_budget_ran_out(self):
        zero = self.explain(rows=[lost(f'http://11.0.0.{i}:8080', 'BUDGET_EXHAUSTED') for i in (1, 2)])
        self.assertEqual(zero.code, 'E_LIMIT_BUDGET')

    def test_everything_passed_and_the_export_filter_dropped_it(self):
        zero = self.explain(rows=[good(f'http://11.0.0.{i}:8080') for i in (1, 2, 3)],
                            status={'scope_candidates': 3, 'checked': 3, 'passed': 3, 'exported': 0})
        self.assertEqual(zero.code, 'SCOPE_FILTERED_ALL')
        self.assertIn('Relax the export filters', zero.action)

    def test_the_local_denylist_took_the_rows(self):
        zero = self.explain(rows=[good(f'http://11.0.0.{i}:8080') for i in (1, 2, 3)],
                            status={'scope_candidates': 3, 'checked': 3, 'passed': 3,
                                    'exported': 0, 'local_filtered': 2})
        self.assertEqual(zero.code, 'SCOPE_DENYLIST')

    def test_a_run_with_results_is_not_explained(self):
        funnel = dg.build_funnel([good('http://11.0.0.1:8080')],
                                 status={'exported': 1, 'passed': 1, 'checked': 1}, now=NOW)
        self.assertFalse(funnel.zero_result())
        self.assertIsNone(dg.explain_zero(funnel))
        self.assertIsNone(dg.funnel_explanation(funnel))

    def test_a_single_surviving_row_stops_the_zero_explanation(self):
        # One address out of four: not a zero result, and the loss of the other
        # three is not reported as the reason for an empty list.
        funnel = dg.build_funnel([good('http://11.0.0.1:8080'),
                                  lost('http://11.0.0.2:8080', 'ConnectError')],
                                 status={'checked': 2}, now=NOW)
        self.assertFalse(funnel.zero_result())
        self.assertIsNone(dg.explain_zero(funnel))


class BlameTests(unittest.TestCase):
    """A global outage must not become a reputation mark on every address."""

    def test_a_device_outage_is_attributed_to_the_device_at_every_early_stage(self):
        verdict = control('device_network', 'DEVICE_NETWORK_DOWN', 'connect')
        for stage in ('device_network', 'dns', 'tcp', 'handshake', 'tls'):
            self.assertEqual(verdict.blame(stage), 'device', stage)
        self.assertEqual(verdict.blame('target'), 'proxy')

    def test_a_target_outage_is_attributed_to_the_target(self):
        verdict = control('target_outage', 'TARGET_OUTAGE', 'http')
        for stage in ('target', 'assertion', 'rate_limit'):
            self.assertEqual(verdict.blame(stage), 'target', stage)
        self.assertEqual(verdict.blame('tcp'), 'proxy')

    def test_the_funnel_counters_carry_the_attribution_of_each_loss(self):
        verdict = control('device_network', 'DEVICE_NETWORK_DOWN', 'connect')
        funnel = dg.build_funnel([lost(f'http://11.0.0.{i}:8080', 'ConnectError') for i in (1, 2)],
                                 control=verdict, now=NOW)
        self.assertEqual(dict(funnel.attribution), {'device': 2})
        self.assertEqual(dict(funnel.stage_counters('tcp').attribution), {'device': 2})
        self.assertEqual(funnel.stage_counters('tcp').reasons['UNREACHABLE'], 2)

    def test_without_a_control_a_loss_stays_the_address_fault(self):
        funnel = dg.build_funnel([lost('http://11.0.0.1:8080', 'ConnectError')], now=NOW)
        self.assertEqual(dict(funnel.attribution), {'proxy': 1})

    def test_the_control_is_bounded_and_zero_requests_touches_nothing(self):
        def refuse(*args, **kwargs):
            raise AssertionError('the control opened a socket')
        with mock.patch.object(socket, 'socket', refuse), \
                mock.patch.object(socket, 'getaddrinfo', refuse), \
                mock.patch.object(socket, 'create_connection', refuse):
            verdict = dg.check_control(limits=dg.ControlLimits(total_requests=0))
        self.assertEqual((verdict.state, verdict.checked, verdict.network_ok), ('skipped', 0, True))
        with self.assertRaises(ValueError):
            dg.ControlLimits(total_requests=-1).validate()

    def test_the_control_never_asks_a_checker_for_more_than_its_budget(self):
        asked = []

        def checker(probe, limits):
            asked.append(probe.kind)
            return {'ok': True}

        dg.check_control(checker=checker, limits=dg.ControlLimits(total_requests=1),
                         targets=({'url': 'http://localhost/a'}, {'url': 'http://localhost/b'}))
        self.assertEqual(len(asked), 1)


# ---------------------------------------------------------------------------
# F25: help by code, redaction, the local bundle, a reproducible recipe
# ---------------------------------------------------------------------------

class HelpByCodeTests(unittest.TestCase):
    def test_a_code_and_its_parameters_are_separate_from_the_text(self):
        help_text = dg.help_for('AUTH_FAILED', params={'scheme': 'socks5'}, lang='ru')
        payload = help_text.to_dict()
        self.assertEqual(payload['code'], 'AUTH_FAILED')
        self.assertEqual(payload['stage'], 'auth')
        self.assertEqual(payload['params'], ['scheme'])
        self.assertEqual(payload['values'], {'scheme': 'socks5'})
        self.assertTrue(payload['title'].startswith('Прокси'))
        self.assertNotIn('AUTH_FAILED', payload['title'])
        self.assertIn('socks5', help_text.render('ru'))
        self.assertIn('Действие:', help_text.render('ru'))
        english = dg.help_for('AUTH_FAILED', params={'scheme': 'socks5'}, lang='en')
        self.assertTrue(english.render('en').startswith('Proxy rejected the credentials'))
        self.assertIn('Action:', english.render('en'))

    def test_a_specific_status_is_explained_by_the_documented_family(self):
        # ``classification`` keeps HTTP_503 as the counter key on purpose; asking
        # for help with it used to answer "Unknown error code: HTTP_503", so the
        # single most common target failure had no action to offer.
        for status, family, stage in ((503, 'HTTP_5XX', 'target'), (404, 'HTTP_4XX', 'target'),
                                      (302, 'HTTP_3XX', 'target')):
            with self.subTest(status=status):
                code = f'HTTP_{status}'
                self.assertEqual(dg.classification(code), (stage, code))
                help_text = dg.help_for(code, lang='en')
                self.assertTrue(help_text.known, code)
                self.assertEqual(help_text.code, family)
                self.assertEqual(help_text.values, {'status': status})
                self.assertTrue(help_text.action)
        # a rate limit and a proxy auth challenge keep their own, more specific code
        self.assertEqual(dg.help_for('HTTP_429').code, 'RATE_LIMITED')
        self.assertEqual(dg.help_for('HTTP_407').code, 'AUTH_FAILED')
        # and an engine literal nobody described is still reported, not invented
        self.assertFalse(dg.help_for('HTTP_999').known)

    def test_an_undocumented_code_is_honest_rather_than_invented(self):
        help_text = dg.help_for('E_MADE_UP_CODE')
        self.assertFalse(help_text.known)
        self.assertEqual(help_text.domain, 'UNKNOWN')
        self.assertIn('E_MADE_UP_CODE', help_text.title)
        self.assertTrue(help_text.action)

    def test_every_documented_code_carries_an_action_and_a_known_stage(self):
        for entry in dg.help_entries():
            self.assertTrue(entry.action, entry.code)
            self.assertIn(entry.stage, dg.STAGES, entry.code)
        self.assertEqual(len(dg.help_entries()), len(dg.CODES))
        # the module documents contract codes; it may not mint a new E_* one
        minted = [code for code in dg.CODES if code.startswith('E_') and code not in dg.CONTRACT_CODES]
        self.assertEqual(minted, [])
        self.assertEqual(dg.help_for('E_LIMIT_BUDGET').code, 'E_LIMIT_BUDGET')

    def test_an_exception_class_is_named_by_what_actually_happened(self):
        for exc_name, code in (('ConnectTimeout', 'TCP_TIMEOUT'), ('ConnectionRefusedError', 'TCP_REFUSED'),
                               ('SSLError', 'TLS_ERROR'), ('SSLCertVerificationError', 'TLS_CERT_INVALID'),
                               ('ProxyError', 'HANDSHAKE_ERROR'), ('ReadTimeout', 'TIMEOUT')):
            with self.subTest(exc=exc_name):
                self.assertEqual(dg.explain_error(type(exc_name, (Exception,), {}), lang='en').code, code)

    def test_a_resolver_failure_inside_a_connect_error_is_still_a_dns_diagnosis(self):
        exc = type('ConnectError', (Exception,), {})('Name or service not known')
        self.assertEqual(dg.explain_error(exc, lang='en').code, 'DNS_ERROR')
        self.assertEqual(dg.explain_error('HTTP_429', lang='en').code, 'RATE_LIMITED')
        self.assertEqual(dg.explain_error({'status': 407}, lang='en').code, 'AUTH_FAILED')
        self.assertEqual(dg.explain_error('HTTP_503', lang='en').code, 'HTTP_5XX')

    def test_an_actionable_error_renders_without_a_traceback(self):
        error = dg.actionable('E_LIMIT_BUDGET', params={'kind': 'requests', 'used': 900, 'limit': 900})
        self.assertEqual(error.stage, 'budget')
        rendered = error.render('en')
        self.assertIn('E_LIMIT_BUDGET', rendered)
        self.assertIn('Action:', rendered)
        self.assertNotIn('Traceback', rendered)
        with self.assertRaises(KeyError):
            dg.ActionableError('NOT_A_CODE')


class HealthTests(unittest.TestCase):
    def test_every_missing_piece_is_named_with_an_action(self):
        report = dg.health_report(version=None, schema_version=None,
                                  scope={'collection_id': 'col-1'},
                                  job={'state': 'weird'}, max_age_seconds=0)
        self.assertFalse(report.ok)
        self.assertEqual([check.name for check in report.failures],
                         ['version', 'schema', 'scope', 'job', 'freshness'])
        for check in report.failures:
            self.assertIn(check.code, dg.CODES)
            self.assertTrue(check.action)
        rendered = report.render('en')
        self.assertNotIn('Traceback', rendered)
        self.assertEqual(rendered.count('Action:'), 5)

    def test_a_healthy_configuration_reports_every_check(self):
        report = dg.health_report(version='2.3.0', schema_version=11,
                                  scope={'collection_id': 'col-1', 'profile_id': 'basic',
                                         'profile_revision': 1},
                                  job={'state': 'succeeded'}, max_age_seconds=900)
        self.assertTrue(report.ok)
        self.assertEqual(report.render('ru').count('Action:'), 0)


class BundleRedactionTests(unittest.TestCase):
    CANARY_ROWS = [
        {'proxy': f'http://user:{CANARY}@11.0.0.{i}:8080', 'checked_at': NOW, 'valid_until': NOW + 600,
         'requests': 1, 'error': f'ProxyError: 407 with Authorization: Basic {CANARY}',
         'samples': [{'target': 'http://localhost/health', 'ok': False, 'error': 'AUTH_FAILED',
                      'url': f'http://user:{CANARY}@11.0.0.{i}:8080/target?api_key={CANARY}&page=2',
                      'headers': {'Authorization': f'Bearer {CANARY}', 'X-Api-Key': CANARY,
                                  'Accept': '*/*'}}]}
        for i in (1, 2)]

    def bundle(self):
        rows = self.CANARY_ROWS
        funnel = dg.build_funnel(rows, status={'state': 'empty', 'exported': 0},
                                 sources=[{'source': 's1', 'url': f'https://list.example/p.txt?token={CANARY}',
                                           'rows': 0, 'error': 'HTTP_429',
                                           'headers': {'Set-Cookie': f'sid={CANARY}'}}], now=NOW)
        return dg.build_bundle(status={'state': 'empty', 'exported': 0}, rows=rows, funnel=funnel,
                               job={'id': 'job-7', 'state': 'failed', 'api_token': CANARY,
                                    'env': {'API_TOKEN': CANARY, 'HOME': '/home/u'}},
                               zero=dg.explain_zero(funnel), now=NOW, version=1)

    def test_the_canary_is_nowhere_in_the_bundle(self):
        text = self.bundle().to_json()
        self.assertNotIn(CANARY, text)
        self.assertNotIn(CANARY, json.dumps([note.to_dict() for note in self.bundle().redactions]))

    def test_what_was_removed_is_recorded_without_the_value(self):
        bundle = self.bundle()
        kinds = {note.kind for note in bundle.redactions}
        self.assertTrue({'secret', 'header', 'query', 'credentials'} <= kinds)
        for note in bundle.redactions:
            self.assertNotIn(CANARY, json.dumps(note.to_dict()))

    def test_the_credential_in_the_endpoint_becomes_a_placeholder(self):
        payload = self.bundle().payload
        self.assertEqual(payload['rows']['items'][0]['proxy'], 'http://***@11.0.0.1:8080')
        self.assertEqual(payload['job']['api_token'], '***')
        self.assertEqual(payload['job']['env']['HOME'], '/home/u')

    def test_the_preview_names_what_it_is_before_anything_is_written(self):
        bundle = self.bundle()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / 'bundle.json'
            self.assertFalse(target.exists())
            self.assertIn('diagnostic bundle', bundle.preview())
            self.assertFalse(target.exists(), 'a preview must not create the file')
            bundle.save(target)
            self.assertNotIn(CANARY, target.read_text(encoding='utf-8'))
            with self.assertRaises(FileExistsError):
                bundle.save(target)
            bundle.save(target, overwrite=True)
        self.assertIn('sent nowhere', bundle.describe('en'))
        self.assertIn('никуда не отправляется', bundle.describe('ru'))

    def test_the_bundle_reports_versions_scope_and_funnel_counters(self):
        payload = self.bundle().payload
        self.assertEqual(payload['product'], 'Proxy Workbench')
        self.assertIn('python', payload['environment'])
        self.assertEqual(payload['status']['state'], 'empty')
        self.assertIn('auth', payload['funnel']['stages'])
        self.assertTrue(payload['zero_result']['code'])

    def test_only_a_sample_of_the_rows_is_carried(self):
        rows = [good(f'http://11.0.0.{i}:8080') for i in range(1, 8)]
        bundle = dg.build_bundle(rows=rows, sample=3, now=NOW, version=1)
        self.assertEqual((bundle.sample_size, bundle.total_size, bundle.truncated), (3, 7, True))


class RecipeTests(unittest.TestCase):
    ROW = {'proxy': f'http://user:{CANARY}@11.0.0.1:8080', 'checked_at': NOW, 'profile': 'basic',
           'samples': [{'target': 'http://localhost/health', 'ok': False, 'error': 'AUTH_FAILED',
                        'url': f'http://user:{CANARY}@11.0.0.1:8080/x?token={CANARY}', 'attempt': 1}]}
    CONFIG = {'targets': [{'url': f'https://example.com/p?token={CANARY}', 'statuses': [200],
                           'headers': {'Authorization': f'Bearer {CANARY}'}}],
              'attempts': 2, 'timeout': 7.5, 'connect_timeout': 2.0, 'request_profile': 'workbench'}

    def test_the_serialized_recipe_never_carries_the_password(self):
        # This is the defect: `to_dict` is what a help page or an API serves,
        # and it used to pass source.proxy and the sample errors through.
        payload = json.dumps(self.ROW and dg.fixture_recipe(self.ROW, self.CONFIG).to_dict())
        self.assertNotIn(CANARY, payload)
        self.assertIn('http://***@11.0.0.1:8080', payload)
        self.assertNotIn(CANARY, dg.fixture_recipe(self.ROW, self.CONFIG).config_json())

    def test_a_recipe_without_a_profile_says_so_instead_of_guessing(self):
        recipe = dg.fixture_recipe(self.ROW, None)
        self.assertFalse(recipe.complete)
        self.assertEqual(recipe.code, 'RECIPE_NO_PROFILE')
        self.assertIn('Action:', recipe.render('en'))
        self.assertNotIn(CANARY, json.dumps(recipe.to_dict()))

    def test_a_row_without_samples_says_so(self):
        recipe = dg.fixture_recipe({'proxy': 'http://11.0.0.1:8080'}, self.CONFIG)
        self.assertFalse(recipe.complete)
        self.assertEqual(recipe.code, 'RECIPE_NO_SAMPLES')

    def test_a_complete_recipe_repeats_the_measurement(self):
        recipe = dg.fixture_recipe(self.ROW, self.CONFIG)
        self.assertTrue(recipe.complete)
        config = recipe.to_dict()['config']
        self.assertEqual(config['attempts'], 2)
        self.assertEqual(config['timeout'], 7.5)
        self.assertEqual(config['connect_timeout'], 2.0)
        self.assertEqual(config['request_profile'], 'workbench')
        self.assertEqual(config['targets'][0]['statuses'], [200])
        self.assertEqual(config['targets'][0]['headers']['Authorization'], '***')
        self.assertEqual(recipe.to_dict()['source']['samples'], 1)
        self.assertIn('example.com/p', recipe.render('en'))


if __name__ == '__main__':
    unittest.main()

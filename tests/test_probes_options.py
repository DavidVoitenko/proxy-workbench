"""F07: parameters with units, limits, one validation, and no silent downgrade."""
import json
import ssl
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import probes as pr

PUBLIC_TARGET = {'id': 'svc', 'url': 'https://svc.invalid/health', 'statuses': [200],
                 'contains': 'ok', 'min_body_bytes': 1}


def ca_bundle_path():
    """A real CA file if this machine has one; the TLS assertions do not need it."""
    candidates = []
    try:
        import certifi
        candidates.append(certifi.where())
    except ImportError:
        pass
    candidates.append(ssl.get_default_verify_paths().cafile)
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return None


class LimitsTests(unittest.TestCase):
    def test_every_limit_is_documented_with_a_unit(self):
        for name, limit in pr.LIMITS.items():
            with self.subTest(name=name):
                self.assertTrue(limit.describe())
                self.assertLessEqual(limit.minimum, limit.maximum)

    def test_each_limit_rejects_its_own_out_of_range_values(self):
        cases = {'connect_timeout_s': (0.0, 31), 'handshake_timeout_s': (0.0, 31), 'read_timeout_s': (0.0, 200),
                 'whole_probe_timeout_s': (0.1, 601), 'attempts': (0, 11), 'backoff_s': (-1, 31),
                 'backoff_factor': (0.5, 6), 'backoff_max_s': (-1, 61),
                 'max_body_bytes': (0, 9 * 1024 * 1024), 'max_redirects': (-1, 6)}
        for name, (low, high) in cases.items():
            for value in (low, high):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(pr.ProbeError) as caught:
                        pr.validate_options({name: value})
                    self.assertEqual(caught.exception.code, pr.E_VALIDATION_FIELD)

    def test_unknown_parameters_are_never_ignored_silently(self):
        with self.assertRaises(pr.ProbeError) as caught:
            pr.validate_options({'timoout_s': 3, 'whole_probe_timeout': 10})
        self.assertEqual(caught.exception.code, pr.E_VALIDATION_UNKNOWN_FIELD)
        self.assertIn('timoout_s', str(caught.exception))

    def test_wrong_types_and_booleans_are_refused(self):
        for value in ('fast', True, [1], float('nan'), float('inf')):
            with self.subTest(value=value):
                with self.assertRaises(pr.ProbeError):
                    pr.validate_options({'read_timeout_s': value})
        with self.assertRaises(pr.ProbeError):
            pr.validate_options({'attempts': 1.5})

    def test_options_describe_themselves_for_every_client(self):
        options = pr.validate_options({'attempts': 3})
        self.assertEqual(options.attempts, 3)
        described = options.describe()
        self.assertEqual(set(described), set(options.to_public()))
        self.assertIn('с', described['read_timeout_s'])

    def test_whole_probe_deadline_must_fit_one_attempt(self):
        with self.assertRaises(pr.ProbeError) as caught:
            pr.validate_options({'connect_timeout_s': 4, 'handshake_timeout_s': 6, 'read_timeout_s': 8,
                                 'whole_probe_timeout_s': 10})
        self.assertIn('whole_probe_timeout_s', str(caught.exception))
        options = pr.validate_options({'whole_probe_timeout_s': 18})
        self.assertEqual(pr.time_budget(options).worst_case_s, 18.0)

    def test_backoff_is_geometric_and_capped(self):
        options = pr.validate_options({'attempts': 5, 'backoff_s': 0.5, 'backoff_factor': 3.0, 'backoff_max_s': 2.0})
        self.assertEqual(pr.plan_attempts(options), (0.5, 1.5, 2.0, 2.0))
        self.assertEqual(pr.plan_attempts(pr.validate_options({'attempts': 1})), ())


class TargetValidationTests(unittest.TestCase):
    def test_statuses_body_and_hash_rules(self):
        target = pr.validate_target({**PUBLIC_TARGET, 'statuses': [200, 204, 200],
                                     'not_contains': 'captcha',
                                     'sha256': 'A' * 64, 'content_type': 'application/json; charset=utf-8'})
        self.assertEqual(target.statuses, (200, 204))
        self.assertEqual(target.sha256, 'a' * 64)
        for bad in ({'statuses': []}, {'statuses': [99]}, {'statuses': ['200']}, {'statuses': [200.0]},
                    {'sha256': 'zz'}, {'content_type': 'text'}, {'min_body_bytes': -1},
                    {'min_body_bytes': 100, 'max_body_bytes': 10}, {'max_redirects': 6},
                    {'dns_mode': 'system'}, {'method': 'TRACE'}, {'url': 'ftp://x.invalid/'},
                    {'url': 'https://user:pw@svc.invalid/'}):
            with self.subTest(bad=bad):
                with self.assertRaises(pr.ProbeError):
                    pr.validate_target({**PUBLIC_TARGET, **bad})

    def test_contains_and_not_contains_cannot_contradict(self):
        with self.assertRaises(pr.ProbeError):
            pr.validate_target({**PUBLIC_TARGET, 'contains': 'ok', 'not_contains': 'not ok'})

    def test_json_assertions_are_validated(self):
        target = pr.validate_target({**PUBLIC_TARGET, 'content_type': 'application/json',
                                     'json_assertions': [{'path': 'data.status', 'op': 'equals', 'value': 'ok'},
                                                         {'path': 'items.0.id', 'op': 'exists'}]})
        self.assertEqual(len(target.json_assertions), 2)
        for bad in ([{'path': 'a', 'op': 'equals'}], [{'path': 'a', 'op': 'nope', 'value': 1}],
                    [{'path': '', 'op': 'exists'}], [{'path': 'a', 'op': 'min', 'value': 'x'}],
                    [{'path': 'a', 'op': 'exists', 'typo': 1}], ['a'], 'a'):
            with self.subTest(bad=bad):
                with self.assertRaises(pr.ProbeError):
                    pr.validate_target({**PUBLIC_TARGET, 'json_assertions': bad})


class OwnTargetTests(unittest.TestCase):
    def own(self, **extra):
        return pr.validate_target({'url': 'https://api.my.invalid/v1', 'own': True, 'scope': 'trusted', **extra})

    def test_post_body_and_api_auth_need_an_own_profile(self):
        for extra in ({'method': 'POST'}, {'body': '{"a":1}'}, {'auth': {'secret_ref': 'api-key'}}):
            with self.subTest(extra=extra):
                with self.assertRaises(pr.ProbeError) as caught:
                    pr.validate_target({**PUBLIC_TARGET, **extra})
                self.assertEqual(caught.exception.code, pr.E_VALIDATION_TARGET_UNSAFE)

    def test_own_profile_accepts_them_by_reference(self):
        target = self.own(method='POST', body='{"a":1}', auth={'secret_ref': 'api-key'},
                          content_type='application/json')
        self.assertEqual(target.method, 'POST')
        self.assertEqual(target.scope, 'trusted')
        self.assertEqual(target.auth.name, 'api-key')
        self.assertEqual(target.body, b'{"a":1}')

    def test_a_secret_value_is_never_accepted(self):
        with self.assertRaises(pr.ProbeError) as caught:
            self.own(auth={'secret_ref': 'api-key', 'value': 'canary-secret-value'})
        self.assertEqual(caught.exception.code, pr.E_VALIDATION_TARGET_UNSAFE)
        with self.assertRaises(pr.ProbeError):
            self.own(auth={'secret_ref': 'has space'})

    def test_credential_headers_are_refused_on_public_targets(self):
        with self.assertRaises(pr.ProbeError) as caught:
            pr.validate_target({**PUBLIC_TARGET, 'headers': {'Authorization': 'Bearer canary'}})
        self.assertEqual(caught.exception.code, pr.E_VALIDATION_TARGET_UNSAFE)
        with self.assertRaises(pr.ProbeError):
            pr.validate_target({**PUBLIC_TARGET, 'headers': {'X-Custom': 'value'}})
        self.assertEqual(pr.validate_target({**PUBLIC_TARGET, 'headers': {'Accept': 'text/plain'}}).headers,
                         (('Accept', 'text/plain'),))

    def test_non_idempotent_method_may_not_be_repeated(self):
        payload = {'mode': 'custom', 'options': {'attempts': 3},
                   'targets': [{'url': 'https://api.my.invalid/v1', 'method': 'POST', 'own': True,
                                'scope': 'trusted', 'body': 'x'}]}
        with self.assertRaises(pr.ProbeError) as caught:
            pr.build_plan(payload)
        self.assertEqual(caught.exception.code, pr.E_VALIDATION_RETRY_UNSAFE)
        self.assertIn('attempts', str(caught.exception))
        self.assertTrue(pr.build_plan({**payload, 'options': {'attempts': 1}}).targets[0].own)

    def test_public_plan_never_carries_a_credential(self):
        plan = pr.build_plan({'mode': 'basic'})
        text = json.dumps(plan.to_public(), ensure_ascii=False)
        self.assertNotIn('password', text.lower())
        self.assertNotIn('token', text.lower())
        self.assertTrue(all(item['auth_configured'] is False for item in plan.to_public()['targets']))


class TlsAndDnsTests(unittest.TestCase):
    def test_a_custom_ca_never_disables_verification(self):
        bundle = ca_bundle_path()
        for ca in (None, bundle):
            with self.subTest(ca=ca):
                context = pr.build_ssl_context(ca)
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_a_broken_ca_is_an_error_not_a_silent_secure_mode(self):
        with self.assertRaises(OSError):
            pr.build_ssl_context('/nonexistent/ca-bundle.pem')

    def test_no_option_reaches_a_verify_switch(self):
        options = pr.validate_options({'max_redirects': 5, 'attempts': 1})
        self.assertFalse(hasattr(options, 'verify'))
        self.assertFalse(hasattr(options, 'insecure'))
        for target in pr.basic_profiles():
            self.assertNotIn('verify', target.to_public())
        self.assertNotIn('verify', pr.build_plan({'mode': 'basic'}).to_public()['options'])

    def test_dns_mode_is_a_closed_set(self):
        self.assertEqual(pr.validate_target({**PUBLIC_TARGET, 'dns_mode': 'local'}).dns_mode, 'local')
        self.assertEqual(pr.validate_target({**PUBLIC_TARGET, 'dns_mode': 'proxy'}).dns_mode, 'proxy')
        with self.assertRaises(pr.ProbeError):
            pr.validate_target({**PUBLIC_TARGET, 'dns_mode': 'os'})


class PlanAssemblyTests(unittest.TestCase):
    def test_unknown_top_level_setting_is_rejected(self):
        with self.assertRaises(pr.ProbeError) as caught:
            pr.build_plan({'mode': 'basic', 'threads': 8})
        self.assertEqual(caught.exception.code, pr.E_VALIDATION_UNKNOWN_FIELD)

    def test_modes_without_targets_reject_them(self):
        with self.assertRaises(pr.ProbeError):
            pr.build_plan({'mode': 'tcp', 'targets': [PUBLIC_TARGET]})
        with self.assertRaises(pr.ProbeError):
            pr.build_plan({'mode': 'basic', 'targets': []})
        with self.assertRaises(pr.ProbeError):
            pr.build_plan({'mode': 'basic', 'targets': [PUBLIC_TARGET] * 21})

    def test_plan_is_content_addressed_and_stable(self):
        first = pr.build_plan({'mode': 'basic'})
        second = pr.build_plan({'mode': 'basic'})
        self.assertEqual(pr.plan_digest(first), pr.plan_digest(second))
        other = pr.build_plan({'mode': 'basic', 'options': {'attempts': 5}})
        self.assertNotEqual(pr.plan_digest(first), pr.plan_digest(other))
        self.assertEqual(len(pr.plan_digest(first)), 20)

    def test_speed_target_is_validated_and_bounded(self):
        speed = pr.validate_speed_target({'url': 'https://cdn.invalid/file', 'max_bytes': 5_000_000})
        self.assertEqual(speed.max_bytes, 5_000_000)
        self.assertIsNone(pr.validate_speed_target({'url': '  '}))
        for bad in ({'url': 'ftp://cdn.invalid/'}, {'url': 'https://x.invalid/', 'max_bytes': 10},
                    {'url': 'https://x.invalid/', 'max_bytes': 10 ** 9},
                    {'url': 'https://x.invalid/', 'headers': {'Authorization': 'Bearer canary'}}):
            with self.subTest(bad=bad):
                with self.assertRaises(pr.ProbeError):
                    pr.validate_speed_target(bad)


class CodeCatalogueTests(unittest.TestCase):
    def test_every_measurement_code_is_listed(self):
        for name in dir(pr):
            if name.isupper() and isinstance(getattr(pr, name), str) and name.endswith(('TIMEOUT', 'MISMATCH')):
                with self.subTest(name=name):
                    self.assertIn(getattr(pr, name), pr.MEASUREMENT_CODES)

    def test_configuration_codes_use_the_contract_form(self):
        for code in (pr.E_VALIDATION_SCHEMA, pr.E_VALIDATION_FIELD, pr.E_VALIDATION_UNKNOWN_FIELD,
                     pr.E_VALIDATION_ANONYMITY_REQUIRED, pr.E_VALIDATION_SCOPE,
                     pr.E_VALIDATION_TARGET_UNSAFE, pr.E_VALIDATION_RETRY_UNSAFE, pr.E_LIMIT_BUDGET):
            with self.subTest(code=code):
                self.assertRegex(code, r'^E_[A-Z]+_[A-Z_]+$')
                self.assertNotIn(code, pr.MEASUREMENT_CODES)

    def test_probe_error_carries_code_and_action(self):
        error = pr.ProbeError(pr.E_VALIDATION_FIELD, 'что-то не так')
        self.assertEqual(error.code, pr.E_VALIDATION_FIELD)
        self.assertIn('что-то не так', str(error))


if __name__ == '__main__':
    unittest.main()

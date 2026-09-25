import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import core

NOW = 1_700_000_000.0
SCOPE = core.Scope(collection_id='public', profile_id='p1', profile_revision=3)
ACCESS = core.Access(access_id='acc-1', access_revision=2)
POLICY = core.Policy(max_age_seconds=600.0, min_success=0.5)


def row(checked_at=NOW - 10, max_age=600.0, **extra):
    """A row that passed a real measurement, stamped as the contract requires."""
    data = {
        'proxy': 'http://11.0.0.1:80',
        'endpoint_id': 'ep-1',
        'collection_id': 'public',
        'profile_id': 'p1',
        'profile_revision': 3,
        'network_id': 'default',
        'access_id': 'acc-1',
        'access_revision': 2,
        'min_target_reliability': 1.0,
        'reliability': 1.0,
        'latency_ms': 120.0,
        'protocol': 'http',
        'country': 'NL',
        'reputation': {'status': 'clean'},
        'anonymity': {'level': 'anonymous'},
    }
    if checked_at is not None:
        data['checked_at'] = checked_at
        data['valid_until'] = checked_at + max_age
    data.update(extra)
    return data


class ModuleContractTests(unittest.TestCase):
    """The leaf rules the integration contract puts on a new module."""

    def test_importing_core_pulls_in_no_other_workbench_module(self):
        code = ('import proxy_workbench.core, sys; '
                "print(sorted(m for m in sys.modules if m.startswith('proxy_workbench')))")
        done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                              cwd=str(Path(__file__).resolve().parents[1]), check=True)
        self.assertEqual(eval(done.stdout.strip()),
                         ['proxy_workbench', 'proxy_workbench.branding', 'proxy_workbench.core'])

    def test_public_api_is_exported(self):
        for name in core.__all__:
            self.assertTrue(hasattr(core, name), name)

    def test_reason_codes_are_unique_and_time_codes_are_known(self):
        self.assertEqual(len(core.REASON_CODES), len(set(core.REASON_CODES)))
        self.assertTrue(core.TIME_REASONS <= set(core.REASON_CODES))


class IdentityTests(unittest.TestCase):
    def test_matching_identity_is_admitted_with_visible_age_and_policy(self):
        result = core.admit(row(), SCOPE, ACCESS, POLICY, NOW)
        self.assertTrue(result.admitted)
        self.assertIsNone(result.reason_code)
        self.assertEqual((result.time_state, result.observation_state), (core.TIME_OK, core.OBSERVATION_OK))
        self.assertAlmostEqual(result.age_seconds, 10.0)
        self.assertEqual(result.max_age_seconds, 600.0)
        self.assertEqual(result.valid_until, NOW - 10 + 600)
        self.assertFalse(result.ttl_backfilled)

    def test_other_collection_profile_network_and_access_are_all_rejected(self):
        cases = {
            'E_SCOPE_COLLECTION': row(collection_id='mine'),
            'E_SCOPE_PROFILE_REVISION': row(profile_revision=4),
            'E_SCOPE_NETWORK': row(network_id='eth0'),
            'E_CONFLICT_ACCESS_REVISION': row(access_revision=3),
        }
        for code, data in cases.items():
            with self.subTest(code=code):
                result = core.admit(data, SCOPE, ACCESS, POLICY, NOW)
                self.assertFalse(result.admitted)
                self.assertEqual(result.reason_code, code)

    def test_new_password_does_not_inherit_the_old_success(self):
        rotated = core.Access(access_id='acc-1', access_revision=3)
        result = core.admit(row(), SCOPE, rotated, POLICY, NOW)
        self.assertEqual(result.reason_code, 'E_CONFLICT_ACCESS_REVISION')
        self.assertEqual(result.detail['access_revision'], 2)
        self.assertEqual(result.detail['required'], 3)

    def test_missing_identity_is_refused_unless_migration_opt_in(self):
        legacy = row()
        for field in ('collection_id', 'access_id', 'access_revision', 'network_id'):
            legacy.pop(field)
        result = core.admit(legacy, SCOPE, ACCESS, POLICY, NOW)
        self.assertFalse(result.admitted)
        self.assertEqual(result.reason_code, 'E_SCOPE_COLLECTION')
        self.assertTrue(result.detail['missing'])
        migrating = core.Policy(max_age_seconds=600.0, allow_missing_identity=True)
        self.assertTrue(core.admit(legacy, SCOPE, ACCESS, migrating, NOW).admitted)

    def test_wrong_identity_still_loses_even_with_migration_opt_in(self):
        migrating = core.Policy(max_age_seconds=600.0, allow_missing_identity=True)
        self.assertEqual(core.admit(row(access_revision=9), SCOPE, ACCESS, migrating, NOW).reason_code,
                         'E_CONFLICT_ACCESS_REVISION')


class TimeStateTests(unittest.TestCase):
    def test_unknown_time_is_never_fresh(self):
        for value in (None, '', 'yesterday', 0, -5, True, float('nan'), float('inf')):
            with self.subTest(value=value):
                data = row(checked_at=NOW - 10)
                data['checked_at'] = value
                result = core.admit(data, SCOPE, ACCESS, POLICY, NOW)
                self.assertFalse(result.admitted)
                self.assertEqual(result.reason_code, 'E_TIME_UNKNOWN')
                self.assertEqual(result.time_state, core.TIME_UNKNOWN)
                self.assertIsNone(result.age_seconds)
                self.assertIsNone(result.checked_at)

    def test_future_timestamp_is_suspicious_not_extra_fresh(self):
        data = row(checked_at=NOW + 7200, max_age=600.0)
        result = core.admit(data, SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual(result.reason_code, 'E_TIME_FUTURE')
        self.assertEqual(result.time_state, core.TIME_FUTURE)
        self.assertAlmostEqual(result.age_seconds, -7200.0)
        self.assertEqual(result.valid_until, NOW + 7200 + 600.0)

    def test_future_tolerance_is_configurable(self):
        data = row(checked_at=NOW + 5, max_age=600.0)
        self.assertEqual(core.admit(data, SCOPE, ACCESS, POLICY, NOW).reason_code, 'E_TIME_FUTURE')
        skewed = core.Policy(max_age_seconds=600.0, future_tolerance_seconds=30.0)
        self.assertTrue(core.admit(data, SCOPE, ACCESS, skewed, NOW).admitted)

    def test_expired_row_keeps_its_stored_deadline(self):
        data = row(checked_at=NOW - 3600, max_age=600.0)
        result = core.admit(data, SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual(result.reason_code, 'E_TIME_TTL_EXPIRED')
        self.assertEqual(result.valid_until, NOW - 3000.0)
        self.assertAlmostEqual(result.age_seconds, 3600.0)

    def test_legacy_row_without_deadline_is_backfilled_once_and_flagged(self):
        data = row(checked_at=NOW - 60)
        data.pop('valid_until')
        result = core.admit(data, SCOPE, ACCESS, POLICY, NOW)
        self.assertTrue(result.admitted)
        self.assertTrue(result.ttl_backfilled)
        self.assertEqual(result.valid_until, NOW - 60 + core.LEGACY_TTL_SECONDS)

    def test_legacy_row_without_deadline_can_be_refused_instead(self):
        data = row(checked_at=NOW - 60)
        data.pop('valid_until')
        strict = core.Policy(max_age_seconds=600.0, backfill_legacy_ttl=False)
        result = core.admit(data, SCOPE, ACCESS, strict, NOW)
        self.assertEqual(result.reason_code, 'E_TIME_TTL_MISSING')
        self.assertFalse(result.admitted)

    def test_backfilled_legacy_row_still_expires_instead_of_living_forever(self):
        data = row(checked_at=NOW - core.LEGACY_TTL_SECONDS - 1)
        data.pop('valid_until')
        result = core.admit(data, SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual(result.reason_code, 'E_TIME_TTL_EXPIRED')

    def test_published_at_never_enters_the_ttl(self):
        data = row(checked_at=NOW - 10, max_age=600.0)
        first = core.admit(data, SCOPE, ACCESS, POLICY, NOW, published_at=NOW)
        second = core.admit(data, SCOPE, ACCESS, POLICY, NOW, published_at=NOW + 10_000)
        self.assertTrue(first.admitted)
        self.assertTrue(second.admitted)
        self.assertEqual(second.published_at, NOW + 10_000)
        self.assertEqual(first.valid_until, second.valid_until)
        self.assertEqual(first.age_seconds, second.age_seconds)
        self.assertEqual(first.as_dict()['admission_reason'], second.as_dict()['admission_reason'])

    def test_max_age_is_a_visible_parameter_not_a_builtin_optimum(self):
        data = row(checked_at=NOW - 10)
        for max_age in (60.0, 300.0, 7200.0):
            with self.subTest(max_age=max_age):
                policy = core.Policy(max_age_seconds=max_age, min_success=0.5)
                result = core.admit(data, SCOPE, ACCESS, policy, NOW)
                # The applied policy is visible, but a deadline that was written at
                # measurement time is not moved by re-reading under another policy.
                self.assertEqual(result.max_age_seconds, max_age)
                self.assertEqual(result.valid_until, NOW - 10 + 600.0)
                self.assertEqual(result.as_dict()['max_age_seconds'], max_age)
        self.assertNotIn(300, (core.DEFAULT_MAX_AGE_SECONDS, core.LEGACY_TTL_SECONDS))

    def test_invalid_policy_is_refused_not_ignored(self):
        cases = (dict(max_age_seconds=0), dict(min_success=1.5), dict(min_anonymity='elite+'),
                 dict(unknown_country='maybe'), dict(required_capabilities=frozenset({'carrier-pigeon'})),
                 dict(max_latency_ms=-1), dict(future_tolerance_seconds=-5))
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(core.AdmissionError) as caught:
                    core.Policy(**case)
                self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')


class CheckOrderTests(unittest.TestCase):
    def test_identity_is_reported_before_time(self):
        data = row(checked_at=None, access_revision=9)
        self.assertEqual(core.admit(data, SCOPE, ACCESS, POLICY, NOW).reason_code,
                         'E_CONFLICT_ACCESS_REVISION')

    def test_missing_measurement_is_reported_before_time(self):
        self.assertEqual(core.admit({}, SCOPE, ACCESS, POLICY, NOW).reason_code,
                         'E_STATE_NO_OBSERVATION')
        self.assertEqual(core.admit(None, SCOPE, ACCESS, POLICY, NOW).reason_code,
                         'E_STATE_NO_OBSERVATION')

    def test_time_is_reported_before_exclusions(self):
        data = row(checked_at=NOW - 10_000, max_age=600.0, country='DE')
        self.assertEqual(core.admit(data, SCOPE, ACCESS, POLICY, NOW).reason_code,
                         'E_TIME_TTL_EXPIRED')

    def test_fresh_failure_is_not_masked_by_an_older_success(self):
        history = {'checks': 7, 'passes': 7, 'first_checked': NOW - 9000, 'last_ok': NOW - 60}
        data = row(error='UNREACHABLE', error_code='UNREACHABLE', min_target_reliability=0.0,
                   reliability=0.0, history=history)
        result = core.admit(data, SCOPE, ACCESS, POLICY, NOW)
        self.assertEqual(result.reason_code, 'E_STATE_MEASUREMENT_FAILED')
        self.assertEqual(result.observation_state, core.OBSERVATION_FAILED)
        self.assertEqual(result.time_state, core.TIME_OK)
        self.assertEqual(result.detail['error'], 'UNREACHABLE')

    def test_zero_reliability_without_error_is_still_a_failed_measurement(self):
        data = row(min_target_reliability=0.0, reliability=0.0)
        self.assertEqual(core.admit(data, SCOPE, ACCESS, POLICY, NOW).reason_code,
                         'E_STATE_MEASUREMENT_FAILED')


class ExclusionAndQualityTests(unittest.TestCase):
    def test_denylist_revokes_immediately(self):
        data = row()
        self.assertTrue(core.admit(data, SCOPE, ACCESS, POLICY, NOW).admitted)
        denied = core.Policy(max_age_seconds=600.0, denied=frozenset({data['proxy']}))
        self.assertEqual(core.admit(data, SCOPE, ACCESS, denied, NOW).reason_code,
                         'E_SCOPE_DENYLIST')

    def test_denylist_callable_is_injected_not_duplicated(self):
        policy = core.Policy(max_age_seconds=600.0, deny_match=lambda proxy: proxy.endswith(':81'))
        self.assertEqual(core.admit(row(proxy='http://11.0.0.1:81'), SCOPE, ACCESS, policy, NOW).reason_code,
                         'E_SCOPE_DENYLIST')

    def test_protocol_country_and_hosting_filters(self):
        cases = (
            (core.Policy(max_age_seconds=600.0, protocol='socks5'), 'E_SCOPE_PROTOCOL'),
            (core.Policy(max_age_seconds=600.0, countries=frozenset({'DE'})), 'E_SCOPE_COUNTRY'),
            (core.Policy(max_age_seconds=600.0, exclude_hosting=True), 'E_SCOPE_HOSTING'),
        )
        for policy, code in cases:
            with self.subTest(code=code):
                data = row(hosting=True) if code == 'E_SCOPE_HOSTING' else row()
                self.assertEqual(core.admit(data, SCOPE, ACCESS, policy, NOW).reason_code, code)

    def test_unknown_country_is_not_treated_as_verified(self):
        data = row(country=None)
        policy = core.Policy(max_age_seconds=600.0, countries=frozenset({'NL'}))
        self.assertEqual(core.admit(data, SCOPE, ACCESS, policy, NOW).reason_code, 'E_SCOPE_COUNTRY')
        lenient = core.Policy(max_age_seconds=600.0, countries=frozenset({'NL'}),
                              unknown_country='include')
        self.assertTrue(core.admit(data, SCOPE, ACCESS, lenient, NOW).admitted)

    def test_resolvers_are_injected_for_rows_without_the_field(self):
        data = row()
        data.pop('protocol')
        data.pop('country')
        policy = core.Policy(max_age_seconds=600.0, protocol='http',
                             countries=frozenset({'NL'}),
                             protocol_of=lambda proxy: proxy.split('://')[0],
                             country_of=lambda proxy: 'NL')
        self.assertTrue(core.admit(data, SCOPE, ACCESS, policy, NOW).admitted)
        data['country'] = 'DE'
        self.assertEqual(core.admit(data, SCOPE, ACCESS, policy, NOW).reason_code, 'E_SCOPE_COUNTRY')

    def test_reputation_strictness(self):
        listed = core.Policy(max_age_seconds=600.0)
        self.assertEqual(core.admit(row(reputation={'status': 'listed'}), SCOPE, ACCESS, listed, NOW).reason_code,
                         'E_STATE_REPUTATION_LISTED')
        self.assertEqual(core.admit(row(reputation={'status': 'local_denied'}), SCOPE, ACCESS, listed, NOW).reason_code,
                         'E_SCOPE_DENYLIST')
        strict = core.Policy(max_age_seconds=600.0, strict=True)
        self.assertEqual(core.admit(row(reputation={'status': 'unknown'}), SCOPE, ACCESS, strict, NOW).reason_code,
                         'E_STATE_REPUTATION_UNKNOWN')
        self.assertEqual(core.admit(row(reputation=None), SCOPE, ACCESS, strict, NOW).reason_code,
                         'E_STATE_REPUTATION_UNKNOWN')
        self.assertTrue(core.admit(row(reputation={'status': 'unknown'}), SCOPE, ACCESS, listed, NOW).admitted)

    def test_anonymity_latency_and_reliability(self):
        cases = (
            (core.Policy(max_age_seconds=600.0, min_anonymity='elite'), row(), 'E_STATE_ANONYMITY'),
            (core.Policy(max_age_seconds=600.0, max_latency_ms=50.0), row(), 'E_STATE_LATENCY'),
            (core.Policy(max_age_seconds=600.0, max_latency_ms=200.0), row(latency_ms=None), 'E_STATE_LATENCY'),
            (core.Policy(max_age_seconds=600.0, min_success=0.9),
             row(min_target_reliability=0.75, reliability=0.75), 'E_STATE_MIN_SUCCESS'),
            (core.Policy(max_age_seconds=600.0, min_success=0.9), row(min_target_reliability=None), 'E_STATE_MIN_SUCCESS'),
        )
        for policy, data, code in cases:
            with self.subTest(code=code):
                self.assertEqual(core.admit(data, SCOPE, ACCESS, policy, NOW).reason_code, code)

    def test_anonymity_any_does_not_require_a_probe(self):
        result = core.admit(row(anonymity=None), SCOPE, ACCESS, POLICY, NOW)
        self.assertTrue(result.admitted)

    def test_capabilities_are_required_explicitly(self):
        data = row(exit_ip='198.51.100.7', speed={'state': 'ok', 'mbps': 12.5})
        policy = core.Policy(max_age_seconds=600.0,
                             required_capabilities=frozenset({'https', 'udp'}))
        result = core.admit(data, SCOPE, ACCESS, policy, NOW)
        self.assertEqual(result.reason_code, 'E_SCOPE_CAPABILITY')
        self.assertEqual(result.detail['missing'], ['https', 'udp'])
        ok = core.Policy(max_age_seconds=600.0,
                         required_capabilities=frozenset({'http', 'tcp', 'exit_ip', 'speed'}))
        self.assertTrue(core.admit(data, SCOPE, ACCESS, ok, NOW).admitted)

    def test_capabilities_are_not_inferred_from_the_address(self):
        derived = core.capabilities_of(row())
        self.assertIn('http', derived)
        self.assertNotIn('https', derived)
        self.assertNotIn('udp', derived)
        self.assertNotIn('speed', derived)
        unreachable = core.capabilities_of(row(error='UNREACHABLE'))
        self.assertNotIn('tcp', unreachable)

    def test_every_reason_code_is_documented(self):
        for code in core.REASON_CODES:
            self.assertTrue(code.startswith('E_'))
        self.assertIn('E_TIME_CLOCK_ROLLBACK', core.REASON_CODES)


if __name__ == '__main__':
    unittest.main()

"""The zero-result funnel, its stages and the device/target control (F10).

Every scenario here is local: rows are plain dicts in the shape the engine
stores, sources are the reports `collect()` already returns, and the control
check is driven by an injected checker.  No test in this file opens a socket.
"""
import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import diagnostics as d

NOW = 1_700_000_000.0


def row(proxy='http://10.0.0.1:8080', *, ok=True, error=None, status=200, age=60, lifetime=3600,
        samples=None, **extra):
    """One stored result row, shaped like `results.payload`."""
    payload = dict(proxy=proxy, checked_at=NOW - age, valid_until=NOW - age + lifetime,
                   reliability=1.0 if ok else 0.0, min_target_reliability=1.0 if ok else 0.0,
                   successes=1 if ok else 0, requests=1)
    if samples is not None:
        payload['samples'] = samples
    elif error is None and ok:
        payload['samples'] = [dict(ok=True, ms=10, status=200, bytes=8, error=None, target=0, attempt=1)]
    else:
        payload['samples'] = []
    if error is not None:
        payload['error'] = error
    payload.update(extra)
    return payload


def status(**overrides):
    base = dict(state='complete', stop_reason='complete', candidates=10, scope_candidates=10,
                checked=10, pending=0, passed=0, exported=0, local_filtered=0)
    base.update(overrides)
    return base


def source_report(index=1, rows=0, error=None, complete=True, **extra):
    report = dict(source=index, rows=rows, invalid=0, blocked=0, pages=1, attempts=1,
                  complete=complete, error=error, format='text')
    report.update(extra)
    return report


class FunnelStageTests(unittest.TestCase):
    def test_a_loss_is_counted_at_the_stage_where_it_happened(self):
        rows = [
            row('http://10.0.0.1:1', ok=False, error='UNREACHABLE'),
            row('http://10.0.0.2:2', ok=False, samples=[dict(ok=False, ms=5, status=407, error='HTTP_407',
                                                              target=0, attempt=1)]),
            row('http://10.0.0.3:3', ok=False, samples=[dict(ok=False, ms=5, status=200, error='CONTENT_MISMATCH',
                                                              target=0, attempt=1)]),
            row('http://10.0.0.4:4'),
        ]
        funnel = d.build_funnel(rows, status=status(exported=1, passed=1, checked=4), now=NOW)
        self.assertEqual(funnel.stage_counters('scope').entered, 4)
        self.assertEqual(funnel.stage_counters('tcp').reasons['UNREACHABLE'], 1)
        self.assertEqual(funnel.stage_counters('auth').reasons['AUTH_FAILED'], 1)
        self.assertEqual(funnel.stage_counters('assertion').reasons['CONTENT_MISMATCH'], 1)
        self.assertEqual(funnel.terminal['time_ok'], 1)
        self.assertEqual(funnel.lost_total(), 3)

    def test_dns_and_tcp_are_not_the_same_funnel_stage(self):
        rows = [row('http://10.0.0.1:1', ok=False, samples=[dict(ok=False, ms=1, status=None,
                                                                 error='ConnectError',
                                                                 detail='nodename nor servname provided',
                                                                 target=0, attempt=1)])]
        funnel = d.build_funnel(rows, now=NOW)
        # A stored sample carries only the class name, so it lands on tcp; the
        # live exception path promotes the same failure to dns.
        self.assertEqual(funnel.stage_counters('tcp').reasons['UNREACHABLE'], 1)
        self.assertEqual(d.classification(httpx.ConnectError('nodename nor servname provided'))[0], 'dns')

    def test_download_and_parser_failures_are_separated(self):
        reports = [source_report(1, rows=0, error='SOURCE_TOO_LARGE', complete=False),
                   source_report(2, rows=0, error='SOURCE_INVALID_UTF8', complete=False),
                   source_report(3, rows=0, error=None, complete=True)]
        funnel = d.build_funnel(sources=reports, status=status(candidates=0), now=NOW)
        self.assertEqual(funnel.stage_counters('download').reasons['SOURCE_TOO_LARGE'], 1)
        self.assertEqual(funnel.stage_counters('parser').reasons['SOURCE_INVALID_UTF8'], 1)
        self.assertEqual(funnel.stage_counters('parser').reasons['SOURCE_EMPTY'], 1)
        self.assertEqual(funnel.source_entry(), 3)

    def test_a_blocked_row_leaves_before_any_measurement(self):
        denied = row('http://10.0.0.9:9', ok=False, samples=[],
                     reputation={'status': 'local_denied', 'local_rule': 'my-denylist', 'dnsbl': []})
        listed = row('http://10.0.0.8:8', ok=False, samples=[],
                     reputation={'status': 'listed', 'local_rule': None,
                                 'dnsbl': [{'zone': 'zen.example', 'status': 'listed'}]})
        funnel = d.build_funnel([denied, listed], now=NOW)
        self.assertEqual(funnel.stage_counters('scope').reasons['SCOPE_DENYLIST'], 1)
        self.assertEqual(funnel.stage_counters('scope').reasons['SCOPE_REPUTATION_LISTED'], 1)
        self.assertEqual(funnel.stage_counters('tcp').lost, 0)

    def test_rate_limit_and_assertion_do_not_collapse_into_target(self):
        rows = [row('http://10.0.0.1:1', ok=False, samples=[dict(ok=False, ms=1, status=429, error='HTTP_429',
                                                                  target=0, attempt=1)]),
                row('http://10.0.0.2:2', ok=False, samples=[dict(ok=False, ms=1, status=200,
                                                                  error='HASH_MISMATCH', target=0, attempt=1)])]
        funnel = d.build_funnel(rows, now=NOW)
        self.assertEqual(funnel.stage_counters('rate_limit').reasons['RATE_LIMITED'], 1)
        self.assertEqual(funnel.stage_counters('assertion').reasons['HASH_MISMATCH'], 1)
        self.assertEqual(funnel.stage_counters('target').lost, 0)

    def test_the_funnel_is_json_serializable_for_events_and_the_bundle(self):
        funnel = d.build_funnel([row(), row('http://10.0.0.2:2', ok=False, error='UNREACHABLE')],
                                status=status(), now=NOW)
        restored = json.loads(json.dumps(funnel.to_dict()))
        self.assertEqual(restored['stages']['tcp']['reasons'], {'UNREACHABLE': 1})
        self.assertEqual(restored['totals']['exported'], 0)


class UnmeasuredAndVerdictTests(unittest.TestCase):
    def test_a_row_without_samples_is_not_counted_as_a_measurement(self):
        empty = row()
        empty['samples'] = []
        empty['requests'] = 0
        empty.pop('error', None)
        funnel = d.build_funnel([empty], status=status(exported=0, passed=0, checked=1), now=NOW)
        self.assertEqual(funnel.terminal[d.OBSERVATION_MISSING], 1)
        self.assertEqual(funnel.terminal['time_ok'], 0)
        self.assertEqual(funnel.lost_total(), 0)

    def test_an_unknown_verdict_reports_the_code_the_engine_recorded(self):
        screened = row(reputation={'status': 'unknown', 'error': 'NO_DNSBL_ZONES', 'local_rule': None,
                                   'dnsbl': []})
        funnel = d.build_funnel([screened], status=status(exported=0, passed=0, checked=1), now=NOW)
        self.assertEqual(funnel.verdicts['NO_DNSBL_ZONES'], 1)
        self.assertEqual(funnel.terminal['unknown_verdict'], 1)
        self.assertEqual(d.explain_zero(funnel, lang='en').code, 'NO_DNSBL_ZONES')

    def test_a_clean_verdict_is_not_reported_as_unknown(self):
        clean = row(reputation={'status': 'clean', 'local_rule': None, 'dnsbl': []})
        funnel = d.build_funnel([clean], status=status(exported=1, passed=1, checked=1), now=NOW)
        self.assertEqual(funnel.terminal['unknown_verdict'], 0)
        self.assertEqual(dict(funnel.verdicts), {})


class FreshnessTests(unittest.TestCase):
    def test_each_time_state_is_reported_on_its_own(self):
        fresh = row(lifetime=3600)
        expired = row(lifetime=10)
        unknown = row(checked_at=None, valid_until=None)
        unknown.pop('checked_at', None)
        future = row(age=-3600)
        self.assertEqual(d.data_state(fresh, NOW), 'time_ok')
        self.assertEqual(d.data_state(expired, NOW), 'time_expired')
        self.assertEqual(d.data_state(unknown, NOW), 'time_unknown')
        self.assertEqual(d.data_state(future, NOW), 'time_future')

    def test_a_row_without_a_recorded_lifetime_is_not_fresh_forever(self):
        legacy = row()
        legacy.pop('valid_until')
        self.assertEqual(d.data_state(legacy, NOW), 'time_ok')
        self.assertEqual(d.data_state(legacy, NOW + d.LEGACY_MAX_AGE_SECONDS + 1), 'time_expired')

    def test_a_profile_may_refuse_the_legacy_backfill(self):
        legacy = row()
        legacy.pop('valid_until')
        self.assertEqual(d.data_state(legacy, NOW, backfill_legacy_ttl=False), 'time_ttl_missing')
        funnel = d.build_funnel([legacy], status=status(exported=0, passed=0, checked=1),
                                now=NOW, backfill_legacy_ttl=False)
        self.assertEqual(funnel.terminal['time_ttl_missing'], 1)
        self.assertEqual(funnel.stage_counters('freshness').reasons['E_TIME_TTL_MISSING'], 1)

    def test_the_time_state_names_are_the_admission_contract_names(self):
        try:
            from proxy_workbench import core
        except ImportError:
            self.skipTest('core.py is not on this revision yet')
        self.assertEqual(set(d.TIME_STATES), set(core.TIME_STATES))
        self.assertEqual(d.LEGACY_MAX_AGE_SECONDS, core.LEGACY_TTL_SECONDS)

    def test_the_funnel_and_the_admission_contract_agree_on_the_time_state(self):
        try:
            from proxy_workbench import core
        except ImportError:
            self.skipTest('core.py is not on this revision yet')
        policy = core.Policy(future_tolerance_seconds=d.CLOCK_SKEW_SECONDS)
        legacy = row()
        legacy.pop('valid_until')
        no_time = row()
        no_time.pop('checked_at')
        cases = [
            (row(), {}),
            (row(lifetime=10), {}),
            (row(age=-3600), {}),
            (no_time, {}),
            (legacy, {}),
            (row(), dict(clock=core.ClockState(high_water=NOW))),
        ]
        for candidate, options in cases:
            with self.subTest(row=candidate.get('proxy'), valid_until=candidate.get('valid_until')):
                expected = core.time_state_of(candidate, NOW, policy, **options)['state']
                self.assertEqual(d.data_state(candidate, NOW,
                                              last_seen=({'http://10.0.0.1:8080': NOW}
                                                        if 'clock' in options else None)),
                                 expected)

    def test_clock_rollback_is_separate_from_a_future_timestamp(self):
        rolled = d.data_state(row(), NOW - 60, last_seen={'http://10.0.0.1:8080': NOW})
        self.assertEqual(rolled, 'clock_rollback')
        funnel = d.build_funnel([row()], now=NOW - 60, last_seen={'http://10.0.0.1:8080': NOW})
        self.assertEqual(funnel.terminal['clock_rollback'], 1)
        self.assertEqual(funnel.stage_counters('freshness').reasons['CLOCK_ROLLBACK'], 1)

    def test_expired_rows_are_not_mistaken_for_measurements(self):
        funnel = d.build_funnel([row('http://10.0.0.1:1', lifetime=10), row('http://10.0.0.2:2', lifetime=10)],
                                status=status(exported=0, passed=0), now=NOW)
        self.assertEqual(funnel.terminal['time_expired'], 2)
        self.assertEqual(funnel.terminal['time_ok'], 0)


class ZeroResultTests(unittest.TestCase):
    """F10: the user understands why there are zero results and fixes it."""

    def test_a_total_upstream_loss_names_the_stage_and_the_action(self):
        rows = [row(f'http://10.0.0.{index}:80', ok=False, error='UNREACHABLE') for index in range(1, 9)]
        funnel = d.build_funnel(rows, status=status(exported=0, passed=0, checked=8), now=NOW)
        zero = d.explain_zero(funnel, lang='ru')
        self.assertIsNotNone(zero)
        self.assertEqual(zero.code, 'UNREACHABLE')
        self.assertEqual(zero.stage, 'tcp')
        self.assertTrue(zero.action)
        self.assertEqual(zero.params['count'], 8)
        text = zero.render('ru')
        self.assertIn('UNREACHABLE', text)
        self.assertIn('Действие:', text)
        self.assertIn('exported=0', text)
        # No traceback, no exception class: a code, counters and an instruction.
        self.assertNotIn('Traceback', text)
        self.assertNotIn('Error', text)

    def test_the_same_run_with_results_has_nothing_to_explain(self):
        rows = [row('http://10.0.0.1:80'), row('http://10.0.0.2:80', ok=False, error='UNREACHABLE')]
        funnel = d.build_funnel(rows, status=status(exported=1, passed=1, checked=2), now=NOW)
        self.assertFalse(funnel.zero_result())
        self.assertIsNone(d.explain_zero(funnel))
        self.assertIsNone(d.funnel_explanation(funnel))

    def test_an_unreachable_source_is_not_a_parser_problem(self):
        reports = [source_report(1, error='HTTP_404', complete=False), source_report(2, error='HTTP_404', complete=False)]
        funnel = d.build_funnel(sources=reports, status=status(candidates=0), now=NOW)
        zero = d.explain_zero(funnel, lang='en')
        self.assertEqual(zero.code, 'SOURCE_UNAVAILABLE')
        self.assertEqual(zero.params['count'], 2)
        self.assertIn('HTTP_404', zero.related)

    def test_a_source_that_parsed_to_nothing_says_so(self):
        reports = [source_report(1, rows=0, error=None)]
        funnel = d.build_funnel(sources=reports, status=status(candidates=0), now=NOW)
        self.assertEqual(d.explain_zero(funnel, lang='en').code, 'SOURCE_EMPTY')

    def test_no_candidates_at_all_is_distinct_from_an_empty_scope(self):
        empty_scope = d.build_funnel([], status=status(candidates=40, scope_candidates=0, checked=0), now=NOW)
        self.assertEqual(d.explain_zero(empty_scope, lang='en').code, 'SCOPE_EMPTY')
        nothing = d.build_funnel([], status=status(candidates=0, scope_candidates=0, checked=0), now=NOW)
        self.assertEqual(d.explain_zero(nothing, lang='en').code, 'NO_SOURCES')

    def test_an_exhausted_budget_is_reported_as_a_budget(self):
        funnel = d.build_funnel([row()], status=status(exported=0, passed=0), now=NOW)
        funnel.drop('budget', 'BUDGET_EXHAUSTED', 4)
        zero = d.explain_zero(funnel, lang='en')
        self.assertEqual(zero.code, 'E_LIMIT_BUDGET')
        self.assertEqual(zero.params['limit'], 4)

    def test_expired_and_unknown_rows_are_different_causes(self):
        expired = d.build_funnel([row(lifetime=10)], status=status(exported=0, passed=0), now=NOW)
        self.assertEqual(d.explain_zero(expired, lang='en').code, 'E_TIME_TTL_EXPIRED')
        unknown = row()
        unknown['checked_at'] = None
        no_time = d.build_funnel([unknown], status=status(exported=0, passed=0), now=NOW)
        self.assertEqual(d.explain_zero(no_time, lang='en').code, 'E_TIME_UNKNOWN')

    def test_passing_checks_and_an_empty_export_are_a_filter_problem(self):
        rows = [row(f'http://10.0.0.{index}:80') for index in range(1, 6)]
        filtered = d.build_funnel(rows, status=status(exported=0, passed=5, checked=5), now=NOW)
        zero = d.explain_zero(filtered, lang='en')
        self.assertEqual(zero.code, 'SCOPE_FILTERED_ALL')
        self.assertEqual(zero.params['count'], 5)
        denied = d.build_funnel(rows, status=status(exported=0, passed=5, checked=5, local_filtered=5), now=NOW)
        self.assertEqual(d.explain_zero(denied, lang='en').code, 'SCOPE_DENYLIST')

    def test_a_failed_job_is_reported_before_any_upstream_guess(self):
        funnel = d.build_funnel([], status=status(state='error', stop_reason='error', candidates=3,
                                                 scope_candidates=3, checked=0), now=NOW)
        self.assertEqual(d.explain_zero(funnel, lang='en').code, 'JOB_FAILED')

    def test_the_related_codes_widen_the_help_lookups(self):
        rows = [row('http://10.0.0.1:80', ok=False, error='UNREACHABLE'),
                row('http://10.0.0.2:80', ok=False, error='UNREACHABLE'),
                row('http://10.0.0.3:80', ok=False, error='TLS_ERROR')]
        funnel = d.build_funnel(rows, status=status(exported=0, passed=0, checked=3), now=NOW)
        zero = d.explain_zero(funnel, lang='en')
        self.assertIn('TLS_ERROR', zero.related)
        for code in zero.related:
            self.assertTrue(d.help_for(code).action)


class ControlTests(unittest.TestCase):
    def test_a_zero_budget_means_the_network_is_never_touched(self):
        calls = []
        verdict = d.check_control(limits=d.ControlLimits(total_requests=0),
                                  checker=lambda probe, limits: calls.append(probe) or {'ok': True})
        self.assertEqual(verdict.state, 'skipped')
        self.assertEqual(verdict.checked, 0)
        self.assertEqual(calls, [])
        self.assertTrue(verdict.network_ok)

    def test_the_number_of_outbound_attempts_is_capped(self):
        calls = []

        def checker(probe, limits):
            calls.append(probe.kind)
            return {'ok': True}

        verdict = d.check_control(targets=[{'url': 'https://one.invalid/'}, {'url': 'https://two.invalid/'},
                                           {'url': 'https://three.invalid/'}],
                                  limits=d.ControlLimits(total_requests=2, max_targets=1), checker=checker)
        self.assertEqual(len(calls), 2)
        self.assertEqual(verdict.checked, 2)
        self.assertEqual(verdict.limit, 2)
        self.assertEqual(calls, ['resolve', 'connect'])

    def test_a_broken_device_is_reported_and_does_not_blame_the_proxies(self):
        def checker(probe, limits):
            if probe.kind == 'connect':
                return {'ok': False, 'code': 'DEVICE_NETWORK_DOWN'}
            return {'ok': True}

        verdict = d.check_control(limits=d.ControlLimits(), checker=checker)
        self.assertEqual(verdict.state, 'device_network')
        self.assertEqual(verdict.code, 'DEVICE_NETWORK_DOWN')
        self.assertEqual(verdict.blame('tcp'), 'device')
        self.assertEqual(verdict.blame('dns'), 'device')
        self.assertEqual(verdict.blame('target'), 'proxy')
        self.assertFalse(verdict.network_ok)

    def test_a_dead_target_is_reported_separately_from_a_dead_device(self):
        def checker(probe, limits):
            if probe.kind == 'http':
                return {'ok': False, 'code': 'HTTP_503'}
            return {'ok': True}

        verdict = d.check_control(targets=[{'url': 'https://target.invalid/'}], checker=checker)
        self.assertEqual(verdict.state, 'target_outage')
        self.assertEqual(verdict.blame('target'), 'target')
        self.assertEqual(verdict.blame('tcp'), 'proxy')

    def test_a_global_outage_does_not_taint_every_address(self):
        verdict = d.ControlVerdict(state='device_network', checked=3, limit=3, code='DEVICE_NETWORK_DOWN')
        rows = [row(f'http://10.0.0.{index}:80', ok=False,
                    samples=[dict(ok=False, ms=1, status=None, error='ConnectError', target=0, attempt=1)])
                for index in range(1, 6)]
        funnel = d.build_funnel(rows, status=status(exported=0, passed=0, checked=5),
                                control=verdict, now=NOW)
        zero = d.explain_zero(funnel, lang='en')
        self.assertEqual(zero.code, 'DEVICE_NETWORK_DOWN')
        self.assertEqual(zero.params['attributed'], 5)
        self.assertEqual(zero.params['unattributed'], 0)
        self.assertEqual(funnel.attribution['device'], 5)
        self.assertEqual(funnel.attribution['proxy'], 0)

    def test_a_target_outage_does_not_taint_every_address(self):
        verdict = d.ControlVerdict(state='target_outage', checked=3, limit=3, code='TARGET_OUTAGE')
        rows = [row(f'http://10.0.0.{index}:80', ok=False,
                    samples=[dict(ok=False, ms=1, status=503, error='HTTP_503', target=0, attempt=1)])
                for index in range(1, 4)]
        funnel = d.build_funnel(rows, status=status(exported=0, passed=0, checked=3),
                                control=verdict, now=NOW)
        self.assertEqual(d.explain_zero(funnel, lang='en').code, 'TARGET_OUTAGE')
        self.assertEqual(funnel.attribution['target'], 3)
        self.assertEqual(funnel.attribution['proxy'], 0)

    def test_a_proxy_fault_stays_the_proxys_fault_when_the_device_is_fine(self):
        verdict = d.ControlVerdict(state='ok', checked=3, limit=3)
        rows = [row('http://10.0.0.1:80', ok=False, error='UNREACHABLE')]
        funnel = d.build_funnel(rows, status=status(exported=0, passed=0, checked=1), control=verdict, now=NOW)
        self.assertEqual(funnel.attribution['proxy'], 1)
        self.assertEqual(d.explain_zero(funnel, lang='en').code, 'UNREACHABLE')

    def test_a_raising_checker_does_not_break_the_caller(self):
        def checker(probe, limits):
            raise httpx.ConnectError('the checker itself is broken')

        verdict = d.check_control(limits=d.ControlLimits(), checker=checker)
        self.assertIn(verdict.state, ('device_network', 'unknown'))
        self.assertEqual(verdict.checked, 2)

    def test_impossible_limits_are_refused(self):
        with self.assertRaises(ValueError):
            d.ControlLimits(total_requests=-1).validate()
        with self.assertRaises(ValueError):
            d.ControlLimits(connect_timeout_s=0).validate()
        with self.assertRaises(ValueError):
            d.ControlLimits(max_targets=-2).validate()

    def test_a_probe_outcome_never_carries_a_body_into_the_verdict(self):
        verdict = d.check_control(limits=d.ControlLimits(),
                                  checker=lambda probe, limits: {'ok': True, 'body': 'secret page'})
        self.assertNotIn('body', json.dumps(verdict.to_dict()))


if __name__ == '__main__':
    unittest.main()

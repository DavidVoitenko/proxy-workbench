"""F01: honest check modes, the evidence ladder and target-vs-proxy blame."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import probes as pr


def transport(*responses):
    """A transport that answers with the given ProbeResponse objects in order."""
    queue = list(responses)

    async def send(request, *, options):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item(request) if callable(item) else item

    calls = []

    async def spy(request, *, options):
        calls.append(request)
        return await send(request, options=options)

    return SimpleNamespace(send=spy, calls=calls)


def good(body=(b'<html>Example Domain</html>' + b' ' * 64), status=200, content_type='text/html',
         url='https://example.com/'):
    return pr.ProbeResponse(status=status, headers=(('content-type', content_type),), body=body, url=url,
                            ttfb_ms=12.0, transfer_ms=8.0, total_ms=20.0, connect_ms=5.0, handshake_ms=4.0)


def refused(stage='tcp', code=pr.UNREACHABLE):
    return pr.ProbeResponse(code=code, stage=stage)


class ModeTests(unittest.TestCase):
    def test_unknown_mode_is_rejected(self):
        for name in ('', '   ', 'turbo', None, 7):
            with self.subTest(name=name), self.assertRaises(pr.ProbeError) as caught:
                pr.resolve_mode(name)
            self.assertEqual(caught.exception.code, pr.E_VALIDATION_FIELD)

    def test_aliases_normalize_to_the_same_mode(self):
        pairs = (('collect-only', pr.COLLECT_ONLY), ('collect', pr.COLLECT_ONLY),
                 ('reachability', pr.TCP), ('tcp', pr.TCP), ('protocol', pr.HANDSHAKE),
                 ('basic', pr.BASIC), ('https', pr.BASIC), ('service', pr.SERVICES),
                 ('custom', pr.CUSTOM))
        for alias, expected in pairs:
            with self.subTest(alias=alias):
                self.assertEqual(pr.resolve_mode(alias).name, expected)
        self.assertEqual(pr.resolve_mode('re-check', base='basic').name, pr.RECHECK)
        self.assertEqual(pr.resolve_mode('watch', base='tcp').base, pr.TCP)

    def test_collect_only_has_no_network_and_no_verdict(self):
        mode = pr.resolve_mode('collect-only')
        self.assertFalse(mode.network)
        self.assertFalse(mode.is_measurement)
        self.assertEqual(mode.evidence, 'collected')
        self.assertEqual(mode.evidence, 'collected' if pr.evidence_at_least(mode.evidence, 'collected') else '')

    def test_evidence_ladder_is_ordered(self):
        self.assertTrue(pr.evidence_at_least('transfer_ok', 'handshake_ok'))
        self.assertTrue(pr.evidence_at_least('tcp_open', 'collected'))
        self.assertFalse(pr.evidence_at_least('handshake_ok', 'transfer_ok'))
        self.assertFalse(pr.evidence_at_least('none', 'collected'))

    def test_monitoring_is_a_job_mode_not_a_new_verdict(self):
        mode = pr.resolve_mode('monitor', base='basic')
        self.assertTrue(mode.monitoring)
        self.assertEqual(mode.name, 'basic')
        self.assertEqual(mode.evidence, 'transfer_ok')
        self.assertEqual(pr.resolve_mode('monitor', base='tcp').evidence, 'tcp_open')
        with self.assertRaises(pr.ProbeError) as caught:
            pr.resolve_mode('monitor')
        self.assertEqual(caught.exception.code, pr.E_VALIDATION_FIELD)
        with self.assertRaises(pr.ProbeError):
            pr.resolve_mode('basic', monitoring=True)

    def test_derived_modes_do_not_mint_a_new_measurement_identity(self):
        basic = pr.build_plan({'mode': 'basic'})
        self.assertEqual(pr.plan_digest(pr.build_plan({'mode': 'monitor', 'base_mode': 'basic'})),
                         pr.plan_digest(basic))
        self.assertEqual(pr.plan_digest(pr.build_plan({'mode': 'recheck', 'base_mode': 'basic'})),
                         pr.plan_digest(basic))
        self.assertNotEqual(pr.plan_digest(pr.build_plan({'mode': 'monitor', 'base_mode': 'tcp'})),
                            pr.plan_digest(basic))

    def test_recheck_inherits_the_measured_mode(self):
        mode = pr.resolve_mode('recheck', base='handshake')
        self.assertEqual(mode.name, pr.RECHECK)
        self.assertEqual(mode.base, 'handshake')
        self.assertEqual(mode.evidence, 'handshake_ok')
        self.assertFalse(mode.monitoring)
        with self.assertRaises(pr.ProbeError):
            pr.resolve_mode('recheck', base='collect-only')


class BasicProbeTests(unittest.TestCase):
    def test_basic_probes_are_small_and_honest(self):
        profiles = pr.basic_profiles()
        self.assertTrue(1 <= len(profiles) <= pr.MAX_BASIC_PROBES)
        for profile in profiles:
            with self.subTest(profile=profile.id):
                self.assertTrue(profile.statuses)
                self.assertTrue(profile.contains, 'встроенная probe обязана проверять содержимое')
                self.assertTrue(profile.content_type)
                self.assertLessEqual(profile.max_body_bytes, 65_536)
                self.assertGreaterEqual(profile.min_body_bytes, 1)
                self.assertFalse(profile.own)
                self.assertIsNone(profile.auth)
        self.assertEqual(len({profile.url for profile in profiles}), len(profiles))

    def test_manifest_does_not_promise_verified_definitions(self):
        manifest = pr.probes_manifest()
        self.assertEqual(manifest['verified_count'], 0)
        for item in manifest['probes']:
            with self.subTest(probe=item['id']):
                self.assertFalse(item['verified'])
                self.assertIsNone(item['verified_at'])
        self.assertLessEqual(manifest['max_body_bytes_per_probe'], 65_536)

    def test_fallback_chain_is_bounded(self):
        profiles = pr.basic_profiles()
        self.assertEqual(len(pr.fallback_chain(profiles, limit=1)), 2)
        self.assertEqual(len(pr.fallback_chain(profiles, limit=2)), len(profiles))
        with self.assertRaises(pr.ProbeError):
            pr.fallback_chain(profiles * 2)
        with self.assertRaises(pr.ProbeError):
            pr.fallback_chain(())


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_mode_never_claims_a_transfer(self):
        plan = pr.build_plan({'mode': 'tcp'})
        outcome = await pr.run_plan(plan, transport(pr.ProbeResponse(status=None)), endpoint='http://11.0.0.1:8080')
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.evidence, 'tcp_open')
        self.assertFalse(pr.is_working(outcome))
        self.assertTrue(pr.evidence_at_least(outcome.evidence, 'tcp_open'))
        self.assertFalse(pr.evidence_at_least(outcome.evidence, 'transfer_ok'))

    async def test_handshake_mode_evidence_is_handshake(self):
        plan = pr.build_plan({'mode': 'handshake'})
        outcome = await pr.run_plan(plan, transport(pr.ProbeResponse()), endpoint='socks5://11.0.0.1:1080')
        self.assertEqual(outcome.evidence, 'handshake_ok')
        self.assertFalse(pr.is_working(outcome))

    async def test_basic_transfer_earns_transfer_ok(self):
        plan = pr.build_plan({'mode': 'basic'})
        body = b'<html>Example Domain</html>' + b' ' * 64
        outcome = await pr.run_plan(plan, transport(good(body)), endpoint='http://11.0.0.1:8080')
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.evidence, 'transfer_ok')
        self.assertTrue(pr.is_working(outcome))
        self.assertEqual(outcome.targets[0].bytes, len(body))

    async def test_failed_transfer_earns_nothing(self):
        plan = pr.build_plan({'mode': 'basic'})
        outcome = await pr.run_plan(plan, transport(good(b'x' * 200)), endpoint='http://11.0.0.1:8080')
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.evidence, 'none')
        self.assertEqual(outcome.code, pr.CONTENT_MISMATCH)

    async def test_collect_only_plan_refuses_to_measure(self):
        plan = pr.build_plan({'mode': 'collect-only'})
        self.assertEqual(plan.targets, ())
        with self.assertRaises(pr.ProbeError) as caught:
            await pr.run_plan(plan, transport(good()))
        self.assertEqual(caught.exception.code, pr.E_VALIDATION_FIELD)
        self.assertIn('collect_only', str(caught.exception))

    async def test_monitoring_plan_reports_the_base_mode(self):
        plan = pr.build_plan({'mode': 'monitor', 'base_mode': 'basic'})
        outcome = await pr.run_plan(plan, transport(good()), endpoint='http://11.0.0.1:8080')
        self.assertTrue(outcome.monitoring)
        self.assertEqual(outcome.mode, 'basic')
        self.assertEqual(outcome.evidence, 'transfer_ok')
        self.assertTrue(outcome.to_public()['monitoring'])


class TargetBlameTests(unittest.IsolatedAsyncioTestCase):
    def options(self):
        return pr.validate_options({'attempts': 1})

    async def outcome_for(self, response):
        plan = pr.build_plan({'mode': 'basic', 'options': {'attempts': 1}})
        return await pr.run_plan(plan, transport(response), endpoint='http://11.0.0.1:8080')

    async def test_two_proxies_failing_the_same_way_blame_the_target(self):
        service_down = pr.ProbeResponse(status=503, headers=(('content-type', 'text/html'),), body=b'sorry')
        outcomes = [await self.outcome_for(service_down) for _ in range(2)]
        summary = pr.summarize_basic(outcomes)
        self.assertEqual(summary.working, 0)
        self.assertEqual(summary.code, pr.TARGET_UNAVAILABLE)
        self.assertTrue(summary.target_unavailable)
        self.assertIn(pr.TARGET_UNAVAILABLE, summary.to_public()['code'])

    async def test_one_proxy_failing_at_the_target_is_not_a_target_outage(self):
        outcomes = [await self.outcome_for(pr.ProbeResponse(status=503, body=b'sorry'))]
        self.assertIsNone(pr.summarize_basic(outcomes).code)

    async def test_dead_proxies_are_blamed_on_the_proxies(self):
        outcomes = [await self.outcome_for(pr.ProbeResponse(code=pr.UNREACHABLE, stage='tcp'))
                    for _ in range(3)]
        summary = pr.summarize_basic(outcomes)
        self.assertEqual(summary.code, pr.UNREACHABLE)
        self.assertFalse(summary.target_unavailable)

    async def test_mixed_failures_give_no_global_excuse(self):
        dead = await self.outcome_for(pr.ProbeResponse(code=pr.UNREACHABLE, stage='tcp'))
        service_down = await self.outcome_for(pr.ProbeResponse(status=503, body=b'sorry'))
        self.assertIsNone(pr.summarize_basic([dead, service_down]).code)

    async def test_direct_target_check_is_the_proof_and_is_bounded(self):
        plan = pr.build_plan({'mode': 'basic', 'options': {'attempts': 1}})
        checked = await pr.check_targets(transport(good(), refused()), plan.targets, plan.options, limit=1)
        self.assertEqual(len(checked), 1)
        self.assertTrue(checked[0].ok)
        summary = pr.summarize_basic([await self.outcome_for(good())], target_check=checked)
        self.assertEqual(summary.working, 1)
        self.assertIsNone(summary.code)

    async def test_a_failed_direct_check_proves_the_target_is_down(self):
        one_proxy = await self.outcome_for(pr.ProbeResponse(status=503, body=b'sorry'))
        summary = pr.summarize_basic([one_proxy], target_check=(one_proxy,))
        self.assertEqual(summary.code, pr.TARGET_UNAVAILABLE)
        self.assertTrue(summary.target_unavailable)
        self.assertTrue(summary.to_public()['target_check'])


class RecheckPlanTests(unittest.TestCase):
    def rows(self):
        return [
            {'proxy': 'http://11.0.0.1:80', 'checked_at': 900.0, 'valid_until': 1500.0, 'min_target_reliability': 1.0},
            {'proxy': 'http://11.0.0.2:80', 'checked_at': 500.0, 'valid_until': 600.0, 'min_target_reliability': 1.0},
            {'proxy': 'http://11.0.0.3:80', 'checked_at': None, 'min_target_reliability': 1.0},
            {'proxy': 'http://11.0.0.4:80', 'checked_at': 5000.0, 'min_target_reliability': 1.0},
            {'proxy': 'http://11.0.0.5:80', 'checked_at': 950.0, 'valid_until': 2000.0, 'min_target_reliability': 0.5},
        ]

    def test_expired_and_unmeasured_rows_are_re_measured(self):
        plan = pr.plan_recheck(self.rows(), now=1000.0, max_age_s=600)
        pairs = {(item.proxy, item.reason) for item in plan}
        self.assertIn(('http://11.0.0.2:80', 'expired'), pairs)
        self.assertIn(('http://11.0.0.3:80', 'time_unknown'), pairs)
        self.assertIn(('http://11.0.0.4:80', 'clock_anomaly'), pairs)
        self.assertNotIn(('http://11.0.0.1:80', 'expired'), pairs)

    def test_fresh_rows_are_left_alone_unless_asked(self):
        plan = pr.plan_recheck(self.rows(), now=1000.0, max_age_s=600)
        self.assertNotIn('http://11.0.0.1:80', {item.proxy for item in plan})
        forced = pr.plan_recheck(self.rows(), now=1000.0, max_age_s=600, remeasure_passing=True)
        reasons = {item.proxy: item.reason for item in forced}
        self.assertEqual(reasons['http://11.0.0.1:80'], 'forced')
        self.assertEqual(reasons['http://11.0.0.5:80'], 'failed_before')

    def test_limit_cuts_the_queue(self):
        plan = pr.plan_recheck(self.rows(), now=1000.0, max_age_s=600, limit=1)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].reason, 'clock_anomaly')

    def test_broken_timestamps_are_never_treated_as_fresh(self):
        rows = [{'proxy': 'http://11.0.0.9:80', 'checked_at': 'вчера', 'valid_until': 'завтра'}]
        plan = pr.plan_recheck(rows, now=1000.0, max_age_s=600)
        self.assertEqual([(item.proxy, item.reason) for item in plan],
                         [('http://11.0.0.9:80', 'time_unknown')])

    def test_missing_valid_until_falls_back_to_max_age(self):
        rows = [{'proxy': 'http://11.0.0.8:80', 'checked_at': 100.0, 'min_target_reliability': 1.0}]
        self.assertEqual(pr.plan_recheck(rows, now=1000.0, max_age_s=600)[0].reason, 'expired')
        self.assertEqual(pr.plan_recheck(rows, now=1000.0, max_age_s=5000), ())


if __name__ == '__main__':
    unittest.main()

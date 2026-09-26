"""F01 check modes, run for real against local sockets.

Every mode in this file is started, driven to completion and its answer read
back.  The endpoints are 127.0.0.1 only: a mock proxy that can be told to be
TCP-only, silent, or a working CONNECT tunnel, plus the self-hosted reference
probe the module ships.  No public proxy or third-party service is contacted.
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from probearea_support import LoopbackTransport, MockProxy, reference_probe, rule
from proxy_workbench import probes as pr


def self_hosted_plan(base_url, **settings):
    """A basic plan whose targets come from the app, not from the user.

    This is the self-hosted contract F01 asks for: the newcomer still types no
    URL, the endpoint is the reference probe the module serves, and the plan
    is built and validated by exactly the same code as the built-in probes.
    """
    payload = {'mode': 'basic', 'options': {'preset': 'frugal'},
               'targets': [item.to_public() | {'own': False} for item in
                           pr.reference_probe_targets(base_url)]}
    payload.update(settings)
    return pr.build_plan(payload)


class ModesOnRealSockets(unittest.IsolatedAsyncioTestCase):
    """Each mode is started and its answer read back, not merely constructed."""

    async def asyncSetUp(self):
        self._running = pr.serve_reference_probe()
        self.info = self._running.__enter__()
        self.base = self.info['url']
        self.proxies = {}

    async def asyncTearDown(self):
        for proxy in self.proxies.values():
            await asyncio.to_thread(proxy.stop)
        await asyncio.to_thread(self._running.__exit__, None, None, None)

    def proxy(self, name, mode):
        if name not in self.proxies:
            self.proxies[name] = MockProxy(mode).start()
        return self.proxies[name]

    # -- 1. collect_only ---------------------------------------------------

    async def test_collect_only_runs_and_proves_nothing(self):
        mode = pr.resolve_mode('collect_only')
        plan = pr.build_plan({'mode': 'collect_only'})
        self.assertTrue(mode.collects_only)
        self.assertFalse(mode.network)
        self.assertEqual(plan.mode.evidence, 'collected')
        # It runs, and running it as a measurement is refused out loud rather
        # than silently producing a verdict.
        with self.assertRaises(pr.ProbeError) as caught:
            await pr.run_plan(plan, LoopbackTransport(), endpoint='http://127.0.0.1:1')
        self.assertIn('не измеряет', str(caught.exception))

    # -- 2. tcp ------------------------------------------------------------

    async def test_tcp_mode_runs_against_a_real_socket(self):
        working = self.proxy('tcp-ok', 'connect')
        options = pr.validate_options({'preset': 'frugal'})
        outcome = await pr.run_stage('tcp', working.url, options, LoopbackTransport(working))
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual(outcome.stage, 'tcp')
        closed = MockProxy('open').start()
        try:
            options = pr.validate_options({'preset': 'frugal', 'attempts': 1,
                                           'connect_timeout_s': 1.0, 'handshake_timeout_s': 1.0,
                                           'read_timeout_s': 1.0, 'whole_probe_timeout_s': 3.0})
            dead = await pr.run_stage('tcp', closed.url, options, LoopbackTransport(closed))
            self.assertFalse(dead.ok)
            self.assertIsNotNone(dead.code)
        finally:
            await asyncio.to_thread(closed.stop)

    # -- 3. handshake ------------------------------------------------------

    async def test_handshake_mode_speaks_the_protocol_without_claiming_a_transfer(self):
        speaking = self.proxy('hs-ok', 'connect')
        silent = MockProxy('silent').start()
        try:
            options = pr.validate_options({'preset': 'frugal', 'attempts': 1,
                                           'connect_timeout_s': 1.0, 'handshake_timeout_s': 1.0,
                                           'read_timeout_s': 1.0, 'whole_probe_timeout_s': 4.0})
            ok = await pr.run_stage('handshake', speaking.url, options, LoopbackTransport(speaking))
            self.assertTrue(ok.ok, ok)
            self.assertEqual(ok.stage, 'handshake')
            broken = await pr.run_stage('handshake', silent.url, options, LoopbackTransport(silent))
            self.assertFalse(broken.ok)
        finally:
            await asyncio.to_thread(silent.stop)

    async def test_a_tcp_only_endpoint_is_never_a_transferring_proxy(self):
        """F01 acceptance, on a real socket: open port, no protocol, no transfer."""
        tcp_only = MockProxy('open').start()
        try:
            plan = pr.build_plan({'mode': 'tcp'})
            outcome = await pr.run_plan(plan, LoopbackTransport(tcp_only), endpoint=tcp_only.url)
            self.assertEqual(outcome.evidence, 'none')
            self.assertFalse(pr.is_working(outcome))
            self.assertEqual(pr.evidence_at_least(outcome.evidence, 'tcp_open'), False)

            speaking = self.proxy('tcp-only-proof', 'connect')
            speaking_plan = pr.build_plan({'mode': 'tcp'})
            speaking_outcome = await pr.run_plan(speaking_plan, LoopbackTransport(speaking),
                                                 endpoint=speaking.url)
            self.assertEqual(speaking_outcome.evidence, 'tcp_open')
            self.assertFalse(pr.is_working(speaking_outcome),
                             'TCP-only endpoint must not be reported as a transferring proxy')
            handshake_outcome = await pr.run_plan(pr.build_plan({'mode': 'handshake'}),
                                                 LoopbackTransport(speaking), endpoint=speaking.url)
            self.assertEqual(handshake_outcome.evidence, 'handshake_ok')
            self.assertFalse(pr.is_working(handshake_outcome),
                             'a handshake is not a transfer either')
        finally:
            await asyncio.to_thread(tcp_only.stop)

    # -- 4. basic: no URL typed by the user --------------------------------

    async def test_basic_transfer_runs_without_a_user_supplied_url(self):
        proxy = self.proxy('basic-ok', 'connect')
        plan = self_hosted_plan(self.base)
        self.assertEqual(plan.mode.evidence, 'transfer_ok')
        outcome = await pr.run_plan(plan, LoopbackTransport(proxy), endpoint=proxy.url)
        self.assertTrue(outcome.ok, outcome.to_public())
        self.assertEqual(outcome.evidence, 'transfer_ok')
        self.assertTrue(pr.is_working(outcome))
        self.assertGreater(outcome.bytes, 0)
        # One target answered and fail_fast stopped the chain.
        self.assertEqual(outcome.successes, 1)
        self.assertLessEqual(len(outcome.targets), len(plan.targets))

    async def test_five_working_without_any_url(self):
        """F01 acceptance: "найти 5 рабочих", no URL, exact evidence level."""
        working = [self.proxy(f'find-{index}', 'connect') for index in range(5)]
        dead = [MockProxy('open').start() for _ in range(2)]
        try:
            plan = self_hosted_plan(self.base)
            results = []
            for candidate in working + dead:
                outcome = await pr.run_plan(plan, LoopbackTransport(candidate), endpoint=candidate.url)
                results.append((candidate.url, outcome))
            found = [item for item in results if pr.is_working(item[1])]
            self.assertEqual(len(found), 5)
            for _, outcome in results:
                self.assertIn(outcome.evidence, ('transfer_ok', 'none'))
                if outcome.evidence != 'transfer_ok':
                    self.assertFalse(pr.is_working(outcome))
        finally:
            for proxy in dead:
                await asyncio.to_thread(proxy.stop)

    # -- 5. selected services ---------------------------------------------

    async def test_services_mode_runs_the_selected_definitions(self):
        proxy = self.proxy('services-ok', 'connect')
        service = pr.validate_target({'id': 'reference-api', 'name': 'reference API',
                                      'url': f'{self.base}/api/status', 'kind': 'service',
                                      'statuses': [200], 'content_type': 'application/json',
                                      'min_body_bytes': 10, 'max_body_bytes': 65_536,
                                      'json_assertions': [{'path': 'status', 'op': 'equals',
                                                           'value': 'ok'}]})
        plan = pr.build_plan({'mode': 'services', 'options': {'preset': 'frugal'},
                              'services': [service.to_public()]})
        outcome = await pr.run_plan(plan, LoopbackTransport(proxy), endpoint=proxy.url)
        self.assertTrue(outcome.ok, outcome.to_public())
        self.assertEqual(outcome.evidence, 'transfer_ok')
        # A definition that does not hold is a failure of that service, not of
        # the proxy, and it says so with its own code.
        broken = pr.validate_target({**service.to_public(), 'json_assertions':
                                     [{'path': 'quota.exhausted', 'op': 'equals', 'value': True}]})
        bad_plan = pr.build_plan({'mode': 'services', 'options': {'preset': 'frugal'},
                                  'services': [broken.to_public()]})
        bad = await pr.run_plan(bad_plan, LoopbackTransport(proxy), endpoint=proxy.url)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.code, pr.JSON_ASSERT)
        self.assertEqual(bad.stage, 'assert')

    # -- 6. custom profile -------------------------------------------------

    async def test_custom_profile_runs_user_targets(self):
        proxy = self.proxy('custom-ok', 'connect')
        plan = pr.build_plan({
            'mode': 'custom', 'options': {'preset': 'frugal'},
            'targets': [{'id': 'own', 'name': 'own echo', 'own': True, 'scope': 'trusted',
                         'url': f'{self.base}/echo', 'statuses': [200],
                         'content_type': 'application/json', 'min_body_bytes': 5,
                         'json_assertions': [{'path': 'origin', 'op': 'exists'}]}]})
        outcome = await pr.run_plan(plan, LoopbackTransport(proxy), endpoint=proxy.url)
        self.assertTrue(outcome.ok, outcome.to_public())
        self.assertEqual(outcome.evidence, 'transfer_ok')

    async def test_custom_profile_requires_targets(self):
        with self.assertRaises(pr.ProbeError) as caught:
            pr.build_plan({'mode': 'custom'})
        self.assertIn('требует хотя бы одной цели', str(caught.exception))

    # -- 7. recheck --------------------------------------------------------

    async def test_recheck_is_the_base_mode_and_mints_no_new_identity(self):
        proxy = self.proxy('recheck-ok', 'connect')
        base = self_hosted_plan(self.base)
        recheck = self_hosted_plan(self.base, mode='recheck', base_mode='basic')
        self.assertEqual(recheck.mode.name, pr.RECHECK)
        self.assertEqual(recheck.mode.base, 'basic')
        self.assertEqual(pr.plan_digest(base), pr.plan_digest(recheck))
        outcome = await pr.run_plan(recheck, LoopbackTransport(proxy), endpoint=proxy.url)
        # A recheck earns exactly the base mode's evidence, and says which mode
        # produced it, instead of filing a row no ladder can back up.
        self.assertEqual(outcome.mode, pr.RECHECK)
        self.assertEqual(outcome.base, 'basic')
        self.assertEqual(outcome.requested_mode, pr.RECHECK)
        self.assertEqual(outcome.evidence, 'transfer_ok')
        self.assertTrue(pr.is_working(outcome))
        # A recheck of something that only collects is refused, not downgraded.
        with self.assertRaises(pr.ProbeError):
            pr.resolve_mode('recheck', base='collect_only')

    # -- monitoring is a job mode -----------------------------------------

    async def test_monitor_is_a_job_mode_not_a_new_verdict(self):
        proxy = self.proxy('monitor-ok', 'connect')
        monitor = pr.build_plan({'mode': 'monitor', 'base_mode': 'basic',
                                 'targets': [item.to_public() for item in
                                             pr.reference_probe_targets(self.base)]})
        self.assertTrue(monitor.mode.monitoring)
        self.assertEqual(monitor.mode.name, 'basic')
        self.assertEqual(monitor.mode.base, 'basic')
        outcome = await pr.run_plan(monitor, LoopbackTransport(proxy), endpoint=proxy.url)
        self.assertTrue(outcome.monitoring)
        self.assertEqual(outcome.evidence, 'transfer_ok')
        self.assertEqual(outcome.mode, 'basic')
        self.assertEqual(outcome.requested_mode, 'monitor')
        # Monitoring re-runs a base mode; it never measures on its own.
        tcp_monitor = pr.resolve_mode('monitor', base='tcp')
        self.assertTrue(tcp_monitor.monitoring)
        self.assertEqual(tcp_monitor.name, 'tcp')
        for base in (None, '', 'collect_only'):
            with self.subTest(base=base):
                with self.assertRaises(pr.ProbeError) as caught:
                    pr.resolve_mode('monitor', base=base)
                self.assertIn('нечем измерять', str(caught.exception))
        with self.assertRaises(pr.ProbeError):
            pr.resolve_mode('basic', monitoring=True)

    # -- target refusal is not proxy refusal -------------------------------

    async def test_target_refusal_differs_from_proxy_refusal(self):
        """F01: "отказ нейтрального target отличается от отказа всех прокси"."""
        proxy = self.proxy('blame-ok', 'connect')
        refusing = self_hosted_plan(self.base, targets=[{
            'id': 'down', 'url': f'{self.base}/manifest-missing.m3u8', 'statuses': [200],
            'min_body_bytes': 1}])
        missing = self_hosted_plan(self.base, targets=[{
            'id': 'gone', 'url': 'http://127.0.0.1:9/definitely-not-listening',
            'statuses': [200], 'min_body_bytes': 1}])

        # Every proxy fails because the target answers 404: a direct check of
        # the same target proves the target is the one that is down.
        direct = await pr.check_targets(LoopbackTransport(proxy), refusing.targets,
                                        refusing.options, limit=1)
        summary = pr.summarize_basic([await pr.run_plan(refusing, LoopbackTransport(proxy),
                                                       endpoint=proxy.url)],
                                     target_check=direct)
        self.assertEqual(summary.code, pr.TARGET_UNAVAILABLE)
        self.assertTrue(summary.target_unavailable)

        # Every proxy fails because the proxies themselves are gone: no target
        # conclusion is possible, and none is drawn.
        dead = MockProxy('open').start()
        try:
            dead_outcome = await pr.run_plan(missing, LoopbackTransport(dead), endpoint=dead.url)
            dead_summary = pr.summarize_basic([dead_outcome])
            self.assertNotEqual(dead_summary.code, pr.TARGET_UNAVAILABLE)
        finally:
            await asyncio.to_thread(dead.stop)

    async def test_one_failing_proxy_does_not_condemn_the_target(self):
        proxy = self.proxy('one-of-many', 'connect')
        plan = self_hosted_plan(self.base)
        good = await pr.run_plan(plan, LoopbackTransport(proxy), endpoint=proxy.url)
        one_bad = MockProxy('open').start()
        try:
            bad = await pr.run_plan(plan, LoopbackTransport(one_bad), endpoint=one_bad.url)
            self.assertTrue(good.ok)
            self.assertFalse(bad.ok)
            # A set that mixes a healthy proxy with a dead one blames nobody.
            self.assertIsNone(pr.target_failure_signal([good.targets[0], bad.targets[0]]))
        finally:
            await asyncio.to_thread(one_bad.stop)


def live_report():
    """Print what each mode actually returned on a real socket."""
    async def main():
        with reference_probe() as info:
            base = info['url']
            rule('F01: семь режимов на живых сокетах (только 127.0.0.1)')
            print(f'  reference probe: {base}')
            options = pr.validate_options({'preset': 'frugal'})
            plan = self_hosted_plan(base)
            print(f'  basic-план без URL пользователя: {len(plan.targets)} цели, preset={plan.options.preset}')

            proxies = {}
            def get(name, mode):
                if name not in proxies:
                    proxies[name] = MockProxy(mode).start()
                return proxies[name]

            try:
                m = pr.resolve_mode('collect_only')
                print(f"  1 collect_only : evidence={m.evidence} network={m.network} "
                      f"-> вердикта не даёт: {m.description[:40]}")

                ok = get('live-connect', 'connect')
                o = await pr.run_stage('tcp', ok.url, options, LoopbackTransport(ok))
                print(f"  2 tcp          : ok={o.ok} stage={o.stage} code={o.code} connect_ms={o.connect_ms}")

                o = await pr.run_stage('handshake', ok.url, options, LoopbackTransport(ok))
                print(f"  3 handshake    : ok={o.ok} stage={o.stage} code={o.code}")

                tcp_only = MockProxy('open').start()
                proxies['live-open'] = tcp_only
                o_tcp = await pr.run_plan(pr.build_plan({'mode': 'tcp'}), LoopbackTransport(tcp_only),
                                          endpoint=tcp_only.url)
                print(f"  2b tcp-only    : ok={o_tcp.ok} evidence={o_tcp.evidence} "
                      f"is_working={pr.is_working(o_tcp)}  <- TCP-only НЕ передающий прокси")

                o = await pr.run_plan(plan, LoopbackTransport(ok), endpoint=ok.url)
                print(f"  4 basic        : ok={o.ok} evidence={o.evidence} bytes={o.bytes} "
                      f"is_working={pr.is_working(o)} targets={len(o.targets)}")

                service = pr.validate_target({
                    'id': 'reference-api', 'url': f'{base}/api/status', 'kind': 'service',
                    'statuses': [200], 'content_type': 'application/json', 'min_body_bytes': 10,
                    'json_assertions': [{'path': 'status', 'op': 'equals', 'value': 'ok'}]})
                o = await pr.run_plan(pr.build_plan({'mode': 'services', 'options': {'preset': 'frugal'},
                                                     'services': [service.to_public()]}),
                                      LoopbackTransport(ok), endpoint=ok.url)
                print(f"  5 services     : ok={o.ok} evidence={o.evidence} targets={len(o.targets)}")

                own = {'id': 'own', 'name': 'own echo', 'own': True, 'scope': 'trusted',
                       'url': f'{base}/echo', 'statuses': [200], 'min_body_bytes': 5}
                o = await pr.run_plan(pr.build_plan({'mode': 'custom', 'options': {'preset': 'frugal'},
                                                     'targets': [own]}), LoopbackTransport(ok), endpoint=ok.url)
                print(f"  6 custom       : ok={o.ok} evidence={o.evidence} targets={len(o.targets)}")

                recheck = self_hosted_plan(base, mode='recheck', base_mode='basic')
                o = await pr.run_plan(recheck, LoopbackTransport(ok), endpoint=ok.url)
                print(f"  7 recheck      : base={recheck.mode.base} evidence={o.evidence} "
                      f"digest(base)==digest(recheck): {pr.plan_digest(plan) == pr.plan_digest(recheck)}")

                monitor = self_hosted_plan(base, mode='monitor', base_mode='basic')
                o = await pr.run_plan(monitor, LoopbackTransport(ok), endpoint=ok.url)
                print(f"  7b monitor     : mode={o.mode} monitoring={o.monitoring} evidence={o.evidence} "
                      f"<- режим задания, не новый вердикт")

                rule('F01 приёмка: найти 5 рабочих, URL не вводится')
                found = 0
                for index in range(7):
                    candidate = get(f'candidate-{index}', 'connect' if index < 5 else 'open')
                    outcome = await pr.run_plan(plan, LoopbackTransport(candidate), endpoint=candidate.url)
                    if pr.is_working(outcome):
                        found += 1
                    print(f"  кандидат {index} {candidate.mode:8} ok={outcome.ok} "
                          f"evidence={outcome.evidence:12} is_working={pr.is_working(outcome)}")
                print(f"  найдено рабочих: {found}/7  (ожидание 5)")

                rule('F01: отказ цели != отказ всех прокси')
                down = self_hosted_plan(base, targets=[{'id': 'down', 'url': f'{base}/manifest-missing.m3u8',
                                                         'statuses': [200], 'min_body_bytes': 1}])
                direct = await pr.check_targets(LoopbackTransport(ok), down.targets, down.options, limit=1)
                s1 = pr.summarize_basic([await pr.run_plan(down, LoopbackTransport(ok), endpoint=ok.url)],
                                        target_check=direct)
                print(f"  все прокси упали, прямая проверка цели тоже упала: code={s1.code} "
                      f"target_unavailable={s1.target_unavailable}")
                dead_only = MockProxy('open').start()
                proxies['dead-only'] = dead_only
                s2 = pr.summarize_basic([await pr.run_plan(down, LoopbackTransport(dead_only),
                                                           endpoint=dead_only.url)])
                print(f"  прокси мёртвые, цель не проверялась: code={s2.code} "
                      f"(вывода о цели нет: {s2.code != pr.TARGET_UNAVAILABLE})")
            finally:
                for proxy in proxies.values():
                    proxy.stop()
    asyncio.run(main())


if __name__ == '__main__':
    live_report()

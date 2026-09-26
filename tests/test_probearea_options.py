"""F07 advanced parameters: units, limits, one validation, no silent ignore.

The point of this file is that every parameter is either *effective* or
*rejected*.  The parameter that used to be silently ignored —
``max_body_bytes`` — is checked here against a real transfer, not by reading
the code: the same target, the same body, two different ceilings, two
different answers.
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from probearea_support import LoopbackTransport, MockProxy, reference_probe
from proxy_workbench import probes as pr

OWN = {'id': 'own', 'name': 'own', 'own': True, 'scope': 'trusted'}


class UnitsLimitsValidation(unittest.TestCase):
    def test_every_option_has_a_unit_and_a_range(self):
        options = pr.validate_options({'preset': 'balanced'})
        described = options.describe()
        self.assertEqual(set(described), set(options.to_public()))
        for name, text in described.items():
            self.assertTrue(text, name)
            self.assertIn(pr.LIMITS[name].unit, text)

    def test_every_option_is_range_checked_at_both_ends(self):
        for name, limit in pr.LIMITS.items():
            if name not in pr.DEFAULT_OPTIONS or name == 'whole_probe_timeout_s':
                continue  # own bounds are covered by their dedicated test
            with self.subTest(option=name):
                # The cross-field budget check is part of the contract too, so
                # the worst case is raised to its own maximum when a single
                # field is being driven to its edge.
                wide = {'whole_probe_timeout_s': pr.LIMITS['whole_probe_timeout_s'].maximum}
                low = pr.validate_options({name: limit.minimum, **wide})
                high = pr.validate_options({name: limit.maximum, **wide})
                self.assertEqual(getattr(low, name), limit.minimum)
                self.assertEqual(getattr(high, name), limit.maximum)
                with self.assertRaises(pr.ProbeError):
                    pr.validate_options({name: limit.maximum + 1, **wide})
                if limit.minimum > 0:
                    with self.assertRaises(pr.ProbeError):
                        pr.validate_options({name: limit.minimum - 1, **wide})

    def test_whole_probe_timeout_keeps_its_own_bounds(self):
        limit = pr.LIMITS['whole_probe_timeout_s']
        # The private timeouts each have a 0.1 s floor, so the whole-probe
        # floor of 0.5 s is only reachable with all three at that floor.
        fitting = {'connect_timeout_s': 0.1, 'handshake_timeout_s': 0.1, 'read_timeout_s': 0.1}
        self.assertEqual(pr.validate_options({**fitting, 'whole_probe_timeout_s': limit.minimum}
                                             ).whole_probe_timeout_s, limit.minimum)
        self.assertEqual(pr.validate_options({'whole_probe_timeout_s': limit.maximum}
                                             ).whole_probe_timeout_s, limit.maximum)
        with self.assertRaises(pr.ProbeError):
            pr.validate_options({**fitting, 'whole_probe_timeout_s': limit.maximum + 1})
        # Below the floor it is not a time at all, and the floor is enforced
        # even when the private timeouts would fit.
        with self.assertRaises(pr.ProbeError):
            pr.validate_options({**fitting, 'whole_probe_timeout_s': 0.4})

    def test_speed_and_judge_ceilings_are_validated_by_their_own_owner(self):
        with self.assertRaises(pr.ProbeError):
            pr.validate_speed_target({'url': 'http://127.0.0.1/x', 'max_bytes': 10})
        self.assertEqual(pr.validate_speed_target({'url': 'http://127.0.0.1/x',
                                                  'max_bytes': 100_000}).max_bytes, 100_000)
        with self.assertRaises(pr.ProbeError):
            pr.validate_judge_spec({'url': 'http://127.0.0.1/echo', 'max_bytes': 10})

    def test_unknown_option_is_rejected_not_ignored(self):
        with self.assertRaises(pr.ProbeError) as caught:
            pr.validate_options({'timeout': 5})
        self.assertEqual(caught.exception.code, pr.E_VALIDATION_UNKNOWN_FIELD)

    def test_whole_probe_timeout_must_cover_the_worst_case(self):
        with self.assertRaises(pr.ProbeError) as caught:
            pr.validate_options({'connect_timeout_s': 10, 'handshake_timeout_s': 10,
                                 'read_timeout_s': 10, 'whole_probe_timeout_s': 20})
        self.assertIn('худшего случая', str(caught.exception))

    def test_backoff_is_geometric_and_capped(self):
        options = pr.validate_options({'attempts': 5, 'backoff_s': 0.5, 'backoff_factor': 3.0,
                                       'backoff_max_s': 2.0})
        self.assertEqual(pr.plan_attempts(options), (0.5, 1.5, 2.0, 2.0))

    def test_dns_mode_is_a_closed_set(self):
        for mode in pr.DNS_MODES:
            self.assertEqual(pr.validate_target({**OWN, 'url': 'http://127.0.0.1/x',
                                                 'dns_mode': mode}).dns_mode, mode)
        with self.assertRaises(pr.ProbeError):
            pr.validate_target({**OWN, 'url': 'http://127.0.0.1/x', 'dns_mode': 'system'})


class Presets(unittest.TestCase):
    def test_all_four_presets_exist_with_ru_aliases(self):
        for name, alias in (('fast', 'быстро'), ('balanced', 'баланс'),
                            ('thorough', 'тщательно'), ('frugal', 'экономно')):
            with self.subTest(preset=name):
                self.assertEqual(pr.resolve_preset(alias), name)
                options = pr.validate_options({'preset': alias})
                self.assertEqual(options.preset, name)
                self.assertEqual(set(options.to_public()), set(pr.PRESETS[name]))

    def test_every_preset_fits_its_own_whole_probe_timeout(self):
        for item in pr.presets_manifest()['presets']:
            with self.subTest(preset=item['name']):
                self.assertTrue(item['fits'], item)
                pr.validate_options({'preset': item['name']})

    def test_unknown_preset_is_an_error_not_a_default(self):
        with self.assertRaises(pr.ProbeError) as caught:
            pr.validate_options({'preset': 'turbo'})
        self.assertIn('Неизвестный пресет', str(caught.exception))

    def test_preset_is_a_full_set_so_no_previous_scenario_survives(self):
        """Defect 24: switching preset must not leave the old backoff behind."""
        thorough = pr.validate_options({'preset': 'thorough'})
        self.assertEqual(thorough.backoff_s, 1.0)
        self.assertEqual(thorough.max_redirects, 2)
        frugal = pr.validate_options({'preset': 'frugal'})
        self.assertEqual(frugal.backoff_s, 0.0)
        self.assertEqual(frugal.max_redirects, 0)
        self.assertEqual(frugal.attempts, 1)
        self.assertEqual(pr.plan_attempts(frugal), ())

    def test_visible_overrides_win_and_are_reported(self):
        options = pr.validate_options({'preset': 'fast', 'attempts': 3, 'max_body_bytes': 4096})
        self.assertEqual(options.attempts, 3)
        self.assertEqual(options.max_body_bytes, 4096)
        self.assertEqual(options.connect_timeout_s, 2.0, 'остальное остаётся от пресета')
        self.assertEqual(options.visible_overrides(['attempts', 'max_body_bytes']),
                         {'attempts': 3, 'max_body_bytes': 4096})
        plan = pr.build_plan({'mode': 'tcp', 'options': {'preset': 'fast', 'attempts': 3}})
        self.assertEqual(plan.to_public()['preset'], 'fast')
        self.assertEqual(plan.to_public()['options']['attempts'], 3)
        self.assertIn('read_timeout_s', plan.to_public()['option_limits'])

    def test_preset_name_does_not_mint_a_new_measurement_identity(self):
        explicit = pr.build_plan({'mode': 'tcp', 'options': pr.PRESETS['fast']})
        named = pr.build_plan({'mode': 'tcp', 'options': {'preset': 'fast'}})
        self.assertNotEqual(explicit.options.preset, named.options.preset)
        self.assertEqual(pr.plan_digest(explicit), pr.plan_digest(named))


class OwnProfileOnly(unittest.TestCase):
    PUBLIC = {'id': 'p', 'url': 'https://example.org/'}

    def test_post_body_and_auth_need_an_own_profile(self):
        for payload, fragment in (
            ({**self.PUBLIC, 'method': 'POST'}, 'изменяет состояние'),
            ({**self.PUBLIC, 'body': 'a=1'}, 'тело запроса'),
            ({**self.PUBLIC, 'auth': {'secret_ref': 'api_key'}}, 'API-auth'),
            ({**self.PUBLIC, 'headers': {'authorization': 'Bearer x'}}, 'учётные данные'),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(pr.ProbeError) as caught:
                    pr.validate_target(payload)
                self.assertEqual(caught.exception.code, pr.E_VALIDATION_TARGET_UNSAFE)
                self.assertIn(fragment, str(caught.exception))

    def test_own_profile_accepts_them_with_a_secret_reference(self):
        target = pr.validate_target({**OWN, **self.PUBLIC, 'method': 'POST', 'body': 'a=1',
                                     'auth': {'secret_ref': 'api_key', 'store': 'vault'}})
        self.assertTrue(target.credentialed)
        self.assertEqual(target.auth.store, 'vault')
        self.assertNotIn('value', target.to_public()['auth'])
        self.assertNotIn('a=1', str(target.to_public()))
        self.assertEqual(target.to_public()['body_bytes'], 3)

    def test_a_secret_value_is_never_accepted(self):
        with self.assertRaises(pr.ProbeError) as caught:
            pr.validate_target({**OWN, **self.PUBLIC, 'auth': {'secret_ref': 'k', 'value': 'sk-live'}})
        self.assertIn('ссылкой на хранилище', str(caught.exception))

    def test_unsafe_repeats_are_refused(self):
        target = pr.validate_target({**OWN, **self.PUBLIC, 'method': 'POST', 'body': 'a=1'})
        with self.assertRaises(pr.ProbeError) as caught:
            pr.build_plan({'mode': 'custom', 'targets': [target.to_public()],
                           'options': {'attempts': 2}})
        self.assertEqual(caught.exception.code, pr.E_VALIDATION_RETRY_UNSAFE)

    def test_a_target_public_view_can_be_fed_back_in(self):
        target = pr.validate_target({**OWN, **self.PUBLIC, 'body': 'a=1',
                                     'auth': {'secret_ref': 'api_key'}})
        # The public view is redacted (no body, no secret value), but it must
        # still be loadable, and everything that is not redacted must survive.
        again = pr.validate_target(target.to_public())
        self.assertEqual({k: v for k, v in again.to_public().items() if k != 'body_bytes'},
                         {k: v for k, v in target.to_public().items() if k != 'body_bytes'})
        self.assertEqual(again.body, None, 'тело намеренно не публикуется')
        self.assertEqual(again.auth.name, 'api_key')
        broken = {**target.to_public(), 'body': 'a=1', 'body_bytes': 999}
        with self.assertRaises(pr.ProbeError) as caught:
            pr.validate_target(broken)
        self.assertIn('только для чтения', str(caught.exception))
        contradicted = {**target.to_public(), 'auth': None, 'auth_configured': True}
        with self.assertRaises(pr.ProbeError):
            pr.validate_target(contradicted)

    def test_custom_ca_never_turns_tls_verification_off(self):
        import ssl
        for bundle in (None, '/etc/ssl/cert.pem'):
            context = pr.build_ssl_context(bundle)
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        plan = pr.build_plan({'mode': 'tcp', 'options': {'preset': 'frugal'}})
        self.assertNotIn('verify', plan.to_public()['options'])


class BodyBudgetIsEffective(unittest.IsolatedAsyncioTestCase):
    """The parameter that used to be ignored, proved on a real transfer."""

    async def asyncSetUp(self):
        self._running = pr.serve_reference_probe()
        self.info = self._running.__enter__()
        self.base = self.info['url']
        self.proxy = MockProxy('connect').start()
        self.transport = LoopbackTransport(self.proxy)

    async def asyncTearDown(self):
        await asyncio.to_thread(self.proxy.stop)
        await asyncio.to_thread(self._running.__exit__, None, None, None)

    def body(self, size=400_000):
        return f"{self.base}/bytes/{size}"

    async def test_options_max_body_bytes_caps_a_target_that_asks_for_more(self):
        target = pr.validate_target({'id': 'big', 'url': self.body(), 'min_body_bytes': 1,
                                      'max_body_bytes': 400_000})
        generous = pr.validate_options({'preset': 'balanced', 'max_body_bytes': 262_144})
        stingy = pr.validate_options({'preset': 'balanced', 'max_body_bytes': 4_096})
        self.assertEqual(target.max_body_bytes, 400_000)

        before = pr.body_budget(target, generous)
        after = pr.body_budget(target, stingy)
        self.assertEqual(before, 262_144, 'потолок опций ограничивает цель')
        self.assertEqual(after, 4_096, 'иначе параметр был бы проигнорирован')

        run_before = await pr.run_probe(target, generous, self.transport)
        run_after = await pr.run_probe(target, stingy, self.transport)
        self.assertEqual(run_before.body_limit, 262_144)
        self.assertEqual(run_after.body_limit, 4_096)
        self.assertGreaterEqual(run_before.bytes, run_after.bytes)
        self.assertLessEqual(run_after.bytes, 4_096 + 16_384)
        self.assertEqual(run_before.to_public()['body_limit'], 262_144)

    async def test_a_target_may_lower_the_ceiling_but_not_raise_it(self):
        target = pr.validate_target({'id': 'small', 'url': self.body(50_000), 'min_body_bytes': 1,
                                      'max_body_bytes': 50_000})
        generous = pr.validate_options({'preset': 'balanced', 'max_body_bytes': 262_144})
        self.assertEqual(pr.body_budget(target, generous), 50_000)
        outcome = await pr.run_probe(target, generous, self.transport)
        self.assertEqual(outcome.body_limit, 50_000)

    async def test_redirects_option_is_effective_too(self):
        plan = pr.build_plan({'mode': 'custom', 'options': {'preset': 'frugal', 'max_redirects': 2},
                              'targets': [{'id': 'hop', 'own': True, 'scope': 'trusted',
                                           'url': f'{self.base}/redirect/1', 'statuses': [200],
                                           'min_body_bytes': 2, 'max_body_bytes': 65_536}]})
        outcome = await pr.run_plan(plan, self.transport, endpoint=self.proxy.url)
        self.assertTrue(outcome.ok, outcome.to_public())
        self.assertEqual(plan.options.max_redirects, 2)


def live_report():
    import json
    print('\n--- F07: параметры с единицами, лимитами, одной валидацией ' + '-' * 24)
    manifest = pr.presets_manifest()
    for item in manifest['presets']:
        values = item['values']
        print(f"  {item['name']:9} connect={values['connect_timeout_s']:>5} handshake={values['handshake_timeout_s']:>5} "
              f"read={values['read_timeout_s']:>5} whole={values['whole_probe_timeout_s']:>5} "
              f"attempts={values['attempts']} backoff={values['backoff_s']}×{values['backoff_factor']}"
              f"≤{values['backoff_max_s']} body≤{values['max_body_bytes']} redir≤{values['max_redirects']} "
              f"худший случай={item['worst_case_s']}с помещается={item['fits']}")
    print('  алиасы:', ', '.join(sorted(manifest['aliases'])))

    print('\n  видимые переопределения:')
    options = pr.validate_options({'preset': 'тщательно', 'attempts': 4, 'max_body_bytes': 8192})
    print('   пресет=thorough, задано attempts=4 и max_body_bytes=8192 ->',
          json.dumps(options.visible_overrides(['attempts', 'max_body_bytes'])))
    print('   остальные значения от пресета:',
          json.dumps({k: v for k, v in options.to_public().items()
                      if k not in ('attempts', 'max_body_bytes')}, ensure_ascii=False))

    print('\n  молча игнорируемые параметры (отвергаются, а не теряются):')
    for payload in ({'timeout': 5}, {'verify': False}, {'max_retry': 3},
                    {'judge_max_bytes': 1 << 20}, {'speed_max_bytes': 1 << 20}):
        try:
            pr.validate_options(payload)
            print(f'   {payload} -> ПРИНЯТ (плохо)')
        except pr.ProbeError as exc:
            print(f'   {payload} -> {exc.code}')

    async def main():
        rule_start = '\n--- F07: max_body_bytes реально применяется (живой поток) ' + '-' * 12
        print(rule_start)
        with reference_probe() as info:
            with MockProxy('connect') as proxy:
                transport = LoopbackTransport(proxy)
                target = pr.validate_target({'id': 'big', 'url': f"{info['url']}/bytes/400000",
                                             'min_body_bytes': 1, 'max_body_bytes': 400_000})
                for ceiling in (262_144, 16_384, 4_096):
                    opts = pr.validate_options({'preset': 'balanced', 'max_body_bytes': ceiling})
                    outcome = await pr.run_probe(target, opts, transport)
                    print(f"   потолок опций {ceiling:>7} -> body_limit={outcome.body_limit:>7} "
                          f"прочитано={outcome.bytes:>7} ok={outcome.ok} code={outcome.code}")
                print('   (цель сама просит 400000; потолок опций реально режет ответ)')

                print('\n--- F07: POST/body/API-auth только в собственном профиле ' + '-' * 16)
                public = {'id': 'p', 'url': 'https://example.org/'}
                for payload, label in (({**public, 'method': 'POST'}, 'POST на публичной цели'),
                                       ({**public, 'body': 'a=1'}, 'body на публичной цели'),
                                       ({**public, 'auth': {'secret_ref': 'k'}}, 'API-auth на публичной цели'),
                                       ({**public, 'headers': {'x-api-key': 'k'}}, 'credential-заголовок')):
                    try:
                        pr.validate_target(payload)
                        print(f'   {label}: ПРИНЯТ (плохо)')
                    except pr.ProbeError as exc:
                        print(f'   {label}: {exc.code}')
                own = pr.validate_target({'id': 'own', 'own': True, 'scope': 'trusted',
                                          'url': 'https://example.org/api', 'method': 'POST',
                                          'body': 'a=1', 'auth': {'secret_ref': 'k'}})
                print('   собственный профиль: ok, публичное представление без секрета ->',
                      json.dumps(own.to_public(), ensure_ascii=False)[:150], '...')
                try:
                    pr.build_plan({'mode': 'custom', 'targets': [own.to_public()], 'options': {'attempts': 2}})
                except pr.ProbeError as exc:
                    print(f'   повтор небезопасного метода: {exc.code}')
                import ssl
                ctx = pr.build_ssl_context('/etc/ssl/cert.pem')
                print('   custom CA не отключает TLS:',
                      f'check_hostname={ctx.check_hostname} verify_mode={ctx.verify_mode.name}')
    asyncio.run(main())


if __name__ == '__main__':
    live_report()

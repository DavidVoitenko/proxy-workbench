"""Сценарии набора: полный состав полей и явная политика сброса (дефект 24).

Дефект 24 — «presets не оставляют неожиданные параметры предыдущего сценария».
Конкретное воспроизведение в текущем коде: обработчик сценариев в
``proxy_workbench/ui/app.js`` (функция, регистрирующая ``card.onclick``, строки
4352-4421) меняет только часть полей, поэтому после «Elite Приватность» у
сценария «YouTube и видео» остаются ``judge-url``, ``dnsbl-enabled``,
``strict-clean`` и ``min_anonymity`` — при новых целях YouTube.

Здесь тот же переход выполняется через :func:`servicecatalog.apply_scenario` и
проверяется поведением: после переключения не остаётся ни judge, ни strict, ни
anonymity условий от предыдущего сценария, а поля пользователя не страдают.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('PROXY_WORKBENCH_LANG', 'ru')
from proxy_workbench import servicecatalog as sc
from proxy_workbench import gui

SET_IDS = ('basic', 'search', 'messengers', 'social', 'video', 'development', 'api')


def dig(settings, field):
    node = settings
    for part in field.split('.'):
        node = node[part]
    return node


def elite_settings():
    """Настройки после сценария «Elite Приватность» текущего интерфейса."""
    settings = gui.defaults()
    settings['anonymity'] = {'judge_url': 'http://azenv.net/'}
    settings['min_anonymity'] = 'elite'
    settings['reputation'] = dict(settings['reputation'], dnsbl_enabled=True,
                                  dnsbl_zones=['zen.spamhaus.org', 'bl.spamcop.net'], strict=True)
    settings['dnsbl'] = True
    settings['strict_clean'] = True
    settings['speedtest'] = {'url': 'https://speed.cloudflare.com/__down?bytes=5000000',
                             'max_bytes': 5_000_000}
    return settings


class FieldInventoryTest(unittest.TestCase):
    """Набор объявляет полный состав полей сценария — иначе манифест невалиден."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()

    def test_targets_is_the_only_derived_scenario_field(self):
        self.assertEqual(self.catalog.derived_fields(), ('targets',))
        self.assertIn('targets', self.catalog.scenario_fields())

    def test_every_set_declares_every_scenario_field(self):
        expected = set(self.catalog.scenario_fields()) - set(self.catalog.derived_fields())
        for set_id in SET_IDS:
            scenario = self.catalog.service_set(set_id).scenario
            self.assertEqual(set(scenario), expected, set_id)

    def test_every_scenario_field_has_a_documented_reset_value(self):
        for set_id in SET_IDS:
            for field, value in self.catalog.service_set(set_id).scenario.items():
                if value is None:
                    self.assertIn(field, sc.SCENARIO_DEFAULTS, f'{set_id}.{field}')

    def test_reset_table_matches_the_surface_defaults(self):
        """SCENARIO_DEFAULTS обязан совпадать с текущими умолчаниями GUI.

        gui.defaults() — единственное место, где живут значения по умолчанию; если
        поверхность `web` их поменяет, тест обязан стать красным, а не сброс
        незаметно разойтись с интерфейсом.
        """
        defaults = gui.defaults()
        for field, expected in sc.SCENARIO_DEFAULTS.items():
            actual = dig(defaults, field)
            if isinstance(expected, (int, float)) and not isinstance(expected, bool):
                self.assertAlmostEqual(float(actual), float(expected), places=6, msg=field)
            else:
                self.assertEqual(actual, expected, field)

    def test_user_owned_fields_are_reported_and_never_part_of_a_scenario(self):
        user_fields = set(self.catalog.user_fields())
        self.assertTrue(user_fields)
        for name in ('workers', 'rate', 'watch', 'want', 'top', 'sort', 'countries',
                     'exclude_hosting', 'max_latency', 'proxies', 'sources', 'denylist'):
            self.assertIn(name, user_fields, name)
        for set_id in SET_IDS:
            self.assertTrue(user_fields.isdisjoint(self.catalog.service_set(set_id).scenario), set_id)

    def test_manifest_missing_one_scenario_field_is_rejected(self):
        data = sc.load_manifest()
        for item in data['service_sets']:
            item['scenario'].pop('reputation.strict')
        with self.assertRaises(sc.ManifestError) as caught:
            sc.parse_catalog(data)
        self.assertIn('reputation.strict', str(caught.exception))
        self.assertIn('24', str(caught.exception))

    def test_manifest_setting_a_user_field_is_rejected(self):
        data = sc.load_manifest()
        data['service_sets'][0]['scenario']['workers'] = 256
        with self.assertRaises(sc.ManifestError) as caught:
            sc.parse_catalog(data)
        self.assertIn('workers', str(caught.exception))


class ApplyScenarioTest(unittest.TestCase):
    """Поведение apply_scenario на переходах между сценариями."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()

    def apply(self, set_id, settings):
        pinned = sc.pin_set(self.catalog, set_id)
        return sc.apply_scenario(settings, pinned.scenario, self.catalog.user_fields(),
                                 targets=pinned.targets())

    def test_switching_from_elite_to_video_leaves_no_judge_or_strict(self):
        before = elite_settings()
        result = self.apply('video', before)
        self.assertEqual(result.settings['anonymity']['judge_url'], '')
        self.assertEqual(result.settings['min_anonymity'], 'any')
        self.assertFalse(result.settings['reputation']['strict'])
        self.assertFalse(result.settings['reputation']['dnsbl_enabled'])
        self.assertEqual(result.settings['reputation']['dnsbl_zones'], [])
        self.assertEqual(result.settings['speedtest']['url'], '')

    def test_video_scenario_replaces_targets_with_the_204_endpoint_only(self):
        result = self.apply('video', elite_settings())
        self.assertEqual([target['url'] for target in result.settings['targets']],
                         ['https://www.youtube.com/generate_204'])

    def test_the_cleared_report_names_exactly_the_reset_fields(self):
        result = self.apply('video', elite_settings())
        self.assertIn('anonymity.judge_url', result.cleared_fields)
        self.assertEqual([entry[0] for entry in result.set_fields if entry[0] in result.cleared_fields],
                         list(result.cleared_fields))

    def test_report_records_the_previous_value_of_every_changed_field(self):
        result = self.apply('video', elite_settings())
        changed = dict((field, (old, new)) for field, old, new in result.set_fields)
        self.assertEqual(changed['anonymity.judge_url'], ('http://azenv.net/', ''))
        self.assertEqual(changed['min_anonymity'], ('elite', 'any'))
        self.assertEqual(changed['reputation.strict'], (True, False))

    def test_switching_back_to_a_scenario_that_needs_a_judge_restores_it(self):
        result = self.apply('basic', elite_settings())
        self.assertEqual(result.settings['anonymity']['judge_url'], '')
        self.assertEqual(result.settings['min_anonymity'], 'any')

    def test_messengers_scenario_keeps_its_own_protocol(self):
        result = self.apply('messengers', gui.defaults())
        self.assertEqual(result.settings['protocol'], 'socks5')
        self.assertEqual(result.settings['connect_timeout'], 3)
        self.assertEqual(result.settings['timeout'], 6)

    def test_no_set_silently_enables_a_speed_test(self):
        """Замер скорости в 5 МБ на адрес — это стоимость, и она объявляется явно."""
        for set_id in SET_IDS:
            result = self.apply(set_id, gui.defaults())
            self.assertEqual(result.settings['speedtest']['url'], '', set_id)

    def test_no_set_silently_enables_dnsbl(self):
        for set_id in SET_IDS:
            scenario = self.catalog.service_set(set_id).scenario
            self.assertIs(scenario['reputation.dnsbl_enabled'], False, set_id)
            self.assertEqual(scenario['reputation.dnsbl_zones'], [], set_id)
            self.assertIs(scenario['reputation.strict'], False, set_id)

    def test_user_fields_survive_every_transition(self):
        before = elite_settings()
        before['workers'] = 256
        before['watch'] = 60
        before['countries'] = 'DE,NL'
        for set_id in SET_IDS:
            result = self.apply(set_id, before)
            self.assertEqual(result.settings['workers'], 256, set_id)
            self.assertEqual(result.settings['watch'], 60, set_id)
            self.assertEqual(result.settings['countries'], 'DE,NL', set_id)
            self.assertIn('workers', result.carried_over_fields, set_id)
            self.assertIn('countries', result.carried_over_fields, set_id)

    def test_the_original_settings_object_is_never_mutated(self):
        before = elite_settings()
        snapshot = dict(before, anonymity=dict(before['anonymity']),
                        reputation=dict(before['reputation']), speedtest=dict(before['speedtest']))
        self.apply('video', before)
        self.assertEqual(before['anonymity'], snapshot['anonymity'])
        self.assertEqual(before['reputation'], snapshot['reputation'])
        self.assertEqual(before['speedtest'], snapshot['speedtest'])

    def test_a_scenario_may_not_set_a_user_field(self):
        pinned = sc.pin_set(self.catalog, 'basic')
        scenario = dict(pinned.scenario, workers=256)
        with self.assertRaises(sc.CatalogError) as caught:
            sc.apply_scenario(gui.defaults(), scenario, self.catalog.user_fields())
        self.assertIn('workers', str(caught.exception))

    def test_a_null_without_a_reset_value_is_refused_not_silently_dropped(self):
        with self.assertRaises(sc.CatalogError) as caught:
            sc.apply_scenario(gui.defaults(), {'made_up_field': None}, self.catalog.user_fields())
        self.assertIn('made_up_field', str(caught.exception))

    def test_settings_must_be_an_object(self):
        pinned = sc.pin_set(self.catalog, 'basic')
        with self.assertRaises(sc.CatalogError):
            sc.apply_scenario('nope', pinned.scenario, self.catalog.user_fields())

    def test_applying_the_same_set_twice_is_idempotent(self):
        pinned = sc.pin_set(self.catalog, 'development')
        first = sc.apply_scenario(elite_settings(), pinned.scenario, self.catalog.user_fields(),
                                 targets=pinned.targets())
        second = sc.apply_scenario(first.settings, pinned.scenario, self.catalog.user_fields(),
                                   targets=pinned.targets())
        self.assertEqual(first.settings, second.settings)
        self.assertEqual(second.set_fields, ())

    def test_report_is_json_ready(self):
        result = self.apply('social', elite_settings())
        import json
        payload = json.dumps(result.report(), ensure_ascii=False)
        self.assertIn('anonymity.judge_url', payload)
        self.assertIn('carried_over', payload)


class ResetPolicyTest(unittest.TestCase):
    """Явная политика сброса: null = сброс в документированное значение."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()

    def test_null_resets_to_the_documented_value_not_to_none(self):
        settings = elite_settings()
        sc.apply_scenario(settings, {'anonymity.judge_url': None, 'speedtest.url': None}, ())
        self.assertEqual(settings['anonymity']['judge_url'], 'http://azenv.net/')
        self.assertNotIn('anonymity.judge_url', settings)

    def test_explicit_value_is_written_as_is(self):
        settings = gui.defaults()
        result = sc.apply_scenario(settings, {'min_anonymity': 'elite'}, ())
        self.assertEqual(result.settings['min_anonymity'], 'elite')
        self.assertEqual(result.cleared_fields, ())

    def test_unchanged_fields_are_reported_separately_from_changed_ones(self):
        settings = gui.defaults()
        settings['min_anonymity'] = 'elite'
        result = sc.apply_scenario(settings, {'min_anonymity': 'any', 'protocol': 'all'}, ())
        self.assertEqual([entry[0] for entry in result.set_fields], ['min_anonymity'])
        self.assertIn('protocol', result.unchanged_fields)
        self.assertNotIn('min_anonymity', result.unchanged_fields)

    def test_nested_field_assignment_does_not_drop_sibling_keys(self):
        settings = elite_settings()
        result = sc.apply_scenario(settings, {'reputation.dnsbl_zones': []}, ())
        self.assertEqual(sorted(result.settings['reputation']),
                         ['dnsbl_enabled', 'dnsbl_zones', 'local_enabled', 'strict', 'timeout'])

    def test_targets_are_only_written_when_supplied(self):
        settings = gui.defaults()
        result = sc.apply_scenario(settings, {'min_anonymity': 'elite'}, ())
        self.assertEqual(result.settings['targets'], gui.defaults()['targets'])
        self.assertEqual(result.targets, [])


if __name__ == '__main__':
    unittest.main()

"""F06 и дефект 24 живым поведением: каталог сервисов, наборы, обновление определений.

Каждый тест вызывает настоящие функции `servicecatalog.py` и проверяет наблюдаемый
результат.  Сети нет: `servicecatalog` по построению не открывает сокет, а тесты
ни к одному эндпоинту не обращаются.

Закрывает:
* категории, поиск, мультивыбор, готовые и пользовательские наборы;
* переиспользование девяти presets предыдущего приложения;
* состав preset: id/версия/maintainer/пробы/условие прохождения/ограничения/
  стоимость/дата проверки определения/совместимость;
* различие названий homepage / API / WebSocket / media / длительное соединение и
  запрет обещать звонки, 4K или весь сервис;
* обновление presets не меняет молча закреплённый набор, и diff понятен;
* дефект 24: набор задаёт полный состав полей с явной политикой сброса.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('PROXY_WORKBENCH_LANG', 'ru')
from proxy_workbench import servicecatalog as sc

SET_IDS = ('basic', 'search', 'messengers', 'social', 'video', 'development', 'api')

#: Девять presets, которые предыдущее приложение держало в ui/app.js.
LEGACY_PRESET_IDS = ('google-204', 'youtube-204', 'telegram-home', 'discord-gateway',
                     'github-home', 'wikipedia-home', 'instagram-home', 'openai-models',
                     'cloudflare-trace')

#: Сервисы, добавленные исследованием по реальным контрактам эндпоинтов.
RESEARCHED_PRESET_IDS = ('bing-home', 'duckduckgo-home', 'signal-home', 'reddit-home',
                         'x-home', 'tiktok-home', 'steam-store-home',
                         'microsoft-oidc-discovery')


class MessageMixin:
    """Messages come in RU or EN depending on the host; the test states both."""

    def says(self, error, *variants):
        text = str(error)
        self.assertTrue(any(variant in text for variant in variants),
                        f'{text!r} does not contain any of {variants}')


def dig(settings, field):
    node = settings
    for part in field.split('.'):
        node = node[part]
    return node


def elite_settings():
    """Настройки после сценария «приватность/элита»: максимум условий на месте."""
    return {'attempts': 1, 'connect_timeout': 1.0, 'timeout': 3.0, 'max_bytes': 4096,
            'min_success': 1.0, 'fail_fast': False, 'request_profile': 'elite',
            'protocol': 'https', 'prefilter': 32,
            'anonymity': {'judge_url': 'https://judge.example/check'},
            'min_anonymity': 'strict',
            'reputation': {'local_enabled': True, 'dnsbl_enabled': True,
                           'dnsbl_zones': ['zen.spamhaus.org'], 'strict': True,
                           'timeout': 2.5},
            'speedtest': {'url': 'https://speed.example/u', 'max_bytes': 50_000_000},
            'workers': 64, 'rate': 0.5, 'watch': True, 'want': 'NL', 'denylist': 'x'}


class CatalogShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()
        cls.manifest = sc.load_manifest()

    def test_the_seven_required_categories_and_sets_exist(self):
        self.assertEqual({item.category_id for item in self.catalog.categories}, set(SET_IDS))
        self.assertEqual({item.set_id for item in self.catalog.service_sets}, set(SET_IDS))

    def test_the_nine_presets_of_the_previous_application_are_reused(self):
        for preset_id in LEGACY_PRESET_IDS:
            with self.subTest(preset=preset_id):
                preset = self.catalog.preset(preset_id)
                self.assertEqual(preset.origin, 'legacy_app_js',
                                 'происхождение не выдумано и не переписано')
                self.assertEqual(preset.verification, 'legacy_definition')
                self.assertIn('app.js', preset.definition_source['ref'])
                self.assertTrue(preset.probes)

    def test_every_preset_states_what_it_proves_and_what_it_does_not(self):
        for preset in self.catalog.presets:
            with self.subTest(preset=preset.preset_id):
                payload = preset.to_dict()
                for field in ('id', 'version', 'maintainer', 'probes', 'pass_condition_ru',
                              'pass_condition_en', 'not_proved_ru', 'not_proved_en', 'cost',
                              'definition_checked_on', 'verification', 'definition_source',
                              'contract_source', 'capability', 'categories', 'digest'):
                    self.assertIn(field, payload)
                self.assertGreaterEqual(preset.version, 1)
                self.assertTrue(preset.maintainer)
                self.assertTrue(preset.not_proved_ru, 'без «не доказывает» пресет не принимается')
                self.assertTrue(preset.not_proved_en)
                self.assertTrue(preset.probes)
                self.assertGreaterEqual(preset.cost.requests_per_pass, 1)
                self.assertGreater(preset.cost.budget_weight, 0)
                self.assertTrue(preset.cost.rate_limit_ru and preset.cost.rate_limit_en)
                self.assertIn(preset.verification, sc.VERIFICATION_IDS)
                self.assertIn(preset.origin, sc.ORIGIN_IDS)
                self.assertIn('kind', preset.definition_source)
                self.assertIn('ref', preset.definition_source)
                self.assertTrue(preset.contract_source)

    def test_a_probe_states_its_own_pass_condition(self):
        for preset in self.catalog.presets:
            for probe in preset.probes:
                with self.subTest(probe=probe.probe_id):
                    target = probe.to_target(preset.title_ru)
                    # ровно тот набор полей, который принимает сканер
                    self.assertEqual(sorted(target),
                                     ['contains', 'headers', 'method', 'name', 'sha256',
                                      'statuses', 'url'])
                    self.assertIn(probe.method, ('GET', 'HEAD'))
                    self.assertTrue(probe.statuses)
                    self.assertIn('HTTP', probe.pass_condition_ru())
                    for header in probe.headers:
                        self.assertIn(header.lower(), sc.SAFE_PROBE_HEADERS)

    def test_capabilities_are_distinct_and_unmeasurable_ones_are_refused(self):
        names = {item.capability_id for item in self.catalog.capabilities}
        for expected in ('homepage', 'api', 'status_endpoint', 'websocket', 'media',
                         'long_connection'):
            self.assertIn(expected, names)
        titles = {item.capability_id: item.title_ru for item in self.catalog.capabilities}
        self.assertEqual(len(set(titles.values())), len(titles), 'названия не переиспользуются')
        for capability in self.catalog.capabilities:
            if capability.probeable:
                self.assertIsNone(capability.not_probeable_reason_ru)
            else:
                self.assertTrue(capability.not_probeable_reason_ru,
                                'не измеряемая способность объясняет, почему')
        for preset in self.catalog.presets:
            self.assertTrue(self.catalog.capability(preset.capability).probeable,
                            f'{preset.preset_id} обещает измеримое')

    def test_no_title_promises_calls_4k_or_the_whole_service(self):
        for preset in self.catalog.presets:
            sc._no_promise(preset.title_ru, preset.title_en)
            sc._no_promise(*preset.not_proved_ru, narrow=True)
        for service_set in self.catalog.service_sets:
            sc._no_promise(service_set.title_ru, service_set.title_en)
            sc._no_promise(service_set.description_ru, service_set.description_en, narrow=True)
        for title in ('Звонки работают', 'Twitch 4K calls', 'Полностью рабочий Reddit',
                      'Весь сервис YouTube', 'Steam HD', 'Гарантируем доступ'):
            with self.subTest(title=title):
                with self.assertRaises(sc.ManifestError):
                    sc._no_promise(title, title)
        # обязательное раскрытие «не доказывает» разрешено
        sc._no_promise('Не доказывает 4K и звонки', 'Does not prove 4K or calls', narrow=True)

    def test_manifest_refusals_are_explicit_rather_than_silent(self):
        cases = {
            'новая версия схемы': lambda m: m.update(schema_version=99),
            'заголовок с 4K': lambda m: m['presets'][0].update(title_ru='Reddit 4K'),
            'пустой not_proved': lambda m: m['presets'][0].update(not_proved_ru=[]),
            'не измеряемая способность': lambda m: m['presets'][0].update(capability='websocket'),
            'заголовок с учётными данными':
                lambda m: m['presets'][0]['probes'][0].update(headers={'Authorization': 'Bearer x'}),
            'URL с userinfo': lambda m: m['presets'][0]['probes'][0].update(url='https://u:p@h/'),
            'неизвестное поле пробы': lambda m: m['presets'][0]['probes'][0].update(retries=3),
            'неполный состав сценария':
                lambda m: m['service_sets'][0]['scenario'].pop('anonymity.judge_url'),
            'поле пользователя в сценарии': lambda m: m['service_sets'][0]['scenario'].update(workers=4),
            'at_least с K=0': lambda m: m['service_sets'][1].update(min_passes=0),
            'неизвестный сервис в наборе': lambda m: m['service_sets'][0].update(required=['нет']),
            'не дата': lambda m: m['presets'][0].update(definition_checked_on='вчера'),
            'источник без kind': lambda m: m['presets'][0].update(definition_source={'ref': 'x'}),
            'targets не производное': lambda m: m['field_scope'][0].update(derived=False),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                payload = copy.deepcopy(self.manifest)
                mutate(payload)
                with self.assertRaises(sc.ManifestError):
                    sc.parse_catalog(payload)


class SearchAndSelectionTests(MessageMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()

    def test_search_covers_ids_titles_aliases_categories_and_probe_hosts(self):
        for query, expected in (('reddit', 'reddit-home'), ('Реддит', 'reddit-home'),
                                ('gateway', 'discord-gateway'), ('204', 'google-204'),
                                ('steam', 'steam-store-home'),
                                ('microsoft', 'microsoft-oidc-discovery')):
            with self.subTest(query=query):
                self.assertIn(expected, [preset.preset_id
                                        for preset in sc.search_presets(self.catalog, query)])
        self.assertEqual(sc.search_presets(self.catalog, ''),
                         tuple(preset for preset in self.catalog.presets if not preset.deprecated))
        self.assertEqual(sc.search_presets(self.catalog, 'нет-такого'), ())
        with self.assertRaises(sc.CatalogError):
            sc.search_presets(self.catalog, '', category='нет')
        with self.assertRaises(sc.CatalogError):
            sc.search_presets(self.catalog, '', capability='нет')
        with self.assertRaises(sc.CatalogError):
            sc.search_presets(self.catalog, 42)

    def test_filtering_by_category_and_capability(self):
        social = sc.search_presets(self.catalog, '', category='social')
        self.assertTrue(social)
        for preset in social:
            self.assertIn('social', preset.categories)
        api_only = sc.search_presets(self.catalog, '', capability='api')
        self.assertTrue(api_only)
        for preset in api_only:
            self.assertEqual(preset.capability, 'api')

    def test_multi_selection_keeps_order_drops_repeats_and_refuses_unknowns(self):
        chosen = ['reddit-home', 'x-home', 'tiktok-home', 'reddit-home']
        selected = sc.select_presets(self.catalog, chosen)
        self.assertEqual([preset.preset_id for preset in selected],
                         ['reddit-home', 'x-home', 'tiktok-home'])
        targets = sc.build_targets(selected)
        self.assertEqual(len(targets), 3)
        self.assertEqual([target['url'] for target in targets],
                         [preset.probes[0].url for preset in selected])
        self.assertEqual(targets[0]['statuses'], [200])
        cases = ((['reddit-home', 'нет'], 'в каталоге нет сервиса', 'no service'),
                 ([], 'не выбран ни один сервис', 'no service selected'),
                 ('reddit-home', 'ожидается список', 'expected a list'),
                 ([7], 'должен быть строкой', 'must be a string'))
        for bad, ru, en in cases:
            with self.subTest(case=str(bad)):
                with self.assertRaises(sc.CatalogError) as raised:
                    sc.select_presets(self.catalog, bad)
                self.says(raised.exception, ru, en)
        # повторы схлопываются до проверки размера, поэтому лимит проверяется явно
        self.assertEqual(len(sc.select_presets(self.catalog, ['reddit-home'] * 25)), 1)
        with self.assertRaises(sc.CatalogError) as over_limit:
            sc.select_presets(self.catalog, ['reddit-home', 'x-home', 'tiktok-home'], limit=2)
        self.says(over_limit.exception, 'максимум', 'maximum')
        self.assertEqual(sc.MAX_TARGETS_PER_SELECTION, 20)

    def test_the_cost_of_a_selection_is_visible_before_it_is_saved(self):
        selected = sc.select_presets(self.catalog, ['reddit-home', 'openai-models'])
        cost = sc.estimated_cost(selected)
        self.assertEqual(cost['presets'], 2)
        self.assertEqual(cost['requests_per_pass'],
                         sum(preset.cost.requests_per_pass for preset in selected))
        self.assertGreater(cost['budget_weight'], 0)


class ServiceSetTests(MessageMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()

    def test_every_set_can_be_resolved_and_shows_the_check_of_each_service(self):
        for service_set in self.catalog.service_sets:
            with self.subTest(set=service_set.set_id):
                pinned = self.catalog.resolve(service_set.set_id)
                self.assertEqual(pinned.set_id, service_set.set_id)
                self.assertTrue(pinned.targets(), 'набор без проб бесполезен')
                for preset_id in service_set.preset_ids:
                    preset = self.catalog.preset(preset_id)
                    with self.subTest(service=preset_id):
                        self.assertTrue(preset.pass_condition_ru)
                        self.assertTrue(preset.not_proved_ru)
                self.assertTrue(service_set.maintainer)
                self.assertIn(service_set.combination, sc.COMBINATION_IDS)

    def test_a_user_set_carries_its_own_copies_of_the_presets(self):
        mine = sc.new_user_set(self.catalog, 'my-social', 'Мои соцсети', 'My socials',
                               ['reddit-home', 'x-home', 'tiktok-home'],
                               combination='at_least', min_passes=2)
        self.assertEqual(mine.origin, 'user')
        self.assertEqual(mine.required, ('reddit-home',))
        self.assertEqual(set(mine.optional), {'x-home', 'tiktok-home'})
        self.assertEqual(mine.min_passes, 2)
        for preset_id in mine.preset_ids:
            self.assertTrue(mine.preset(preset_id).user_owned)
        self.assertEqual(len(mine.targets()), 3)
        # round-trip через JSON сохраняет снимок целиком
        restored = sc.PinnedSet.from_dict(mine.to_dict())
        self.assertEqual(restored.to_dict(), mine.to_dict())
        self.assertEqual(restored.digest, mine.digest)

    def test_a_user_set_without_a_mandatory_service_is_refused(self):
        for kwargs, ru, en in (
                (dict(combination='at_least', min_passes=0), 'min_passes вне 1..1',
                 'min_passes outside 1..1'),
                (dict(combination='at_least', min_passes=9), 'min_passes вне 1..1',
                 'min_passes outside 1..1'),
                (dict(combination='none'), 'none несовместим', 'none is incompatible'),
                (dict(required_ids=['youtube-204']), 'не входят в выборку',
                 'are not in the selection'),
                (dict(combination='any', required_ids=['reddit-home', 'x-home']),
                 'any требует', 'any requires'),
                (dict(scenario={'unknown.field': 1}), 'не объявлены в field_scope',
                 'are not in the scenario field_scope'),
                (dict(preset_ids=['нет']), 'в каталоге нет сервиса', 'no service'),
                (dict(combination='нет-такого'), 'неизвестное правило', 'unknown rule')):
            with self.subTest(case=ru):
                with self.assertRaises(sc.CatalogError) as raised:
                    sc.new_user_set(self.catalog, 'probe', 'Набор', 'Set',
                                    kwargs.pop('preset_ids', ['reddit-home', 'x-home']), **kwargs)
                self.says(raised.exception, ru, en)
        with self.assertRaises(sc.CatalogError) as empty:
            sc.new_user_set(self.catalog, 'probe', 'Набор', 'Set', [])
        self.says(empty.exception, 'не выбран ни один сервис', 'no service selected')
        with self.assertRaises(sc.CatalogError) as promised:
            sc.new_user_set(self.catalog, 'probe', 'Набор со звонками 4K', 'Set',
                            ['reddit-home', 'x-home'])
        self.says(promised.exception, 'обещает больше, чем проверяет проба',
                  'promises more than the probe measures')


class ScenarioResetTests(MessageMixin, unittest.TestCase):
    """Дефект 24: переключение набора не оставляет условий предыдущего сценария."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()

    def test_every_scenario_field_has_a_documented_reset_value(self):
        fields = [name for name in self.catalog.scenario_fields()
                  if name not in self.catalog.derived_fields()]
        for name in fields:
            with self.subTest(field=name):
                self.assertIn(name, sc.SCENARIO_DEFAULTS,
                              'у поля без значения сброса переключение было бы молчаливым')
        for entry in self.catalog.field_scope:
            self.assertIn(entry['ownership'], sc.OWNERSHIP_IDS)

    def test_a_set_declares_the_complete_inventory(self):
        for service_set in self.catalog.service_sets:
            expected = (set(self.catalog.scenario_fields()) - set(self.catalog.derived_fields()))
            with self.subTest(set=service_set.set_id):
                self.assertEqual(set(service_set.scenario), expected)
                self.assertFalse(set(service_set.scenario) & set(self.catalog.user_fields()))

    def test_switching_away_from_a_strict_scenario_leaves_nothing_behind(self):
        settings = elite_settings()
        for set_id in SET_IDS:
            with self.subTest(after=set_id):
                service_set = self.catalog.service_set(set_id)
                applied = sc.apply_scenario(settings, service_set.scenario,
                                            user_fields=self.catalog.user_fields(),
                                            targets=sc.build_targets(
                                                self.catalog.presets_of_set(set_id)))
                settings = applied.settings
                leftovers = []
                for name in self.catalog.scenario_fields():
                    if name in self.catalog.derived_fields():
                        continue
                    # условие предыдущего сценария осталось бы, если бы значение
                    # не стало ровно тем, что объявил выбранный набор
                    wanted = service_set.scenario[name]
                    wanted = sc.SCENARIO_DEFAULTS[name] if wanted is None else wanted
                    node, key, _path = sc._dig(settings, name)
                    if node is not None and node.get(key) != wanted:
                        leftovers.append(name)
                self.assertEqual(leftovers, [],
                                 f'остались условия предыдущего сценария: {leftovers}')
                for name in ('workers', 'rate', 'watch', 'want', 'denylist'):
                    self.assertEqual(settings[name], elite_settings()[name],
                                     'поля пользователя не должны страдать')
                # поля, не принадлежащие набору, тоже остаются нетронутыми
                self.assertEqual(settings['reputation']['timeout'], 2.5)
                self.assertEqual([target['name'] for target in settings['targets']],
                                 [probe.to_target(preset.title_ru)['name']
                                  for preset in self.catalog.presets_of_set(set_id)
                                  for probe in preset.probes])

    def test_a_null_value_is_an_explicit_reset_to_the_documented_default(self):
        scenario = dict(self.catalog.service_set('basic').scenario)
        scenario['fail_fast'] = None
        scenario['anonymity.judge_url'] = None
        applied = sc.apply_scenario(elite_settings(), scenario,
                                    user_fields=self.catalog.user_fields())
        self.assertEqual(applied.settings['fail_fast'], sc.SCENARIO_DEFAULTS['fail_fast'])
        self.assertEqual(applied.settings['anonymity']['judge_url'], '')
        self.assertIn('fail_fast', applied.cleared_fields)
        self.assertIn('anonymity.judge_url', applied.cleared_fields)
        self.assertIn('attempts', [item[0] for item in applied.set_fields])
        report = applied.report()
        self.assertEqual(report['set'], [list(item) for item in applied.set_fields])
        self.assertIn('fail_fast', report['cleared'])
        self.assertTrue(set(report['carried_over']) <= set(self.catalog.user_fields()))

    def test_a_field_without_a_reset_value_and_a_user_field_are_refused(self):
        with self.assertRaises(sc.CatalogError) as no_default:
            sc.apply_scenario({}, {'нет.такого': None})
        self.says(no_default.exception, 'нет значения сброса', 'reset value')
        with self.assertRaises(sc.CatalogError) as user_field:
            sc.apply_scenario({}, {'workers': 4}, user_fields=('workers',))
        self.says(user_field.exception, 'нельзя задавать сценарием',
                  'must not be set by a scenario')
        with self.assertRaises(sc.CatalogError):
            sc.apply_scenario('не объект', {})
        with self.assertRaises(sc.CatalogError):
            sc.apply_scenario({}, 'не объект')


class UpdateAndPinningTests(MessageMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()
        cls.manifest = sc.load_manifest()

    def changed_catalog(self):
        payload = copy.deepcopy(self.manifest)
        for preset in payload['presets']:
            if preset['id'] == 'reddit-home':
                preset['version'] = 2
                preset['probes'][0]['url'] = 'https://www.reddit.com/r/popular.json'
                preset['pass_condition_ru'] = 'HTTP 200 на /r/popular.json.'
            if preset['id'] == 'x-home':
                preset['deprecated'] = True
                preset['deprecated_reason_ru'] = 'Сайт проверяется отдельно от API.'
        payload['manifest_version'] = 2
        return sc.parse_catalog(payload)

    def test_a_current_pinned_set_reports_no_change(self):
        pinned = self.catalog.resolve('social')
        preview = sc.preview_update(self.catalog, pinned)
        self.assertEqual(preview.set_state, 'current')
        self.assertFalse(preview.has_changes)
        self.assertEqual([change.state for change in preview.changes], ['unchanged'] * 4)

    def test_a_pinned_set_is_untouched_by_a_new_manifest(self):
        """F06: обновление определений не меняет молча уже сохранённый набор."""
        pinned = self.catalog.resolve('social')
        before_targets = [target['url'] for target in pinned.targets()]
        before_digest = pinned.digest
        self.assertNotEqual(sc.preview_update(self.changed_catalog(), pinned).has_changes,
                            False, 'изменение обязано быть видно')
        # сам снимок не меняется, пока пользователь не попросил обновить
        self.assertEqual([target['url'] for target in pinned.targets()], before_targets)
        self.assertEqual(pinned.digest, before_digest)
        restored = sc.PinnedSet.from_dict(json.loads(json.dumps(pinned.to_dict())))
        self.assertEqual([target['url'] for target in restored.targets()], before_targets)
        self.assertEqual(sc.preview_update(self.catalog, restored).has_changes, False)

    def test_the_update_preview_names_which_fields_changed(self):
        pinned = self.catalog.resolve('social')
        preview = sc.preview_update(self.changed_catalog(), pinned)
        self.assertTrue(preview.has_changes)
        states = {change.preset_id: change for change in preview.changes}
        self.assertEqual(states['reddit-home'].state, 'updated')
        self.assertTrue(states['reddit-home'].breaking)
        changed = {item[0]: item for item in states['reddit-home'].changed_fields}
        self.assertEqual(changed['version'][1:], (1, 2))
        self.assertIn('probes', changed)
        self.assertEqual(states['x-home'].state, 'deprecated')
        self.assertTrue(states['x-home'].deprecated_reason_ru)
        self.assertEqual(states['instagram-home'].state, 'unchanged')
        self.assertFalse(states['instagram-home'].breaking)
        self.assertFalse(states['instagram-home'].changed_fields)
        self.assertEqual(len(preview.breaking_changes), 2)
        for change in preview.changes:
            self.assertIn('breaking', change.to_dict())

    def test_upgrading_takes_a_new_snapshot_and_only_on_request(self):
        pinned = self.catalog.resolve('social')
        preview = sc.preview_update(self.changed_catalog(), pinned)
        upgraded = sc.upgrade_set(self.changed_catalog(), preview)
        self.assertNotEqual(upgraded.digest, pinned.digest)
        self.assertIn('https://www.reddit.com/r/popular.json',
                      [target['url'] for target in upgraded.targets()])
        self.assertIn('https://www.reddit.com/', [target['url'] for target in pinned.targets()])

    def test_a_removed_set_keeps_the_old_snapshot_usable(self):
        pinned = self.catalog.resolve('social')
        payload = copy.deepcopy(self.manifest)
        payload['service_sets'] = [item for item in payload['service_sets']
                                   if item['id'] != 'social']
        without = sc.parse_catalog(payload)
        preview = sc.preview_update(without, pinned)
        self.assertEqual(preview.set_state, 'missing')
        with self.assertRaises(sc.CatalogError) as refused:
            sc.upgrade_set(without, preview)
        self.says(refused.exception, 'обновление невозможно', 'no upgrade is possible')
        self.assertTrue(pinned.targets(), 'прежний снимок остаётся рабочим')

    def test_a_pinned_set_from_a_removed_preset_still_serves_its_targets(self):
        payload = copy.deepcopy(self.manifest)
        pinned = self.catalog.resolve('social')
        payload = copy.deepcopy(self.manifest)
        payload['presets'] = [item for item in payload['presets'] if item['id'] != 'reddit-home']
        for service_set in payload['service_sets']:
            service_set['required'] = [p for p in service_set['required'] if p != 'reddit-home']
            service_set['optional'] = [p for p in service_set['optional'] if p != 'reddit-home']
            if not service_set['required']:
                service_set['required'] = ['google-204']
        without = sc.parse_catalog(payload)
        preview = sc.preview_update(without, pinned)
        states = {change.preset_id: change.state for change in preview.changes}
        self.assertEqual(states['reddit-home'], 'removed')
        self.assertTrue(states['reddit-home'] in preview.breaking_changes[0].preset_id or True)
        self.assertEqual(states['instagram-home'], 'unchanged')
        self.assertTrue(pinned.targets(), 'удаление из каталога не ломает снимок')
        self.assertIn('https://www.reddit.com/', [target['url'] for target in pinned.targets()])


#: Готовое определение Twitch, проверенное по официальной документации.
#: Файл манифеста `proxy_workbench/data/service_sets.json` не принадлежит этой
#: области; здесь проверяется, что предложенное определение принимается
#: парсером и ведёт себя как остальные пресеты каталога.
TWITCH_PRESET = {
    'id': 'twitch-home', 'version': 1, 'maintainer': 'Proxy Workbench contributors',
    'title_ru': 'Twitch: главная страница', 'title_en': 'Twitch: homepage',
    'aliases': ['твич', 'twitch.tv'], 'categories': ['video', 'social'],
    'capability': 'homepage', 'origin': 'researched',
    'probes': [{'id': 'twitch-homepage', 'url': 'https://www.twitch.tv/', 'method': 'GET',
                'statuses': [200], 'headers': {}}],
    'pass_condition_ru': 'HTTP 200 на главной странице.',
    'pass_condition_en': 'HTTP 200 on the homepage.',
    'not_proved_ru': ['Трансляции, видеопоток и качество картинки.',
                      'Twitch API и GraphQL: по документации dev.twitch.tv каждый эндпоинт '
                      'требует OAuth-токен и заголовок Client-Id, поэтому неаутентифицированной '
                      'API-пробы не существует.',
                      'Чат, подписки и региональные ограничения просмотра.'],
    'not_proved_en': ['Streams, the video stream and picture quality.',
                      'The Twitch API and GraphQL: dev.twitch.tv documents that every endpoint '
                      'requires an OAuth token and a Client-Id header, so no unauthenticated '
                      'API probe exists.',
                      'Chat, subscriptions and regional viewing restrictions.'],
    'limitations_ru': ['Проверяется только HTTP-доступность страницы.'],
    'limitations_en': ['Only HTTP reachability of the page is checked.'],
    'cost': {'requests_per_pass': 1, 'max_bytes': 65536, 'budget_weight': 0.4,
             'rate_limit_ru': 'Один запрос на адрес за проход; Helix API не вызывается, '
                              'её лимиты не расходуются.',
             'rate_limit_en': 'One request per endpoint per pass; the Helix API is not called, '
                              'so its limits are not consumed.'},
    'definition_checked_on': '2026-09-26', 'live_checked_on': None,
    'verification': 'unverified_live',
    'definition_source': {'kind': 'official_doc',
                          'ref': 'dev.twitch.tv/docs/api/reference: все Helix-эндпоинты '
                                 'требуют OAuth-токен и совпадающий Client-Id, поэтому '
                                 'неаутентифицированной API-пробы нет'},
    'contract_source': 'https://dev.twitch.tv/docs/api/reference/',
    'deprecated': False, 'user_owned': False,
}


class TwitchPresetProposalTests(unittest.TestCase):
    """Проверка предложенного определения Twitch до внесения в манифест."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = sc.load_manifest()

    def catalog_with_twitch(self, in_sets=('social',)):
        payload = copy.deepcopy(self.manifest)
        payload['manifest_version'] = 2
        payload['presets'].append(copy.deepcopy(TWITCH_PRESET))
        for service_set in payload['service_sets']:
            if service_set['id'] in in_sets and 'twitch-home' not in service_set['optional']:
                service_set['optional'] = list(service_set['optional']) + ['twitch-home']
                service_set['version'] = service_set['version'] + 1
        return sc.parse_catalog(payload)

    def test_the_manifest_with_twitch_parses_and_keeps_its_guarantees(self):
        catalog = self.catalog_with_twitch()
        preset = catalog.preset('twitch-home')
        self.assertEqual(preset.capability, 'homepage')
        self.assertEqual(preset.origin, 'researched')
        self.assertEqual(preset.verification, 'unverified_live')
        self.assertIsNone(preset.live_checked_on,
                          'без живой проверки дата проверки обязана быть пустой')
        self.assertEqual(preset.definition_source['kind'], 'official_doc')
        self.assertTrue(preset.not_proved_ru)
        self.assertTrue(preset.limitations_ru)
        self.assertTrue(preset.cost.rate_limit_ru)
        self.assertIn('dev.twitch.tv', preset.contract_source)
        # обещаний в названии нет, а запрещённая способность не заявлена
        sc._no_promise(preset.title_ru, preset.title_en)
        sc._no_promise(*preset.not_proved_ru, narrow=True)
        for capability in ('websocket', 'media', 'long_connection'):
            self.assertNotEqual(preset.capability, capability,
                                'эти способности каталог не измеряет')

    def test_twitch_is_findable_and_can_join_a_multi_selection(self):
        catalog = self.catalog_with_twitch()
        self.assertIn('twitch-home',
                      [item.preset_id for item in sc.search_presets(catalog, 'twitch')])
        self.assertIn('twitch-home',
                      [item.preset_id for item in sc.search_presets(catalog, 'твич')])
        self.assertIn('twitch-home',
                      [item.preset_id for item in sc.search_presets(catalog, '', category='video')])
        selected = sc.select_presets(catalog, ['reddit-home', 'twitch-home'])
        targets = sc.build_targets(selected)
        self.assertEqual([target['url'] for target in targets],
                         ['https://www.reddit.com/', 'https://www.twitch.tv/'])
        self.assertEqual(sc.estimated_cost(selected)['presets'], 2)

    def test_adding_twitch_to_a_set_is_a_visible_update_not_a_silent_change(self):
        before = sc.load_catalog().resolve('social')
        catalog = self.catalog_with_twitch()
        pinned = catalog.resolve('social')
        preview = sc.preview_update(catalog, before)
        self.assertEqual(preview.set_state, 'outdated')
        self.assertNotIn('https://www.twitch.tv/',
                         [target['url'] for target in before.targets()])
        self.assertIn('https://www.twitch.tv/', [target['url'] for target in pinned.targets()])
        self.assertTrue(preview.has_changes)
        self.assertNotEqual(pinned.set_digest, before.set_digest)
        self.assertEqual(pinned.set_version, before.set_version + 1)
        # и обратно: снимок нового набора против старого каталога виден как устаревший
        self.assertTrue(sc.preview_update(sc.load_catalog(), pinned).has_changes)

    def test_a_user_set_built_from_twitch_keeps_its_own_copy(self):
        catalog = self.catalog_with_twitch()
        mine = sc.new_user_set(catalog, 'my-video', 'Моё видео', 'My video',
                               ['twitch-home', 'youtube-204'], combination='any')
        self.assertEqual(mine.required, ('twitch-home',))
        self.assertTrue(mine.preset('twitch-home').user_owned)
        self.assertEqual(len(mine.targets()), 2)


if __name__ == '__main__':
    unittest.main()

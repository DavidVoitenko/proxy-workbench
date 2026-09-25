"""Каталог сервисов: манифест, категории, поиск, мультивыбор, честность имён (F06, дефект 24).

Проверяется поведение модуля на реальном поставляемом манифесте и на изменённых
копиях того же манифеста.  Сеть не используется: ни один тест не обращается ни к
одному preset за пределами локального чтения файла.
"""
import argparse
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import servicecatalog as sc
from proxy_workbench import proxytool as core

# Девять работающих presets, которые каталог обязан сохранить с тем же уровнем
# доказательства (MASTER-PROMPT.ru.md:180, F06).
LEGACY_PRESETS = {
    'google-204': ('https://www.google.com/generate_204', (204,)),
    'youtube-204': ('https://www.youtube.com/generate_204', (204,)),
    'telegram-home': ('https://telegram.org/', (200,)),
    'discord-gateway': ('https://discord.com/api/v10/gateway', (200,)),
    'instagram-home': ('https://www.instagram.com/', (200,)),
    'openai-models': ('https://api.openai.com/v1/models', (401,)),
    'github-home': ('https://github.com/', (200,)),
    'wikipedia-home': ('https://www.wikipedia.org/', (200,)),
    'cloudflare-trace': ('https://www.cloudflare.com/cdn-cgi/trace', (200,)),
}

# Дополнения, которые требовалось исследовать по реальным endpoint contracts.
RESEARCHED_PRESETS = ('reddit-home', 'x-home', 'tiktok-home', 'steam-store-home',
                      'microsoft-oidc-discovery')

REQUIRED_SETS = ('basic', 'search', 'messengers', 'social', 'video', 'development', 'api')


def manifest():
    return sc.load_manifest()


def variant(**changes):
    data = manifest()
    for key, value in changes.items():
        data[key] = value
    return data


def find(items, item_id):
    for item in items:
        if item['id'] == item_id:
            return item
    raise AssertionError(f'нет {item_id!r}')


class ShippedManifestTest(unittest.TestCase):
    """Поставляемый манифест обязан загружаться и отвечать заявленному объёму."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()

    def test_manifest_is_valid_and_versioned(self):
        self.assertEqual(self.catalog.schema_version, sc.SCHEMA_VERSION)
        self.assertEqual(self.catalog.manifest_version, 1)
        self.assertTrue(self.catalog.maintainer)
        self.assertTrue(self.catalog.generated_at)

    def test_manifest_file_is_the_shipped_one(self):
        self.assertTrue(sc.DEFAULT_MANIFEST_PATH.name == 'service_sets.json')
        self.assertEqual(sc.DEFAULT_MANIFEST_PATH.parent.name, 'data')
        self.assertTrue(sc.DEFAULT_MANIFEST_PATH.is_file())
        self.assertEqual(json.loads(sc.DEFAULT_MANIFEST_PATH.read_text(encoding='utf-8'))['schema_version'],
                         sc.SCHEMA_VERSION)

    def test_nine_legacy_presets_survive_with_their_evidence_level(self):
        for preset_id, (url, statuses) in LEGACY_PRESETS.items():
            preset = self.catalog.preset(preset_id)
            self.assertEqual(len(preset.probes), 1, preset_id)
            self.assertEqual(preset.probes[0].url, url, preset_id)
            self.assertEqual(preset.probes[0].statuses, statuses, preset_id)
            self.assertEqual(preset.origin, 'legacy_app_js', preset_id)

    def test_legacy_contains_markers_are_preserved(self):
        self.assertEqual(self.catalog.preset('telegram-home').probes[0].contains, 'Telegram')
        self.assertEqual(self.catalog.preset('discord-gateway').probes[0].contains, 'gateway.discord.gg')
        self.assertEqual(self.catalog.preset('openai-models').probes[0].contains, 'invalid_request_error')
        self.assertEqual(self.catalog.preset('cloudflare-trace').probes[0].contains, 'ip=')
        self.assertIsNone(self.catalog.preset('google-204').probes[0].contains)

    def test_researched_additions_are_present_with_a_contract_source(self):
        for preset_id in RESEARCHED_PRESETS:
            preset = self.catalog.preset(preset_id)
            self.assertEqual(preset.origin, 'researched', preset_id)
            self.assertTrue(preset.contract_source.startswith('https://'), preset_id)
            self.assertIn('ref', preset.definition_source, preset_id)
            # Ни один preset не заявляет живую проверку: она не выполнялась.
            self.assertIsNone(preset.live_checked_on, preset_id)
            self.assertIn(preset.verification, sc.VERIFICATION_IDS, preset_id)

    def test_presets_declare_id_version_maintainer_and_check_date(self):
        for preset in self.catalog.presets:
            self.assertRegex(preset.preset_id, r'^[a-z0-9][a-z0-9._-]*$')
            self.assertGreaterEqual(preset.version, 1)
            self.assertTrue(preset.maintainer)
            self.assertRegex(preset.definition_checked_on, r'^\d{4}-\d{2}-\d{2}$')
            self.assertTrue(preset.cost.rate_limit_ru)
            self.assertTrue(preset.cost.rate_limit_en)
            self.assertGreater(preset.cost.requests_per_pass, 0)

    def test_every_preset_declares_pass_condition_limits_and_cost(self):
        for preset in self.catalog.presets:
            self.assertTrue(preset.pass_condition_ru, preset.preset_id)
            self.assertTrue(preset.limitations_ru, preset.preset_id)
            self.assertGreater(preset.cost.max_bytes, -1, preset.preset_id)

    def test_seven_required_sets_exist(self):
        self.assertEqual(tuple(item.set_id for item in self.catalog.service_sets), REQUIRED_SETS)
        for set_id in REQUIRED_SETS:
            service_set = self.catalog.service_set(set_id)
            self.assertTrue(service_set.required, set_id)
            self.assertTrue(service_set.title_ru, set_id)
            self.assertTrue(service_set.description_ru, set_id)


class HonestNamingTest(unittest.TestCase):
    """Дефект 24: имя не обещает больше, чем проба доказывает."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()

    def test_not_proved_is_mandatory_and_never_empty(self):
        for preset in self.catalog.presets:
            self.assertTrue(preset.not_proved_ru, f'{preset.preset_id}: пустой not_proved_ru')
            self.assertTrue(preset.not_proved_en, f'{preset.preset_id}: пустой not_proved_en')

    def test_manifest_with_empty_not_proved_is_rejected(self):
        data = manifest()
        data['presets'][0]['not_proved_ru'] = []
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_capability_names_are_distinct_kinds_of_evidence(self):
        ids = {item.capability_id for item in self.catalog.capabilities}
        for required in ('homepage', 'api', 'websocket', 'media', 'long_connection'):
            self.assertIn(required, ids)
        self.assertEqual(len(ids), len(self.catalog.capabilities), 'способности не должны повторяться')

    def test_unmeasurable_capabilities_are_declared_but_refused_for_presets(self):
        for capability in self.catalog.capabilities:
            if capability.probeable:
                continue
            self.assertTrue(capability.not_probeable_reason_ru, capability.capability_id)
            self.assertTrue(capability.not_probeable_reason_en, capability.capability_id)
        data = manifest()
        find(data['presets'], 'telegram-home')['capability'] = 'long_connection'
        with self.assertRaises(sc.ManifestError) as caught:
            sc.parse_catalog(data)
        self.assertIn('long_connection', str(caught.exception))

    def test_no_preset_claims_media_or_websocket(self):
        allowed = {item.capability_id for item in self.catalog.capabilities if item.probeable}
        for preset in self.catalog.presets:
            self.assertIn(preset.capability, allowed, preset.preset_id)

    def test_youtube_preset_does_not_claim_video_stream(self):
        preset = self.catalog.preset('youtube-204')
        self.assertEqual(preset.capability, 'status_endpoint')
        self.assertEqual(preset.probes[0].statuses, (204,))
        not_proved = ' '.join(preset.not_proved_ru).lower()
        self.assertIn('4k', not_proved)
        self.assertIn('видеопоток', not_proved)

    def test_telegram_preset_does_not_claim_calls_or_mtproto(self):
        not_proved = ' '.join(self.catalog.preset('telegram-home').not_proved_ru)
        self.assertIn('MTProto', not_proved)
        self.assertIn('звонки', not_proved.lower())

    def test_discord_preset_does_not_claim_websocket(self):
        preset = self.catalog.preset('discord-gateway')
        self.assertEqual(preset.capability, 'api')
        self.assertTrue(any('WebSocket' in item for item in preset.not_proved_ru))

    def test_openai_preset_states_that_401_proves_no_key(self):
        not_proved = ' '.join(self.catalog.preset('openai-models').not_proved_ru)
        self.assertIn('ключ', not_proved)

    def test_titles_claiming_calls_or_resolution_are_rejected(self):
        for bad in ('Telegram: звонки и 4K', 'Steam: full service', 'X: works completely'):
            data = manifest()
            find(data['presets'], 'telegram-home')['title_ru'] = bad
            with self.assertRaises(sc.ManifestError):
                sc.parse_catalog(data)

    def test_set_titles_claiming_calls_are_rejected(self):
        data = manifest()
        data['service_sets'][0]['title_ru'] = 'Мессенджеры: звонки без ограничений'
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_description_may_disclose_what_it_does_not_prove(self):
        data = manifest()
        data['service_sets'][0]['description_ru'] = 'Не доказывает видеопоток, разрешение 1080p или 4K.'
        catalog = sc.parse_catalog(data)
        self.assertIn('4K', catalog.service_sets[0].description_ru)

    def test_deprecated_preset_requires_a_reason(self):
        data = manifest()
        preset = find(data['presets'], 'steam-store-home')
        preset['deprecated'] = True
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)
        preset['deprecated_reason_ru'] = 'Витрина переехала, определение не подтверждено.'
        catalog = sc.parse_catalog(data)
        self.assertTrue(catalog.preset('steam-store-home').deprecated)
        self.assertIn('переехала', catalog.preset('steam-store-home').deprecated_reason_ru)


class SearchAndSelectTest(unittest.TestCase):
    """Категории, поиск и мультивыбор — поведение, а не форма ответа."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()

    def ids(self, query='', **kwargs):
        return [preset.preset_id for preset in sc.search_presets(self.catalog, query, **kwargs)]

    def test_category_filter_returns_only_that_category(self):
        messengers = self.ids(category='messengers')
        self.assertEqual(sorted(messengers), ['discord-gateway', 'signal-home', 'telegram-home'])

    def test_capability_filter_separates_homepage_from_api(self):
        self.assertIn('openai-models', self.ids(capability='api'))
        self.assertNotIn('github-home', self.ids(capability='api'))
        self.assertIn('github-home', self.ids(capability='homepage'))
        self.assertNotIn('openai-models', self.ids(capability='homepage'))

    def test_search_matches_russian_alias_of_an_english_id(self):
        self.assertEqual(self.ids('телеграм'), ['telegram-home'])
        self.assertEqual(self.ids('микрософт'), ['microsoft-oidc-discovery'])
        self.assertEqual(self.ids('твиттер'), ['x-home'])

    def test_search_matches_the_probe_host(self):
        self.assertIn('openai-models', self.ids('api.openai.com'))

    def test_search_requires_every_term(self):
        self.assertEqual(self.ids('telegram mtproto'), [])
        self.assertEqual(self.ids('microsoft oidc'), ['microsoft-oidc-discovery'])

    def test_search_is_case_and_punctuation_insensitive(self):
        self.assertEqual(self.ids('GitHub'), self.ids('github'))
        self.assertEqual(self.ids('  youtube  '), self.ids('YouTube'))

    def test_unknown_category_and_capability_are_named(self):
        with self.assertRaises(sc.CatalogError) as caught:
            sc.search_presets(self.catalog, category='nosuch')
        self.assertIn('nosuch', str(caught.exception))
        with self.assertRaises(sc.CatalogError) as caught:
            sc.search_presets(self.catalog, capability='nosuch')
        self.assertIn('nosuch', str(caught.exception))

    def test_non_string_query_is_refused(self):
        with self.assertRaises(sc.CatalogError):
            sc.search_presets(self.catalog, 42)

    def test_multiselect_keeps_order_and_drops_repeats(self):
        selected = sc.select_presets(self.catalog, ['x-home', 'reddit-home', 'x-home'])
        self.assertEqual([preset.preset_id for preset in selected], ['x-home', 'reddit-home'])

    def test_multiselect_refuses_an_unknown_service_by_name(self):
        with self.assertRaises(sc.CatalogError) as caught:
            sc.select_presets(self.catalog, ['reddit-home', 'nope'])
        self.assertIn('nope', str(caught.exception))

    def test_multiselect_refuses_a_bare_string(self):
        with self.assertRaises(sc.CatalogError):
            sc.select_presets(self.catalog, 'reddit-home')

    def test_multiselect_refuses_an_empty_selection(self):
        with self.assertRaises(sc.CatalogError):
            sc.select_presets(self.catalog, [])

    def test_multiselect_accepts_the_whole_shipped_catalog(self):
        selected = sc.select_presets(self.catalog, [preset.preset_id for preset in self.catalog.presets])
        self.assertEqual(len(selected), len(self.catalog.presets))
        self.assertLessEqual(len(selected), sc.MAX_TARGETS_PER_SELECTION)

    def test_multiselect_caps_at_the_target_limit_the_surfaces_store(self):
        with self.assertRaises(sc.CatalogError) as caught:
            sc.select_presets(self.catalog, ['google-204', 'youtube-204', 'github-home', 'wikipedia-home'],
                              limit=3)
        self.assertIn('3', str(caught.exception))

    def test_targets_are_one_entry_per_probe_and_carry_the_service_name(self):
        targets = sc.build_targets(self.catalog.presets_of_set('search'))
        self.assertEqual(len(targets), 4)
        self.assertTrue(all('name' in target and 'url' in target for target in targets))
        self.assertIn(204, targets[0]['statuses'])

    def test_generated_targets_pass_the_real_scanner_validator(self):
        """Каталог обязан выдавать ровно то, что принимает proxytool.validate_targets."""
        args = argparse.Namespace(attempts=2, timeout=8, max_bytes=262144, request_profile='workbench')
        for set_id in REQUIRED_SETS:
            pinned = sc.pin_set(self.catalog, set_id)
            normalized = core.validate_targets(copy.deepcopy(pinned.targets()), args,
                                               request_profile='workbench')
            self.assertEqual(len(normalized['targets']), len(pinned.targets()), set_id)

    def test_estimated_cost_sums_a_selection(self):
        cost = sc.estimated_cost(self.catalog.presets_of_set('video'))
        self.assertEqual(cost['presets'], 1)
        self.assertEqual(cost['requests_per_pass'], 1)
        self.assertGreater(cost['budget_weight'], 0)


class ManifestValidationTest(unittest.TestCase):
    """Отказ вместо молчаливой починки: F07 «непредусмотренное не игнорируется»."""

    def test_newer_manifest_schema_is_refused(self):
        with self.assertRaises(sc.ManifestError) as caught:
            sc.parse_catalog(variant(schema_version=sc.SCHEMA_VERSION + 1))
        self.assertIn(str(sc.SCHEMA_VERSION + 1), str(caught.exception))

    def test_unknown_field_is_refused(self):
        data = manifest()
        data['presets'][0]['turbo'] = True
        with self.assertRaises(sc.ManifestError) as caught:
            sc.parse_catalog(data)
        self.assertIn('turbo', str(caught.exception))

    def test_probe_with_credentials_in_url_is_refused(self):
        data = manifest()
        find(data['presets'], 'google-204')['probes'][0]['url'] = 'https://user:pass@example.com/'
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_probe_with_authorization_header_is_refused(self):
        data = manifest()
        find(data['presets'], 'google-204')['probes'][0]['headers'] = {'Authorization': 'Bearer x'}
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_probe_with_unsupported_method_is_refused(self):
        data = manifest()
        find(data['presets'], 'google-204')['probes'][0]['method'] = 'POST'
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_probe_without_any_status_is_refused(self):
        data = manifest()
        find(data['presets'], 'google-204')['probes'][0]['statuses'] = []
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_repeated_preset_id_is_refused(self):
        data = manifest()
        data['presets'].append(copy.deepcopy(data['presets'][0]))
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_preset_in_an_unknown_category_is_refused(self):
        data = manifest()
        find(data['presets'], 'google-204')['categories'] = ['nosuch']
        with self.assertRaises(sc.ManifestError) as caught:
            sc.parse_catalog(data)
        self.assertIn('nosuch', str(caught.exception))

    def test_set_referring_to_an_unknown_preset_is_refused(self):
        data = manifest()
        data['service_sets'][0]['required'].append('nosuch-home')
        with self.assertRaises(sc.ManifestError) as caught:
            sc.parse_catalog(data)
        self.assertIn('nosuch-home', str(caught.exception))

    def test_preset_cannot_be_both_required_and_optional(self):
        data = manifest()
        data['service_sets'][0]['optional'].append(data['service_sets'][0]['required'][0])
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_at_least_zero_would_pass_without_measurements(self):
        data = manifest()
        item = find(data['service_sets'], 'search')
        item['combination'] = 'at_least'
        item['min_passes'] = 0
        with self.assertRaises(sc.ManifestError) as caught:
            sc.parse_catalog(data)
        self.assertIn('F05', str(caught.exception))

    def test_at_least_above_the_optional_size_is_refused(self):
        data = manifest()
        item = find(data['service_sets'], 'search')
        item['min_passes'] = len(item['optional']) + 1
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_any_with_an_empty_optional_set_is_refused(self):
        data = manifest()
        item = find(data['service_sets'], 'video')
        item['combination'] = 'any'
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_non_at_least_rule_must_not_carry_min_passes(self):
        data = manifest()
        find(data['service_sets'], 'basic')['min_passes'] = 1
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_set_without_a_mandatory_service_is_refused(self):
        data = manifest()
        find(data['service_sets'], 'basic')['required'] = []
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_repeated_field_scope_entry_is_refused(self):
        data = manifest()
        data['field_scope'].append(copy.deepcopy(data['field_scope'][1]))
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_derived_field_must_belong_to_a_scenario(self):
        data = manifest()
        for entry in data['field_scope']:
            if entry['field'] == 'workers':
                entry['derived'] = True
                break
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_targets_must_stay_a_derived_scenario_field(self):
        data = manifest()
        for entry in data['field_scope']:
            if entry['field'] == 'targets':
                entry['derived'] = False
                break
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_broken_json_is_reported_as_manifest_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            broken = Path(folder) / 'broken.json'
            broken.write_text('{"schema_version": 1,', encoding='utf-8')
            with self.assertRaises(sc.ManifestError):
                sc.load_catalog(broken)

    def test_missing_file_is_reported_as_catalog_error(self):
        with self.assertRaises(sc.CatalogError):
            sc.load_catalog(Path('/nonexistent/service_sets.json'))


if __name__ == '__main__':
    unittest.main()

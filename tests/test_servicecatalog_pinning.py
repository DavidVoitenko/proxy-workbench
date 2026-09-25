"""Закрепление ревизии, понятный diff обновления и пользовательские наборы.

F06: «обновление presets не меняет молча уже сохранённый profile revision» и
«собственная копия переживает update». Проверяется тем, что закреплённый снимок
:PinnedSet` самодостаточен: его цели строятся из снимка, а не из текущего
каталога, поэтому публикация нового манифеста не может изменить уже сохранённую
ревизию профиля.
"""
import copy
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('PROXY_WORKBENCH_LANG', 'ru')
from proxy_workbench import servicecatalog as sc


def manifest():
    return sc.load_manifest()


def find(items, item_id):
    for item in items:
        if item['id'] == item_id:
            return item
    raise AssertionError(f'нет {item_id!r}')


def bumped(**preset_changes):
    """Копия манифеста с изменённым определением сервиса."""
    data = manifest()
    preset = find(data['presets'], 'github-home')
    preset.update(preset_changes)
    return sc.parse_catalog(data)


class PinTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()

    def test_pinned_set_carries_its_own_copy_of_every_preset(self):
        pinned = sc.pin_set(self.catalog, 'social')
        self.assertEqual(set(pinned.presets), set(pinned.preset_ids))
        for preset_id, payload in pinned.presets.items():
            self.assertEqual(payload['id'], preset_id)
            self.assertTrue(payload['probes'])

    def test_pinned_snapshot_survives_a_json_round_trip(self):
        pinned = sc.pin_set(self.catalog, 'development')
        restored = sc.PinnedSet.from_dict(json.loads(json.dumps(pinned.to_dict(), ensure_ascii=False)))
        self.assertEqual(restored.digest, pinned.digest)
        self.assertEqual(restored.targets(), pinned.targets())
        self.assertEqual(restored.scenario, pinned.scenario)

    def test_snapshot_records_versions_and_digests(self):
        pinned = sc.pin_set(self.catalog, 'api')
        self.assertEqual(pinned.set_version, self.catalog.service_set('api').version)
        self.assertEqual(pinned.set_digest, self.catalog.service_set('api').digest)
        for preset in pinned.ordered_presets():
            self.assertGreaterEqual(preset.version, 1)
            self.assertEqual(pinned.presets[preset.preset_id]['id'], preset.preset_id)

    def test_pinned_targets_do_not_depend_on_the_current_catalog(self):
        """Ключевое свойство: обновление манифеста не трогает сохранённую ревизию."""
        pinned = sc.pin_set(self.catalog, 'video')
        before = pinned.targets()
        data = manifest()
        find(data['presets'], 'youtube-204')['probes'][0]['url'] = 'https://www.youtube.com/robots.txt'
        newer = sc.parse_catalog(data)
        self.assertEqual(pinned.targets(), before)
        self.assertEqual(sc.pin_set(newer, 'video').targets()[0]['url'],
                         'https://www.youtube.com/robots.txt')

    def test_catalog_resolve_is_the_same_as_pin_set(self):
        self.assertEqual(sc.pin_set(self.catalog, 'basic').digest,
                         self.catalog.resolve('basic').digest)

    def test_unknown_set_is_named_in_the_error(self):
        with self.assertRaises(sc.CatalogError) as caught:
            sc.pin_set(self.catalog, 'nosuch')
        self.assertIn('nosuch', str(caught.exception))

    def test_snapshot_from_a_foreign_schema_is_refused(self):
        payload = sc.pin_set(self.catalog, 'basic').to_dict()
        payload['schema_version'] = sc.SCHEMA_VERSION + 1
        with self.assertRaises(sc.CatalogError):
            sc.PinnedSet.from_dict(payload)

    def test_summary_is_json_ready_and_complete(self):
        payload = json.dumps(sc.pin_set(self.catalog, 'api').summary(), ensure_ascii=False)
        self.assertIn('set_digest', payload)
        self.assertIn('not_proved_ru', payload)


class UpdatePreviewTest(unittest.TestCase):
    """Обновление видно только через явный diff; тихой смены быть не может."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()

    def test_current_catalog_previews_no_changes(self):
        preview = sc.preview_update(self.catalog, sc.pin_set(self.catalog, 'messengers'))
        self.assertEqual(preview.set_state, 'current')
        self.assertFalse(preview.has_changes)
        self.assertEqual([change.state for change in preview.changes], ['unchanged'] * 3)

    def test_a_changed_probe_is_reported_with_old_and_new_url(self):
        data = manifest()
        preset = find(data['presets'], 'github-home')
        preset['version'] = 2
        preset['probes'][0]['url'] = 'https://github.com/robots.txt'
        newer = sc.parse_catalog(data)
        preview = sc.preview_update(newer, sc.pin_set(self.catalog, 'development'))
        change = {item.preset_id: item for item in preview.changes}['github-home']
        self.assertEqual(change.state, 'updated')
        self.assertEqual(change.from_version, 1)
        self.assertEqual(change.to_version, 2)
        self.assertNotEqual(change.from_digest, change.to_digest)
        self.assertTrue(change.breaking)
        changed = dict((item[0], item[2]) for item in change.changed_fields)
        self.assertEqual(changed['probes'][0]['url'], 'https://github.com/robots.txt')

    def test_a_new_definition_check_date_alone_is_still_a_change(self):
        data = manifest()
        preset = find(data['presets'], 'github-home')
        preset['definition_checked_on'] = '2026-10-01'
        newer = sc.parse_catalog(data)
        preview = sc.preview_update(newer, sc.pin_set(self.catalog, 'development'))
        change = {item.preset_id: item for item in preview.changes}['github-home']
        self.assertEqual(change.state, 'updated')
        self.assertEqual(change.from_version, change.to_version)
        fields = dict((item[0], item[1:]) for item in change.changed_fields)
        self.assertEqual(fields['definition_checked_on'], ('2026-09-25', '2026-10-01'))

    def test_deprecation_is_visible_and_carries_its_reason(self):
        data = manifest()
        preset = find(data['presets'], 'signal-home')
        preset['deprecated'] = True
        preset['deprecated_reason_ru'] = 'Определение не подтверждено, пересматривается.'
        newer = sc.parse_catalog(data)
        preview = sc.preview_update(newer, sc.pin_set(self.catalog, 'messengers'))
        change = {item.preset_id: item for item in preview.changes}['signal-home']
        self.assertEqual(change.state, 'deprecated')
        self.assertTrue(change.breaking)
        self.assertIn('пересматривается', change.deprecated_reason_ru)

    def test_deprecated_presets_disappear_from_search_by_default(self):
        data = manifest()
        preset = find(data['presets'], 'signal-home')
        preset['deprecated'] = True
        preset['deprecated_reason_ru'] = 'Устарело.'
        newer = sc.parse_catalog(data)
        found = [item.preset_id for item in sc.search_presets(newer, category='messengers')]
        self.assertNotIn('signal-home', found)
        with_deprecated = [item.preset_id for item in
                           sc.search_presets(newer, category='messengers', include_deprecated=True)]
        self.assertIn('signal-home', with_deprecated)

    def test_a_removed_preset_is_reported_not_silently_dropped(self):
        data = manifest()
        data['presets'] = [item for item in data['presets'] if item['id'] != 'signal-home']
        for item in data['service_sets']:
            item['optional'] = [pid for pid in item['optional'] if pid != 'signal-home']
            item['combination'] = 'at_least' if item['optional'] else 'none'
            item['min_passes'] = 1 if item['combination'] == 'at_least' else 0
        newer = sc.parse_catalog(data)
        preview = sc.preview_update(newer, sc.pin_set(self.catalog, 'messengers'))
        change = {item.preset_id: item for item in preview.changes}['signal-home']
        self.assertEqual(change.state, 'removed')
        self.assertIsNone(change.to_digest)
        self.assertTrue(preview.has_changes)

    def test_scenario_changes_are_reported_separately_from_preset_changes(self):
        data = manifest()
        find(data['service_sets'], 'video')['scenario']['timeout'] = 12
        newer = sc.parse_catalog(data)
        preview = sc.preview_update(newer, sc.pin_set(self.catalog, 'video'))
        self.assertEqual(dict((item[0], item[1:]) for item in preview.scenario_changes)['timeout'],
                         (8, 12))
        self.assertTrue(all(change.state == 'unchanged' for change in preview.changes))

    def test_building_a_preview_changes_nothing(self):
        pinned = sc.pin_set(self.catalog, 'basic')
        before = pinned.to_dict()
        sc.preview_update(bumped(), pinned)
        self.assertEqual(pinned.to_dict(), before)

    def test_upgrade_is_explicit_and_returns_a_new_snapshot(self):
        data = manifest()
        preset = find(data['presets'], 'google-204')
        preset['version'] = 2
        preset['probes'][0]['url'] = 'https://www.google.com/generate_204?x=1'
        newer = sc.parse_catalog(data)
        old = sc.pin_set(self.catalog, 'basic')
        preview = sc.preview_update(newer, old)
        upgraded = sc.upgrade_set(newer, preview)
        self.assertNotEqual(upgraded.digest, old.digest)
        self.assertEqual(upgraded.targets()[0]['url'], 'https://www.google.com/generate_204?x=1')
        # Прежний снимок не тронут: обновление не молчаливый процесс.
        self.assertEqual(old.targets()[0]['url'], 'https://www.google.com/generate_204')

    def test_upgrade_of_a_missing_set_is_refused_and_names_the_set(self):
        data = manifest()
        data['service_sets'] = [item for item in data['service_sets'] if item['id'] != 'video']
        newer = sc.parse_catalog(data)
        preview = sc.preview_update(newer, sc.pin_set(self.catalog, 'video'))
        self.assertEqual(preview.set_state, 'missing')
        with self.assertRaises(sc.CatalogError) as caught:
            sc.upgrade_set(newer, preview)
        self.assertIn('video', str(caught.exception))

    def test_a_broken_new_manifest_cannot_be_upgraded_into(self):
        data = manifest()
        data['presets'][0]['not_proved_ru'] = []
        with self.assertRaises(sc.ManifestError):
            sc.parse_catalog(data)

    def test_preview_to_dict_is_json_ready(self):
        preview = sc.preview_update(self.catalog, sc.pin_set(self.catalog, 'api'))
        payload = json.loads(json.dumps(preview.to_dict(), ensure_ascii=False))
        self.assertEqual(payload['set_id'], 'api')
        self.assertEqual(len(payload['changes']), 3)


class UserSetTest(unittest.TestCase):
    """Пользовательский набор и его копия сервисов переживают обновление каталога."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = sc.load_catalog()

    def make(self, **kwargs):
        params = dict(set_id='my-set', title_ru='Мои сервисы', title_en='My services',
                      preset_ids=['github-home', 'openai-models', 'microsoft-oidc-discovery'])
        params.update(kwargs)
        set_id = params.pop('set_id')
        title_ru = params.pop('title_ru')
        title_en = params.pop('title_en')
        preset_ids = params.pop('preset_ids')
        return sc.new_user_set(self.catalog, set_id, title_ru, title_en, preset_ids, **params)

    def test_user_set_is_owned_by_the_user_and_snapshots_its_services(self):
        user_set = self.make()
        self.assertEqual(user_set.origin, 'user')
        self.assertEqual(user_set.required, ('github-home',))
        self.assertEqual(user_set.optional, ('openai-models', 'microsoft-oidc-discovery'))
        for payload in user_set.presets.values():
            self.assertTrue(payload['user_owned'])
            self.assertEqual(payload['origin'], 'user')

    def test_user_set_survives_a_catalog_update(self):
        user_set = self.make()
        before = user_set.targets()
        data = manifest()
        preset = find(data['presets'], 'github-home')
        preset['version'] = 9
        preset['probes'][0]['url'] = 'https://github.com/robots.txt'
        newer = sc.parse_catalog(data)
        self.assertEqual(user_set.targets(), before)
        self.assertEqual(user_set.preset('github-home').version, 1)
        self.assertEqual(sc.pin_set(newer, 'development').presets['github-home']['version'], 9)

    def test_user_set_keeps_a_preset_the_catalog_no_longer_has(self):
        user_set = self.make()
        data = manifest()
        data['presets'] = [item for item in data['presets'] if item['id'] != 'microsoft-oidc-discovery']
        for item in data['service_sets']:
            item['required'] = [pid for pid in item['required'] if pid != 'microsoft-oidc-discovery']
            item['optional'] = [pid for pid in item['optional'] if pid != 'microsoft-oidc-discovery']
            item['combination'] = 'at_least' if item['optional'] else 'none'
            item['min_passes'] = 1 if item['combination'] == 'at_least' else 0
        sc.parse_catalog(data)
        self.assertEqual(len(user_set.targets()), 3)

    def test_user_set_declares_the_full_field_inventory(self):
        user_set = self.make()
        expected = set(self.catalog.scenario_fields()) - set(self.catalog.derived_fields())
        self.assertEqual(set(user_set.scenario), expected)

    def test_user_set_title_cannot_promise_more_than_a_probe(self):
        with self.assertRaises(sc.ManifestError):
            self.make(title_ru='Мои сервисы: звонки и 4K')
        with self.assertRaises(sc.ManifestError):
            self.make(title_ru='My services: full service')

    def test_user_set_rejects_an_unknown_mandatory_service(self):
        with self.assertRaises(sc.CatalogError) as caught:
            self.make(required_ids=['nosuch-home'])
        self.assertIn('nosuch-home', str(caught.exception))

    def test_user_set_needs_at_least_one_mandatory_service(self):
        with self.assertRaises(sc.CatalogError):
            self.make(required_ids=[])

    def test_user_set_at_least_validates_k_against_the_optional_size(self):
        with self.assertRaises(sc.CatalogError):
            self.make(combination='at_least', min_passes=5)
        self.assertEqual(self.make(combination='at_least', min_passes=2).min_passes, 2)

    def test_user_set_any_without_optional_is_refused(self):
        with self.assertRaises(sc.CatalogError):
            self.make(preset_ids=['github-home'])
        self.assertEqual(self.make(preset_ids=['github-home'],
                                   combination='none').optional, ())

    def test_user_set_rejects_a_field_outside_the_scenario_scope(self):
        with self.assertRaises(sc.CatalogError) as caught:
            self.make(scenario={'workers': 256})
        self.assertIn('workers', str(caught.exception))

    def test_user_set_round_trips_through_json(self):
        user_set = self.make(combination='at_least', min_passes=1)
        restored = sc.PinnedSet.from_dict(json.loads(json.dumps(user_set.to_dict(), ensure_ascii=False)))
        self.assertEqual(restored.digest, user_set.digest)
        self.assertEqual(restored.combination, 'at_least')
        self.assertEqual(restored.targets(), user_set.targets())

    def test_user_set_snapshot_does_not_share_state_with_the_catalog(self):
        user_set = self.make()
        user_set.presets['github-home']['version'] = 99
        self.assertEqual(self.catalog.preset('github-home').version, 1)

    def test_user_set_targets_pass_the_real_scanner_validator(self):
        import argparse
        from proxy_workbench import proxytool as core
        user_set = self.make()
        args = argparse.Namespace(attempts=2, timeout=8, max_bytes=262144, request_profile='workbench')
        normalized = core.validate_targets(copy.deepcopy(user_set.targets()), args,
                                          request_profile='workbench')
        self.assertEqual(len(normalized['targets']), 3)


if __name__ == '__main__':
    unittest.main()

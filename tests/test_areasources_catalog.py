"""F13: the catalog itself -- ids, families, datasets, statuses and migration.

Everything asserted here is a statement about the *bundled data file* or about
the pure functions that read it.  No network, no database, no proxy endpoint:
the catalog is the one part of the source system that is allowed to be a fact
about other people's files without us having fetched anything ourselves.
"""
import json
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import source_adapters as sa
from proxy_workbench import source_catalog as sc
from proxy_workbench import source_management as sm

# Pairs the source research proved carry the same bytes.  The numbers and the
# pairs are taken from docs/requirements/sources-research/overlap.md; they are
# file comparisons made by that pass, not results of this application.
# ``different_publishers`` says what the copy actually is: hookzof and proxifly
# are two people serving one list, while dinoz0rg serves one list under two
# formats of the same repository.
IDENTICAL_PAIRS = (
    # overlap.md: "| cur-41 | cur-43 | 21036 | 1.0 | 0 | 0 |" -- Jaccard 1.0
    ('cur-41', 'cur-43', 21036, True),
    # overlap.md family F12: "cur-11:0/2037, new-079:0/2037" -- no own addresses either side
    ('cur-11', 'new-079', 2037, False),
)


class BundledCatalogTest(unittest.TestCase):
    def setUp(self):
        self.catalog = sc.load_bundled()

    def test_the_catalog_is_the_extended_one_and_not_only_the_recommended_eight(self):
        self.assertEqual(self.catalog['catalog_id'], sc.CATALOG_ID)
        self.assertEqual(self.catalog['schema_version'], sc.CATALOG_SCHEMA_VERSION)
        self.assertGreaterEqual(len(self.catalog['sources']), 140)
        quick = next(item for item in self.catalog['sets'] if item['id'] == 'quick')
        self.assertEqual(len(sc.set_members(quick)), 8)
        self.assertLess(len(sc.set_members(quick)), len(self.catalog['sources']))

    def test_every_record_has_the_identity_the_catalog_is_about(self):
        for source in self.catalog['sources']:
            with self.subTest(source=source['id']):
                self.assertTrue(sc.ID_RE.fullmatch(source['id']))
                self.assertTrue(source['name'].strip())
                self.assertTrue(source['publisher'].get('id'))
                self.assertTrue(source['family_id'])
                self.assertTrue(source['endpoints'])
                # A record with no adapter says why it is not a proxy list; it
                # must not borrow a plausible-looking one.
                if source['adapter']['kind'] is None:
                    self.assertIsNotNone(sm.not_proxy_reason(source))

    def test_ids_are_unique_and_legacy_aliases_resolve_back_to_them(self):
        ids = [source['id'] for source in self.catalog['sources']]
        self.assertEqual(len(ids), len(set(ids)))
        aliases = sc.legacy_aliases(self.catalog)
        for source in self.catalog['sources']:
            for spec in source['legacy_specs']:
                self.assertEqual(aliases.get(spec), source['id'])

    def test_every_source_is_in_a_five_state_status_and_none_claims_a_liveness_claim(self):
        for source in self.catalog['sources']:
            with self.subTest(source=source['id']):
                evidence = source['evidence']
                self.assertEqual(set(evidence), set(sc.EVIDENCE_STATES))
                # The research never opened a connection through an address, so
                # no record may carry a liveness claim.
                self.assertEqual(evidence['proxy_liveness']['state'], 'not_run')

    def test_not_everything_is_enabled_by_default(self):
        enabled = [source for source in self.catalog['sources'] if source['enabled_by_default']]
        self.assertTrue(enabled)
        self.assertLess(len(enabled), len(self.catalog['sources']))

    def test_the_reasons_a_source_is_not_a_proxy_list_are_separate_kinds(self):
        reasons = Counter(sm.not_proxy_reason(source) for source in self.catalog['sources'])
        self.assertGreaterEqual(len([key for key in reasons if key]), 4)
        # A subscription config is not a broken list: it is a different thing.
        self.assertTrue(reasons['subscription_config'])
        # A provider page is not a list either.
        self.assertTrue(reasons['commercial_page'])

    def test_every_record_that_is_not_collectable_says_why(self):
        """A source the collector will not touch must never be a mystery.

        Three shapes are legitimate -- an unreadable format, an access
        requirement, or the catalog's own refusal -- and a fourth one is a bug:
        a record that simply cannot be collected with no reason anywhere.
        """
        for source in self.catalog['sources']:
            if sc.collectable_source(source):
                continue
            with self.subTest(source=source['id']):
                row = sm.public_row(source)
                self.assertTrue(row['not_proxy_source'] or row['access_blocked_code'],
                                'источник недоступен для сбора без объяснения')
                self.assertTrue(row['collection_note'],
                                'у источника без сбора должен быть текст каталога')

    def test_the_set_members_all_exist_and_custom_is_the_users_own(self):
        ids = {source['id'] for source in self.catalog['sources']}
        for definition in self.catalog['sets']:
            members = sc.set_members(definition, ['my-own'])
            with self.subTest(set=definition['id']):
                if definition['id'] == sc.CUSTOM_SET_ID:
                    self.assertEqual(members, ['my-own'])
                else:
                    self.assertTrue(members)
                    self.assertLessEqual(set(members), ids)

    def test_a_record_with_a_custom_family_is_kept_verbatim(self):
        for source in self.catalog['sources']:
            with self.subTest(source=source['id']):
                row = sm.public_row(source)
                self.assertEqual(row['dataset_group'], sc.dataset_group_of(source))
                self.assertTrue(row['dataset_group'])


class ResearchFactsTest(unittest.TestCase):
    """The function must express what the research established, not more."""

    def setUp(self):
        self.catalog = sc.load_bundled()
        self.by_id = {source['id']: source for source in self.catalog['sources']}
        # Only an *explicit* field is a statement about the bytes; a record
        # without one simply defaults to its publisher family.
        self.raw = json.loads(sc.bundled_path().read_text(encoding='utf-8'))
        self.raw_by_id = {source['id']: source for source in self.raw['sources']}

    def test_the_two_known_copies_are_one_dataset(self):
        for left, right, size, different_publishers in IDENTICAL_PAIRS:
            with self.subTest(pair=(left, right)):
                first, second = self.by_id[left], self.by_id[right]
                # Separate records, separately addressable: a copy is still two
                # things a user can turn on and off.
                self.assertNotEqual(first['id'], second['id'])
                self.assertNotEqual(first['data_urls'], second['data_urls'])
                self.assertNotEqual(first['family_id'], second['family_id'])
                if different_publishers:
                    self.assertNotEqual(first['publisher']['id'], second['publisher']['id'])
                # ...and one dataset, so one is not a second observation.
                self.assertEqual(sc.dataset_group_of(first), sc.dataset_group_of(second))
                self.assertNotEqual(sc.dataset_group_of(first), first['id'])
                self.assertGreater(size, 1000)

    def test_a_dataset_group_is_never_silently_a_family_rename(self):
        groups = {}
        for source in self.raw['sources']:
            if source.get('dataset_group'):
                groups.setdefault(source['dataset_group'], []).append(source)
        self.assertTrue(groups)
        for group, members in groups.items():
            with self.subTest(group=group):
                self.assertGreaterEqual(len(members), 2,
                                        f'группа {group} объявляет копию в одиночку')
                self.assertEqual(len({member['id'] for member in members}), len(members))
                # A group never merges two records of the same publisher family:
                # that would hide a genuinely different dataset behind a copy.
                self.assertEqual(len({member['family_id'] for member in members}), len(members))

    def test_a_record_without_the_field_keeps_its_family_as_the_dataset(self):
        for source in self.raw['sources']:
            if source.get('dataset_group'):
                continue
            with self.subTest(source=source['id']):
                self.assertEqual(self.by_id[source['id']]['dataset_group'], source['family_id'])

    def test_the_empty_but_reachable_source_is_described_as_empty_not_as_dead(self):
        source = self.by_id['cur-37']
        self.assertIn('пустым телом', source['access_note'])
        self.assertEqual(source['evidence']['url_reachable']['state'], 'http_2xx_nonempty')
        self.assertIn('empty', source['update_claim'].lower())
        # It stays collectable: an empty Tuesday is not a dead source.
        self.assertTrue(sc.collectable_source(source))

    def test_paid_and_trial_providers_are_visible_but_inert(self):
        inert = [source for source in self.catalog['sources']
                 if sm.access_group((source.get('access') or {}).get('kind')) in ('paid', 'trial')]
        self.assertTrue(inert, 'в каталоге должны быть видны коммерческие провайдеры')
        for source in inert:
            with self.subTest(source=source['id']):
                self.assertFalse(source['collection_allowed'])
                self.assertFalse(sc.collectable_source(source))
                self.assertTrue(source['access_note'] or source['quota_text'])
                self.assertTrue(sm.access_reason(source))
                row = sm.public_row(source)
                self.assertFalse(row['collectable'])
                self.assertIsNotNone(row['access_blocked_reason'])

    def test_a_source_needing_an_account_never_reaches_the_collector(self):
        for source in self.catalog['sources']:
            if source['account_required'] or (source.get('access') or {}).get('account_required'):
                with self.subTest(source=source['id']):
                    self.assertFalse(source['collection_allowed'])
                    self.assertEqual(sm.access_reason(source), 'account_required')

    def test_a_source_with_a_registered_adapter_is_collectable(self):
        for source in self.catalog['sources']:
            if sc.collectable_source(source):
                with self.subTest(source=source['id']):
                    self.assertEqual(source['payload_role'], 'proxy_list')
                    self.assertIn(source['adapter']['kind'], sa.ADAPTER_KINDS)
                    self.assertFalse(source['account_required'])

    def test_a_record_without_an_account_may_still_be_refused_by_the_catalog(self):
        refused = [source for source in self.catalog['sources']
                   if not source['collection_allowed'] and sm.access_reason(source) is None]
        self.assertTrue(refused, 'в каталоге есть записи, выключенные решением каталога')
        for source in refused:
            with self.subTest(source=source['id']):
                self.assertEqual(sm.public_row(source)['access_blocked_code'], 'catalog_not_enabled')

    def test_the_huge_aggregator_carries_limits_rather_than_a_promise(self):
        aggregator = self.by_id['new-043']
        self.assertEqual(aggregator['category'], 'aggregator')
        self.assertTrue(sc.collectable_source(aggregator))
        # A 500k-address list is a budget question, not a claim. The catalog
        # must not carry a freshness promise it did not establish itself.
        self.assertIn('liveness', aggregator['priority_reason'].lower())
        self.assertNotIn('verified', aggregator['name'].lower())


class SupportStatusTest(unittest.TestCase):
    """F13 asks for five states a reader can tell apart; the data has only fields."""

    def setUp(self):
        self.catalog = sc.load_bundled()
        self.by_id = {source['id']: source for source in self.catalog['sources']}

    def test_every_record_gets_exactly_one_of_the_five_states(self):
        counts = Counter(sc.support_status(source) for source in self.catalog['sources'])
        for state in sc.SUPPORT_STATES:
            with self.subTest(state=state):
                self.assertIn(state, counts)
                self.assertIsInstance(counts[state], int)
        self.assertEqual(sum(counts.values()), len(self.catalog['sources']))

    def test_supported_means_readable_and_allowed(self):
        for source in self.catalog['sources']:
            if sc.support_status(source) == 'supported':
                self.assertTrue(sc.collectable_source(source))

    def test_every_collectable_record_is_supported_and_nothing_else_is(self):
        for source in self.catalog['sources']:
            with self.subTest(source=source['id']):
                self.assertEqual(sc.support_status(source) == 'supported',
                                 sc.collectable_source(source))

    def test_needs_auth_wins_over_a_missing_adapter(self):
        for source in self.catalog['sources']:
            if sc.support_status(source) == 'needs_auth':
                self.assertTrue(source['account_required']
                                or (source.get('access') or {}).get('account_required')
                                or (source.get('access') or {}).get('kind')
                                in sc.NEEDS_AUTH_ACCESS)

    def test_a_paid_provider_is_never_supported(self):
        for source in self.catalog['sources']:
            if (source.get('access') or {}).get('kind') in ('paid', 'temporary_trial',
                                                             'free_with_api_key'):
                self.assertNotEqual(sc.support_status(source), 'supported')

    def test_a_list_without_a_readable_adapter_asks_for_one(self):
        needs = [source for source in self.catalog['sources']
                 if sc.support_status(source) == 'needs_adapter']
        self.assertTrue(needs)
        for source in needs:
            with self.subTest(source=source['id']):
                self.assertEqual(source['payload_role'], 'proxy_list')
                self.assertNotIn((source.get('adapter') or {}).get('kind'),
                                 sc.ADAPTERS - {'unsupported'})

    def test_the_row_carries_the_status_and_the_reason_together(self):
        row = sm.public_row(self.by_id['new-011'])
        self.assertEqual(row['support'], 'needs_adapter')
        self.assertEqual(row['not_proxy_source'], 'no_adapter')
        self.assertTrue(row['not_proxy_source_reason'])


class MigrationTest(unittest.TestCase):
    """An old flat URL list must survive the move into the catalog world."""

    LEGACY = [
        "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt",
        "socks5 https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
        "text https://free-proxy-list.net/",
        "http-fields https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
        "https://my-own.example/list.txt",
    ]

    def setUp(self):
        self.catalog = sc.load_bundled()

    def test_every_legacy_url_keeps_its_exact_spec(self):
        migrated = sc.migrate_settings({'sources': list(self.LEGACY)}, self.catalog)
        selection = migrated['source_selection']
        self.assertEqual(len(selection['specs']), len(self.LEGACY))
        for spec in self.LEGACY:
            self.assertIn(spec, selection['specs'].values())
        self.assertEqual(migrated['settings_version'], 3)

    def test_a_known_url_becomes_the_stable_catalog_id(self):
        migrated = sc.migrate_settings({'sources': [self.LEGACY[0]]}, self.catalog)
        self.assertEqual(migrated['source_selection']['selected_ids'], ['cur-01'])

    def test_an_unknown_url_becomes_a_custom_source_and_is_still_collectable(self):
        migrated = sc.migrate_settings({'sources': [self.LEGACY[4]]}, self.catalog)
        selection = migrated['source_selection']
        self.assertEqual(len(selection['custom_sources']), 1)
        custom = selection['custom_sources'][0]
        self.assertTrue(custom['id'].startswith('custom-'))
        plans = sc.materialize_selection(migrated, self.catalog)
        self.assertEqual([plan['endpoints'][0]['url'] for plan in plans],
                         ['https://my-own.example/list.txt'])

    def test_a_disabled_legacy_source_stays_disabled(self):
        """A pause the user set in an older version must survive the migration.

        Both spellings are checked because both existed: the old file wrote the
        URL, and a partially migrated file already carried the catalog id.
        """
        for spelling in (self.LEGACY[0], 'cur-01'):
            with self.subTest(spelling=spelling):
                migrated = sc.migrate_settings(
                    {'sources': list(self.LEGACY), 'download_disabled_ids': [spelling]}, self.catalog)
                self.assertNotIn('cur-01', sc.selection_ids(migrated))
                self.assertNotIn('cur-01', [plan['id'] for plan
                                            in sc.materialize_selection(migrated, self.catalog)])
                self.assertIn('cur-01', migrated['source_selection']['selected_ids'],
                              'источник должен остаться в настройках, но быть выключен')

    def test_migration_is_idempotent(self):
        once = sc.migrate_settings({'sources': list(self.LEGACY)}, self.catalog)
        twice = sc.migrate_settings(once, self.catalog)
        self.assertEqual(once['source_selection'], twice['source_selection'])

    def test_a_custom_source_keeps_the_name_the_user_typed(self):
        entry = {'id': 'my-own', 'url': 'https://my-own.example/list.txt', 'name': 'Мой список'}
        migrated = sc.migrate_settings({'source_selection': {
            'schema_version': 1, 'selected_ids': ['my-own'], 'download_disabled_ids': [],
            'sets': [], 'specs': {}, 'custom_sources': [entry]}}, self.catalog)
        again = sc.migrate_settings(migrated, self.catalog)
        self.assertEqual(again['source_selection']['custom_sources'][0]['name'], 'Мой список')

    def test_an_override_the_user_picked_survives_a_later_migration(self):
        first = sc.migrate_settings({'sources': [self.LEGACY[1]]}, self.catalog)
        self.assertEqual(first['source_selection']['specs']['cur-41'],
                         'socks5 https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt')
        second = sc.migrate_settings(first, self.catalog)
        self.assertEqual(second['source_selection']['specs']['cur-41'],
                         'socks5 https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt')

    def test_applying_and_removing_a_set_changes_only_the_selection(self):
        settings = sc.migrate_settings({'sources': []}, self.catalog)
        quick = next(item for item in self.catalog['sets'] if item['id'] == 'quick')
        selected = sc.apply_set(settings, 'quick', self.catalog)
        self.assertEqual(sorted(sc.selection_ids(selected)), sorted(sc.set_members(quick)))
        cleared = sc.remove_ids(selected, list(sc.set_members(quick)), self.catalog)
        self.assertEqual(sc.selection_ids(cleared), [])
        self.assertEqual(self.catalog['revision'], sc.load_bundled()['revision'])


def _minimal_catalog(*, revision=1, source_id='src-01', url='https://lists.example/a.txt'):
    """A one-record catalog, for the rules that must not depend on real data."""
    return {
        'schema_version': 1, 'catalog_id': sc.CATALOG_ID, 'revision': revision,
        'published_at': '2026-01-01T00:00:00Z', 'minimum_app_version': '2.3.0',
        'sources': [{
            'id': source_id, 'name': 'Example list',
            'publisher': {'id': 'example', 'name': 'Example'},
            'family_id': 'example:list', 'category': 'txt-list',
            'data_urls': [url], 'protocols': ['http'],
            'adapter': {'kind': 'line', 'profile': 'http-v1', 'config': {}},
        }],
        'sets': [{'id': 'quick', 'name': 'Quick', 'kind': 'system',
                  'members': [source_id], 'auto_add_new': False}],
    }


class CatalogIntegrityTest(unittest.TestCase):
    def test_the_bundled_file_is_the_object_the_loader_accepts(self):
        path = sc.bundled_path()
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['catalog_id'], sc.CATALOG_ID)

    def test_a_legacy_flat_url_file_is_still_accepted_as_settings(self):
        path = Path(__file__).resolve().parents[1] / 'proxy_workbench' / 'sources.json'
        self.assertTrue(path.is_file(), 'старый плоский список URL должен остаться на месте')
        legacy = json.loads(path.read_text(encoding='utf-8'))
        self.assertIsInstance(legacy, list)
        self.assertTrue(legacy)
        migrated = sc.migrate_settings({'sources': legacy})
        self.assertTrue(sc.selection_ids(migrated))
        self.assertEqual(migrated['settings_version'], 3)

    def test_a_malformed_evidence_block_is_refused_with_a_reason(self):
        catalog = json.loads(sc.bundled_path().read_text(encoding='utf-8'))
        catalog['sources'][0]['evidence'] = {'made_up_claim': {'state': 'yes'}}
        with self.assertRaises(sc.CatalogError):
            sc.validate_catalog(catalog, allow_research=True, allow_unsafe=True)

    def test_a_malformed_adapter_config_is_refused_instead_of_crashing(self):
        catalog = json.loads(sc.bundled_path().read_text(encoding='utf-8'))
        catalog['sources'][0]['adapter'] = {'kind': 'line', 'profile': 'http-v1', 'config': 'nope'}
        with self.assertRaises(sc.CatalogError):
            sc.validate_catalog(catalog, allow_research=True, allow_unsafe=True)

    def test_a_bare_string_where_a_list_belongs_is_refused(self):
        catalog = json.loads(sc.bundled_path().read_text(encoding='utf-8'))
        catalog['sources'][0]['tags'] = 'quick'
        with self.assertRaises(sc.CatalogError):
            sc.validate_catalog(catalog, allow_research=True, allow_unsafe=True)

    def test_a_revision_downgrade_is_refused(self):
        current = sc.load_bundled()
        older = json.loads(sc.bundled_path().read_text(encoding='utf-8'))
        older['revision'] = current['revision'] - 1
        with self.assertRaises(sc.CatalogError):
            sc.accept_catalog(current, older)

    def test_the_same_revision_with_different_content_is_refused(self):
        current = sc.load_bundled()
        twin = json.loads(sc.bundled_path().read_text(encoding='utf-8'))
        twin['sources'][0]['name'] = twin['sources'][0]['name'] + ' (edited)'
        with self.assertRaises(sc.CatalogError):
            sc.accept_catalog(current, twin)

    def test_dataset_identity_is_part_of_what_a_new_revision_may_change(self):
        base = _minimal_catalog(revision=7)
        current = sc.validate_catalog(base, allow_research=True, allow_unsafe=True)
        twin = json.loads(json.dumps(base))
        twin['revision'] = 8
        twin['sources'][0]['dataset_group'] = 'dataset:changed'
        accepted = sc.accept_catalog(current, twin)
        self.assertEqual(sc.dataset_group_of(accepted['sources'][0]), 'dataset:changed')
        self.assertIn(twin['sources'][0]['id'], sc.catalog_diff(current, accepted)['changed'])

if __name__ == '__main__':
    unittest.main()

"""F05 живым поведением: профили, версии, правила целей, паритет поверхностей.

Каждый тест вызывает настоящие функции `profiles.py` на временной базе и
проверяет наблюдаемый вердикт, а не исходный текст.  Сети нет, публичных адресов
нет, секретов нет.

Закрывает:
* именованные профили, копирование, версии, import/export без секретов,
  сохранение истории предыдущей версии;
* правила целей required/optional, all/any/at-least-K, target-specific thresholds
  и budget;
* непустое подтверждение: all с пустым набором, K=0 и полностью выключенные
  probes не дают pass;
* fail-fast согласован с правилом, а смена порога не приписывает недовыполненным
  измерениям доказательство полного теста;
* GUI, CLI и API исполняют профиль одинаково.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import profiles
from proxy_workbench.profiles import (OptionalRule, ProfileSpec, ProfileStore, RunState,
                                      TargetRule, evaluate, export_profile, import_profile,
                                      run_request)

#: Фиксированный момент вместо часов.
START = 1_700_000_000.0

#: Идентификаторы целей не называют ни одного реального сервиса.
BASIC = 'http-basic'
SEARCH = 'http-search'
VIDEO = 'video-optional'


def target(target_id, kind='required', *, enabled=True, min_success=1.0, max_latency_ms=None):
    return {'id': target_id, 'kind': kind, 'enabled': enabled,
            'min_success': min_success, 'max_latency_ms': max_latency_ms}


def spec(*, optional_rule='none', k=None, attempts=1, budget=None, targets=None,
         description=''):
    if targets is None:
        targets = [target(BASIC), target(SEARCH, 'optional')]
    return ProfileSpec.create(targets=targets, optional_rule=optional_rule, k=k,
                              attempts=attempts, budget=budget, description=description)


def unvalidated(**document):
    """Документ, который пришёл от чужого писателя: читается, но не валидируется."""
    return ProfileSpec.from_dict(document, validate=False)


def verdict_of(document, evidence=None, **kwargs):
    return evaluate(unvalidated(**document), evidence, **kwargs)


# --------------------------------------------------------------------------- #
# Хранилище: имена, копии, версии
# --------------------------------------------------------------------------- #
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix='pw_areacat_profiles_')) / 'workbench.db'
        self.store = profiles.open_store(self.path)
        self.addCleanup(self.store.close)

    def test_a_named_profile_is_created_with_revisions_and_readable_back(self):
        built = spec(targets=[target(BASIC, max_latency_ms=2000),
                              target(SEARCH, 'optional', min_success=0.5),
                              target(VIDEO, 'optional', min_success=0.5)],
                     optional_rule='at_least', k=1, attempts=2,
                     budget={'max_probes': 6}, description='Рабочий профиль')
        ref = self.store.create('Мой профиль', built, at=START)
        self.assertEqual(ref.revision, 1)
        self.assertEqual(ref.row_id, f'{ref.profile_id}@1')
        head = self.store.get(ref.profile_id)
        self.assertEqual(head.name, 'Мой профиль')
        self.assertEqual(head.digest, built.digest)
        self.assertEqual(head.spec.description, 'Рабочий профиль')
        self.assertEqual(head.as_dict(include_plan=True)['plan']['min_probes'], 4)
        self.assertEqual([record.name for record in self.store.list()], ['Мой профиль'])
        self.assertIsNone(self.store.default())

    def test_copy_leaves_the_source_untouched_and_records_its_origin(self):
        built = spec(description='Исходный')
        source = self.store.create('Исходный', built, at=START)
        clone = self.store.copy(source.profile_id, new_name='Копия', at=START + 1)
        copied = self.store.get(clone.profile_id)
        self.assertNotEqual(copied.profile_id, source.profile_id)
        self.assertEqual(copied.parent_id, source.row_id)
        self.assertEqual(copied.digest, built.digest, 'копия несёт то же содержимое')
        self.assertEqual(self.store.get(source.profile_id).revision, 1)
        # копия без имени получает свободное
        again = self.store.copy(source.profile_id, at=START + 2)
        self.assertEqual(self.store.get(again.profile_id).name, 'Исходный (2)')

    def test_update_appends_a_revision_and_keeps_the_previous_one_readable(self):
        first = spec(description='v1', budget={'max_probes': 6})
        ref = self.store.create('Профиль', first, at=START)
        second = first.copy_of(description='v2', budget={'max_probes': 8})
        head = self.store.update(ref.profile_id, second, base_revision=1, at=START + 1)
        self.assertEqual(head.revision, 2)
        history = self.store.history(ref.profile_id)
        self.assertEqual([record.revision for record in history], [1, 2])
        self.assertEqual(history[0].digest, first.digest, 'ревизия 1 не изменилась')
        self.assertEqual(history[1].parent_id, f'{ref.profile_id}@1')
        self.assertEqual(self.store.get(ref.profile_id, 1).spec.budget.max_probes, 6)
        self.assertEqual(self.store.get(ref.profile_id, 2).spec.budget.max_probes, 8)
        with self.assertRaises(profiles.ProfileError) as stale:
            self.store.update(ref.profile_id, first, base_revision=1, at=START + 2)
        self.assertEqual(stale.exception.code, profiles.E_CONFLICT_REVISION)
        # запись без изменений не плодит ревизии
        self.assertEqual(self.store.update(ref.profile_id, second, at=START + 3).revision, 2)
        # архив скрывает профиль, история остаётся
        self.store.archive(ref.profile_id, at=START + 4)
        self.assertEqual([r.name for r in self.store.list()], [])
        self.assertEqual(len(self.store.history(ref.profile_id)), 2)
        with self.assertRaises(profiles.ProfileError) as blocked:
            self.store.update(ref.profile_id, first, at=START + 5)
        self.assertEqual(blocked.exception.code, profiles.E_CONFLICT_ARCHIVED)
        self.store.unarchive(ref.profile_id)
        self.assertEqual([r.name for r in self.store.list()], ['Профиль'])

    def test_a_duplicate_name_is_refused_and_a_renamed_one_is_free(self):
        ref = self.store.create('Один', spec(), at=START)
        with self.assertRaises(profiles.ProfileError) as taken:
            self.store.create('Один', spec(), at=START + 1)
        self.assertEqual(taken.exception.code, profiles.E_CONFLICT_NAME)
        self.assertEqual(self.store.find('Один').profile_id, ref.profile_id)
        renamed = spec(description='переименован')
        self.store.update(ref.profile_id, renamed, name='Два', at=START + 2)
        self.assertEqual(self.store.find('Два').profile_id, ref.profile_id)
        self.assertIsNone(self.store.find('Один'))
        self.assertEqual(self.store.free_name('Один'), 'Один')
        self.assertEqual(self.store.free_name('Два'), 'Два (2)')

    def test_diff_reports_targets_thresholds_rule_and_budget(self):
        ref = self.store.create('П', spec(attempts=1, budget={'max_probes': 4}), at=START)
        changed = ProfileSpec.create(
            targets=[target(BASIC, max_latency_ms=900), target(VIDEO, 'optional'),
                     target(SEARCH, 'optional', min_success=0.5, enabled=False)],
            optional_rule='any', attempts=2, budget={'max_probes': 8})
        self.store.update(ref.profile_id, changed, base_revision=1, at=START + 1)
        diff = self.store.diff(ref.profile_id, 1, 2)
        self.assertEqual(diff['added'], [VIDEO])
        self.assertEqual(diff['removed'], [], 'SEARCH не исчез, а был выключен')
        self.assertEqual([item['target_id'] for item in diff['changed']], [BASIC, SEARCH])
        self.assertEqual(diff['changed'][1]['after']['enabled'], False)
        self.assertEqual(diff['changed'][0]['after']['max_latency_ms'], 900)
        self.assertEqual(diff['optional_rule']['before'], {'mode': 'none', 'k': None})
        self.assertEqual(diff['budget']['after'], {'max_probes': 8, 'max_duration_s': None})
        self.assertEqual(diff['attempts'], {'before': 1, 'after': 2})
        self.assertTrue(diff['digest_changed'])

    def test_default_flag_moves_between_profiles(self):
        first = self.store.create('A', spec(), is_default=True, at=START)
        second = self.store.create('B', spec(), at=START + 1)
        self.assertEqual(self.store.default().profile_id, first.profile_id)
        self.store.set_default(second.profile_id)
        self.assertEqual(self.store.default().profile_id, second.profile_id)
        self.assertFalse(self.store.get(first.profile_id).is_default)

    def test_an_unmigrated_table_is_refused_instead_of_repaired(self):
        raw = __import__('sqlite3').connect(self.path)
        raw.execute('DROP TABLE profiles')
        raw.execute('CREATE TABLE profiles(id TEXT PRIMARY KEY, config TEXT)')
        raw.commit()
        raw.close()
        with self.assertRaises(profiles.ProfileError) as refused:
            profiles.ProfileStore(self.store.conn)
        self.assertEqual(refused.exception.code, profiles.E_DATA_DB_FOREIGN)


# --------------------------------------------------------------------------- #
# Interchange: экспорт и импорт без секретов
# --------------------------------------------------------------------------- #
class InterchangeTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix='pw_areacat_xchg_')) / 'workbench.db'
        self.store = profiles.open_store(self.path)
        self.addCleanup(self.store.close)

    def test_export_is_secret_free_and_re_imports_with_its_history(self):
        first = spec(targets=[target(BASIC, max_latency_ms=1500)], attempts=1, description='v1')
        ref = self.store.create('Проект', first, at=START)
        second = first.copy_of(description='v2', budget={'max_probes': 8})
        self.store.update(ref.profile_id, second, base_revision=1, at=START + 1)
        document = export_profile(self.store, ref.profile_id, history=True, at=START + 2)
        self.assertEqual(document['kind'], profiles.EXPORT_KIND)
        self.assertEqual(document['secrets'], 'none')
        self.assertEqual([item['revision'] for item in document['revisions']], [1, 2])
        blob = json.dumps(document, ensure_ascii=False)
        for word in ('password', 'token', 'credential', 'authorization'):
            self.assertNotIn(word, blob, word)
        self.assertEqual(sorted(document['revisions'][0]['config']['targets'][0]),
                         ['enabled', 'id', 'kind', 'max_latency_ms', 'min_success'])
        profiles.assert_secret_free(document)

        back = import_profile(self.store, document, name='Скопированный', at=START + 3)
        restored = self.store.history(back.profile_id)
        self.assertEqual([record.revision for record in restored], [1, 2])
        self.assertEqual([record.digest for record in restored],
                         [record.digest for record in self.store.history(ref.profile_id)])
        self.assertEqual(restored[0].spec.target(BASIC).max_latency_ms, 1500)
        self.assertEqual(restored[1].spec.budget.max_probes, 8)

    def test_import_never_overwrites_and_reuse_is_idempotent(self):
        ref = self.store.create('Один', spec(), at=START)
        document = export_profile(self.store, ref.profile_id, at=START + 1)
        with self.assertRaises(profiles.ProfileError) as taken:
            import_profile(self.store, document, at=START + 2)
        self.assertEqual(taken.exception.code, profiles.E_CONFLICT_NAME)
        same = import_profile(self.store, document, on_conflict='reuse', at=START + 3)
        self.assertEqual(same.profile_id, ref.profile_id)
        other = import_profile(self.store, document, on_conflict='rename', at=START + 4)
        self.assertNotEqual(other.profile_id, ref.profile_id)
        self.assertEqual(self.store.get(other.profile_id).name, 'Один (2)')
        different = dict(document, name='Один', digest='x')
        different['revisions'] = [dict(document['revisions'][0],
                                       config=spec(description='другое').as_dict())]
        with self.assertRaises(profiles.ProfileError) as different_content:
            import_profile(self.store, different, on_conflict='reuse', at=START + 5)
        self.assertEqual(different_content.exception.code, profiles.E_CONFLICT_NAME)

    def test_a_document_with_a_credential_is_refused(self):
        for payload, expected in (
                ({'targets': [{'id': BASIC, 'password': 'x'}]}, profiles.E_SECRET_IN_PROFILE),
                ({'targets': [{'id': BASIC, 'url': 'http://u:p@h:80'}]},
                 profiles.E_SECRET_IN_PROFILE),
                ({'targets': [{'id': BASIC, 'note': 'Bearer abc'}]},
                 profiles.E_SECRET_IN_PROFILE)):
            with self.subTest(expected=expected):
                with self.assertRaises(profiles.ProfileError) as refused:
                    profiles.assert_secret_free(payload)
                self.assertEqual(refused.exception.code, expected)

    def test_a_document_of_another_kind_or_version_is_refused(self):
        ref = self.store.create('П', spec(), at=START)
        document = export_profile(self.store, ref.profile_id, at=START + 1)
        for mutate, code in ((lambda d: d.update(kind='что-то-else'), profiles.E_IMPORT_FORMAT),
                             (lambda d: d.update(version=99), profiles.E_IMPORT_REVISION),
                             (lambda d: d.update(revisions=[]), profiles.E_IMPORT_FORMAT),
                             (lambda d: d.update(лишнее=1), profiles.E_VALIDATION_UNKNOWN_FIELD)):
            payload = dict(document)
            mutate(payload)
            with self.assertRaises(profiles.ProfileError) as refused:
                import_profile(self.store, payload, at=START + 2)
            self.assertEqual(refused.exception.code, code)

    def test_legacy_config_becomes_a_named_revision_with_the_same_acceptance_rule(self):
        legacy = {'targets': [{'id': BASIC}, {'id': SEARCH}],
                  'fail_fast': {'min_success': 1.0}, 'attempts': 1}
        converted = profiles.from_legacy_config(legacy)
        self.assertEqual([rule.id for rule in converted.targets], [BASIC, SEARCH])
        self.assertEqual({rule.kind for rule in converted.targets}, {'required'},
                         'старый глобальный порог становится обязательным для каждой цели')
        for rule in converted.targets:
            self.assertEqual(rule.min_success, 1.0)


# --------------------------------------------------------------------------- #
# Правила и непустое подтверждение
# --------------------------------------------------------------------------- #
class NonEmptyGuaranteeTests(unittest.TestCase):
    def test_a_mandatory_set_is_required_on_the_way_in(self):
        cases = {
            'пустой набор целей': dict(targets=[]),
            'нет обязательных': dict(targets=[target(SEARCH, 'optional')], optional_rule='any'),
            'all при пустом optional': dict(targets=[target(BASIC)], optional_rule='all'),
            'at_least K=0': dict(targets=[target(BASIC), target(SEARCH, 'optional')],
                                 optional_rule='at_least', k=0),
            'всё выключено': dict(targets=[target(BASIC, enabled=False)]),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(profiles.ProfileError) as refused:
                    spec(**kwargs)
                self.assertEqual(refused.exception.code, profiles.E_VALIDATION_FIELD)

    def test_a_foreign_document_with_an_empty_guarantee_never_passes(self):
        """Те же три случая, что и на входе, но на готовом документе чужого писателя."""
        cases = {
            'all с пустым набором': ({'targets': [target(BASIC)], 'optional_rule': 'all'},
                                     'E_VERDICT_EMPTY_OPTIONAL_SET'),
            'K=0': ({'targets': [target(BASIC), target(SEARCH, 'optional')],
                     'optional_rule': {'mode': 'at_least', 'k': 0}},
                    'E_VERDICT_K_NOT_POSITIVE'),
            'все probes выключены': ({'targets': [target(BASIC, enabled=False),
                                                 target(SEARCH, 'optional', enabled=False)],
                                      'optional_rule': 'any'},
                                     'E_VERDICT_EMPTY_TARGET_SET'),
            'K больше набора': ({'targets': [target(BASIC), target(SEARCH, 'optional')],
                                'optional_rule': {'mode': 'at_least', 'k': 3}},
                               'E_VERDICT_K_UNREACHABLE'),
        }
        for label, (document, reason) in cases.items():
            with self.subTest(case=label):
                parsed = unvalidated(**document)
                best = [parsed.evidence(rule.id, ok=1, attempts=1) for rule in parsed.targets]
                verdict = evaluate(parsed, best)
                self.assertFalse(verdict['pass'])
                self.assertEqual(verdict['reason'], reason)

    def test_a_pass_always_rests_on_a_successful_measured_probe(self):
        parsed = unvalidated(targets=[target(SEARCH, 'optional')], optional_rule='none')
        empty = evaluate(parsed, [])
        self.assertFalse(empty['pass'])
        self.assertEqual(empty['reason'], 'E_VERDICT_NO_EFFECTIVE_PROBE')
        self.assertEqual(empty['effective_probes'], 0)
        built = spec()
        with self.assertRaises(profiles.ProfileError) as outside:
            evaluate(built, [{'target': 'не-в-профиле', 'ok': 1, 'attempts': 1}])
        self.assertEqual(outside.exception.code, profiles.E_VALIDATION_FIELD)

    def test_all_any_and_at_least_K_separate_the_same_evidence(self):
        """Одни и те же измерения дают разные общие итоги под разными правилами."""
        shape = [target(BASIC, min_success=0.5), target(SEARCH, 'optional', min_success=0.5),
                 target(VIDEO, 'optional', min_success=0.5)]
        # BASIC ок, SEARCH ок, VIDEO провален
        outcomes = (1, 1, 0)
        # K считает только дополнительные цели: обязательная в него не входит
        expected = {'all': False, 'any': True, 'at_least': False, 'none': True}
        for mode in expected:
            with self.subTest(rule=mode):
                built = spec(targets=shape, optional_rule=mode,
                             k=2 if mode == 'at_least' else 1)
                evidence = [built.evidence(rule_id, ok=ok, attempts=1)
                            for rule_id, ok in zip((BASIC, SEARCH, VIDEO), outcomes)]
                verdict = evaluate(built, evidence)
                self.assertEqual(verdict['pass'], expected[mode], verdict['reason'])
                self.assertEqual(verdict['targets'][VIDEO]['state'], 'fail',
                                 'раздельный результат по каждому сервису виден всегда')
        # at_least(2) требует двух, а any доволен одним
        for k, want in ((1, True), (2, False)):
            with self.subTest(k=k):
                built = spec(targets=shape, optional_rule='at_least', k=k)
                evidence = [built.evidence(BASIC, ok=1, attempts=1),
                            built.evidence(SEARCH, ok=1, attempts=1),
                            built.evidence(VIDEO, ok=0, attempts=1)]
                self.assertEqual(evaluate(built, evidence)['pass'], want)
                self.assertEqual(evaluate(built, evidence)['optional_needed'], k)
        one = [built.evidence(BASIC, ok=1, attempts=1), built.evidence(SEARCH, ok=0, attempts=1),
               built.evidence(VIDEO, ok=0, attempts=1)]
        self.assertFalse(evaluate(built, one)['pass'])
        # обязательная цель провалилась - ни одно правило её не спасёт
        for mode, k in (('all', 1), ('any', 1), ('at_least', 2), ('none', 1)):
            with self.subTest(rule=mode, case='обязательная провалена'):
                built = spec(targets=shape, optional_rule=mode, k=k)
                evidence = [built.evidence(BASIC, ok=0, attempts=1),
                            built.evidence(SEARCH, ok=1, attempts=1),
                            built.evidence(VIDEO, ok=1, attempts=1)]
                verdict = evaluate(built, evidence)
                self.assertFalse(verdict['pass'])
                self.assertEqual(verdict['reason'], 'E_VERDICT_REQUIRED_NOT_PASSED')

    def test_unknown_is_neither_fail_nor_pass(self):
        built = spec(targets=[target(BASIC), target(SEARCH, 'optional')], optional_rule='any')
        verdict = evaluate(built, [built.evidence(BASIC, ok=0, attempts=1, unknown=True),
                                   built.evidence(SEARCH, ok=1, attempts=1)])
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['targets'][BASIC]['state'], 'unknown')
        self.assertEqual(verdict['targets'][BASIC]['reason'], 'E_TARGET_UNKNOWN')

    def test_a_target_without_a_measurement_is_unmeasured_not_failed(self):
        built = spec(targets=[target(BASIC), target(SEARCH, 'optional')], optional_rule='any')
        verdict = evaluate(built, [built.evidence(SEARCH, ok=1, attempts=1)])
        self.assertEqual(verdict['targets'][BASIC]['state'], 'unmeasured',
                         'нет ни одного допустимого измерения этой цели')
        self.assertEqual(verdict['targets'][BASIC]['reason'], 'E_TARGET_STALE_EVIDENCE')


class ThresholdAndBudgetTests(unittest.TestCase):
    def test_a_target_specific_threshold_and_latency_are_judged_separately(self):
        built = spec(targets=[target(BASIC, min_success=1.0, max_latency_ms=500),
                              target(SEARCH, 'optional', min_success=0.5, max_latency_ms=100)],
                     optional_rule='any', attempts=2)
        self.assertEqual(built.plan()['min_probes'], 4)
        inside = [built.evidence(BASIC, ok=1, attempts=1, latency_ms=400),
                  built.evidence(SEARCH, ok=1, attempts=1, latency_ms=50)]
        self.assertTrue(evaluate(built, inside)['pass'])
        slow = [built.evidence(BASIC, ok=1, attempts=1, latency_ms=400),
                built.evidence(SEARCH, ok=1, attempts=1, latency_ms=500)]
        verdict = evaluate(built, slow)
        self.assertFalse(verdict['pass'])
        self.assertEqual(verdict['targets'][SEARCH]['reason'], 'E_TARGET_LATENCY_EXCEEDED')
        unmeasured = [built.evidence(BASIC, ok=1, attempts=1, latency_ms=None),
                      built.evidence(SEARCH, ok=1, attempts=1, latency_ms=50)]
        verdict = evaluate(built, unmeasured)
        self.assertEqual(verdict['targets'][BASIC]['reason'], 'E_TARGET_LATENCY_MISSING')
        self.assertEqual(verdict['targets'][BASIC]['state'], 'fail')
        loose = [built.evidence(BASIC, ok=1, attempts=1, latency_ms=400),
                 built.evidence(SEARCH, ok=1, attempts=1, latency_ms=0)]
        self.assertTrue(evaluate(built, loose)['pass'])

    def test_lowering_a_threshold_does_not_buy_a_pass_for_an_unmeasured_probe(self):
        """Ключевое обещание F05: измерение не переносится под другой порог."""
        strict = spec(targets=[target(BASIC, min_success=1.0, max_latency_ms=200)])
        loose = spec(targets=[target(BASIC, min_success=1.0, max_latency_ms=2000)])
        self.assertNotEqual(strict.target(BASIC).fingerprint(), loose.target(BASIC).fingerprint())
        measured_under_strict = strict.evidence(BASIC, ok=1, attempts=1, latency_ms=900)
        self.assertFalse(evaluate(strict, [measured_under_strict])['pass'])
        verdict = evaluate(loose, [measured_under_strict])
        self.assertFalse(verdict['pass'], 'ослабление порога не выдало pass')
        self.assertEqual(verdict['targets'][BASIC]['state'], 'unmeasured')
        self.assertEqual(verdict['targets'][BASIC]['reason'], 'E_TARGET_STALE_EVIDENCE')
        remeasured = loose.evidence(BASIC, ok=1, attempts=1, latency_ms=900)
        self.assertTrue(evaluate(loose, [remeasured])['pass'])
        # смена правила и kind - это не смена измерения
        moved = spec(targets=[target(BASIC, min_success=1.0, max_latency_ms=200),
                              target(VIDEO, 'optional')], optional_rule='any')
        self.assertEqual(moved.target(BASIC).fingerprint(), strict.target(BASIC).fingerprint())
        self.assertTrue(evaluate(moved, [moved.evidence(BASIC, ok=1, attempts=1, latency_ms=100),
                                         moved.evidence(VIDEO, ok=1, attempts=1)])['pass'])

    def test_a_budget_below_the_minimum_is_refused_and_a_reached_budget_stops(self):
        with self.assertRaises(profiles.ProfileError) as refused:
            spec(targets=[target(BASIC, min_success=0.5)], attempts=3,
                 budget={'max_probes': 2})
        self.assertEqual(refused.exception.code, profiles.E_VALIDATION_FIELD)
        built = spec(targets=[target(BASIC, min_success=0.5), target(SEARCH, 'optional',
                                                                    min_success=0.5)],
                     optional_rule='any', attempts=2, budget={'max_probes': 4})
        run = RunState(built)
        for step, target_id in enumerate((BASIC, SEARCH, BASIC, SEARCH), 1):
            run.record(target_id, ok=1)
            decision = run.stop() if step < 4 else run.stop()
        self.assertEqual(run.probes_used, 4)
        self.assertTrue(decision.stop)
        self.assertEqual(decision.reason, profiles.E_LIMIT_BUDGET)
        self.assertTrue(run.evaluate()['pass'], 'бюджет может остановить прогон, но не испортить вердикт')
        timed = spec(targets=[target(BASIC, min_success=0.5)], attempts=3,
                     budget={'max_duration_s': 5})
        run = RunState(timed)
        run.record(BASIC, ok=1)
        self.assertFalse(run.stop(elapsed_s=3.0).stop)
        self.assertEqual(run.stop(elapsed_s=5.0).reason, profiles.E_LIMIT_BUDGET)

    def test_the_optional_rule_need_and_the_minimum_probe_count_agree(self):
        for mode, k, optional, needed in (('all', 1, 2, 2), ('any', 1, 2, 1),
                                          ('at_least', 2, 3, 2), ('none', 1, 0, 0)):
            with self.subTest(rule=mode, k=k):
                built = spec(targets=[target(BASIC)] + [target(f'opt-{index}', 'optional')
                                                        for index in range(optional)],
                             optional_rule=mode, k=k, attempts=1)
                self.assertEqual(built.plan()['optional_needed'], needed)
                want = OptionalRule.parse(mode, k).need(optional)
                self.assertEqual(optional if want is None else want, needed)
                self.assertEqual(built.min_probes, 1 + needed)


class FailFastTests(unittest.TestCase):
    #: Every rule, every probe outcome prefix, and the best possible completion.
    CASES = {
        'all': dict(optional_rule='all', attempts=2),
        'any': dict(optional_rule='any', attempts=2),
        'at_least 2': dict(optional_rule='at_least', k=2, attempts=2),
        'none': dict(optional_rule='none', attempts=2),
        'single, 3 попытки': dict(optional_rule='none', attempts=3,
                                   targets=[target(BASIC, min_success=0.34)]),
    }
    RULE_REASONS = ('E_VERDICT_REQUIRED_NOT_PASSED', 'E_VERDICT_OPTIONAL_NOT_PASSED',
                    'E_VERDICT_EMPTY_TARGET_SET', 'E_VERDICT_K_NOT_POSITIVE',
                    'E_VERDICT_K_UNREACHABLE', 'E_VERDICT_EMPTY_OPTIONAL_SET')

    def test_a_stop_never_disagrees_with_the_rule(self):
        checked = stops = 0
        for label, kwargs in self.CASES.items():
            built = spec(targets=kwargs.pop('targets', None) or
                         [target(BASIC, min_success=0.5), target(SEARCH, 'optional', min_success=0.5),
                          target(VIDEO, 'optional', min_success=0.5)], **kwargs)
            ids = [rule.id for rule in built.enabled_targets]
            width = len(ids) * built.attempts
            for combo in itertools.product((0, 1), repeat=width):
                with self.subTest(profile=label, probes=combo):
                    run = RunState(built)
                    for index, ok in enumerate(combo):
                        run.record(ids[index % len(ids)], ok=ok)
                    decision = run.stop()
                    for target_id in run.pending_targets():
                        for _ in range(built.attempts):
                            run.record(target_id, ok=1)
                    best = evaluate(built, [run.evidence(rule.id) for rule in built.targets])
                    checked += 1
                    if decision.stop and decision.reason in self.RULE_REASONS:
                        stops += 1
                        self.assertFalse(best['pass'],
                                         f'{decision.as_dict()} противоречит правилу: {combo}')
                    if not decision.stop:
                        self.assertTrue(best['pass'],
                                        f'остановиться нельзя, а проход недостижим: {combo}')
        self.assertGreater(checked, 100)
        self.assertGreater(stops, 0)

    def test_a_target_with_attempts_left_is_live_while_it_can_still_pass(self):
        built = spec(targets=[target(BASIC, min_success=0.5)], attempts=2)
        run = RunState(built)
        run.record(BASIC, ok=0)
        self.assertFalse(run.stop().stop, 'один провал из двух не убивает порог 0.5')
        self.assertEqual(run.state(BASIC), 'fail', 'но текущее состояние - провал')
        run.record(BASIC, ok=1)
        self.assertTrue(run.evaluate()['pass'])
        strict = spec(targets=[target(BASIC, min_success=1.0)], attempts=2)
        run = RunState(strict)
        run.record(BASIC, ok=0)
        self.assertTrue(run.stop().stop, 'порог 1.0 не оставляет шанса')

    def test_pending_targets_after_a_stop_are_skipped_not_measured(self):
        built = spec(targets=[target(BASIC), target(SEARCH, 'optional')], optional_rule='all',
                     attempts=1)
        run = RunState(built)
        run.record(BASIC, ok=1)
        self.assertEqual(run.pending_targets(), [SEARCH])
        run.record(SEARCH, ok=0)
        self.assertTrue(run.stop().stop)
        self.assertEqual([decision.as_dict() for decision in run.as_decisions()],
                         [{'target_id': BASIC, 'done': 1, 'ok': 1, 'latency_ms': None,
                           'unknown': False},
                          {'target_id': SEARCH, 'done': 1, 'ok': 0, 'latency_ms': None,
                           'unknown': False}])

    def test_recording_a_disabled_target_or_an_extra_probe_is_refused(self):
        built = spec(targets=[target(BASIC), target(SEARCH, 'optional', enabled=False)],
                     optional_rule='none')
        run = RunState(built)
        with self.assertRaises(profiles.ProfileError):
            run.record(SEARCH, ok=1)
        with self.assertRaises(profiles.ProfileError) as extra:
            run.record(BASIC, ok=5)
        self.assertEqual(extra.exception.code, profiles.E_VALIDATION_FIELD)


# --------------------------------------------------------------------------- #
# Паритет GUI / CLI / API
# --------------------------------------------------------------------------- #
def gui_document(built, evidence):
    return {'profile': built.as_dict(),
            'evidence': {item.target_id: item.as_dict() for item in evidence}}


def cli_document(built, evidence):
    namespace = argparse.Namespace(
        targets=[dict(id=rule.id, kind=rule.kind, enabled=rule.enabled,
                      min_success=rule.min_success, max_latency_ms=rule.max_latency_ms)
                 for rule in built.targets],
        optional_rule=built.optional_rule.mode, k=built.optional_rule.k,
        attempts=built.attempts, max_probes=built.budget.max_probes,
        max_duration_s=built.budget.max_duration_s, description=built.description)
    return {'profile': {'version': profiles.CONFIG_VERSION, 'targets': namespace.targets,
                        'optional_rule': namespace.optional_rule, 'k': namespace.k,
                        'attempts': namespace.attempts,
                        'budget': {'max_probes': namespace.max_probes,
                                   'max_duration_s': namespace.max_duration_s},
                        'description': namespace.description},
            'evidence': [item.as_dict() for item in evidence]}


def api_document(built, evidence):
    return json.loads(json.dumps({'profile': built.as_dict(),
                                  'evidence': [item.as_dict() for item in evidence]}))


class ParityTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix='pw_areacat_parity_')) / 'workbench.db'
        self.store = profiles.open_store(self.path)
        self.addCleanup(self.store.close)

    def test_gui_cli_and_api_return_the_same_verdict_and_composition(self):
        built = spec(targets=[target(BASIC, min_success=1.0, max_latency_ms=1500),
                              target(SEARCH, 'optional', min_success=0.5),
                              target(VIDEO, 'optional', min_success=0.5)],
                     optional_rule='at_least', k=1, attempts=2, budget={'max_probes': 6})
        evidence = [built.evidence(BASIC, ok=1, attempts=1, latency_ms=900),
                    built.evidence(SEARCH, ok=1, attempts=1, latency_ms=800),
                    built.evidence(VIDEO, ok=0, attempts=1, latency_ms=1200)]
        payloads = {'gui': gui_document(built, evidence),
                    'cli': cli_document(built, evidence),
                    'api': api_document(built, evidence)}
        verdicts = {name: run_request(document) for name, document in payloads.items()}
        reference = json.dumps(verdicts['gui'], sort_keys=True, ensure_ascii=False)
        for name, verdict in verdicts.items():
            with self.subTest(surface=name):
                self.assertEqual(json.dumps(verdict, sort_keys=True, ensure_ascii=False),
                                 reference)
        self.assertTrue(verdicts['gui']['pass'])
        self.assertEqual(verdicts['gui']['digest'], built.digest)

        ref = self.store.create('Проект', built, at=START)
        by_id = run_request({'profile_id': ref.profile_id, 'profile_revision': 1,
                             'evidence': [item.as_dict() for item in evidence]},
                            store=self.store)
        self.assertEqual(by_id['digest'], built.digest)
        self.assertEqual(by_id['pass'], verdicts['gui']['pass'])
        self.assertEqual(by_id['reason'], verdicts['gui']['reason'])
        with self.assertRaises(profiles.ProfileError):
            run_request({'profile_id': ref.profile_id})

    def test_saving_a_new_revision_does_not_move_the_previous_measurement_versions(self):
        built = spec(targets=[target(BASIC, min_success=1.0, max_latency_ms=1500)],
                     attempts=1, description='v1')
        ref = self.store.create('Проект', built, at=START)
        evidence = [built.evidence(BASIC, ok=1, attempts=1, latency_ms=900)]
        before = run_request({'profile_id': ref.profile_id, 'profile_revision': 1,
                              'evidence': [item.as_dict() for item in evidence]},
                             store=self.store)
        self.assertTrue(before['pass'])
        self.store.update(ref.profile_id, built.copy_of(description='v2',
                                                         budget={'max_probes': 4}),
                         base_revision=1, at=START + 1)
        head = run_request({'profile_id': ref.profile_id,
                            'evidence': [item.as_dict() for item in evidence]},
                           store=self.store)
        old = run_request({'profile_id': ref.profile_id, 'profile_revision': 1,
                           'evidence': [item.as_dict() for item in evidence]},
                          store=self.store)
        self.assertNotEqual(head['digest'], old['digest'], 'ревизии различимы')
        self.assertEqual(head['profile']['budget'], {'max_probes': 4, 'max_duration_s': None})
        self.assertEqual(old['profile']['budget'], {'max_probes': None, 'max_duration_s': None})
        self.assertEqual(old['digest'], before['digest'])
        self.assertEqual(old['pass'], before['pass'])
        # документ старой ревизии экспортируется и импортируется без смешения версий
        document = export_profile(self.store, ref.profile_id, revision=1, at=START + 2)
        self.assertEqual(document['revisions'][0]['config']['budget']['max_probes'], None)
        self.assertEqual(len(document['revisions']), 1)
        back = import_profile(self.store, document, name='Старая', at=START + 3)
        self.assertEqual(self.store.get(back.profile_id, 1).digest,
                         self.store.get(ref.profile_id, 1).digest)

    def test_a_run_request_refuses_an_unknown_field_or_a_missing_store(self):
        built = spec()
        with self.assertRaises(profiles.ProfileError) as unknown:
            run_request({'profile': built.as_dict(), 'evidence': [], 'лишнее': 1})
        self.assertEqual(unknown.exception.code, profiles.E_VALIDATION_UNKNOWN_FIELD)
        with self.assertRaises(profiles.ProfileError) as no_store:
            run_request({'profile_id': 'p_x', 'evidence': []})
        self.assertEqual(no_store.exception.code, profiles.E_VALIDATION_FIELD)

    def test_validation_rejects_nonsense_rather_than_ignoring_it(self):
        cases = (dict(targets=[{ 'id': 'Плохое Имя' }]),
                 dict(targets=[target(BASIC, min_success=0)]),
                 dict(targets=[target(BASIC, min_success=1.5)]),
                 dict(targets=[target(BASIC, kind='обязательный')]),
                 dict(targets=[target(BASIC)], optional_rule='полу-все'),
                 dict(targets=[target(BASIC)], attempts=0),
                 dict(targets=[target(BASIC), target(BASIC)]),
                 dict(targets=[target(BASIC, enabled='да')]),
                 dict(targets=[{'id': BASIC, 'неизвестное': 1}]))
        for kwargs in cases:
            with self.subTest(case=kwargs):
                with self.assertRaises(profiles.ProfileError):
                    ProfileSpec.create(**kwargs)

    def test_target_fingerprints_ignore_kind_and_rule_but_not_thresholds(self):
        rule = TargetRule(BASIC, 'required', min_success=0.5, max_latency_ms=100)
        same_shape = TargetRule(BASIC, 'optional', min_success=0.5, max_latency_ms=100)
        other = TargetRule(BASIC, 'required', min_success=0.5, max_latency_ms=200)
        self.assertEqual(profiles.target_fingerprint(rule),
                         profiles.target_fingerprint(same_shape))
        self.assertNotEqual(profiles.target_fingerprint(rule),
                            profiles.target_fingerprint(other))
        self.assertEqual(profiles.target_fingerprint(rule.as_dict()),
                         profiles.target_fingerprint(rule))


if __name__ == '__main__':
    unittest.main()

"""F02/F03 живым поведением: коллекции, scope, все входы импорта, дефект 10.

Каждый тест выполняет настоящий вызов модуля на временной базе и проверяет
наблюдаемый результат, а не исходный текст.  Сети нет: адреса - только
идентификаторы строк в локальной БД, ни один из них не опрашивается.

Закрывает:
* F02 - создание/переименование/архивирование, membership, изоляция scope,
  миграция старых данных без выдуманного происхождения;
* F03 - файл/drop/буфер, txt/uri/csv/json, preview с номерами строк, mapping,
  merge/replace выбранной коллекции, отмена, revision conflict, идемпотентный
  commit, импортный отчёт;
* дефект 10 - hostname/приватные адреса/auth отклоняются сразу и одинаково;
* F04 (canary) - пароль не попадает в raw input, лог и provenance на диск.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import db
from proxy_workbench import importer as imp

#: Значение, которое не имеет права пережить ни одна строка отчёта, лога или БД.
#: Реальных учётных данных здесь нет и быть не может: это проверка формы.
CANARY = 'canary-pw-4f81c0-do-not-persist'

#: Глобально маршрутизируемые адреса.  Они используются ТОЛЬКО как значения
#: строк в локальной БД; ни один запрос по ним не выполняется.
PUBLIC_A = 'http://45.33.32.156:8080'
PUBLIC_B = 'socks5://185.220.101.5:1080'
PUBLIC_C = 'http://91.198.174.192:3128'


class Base(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix='pw_areacat_import_'))
        self.path = self.work / 'workbench.db'
        db.migrate(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)

    # -- helpers ----------------------------------------------------------- #
    def plan(self, text, collection_id, mode='merge', *, name='f.txt', key=None,
             policy=None, fmt=None, mapping=None):
        source = imp.ImportSource.from_text(text, name=name)
        return imp.preview(self.conn, source, collection_id=collection_id, mode=mode,
                           idempotency_key=key, policy=policy, fmt=fmt, mapping=mapping)

    def members(self, collection_id):
        return [row['canonical'] for row in db.collection_members(self.conn, collection_id)]

    def tables(self):
        return {name: self.conn.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                for name in ('endpoints', 'membership', 'import_batch')}


# --------------------------------------------------------------------------- #
# F02: коллекции и scope
# --------------------------------------------------------------------------- #
class CollectionScopeTests(Base):
    def test_migration_starts_with_a_public_base_and_an_unnamed_legacy_list(self):
        """Публичная база и «Ранее собранные» - разные строки, не один список."""
        collections = {row['id']: row for row in db.list_collections(self.conn)}
        self.assertEqual(db.PUBLIC_COLLECTION_ID, 'public-base')
        self.assertEqual(collections[db.PUBLIC_COLLECTION_ID]['kind'], 'public')
        self.assertEqual(collections[db.LEGACY_COLLECTION_ID]['kind'], 'public')
        self.assertNotIn(db.PUBLIC_COLLECTION_ID, db.LEGACY_COLLECTION_ID)
        # Дефект 11: личная коллекция не рождается наполненной публичными адресами.
        self.assertEqual(self.members(db.PUBLIC_COLLECTION_ID), [])

    def test_create_is_find_or_create_and_a_personal_list_starts_empty(self):
        first = imp.create_collection(self.conn, 'Мои A', 'private')
        again = imp.create_collection(self.conn, 'Мои A', 'private')
        second = imp.create_collection(self.conn, 'Мои B', 'private')
        self.assertEqual(first, again, 'повторный вызов с тем же именем и типом даёт ту же коллекцию')
        self.assertNotEqual(first, second)
        self.assertEqual(self.members(first), [])
        self.assertEqual(self.members(db.PUBLIC_COLLECTION_ID), [],
                         'создание личной коллекции не тронуло публичную базу')
        with self.assertRaises(imp.ImportProblem) as raised:
            imp.create_collection(self.conn, '  ', 'private')
        self.assertEqual(raised.exception.code, imp.CODE_FIELD)
        with self.assertRaises(imp.ImportProblem):
            imp.create_collection(self.conn, 'Мои C', 'выдумка')

    def test_two_personal_lists_and_the_public_base_never_mix(self):
        """Приёмка F02: адрес из A отсутствует в выдаче B и наоборот."""
        a = imp.create_collection(self.conn, 'A', 'private')
        b = imp.create_collection(self.conn, 'B', 'private')
        imp.commit(self.conn, self.plan(f'{PUBLIC_A}\n{PUBLIC_B}\n', a))
        imp.commit(self.conn, self.plan(f'{PUBLIC_C}\n', b))
        self.assertIn(PUBLIC_A, self.members(a))
        self.assertNotIn(PUBLIC_A, self.members(b), 'адрес A не должен появляться в B')
        self.assertIn(PUBLIC_C, self.members(b))
        self.assertNotIn(PUBLIC_C, self.members(a), 'адрес B не должен появляться в A')

    def test_replace_in_one_collection_does_not_touch_another(self):
        """Приёмка F02: работа в одной не меняет подключённую другую."""
        a = imp.create_collection(self.conn, 'A', 'private')
        b = imp.create_collection(self.conn, 'B', 'private')
        imp.commit(self.conn, self.plan(f'{PUBLIC_A}\n{PUBLIC_B}\n', a))
        imp.commit(self.conn, self.plan(f'{PUBLIC_C}\n', b))
        before_b = self.members(b)
        report = imp.commit(self.conn, self.plan(f'{PUBLIC_A}\n', a, mode='replace'))
        # адрес уже был в A: он остаётся, но новым не считается
        self.assertEqual(report.added, ())
        self.assertEqual(report.counts['already_member'], 1)
        self.assertEqual(report.removed, (PUBLIC_B,))
        self.assertEqual(self.members(a), [PUBLIC_A])
        self.assertEqual(self.members(b), before_b, 'коллекция B не изменилась')
        self.assertEqual(self.members(db.PUBLIC_COLLECTION_ID), [])

    def test_one_address_may_belong_to_several_collections(self):
        a = imp.create_collection(self.conn, 'A', 'private')
        b = imp.create_collection(self.conn, 'B', 'private')
        shared = 'http://5.255.255.5:8888\n'
        for collection in (a, b, db.PUBLIC_COLLECTION_ID):
            imp.commit(self.conn, self.plan(shared, collection))
        identifier = db.endpoint_id(shared.strip())
        self.assertEqual(db.endpoint_collections(self.conn, identifier),
                         sorted([a, b, db.PUBLIC_COLLECTION_ID]))
        # один адрес - одна строка endpoints, три membership
        self.assertEqual(self.tables()['endpoints'], 1)
        self.assertEqual(self.tables()['membership'], 3)

    def test_removing_a_membership_keeps_the_address_and_the_other_lists(self):
        a = imp.create_collection(self.conn, 'A', 'private')
        b = imp.create_collection(self.conn, 'B', 'private')
        shared = 'http://5.255.255.5:8888\n'
        for collection in (a, b, db.PUBLIC_COLLECTION_ID):
            imp.commit(self.conn, self.plan(shared, collection))
        identifier = db.endpoint_id(shared.strip())
        self.assertTrue(db.remove_member(self.conn, a, identifier))
        self.assertNotIn(shared.strip(), self.members(a))
        self.assertIn(shared.strip(), self.members(b), 'удаление из A не убрало адрес из B')
        self.assertIn(shared.strip(), self.members(db.PUBLIC_COLLECTION_ID))
        self.assertIsNotNone(
            self.conn.execute('SELECT 1 FROM endpoints WHERE id = ?', (identifier,)).fetchone(),
            'строка адреса осталась: удаление membership не удаляет адрес')

    def test_rename_and_archive_hide_a_list_without_deleting_it(self):
        a = imp.create_collection(self.conn, 'A', 'private')
        imp.commit(self.conn, self.plan(f'{PUBLIC_A}\n', a))
        db.rename_collection(self.conn, a, 'A (рабочие)')
        self.assertIn('A (рабочие)', [row['name'] for row in db.list_collections(self.conn)])
        db.archive_collection(self.conn, a, now=500.0)
        live = {row['id'] for row in db.list_collections(self.conn)}
        everything = {row['id'] for row in db.list_collections(self.conn, include_archived=True)}
        self.assertNotIn(a, live)
        self.assertIn(a, everything)
        self.assertEqual(self.members(a), [PUBLIC_A], 'архивирование скрывает список, а не удаляет')

    def test_legacy_candidates_migrate_with_an_honest_origin(self):
        """Старые данные получают origin='legacy', а не выдуманное происхождение."""
        self.conn.close()
        legacy = self.work / 'old.db'
        raw = sqlite3.connect(legacy)
        raw.executescript('CREATE TABLE candidates(proxy TEXT PRIMARY KEY);'
                          'CREATE TABLE candidate_meta(proxy TEXT PRIMARY KEY, country TEXT);')
        raw.execute("INSERT INTO candidates VALUES ('http://45.33.32.156:8080')")
        raw.execute("INSERT INTO candidate_meta VALUES ('http://45.33.32.156:8080', 'NL')")
        raw.commit()
        raw.close()
        db.migrate(legacy)
        conn = db.connect(legacy)
        self.addCleanup(conn.close)
        rows = list(conn.execute('SELECT e.canonical, e.country, m.collection_id, m.origin'
                                 ' FROM endpoints e JOIN membership m ON m.endpoint_id = e.id'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], 'legacy', 'происхождение не выдумано')
        self.assertEqual(rows[0][2], db.LEGACY_COLLECTION_ID)
        self.assertEqual(rows[0][1], 'NL', 'страна из старой базы сохранена')
        self.assertEqual(db.legacy_summary(conn)['public_base_members'], 0,
                         'миграция не наполнила публичную базу')
        # второй прогон ничего не удваивает
        db.migrate(legacy)
        again = db.connect(legacy)
        self.addCleanup(again.close)
        self.assertEqual(again.execute('SELECT count(*) FROM endpoints').fetchone()[0], 1)
        self.assertEqual(again.execute('SELECT count(*) FROM membership').fetchone()[0], 1)


# --------------------------------------------------------------------------- #
# F03: входы, форматы, preview
# --------------------------------------------------------------------------- #
MIXED_TXT = (
    '# комментарий\n'
    f'{PUBLIC_A}\n'
    '\n'
    '91.198.174.192:3128\n'
    'socks5://185.220.101.5:1080\n'
    'мусор не адрес\n'
    f'{PUBLIC_A}\n'
    '203.0.113.7:8080\n'
    f'http://user:{CANARY}@104.244.72.115:8080\n'
    'http://proxy.example.com:8080\n'
    '10.0.0.5:3128\n'
)


class ChannelAndPreviewTests(Base):
    def setUp(self):
        super().setUp()
        self.collection = imp.create_collection(self.conn, 'Ch', 'private')
        self.list_path = self.work / 'list.txt'
        self.list_path.write_text(MIXED_TXT, encoding='utf-8')

    def channels(self):
        payload = self.list_path.read_bytes()
        return {
            'file': imp.ImportSource.from_path(self.list_path, channel='file'),
            'drop-bytes': imp.ImportSource.from_drop(payload, name='list.txt',
                                                     path=self.list_path),
            'drop-text': imp.ImportSource.from_drop(MIXED_TXT, name='dropped'),
            'clipboard': imp.ImportSource.from_text(MIXED_TXT, name='clipboard',
                                                    channel='clipboard'),
        }

    def test_every_channel_produces_the_same_decisions(self):
        decisions = set()
        for source in self.channels().values():
            plan = imp.preview(self.conn, source, collection_id=self.collection, fmt='txt')
            decisions.add(json.dumps([(row.line, row.state, row.reason, row.canonical)
                                      for row in plan.rows], sort_keys=True))
        self.assertEqual(len(decisions), 1,
                         'файл, drop и буфер обязаны решать одинаково')

    def test_preview_reports_valid_duplicates_and_rejected_with_line_numbers(self):
        plan = imp.preview(self.conn, imp.ImportSource.from_path(self.list_path),
                           collection_id=self.collection, fmt='txt')
        by_line = {row.line: row for row in plan.rows}
        self.assertEqual(by_line[2].canonical, PUBLIC_A)
        self.assertEqual(by_line[4].state, imp.VALID)
        self.assertEqual(by_line[5].state, imp.VALID)
        self.assertEqual(by_line[6].reason, imp.CODE_FORMAT)
        self.assertEqual(by_line[7].reason, imp.ROW_DUPLICATE_IN_SOURCE)
        self.assertEqual(by_line[7].duplicate_of, 2, 'дубликат ссылается на первую строку')
        self.assertEqual(by_line[7].canonical, PUBLIC_A, 'дубликат сохраняет адрес для replace')
        self.assertEqual(plan.counts['rejected'], 5)
        self.assertEqual(plan.skipped, 2, 'пустая строка и комментарий посчитаны отдельно')
        self.assertEqual(plan.reasons[imp.ROW_CREDENTIALS], 1)
        self.assertEqual(plan.reasons[imp.ROW_HOSTNAME], 1)
        self.assertEqual(plan.reasons[imp.ROW_PRIVATE], 2)

    def test_preview_writes_nothing_and_opens_no_transaction(self):
        before = self.tables()
        self.assertFalse(self.conn.in_transaction)
        for source in self.channels().values():
            imp.preview(self.conn, source, collection_id=self.collection, fmt='txt')
        self.assertEqual(self.tables(), before)
        self.assertFalse(self.conn.in_transaction)

    def test_format_detection(self):
        cases = {'45.33.32.156:8080\n91.198.174.192:3128\n': 'txt',
                 'http://45.33.32.156:8080\n': 'uri',
                 'host,port\n45.33.32.156,8080\n': 'csv',
                 '45.33.32.156\t8080\n91.198.174.192\t3128\n': 'csv',
                 '[{"host":"45.33.32.156","port":8080}]': 'json',
                 '{"proxies": ["45.33.32.156:8080"]}': 'json'}
        for text, expected in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(imp.detect_format(imp.ImportSource.from_text(text)), expected)

    def test_uri_format_requires_a_scheme_and_txt_does_not(self):
        text = 'http://45.33.32.156:8080\n91.198.174.192:3128\n'
        uri = imp.preview(self.conn, imp.ImportSource.from_text(text), collection_id=self.collection,
                          fmt='uri')
        self.assertEqual([row.state for row in uri.rows], [imp.VALID, imp.REJECTED])
        plain = imp.preview(self.conn, imp.ImportSource.from_text(text), collection_id=self.collection,
                            fmt='txt')
        self.assertEqual([row.state for row in plain.rows], [imp.VALID, imp.VALID])

    def test_json_list_of_strings_and_of_objects(self):
        strings = json.dumps([PUBLIC_A, 'socks5://185.220.101.5:1080', '10.0.0.1:80'])
        plan = imp.preview(self.conn, imp.ImportSource.from_text(strings), collection_id=self.collection,
                           fmt='json')
        self.assertEqual([row.state for row in plan.rows],
                         [imp.VALID, imp.VALID, imp.REJECTED])
        objects = json.dumps({'proxies': [{'host': '45.33.32.156', 'port': 8080, 'country': 'NL'},
                                          {'host': '10.0.0.1', 'port': 3128, 'country': 'US'},
                                          {'host': 'a.example', 'port': 80}]})
        plan = imp.preview(self.conn, imp.ImportSource.from_text(objects),
                           collection_id=self.collection, fmt='json')
        self.assertEqual(plan.rows[0].country, 'NL')
        self.assertEqual(plan.rows[0].canonical, PUBLIC_A)
        self.assertEqual(plan.rows[1].reason, imp.ROW_PRIVATE)
        self.assertEqual(plan.rows[2].reason, imp.ROW_HOSTNAME)
        # объект, у которого в поле host сидит userinfo
        userinfo = json.dumps([{'host': f'http://u:{CANARY}@104.244.72.115:8080', 'port': ''}])
        plan = imp.preview(self.conn, imp.ImportSource.from_text(userinfo),
                           collection_id=self.collection, fmt='json')
        self.assertEqual(plan.rows[0].reason, imp.ROW_CREDENTIALS)
        self.assertNotIn(CANARY, plan.rows[0].sample)

    def test_a_file_that_offers_a_credential_column_is_refused_as_a_whole(self):
        """Принять строку и молча выбросить поле пароля - тот же дефект, что и наоборот."""
        csv_text = 'host,port,username,password\n45.33.32.156,8080,,\n91.198.174.192,3128,,\n'
        plan = imp.preview(self.conn, imp.ImportSource.from_text(csv_text),
                           collection_id=self.collection, fmt='csv')
        self.assertEqual([row.reason for row in plan.rows],
                         [imp.ROW_CREDENTIALS, imp.ROW_CREDENTIALS])
        self.assertEqual(plan.rows[0].detail, 'колонки: username, password')
        objects = json.dumps([{'host': '45.33.32.156', 'port': 8080},
                              {'host': '91.198.174.192', 'port': 3128, 'password': CANARY}])
        plan = imp.preview(self.conn, imp.ImportSource.from_text(objects),
                           collection_id=self.collection, fmt='json')
        self.assertEqual([row.reason for row in plan.rejected], [imp.ROW_CREDENTIALS] * 2)
        self.assertNotIn(CANARY, json.dumps(plan.to_dict(), ensure_ascii=False))

    def test_a_header_without_a_role_word_is_still_mappable_by_name(self):
        """Без этого файл `a,b,c` нельзя было разобрать по именам колонок."""
        text = 'a,b,c\n45.33.32.156,8080,NL\n185.220.101.5,1080,DE\n'
        plan = imp.preview(self.conn, imp.ImportSource.from_text(text), collection_id=self.collection,
                           fmt='csv', mapping=imp.ColumnMapping(host='a', port='b', country='c'))
        self.assertEqual(list(plan.header), ['a', 'b', 'c'])
        self.assertEqual([row.state for row in plan.rows], [imp.VALID, imp.VALID])
        self.assertEqual(plan.rows[0].country, 'NL')
        self.assertEqual(plan.rows[0].line, 2, 'первая строка прочитана как заголовок, не как данные')
        report = imp.commit(self.conn, plan)
        self.assertEqual(report.state, 'committed', 'файл без мусорной строки коммитится без флагов')

    def test_a_role_header_is_suggested_and_an_unknown_one_asks_the_user(self):
        plan = imp.preview(self.conn,
                           imp.ImportSource.from_text('host,port,protocol,country\n'
                                                      '45.33.32.156,8080,http,NL\n'),
                           collection_id=self.collection, fmt='csv')
        self.assertFalse(plan.needs_mapping)
        self.assertEqual(plan.mapping.to_dict()['host'], 'host')
        unknown = imp.preview(self.conn, imp.ImportSource.from_text('alpha,beta\n1.2.3.4,80\n'),
                              collection_id=self.collection, fmt='csv')
        self.assertTrue(unknown.mapping is not None or unknown.needs_mapping)
        partial = imp.preview(self.conn,
                              imp.ImportSource.from_text('server_addr,tcp_port,cc\n'
                                                         '45.33.32.156,8080,NL\n'),
                              collection_id=self.collection, fmt='csv')
        self.assertTrue(partial.needs_mapping)
        self.assertEqual(partial.mapping_problem[0], ('host',))
        with self.assertRaises(imp.ImportFormatError):
            imp.commit(self.conn, partial)

    def test_mapping_by_index_is_equivalent_and_bad_mappings_are_refused(self):
        text = 'a,b\n45.33.32.156,8080\n'
        by_name = imp.preview(self.conn, imp.ImportSource.from_text(text),
                              collection_id=self.collection, fmt='csv',
                              mapping=imp.ColumnMapping(host='a', port='b'))
        by_index = imp.preview(self.conn, imp.ImportSource.from_text(text),
                               collection_id=self.collection, fmt='csv',
                               mapping=imp.ColumnMapping(host=0, port=1), header=True)
        self.assertEqual([row.canonical for row in by_name.rows],
                         [row.canonical for row in by_index.rows])
        with self.assertRaises(imp.ImportFormatError):
            imp.preview(self.conn, imp.ImportSource.from_text(text), collection_id=self.collection,
                        fmt='csv', mapping=imp.ColumnMapping(host='нет', port='b'))
        with self.assertRaises(imp.ImportFormatError):
            imp.preview(self.conn, imp.ImportSource.from_text(text), collection_id=self.collection,
                        fmt='csv', mapping=imp.ColumnMapping(port='b'))
        with self.assertRaises(imp.ImportFormatError):
            imp.preview(self.conn, imp.ImportSource.from_text(text), collection_id=self.collection,
                        fmt='csv', mapping=imp.ColumnMapping(host=7, port=0))


# --------------------------------------------------------------------------- #
# F03: commit - отмена, сбой, идемпотентность, конфликт, отчёт
# --------------------------------------------------------------------------- #
class CommitTests(Base):
    def setUp(self):
        super().setUp()
        self.a = imp.create_collection(self.conn, 'A', 'private')
        self.b = imp.create_collection(self.conn, 'B', 'private')
        imp.commit(self.conn, self.plan(f'{PUBLIC_A}\n{PUBLIC_B}\n', self.a))
        imp.commit(self.conn, self.plan(f'{PUBLIC_C}\n', self.b))

    def test_partial_file_is_refused_then_fixable(self):
        bad = f'{PUBLIC_A}\nмусор\n'
        plan = self.plan(bad, self.a, name='bad.txt')
        with self.assertRaises(imp.ImportPartialBlocked) as raised:
            imp.commit(self.conn, plan)
        self.assertEqual(raised.exception.code, imp.CODE_PARTIAL)
        self.assertEqual(raised.exception.detail['reasons'][imp.CODE_FORMAT], 1)
        self.assertEqual(self.tables()['membership'], 3, 'отказ ничего не записал')
        confirmed = imp.commit(self.conn, self.plan(bad, self.a, name='bad.txt'),
                               allow_partial=True)
        self.assertEqual([row['line'] for row in confirmed.rejected], [2])
        fixed = imp.commit(self.conn, self.plan(PUBLIC_A + '\n', self.a, name='fixed.txt'))
        self.assertEqual(fixed.counts['rejected'], 0)
        self.assertEqual(fixed.counts['already_member'], 1)

    def test_a_crash_mid_commit_leaves_no_half_replace_and_the_batch_can_retry(self):
        before = self.members(self.a)
        plan = self.plan(f'{PUBLIC_A}\n{PUBLIC_B}\n{PUBLIC_C}\n', self.a, mode='replace',
                         name='crash.txt')
        calls = {'n': 0}

        def progress(phase, done, total):
            calls['n'] += 1
            if done == 2:
                raise RuntimeError('сбой писателя')

        with self.assertRaises(RuntimeError):
            imp.commit(self.conn, plan, on_progress=progress)
        self.assertEqual(self.members(self.a), before, 'половины replace не осталось')
        state = self.conn.execute('SELECT state FROM import_batch WHERE id = ?',
                                  (plan.batch_id,)).fetchone()
        self.assertEqual(state[0], 'failed')
        retried = imp.commit(self.conn, plan)
        self.assertEqual(retried.state, 'committed')
        self.assertEqual(sorted(self.members(self.a)),
                         sorted([PUBLIC_A, PUBLIC_B, PUBLIC_C]))

    def test_cancellation_rolls_back_and_can_be_retried(self):
        before = self.members(self.a)
        plan = self.plan(f'{PUBLIC_C}\n185.220.101.5:1080\n', self.a, mode='replace',
                         name='cancel.txt')
        state = {'n': 0}

        def cancel():
            state['n'] += 1
            return state['n'] >= 2

        with self.assertRaises(imp.ImportCancelled) as raised:
            imp.commit(self.conn, plan, should_cancel=cancel)
        self.assertEqual(raised.exception.code, imp.CODE_CANCELLED)
        self.assertEqual(self.members(self.a), before)
        self.assertEqual(self.conn.execute('SELECT state FROM import_batch WHERE id = ?',
                                           (plan.batch_id,)).fetchone()[0], 'cancelled')
        self.assertEqual(imp.commit(self.conn, plan).state, 'committed')

    def test_the_same_preview_committed_twice_is_a_replay(self):
        plan = self.plan(f'{PUBLIC_C}\n', self.a, key='idem-1', name='i.txt')
        first = imp.commit(self.conn, plan)
        second = imp.commit(self.conn, plan)
        third = imp.commit(self.conn, self.plan(f'{PUBLIC_C}\n', self.a, key='idem-1',
                                                name='i.txt'))
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(second.batch_id, first.batch_id)
        self.assertEqual(third.batch_id, first.batch_id)
        self.assertEqual(first.counts, second.counts)
        self.assertEqual(self.tables()['membership'], 4, 'повторный commit не размножил записи')

    def test_revision_conflict_busy_and_empty_replace_are_refused(self):
        plan = self.plan('http://5.255.255.5:8888\n', self.a, name='stale.txt')
        imp.commit(self.conn, self.plan('http://104.244.72.115:8080\n', self.a, name='other.txt'))
        with self.assertRaises(imp.ImportRevisionConflict) as raised:
            imp.commit(self.conn, plan)
        self.assertEqual(raised.exception.code, imp.CODE_REVISION)
        self.assertEqual(raised.exception.detail['current'],
                         raised.exception.detail['expected'] + 1)
        fresh = self.plan('http://5.255.255.5:8888\n', self.a, name='busy.txt')
        with self.assertRaises(imp.ImportBusy) as busy:
            imp.commit(self.conn, fresh, busy=lambda: 'идёт job 42')
        self.assertEqual(busy.exception.code, imp.CODE_BUSY)
        empty = self.plan('мусор\n', self.a, mode='replace', name='empty.txt')
        with self.assertRaises(imp.ImportProblem) as refusal:
            imp.commit(self.conn, empty, allow_partial=True)
        self.assertEqual(refusal.exception.code, imp.CODE_EMPTY)
        self.assertEqual(self.members(self.a), sorted(self.members(self.a)))

    def test_the_report_records_what_happened_and_carries_no_raw_input(self):
        plan = self.plan(f'{PUBLIC_C}\nмусор\n', self.a, name='report.txt')
        report = imp.commit(self.conn, plan, allow_partial=True)
        stored = imp.load_report(self.conn, report.batch_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.revision_after, stored.revision_before + 1)
        self.assertEqual(stored.source_name, 'report.txt')
        self.assertEqual(stored.source_digest, plan.source_digest)
        self.assertEqual([row['line'] for row in stored.rejected], [2])
        payload = json.loads(stored.to_json())
        self.assertEqual(payload['state'], 'committed')
        self.assertIn('counts', payload)
        self.assertIn('duration_s', payload)
        self.assertNotIn(CANARY, json.dumps(payload, ensure_ascii=False))

    def test_replace_would_not_empty_a_collection_without_an_explicit_allowance(self):
        collection = imp.create_collection(self.conn, 'C', 'private')
        imp.commit(self.conn, self.plan(f'{PUBLIC_A}\n', collection))
        plan = self.plan('мусор\n', collection, mode='replace', name='x.txt')
        with self.assertRaises(imp.ImportProblem) as raised:
            imp.commit(self.conn, plan, allow_partial=True)
        self.assertEqual(raised.exception.code, imp.CODE_EMPTY)
        self.assertEqual(self.members(collection), [PUBLIC_A])


# --------------------------------------------------------------------------- #
# Дефект 10 и F04: единый отказ и отсутствие секрета на диске
# --------------------------------------------------------------------------- #
class Defect10Tests(Base):
    HOSTNAME = 'proxy.example.com:3128'
    PRIVATE = '10.0.0.7:8080'
    CREDENTIALS = f'http://user:{CANARY}@104.244.72.115:8080'

    def test_every_format_refuses_the_same_rows_with_the_same_codes(self):
        """TXT, URI и JSON несут credentials внутри адреса - решение одно и то же."""
        shapes = {
            'txt': '\n'.join([self.HOSTNAME, self.PRIVATE, self.CREDENTIALS,
                              '91.198.174.192:3128']),
            'uri': '\n'.join([f'http://{self.HOSTNAME}', f'http://{self.PRIVATE}',
                              self.CREDENTIALS, 'http://91.198.174.192:3128']),
            'json': json.dumps(['proxy.example.com:3128', '10.0.0.7:8080',
                                f'http://user:{CANARY}@104.244.72.115:8080',
                                '91.198.174.192:3128']),
        }
        private = imp.EndpointPolicy(public_only=False)
        for fmt, text in shapes.items():
            with self.subTest(format=fmt, mode='public'):
                plan = imp.preview(self.conn, imp.ImportSource.from_text(text),
                                   collection_id=db.PUBLIC_COLLECTION_ID, fmt=fmt)
                self.assertEqual([row.reason for row in plan.rows],
                                 [imp.ROW_HOSTNAME, imp.ROW_PRIVATE, imp.ROW_CREDENTIALS, None])
                self.assertEqual(plan.rows[3].state, imp.VALID)
            with self.subTest(format=fmt, mode='private'):
                # явный режим доверенной коллекции: адрес и имя хоста поддержаны,
                # учётные данные всё равно отвергнуты - флаг этого не отключает
                plan = imp.preview(self.conn, imp.ImportSource.from_text(text),
                                   collection_id=db.PUBLIC_COLLECTION_ID, fmt=fmt, policy=private)
                self.assertEqual([row.reason for row in plan.rows],
                                 [None, None, imp.ROW_CREDENTIALS, None])
                self.assertEqual(plan.counts['rejected'], 1)

    def test_a_csv_with_a_credential_column_is_refused_in_both_modes(self):
        """Единственный способ выразить пароль в CSV - отдельная колонка,
        поэтому отвергается весь файл, а не часть строк."""
        text = ('host,port,username\n' + f'proxy.example.com,3128,\n'
                f'10.0.0.7,8080,\n104.244.72.115,8080,{CANARY}\n'
                '91.198.174.192,3128,\n')
        for policy, mode in ((imp.DEFAULT_POLICY, 'public'),
                             (imp.EndpointPolicy(public_only=False), 'private')):
            with self.subTest(mode=mode):
                plan = imp.preview(self.conn, imp.ImportSource.from_text(text),
                                   collection_id=db.PUBLIC_COLLECTION_ID, fmt='csv',
                                   policy=policy)
                self.assertEqual([row.reason for row in plan.rows],
                                 [imp.ROW_CREDENTIALS] * 4)
                self.assertEqual(plan.counts['valid'], 0)
                self.assertNotIn(CANARY, json.dumps(plan.to_dict(), ensure_ascii=False))

    def test_credentials_are_refused_whatever_the_mode_and_never_stored(self):
        private = imp.EndpointPolicy(public_only=False)
        text = f'proxy.example.com:8080\n10.0.0.5:3128\n192.168.1.9:1080\n{self.CREDENTIALS}\n'
        collection = imp.create_collection(self.conn, 'Trusted', 'private')
        for policy in (imp.DEFAULT_POLICY, private):
            plan = imp.preview(self.conn, imp.ImportSource.from_text(text),
                               collection_id=collection, policy=policy)
            self.assertEqual(plan.rows[3].reason, imp.ROW_CREDENTIALS,
                             'credentials refused identically in both modes')
        report = imp.commit(self.conn,
                            imp.preview(self.conn, imp.ImportSource.from_text(text),
                                        collection_id=collection, policy=private),
                            allow_partial=True)
        self.assertEqual(report.rejected[0]['reason'], imp.ROW_CREDENTIALS)
        self.assertNotIn(CANARY, json.dumps([dict(row) for row in report.rejected],
                                            ensure_ascii=False))
        self.assertEqual(self.members(collection),
                         ['http://10.0.0.5:3128', 'http://192.168.1.9:1080',
                          'http://proxy.example.com:8080'])

    def test_a_refused_row_is_never_written_into_endpoints(self):
        collection = imp.create_collection(self.conn, 'C', 'private')
        text = '\n'.join([self.HOSTNAME, self.PRIVATE, self.CREDENTIALS, PUBLIC_C])
        plan = imp.preview(self.conn, imp.ImportSource.from_text(text),
                           collection_id=collection)
        self.assertEqual([row.reason for row in plan.rows],
                         [imp.ROW_HOSTNAME, imp.ROW_PRIVATE, imp.ROW_CREDENTIALS, None])
        imp.commit(self.conn, plan, allow_partial=True)
        self.assertEqual(self.members(collection), [PUBLIC_C])
        self.assertEqual(self.tables()['endpoints'], 1)

    def test_the_canary_never_reaches_a_file_in_the_data_directory(self):
        """Пароль не должен пережить ни raw input, ни лог, ни provenance на диске."""
        collection = imp.create_collection(self.conn, 'C', 'private')
        payloads = {
            'uri': f'http://user:{CANARY}@45.33.32.156:8080\n91.198.174.192:3128\n',
            'csv': f'host,port,password\n45.33.32.156,8080,{CANARY}\n91.198.174.192,3128,x\n',
            'json': json.dumps([{'host': '45.33.32.156', 'port': 8080, 'password': CANARY},
                                {'host': '91.198.174.192', 'port': 3128}]),
            'hostpass': f'45.33.32.156:8080:{CANARY}\nuser:{CANARY}\n{CANARY}\n'
                        f'91.198.174.192:3128\n',
            'query': f'http://45.33.32.156:8080/?token={CANARY}\n91.198.174.192:3128\n',
        }
        for name, text in payloads.items():
            plan = imp.preview(self.conn, imp.ImportSource.from_text(text, name=name),
                               collection_id=collection)
            report = imp.commit(self.conn, plan, allow_partial=True)
            self.assertNotIn(CANARY, json.dumps([dict(row) for row in report.rejected],
                                                ensure_ascii=False), name)
            self.assertNotIn(CANARY, json.dumps(plan.to_dict(), ensure_ascii=False), name)
        self.conn.commit()
        self.conn.close()
        leaked = [path.name for path in self.work.iterdir()
                  if path.is_file() and CANARY.encode() in path.read_bytes()]
        self.assertEqual(leaked, [], 'canary found on disk')
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        for (blob,) in conn.execute('SELECT report_json FROM import_batch'):
            if blob:
                self.assertNotIn(CANARY, blob)

    def test_import_source_never_prints_its_text(self):
        source = imp.ImportSource.from_text(f'http://user:{CANARY}@45.33.32.156:8080',
                                            name='x.txt')
        self.assertNotIn(CANARY, repr(source))
        self.assertNotIn(CANARY, str(source))
        self.assertNotIn(CANARY, json.dumps({'name': source.name, 'digest': source.digest}))

    def test_oversized_binary_and_unreadable_inputs_are_refused_by_code(self):
        with self.assertRaises(imp.ImportTooLarge) as too_large:
            imp.ImportSource.from_bytes(b'x' * (imp.MAX_IMPORT_BYTES + 1), name='big.txt')
        self.assertEqual(too_large.exception.code, imp.CODE_SIZE)
        with self.assertRaises(imp.ImportEncodingError) as encoding:
            imp.ImportSource.from_bytes(b'\xff\xfe\x00binary', name='bin.dat')
        self.assertEqual(encoding.exception.code, imp.CODE_ENCODING)
        with self.assertRaises(imp.ImportFormatError) as missing:
            imp.ImportSource.from_path(self.work / 'нет-такого.txt')
        self.assertEqual(missing.exception.code, imp.CODE_FORMAT)
        with self.assertRaises(imp.ImportProblem) as wrong_type:
            imp.ImportSource.from_text(12345)
        self.assertEqual(wrong_type.exception.code, imp.CODE_FIELD)
        with self.assertRaises(imp.ImportFormatError):
            imp.detect_format(imp.ImportSource.from_text('   \n# только комментарий\n'))
        with self.assertRaises(imp.ImportFormatError) as unknown:
            imp.preview(self.conn, imp.ImportSource.from_text(PUBLIC_A),
                        collection_id=db.PUBLIC_COLLECTION_ID, fmt='yaml')
        self.assertEqual(unknown.exception.code, imp.CODE_FORMAT)

    def test_an_unknown_collection_and_a_bad_mode_are_refused(self):
        with self.assertRaises(imp.UnknownCollection) as unknown:
            self.plan(PUBLIC_A + '\n', 'col-нет-такой')
        self.assertEqual(unknown.exception.code, imp.CODE_NO_COLLECTION)
        with self.assertRaises(imp.ImportProblem) as mode:
            self.plan(PUBLIC_A + '\n', db.PUBLIC_COLLECTION_ID, mode='upsert')
        self.assertEqual(mode.exception.code, imp.CODE_FIELD)


if __name__ == '__main__':
    unittest.main()

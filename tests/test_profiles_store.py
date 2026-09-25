"""The named profile library: create, copy, revisions, archive, default, diff.

All of it runs on a temporary SQLite file that `db.migrate()` brought to the
contract schema (CONTRACTS §3.3, migration 9).  This module writes no DDL, and
one test proves it by tracing every statement the store issues.
"""
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db  # noqa: E402
from proxy_workbench import profiles  # noqa: E402
from tests import profiles_support as support  # noqa: E402

START = support.START


def relaxed(spec, target_id=support.BASIC, **changes):
    """The same profile with one threshold lowered, i.e. a new revision."""
    changes = changes or {'min_success': 0.5}
    targets = [dict(support.target(item.id, item.kind, enabled=item.enabled,
                                   min_success=item.min_success,
                                   max_latency_ms=item.max_latency_ms))
               for item in spec.targets]
    for target in targets:
        if target['id'] == target_id:
            target.update(changes)
    return spec.copy_of(targets=targets)


class CreateAndReadTests(support.StoreFixture):
    def test_a_new_profile_is_revision_one_with_its_own_digest(self):
        spec = support.spec(description='API проекта')
        ref = self.store.create('API', spec, is_default=True, at=START)
        self.assertEqual(ref.revision, 1)
        self.assertEqual(ref.row_id, f'{ref.profile_id}@1')
        record = self.store.get(ref.profile_id)
        self.assertEqual(record.name, 'API')
        self.assertEqual(record.digest, spec.digest)
        self.assertEqual(record.spec.as_dict(), spec.as_dict())
        self.assertIsNone(record.parent_id)
        self.assertEqual(record.created_at, START)
        self.assertIsNone(record.archived_at)
        self.assertTrue(record.is_default)
        row = self.row(ref)
        self.assertEqual(row['name'], 'API')
        self.assertEqual(row['revision'], 1)
        self.assertEqual(row['is_default'], 1)
        self.assertEqual(json.loads(row['config']), spec.as_dict())

    def test_the_library_lists_one_head_per_profile(self):
        first = self.store.create('Первый', support.spec(), at=START)
        self.store.create('Второй', support.spec(), at=START + 1)
        relaxed_ref = self.store.update(first.profile_id, relaxed(support.spec()), at=START + 2)
        listing = [(item.name, item.revision) for item in self.store.list()]
        self.assertEqual(listing, [('Второй', 1), ('Первый', 2)])
        self.assertEqual(self.store.head(first.profile_id).revision, 2)
        self.assertEqual(relaxed_ref.revision, 2)
        self.assertEqual(self.store.find('Первый').revision, 2)
        self.assertIsNone(self.store.find('нет такого'))

    def test_a_name_can_be_used_once_and_a_refusal_carries_its_code(self):
        self.store.create('API', support.spec(), at=START)
        with self.assertRaises(profiles.ProfileError) as raised:
            self.store.create('API', support.spec(), at=START)
        self.assertEqual(raised.exception.code, 'E_CONFLICT_NAME')
        with self.assertRaises(profiles.ProfileError):
            self.store.create('   ', support.spec(), at=START)
        with self.assertRaises(profiles.ProfileError) as raised:
            self.store.create('Broken', support.spec(targets=[]), at=START)
        self.assertEqual(raised.exception.code, 'E_VALIDATION_FIELD')
        # Neither refusal left a row behind.
        self.assertEqual([item.name for item in self.store.list()], ['API'])
        self.assertEqual(len(self.rows()), 1)

    def test_an_unknown_profile_or_revision_is_named_as_such(self):
        for call in (lambda: self.store.head('p_0000000000000000'),
                     lambda: self.store.get('p_0000000000000000'),
                     lambda: self.store.history('p_0000000000000000'),
                     lambda: self.store.get('p_0000000000000000', 7)):
            with self.subTest(call=call):
                with self.assertRaises(profiles.ProfileError) as raised:
                    call()
                self.assertEqual(raised.exception.code, 'E_STATE_PROFILE_UNKNOWN')
        for bad_id in ('p_1*', '', None, 'x' * 200):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(profiles.ProfileError):
                    self.store.head(bad_id)


class RevisionTests(support.StoreFixture):
    def test_update_appends_a_revision_and_keeps_the_previous_one_readable(self):
        first_spec = support.spec()
        ref = self.store.create('API', first_spec, at=START)
        second_spec = relaxed(first_spec)
        new_ref = self.store.update(ref.profile_id, second_spec, base_revision=1,
                                    at=START + 60)
        self.assertEqual(new_ref.revision, 2)

        head = self.store.get(ref.profile_id)
        self.assertEqual(head.spec.digest, second_spec.digest)
        previous = self.store.get(ref.profile_id, 1)
        self.assertEqual(previous.spec.digest, first_spec.digest)
        self.assertEqual(previous.spec.required_targets[0].min_success, 1.0)
        self.assertEqual(previous.parent_id, None)
        self.assertEqual(previous.created_at, START)
        # The history is a chain, not a rewrite.
        self.assertEqual(head.parent_id, previous.row_id)
        self.assertEqual([item.revision for item in self.store.history(ref.profile_id)], [1, 2])
        self.assertEqual([item.digest for item in self.store.history(ref.profile_id)],
                         [first_spec.digest, second_spec.digest])

    def test_the_previous_revision_still_decides_on_its_own_evidence(self):
        """A later threshold does not travel back into the results of an earlier run."""
        first_spec = support.spec(attempts=2)
        ref = self.store.create('API', first_spec, at=START)
        measured = [support.mixed(first_spec, support.BASIC, 1, 2),
                    support.ok(first_spec, support.SEARCH)]
        self.assertFalse(profiles.run_request(
            {'profile_id': ref.profile_id, 'profile_revision': 1, 'evidence': measured},
            store=self.store)['pass'])
        self.store.update(ref.profile_id, relaxed(first_spec), base_revision=1, at=START + 1)
        replayed = profiles.run_request(
            {'profile_id': ref.profile_id, 'profile_revision': 2, 'evidence': measured},
            store=self.store)
        self.assertFalse(replayed['pass'])
        self.assertEqual(replayed['reason'], 'E_VERDICT_REQUIRED_NOT_PASSED')
        self.assertEqual(replayed['targets'][support.BASIC]['state'], 'unmeasured')
        self.assertEqual(replayed['targets'][support.BASIC]['reason'], 'E_TARGET_STALE_EVIDENCE')
        # Re-measured under revision 2, the same numbers now pass.
        remeasured = [support.mixed(relaxed(first_spec), support.BASIC, 1, 2),
                      support.ok(relaxed(first_spec), support.SEARCH)]
        self.assertTrue(profiles.run_request(
            {'profile_id': ref.profile_id, 'profile_revision': 2, 'evidence': remeasured},
            store=self.store)['pass'])

    def test_a_write_that_changes_nothing_adds_no_revision(self):
        spec = support.spec()
        ref = self.store.create('API', spec, at=START)
        same = self.store.update(ref.profile_id, spec.copy_of(), base_revision=1, at=START + 1)
        self.assertEqual(same.revision, 1)
        self.assertEqual(len(self.store.history(ref.profile_id)), 1)

    def test_a_stale_base_revision_is_a_conflict_not_an_overwrite(self):
        spec = support.spec()
        ref = self.store.create('API', spec, at=START)
        self.store.update(ref.profile_id, relaxed(spec), base_revision=1, at=START + 1)
        with self.assertRaises(profiles.ProfileError) as raised:
            self.store.update(ref.profile_id, spec, base_revision=1, at=START + 2)
        self.assertEqual(raised.exception.code, 'E_CONFLICT_REVISION')
        self.assertEqual(self.store.head(ref.profile_id).revision, 2)
        with self.assertRaises(profiles.ProfileError):
            self.store.update(ref.profile_id, spec, base_revision=0, at=START + 2)

    def test_an_update_that_would_break_another_profile_is_refused(self):
        first = self.store.create('Первый', support.spec(), at=START)
        self.store.create('Второй', support.spec(), at=START)
        with self.assertRaises(profiles.ProfileError) as raised:
            self.store.update(first.profile_id, relaxed(support.spec()), name='Второй',
                              at=START + 1)
        self.assertEqual(raised.exception.code, 'E_CONFLICT_NAME')
        self.assertEqual(self.store.head(first.profile_id).revision, 1)
        # Renaming to its own name is how a profile keeps its identity.
        renamed = self.store.update(first.profile_id, relaxed(support.spec()),
                                    name='Первый', at=START + 2)
        self.assertEqual(renamed.revision, 2)


class CopyTests(support.StoreFixture):
    def test_copy_from_an_older_revision_keeps_that_content(self):
        spec = support.spec()
        ref = self.store.create('API', spec, at=START)
        self.store.update(ref.profile_id, relaxed(spec), base_revision=1, at=START + 1)
        copied = self.store.copy(ref.profile_id, new_name='API (черновик)', revision=1, at=START + 2)
        self.assertEqual(copied.revision, 1)
        record = self.store.get(copied.profile_id)
        self.assertEqual(record.name, 'API (черновик)')
        self.assertEqual(record.spec.digest, spec.digest)
        self.assertEqual(record.parent_id, f'{ref.profile_id}@1')
        self.assertEqual(record.digest, spec.digest)
        # The source is untouched, head included.
        self.assertEqual(self.store.head(ref.profile_id).revision, 2)
        self.assertEqual(self.store.head(ref.profile_id).spec.digest, relaxed(spec).digest)

    def test_copy_without_a_name_finds_a_free_one(self):
        ref = self.store.create('API', support.spec(), at=START)
        first = self.store.copy(ref.profile_id, at=START + 1)
        second = self.store.copy(ref.profile_id, at=START + 2)
        self.assertEqual(self.store.get(first.profile_id).name, 'API (2)')
        self.assertEqual(self.store.get(second.profile_id).name, 'API (3)')
        self.assertEqual(self.store.free_name('API'), 'API (4)')
        self.assertEqual(self.store.free_name('Свободно'), 'Свободно')

    def test_copy_onto_an_existing_name_is_refused(self):
        ref = self.store.create('API', support.spec(), at=START)
        with self.assertRaises(profiles.ProfileError) as raised:
            self.store.copy(ref.profile_id, new_name='API', at=START + 1)
        self.assertEqual(raised.exception.code, 'E_CONFLICT_NAME')


class ArchiveTests(support.StoreFixture):
    def test_archiving_hides_a_profile_but_keeps_its_history_readable(self):
        spec = support.spec()
        ref = self.store.create('API', spec, at=START)
        self.store.update(ref.profile_id, relaxed(spec), base_revision=1, at=START + 1)
        self.store.archive(ref.profile_id, at=START + 2)
        self.assertEqual(self.store.list(), [])
        self.assertIsNone(self.store.find('API'))
        self.assertIsNone(self.store.default())
        archived = self.store.find('API', include_archived=True)
        self.assertTrue(archived.archived)
        self.assertEqual(archived.archived_at, START + 2)
        self.assertEqual([item.revision for item in self.store.history(ref.profile_id)], [1, 2])
        self.assertTrue(all(row['archived_at'] == START + 2 for row in self.rows()))

    def test_an_archived_profile_is_frozen_until_it_is_brought_back(self):
        ref = self.store.create('API', support.spec(), at=START)
        self.store.archive(ref.profile_id, at=START + 1)
        for call in (lambda: self.store.update(ref.profile_id, relaxed(support.spec()),
                                               at=START + 2),
                     lambda: self.store.set_default(ref.profile_id)):
            with self.assertRaises(profiles.ProfileError) as raised:
                call()
            self.assertEqual(raised.exception.code, 'E_CONFLICT_ARCHIVED')
        self.store.unarchive(ref.profile_id)
        self.assertEqual([item.name for item in self.store.list()], ['API'])
        self.assertIsNone(self.store.head(ref.profile_id).archived_at)
        self.assertEqual(self.store.update(ref.profile_id, relaxed(support.spec()),
                                           at=START + 3).revision, 2)

    def test_unarchiving_onto_a_taken_name_is_refused(self):
        first = self.store.create('API', support.spec(), at=START)
        self.store.archive(first.profile_id, at=START + 1)
        self.store.create('API', support.spec(), at=START + 2)
        with self.assertRaises(profiles.ProfileError) as raised:
            self.store.unarchive(first.profile_id)
        self.assertEqual(raised.exception.code, 'E_CONFLICT_NAME')
        self.assertTrue(self.store.head(first.profile_id).archived)


class DefaultTests(support.StoreFixture):
    def test_exactly_one_profile_is_the_default(self):
        first = self.store.create('Первый', support.spec(), is_default=True, at=START)
        second = self.store.create('Второй', support.spec(), at=START + 1)
        self.assertEqual(self.store.default().profile_id, first.profile_id)
        self.store.set_default(second.profile_id)
        self.assertEqual(self.store.default().profile_id, second.profile_id)
        self.assertEqual(self.store.get(first.profile_id).is_default, False)
        self.assertTrue(self.store.get(second.profile_id).is_default)
        # The flag belongs to the profile, so a new revision keeps it.
        self.store.update(second.profile_id, relaxed(support.spec()), at=START + 2)
        self.assertEqual(self.store.default().revision, 2)


class DiffTests(support.StoreFixture):
    def test_the_diff_names_every_kind_of_change(self):
        first_spec = support.spec(optional_rule='any', attempts=2,
                                  targets=[support.target(support.BASIC, min_success=1.0),
                                           support.target(support.SEARCH, min_success=1.0),
                                           support.target(support.VIDEO, 'optional')])
        ref = self.store.create('API', first_spec, at=START)
        second_spec = support.spec(optional_rule='at_least', k=2, attempts=3,
                                   budget={'max_probes': 40},
                                   targets=[support.target(support.BASIC, min_success=0.5,
                                                           max_latency_ms=900.0),
                                            support.target(support.VIDEO, 'optional'),
                                            support.target(support.CHAT, 'optional')])
        self.store.update(ref.profile_id, second_spec, base_revision=1, at=START + 1)
        diff = self.store.diff(ref.profile_id, 1, 2)
        self.assertEqual(diff['added'], [support.CHAT])
        self.assertEqual(diff['removed'], [support.SEARCH])
        self.assertEqual([item['target_id'] for item in diff['changed']], [support.BASIC])
        self.assertEqual(diff['changed'][0]['before']['min_success'], 1.0)
        self.assertEqual(diff['changed'][0]['after']['max_latency_ms'], 900.0)
        self.assertEqual(diff['optional_rule'], {'before': {'mode': 'any', 'k': None},
                                                 'after': {'mode': 'at_least', 'k': 2}})
        self.assertEqual(diff['attempts'], {'before': 2, 'after': 3})
        self.assertEqual(diff['budget'], {'before': {'max_probes': None, 'max_duration_s': None},
                                          'after': {'max_probes': 40, 'max_duration_s': None}})
        self.assertTrue(diff['digest_changed'])
        with self.assertRaises(profiles.ProfileError):
            self.store.diff(ref.profile_id, 1, 3)

    def test_a_diff_of_a_revision_against_itself_reports_no_change(self):
        ref = self.store.create('API', support.spec(), at=START)
        diff = self.store.diff(ref.profile_id, 1, 1)
        self.assertEqual((diff['added'], diff['removed'], diff['changed']), ([], [], []))
        self.assertFalse(diff['digest_changed'])


class NoDdlTests(support.StoreFixture):
    def test_the_store_issues_no_ddl_of_its_own(self):
        """CONTRACTS §3.3: `db.py` owns every DDL statement in the project."""
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            spec = support.spec()
            ref = self.store.create('API', spec, at=START)
            self.store.update(ref.profile_id, relaxed(spec), base_revision=1, at=START + 1)
            self.store.copy(ref.profile_id, at=START + 2)
            self.store.set_default(ref.profile_id)
            self.store.archive(ref.profile_id, at=START + 3)
            self.store.unarchive(ref.profile_id)
            self.store.list()
            self.store.history(ref.profile_id)
        finally:
            self.conn.set_trace_callback(None)
        ddl = [line for line in statements
               if line.lstrip().upper().startswith(('CREATE', 'ALTER', 'DROP', 'PRAGMA'))]
        self.assertEqual(ddl, [])
        self.assertTrue(statements)

    def test_a_database_without_the_contract_columns_is_refused_not_repaired(self):
        path = Path(self.temp.name) / 'legacy-shapes.sqlite3'
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE profiles(id TEXT PRIMARY KEY, config TEXT NOT NULL)')
        conn.commit()
        with self.assertRaises(profiles.ProfileError) as raised:
            profiles.ProfileStore(conn)
        self.assertEqual(raised.exception.code, 'E_DATA_DB_FOREIGN')
        self.assertIn('db.migrate', str(raised.exception))
        conn.close()
        # And an empty file is refused the same way, not silently created.
        other = sqlite3.connect(Path(self.temp.name) / 'nothing.sqlite3')
        with self.assertRaises(profiles.ProfileError):
            profiles.ProfileStore(other)
        other.close()

    def test_the_store_rejects_a_connection_it_did_not_get(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.ProfileStore('data/profiles.sqlite3')


class LegacyDatabaseTests(unittest.TestCase):
    """A database written before versioning: migrated in place, rows untouched."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'legacy.sqlite3'
        support.legacy_database(self.path)
        self.store = profiles.open_store(self.path)

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def test_migration_keeps_the_legacy_row_and_adds_named_profiles(self):
        legacy_rows = self.store.conn.execute(
            'SELECT id, name, revision, digest FROM profiles').fetchall()
        self.assertEqual([(row['id'], row['name'], row['revision']) for row in legacy_rows],
                         [('profile0001', None, 1)])
        # db.migrate backfills the digest from the content hash, and the name stays
        # NULL: naming a legacy profile would be inventing an origin (F02).
        self.assertEqual(legacy_rows[0]['digest'], 'profile0001')
        self.assertEqual(self.store.list(), [])
        spec = support.spec()
        ref = self.store.create('Перенесённый', spec, at=START)
        self.assertEqual(len(self.store.conn.execute('SELECT id FROM profiles').fetchall()), 2)
        self.assertEqual(self.store.head(ref.profile_id).digest, spec.digest)
        with self.assertRaises(profiles.ProfileError):
            self.store.get('profile0001')

    def test_a_legacy_config_becomes_a_named_revision_through_the_store(self):
        row = self.store.conn.execute('SELECT config FROM profiles WHERE id = ?',
                                      ('profile0001',)).fetchone()
        spec = profiles.from_legacy_config(json.loads(row['config']))
        ref = self.store.create('Из старого', spec, at=START)
        record = self.store.get(ref.profile_id)
        self.assertEqual(record.revision, 1)
        self.assertEqual([item.id for item in record.spec.required_targets],
                         ['target-0', 'target-1'])
        self.assertEqual(record.spec.attempts, 2)
        # The old acceptance behaviour survives the move: the same global threshold,
        # applied to every target as before.
        measured = [spec.evidence('target-0', ok=1, attempts=2),
                    spec.evidence('target-1', ok=2, attempts=2)]
        self.assertFalse(profiles.evaluate(record.spec, measured)['pass'])
        measured[0] = spec.evidence('target-0', ok=2, attempts=2)
        self.assertTrue(profiles.evaluate(record.spec, measured)['pass'])

    def test_migrating_twice_changes_nothing(self):
        self.store.conn.close()
        before = self.path.read_bytes()
        report = db.migrate(self.path)
        self.assertEqual(report.status, 'current')
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

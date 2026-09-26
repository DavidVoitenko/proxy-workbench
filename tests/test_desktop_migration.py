"""Moving the folder an older build wrote into.

F23 asks for a migration of the old folder; CONTRACTS §3.4 requires that a data
path change happen only after the user agrees and with a backup of the old
folder.  So the default here is a preview that writes nothing.
"""
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import desktop


def workspace():
    root = Path(tempfile.mkdtemp(prefix='pw-migrate-'))
    return root


def make_database(path, rows=3):
    """A pre-versioning database, the way the 2.x engine left it behind."""
    with closing(sqlite3.connect(path)) as db:
        with db:
            db.execute('CREATE TABLE candidates(proxy TEXT PRIMARY KEY)')
            db.executemany('INSERT INTO candidates(proxy) VALUES (?)',
                           [(f'http://127.0.0.{index}:8080',) for index in range(rows)])
    return path


def layout_for(root, mode='per-user'):
    return desktop.Layout(root / 'data', root / 'cache', root / 'logs', mode, root / 'data', 'test')


class LegacyFolderDiscoveryTests(unittest.TestCase):
    def test_the_folder_next_to_the_program_is_found(self):
        root = workspace().resolve()
        program = root / 'Program'
        (program / 'data').mkdir(parents=True)
        (program / 'data' / 'proxies.sqlite3').write_bytes(b'')
        found = desktop.legacy_data_roots(layout_for(root), executable=str(program / 'app.exe'),
                                          package=root / 'nowhere' / 'proxy_workbench')
        self.assertIn(program / 'data', found)

    def test_the_current_data_folder_is_never_offered_as_a_source(self):
        root = workspace()
        layout = layout_for(root)
        layout.data.mkdir(parents=True)
        found = desktop.legacy_data_roots(layout, executable=str(root / 'app'),
                                          package=root / 'nowhere' / 'proxy_workbench')
        self.assertNotIn(layout.data, found)

    def test_a_named_previous_folder_can_be_offered(self):
        root = workspace()
        previous = root / 'Старая папка'
        previous.mkdir()
        found = desktop.legacy_data_roots(layout_for(root), environ={'PROXY_WORKBENCH_PREVIOUS_DATA': str(previous)},
                                          executable=str(root / 'app'),
                                          package=root / 'nowhere' / 'proxy_workbench')
        self.assertEqual(found[0], previous)


class PlanTests(unittest.TestCase):
    def test_plan_lists_files_and_writes_nothing(self):
        root = workspace()
        source = root / 'legacy'
        source.mkdir(parents=True)
        (source / 'proxies.sqlite3').write_bytes(b'sqlite')
        (source / 'gui-settings.json').write_text('{"a": 1}', encoding='utf-8')
        (source / 'exports').mkdir()
        (source / 'exports' / 'proxies.txt').write_bytes(b'http://127.0.0.1:80\n')

        plan = desktop.plan_migration(layout_for(root), source=source, now=1_700_000_000)
        self.assertEqual(sorted(name.as_posix() for name, _ in plan.items),
                         ['exports/proxies.txt', 'gui-settings.json', 'proxies.sqlite3'])
        self.assertEqual(plan.total_bytes, 34)
        self.assertFalse((root / 'data-copy').exists())
        self.assertFalse(layout_for(root).data.exists())

    def test_a_missing_folder_is_reported_not_guessed(self):
        root = workspace()
        plan = desktop.plan_migration(layout_for(root), source=root / 'nowhere')
        self.assertTrue(plan.empty)
        self.assertFalse(plan.source.is_dir())

    def test_a_differing_file_in_the_target_is_a_conflict_not_an_overwrite(self):
        root = workspace()
        source = root / 'legacy'
        source.mkdir(parents=True)
        (source / 'gui-settings.json').write_text('{"old": true}', encoding='utf-8')
        layout = layout_for(root)
        layout.data.mkdir(parents=True)
        (layout.data / 'gui-settings.json').write_text('{"new": true}', encoding='utf-8')

        plan = desktop.plan_migration(layout, source=source)
        self.assertEqual([str(name) for name in plan.missing], ['gui-settings.json'])
        self.assertEqual(plan.items, ())

    def test_an_identical_file_is_copied_again_rather_than_called_a_conflict(self):
        root = workspace()
        source = root / 'legacy'
        source.mkdir(parents=True)
        (source / 'gui-settings.json').write_text('{"same": 1}', encoding='utf-8')
        layout = layout_for(root)
        layout.data.mkdir(parents=True)
        (layout.data / 'gui-settings.json').write_text('{"same": 1}', encoding='utf-8')

        plan = desktop.plan_migration(layout, source=source)
        self.assertEqual(plan.missing, ())
        self.assertEqual(len(plan.items), 1)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.root = workspace()
        self.source = self.root / 'legacy'
        self.source.mkdir(parents=True)
        make_database(self.source / 'proxies.sqlite3')
        (self.source / 'gui-settings.json').write_text('{"a": 1}', encoding='utf-8')
        self.layout = layout_for(self.root)
        self.plan = desktop.plan_migration(self.layout, source=self.source, now=1_700_000_000)

    def test_without_execute_nothing_is_written(self):
        result = desktop.apply_migration(self.plan)
        self.assertFalse(result.applied)
        self.assertFalse(self.layout.data.exists())
        self.assertFalse(self.plan.backup.exists())

    def test_apply_copies_the_files_and_leaves_a_receipt(self):
        result = desktop.apply_migration(self.plan, execute=True)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual((self.layout.data / 'gui-settings.json').read_text(encoding='utf-8'), '{"a": 1}')
        receipt = json.loads((self.layout.data / 'migration-receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['source'], str(self.source))
        self.assertIn('gui-settings.json', receipt['copied'])

    def test_the_old_folder_is_never_deleted(self):
        desktop.apply_migration(self.plan, execute=True)
        self.assertTrue((self.source / 'proxies.sqlite3').is_file())

    def test_a_backup_of_the_old_folder_exists_and_is_readable(self):
        result = desktop.apply_migration(self.plan, execute=True)
        backup = Path(result.backup)
        self.assertTrue((backup / 'gui-settings.json').is_file())
        with closing(sqlite3.connect(backup / 'proxies.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM candidates').fetchone()[0], 3)

    def test_a_live_database_is_copied_through_sqlite_not_byte_by_byte(self):
        # A file copy of a WAL database can tear; the backup goes through the
        # SQLite backup API so the restored copy is always openable.
        (self.source / 'proxies.sqlite3-wal').write_bytes(b'not a real wal')
        result = desktop.apply_migration(self.plan, execute=True)
        with closing(sqlite3.connect(Path(result.backup) / 'proxies.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM candidates').fetchone()[0], 3)

    def test_live_wal_transactions_reach_both_backup_and_migrated_database(self):
        # '#' exercises SQLite URI escaping too. The writer remains open so
        # the fourth row exists only in the live WAL when migration begins.
        folder = self.root / 'legacy # snapshot'
        folder.mkdir()
        database = make_database(folder / 'proxies.sqlite3')
        with closing(sqlite3.connect(database)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("INSERT INTO candidates VALUES ('http://127.0.0.9:9090')")
            writer.commit()
            self.assertTrue(Path(str(database) + '-wal').is_file())
            plan = desktop.plan_migration(self.layout, source=folder)
            result = desktop.apply_migration(plan, execute=True)
            self.assertTrue(result.ok, result.errors)
            for root in (self.layout.data, Path(result.backup)):
                self.assertFalse((root / 'proxies.sqlite3-wal').exists())
                self.assertFalse((root / 'proxies.sqlite3-shm').exists())
                with closing(sqlite3.connect(root / 'proxies.sqlite3')) as restored:
                    self.assertEqual(restored.execute('SELECT count(*) FROM candidates').fetchone()[0], 4)

    def test_a_conflicted_file_is_reported_and_left_alone(self):
        self.layout.data.mkdir(parents=True)
        (self.layout.data / 'gui-settings.json').write_text('{"mine": true}', encoding='utf-8')
        plan = desktop.plan_migration(self.layout, source=self.source)
        result = desktop.apply_migration(plan, execute=True)
        self.assertEqual((self.layout.data / 'gui-settings.json').read_text(encoding='utf-8'), '{"mine": true}')
        self.assertEqual([str(name) for name in result.skipped], ['gui-settings.json'])

    def test_unicode_folders_survive_the_move(self):
        folder = self.source / 'Экспорт' / 'proxies'
        folder.mkdir(parents=True)
        (folder / 'прокси-1.txt').write_text('http://127.0.0.1:80\n', encoding='utf-8')
        plan = desktop.plan_migration(self.layout, source=self.source)
        result = desktop.apply_migration(plan, execute=True)
        target = self.layout.data / 'Экспорт' / 'proxies' / 'прокси-1.txt'
        self.assertTrue(target.is_file(), result.errors)


if __name__ == '__main__':
    unittest.main()

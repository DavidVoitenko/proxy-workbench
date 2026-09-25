"""Update notice, artifact verification, backup and rollback.

F23 requires a managed update that checks origin, backs the data up, respects
the schema contract and can roll back.  The tests below cover the four refusal
paths that matter: a wrong digest, an absent signing tool, a data folder the new
version cannot read, and a failure while installing.
"""
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import desktop


def workspace():
    return Path(tempfile.mkdtemp(prefix='pw-update-'))


def artifact(root, payload=b'new build', name='proxy-workbench'):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(payload)
    return path


def manifest_for(path, **extra):
    payload = dict(version='2.3.0', url='https://example.invalid/proxy-workbench-2.3.0',
                   sha256=desktop.sha256_file(path), size=path.stat().st_size)
    payload.update(extra)
    return payload


def with_database(data, user_version):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(data / 'proxies.sqlite3')) as db:
        db.execute(f'PRAGMA user_version = {int(user_version)}')
        db.commit()
    return data


class ManifestTests(unittest.TestCase):
    def test_a_manifest_without_a_checksum_is_refused(self):
        with self.assertRaises(desktop.UpdateError) as caught:
            desktop.UpdateManifest.from_dict(dict(version='2.3.0', url='https://example.invalid/x'))
        self.assertIn('sha256', str(caught.exception))

    def test_a_malformed_file_names_itself(self):
        path = workspace() / 'update.json'
        path.write_text('{not json', encoding='utf-8')
        with self.assertRaises(desktop.UpdateError) as caught:
            desktop.read_manifest(path)
        self.assertIn('update.json', str(caught.exception))

    def test_a_manifest_round_trips(self):
        path = workspace() / 'update.json'
        path.write_text(json.dumps(manifest_for(artifact(workspace()))), encoding='utf-8')
        self.assertEqual(desktop.read_manifest(path).version, '2.3.0')

    def test_unknown_fields_are_kept_rather_than_dropped(self):
        payload = manifest_for(artifact(workspace()), notes='read the migration guide')
        manifest = desktop.UpdateManifest.from_dict(payload)
        self.assertEqual(manifest.extra['notes'], 'read the migration guide')


class VersionTests(unittest.TestCase):
    def test_numeric_parts_compare_as_numbers(self):
        self.assertEqual(desktop.compare_versions('2.2.1', '2.10.0'), -1)
        self.assertEqual(desktop.compare_versions('2.10.0', '2.2.1'), 1)
        self.assertEqual(desktop.compare_versions('2.2.1', '2.2.1'), 0)

    def test_a_pre_release_sorts_before_its_release(self):
        self.assertEqual(desktop.compare_versions('3.0.0rc1', '3.0.0'), -1)


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.path = artifact(workspace())

    def test_a_newer_version_produces_a_notice(self):
        notice = desktop.find_update('2.2.1', manifest_for(self.path))
        self.assertIsNotNone(notice)
        self.assertTrue(notice.available)
        self.assertEqual(notice.manifest.version, '2.3.0')

    def test_the_same_or_older_version_produces_nothing(self):
        self.assertIsNone(desktop.find_update('2.3.0', manifest_for(self.path)))
        self.assertIsNone(desktop.find_update('3.0.0', manifest_for(self.path)))

    def test_a_manifest_from_another_channel_is_not_offered(self):
        payload = manifest_for(self.path, channel='beta')
        self.assertIsNone(desktop.find_update('2.2.1', payload, channel='stable'))

    def test_a_manifest_that_claims_a_signature_is_reported_as_a_claim(self):
        payload = manifest_for(self.path, signature='MEUCIQ...', key_id='abc123')
        notice = desktop.find_update('2.2.1', payload)
        self.assertTrue(notice.signed_claim)
        self.assertEqual(notice.manifest.key_id, 'abc123')


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.root = workspace()
        self.path = artifact(self.root)

    def test_a_matching_file_verifies(self):
        result = desktop.verify_artifact(self.path, manifest_for(self.path), platform='linux')
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.digest_matches)
        self.assertTrue(result.size_matches)

    def test_a_changed_byte_fails_verification(self):
        manifest = manifest_for(self.path)
        self.path.write_bytes(b'new build!')
        result = desktop.verify_artifact(self.path, manifest, platform='linux')
        self.assertFalse(result.ok)
        self.assertFalse(result.digest_matches)

    def test_a_wrong_size_fails_even_when_the_digest_was_forgotten(self):
        manifest = manifest_for(self.path)
        manifest['size'] = manifest['size'] + 1
        result = desktop.verify_artifact(self.path, manifest, platform='linux')
        self.assertFalse(result.ok)
        self.assertFalse(result.size_matches)

    def test_a_missing_file_is_reported_not_crashed(self):
        result = desktop.verify_artifact(self.root / 'absent', manifest_for(self.path), platform='linux')
        self.assertFalse(result.ok)

    def test_an_unsigned_artifact_is_never_reported_as_signed(self):
        result = desktop.verify_artifact(self.path, manifest_for(self.path), platform='linux')
        self.assertFalse(result.signed)
        self.assertEqual(result.signature.kind, 'unknown')

    def test_a_real_verified_signature_is_reported_as_signed(self):
        done = mock.Mock(returncode=0, stdout='', stderr='Signature=Developer ID\n'
                                                     'Authority=Developer ID Application: Example (TEAM)')
        result = desktop.verify_artifact(self.path, manifest_for(self.path), platform='darwin',
                                         runner=lambda command: done, finder=lambda name: f'/usr/bin/{name}')
        self.assertTrue(result.signed)
        self.assertEqual(result.signature.authority, 'Developer ID Application: Example (TEAM)')


class SchemaCompatibilityTests(unittest.TestCase):
    def test_a_data_folder_with_no_database_is_compatible(self):
        result = desktop.schema_compatible(workspace(), manifest_for(artifact(workspace())))
        self.assertTrue(result.compatible)

    def test_a_database_newer_than_this_build_is_refused(self):
        data = with_database(workspace() / 'data', desktop.supported_schema_version() + 5)
        result = desktop.schema_compatible(data, manifest_for(artifact(workspace())))
        self.assertFalse(result.compatible)
        self.assertIn(str(desktop.supported_schema_version() + 5), result.reason)

    def test_a_manifest_that_needs_a_newer_schema_is_refused(self):
        data = with_database(workspace() / 'data', 3)
        result = desktop.schema_compatible(data, manifest_for(artifact(workspace()), min_data_schema=9))
        self.assertFalse(result.compatible)

    def test_a_manifest_that_does_not_claim_the_current_schema_is_refused(self):
        data = with_database(workspace() / 'data', 12)
        result = desktop.schema_compatible(data, manifest_for(artifact(workspace()), max_data_schema=10))
        self.assertFalse(result.compatible)

    def test_a_matching_range_is_accepted(self):
        data = with_database(workspace() / 'data', 10)
        result = desktop.schema_compatible(data, manifest_for(artifact(workspace()),
                                                            min_data_schema=8, max_data_schema=14))
        self.assertTrue(result.compatible, result.reason)


class ApplyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.root = workspace()
        self.data = with_database(self.root / 'data', 3)
        (self.data / 'gui-settings.json').write_text('{"a": 1}', encoding='utf-8')
        self.installed = self.root / 'Applications' / 'Proxy Workbench.app' / 'Contents' / 'MacOS' / 'Proxy Workbench'
        self.installed.parent.mkdir(parents=True)
        self.installed.write_bytes(b'old build')
        self.download = artifact(self.root / 'downloads')
        self.manifest = manifest_for(self.download)
        self.plan = desktop.plan_update(
            desktop.Layout(self.data, self.root / 'cache', self.root / 'logs', 'per-user', self.data, 'test'),
            self.manifest, self.download, executable=self.installed, platform='linux', now=1_700_000_000)

    def test_a_plan_is_not_ready_when_the_digest_does_not_match(self):
        self.download.write_bytes(b'tampered')
        plan = desktop.plan_update(self.plan.layout, self.manifest, self.download,
                                   executable=self.installed, platform='linux')
        self.assertFalse(plan.ready)

    def test_a_plan_is_not_ready_when_the_schema_cannot_be_read(self):
        data = with_database(self.root / 'other', desktop.supported_schema_version() + 1)
        layout = desktop.Layout(data, self.root / 'cache', self.root / 'logs', 'per-user', data, 'test')
        plan = desktop.plan_update(layout, self.manifest, self.download,
                                   executable=self.installed, platform='linux')
        self.assertFalse(plan.ready)

    def test_without_execute_nothing_is_touched(self):
        result = desktop.apply_update(self.plan)
        self.assertFalse(result.applied)
        self.assertEqual(self.installed.read_bytes(), b'old build')
        self.assertFalse(self.plan.backup.exists())

    def test_apply_backs_up_data_and_installs_the_new_build(self):
        result = desktop.apply_update(self.plan, execute=True)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(self.installed.read_bytes(), b'new build')
        self.assertTrue((Path(result.backup) / 'gui-settings.json').is_file())
        self.assertTrue((Path(result.backup) / 'proxies.sqlite3').is_file())

    def test_the_backup_database_is_readable(self):
        result = desktop.apply_update(self.plan, execute=True)
        with closing(sqlite3.connect(Path(result.backup) / 'proxies.sqlite3')) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_the_receipt_records_the_digest_and_that_nothing_was_signed(self):
        result = desktop.apply_update(self.plan, execute=True)
        receipt = json.loads(Path(result.receipt).read_text(encoding='utf-8'))
        self.assertEqual(receipt['sha256'], self.manifest['sha256'])
        self.assertFalse(receipt['signed'])

    def test_a_failed_install_leaves_the_previous_program_in_place(self):
        with mock.patch('shutil.copy2', side_effect=OSError('disk full')):
            result = desktop.apply_update(self.plan, execute=True)
        self.assertFalse(result.applied)
        self.assertIn('disk full', ' '.join(result.errors))
        self.assertEqual(self.installed.read_bytes(), b'old build')

    def test_rollback_restores_the_previous_program_and_data(self):
        desktop.apply_update(self.plan, execute=True)
        self.installed.write_bytes(b'newer build wrote a new schema')
        result = desktop.rollback(self.plan, execute=True)
        self.assertTrue(result.rolled_back)
        # The program that comes back is the one installed before the update,
        # not the one the update wrote.
        self.assertEqual(self.installed.read_bytes(), b'old build')
        self.assertTrue((self.data / 'gui-settings.json').is_file())
        with closing(sqlite3.connect(self.data / 'proxies.sqlite3')) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_rollback_keeps_the_newer_database_instead_of_deleting_it(self):
        desktop.apply_update(self.plan, execute=True)
        with closing(sqlite3.connect(self.data / 'proxies.sqlite3')) as db:
            db.execute('PRAGMA user_version = 99')
        desktop.rollback(self.plan, execute=True)
        kept = list(self.data.parent.glob('data-from-newer-*'))
        self.assertEqual(len(kept), 1)
        with closing(sqlite3.connect(kept[0] / 'proxies.sqlite3')) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 99)

    def test_rollback_without_execute_changes_nothing(self):
        desktop.apply_update(self.plan, execute=True)
        result = desktop.rollback(self.plan)
        self.assertFalse(result.applied)
        self.assertEqual(self.installed.read_bytes(), b'new build')


class HostCommandTests(unittest.TestCase):
    """The host must be able to explain itself without starting the interface."""

    def test_print_paths_reports_the_resolved_folders(self):
        root = workspace()
        environ = {desktop.DATA_ENV: str(root / 'Данные')}
        with mock.patch.dict('os.environ', environ, clear=False), \
                mock.patch.object(sys, 'argv', ['proxy-workbench', '--print-paths']), \
                mock.patch('builtins.print') as printed:
            code = desktop.main(['--print-paths'])
        self.assertEqual(code, 0)
        payload = json.loads(printed.call_args[0][0])
        self.assertEqual(payload['layout']['data'], str(root / 'Данные'))

    def test_update_notice_without_a_manifest_explains_itself(self):
        with mock.patch.dict('os.environ', {}, clear=False), \
                mock.patch('builtins.print') as printed:
            code = desktop.main(['--update-notice'])
        self.assertEqual(code, 2)
        self.assertIn(desktop.UPDATE_MANIFEST_ENV, printed.call_args[0][0])

    def test_update_notice_reports_when_the_installed_version_is_current(self):
        from proxy_workbench.branding import PRODUCT_VERSION
        manifest = workspace() / 'update.json'
        manifest.write_text(json.dumps(manifest_for(artifact(workspace()), version='0.0.1')), encoding='utf-8')
        with mock.patch.dict('os.environ', {desktop.UPDATE_MANIFEST_ENV: str(manifest)}, clear=False), \
                mock.patch('builtins.print') as printed:
            code = desktop.main(['--update-notice'])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(printed.call_args[0][0])['available'])
        self.assertEqual(PRODUCT_VERSION, json.loads(printed.call_args[0][0])['current_version'])


if __name__ == '__main__':
    unittest.main()

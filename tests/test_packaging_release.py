"""What the build produces, and what a release note is allowed to claim.

The macOS build itself needs PyInstaller, a signing identity and macOS, so it is
not run here.  What is checked instead is the part that must never be wrong: the
build descriptors, the per-user installer settings, the manifest the release
note is written from, and the refusal to call an unsigned artifact signed.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'packaging'))

import release_manifest
import verify_release
from proxy_workbench import desktop


def workspace():
    return Path(tempfile.mkdtemp(prefix='pw-pack-'))


def done(returncode=0, stdout='', stderr=''):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


def unsigned_runner(command):
    return done(returncode=1)


def manifest_for(path, **extra):
    payload = dict(version='2.2.1', url='https://example.invalid/x',
                   sha256=desktop.sha256_file(path), size=path.stat().st_size)
    payload.update(extra)
    return payload


class BuildDescriptorTests(unittest.TestCase):
    """F23: a windowed GUI, a separate CLI, and a per-user installer."""

    def spec(self, name):
        return (ROOT / 'packaging' / name).read_text(encoding='utf-8')

    def test_the_windows_gui_build_has_no_console(self):
        self.assertIn('console=False', self.spec('proxy-workbench-windows-gui.spec'))

    def test_the_windows_cli_build_has_a_console(self):
        self.assertIn('console=True', self.spec('proxy-workbench-windows-cli.spec'))

    def test_the_gui_and_cli_builds_have_different_names(self):
        self.assertIn("name='proxy-workbench-gui'", self.spec('proxy-workbench-windows-gui.spec'))
        self.assertIn("name='proxy-workbench-cli'", self.spec('proxy-workbench-windows-cli.spec'))

    def test_both_windows_builds_use_the_same_gui_entry_point_as_the_wheel(self):
        # The browser path has to keep working, so the windowed build must run
        # the real GUI entry point rather than a second front end.
        self.assertIn('desktop_launcher.py', self.spec('proxy-workbench-windows-gui.spec'))
        self.assertIn('cli_launcher.py', self.spec('proxy-workbench-windows-cli.spec'))

    def test_every_spec_copies_the_interface_and_the_source_list(self):
        for name in ('proxy-workbench-macos.spec', 'proxy-workbench-windows-gui.spec',
                     'proxy-workbench-windows-cli.spec', 'proxy-workbench.spec'):
            text = self.spec(name)
            self.assertIn("'ui'), 'proxy_workbench/ui'", text, name)
            for resource in ('sources.json', 'source-catalog.json', 'openapi.json'):
                self.assertIn(f"'{resource}'), 'proxy_workbench'", text, name)

    def test_the_macos_bundle_declares_the_version_it_reads_from_the_package(self):
        text = self.spec('proxy-workbench-macos.spec')
        self.assertIn('PRODUCT_VERSION', text)
        self.assertIn("bundle_identifier='org.proxyworkbench.app'", text)

    def test_the_macos_build_is_onedir_not_onefile(self):
        # PyInstaller 6.22 reports that onefile combined with a .app bundle
        # clashes with macOS security and becomes an error in v7.
        text = self.spec('proxy-workbench-macos.spec')
        self.assertIn('COLLECT(', text)
        self.assertIn('exclude_binaries=True', text)

    def test_the_installer_is_per_user_and_never_elevates(self):
        text = self.spec('windows-installer.iss')
        self.assertIn('PrivilegesRequired=lowest', text)
        self.assertIn('DefaultDirName={localappdata}\\Programs', text)
        self.assertNotIn('PrivilegesRequired=admin', text)

    def test_the_installer_refuses_to_build_without_a_version(self):
        self.assertIn('#error ProductVersion is required', self.spec('windows-installer.iss'))


class ManifestTests(unittest.TestCase):
    def test_an_artifact_entry_records_size_digest_and_signature_state(self):
        path = workspace() / 'proxy-workbench-2.2.1-macos-arm64.dmg'
        path.write_bytes(b'disk image')
        entry = release_manifest.artifact_entry(path, platform='linux')
        self.assertEqual(entry['name'], path.name)
        self.assertEqual(entry['kind'], 'macos-dmg')
        self.assertEqual(entry['sha256'], desktop.sha256_file(path))
        self.assertFalse(entry['signature']['signed'])

    def test_a_directory_is_refused_because_it_has_no_single_digest(self):
        with self.assertRaises(release_manifest.ProvenanceError):
            release_manifest.artifact_entry(workspace(), platform='linux')

    def test_a_manifest_never_records_a_signature_nobody_verified(self):
        path = workspace() / 'app.zip'
        path.write_bytes(b'zip')
        entry = release_manifest.artifact_entry(path, platform='linux')
        self.assertFalse(entry['signature']['signed'])

    def test_a_signer_that_does_not_match_the_authority_is_refused(self):
        path = workspace() / 'app.exe'
        path.write_bytes(b'exe')
        entry = dict(name=path.name, kind='windows-executable', bytes=1, sha256='0' * 64,
                     signature=dict(signed=True, authority='Developer ID Application: Someone Else (T)',
                                    kind='codesign', reason='verified'))
        with self.assertRaises(release_manifest.ProvenanceError):
            release_manifest.build_manifest([entry], version='2.2.1', signed_by='Proxy Workbench')

    def test_the_matching_signer_is_accepted(self):
        path = workspace() / 'app.exe'
        path.write_bytes(b'exe')
        entry = dict(name=path.name, kind='windows-executable', bytes=1, sha256='0' * 64,
                     signature=dict(signed=True, authority='Developer ID Application: Proxy Workbench (T)',
                                    kind='codesign', reason='verified'))
        manifest = release_manifest.build_manifest([entry], version='2.2.1',
                                                   signed_by='Proxy Workbench')
        self.assertEqual(manifest['version'], '2.2.1')

    def test_a_written_manifest_round_trips(self):
        path = workspace() / 'app.dmg'
        path.write_bytes(b'dmg')
        manifest = release_manifest.build_manifest([path], version='2.2.1', platform='linux')
        written = release_manifest.write_manifest(workspace() / 'manifest.json', manifest)
        self.assertEqual(release_manifest.read_manifest(written)['artifacts'][0]['name'], 'app.dmg')


class VerifyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.dir = workspace()
        self.artifact = self.dir / 'proxy-workbench-2.2.1-macos-arm64.dmg'
        self.artifact.write_bytes(b'disk image')
        self.manifest = release_manifest.build_manifest([self.artifact], version='2.2.1', platform='linux')
        self.manifest['artifacts'][0]['name'] = self.artifact.name

    def test_an_untouched_artifact_verifies(self):
        report = verify_release.verify_manifest(self.manifest, self.dir, platform='linux')
        self.assertTrue(report['ok'], report['results'])

    def test_a_changed_artifact_fails(self):
        self.artifact.write_bytes(b'disk image, edited')
        report = verify_release.verify_manifest(self.manifest, self.dir, platform='linux')
        self.assertFalse(report['ok'])
        self.assertIn('checksum', report['results'][0]['problem'])

    def test_a_missing_artifact_fails(self):
        self.artifact.unlink()
        report = verify_release.verify_manifest(self.manifest, self.dir, platform='linux')
        self.assertFalse(report['ok'])

    def test_a_manifest_claiming_a_signature_local_tools_cannot_confirm_fails(self):
        self.manifest['artifacts'][0]['signature'] = dict(signed=True, authority='Somebody (T)',
                                                          kind='codesign', reason='claimed')
        report = verify_release.verify_manifest(self.manifest, self.dir, platform='linux',
                                                runner=unsigned_runner)
        self.assertFalse(report['ok'])
        self.assertIn('signature', report['results'][0]['problem'])

    def test_a_signed_file_the_manifest_calls_unsigned_fails(self):
        self.manifest['artifacts'][0]['signature'] = dict(signed=False, kind='unsigned', reason='none')
        report = verify_release.verify_manifest(
            self.manifest, self.dir, platform='darwin', finder=lambda name: f'/usr/bin/{name}',
            runner=lambda command: done(stderr='Authority=Developer ID Application: E (T)\n'))
        self.assertFalse(report['ok'])
        self.assertIn('signed', report['results'][0]['problem'])

    def test_an_adhoc_build_is_recorded_as_unsigned(self):
        # This is the exact state a PyInstaller build lands in with no identity:
        # codesign --verify passes on it, so a naive check would record it as
        # signed and a release note would call a plain build signed.
        adhoc = lambda command: done(stderr='CodeDirectory flags=0x2(adhoc)\nSignature=adhoc\n')
        entry = release_manifest.artifact_entry(self.artifact, platform='darwin', runner=adhoc,
                                                finder=lambda name: f'/usr/bin/{name}')
        self.assertFalse(entry['signature']['signed'])
        self.assertEqual(entry['signature']['kind'], 'adhoc')
        manifest = release_manifest.build_manifest(
            [entry], version='2.2.1', platform='darwin', runner=adhoc, finder=lambda name: f'/usr/bin/{name}')
        self.assertFalse(manifest['artifacts'][0]['signature']['signed'])


class BuildScriptTests(unittest.TestCase):
    def test_windows_installer_receives_absolute_source_and_output_paths(self):
        import build_windows
        with tempfile.TemporaryDirectory(prefix='pw-installer-') as temporary:
            root = Path(temporary).resolve()
            relative = Path('dist with spaces')
            expected = relative.resolve() / 'proxy-workbench-2.3.0-windows-x64-setup.exe'
            def compile_installer(command):
                values = dict(value[2:].split('=', 1) for value in command if str(value).startswith('/D'))
                self.assertEqual(Path(values['SourceDir']), relative.resolve())
                self.assertEqual(Path(values['OutDir']), relative.resolve())
            with mock.patch.object(build_windows, 'run', side_effect=compile_installer), \
                    mock.patch.object(Path, 'is_file', return_value=True):
                result, reason = build_windows.build_installer(root, relative, '2.3.0', compiler='iscc-fixture')
            self.assertEqual(result, expected)
            self.assertIsNone(reason)

    def test_the_macos_build_refuses_to_claim_an_unavailable_signature(self):
        import build_macos
        with mock.patch.object(desktop, 'signing_status',
                               return_value=desktop.SigningStatus('darwin', 'codesign', False,
                                                                 reason='no identity here')):
            with mock.patch.dict('os.environ', {}, clear=False):
                with mock.patch('builtins.print'):
                    code = build_macos.main(['--sign', '--dist', str(workspace())])
        self.assertEqual(code, 3)

    def test_the_windows_build_refuses_to_sign_without_a_thumbprint(self):
        import build_windows
        with mock.patch.object(desktop, 'signing_status',
                               return_value=desktop.SigningStatus('win32', 'signtool', True,
                                                                 reason='tool present')), \
                mock.patch.dict('os.environ', {build_windows.THUMBPRINT_ENV: ''}, clear=False), \
                mock.patch('builtins.print'):
            code = build_windows.main(['--sign', '--dist', str(workspace())])
        self.assertEqual(code, 3)

    def test_a_build_script_never_creates_a_credential(self):
        for name in ('build_macos.py', 'build_windows.py'):
            text = (ROOT / 'packaging' / name).read_text(encoding='utf-8')
            for forbidden in ('create-keypair', 'certutil -addstore', 'New-SelfSignedCertificate',
                              'openssl req', 'generate-private-key'):
                self.assertNotIn(forbidden, text, name)

    def test_no_packaging_file_contains_a_credential(self):
        for path in sorted((ROOT / 'packaging').glob('*')):
            if path.is_dir() or path.suffix == '.pyc':
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            for marker in ('BEGIN PRIVATE KEY', 'BEGIN RSA PRIVATE KEY', 'p@ssw0rd', 'password='):
                self.assertNotIn(marker, text, path.name)


if __name__ == '__main__':
    unittest.main()

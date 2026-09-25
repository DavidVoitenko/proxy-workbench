"""Signing and notarization: what this machine can do, and what an artifact really carries.

F23 forbids publishing an unsigned artifact as a signed one, and forbids
creating or buying signing credentials.  The tests below pin both: ad-hoc
signatures are unsigned, a missing tool is not a pass, and nothing in the
pipeline ever writes a key or asks for a password.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import desktop


def artifact():
    root = Path(tempfile.mkdtemp(prefix='pw-sign-'))
    path = root / 'proxy-workbench-2.2.1-macos-arm64.dmg'
    path.write_bytes(b'not really a disk image')
    return path


def done(returncode=0, stdout='', stderr=''):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class SignatureInspectionTests(unittest.TestCase):
    def setUp(self):
        self.path = artifact()

    def test_a_missing_file_is_unsigned(self):
        info = desktop.signature_of(self.path.parent / 'absent.dmg', platform='darwin',
                                    finder=lambda name: f'/usr/bin/{name}')
        self.assertFalse(info.signed)

    def test_without_a_tool_nothing_is_claimed(self):
        info = desktop.signature_of(self.path, platform='darwin', finder=lambda name: None)
        self.assertFalse(info.signed)
        self.assertEqual(info.kind, 'unknown')

    def test_a_file_codesign_cannot_display_is_unsigned(self):
        info = desktop.signature_of(self.path, platform='darwin', finder=lambda name: f'/usr/bin/{name}',
                                    runner=lambda command: done(returncode=1))
        self.assertFalse(info.signed)
        self.assertEqual(info.kind, 'unsigned')

    def test_an_adhoc_signature_is_not_a_distribution_signature(self):
        # PyInstaller signs an unsigned build ad-hoc by default and
        # `codesign --verify` passes on it.  Reporting that as "signed" is how
        # an unsigned artifact gets published as a signed one.
        display = done(stderr='Executable=/x\nCodeDirectory v=20400 flags=0x2(adhoc)\nSignature=adhoc\n')
        info = desktop.signature_of(self.path, platform='darwin', finder=lambda name: f'/usr/bin/{name}',
                                    runner=lambda command: display)
        self.assertFalse(info.signed)
        self.assertEqual(info.kind, 'adhoc')
        self.assertIn('ad-hoc', info.reason)

    def test_a_signature_with_no_authority_is_not_signed(self):
        display = done(stderr='CodeDirectory v=20400\n')
        info = desktop.signature_of(self.path, platform='darwin', finder=lambda name: f'/usr/bin/{name}',
                                    runner=lambda command: display)
        self.assertFalse(info.signed)
        self.assertEqual(info.kind, 'unsigned')

    def test_a_verified_developer_id_signature_is_signed(self):
        def runner(command):
            if '--verify' in command:
                return done()
            return done(stderr='Authority=Developer ID Application: Example (TEAMID)\n')
        info = desktop.signature_of(self.path, platform='darwin', finder=lambda name: f'/usr/bin/{name}',
                                    runner=runner)
        self.assertTrue(info.signed)
        self.assertEqual(info.authority, 'Developer ID Application: Example (TEAMID)')

    def test_a_present_signature_that_fails_verification_is_not_signed(self):
        def runner(command):
            if '--verify' in command:
                return done(returncode=1)
            return done(stderr='Authority=Developer ID Application: Example (TEAMID)\n')
        info = desktop.signature_of(self.path, platform='darwin', finder=lambda name: f'/usr/bin/{name}',
                                    runner=runner)
        self.assertFalse(info.signed)
        self.assertEqual(info.kind, 'unverified')

    def test_windows_without_signtool_claims_nothing(self):
        info = desktop.signature_of(self.path, platform='win32', finder=lambda name: None)
        self.assertFalse(info.signed)
        self.assertEqual(info.kind, 'unknown')

    def test_windows_with_a_passing_signtool_check_is_signed(self):
        info = desktop.signature_of(self.path, platform='win32', finder=lambda name: r'C:\signtool.exe',
                                    runner=lambda command: done())
        self.assertTrue(info.signed)
        self.assertEqual(info.kind, 'authenticode')

    def test_an_unimplemented_platform_does_not_guess(self):
        info = desktop.signature_of(self.path, platform='linux')
        self.assertFalse(info.signed)
        self.assertEqual(info.kind, 'unknown')


class SigningCapabilityTests(unittest.TestCase):
    def test_macos_without_codesign_reports_what_is_missing(self):
        status = desktop.signing_status(platform='darwin', finder=lambda name: None)
        self.assertFalse(status.available)
        self.assertIn('codesign', status.reason)

    def test_macos_without_any_identity_is_not_available(self):
        status = desktop.signing_status(platform='darwin', finder=lambda name: f'/usr/bin/{name}',
                                        runner=lambda command: done(stdout='     0 valid identities found\n'))
        self.assertFalse(status.available)

    def test_a_developer_id_identity_makes_signing_available(self):
        listing = done(stdout='  1) ABC123 "Developer ID Application: Example (TEAMID)"\n')
        status = desktop.signing_status(platform='darwin', finder=lambda name: f'/usr/bin/{name}',
                                        runner=lambda command: listing)
        self.assertTrue(status.available)
        self.assertEqual(status.identity, 'Developer ID Application: Example (TEAMID)')

    def test_a_development_identity_is_flagged_as_not_for_distribution(self):
        listing = done(stdout='  1) ABC123 "Apple Development: Someone (TEAMID)"\n')
        status = desktop.signing_status(platform='darwin', finder=lambda name: f'/usr/bin/{name}',
                                        runner=lambda command: listing)
        self.assertTrue(status.available)
        self.assertIn('Apple Development', status.identity)
        self.assertNotIn('Developer ID Application', status.identity)

    def test_windows_without_signtool_reports_what_is_missing(self):
        status = desktop.signing_status(platform='win32', finder=lambda name: None)
        self.assertFalse(status.available)
        self.assertIn('signtool', status.reason)

    def test_windows_with_the_tool_but_no_certificate_is_still_only_a_tool(self):
        status = desktop.signing_status(platform='win32', finder=lambda name: r'C:\signtool.exe')
        self.assertTrue(status.available)
        self.assertIn('certificate', status.reason)

    def test_linux_has_no_signing_configuration(self):
        self.assertFalse(desktop.signing_status(platform='linux').available)


class NotarizationCapabilityTests(unittest.TestCase):
    def test_macos_without_credentials_names_them(self):
        status = desktop.notarization_status(environ={}, platform='darwin',
                                             finder=lambda name: f'/usr/bin/{name}')
        self.assertFalse(status.available)
        self.assertEqual(status.env_names, ('APPLE_ID', 'APPLE_PASSWORD', 'APPLE_TEAM_ID'))

    def test_a_stored_profile_is_enough_to_try(self):
        status = desktop.notarization_status(environ={'NOTARYTOOL_PROFILE': 'workbench'}, platform='darwin',
                                             finder=lambda name: f'/usr/bin/{name}')
        self.assertTrue(status.available)
        self.assertEqual(status.profile, 'workbench')

    def test_credentials_that_notarytool_rejects_are_not_reported_as_available(self):
        status = desktop.notarization_status(
            environ={'APPLE_ID': 'a@example.invalid', 'APPLE_PASSWORD': 'x', 'APPLE_TEAM_ID': 'T'},
            platform='darwin', finder=lambda name: f'/usr/bin/{name}',
            runner=lambda command: done(returncode=1))
        self.assertFalse(status.available)

    def test_notarization_is_described_for_macos_only(self):
        status = desktop.notarization_status(environ={}, platform='linux')
        self.assertFalse(status.available)
        self.assertIn('macOS', status.reason)


class PublishingLabelTests(unittest.TestCase):
    """One wording per signature state; a label is never better than the evidence."""

    def test_an_available_tool_that_did_not_sign_is_not_called_signed(self):
        status = desktop.signing_status(platform='darwin', finder=lambda name: f'/usr/bin/{name}',
                                        runner=lambda command: done(stdout='  1) A "Developer ID Application: E (T)"\n'))
        label = desktop.publish_label(status)
        self.assertNotEqual(label, desktop.tr('подписано и нотарифицировано', 'signed and notarized'))
        self.assertIn('not performed', label)

    def test_only_a_signed_and_notarized_build_gets_the_full_label(self):
        status = desktop.signing_status(platform='darwin', finder=lambda name: f'/usr/bin/{name}',
                                        runner=lambda command: done(stdout='  1) A "Developer ID Application: E (T)"\n'))
        self.assertEqual(desktop.publish_label(status, notarized=True),
                         desktop.tr('подписано и нотарифицировано', 'signed and notarized'))

    def test_an_unavailable_tool_is_reported_as_unsigned(self):
        status = desktop.signing_status(platform='darwin', finder=lambda name: None)
        self.assertEqual(desktop.publish_label(status, notarized=True),
                         desktop.tr('не подписано', 'unsigned'))


class NoCredentialCreationTests(unittest.TestCase):
    """The pipeline looks for credentials; it never makes them."""

    def test_the_module_writes_nothing_and_never_runs_a_key_creation_command(self):
        source = Path(desktop.__file__).read_text(encoding='utf-8')
        for forbidden in ('security create-keypair', 'security import', 'certutil -addstore',
                          'certutil -import', 'New-SelfSignedCertificate', 'openssl req',
                          'generate-private-key'):
            self.assertNotIn(forbidden, source)

    def test_status_probes_only_read_commands(self):
        seen = []

        def runner(command):
            seen.append(command)
            return done(stdout='')

        desktop.signing_status(platform='darwin', finder=lambda name: f'/usr/bin/{name}', runner=runner)
        desktop.notarization_status(environ={'NOTARYTOOL_PROFILE': 'p'}, platform='darwin',
                                    finder=lambda name: f'/usr/bin/{name}', runner=runner)
        for command in seen:
            self.assertNotIn('create', ' '.join(command))
            self.assertNotIn('delete', ' '.join(command))


class SchemaMirrorTests(unittest.TestCase):
    def test_the_fallback_schema_version_matches_the_storage_layer(self):
        # desktop.py carries a fallback so it imports without the storage
        # layer; a stale copy would make the update check refuse valid updates.
        try:
            from proxy_workbench import db
        except ImportError:
            self.skipTest('the storage layer is not present in this tree')
        self.assertEqual(desktop.supported_schema_version(), db.SCHEMA_VERSION)
        self.assertEqual(desktop.FALLBACK_SCHEMA_VERSION, db.SCHEMA_VERSION)


if __name__ == '__main__':
    unittest.main()

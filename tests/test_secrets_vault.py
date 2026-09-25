"""Secret stores: memory, session-only and the OS keychain adapter."""
import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import secrets as s

CANARY = 'canary-secret-1f2e'


def payload(secret=CANARY, username='alice', revision=1):
    return s.SecretPayload(username=username, password=secret,
                           verifier=s.make_verifier(secret, iterations=1000), revision=revision)


class StubKeyring:
    """A keyring-shaped object. Nothing here touches a real OS keychain."""

    def __init__(self):
        self.items = {}

    def set_password(self, service, account, value):
        self.items[(service, account)] = value

    def get_password(self, service, account):
        return self.items.get((service, account))

    def delete_password(self, service, account):
        if (service, account) not in self.items:
            raise KeyError('password not found')  # keyring raises for a missing entry too
        del self.items[(service, account)]
        return True


class MemoryVaultTests(unittest.TestCase):
    def setUp(self):
        self.vault = s.MemoryVault()

    def test_stage_is_not_readable_as_ready_until_finalized(self):
        self.vault.stage('sec_a', payload())
        self.assertEqual(self.vault.state('sec_a'), s.STATE_STAGED)
        self.vault.mark_ready('sec_a')
        self.assertEqual(self.vault.state('sec_a'), s.STATE_READY)
        self.assertEqual(self.vault.get('sec_a').password, CANARY)

    def test_unknown_reference_reports_not_provided(self):
        self.assertIsNone(self.vault.state('sec_missing'))
        with self.assertRaises(s.SecretNotProvided) as caught:
            self.vault.get('sec_missing')
        self.assertEqual(caught.exception.code, s.E_NOT_PROVIDED)
        self.assertTrue(caught.exception.action)
        with self.assertRaises(s.SecretNotProvided):
            self.vault.mark_ready('sec_missing')

    def test_locked_vault_refuses_every_operation(self):
        self.vault.stage('sec_a', payload())
        self.vault.mark_ready('sec_a')
        self.vault.lock()
        self.assertTrue(self.vault.locked)
        for call in (lambda: self.vault.get('sec_a'), lambda: self.vault.stage('sec_b', payload()),
                     lambda: self.vault.delete('sec_a'), lambda: self.vault.state('sec_a'),
                     lambda: self.vault.refs()):
            with self.assertRaises(s.SecretVaultLocked) as caught:
                call()
            self.assertEqual(caught.exception.code, s.E_VAULT_LOCKED)
        self.vault.unlock()
        self.assertEqual(self.vault.get('sec_a').password, CANARY)

    def test_refs_delete_and_close(self):
        self.vault.stage('sec_b', payload())
        self.vault.stage('sec_a', payload())
        self.assertEqual(self.vault.refs(), ['sec_a', 'sec_b'])
        self.assertTrue(self.vault.delete('sec_a'))
        self.assertFalse(self.vault.delete('sec_a'))
        self.assertEqual(self.vault.refs(), ['sec_b'])
        self.vault.close()
        self.assertEqual(self.vault.refs(), [])

    def test_vault_has_no_file_backing(self):
        self.assertIsNone(s.MemoryVault.storage_path)
        self.assertFalse(s.MemoryVault.is_persistent)
        with tempfile.TemporaryDirectory() as folder:
            here = os.getcwd()
            os.chdir(folder)
            try:
                self.vault.stage('sec_a', payload())
                self.vault.mark_ready('sec_a')
            finally:
                os.chdir(here)
            self.assertEqual(os.listdir(folder), [])

    def test_stage_refuses_a_foreign_payload(self):
        with self.assertRaises(s.SecretValidationError):
            self.vault.stage('sec_a', {'username': 'a', 'password': CANARY})


class SessionVaultTests(unittest.TestCase):
    def tearDown(self):
        for session_id in list(getattr(s, '_SESSIONS', {})):
            s.SessionVault.forget(session_id)

    def test_session_is_claimable_inside_its_own_process(self):
        vault = s.SessionVault('session-under-test')
        vault.stage('sec_a', payload())
        vault.mark_ready('sec_a')
        self.assertIs(s.SessionVault.claim('session-under-test'), vault)
        self.assertEqual(vault.get('sec_a').password, CANARY)

    def test_unknown_session_is_locked_not_empty(self):
        with self.assertRaises(s.SecretVaultLocked) as caught:
            s.SessionVault.claim('session-that-does-not-exist')
        self.assertEqual(caught.exception.code, s.E_VAULT_LOCKED)
        self.assertIn('session', caught.exception.message)

    def test_expired_session_reports_not_provided(self):
        clock = {'now': 100.0}
        vault = s.SessionVault('session-expiring', lifetime_s=10, now=lambda: clock['now'])
        vault.stage('sec_a', payload())
        vault.mark_ready('sec_a')
        self.assertEqual(vault.get('sec_a').password, CANARY)
        clock['now'] = 111.0
        self.assertTrue(vault.expired)
        with self.assertRaises(s.SecretNotProvided) as caught:
            vault.get('sec_a')
        self.assertEqual(caught.exception.code, s.E_NOT_PROVIDED)

    def test_session_never_keeps_a_secret_after_close(self):
        vault = s.SessionVault('session-closed')
        vault.stage('sec_a', payload())
        vault.close()
        self.assertEqual(vault.refs(), [])
        with self.assertRaises(s.SecretVaultLocked):
            s.SessionVault.claim('session-closed')

    def test_another_process_cannot_claim_a_session_secret(self):
        vault = s.SessionVault('session-across-processes')
        vault.stage('sec_a', payload())
        vault.mark_ready('sec_a')
        code = """
import sys
sys.path.insert(0, %r)
from proxy_workbench import secrets as s
try:
    s.SessionVault.claim(sys.argv[1])
    print('CLAIMED')
except s.SecretError as exc:
    print(exc.code)
""" % str(Path(__file__).resolve().parents[1])
        done = subprocess.run([sys.executable, '-c', code, vault.session_id],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), s.E_VAULT_LOCKED)
        self.assertNotIn(CANARY, done.stdout + done.stderr)
        self.assertNotIn(CANARY, ' '.join(done.args))

    def test_a_password_is_never_needed_to_reach_the_session(self):
        """The worker gets a reference and a session id, never a credential."""
        vault = s.SessionVault()
        vault.stage('sec_a', payload())
        vault.mark_ready('sec_a')
        argv = ['worker-check', '--access', 'acc-1', '--ref', vault.refs()[0], '--session', vault.session_id]
        self.assertNotIn(CANARY, ' '.join(argv))
        self.assertEqual(vault.get('sec_a').password, CANARY)


class OsVaultTests(unittest.TestCase):
    def test_round_trip_through_a_keyring_shaped_stub(self):
        vault = s.OsVault('proxy-workbench-test', keyring_module=StubKeyring())
        self.assertTrue(vault.is_persistent)
        self.assertIsNone(vault.storage_path)
        vault.stage('sec_a', payload())
        stored = vault.get('sec_a')
        self.assertEqual((stored.username, stored.password, stored.revision), ('alice', CANARY, 1))
        self.assertEqual(vault.state('sec_a'), s.STATE_READY)
        self.assertTrue(vault.delete('sec_a'))
        self.assertFalse(vault.delete('sec_a'))
        self.assertEqual(vault.refs(), [])

    def test_rotation_keeps_a_verifier_not_the_previous_password(self):
        stub = StubKeyring()
        vault = s.OsVault('proxy-workbench-test', keyring_module=stub)
        vault.stage('sec_a', payload())
        first = vault.get('sec_a')
        second = 'canary-secret-2'
        vault.stage('sec_a', s.SecretPayload(username='alice', password=second,
                                              verifier=s.make_verifier(second, iterations=1000),
                                              revision=2, previous_verifier=first.verifier))
        blob = stub.items[('proxy-workbench-test', 'access:sec_a')]
        stored = vault.get('sec_a')
        self.assertEqual(stored.revision, 2)
        self.assertTrue(stored.matches(second))
        self.assertFalse(stored.matches(CANARY))
        self.assertTrue(stored.matches_previous(CANARY))
        self.assertIn(second, blob)
        self.assertNotIn(CANARY, blob)

    def test_locked_keychain_maps_to_the_vault_locked_code(self):
        class Locked(StubKeyring):
            def get_password(self, service, account):
                raise RuntimeError('The user name or passphrase you entered is not correct')

        vault = s.OsVault('proxy-workbench-test', keyring_module=Locked())
        vault.stage('sec_a', payload())
        with self.assertRaises(s.SecretVaultLocked) as caught:
            vault.get('sec_a')
        self.assertEqual(caught.exception.code, s.E_VAULT_LOCKED)

    def test_missing_entry_reports_not_provided(self):
        vault = s.OsVault('proxy-workbench-test', keyring_module=StubKeyring())
        with self.assertRaises(s.SecretNotProvided) as caught:
            vault.get('sec_absent')
        self.assertEqual(caught.exception.code, s.E_NOT_PROVIDED)

    def test_unreadable_entry_reports_not_provided(self):
        stub = StubKeyring()
        stub.set_password('proxy-workbench-test', 'access:sec_a', 'not json')
        vault = s.OsVault('proxy-workbench-test', keyring_module=stub)
        with self.assertRaises(s.SecretNotProvided):
            vault.get('sec_a')

    def test_without_a_backend_it_refuses_instead_of_writing_a_file(self):
        with self._no_keyring():
            self.assertFalse(s.OsVault.available())
            with self.assertRaises(s.SecretVaultUnavailable) as caught:
                s.OsVault('proxy-workbench-test')
        self.assertEqual(caught.exception.code, s.E_VAULT_UNAVAILABLE)
        self.assertTrue(caught.exception.action)

    @contextlib.contextmanager
    def _no_keyring(self):
        """Pretend the optional keyring dependency is not installed."""
        saved = sys.modules.get('keyring', 'absent')
        sys.modules['keyring'] = None
        try:
            yield
        finally:
            if saved == 'absent':
                del sys.modules['keyring']
            else:
                sys.modules['keyring'] = saved


class OpenVaultTests(unittest.TestCase):
    def tearDown(self):
        for session_id in list(getattr(s, '_SESSIONS', {})):
            s.SessionVault.forget(session_id)

    def test_session_preference_gives_a_session_vault(self):
        vault = s.open_vault('session', session_id='session-open')
        self.assertIsInstance(vault, s.SessionVault)
        self.assertFalse(vault.is_persistent)
        self.assertEqual(vault.session_id, 'session-open')

    def test_auto_preference_matches_what_this_installation_offers(self):
        vault = s.open_vault('auto')
        if s.OsVault.available():
            self.assertIsInstance(vault, s.OsVault)
        else:
            self.assertIsInstance(vault, s.SessionVault)

    def test_os_preference_never_silently_downgrades(self):
        if s.OsVault.available():
            self.assertIsInstance(s.open_vault('os'), s.OsVault)
        else:
            with self.assertRaises(s.SecretVaultUnavailable) as caught:
                s.open_vault('os')
            self.assertEqual(caught.exception.code, s.E_VAULT_UNAVAILABLE)

    def test_unknown_preference_is_refused(self):
        with self.assertRaises(s.SecretValidationError):
            s.open_vault('usb-stick')

    def test_references_are_random_and_disclose_nothing(self):
        refs = {s.new_reference() for _ in range(64)}
        self.assertEqual(len(refs), 64)
        for ref in refs:
            self.assertTrue(ref.startswith('sec_'))
            self.assertNotIn(CANARY, ref)


if __name__ == '__main__':
    unittest.main()

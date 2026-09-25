"""One-way verifiers for stored credentials (proxy_workbench/secrets.py)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import secrets as s

FAST = 1000


class VerifierTests(unittest.TestCase):
    def test_round_trip_for_text_and_bytes(self):
        for secret in ('canary-secret', 'пароль-ёжик', b'canary-bytes'):
            verifier = s.make_verifier(secret, iterations=FAST)
            self.assertTrue(s.verify_secret(secret, verifier))
            self.assertTrue(s.verify_secret(secret.decode() if isinstance(secret, bytes) else secret, verifier))

    def test_wrong_secret_is_rejected(self):
        verifier = s.make_verifier('canary-secret', iterations=FAST)
        self.assertFalse(s.verify_secret('canary-secre', verifier))
        self.assertFalse(s.verify_secret('canary-secret ', verifier))
        self.assertFalse(s.verify_secret('CANARY-SECRET', verifier))
        self.assertFalse(s.verify_secret('', verifier))

    def test_same_secret_gets_a_fresh_salt(self):
        first = s.make_verifier('canary-secret', iterations=FAST)
        second = s.make_verifier('canary-secret', iterations=FAST)
        self.assertNotEqual(first, second)
        self.assertTrue(s.verify_secret('canary-secret', first))
        self.assertTrue(s.verify_secret('canary-secret', second))

    def test_verifier_never_contains_the_secret(self):
        verifier = s.make_verifier('canary-secret', iterations=FAST)
        self.assertNotIn('canary-secret', verifier)
        self.assertEqual(verifier.split('$')[0], s.VERIFIER_ALGO)

    def test_explicit_salt_is_honoured(self):
        first = s.make_verifier('canary-secret', salt=b'0123456789abcdef', iterations=FAST)
        self.assertEqual(first, s.make_verifier('canary-secret', salt=b'0123456789abcdef', iterations=FAST))
        self.assertNotEqual(first, s.make_verifier('canary-secret', salt=b'fedcba9876543210', iterations=FAST))
        self.assertTrue(s.verify_secret('canary-secret', first))

    def test_malformed_verifier_is_false_and_not_an_exception(self):
        good = s.make_verifier('canary-secret', iterations=FAST)
        broken = ['', 'junk', good.replace(s.VERIFIER_ALGO, 'md5', 1), good.rsplit('$', 1)[0],
                  good.rsplit('$', 1)[0] + '$not base64!', good + '$extra', good.replace('$1000$', '$0$'),
                  good.rsplit('$', 1)[0] + '$']
        for verifier in broken:
            self.assertFalse(s.verify_secret('canary-secret', verifier), verifier)
        for verifier in (None, 42, object(), b'bytes'):
            self.assertFalse(s.verify_secret('canary-secret', verifier))

    def test_short_or_empty_salt_is_refused(self):
        for salt in (b'', b'1234567', b'x' * 65, 'not bytes'):
            with self.assertRaises(s.SecretValidationError):
                s.make_verifier('canary-secret', salt=salt, iterations=FAST)

    def test_zero_iterations_is_refused(self):
        with self.assertRaises(s.SecretValidationError):
            s.make_verifier('canary-secret', iterations=0)

    def test_empty_and_oversized_secrets_are_refused(self):
        with self.assertRaises(s.SecretValidationError):
            s.make_verifier('')
        with self.assertRaises(s.SecretValidationError):
            s.make_verifier('x' * (s.MAX_SECRET_BYTES + 1))
        with self.assertRaises(s.SecretValidationError):
            s.make_verifier(None)

    def test_comparison_does_not_stop_at_the_first_wrong_digest(self):
        """A verifier is compared whole; a tampered tail must fail, not pass."""
        good = s.make_verifier('canary-secret', iterations=FAST)
        algo, cost, salt, digest = good.split('$')
        flipped = digest[:-2] + ('A' if digest[-2] != 'A' else 'B') + digest[-1]
        self.assertFalse(s.verify_secret('canary-secret', '$'.join((algo, cost, salt, flipped))))


if __name__ == '__main__':
    unittest.main()

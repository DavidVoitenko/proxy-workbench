"""The stored verifier is a record, not a decoding of the secret.

`secrets.make_verifier` writes `algo$iterations$salt$digest`.  `verify_secret`
used to accept any base64 spelling that decoded to the stored digest, and a
base64 field has spare bits in its last character, so two different stored
strings meant the same secret.  That made the pair a one-way function in only
one direction, and a rewritten row authenticated as if it were untouched.

These tests pin the closed contract: the stored text is the canonical one, the
comparison is whole, and no answer derived from a secret contains it.
"""
import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import secrets as s

CANARY = 'CANARY-VERIFIER-2b7d-do-not-leak'
FAST = 1000


def parts(verifier):
    algo, cost, salt, digest = verifier.split('$')
    return algo, int(cost), salt, digest


class CanonicalVerifierTests(unittest.TestCase):
    def test_a_verifier_round_trips_and_a_tampered_tail_fails(self):
        good = s.make_verifier(CANARY, iterations=FAST)
        self.assertTrue(s.verify_secret(CANARY, good))
        for position in (0, 5, len(good) - 2, len(good) - 1):
            with self.subTest(position=position):
                mangled = good[:position] + ('A' if good[position] != 'A' else 'B') + good[position + 1:]
                self.assertFalse(s.verify_secret(CANARY, mangled),
                                 'a changed character must not authenticate')

    def test_a_non_canonical_spelling_of_the_right_digest_is_refused(self):
        """The exact case that made the comparison flaky.

        A 32-byte digest is 43 base64 characters, and the last of them carries
        two bits that no byte uses, so four different characters spell the same
        32 bytes.  `make_verifier` writes the one with the spare bits clear; the
        other three used to verify as well.
        """
        verifier = s.make_verifier(CANARY, iterations=FAST)
        _algo, _cost, salt, digest = parts(verifier)
        self.assertEqual(len(digest), 44, 'a 32-byte digest is 43 characters plus padding')
        raw = base64.b64decode(digest, validate=True)
        for char in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/':
            spelled = digest[:-2] + char + digest[-1]
            if spelled == digest or base64.b64decode(spelled, validate=True) != raw:
                continue
            with self.subTest(spelling=spelled):
                self.assertNotEqual(spelled, digest, 'a genuinely different spelling')
                self.assertFalse(s.verify_secret(CANARY, '$'.join(
                    (s.VERIFIER_ALGO, str(FAST), salt, spelled))),
                    'a non-canonical spelling of a valid digest must not authenticate')
            return
        self.fail('no non-canonical spelling of this digest was found')

    def test_a_salt_or_digest_that_is_not_canonical_is_refused(self):
        verifier = s.make_verifier(CANARY, iterations=FAST)
        algo, cost, salt, digest = parts(verifier)
        for field, value in (('salt', salt + 'A=='), ('digest', digest.rstrip('=')),
                             ('digest', digest.lower() if any(c.isupper() for c in digest) else digest.upper())):
            with self.subTest(field=field, value=value):
                mangled = '$'.join((algo, str(cost), value, digest)) if field == 'salt' \
                    else '$'.join((algo, str(cost), salt, value))
                self.assertFalse(s.verify_secret(CANARY, mangled))

    def test_the_stored_verifier_never_contains_the_secret(self):
        verifier = s.make_verifier(CANARY, iterations=FAST)
        self.assertNotIn(CANARY, verifier)
        self.assertFalse(s.verify_secret(CANARY + 'x', verifier))
        self.assertFalse(s.verify_secret('', verifier))

    def test_a_foreign_or_malformed_verifier_is_false_and_never_raises(self):
        for bad in ('', 'x', 'nope', 'pbkdf2_sha256$x$a$b', 'md5$1$aaaa$bbbb',
                    'pbkdf2_sha256$1$!!!!$@@@@', None, 42, b'bytes'):
            with self.subTest(bad=bad):
                self.assertFalse(s.verify_secret(CANARY, bad))

    def test_two_accesses_with_one_password_store_different_strings(self):
        one = s.make_verifier(CANARY, iterations=FAST)
        two = s.make_verifier(CANARY, iterations=FAST)
        self.assertNotEqual(one, two, 'a per-call random salt keeps the rows distinguishable')
        self.assertTrue(s.verify_secret(CANARY, one) and s.verify_secret(CANARY, two))

    def test_every_useful_error_names_what_to_do_next(self):
        for bad in ('', 'x' * (s.MAX_SECRET_BYTES + 1), None):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(s.SecretValidationError) as caught:
                    s.make_verifier(bad)
                self.assertTrue(caught.exception.action,
                                'a useful error names the next step, not only its class')


if __name__ == '__main__':
    unittest.main()

"""F04 on the wire: the credential material a transport is allowed to build.

`secrets.py` owns the access identity and the vault; the socket belongs to the
caller.  Until now the module could say "this access is HTTP Basic" and
"this access is SOCKS5" but had no way to produce the bytes either mechanism
needs, so the full path of F04 could not be completed by any caller.  These
tests pin the encoders, and pin the three rules that keep a secret off a URL, a
query string, a settings file and a log line: the material exists only as a
short-lived value, only for the mode the access declares, and only for the
revision the caller actually resolved.
"""
import base64
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import secrets as s

CANARY = 'CANARY-TRANSPORT-PASSWORD-do-not-leak'
USERNAME = 'alice'


def access(mode, access_id='acc-1', revision=1):
    return s.Access(id=access_id, endpoint_id='ep-1', mode=mode, secret_ref='sec_1',
                    access_revision=revision, created_at=1000.0, rotated_at=None)


def resolved(mode, username=USERNAME, password=CANARY, acc=None):
    acc = acc or access(mode)
    return s.ResolvedAccess(access=acc, mode=mode, username=username, password=password)


class HttpBasicTests(unittest.TestCase):
    def test_the_header_is_the_rfc_7617_encoding_of_user_colon_password(self):
        header = s.proxy_authorization(USERNAME, CANARY)
        scheme, _, token = header.partition(' ')
        self.assertEqual(scheme, 'Basic')
        self.assertEqual(base64.b64decode(token).decode('utf-8'), '%s:%s' % (USERNAME, CANARY))

    def test_an_open_proxy_sends_no_header_at_all(self):
        self.assertEqual(s.proxy_authorization('', ''), '')

    def test_a_half_credential_is_refused_rather_than_sent_empty(self):
        with self.assertRaises(s.SecretValidationError):
            s.proxy_authorization(USERNAME, None)

    def test_the_material_is_built_for_http_and_https_and_for_nothing_else(self):
        acc, res = access(s.MODE_HTTP_BASIC), resolved(s.MODE_HTTP_BASIC)
        for scheme in ('http', 'https'):
            with self.subTest(scheme=scheme):
                creds = s.transport_credentials(acc, res, scheme=scheme)
                self.assertTrue(creds.authenticated)
                self.assertTrue(creds.proxy_authorization.startswith('Basic '))
                self.assertEqual(creds.socks5_auth, b'')
        for scheme in ('socks5', 'socks5h', 'socks4'):
            with self.subTest(scheme=scheme):
                with self.assertRaises(s.SecretValidationError):
                    s.transport_credentials(acc, res, scheme=scheme)

    def test_a_socks5_identity_is_never_offered_to_an_http_proxy(self):
        acc, res = access(s.MODE_SOCKS5), resolved(s.MODE_SOCKS5)
        for scheme in ('http', 'https', 'socks4'):
            with self.subTest(scheme=scheme):
                with self.assertRaises(s.SecretValidationError):
                    s.transport_credentials(acc, res, scheme=scheme)
        for scheme in ('socks5', 'socks5h'):
            with self.subTest(scheme=scheme):
                self.assertTrue(s.transport_credentials(acc, res, scheme=scheme).authenticated)


class Socks5Tests(unittest.TestCase):
    def test_the_greeting_offers_only_the_method_the_access_uses(self):
        self.assertEqual(s.socks5_greeting(with_auth=False), b'\x05\x01\x00')
        self.assertEqual(s.socks5_greeting(with_auth=True), b'\x05\x01\x02')

    def test_the_sub_negotiation_is_rfc_1929(self):
        raw = s.socks5_username_password(USERNAME, CANARY)
        self.assertEqual(raw[0], 0x01, 'version')
        self.assertEqual(raw[1], len(USERNAME.encode()))
        self.assertEqual(raw[2:2 + len(USERNAME)], USERNAME.encode())
        length = raw[2 + len(USERNAME)]
        self.assertEqual(length, len(CANARY.encode()))
        self.assertEqual(raw[3 + len(USERNAME):], CANARY.encode())
        self.assertEqual(raw, struct.pack('>BB', 1, len(USERNAME)) + USERNAME.encode()
                         + struct.pack('>B', len(CANARY)) + CANARY.encode())

    def test_a_credential_that_cannot_be_framed_is_refused_before_the_socket(self):
        for user, secret in (('', CANARY), (USERNAME, ''), ('u' * 256, CANARY),
                             (USERNAME, 'x' * 256), (USERNAME, 'ю' * 200)):
            with self.subTest(user=user[:12], secret=secret[:12]):
                with self.assertRaises(s.SecretValidationError):
                    s.socks5_username_password(user, secret)

    def test_a_socks5_access_yields_the_greeting_and_the_sub_negotiation(self):
        acc, res = access(s.MODE_SOCKS5), resolved(s.MODE_SOCKS5)
        creds = s.transport_credentials(acc, res, scheme='socks5h')
        self.assertEqual(creds.socks5_greeting, b'\x05\x01\x02')
        self.assertEqual(creds.socks5_auth, s.socks5_username_password(USERNAME, CANARY))
        self.assertEqual(creds.proxy_authorization, '', 'an HTTP header is never built for SOCKS5')


class OpenProxyTests(unittest.TestCase):
    def test_a_credential_free_access_produces_no_credential(self):
        acc = access(s.MODE_NONE)
        res = resolved(s.MODE_NONE, username='', password='')
        for scheme in ('http', 'https', 'socks5'):
            with self.subTest(scheme=scheme):
                creds = s.transport_credentials(acc, res, scheme=scheme)
                self.assertFalse(creds.authenticated)
                self.assertEqual(creds.proxy_authorization, '')
                self.assertEqual(creds.socks5_auth, b'')

    def test_a_socks5_open_proxy_still_offers_the_no_auth_greeting(self):
        creds = s.transport_credentials(access(s.MODE_NONE), resolved(s.MODE_NONE, '', ''),
                                        scheme='socks5')
        self.assertEqual(creds.socks5_greeting, b'\x05\x01\x00')
        self.assertEqual(creds.socks5_auth, b'')


class RefusalTests(unittest.TestCase):
    def test_a_secret_of_another_access_is_refused(self):
        mine, theirs = access(s.MODE_HTTP_BASIC, 'acc-1'), access(s.MODE_HTTP_BASIC, 'acc-2')
        with self.assertRaises(s.SecretConflictError):
            s.transport_credentials(mine, resolved(s.MODE_HTTP_BASIC, acc=theirs), scheme='http')

    def test_a_secret_from_a_superseded_revision_is_not_sent(self):
        acc = access(s.MODE_HTTP_BASIC, revision=2)
        stale = resolved(s.MODE_HTTP_BASIC, acc=access(s.MODE_HTTP_BASIC, revision=1))
        with self.assertRaises(s.SecretConflictError) as caught:
            s.transport_credentials(acc, stale, scheme='http')
        self.assertIn('re-resolve', caught.exception.action)

    def test_an_unsupported_scheme_is_refused_by_name(self):
        for scheme in ('ntlm', 'kerberos', '', None, 'ftp'):
            with self.subTest(scheme=scheme):
                with self.assertRaises(s.SecretUnsupportedError):
                    s.transport_credentials(access(s.MODE_HTTP_BASIC),
                                           resolved(s.MODE_HTTP_BASIC), scheme=scheme)

    def test_the_describable_form_never_carries_the_credential(self):
        creds = s.transport_credentials(access(s.MODE_SOCKS5), resolved(s.MODE_SOCKS5), scheme='socks5')
        import json
        rendered = json.dumps(creds.as_json())
        self.assertNotIn(CANARY, rendered)
        self.assertNotIn(USERNAME, rendered)
        self.assertTrue(creds.as_json()['authenticated'])


if __name__ == '__main__':
    unittest.main()

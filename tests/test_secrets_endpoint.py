"""The access path for hostnames, IDNA, IPv4, IPv6, HTTP Basic and SOCKS5."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import secrets as s
from proxy_workbench import proxytool

CANARY = 'canary-secret-5b1d'


class ParseEndpointTests(unittest.TestCase):
    def test_hostname_port_and_scheme(self):
        endpoint = s.parse_endpoint('socks5://Proxy.Example.COM.:1080')
        self.assertEqual((endpoint.scheme, endpoint.host, endpoint.port), ('socks5', 'proxy.example.com', 1080))
        self.assertTrue(endpoint.is_hostname)
        self.assertEqual(endpoint.canonical, 'socks5://proxy.example.com:1080')
        self.assertEqual(endpoint.authority, 'proxy.example.com:1080')

    def test_bare_host_port_defaults_to_http(self):
        self.assertEqual(s.parse_endpoint('proxy.example.com:8080').canonical, 'http://proxy.example.com:8080')

    def test_idna_names_become_punycode(self):
        self.assertEqual(s.parse_endpoint('https://пример.рф:443').host, 'xn--e1afmkfd.xn--p1ai')
        self.assertEqual(s.parse_endpoint('https://xn--e1afmkfd.xn--p1ai:443').host,
                         s.parse_endpoint('https://пример.рф:443').host)

    def test_ipv4_is_canonicalized(self):
        endpoint = s.parse_endpoint('http://93.184.216.34:3128')
        self.assertEqual((endpoint.host, endpoint.ip_version), ('93.184.216.34', 4))
        self.assertFalse(endpoint.is_hostname)
        self.assertEqual(endpoint.authority, '93.184.216.34:3128')

    def test_ipv6_keeps_its_brackets_in_the_url(self):
        endpoint = s.parse_endpoint('http://[2606:4700:4700::1111]:8080')
        self.assertEqual((endpoint.host, endpoint.ip_version), ('2606:4700:4700::1111', 6))
        self.assertEqual(endpoint.canonical, 'http://[2606:4700:4700::1111]:8080')
        self.assertEqual(endpoint.authority, '[2606:4700:4700::1111]:8080')

    def test_userinfo_is_refused_without_echoing_the_password(self):
        with self.assertRaises(s.SecretValidationError) as caught:
            s.parse_endpoint('http://alice:%s@proxy.example.com:8080' % CANARY)
        self.assertNotIn(CANARY, caught.exception.message)
        self.assertEqual(caught.exception.code, s.E_VALIDATION)
        self.assertTrue(caught.exception.action)

    def test_split_userinfo_returns_the_bare_address_and_decoded_values(self):
        bare, username, password = s.split_userinfo('http://alice%40corp:p%40ss%2Fword@proxy.example.com:8080')
        self.assertEqual(bare, 'http://proxy.example.com:8080')
        self.assertEqual((username, password), ('alice@corp', 'p@ss/word'))

    def test_split_userinfo_handles_ipv6_and_absent_credentials(self):
        bare, username, password = s.split_userinfo('socks5://[2606:4700:4700::1111]:1080')
        self.assertEqual(bare, 'socks5://[2606:4700:4700::1111]:1080')
        self.assertIsNone(username)
        self.assertIsNone(password)

    def test_split_userinfo_rejects_things_that_are_not_proxy_urls(self):
        for value in ('', '   ', 'proxy.example.com', 'http://proxy.example.com', 'http://proxy.example.com:8080/path',
                      'http://proxy.example.com:8080?a=b', 'http://proxy example.com:8080', None, 42):
            self.assertIsNone(s.split_userinfo(value), value)

    def test_addresses_that_cannot_be_a_proxy_are_refused(self):
        cases = {
            'http://proxy.example.com:99999': s.E_VALIDATION,
            'http://proxy.example.com:0': s.E_VALIDATION,
            'ftp://proxy.example.com:21': s.E_VALIDATION,
            'http://-bad-.example.com:80': s.E_VALIDATION,
            'http://%s/a' % ('x' * 64) + '.example.com:80': s.E_VALIDATION,
            'not a url at all': s.E_VALIDATION,
        }
        for value, code in cases.items():
            with self.assertRaises(s.SecretValidationError) as caught:
                s.parse_endpoint(value)
            self.assertEqual(caught.exception.code, code, value)

    def test_a_url_built_from_a_resolved_access_quotes_the_credentials(self):
        endpoint = s.parse_endpoint('http://proxy.example.com:8080')
        access = s.Access('acc-1', 'ep-1', s.MODE_HTTP_BASIC, 'sec_1', 1, 0.0)
        resolved = s.ResolvedAccess(access, s.MODE_HTTP_BASIC, 'alice@corp', CANARY)
        self.assertEqual(endpoint.url(resolved),
                         'http://alice%%40corp:%s@proxy.example.com:8080' % CANARY)
        self.assertEqual(endpoint.url(), 'http://proxy.example.com:8080')
        self.assertNotIn('@', endpoint.url())


class SharedNormalizerTests(unittest.TestCase):
    """The access path must not become a second proxy normalizer."""

    ADDRESSES = ['11.0.0.1:8080', 'http://11.0.0.1:3128', 'socks4://11.0.0.2:4145', 'socks5://11.0.0.3:1080',
                 'socks5h://11.0.0.4:1080', 'https://11.0.0.5:443', '[2606:4700:4700::1111]:8080',
                 'http://[2606:4700:4700::1111]:3128', 'Proxy.Example.com:8080', 'proxy.example.com.:3128']

    def test_agrees_with_the_shared_normalizer_on_addresses_it_accepts(self):
        for value in self.ADDRESSES:
            with self.subTest(value=value):
                endpoint = s.parse_endpoint(value, normalizer=proxytool.normalize_custom)
                self.assertEqual(endpoint.canonical, proxytool.normalize_custom(value))

    def test_rejects_what_the_shared_normalizer_rejects(self):
        for value in ('11.0.0.1', 'http://11.0.0.1:99999', 'ftp://11.0.0.1:21', 'http://11.0.0.1:8080/x'):
            with self.subTest(value=value):
                with self.assertRaises(s.SecretValidationError):
                    s.parse_endpoint(value, normalizer=proxytool.normalize_custom)

    def test_the_shared_normalizer_only_ever_sees_a_credential_free_address(self):
        seen = []

        def spy(value):
            seen.append(value)
            return proxytool.normalize_custom(value)

        with self.assertRaises(s.SecretValidationError):
            s.parse_endpoint('http://alice:%s@proxy.example.com:8080' % CANARY, normalizer=spy)
        self.assertEqual(seen, [], 'a credentialed address must be refused before normalization')
        s.parse_endpoint('http://proxy.example.com:8080', normalizer=spy)
        self.assertEqual(seen, ['http://proxy.example.com:8080'])
        self.assertNotIn(CANARY, ''.join(seen))


class AuthModeTests(unittest.TestCase):
    def test_scheme_to_mode(self):
        self.assertEqual(s.auth_mode_for('http', username='a', password='b'), s.MODE_HTTP_BASIC)
        self.assertEqual(s.auth_mode_for('https', username='a', password='b'), s.MODE_HTTP_BASIC)
        self.assertEqual(s.auth_mode_for('socks5', username='a', password='b'), s.MODE_SOCKS5)
        self.assertEqual(s.auth_mode_for('socks5h', username='a', password='b'), s.MODE_SOCKS5)
        self.assertEqual(s.auth_mode_for('http'), s.MODE_NONE)
        self.assertEqual(s.auth_mode_for('socks4'), s.MODE_NONE)

    def test_unsupported_mechanisms_are_refused_by_name(self):
        for scheme in ('ntlm', 'kerberos', 'digest', 'smtp'):
            with self.subTest(scheme=scheme):
                with self.assertRaises(s.SecretUnsupportedError):
                    s.auth_mode_for(scheme, username='a', password='b')

    def test_socks4_password_is_refused_with_a_reason(self):
        with self.assertRaises(s.SecretUnsupportedError) as caught:
            s.auth_mode_for('socks4', username='a', password='b')
        self.assertIn('SOCKS4', caught.exception.message)
        self.assertTrue(caught.exception.action)

    def test_mode_must_match_the_scheme(self):
        self.assertEqual(s.check_mode_scheme(s.MODE_HTTP_BASIC, 'https'), s.MODE_HTTP_BASIC)
        with self.assertRaises(s.SecretValidationError):
            s.check_mode_scheme(s.MODE_HTTP_BASIC, 'socks5')
        with self.assertRaises(s.SecretUnsupportedError):
            s.check_mode_scheme('ntlm', 'http')

    def test_requires_auth_separates_missing_credentials_from_wrong_ones(self):
        self.assertTrue(s.requires_auth('http', s.STATE_MISSING))
        self.assertTrue(s.requires_auth('http', s.STATE_LOCKED))
        self.assertFalse(s.requires_auth('http', s.STATE_READY))
        self.assertFalse(s.requires_auth('socks4', s.STATE_MISSING))
        self.assertFalse(s.requires_auth('socks5', s.STATE_NO_REF))


class UpstreamClassificationTests(unittest.TestCase):
    """F04: auth-required, auth-failed, locked vault and no credential stay apart."""

    def test_a_rejected_credential_is_auth_failed(self):
        error = s.classify_upstream(407, state=s.STATE_READY, credentials_sent=True)
        self.assertIsInstance(error, s.SecretAuthFailed)
        self.assertEqual(error.code, s.E_AUTH_FAILED)
        self.assertTrue(error.action)

    def test_a_proxy_that_demands_auth_is_auth_required(self):
        error = s.classify_upstream(407, state=s.STATE_NO_REF, credentials_sent=False)
        self.assertIsInstance(error, s.SecretAuthRequired)
        self.assertEqual(error.code, s.E_AUTH_REQUIRED)

    def test_a_locked_store_stays_a_locked_store(self):
        error = s.classify_upstream(407, state=s.STATE_LOCKED, credentials_sent=True)
        self.assertIsInstance(error, s.SecretVaultLocked)
        self.assertEqual(error.code, s.E_VAULT_LOCKED)

    def test_a_missing_credential_is_not_reported_as_a_rejected_one(self):
        for state in (s.STATE_MISSING, s.STATE_STAGED):
            error = s.classify_upstream(None, state=state)
            self.assertIsInstance(error, s.SecretNotProvided)
            self.assertEqual(error.code, s.E_NOT_PROVIDED)

    def test_a_usable_credential_is_not_a_credential_problem(self):
        self.assertIsNone(s.classify_upstream(200, state=s.STATE_READY, credentials_sent=True))
        self.assertIsNone(s.classify_upstream(502, state=s.STATE_READY))
        self.assertIsNone(s.classify_upstream(None))

    def test_the_detail_travels_without_the_password(self):
        error = s.classify_upstream(407, state=s.STATE_READY, credentials_sent=True,
                                    detail='407 from http://alice:%s@host:8080' % CANARY)
        self.assertIn('407', error.detail)
        self.assertNotIn(CANARY, str(error.describe()))


if __name__ == '__main__':
    unittest.main()

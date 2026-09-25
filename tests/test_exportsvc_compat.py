"""Compatibility preview and the credentials policy.

F28: protocol/auth/DNS/TLS/IPv6 are stated, an unsupported row is explained
instead of silently simplified, and credentials are a separate permitted action
whose default is a redacted reference and never a ready direct-auth URI.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import core, exportsvc as es

NOW = 1_700_000_000.0
CANARY = 'canary-secret-must-not-be-published-2f8a1c'
PUBLIC = dict(proxy='http://203.0.113.7:8080', country='DE', checked_at=NOW, valid_until=NOW + 600)
TLS_ROW = dict(proxy='https://203.0.113.8:443', country='DE', checked_at=NOW, valid_until=NOW + 600)
SOCKS5H = dict(proxy='socks5h://198.51.100.9:1080', country='NL', checked_at=NOW, valid_until=NOW + 600)
HOSTNAME = dict(proxy='socks5://proxy.example.invalid:1080', country='NL', checked_at=NOW,
                valid_until=NOW + 600)
IPV6 = dict(proxy='socks5://[2001:db8::1]:1080', country='FI', checked_at=NOW, valid_until=NOW + 600)
AUTHENTICATED = dict(proxy='http://203.0.113.20:8080', country='US', checked_at=NOW,
                     valid_until=NOW + 600, access_id='acc-7', access_revision=2)
LEAKY = dict(AUTHENTICATED, username='workbench', password=CANARY,
             proxy=f'http://user:{CANARY}@203.0.113.20:8080')


def scope(**overrides):
    base = dict(identity=core.Scope('col-1', 'prof-1', 1))
    base.update(overrides)
    return es.ExportScope(**base)


def reasons_of(report, proxy):
    for item in report.unsupported:
        if item.proxy == proxy:
            return item.reasons
    return None


class ReasonTests(unittest.TestCase):
    def test_a_tls_proxy_is_reported_as_tls_and_not_as_a_wrong_protocol(self):
        report = es.compat_report([TLS_ROW, PUBLIC], 'clash')
        self.assertEqual(reasons_of(report, 'https://203.0.113.8:443'),
                         ('E_EXPORT_TLS_UNSUPPORTED',))
        self.assertEqual(report.supported, ('http://203.0.113.7:8080',))
        self.assertIn('TLS', report.unsupported[0].detail)

    def test_pac_keeps_the_tls_proxy_because_pac_has_an_https_keyword(self):
        report = es.compat_report([TLS_ROW], 'pac')
        self.assertEqual(report.unsupported, ())
        self.assertEqual(report.supported, ('https://203.0.113.8:443',))

    def test_a_plain_list_cannot_carry_credentials(self):
        for fmt in ('txt', 'protocol', 'proxychains'):
            with self.subTest(fmt=fmt):
                report = es.compat_report([AUTHENTICATED], fmt)
                self.assertEqual(reasons_of(report, 'http://203.0.113.20:8080'),
                                 ('E_EXPORT_AUTH_UNSUPPORTED',))
        for fmt in ('json', 'csv', 'clash', 'singbox'):
            with self.subTest(fmt=fmt):
                self.assertEqual(es.compat_report([AUTHENTICATED], fmt).unsupported, ())

    def test_a_hostname_row_is_reported_where_ip_addresses_are_required(self):
        report = es.compat_report([HOSTNAME], 'json')
        self.assertEqual(reasons_of(report, 'socks5://proxy.example.invalid:1080'),
                         ('E_EXPORT_HOSTNAME_UNSUPPORTED',))
        self.assertEqual(es.compat_report([HOSTNAME], 'clash').unsupported, ())

    def test_ipv6_is_carried_by_every_format_that_claims_ip_addresses(self):
        for fmt in ('json', 'csv', 'clash', 'singbox', 'pac', 'proxychains'):
            with self.subTest(fmt=fmt):
                self.assertEqual(es.compat_report([IPV6], fmt).unsupported, ())

    def test_remote_dns_is_a_stated_difference_not_a_silent_downgrade(self):
        report = es.compat_report([SOCKS5H], 'singbox', target='1.12.0')
        self.assertEqual(report.unsupported, ())
        self.assertEqual(report.supported, ('socks5h://198.51.100.9:1080',))
        self.assertEqual(len(report.warnings), 1)
        self.assertIn('DNS', report.warnings[0])

    def test_a_limit_is_reported_as_truncation(self):
        report = es.compat_report([PUBLIC, dict(PUBLIC, proxy='http://203.0.113.8:8080')], 'clash',
                                  options=es.ExportOptions(limits={'clash': 1}))
        self.assertEqual(report.dropped_by_limit, 1)
        self.assertEqual(len(report.supported), 1)
        self.assertTrue(any('1' in warning for warning in report.warnings))

    def test_the_summary_states_when_it_truncated_its_own_list(self):
        rows = [dict(PUBLIC, proxy=f'http://203.0.113.{index}:8080', access_id=f'acc-{index}')
                for index in range(5)]
        report = es.compat_report(rows, 'txt')
        summary = report.as_dict(unsupported_limit=2)
        self.assertEqual(summary['unsupported_total'], 5)
        self.assertEqual(summary['unsupported_shown'], 2)
        self.assertEqual(len(summary['rows']), 2)


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def everything(self):
        return ''.join(path.read_text(encoding='utf-8')
                       for path in sorted(self.home.rglob('*')) if path.is_file())

    def test_the_canary_never_reaches_a_file_by_default(self):
        artifact = es.write_snapshot(self.home, [LEAKY], scope=scope(),
                                     options=es.ExportOptions(published_at=NOW), now=NOW)
        text = self.everything()
        self.assertNotIn(CANARY, text)
        self.assertNotIn('workbench', text)
        rows = json.loads((artifact.directory / 'ranked.json').read_text(encoding='utf-8'))
        self.assertEqual(rows[0]['proxy'], 'http://203.0.113.20:8080')
        self.assertEqual(rows[0]['access_id'], 'acc-7')
        self.assertEqual(rows[0]['access_revision'], 2)
        self.assertNotIn('access_ref', rows[0])
        self.assertEqual(artifact.status.as_dict()['credentials'], 'redact')
        self.assertEqual(json.loads((artifact.directory / 'status.json').read_text(
            encoding='utf-8'))['compat']['credentials'], 'redact')

    def test_credentials_need_an_explicit_grant_for_this_scope(self):
        options = es.ExportOptions(published_at=NOW, credentials='reference')
        with self.assertRaises(es.ExportError) as caught:
            es.write_snapshot(self.home, [LEAKY], scope=scope(), options=options, now=NOW)
        self.assertEqual(caught.exception.code, 'E_EXPORT_CREDENTIALS_NOT_GRANTED')
        with self.assertRaises(es.ExportError) as caught:
            es.write_snapshot(self.home, [LEAKY], scope=scope(),
                              options=options, grant=es.SecretGrant(allowed=False), now=NOW)
        self.assertEqual(caught.exception.code, 'E_EXPORT_CREDENTIALS_NOT_GRANTED')
        with self.assertRaises(es.ExportError) as caught:
            es.write_snapshot(self.home, [LEAKY], scope=scope(), options=options,
                              grant=es.SecretGrant(allowed=True, permission='export.create'), now=NOW)
        self.assertEqual(caught.exception.code, 'E_EXPORT_CREDENTIALS_NOT_GRANTED')
        with self.assertRaises(es.ExportError) as caught:
            es.write_snapshot(self.home, [LEAKY], scope=scope(query='de'), options=options,
                              grant=es.SecretGrant(allowed=True,
                                                   scope_digest=scope().digest()), now=NOW)
        self.assertEqual(caught.exception.code, 'E_AUTH_SCOPE')

    def test_a_granted_export_carries_a_reference_and_not_a_ready_uri(self):
        options = es.ExportOptions(published_at=NOW, credentials='reference')
        artifact = es.write_snapshot(self.home, [LEAKY], scope=scope(), options=options,
                                     grant=es.SecretGrant(allowed=True), now=NOW)
        rows = json.loads((artifact.directory / 'ranked.json').read_text(encoding='utf-8'))
        self.assertEqual(rows[0]['access_ref'], 'access:acc-7@2')
        self.assertEqual(rows[0]['credentials_state'], 'reference')
        self.assertIsNone(rows[0]['direct_auth_uri'])
        self.assertNotIn(CANARY, self.everything())
        clash = (artifact.directory / 'clash.yaml').read_text(encoding='utf-8')
        self.assertNotIn(CANARY, clash)
        self.assertNotIn('user', clash)
        self.assertIn('"type": "http"', clash)

    def test_a_gateway_reference_is_not_passed_off_as_a_direct_auth_uri(self):
        options = es.ExportOptions(published_at=NOW, credentials='reference')
        artifact = es.write_snapshot(self.home, [LEAKY], scope=scope(), options=options,
                                     grant=es.SecretGrant(allowed=True), now=NOW)
        rows = json.loads((artifact.directory / 'ranked.json').read_text(encoding='utf-8'))
        reference = rows[0]['access_ref']
        self.assertEqual(reference, 'access:acc-7@2')
        self.assertIsNone(rows[0]['direct_auth_uri'])
        self.assertEqual(rows[0]['credentials_state'], 'reference')
        # nothing in the artifact can be pasted into a client as an authenticated URL
        import re
        userinfo = re.compile(r'://[^/\s"]*@')
        for path in self.home.rglob('*'):
            if not path.is_file():
                continue
            with self.subTest(name=path.name):
                self.assertIsNone(userinfo.search(path.read_text(encoding='utf-8')))

    def test_no_file_of_any_kind_contains_a_userinfo_section(self):
        es.write_snapshot(self.home, [LEAKY], scope=scope(),
                          options=es.ExportOptions(published_at=NOW), now=NOW)
        for path in self.home.rglob('*'):
            if not path.is_file():
                continue
            text = path.read_text(encoding='utf-8')
            with self.subTest(name=path.name):
                self.assertNotIn(CANARY, text)
                for line in text.splitlines():
                    self.assertNotIn('@203.0.113.20', line)


if __name__ == '__main__':
    unittest.main()

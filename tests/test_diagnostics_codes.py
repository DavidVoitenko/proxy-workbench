"""Codes, help and classification of the diagnostics layer (F10, F25).

The tests below check the behaviour a user and a consumer depend on: a code
stays the same in every language, every code says what to do, and the engine's
exception names are separated into the stage where the failure actually
happened instead of collapsing into one value.
"""
import re
import ssl
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import diagnostics as d
from proxy_workbench.i18n import tr

BARE_CODE = re.compile(r'[A-Z][A-Z0-9_]*\*?$')
E_CODE = re.compile(r'E_[A-Z]+_[A-Z0-9_]+$')


class CodeCatalogueTests(unittest.TestCase):
    def test_every_funnel_stage_has_documented_codes(self):
        for stage in d.FUNNEL_STAGES:
            with self.subTest(stage=stage):
                self.assertTrue(d.codes_for_stage(stage),
                                f'stage {stage} has no documented code')

    def test_every_code_says_what_to_do(self):
        for code, entry in d.CODES.items():
            with self.subTest(code=code):
                self.assertTrue(entry.title_ru.strip())
                self.assertTrue(entry.title_en.strip())
                self.assertTrue(entry.action_ru.strip())
                self.assertTrue(entry.action_en.strip())
                self.assertNotEqual(entry.title_ru, entry.title_en)
                self.assertNotEqual(entry.action_ru, entry.action_en)
                self.assertNotIn(code, entry.action_ru)

    def test_no_error_code_outside_the_contract_canon(self):
        invented = {code for code in d.CODES if code.startswith('E_') and code not in d.CONTRACT_CODES}
        self.assertEqual(invented, set(),
                         'E_* codes must come from CONTRACTS §5.4, not from this module')

    def test_codes_keep_their_documented_shape(self):
        for code in d.CODES:
            with self.subTest(code=code):
                if code.startswith('E_'):
                    self.assertRegex(code, E_CODE)
                else:
                    self.assertRegex(code, BARE_CODE)

    def test_row_codes_stay_bare_as_the_contract_lists_them(self):
        for code in ('UNREACHABLE', 'DNS_TIMEOUT', 'DNS_ERROR', 'NO_DNSBL_ZONES', 'INVALID_PROXY',
                     'BODY_TOO_LARGE', 'CONTENT_MISMATCH', 'HASH_MISMATCH', 'SCREEN_ERROR'):
            with self.subTest(code=code):
                self.assertIn(code, d.CODES)
                self.assertFalse(code.startswith('E_'))

    def test_help_entries_cover_the_whole_catalogue(self):
        entries = d.help_entries()
        self.assertEqual({entry.code for entry in entries}, set(d.CODES))

    def test_unknown_code_gets_honest_help_instead_of_silence(self):
        help_text = d.help_for('E_MADE_UP_CODE')
        self.assertFalse(help_text.known)
        self.assertIn('E_MADE_UP_CODE', help_text.title)
        self.assertTrue(help_text.action)

    def test_unknown_stage_is_rejected(self):
        with self.assertRaises(KeyError):
            d.codes_for_stage('nowhere')


class TranslationSeparationTests(unittest.TestCase):
    def test_the_code_is_the_same_in_both_languages(self):
        for code in d.CODES:
            with self.subTest(code=code):
                ru = d.help_for(code, lang='ru')
                en = d.help_for(code, lang='en')
                self.assertEqual(ru.code, en.code)
                self.assertEqual(ru.stage, en.stage)
                self.assertEqual(ru.domain, en.domain)
                self.assertNotEqual(ru.title, en.title)

    def test_the_default_language_follows_the_terminal_language(self):
        original = d.tr
        d.tr = lambda ru, en: ru
        try:
            self.assertEqual(d.help_for('E_LIMIT_BUDGET').title, d.help_for('E_LIMIT_BUDGET', lang='ru').title)
        finally:
            d.tr = original

    def test_help_uses_the_existing_translation_helper(self):
        entry = d.CODES['E_LIMIT_BUDGET']
        self.assertEqual(entry.title(), tr(entry.title_ru, entry.title_en))

    def test_parameters_are_separate_from_the_text(self):
        entry = d.CODES['E_AUTH_RATE_LIMITED']
        self.assertIn('retry_after_s', entry.params)
        help_text = d.help_for('E_AUTH_RATE_LIMITED', params={'retry_after_s': 30})
        self.assertEqual(help_text.values, {'retry_after_s': 30})
        self.assertIn('retry_after_s=30', help_text.render('en'))

    def test_rendered_help_names_the_code_and_the_action(self):
        text = d.help_for('UNREACHABLE', lang='ru').render('ru')
        self.assertIn('UNREACHABLE', text)
        self.assertIn('Действие:', text)


class ClassificationTests(unittest.TestCase):
    def test_connect_and_read_failures_get_different_stages(self):
        cases = {
            httpx.ConnectTimeout: ('tcp', 'TCP_TIMEOUT'),
            httpx.ReadTimeout: ('target', 'TIMEOUT'),
            httpx.ProxyError: ('handshake', 'HANDSHAKE_ERROR'),
            ssl.SSLError: ('tls', 'TLS_ERROR'),
            ssl.SSLCertVerificationError: ('tls', 'TLS_CERT_INVALID'),
        }
        for exc_type, expected in cases.items():
            with self.subTest(exc=exc_type.__name__):
                self.assertEqual(d.classification(exc_type('boom')), expected)

    def test_a_connect_error_is_tcp_but_a_resolver_failure_is_dns(self):
        plain = httpx.ConnectError('All connection attempts failed')
        resolver = httpx.ConnectError('[Errno 8] nodename nor servname provided, not known')
        self.assertEqual(d.classification(plain), ('tcp', 'UNREACHABLE'))
        self.assertEqual(d.classification(resolver), ('dns', 'DNS_ERROR'))

    def test_stored_row_markers_keep_their_stage(self):
        cases = {
            'UNREACHABLE': ('tcp', 'UNREACHABLE'),
            'TCP_TIMEOUT': ('tcp', 'TCP_TIMEOUT'),
            'HANDSHAKE_ERROR': ('handshake', 'HANDSHAKE_ERROR'),
            'TLS_ERROR': ('tls', 'TLS_ERROR'),
            'TIMEOUT': ('target', 'TIMEOUT'),
            'HTTP_503': ('target', 'HTTP_503'),
            'HTTP_403': ('target', 'HTTP_403'),
            'HTTP_429': ('rate_limit', 'RATE_LIMITED'),
            'HTTP_407': ('auth', 'AUTH_FAILED'),
            'CONTENT_MISMATCH': ('assertion', 'CONTENT_MISMATCH'),
            'HASH_MISMATCH': ('assertion', 'HASH_MISMATCH'),
            'SCREEN_ERROR': ('parser', 'SCREEN_ERROR'),
            'SOURCE_TOO_LARGE': ('download', 'SOURCE_TOO_LARGE'),
            'SOURCE_INVALID_UTF8': ('parser', 'SOURCE_INVALID_UTF8'),
            'SOURCE_CANDIDATE_LIMIT': ('parser', 'SOURCE_CANDIDATE_LIMIT'),
            'SOURCE_REDIRECT_TOO_MANY': ('download', 'SOURCE_REDIRECT_TOO_MANY'),
        }
        for marker, expected in cases.items():
            with self.subTest(marker=marker):
                self.assertEqual(d.classification(marker), expected)

    def test_a_sample_mapping_without_a_code_falls_back_to_its_status(self):
        self.assertEqual(d.classification({'status': 503}), ('target', 'HTTP_503'))
        self.assertEqual(d.classification({'status': 404, 'error': None}), ('target', 'HTTP_404'))
        self.assertEqual(d.classification({'ok': False}), ('', ''))

    def test_no_value_means_no_failure(self):
        self.assertEqual(d.classification(None), ('', ''))
        self.assertEqual(d.classification(''), ('', ''))

    def test_an_unknown_value_is_never_dropped_silently(self):
        stage, code = d.classification('SomethingBrandNew')
        self.assertEqual(code, 'UNKNOWN_FAILURE')
        self.assertTrue(stage)
        self.assertFalse(d.is_known_code(code))
        self.assertFalse(d.is_known_code('SomethingBrandNew'))

    def test_stage_lookup_agrees_with_the_catalogue(self):
        self.assertEqual(d.stage_of('CONTENT_MISMATCH'), 'assertion')
        self.assertEqual(d.code_stage('E_TIME_TTL_EXPIRED'), 'freshness')
        with self.assertRaises(KeyError):
            d.code_stage('NOT_A_CODE')

    def test_help_can_be_built_straight_from_an_exception(self):
        help_text = d.explain_error(httpx.ProxyError('CONNECT failed'), lang='en')
        self.assertEqual(help_text.code, 'HANDSHAKE_ERROR')
        self.assertEqual(help_text.action, d.help_for('HANDSHAKE_ERROR', lang='en').action)


class ActionableErrorTests(unittest.TestCase):
    def test_a_useful_error_carries_an_action_not_only_a_class(self):
        error = d.actionable('E_LIMIT_BUDGET', params={'limit': 0, 'kind': 'requests'})
        self.assertEqual(error.code, 'E_LIMIT_BUDGET')
        self.assertEqual(error.stage, 'budget')
        self.assertEqual(error.action('en'), d.help_for('E_LIMIT_BUDGET', lang='en').action)
        rendered = error.render('en')
        self.assertIn('E_LIMIT_BUDGET', rendered)
        self.assertIn('limit=0', rendered)
        self.assertIn('Action:', rendered)
        self.assertEqual(error.help.values['kind'], 'requests')

    def test_raising_the_error_keeps_the_code_and_the_cause(self):
        cause = httpx.ReadTimeout('slow')
        error = d.ActionableError('TIMEOUT', params={'timeout_s': 1.0}, cause=cause)
        self.assertIsInstance(error, Exception)
        self.assertIs(error.cause, cause)
        self.assertEqual(error.params, {'timeout_s': 1.0})

    def test_an_undocumented_code_cannot_be_raised(self):
        with self.assertRaises(KeyError):
            d.ActionableError('E_NOT_IN_THE_CATALOGUE')


if __name__ == '__main__':
    unittest.main()

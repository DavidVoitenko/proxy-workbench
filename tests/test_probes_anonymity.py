"""Defect 13 / R08: a judge verdict is only earned by a validated echo."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import probes as pr

CLEAN_ECHO = b'{"origin": "203.0.113.9", "headers": {"User-Agent": "ProxyWorkbench/2.2.1", "Accept": "*/*"}}'
LEAKING_ECHO = b'{"origin": "198.51.100.7", "headers": {"User-Agent": "ProxyWorkbench/2.2.1"}}'
HEADER_ECHO = (b'{"origin": "203.0.113.9", "headers": {"Via": "1.1 squid.example", '
               b'"X-Forwarded-For": "192.0.2.44"}}')
CHALLENGE_ECHO = b'<html><head><title>Just a moment...</title></head><body>Please enable JavaScript and cookies</body></html>'
OWN = ('198.51.100.7',)


class EchoTests(unittest.TestCase):
    def test_a_valid_echo_through_a_verified_judge_is_elite(self):
        result = pr.classify_echo(CLEAN_ECHO, OWN)
        self.assertEqual(result.level, 'elite')
        self.assertTrue(result.confirmed)
        self.assertEqual(result.exit_ip, '203.0.113.9')
        self.assertIsNone(result.code)

    def test_our_own_address_in_the_echo_is_transparent(self):
        result = pr.classify_echo(LEAKING_ECHO, OWN)
        self.assertEqual(result.level, 'transparent')
        self.assertEqual(result.signals, ('real_ip',))
        self.assertTrue(result.confirmed)

    def test_a_proxy_header_is_anonymous(self):
        result = pr.classify_echo(HEADER_ECHO, OWN)
        self.assertEqual(result.level, 'anonymous')
        self.assertIn('via', result.signals)
        self.assertIn('x-forwarded-for', result.signals)

    def test_an_empty_answer_is_unknown(self):
        for body in (b'', b'   \n', b''):
            with self.subTest(body=body):
                result = pr.classify_echo(body, OWN)
                self.assertEqual(result.level, 'unknown')
                self.assertEqual(result.code, pr.JUDGE_INVALID)
                self.assertIn('empty_body', result.signals)
                self.assertFalse(result.confirmed)

    def test_an_answer_without_an_address_is_unknown_not_elite(self):
        for body in (b'{"hello": "world"}', b'<html>ok</html>', b'null'):
            with self.subTest(body=body):
                result = pr.classify_echo(body, OWN)
                self.assertEqual(result.level, 'unknown')
                self.assertEqual(result.code, pr.JUDGE_INVALID)
                self.assertNotEqual(result.level, 'elite')

    def test_a_captcha_page_is_unknown(self):
        result = pr.classify_echo(CHALLENGE_ECHO, OWN)
        self.assertEqual(result.level, 'unknown')
        self.assertEqual(result.code, pr.JUDGE_CHALLENGE)
        self.assertIn('challenge_page', result.signals)

    def test_elite_needs_a_verified_judge_and_a_baseline(self):
        self.assertEqual(pr.classify_echo(CLEAN_ECHO, OWN, judge_verified=False).code, pr.JUDGE_UNVERIFIED)
        self.assertEqual(pr.classify_echo(CLEAN_ECHO, OWN, judge_verified=False).level, 'unknown')
        self.assertEqual(pr.classify_echo(CLEAN_ECHO, ()).code, pr.JUDGE_UNVERIFIED)
        self.assertEqual(pr.classify_echo(CLEAN_ECHO, ()).level, 'unknown')

    def test_a_leak_is_still_a_leak_on_an_unverified_judge(self):
        result = pr.classify_echo(LEAKING_ECHO, OWN, judge_verified=False)
        self.assertEqual(result.level, 'transparent')
        self.assertTrue(result.confirmed)

    def test_a_self_hosted_judge_may_echo_a_private_address(self):
        body = b'REMOTE_ADDR=127.0.0.1'
        self.assertIn('127.0.0.1', pr.extract_addresses(body))
        self.assertEqual(pr.classify_echo(body, ('127.0.0.1',)).level, 'transparent')
        self.assertEqual(pr.classify_echo(body, ('10.0.0.1',)).level, 'elite')

    def test_judge_content_type_may_be_required(self):
        spec = pr.validate_judge_spec({'url': 'https://judge.my.invalid/', 'content_type': 'application/json'})
        self.assertEqual(pr.classify_echo(b'nothing here', OWN, spec=spec).code, pr.JUDGE_INVALID)
        relaxed = pr.validate_judge_spec({'url': 'https://judge.my.invalid/', 'require_echo_address': False})
        self.assertEqual(pr.classify_echo(b'nothing here', OWN, spec=relaxed).level, 'elite')

    def test_an_oversized_answer_is_refused(self):
        result = pr.classify_echo(b'x' * 400_000, OWN)
        self.assertEqual(result.level, 'unknown')
        self.assertIn('body_too_large', result.signals)


class JudgeSpecTests(unittest.TestCase):
    def test_a_judge_url_is_validated(self):
        self.assertIsNone(pr.validate_judge_spec(None))
        self.assertIsNone(pr.validate_judge_spec({'url': '  '}))
        self.assertEqual(pr.validate_judge_spec({'url': ' https://judge.invalid/ '}).url, 'https://judge.invalid/')
        for bad in ({'url': 'ftp://judge.invalid/'}, {'url': 'https://user:pw@judge.invalid/'},
                    {'url': 'judge.invalid'}, {'url': 'https://judge.invalid:0/'},
                    {'url': 'https://judge.invalid/', 'max_bytes': 10},
                    {'url': 'https://judge.invalid/', 'max_bytes': 10 ** 9},
                    {'url': 'https://judge.invalid/', 'require_echo_address': 'yes'},
                    {'url': 'https://judge.invalid/', 'timeout': 3},
                    {'url': 'https://judge.invalid/', 'secret': 'canary'}):
            with self.subTest(bad=bad):
                with self.assertRaises(pr.ProbeError):
                    pr.validate_judge_spec(bad)


class RequiredLevelTests(unittest.TestCase):
    def test_any_needs_no_judge(self):
        self.assertIsNone(pr.require_anonymity('any', None))
        self.assertIsNone(pr.require_anonymity(None, None))

    def test_a_required_level_without_a_judge_is_an_explainable_error(self):
        for level in ('transparent', 'anonymous', 'elite'):
            with self.subTest(level=level):
                with self.assertRaises(pr.ProbeError) as caught:
                    pr.require_anonymity(level, None)
                self.assertEqual(caught.exception.code, pr.E_VALIDATION_ANONYMITY_REQUIRED)
                message = str(caught.exception)
                self.assertIn('anonymity.url', message)
                self.assertIn('any', message)

    def test_a_plan_with_a_required_level_and_no_judge_is_refused(self):
        payload = {'mode': 'basic', 'min_anonymity': 'elite'}
        with self.assertRaises(pr.ProbeError) as caught:
            pr.build_plan(payload)
        self.assertEqual(caught.exception.code, pr.E_VALIDATION_ANONYMITY_REQUIRED)
        plan = pr.build_plan({**payload, 'anonymity': {'url': 'https://judge.my.invalid/'}})
        self.assertEqual(plan.min_anonymity, 'elite')
        self.assertEqual(plan.judge.url, 'https://judge.my.invalid/')

    def test_an_unknown_level_is_refused(self):
        with self.assertRaises(pr.ProbeError):
            pr.validate_min_level('super')
        with self.assertRaises(pr.ProbeError):
            pr.build_plan({'mode': 'basic', 'min_anonymity': 'verified'})


class SelectionTests(unittest.TestCase):
    def test_unknown_never_satisfies_a_required_level(self):
        unknown = pr.AnonymityOutcome(level='unknown', code=pr.JUDGE_UNVERIFIED)
        for level in ('transparent', 'anonymous', 'elite'):
            with self.subTest(level=level):
                self.assertFalse(pr.anonymity_allows(unknown, level))
        self.assertTrue(pr.anonymity_allows(unknown, 'any'))

    def test_levels_are_ordered(self):
        for level, minimum, expected in (('elite', 'anonymous', True), ('anonymous', 'elite', False),
                                         ('transparent', 'anonymous', False), ('anonymous', 'anonymous', True),
                                         ('transparent', 'transparent', True)):
            with self.subTest(level=level, minimum=minimum):
                self.assertEqual(pr.anonymity_allows({'level': level}, minimum), expected)

    def test_a_row_carrying_an_unknown_verdict_is_not_admitted(self):
        row = {'anonymity': {'level': 'unknown', 'code': pr.JUDGE_CHALLENGE}}
        self.assertFalse(pr.anonymity_allows(row, 'elite'))
        self.assertTrue(pr.anonymity_allows({'anonymity': {'level': 'elite'}}, 'elite'))
        self.assertFalse(pr.anonymity_allows({}, 'elite'))

    def test_outcome_public_view_has_no_hidden_fields(self):
        value = json.loads(json.dumps(pr.classify_echo(HEADER_ECHO, OWN).to_public()))
        self.assertEqual(value['level'], 'anonymous')
        self.assertNotIn('body', value)
        self.assertNotIn('own_ips', value)


if __name__ == '__main__':
    unittest.main()

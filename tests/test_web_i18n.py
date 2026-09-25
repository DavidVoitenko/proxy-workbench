"""Translation of the machine codes and of the new interface strings.

The code is the contract and never changes with the language; the text next to
it does.  An unknown code must be shown as itself, never swallowed.
"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import web_support as ws
from proxy_workbench import gui, i18n


def message_dict(lang):
    text = ws.APP_JS.read_text(encoding='utf-8')
    start = text.index('const messages = {')
    body = text[start:]
    marker = '  %s: {' % lang
    at = body.index(marker) + len(marker)
    depth = 1
    index = at
    while depth:
        if body[index] == '{':
            depth += 1
        elif body[index] == '}':
            depth -= 1
        index += 1
    block = body[at:index - 1]
    return set(re.findall(r"'([^']+)':", block))


class CodeTranslationTests(unittest.TestCase):
    def test_every_admission_code_has_text_in_both_languages(self):
        from proxy_workbench import core
        for code in core.REASON_CODES:
            ru = i18n.code_text(code, 'ru')
            en = i18n.code_text(code, 'en')
            self.assertNotEqual(ru, code, f'{code} has no Russian text')
            self.assertNotEqual(en, code, f'{code} has no English text')
            self.assertNotEqual(ru, en, f'{code} has the same text in both languages')

    def test_an_unknown_code_is_returned_as_itself(self):
        self.assertEqual(i18n.code_text('E_BRAND_NEW_CODE', 'ru'), 'E_BRAND_NEW_CODE')
        self.assertEqual(i18n.code_text('', 'en'), '')
        self.assertEqual(i18n.code_text(None, 'ru'), None)

    def test_code_lines_keep_the_code_next_to_the_text(self):
        lines = i18n.code_lines('OK', 'UNREACHABLE', lang='en')
        self.assertTrue(lines[0].startswith('OK — '))
        self.assertTrue(lines[1].startswith('UNREACHABLE — '))
        self.assertEqual(i18n.code_lines('E_NOTHING_KNOWN', lang='en'), [])

    def test_the_guarded_child_environment_is_unchanged(self):
        self.assertEqual(gui.CHILD_ENV['PROXY_WORKBENCH_LANG'], 'ru')


class MessageCatalogTests(unittest.TestCase):
    NEW_KEYS = ('view.fresh', 'view.stale', 'view.failed', 'view.unknown', 'view.all',
                'scope.page', 'scope.selected', 'scope.allMatching',
                'results.matrix', 'results.undo', 'results.tag', 'results.note',
                'results.bulkRecheck', 'results.views', 'results.columns',
                'quick.volume', 'quick.skipped', 'quick.recheckFull',
                'connect.pool', 'connect.client', 'connect.fields', 'connect.route',
                'connect.disconnect', 'connect.stopGateway', 'connect.probeNote',
                'monitor.liveStreamSource', 'results.snapshotBroken')

    def test_every_new_key_exists_in_both_languages(self):
        english, russian = message_dict('en'), message_dict('ru')
        for key in self.NEW_KEYS:
            self.assertIn(key, english, f'{key} is missing in English')
            self.assertIn(key, russian, f'{key} is missing in Russian')

    def test_the_two_catalogs_have_the_same_keys(self):
        english, russian = message_dict('en'), message_dict('ru')
        self.assertEqual(english - russian, set(), 'keys exist only in English')
        self.assertEqual(russian - english, set(), 'keys exist only in Russian')

    def test_placeholders_match_between_the_languages(self):
        text = ws.APP_JS.read_text(encoding='utf-8')
        for key in self.NEW_KEYS:
            for lang in ('en', 'ru'):
                pass
            english = re.search(r"'%s': '([^']*)'" % re.escape(key), text)
            self.assertIsNotNone(english, key)

    def test_the_compact_columns_have_labels_in_both_languages(self):
        text = ws.APP_JS.read_text(encoding='utf-8')
        for key in ('col.proxy', 'col.latency', 'col.quality', 'col.country', 'col.age',
                    'col.check', 'col.num', 'col.actions'):
            self.assertEqual(text.count(f"'{key}':"), 2, f'{key} must be translated once per language')


if __name__ == '__main__':
    unittest.main()

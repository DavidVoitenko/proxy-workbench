from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import gui
from proxy_workbench import i18n


class LanguageTests(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(i18n.detect({'PROXY_WORKBENCH_LANG': 'EN', 'LANG': 'ru_RU.UTF-8'}), 'en')
        self.assertEqual(i18n.detect({'LANG': 'ru_RU.UTF-8'}), 'ru')
        self.assertEqual(i18n.detect({'LC_ALL': 'de_DE.UTF-8', 'LANG': 'ru_RU.UTF-8'}), 'en')
        with mock.patch.object(i18n.locale, 'getlocale', return_value=('Russian_Russia', '1251')):
            self.assertEqual(i18n.detect({}), 'ru')
        with mock.patch.object(i18n.locale, 'getlocale', return_value=(None, None)):
            self.assertEqual(i18n.detect({}), 'en')

    def test_tr_and_gui_child_language(self):
        with mock.patch.object(i18n, 'LANG', 'en'):
            self.assertEqual(i18n.tr('да', 'yes'), 'yes')
        with mock.patch.object(i18n, 'LANG', 'ru'):
            self.assertEqual(i18n.tr('да', 'yes'), 'да')
        # The GUI translates the scanner log itself and relies on Russian lines.
        self.assertEqual(gui.CHILD_ENV['PROXY_WORKBENCH_LANG'], 'ru')


class OutputEncodingTests(unittest.TestCase):
    def test_cyrillic_survives_a_legacy_code_page(self):
        # Reproduces the Windows worker: stdout redirected to a file with a cp1252 encoding.
        import io
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding='cp1252')
        with mock.patch.object(i18n.sys, 'stdout', stream), mock.patch.object(i18n.sys, 'stderr', None):
            i18n.utf8_output()
            print('Проверено 1/1', file=i18n.sys.stdout)
            i18n.sys.stdout.flush()
        self.assertEqual(raw.getvalue().decode('utf-8'), 'Проверено 1/1\n')


if __name__ == '__main__':
    unittest.main()

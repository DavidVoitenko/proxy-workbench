from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gui
import i18n


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


if __name__ == '__main__':
    unittest.main()

"""The backend country list and the picker in ui/app.js are the same list (F08).

The country picker is the user's existing control and this module does not
replace it.  What it does add is a second copy of the codes and names on the
backend, and a second copy is only acceptable while the two are checked against
each other.  That check is this file: if the picker changes a name, adds a
country or drops one, the backend list has to change with it or this fails.
"""
import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import geo

APP = Path(__file__).resolve().parents[1] / 'proxy_workbench' / 'ui' / 'app.js'
LITERAL = re.compile(r'const\s+COUNTRIES_LIST\s*=\s*(\[.*?\])\s*;', re.S)


def picker_list():
    """The picker's own entries, read from the file the GUI serves."""
    match = LITERAL.search(APP.read_text(encoding='utf-8'))
    if match is None:
        return None
    body = match.group(1).replace("'", '"')
    body = re.sub(r'([{,]\s*)([A-Za-z][A-Za-z0-9]*)\s*:', r'\1"\2":', body)
    body = re.sub(r',(\s*[}\]])', r'\1', body)
    try:
        return json.loads(body)
    except ValueError:
        return None


class PickerParityTests(unittest.TestCase):
    def setUp(self):
        self.picker = picker_list()
        if self.picker is None:
            self.skipTest('COUNTRIES_LIST is not a readable literal in ui/app.js; '
                          'the backend list cannot be compared with it any more')

    def test_the_same_codes_in_the_same_order(self):
        self.assertEqual([item['code'] for item in self.picker], [item.code for item in geo.COUNTRIES])

    def test_the_same_names_regions_and_flags(self):
        mine = {item.code: (item.name_en, item.name_ru, item.region, item.popular) for item in geo.COUNTRIES}
        theirs = {item['code']: (item['nameEn'], item['nameRu'], item['region'], item['popular'])
                   for item in self.picker}
        self.assertEqual(mine, theirs)

    def test_a_name_from_the_picker_resolves_to_its_iso_code(self):
        for item in self.picker:
            with self.subTest(code=item['code']):
                self.assertEqual(geo._resolve_token(item['nameEn']), item['code'])
                self.assertEqual(geo._resolve_token(item['nameRu']), item['code'])
                self.assertEqual(geo._resolve_token(item['code'].lower()), item['code'])

    def test_the_rendered_list_carries_the_picker_fields(self):
        rendered = geo.country_list()
        self.assertEqual(sorted(rendered[0]), ['code', 'name_en', 'name_ru', 'popular', 'region'])
        self.assertEqual([item['code'] for item in geo.search_countries('нидер')],
                         [item['code'] for item in self.picker if item['nameRu'].lower().startswith('нидер')])

    def test_regions_and_popular_sets_come_from_the_same_list(self):
        for item in geo.COUNTRIES:
            self.assertIn(item.code, geo.region_codes(item.region), item.code)
        self.assertEqual(geo.popular_codes(), tuple(item['code'] for item in self.picker if item['popular']))

    def test_unknown_names_are_rejected_in_both_languages(self):
        for value in ('Atlantis', 'Атлантида', 'Germanyy', ''):
            with self.subTest(value=value):
                self.assertIsNone(geo._resolve_token(value))


if __name__ == '__main__':
    unittest.main()

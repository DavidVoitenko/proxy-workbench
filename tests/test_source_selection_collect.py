import argparse
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import proxytool as p
from proxy_workbench import source_adapters
from proxy_workbench import source_catalog
from proxy_workbench import source_management

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / 'proxy_workbench' / 'sources.json'


def args_for(data, **overrides):
    values = dict(sources=BUNDLED, data=Path(data), no_sources=False)
    values.update(overrides)
    return argparse.Namespace(**values)


class SelectionDrivesCollectionTest(unittest.TestCase):
    """What the user chose is what the collector fetches.

    This is the link the catalog, the sets and the adapters all depend on: a
    selection that the collector ignores would make the whole catalog a
    read-only display.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.catalog = source_catalog.load_bundled()
        quick = [row for row in self.catalog['sets'] if row['id'] == 'quick']
        self.quick = set(quick[0]['members']) if quick else set()
        self.assertTrue(self.quick, 'в каталоге должен быть базовый набор')

    def tearDown(self):
        self.temp.cleanup()

    def _select_quick(self):
        settings = source_management.read_settings(self.data) or {'settings_version': 3}
        source_management.write_settings(self.data, source_management.apply_set(settings, 'quick'))

    def test_a_fresh_install_keeps_the_previous_sources(self):
        # Nobody has chosen anything yet, so the 55 sources the app used before
        # are carried over — now as catalog entries, with their own adapters and
        # provenance instead of bare strings.
        values = p.resolve_collect_sources(args_for(self.data))
        self.assertEqual({value['id'] for value in values},
                         {f'cur-{index:02d}' for index in range(1, 56)})
        for value in values:
            self.assertTrue(value['endpoints'])
            self.assertNotEqual(value['adapter']['kind'], 'unsupported')

    def test_a_selection_resolves_to_the_chosen_catalog_sources(self):
        self._select_quick()
        values = p.resolve_collect_sources(args_for(self.data))
        self.assertTrue(values, 'выбранный набор обязан дать источники')
        ids = {value.get('id') for value in values if isinstance(value, dict)}
        self.assertEqual(ids, self.quick)
        for value in values:
            self.assertTrue(value['endpoints'], 'у источника должен быть endpoint')
            self.assertNotEqual(value['adapter']['kind'], 'unsupported')

    def test_an_explicit_sources_file_wins_over_the_selection(self):
        self._select_quick()  # noqa: F841 - the point is that the file still wins
        own = self.data / 'mine.json'
        own.write_text(json.dumps(['https://example.invalid/list.txt']), encoding='utf-8')
        values = p.resolve_collect_sources(args_for(self.data, sources=own))
        self.assertEqual(values, ['https://example.invalid/list.txt'])

    def test_no_sources_flag_yields_nothing(self):
        self._select_quick()
        self.assertEqual(p.resolve_collect_sources(args_for(self.data, no_sources=True)), [])

    def test_collect_reports_the_selected_sources(self):
        self._select_quick()
        values = p.resolve_collect_sources(args_for(self.data))

        seen = []

        async def fake_rich(db, plans, inputs, timeout, on_progress, denylist, **kwargs):
            seen.extend(plan.get('id') for plan in plans)
            return {'sources': [{'source': index, 'source_id': plan.get('id'),
                                 'rows': 1, 'invalid': 0, 'blocked': 0, 'pages': 1,
                                 'attempts': 1, 'complete': True, 'error': None,
                                 'format': (plan.get('adapter') or {}).get('kind')}
                                for index, plan in enumerate(plans, 1)],
                    'raw_rows': len(plans), 'blocked': 0, 'unique': len(plans)}

        with mock.patch.object(p, '_collect_rich_sources', side_effect=fake_rich):
            report = asyncio.run(p.collect(p.open_db(self.data / 'test.sqlite3'), values, [],
                                           on_progress=None))
        self.assertEqual(set(seen), self.quick)
        self.assertEqual({item['source_id'] for item in report['sources']}, self.quick)


class RecordLimitIsReportedAsPartialTest(unittest.TestCase):
    """A list larger than the cap is read up to the cap, and says so."""

    limits = {'max_records': 10, 'max_bytes': 4 * 1024 * 1024, 'max_depth': 8,
              'max_string': 65536, 'max_columns': 32, 'max_nodes': 200000}

    def test_a_long_json_list_is_truncated_not_refused(self):
        body = json.dumps([{'ip': f'11.0.0.{index}', 'port': 8080} for index in range(50)]).encode()
        profile = {'kind': 'json-records', 'profile': 'ip-port-v1', 'config': {'default_protocol': 'http'}}
        parsed = source_adapters.parse_page(body, profile, {'page': 1, 'url': 'https://x.invalid/'},
                                            self.limits)
        self.assertEqual(len(parsed['records']), 10)
        self.assertTrue(parsed.get('truncated'))
        self.assertEqual(parsed.get('reason'), 'SOURCE_RECORD_LIMIT')
        self.assertEqual(parsed['state'], 'partial')

    def test_a_long_text_list_is_truncated_not_refused(self):
        body = '\n'.join(f'11.0.0.{index}:8080' for index in range(50)).encode()
        profile = {'kind': 'line', 'profile': 'http-v1', 'config': {'default_protocol': 'http'}}
        parsed = source_adapters.parse_page(body, profile, {'page': 1, 'url': 'https://x.invalid/'},
                                            self.limits)
        self.assertEqual(len(parsed['records']), 10)
        self.assertTrue(parsed.get('truncated'))

    def test_a_list_under_the_cap_is_complete(self):
        body = json.dumps([{'ip': f'11.0.0.{index}', 'port': 8080} for index in range(3)]).encode()
        profile = {'kind': 'json-records', 'profile': 'ip-port-v1', 'config': {'default_protocol': 'http'}}
        parsed = source_adapters.parse_page(body, profile, {'page': 1, 'url': 'https://x.invalid/'},
                                            self.limits)
        self.assertEqual(parsed['state'], 'complete')
        self.assertIsNone(parsed.get('truncated'))


if __name__ == '__main__':
    unittest.main()

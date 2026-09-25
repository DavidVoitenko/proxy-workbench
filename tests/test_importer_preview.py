"""Preview: formats, line numbers, counters, and the promise that it writes nothing."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import importer as imp
from tests.test_importer_fixtures import Schema, dump, source


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema()
        self.addCleanup(self.schema.close)
        self.db = self.schema.conn
        self.collection = imp.create_collection(self.db, 'Мои прокси')

    def show(self, plan):
        return [(row.line, row.state, row.reason) for row in plan.rows]

    def test_card_fixture_counts_and_line_numbers(self):
        # 04-feature-cards.ru.md, F03: 10 valid, 2 duplicates, 3 invalid.
        lines = [f'11.0.0.{index}:8080' for index in range(1, 11)]
        lines += ['11.0.0.1:8080', '11.0.0.5:8080']
        lines += ['broken', '11.0.0.11:0', 'my.host:3128']
        plan = imp.preview(self.db, source('\n'.join(lines)), collection_id=self.collection)
        self.assertEqual(plan.counts['valid'], 10)
        self.assertEqual(plan.counts['duplicates'], 2)
        self.assertEqual(plan.counts['rejected'], 3)
        rejected = [(row.line, row.reason) for row in plan.rejected]
        self.assertEqual(rejected, [(13, imp.CODE_FORMAT), (14, imp.CODE_FORMAT),
                                    (15, imp.ROW_HOSTNAME)])
        duplicates = [(row.line, row.duplicate_of) for row in plan.duplicates]
        self.assertEqual(duplicates, [(11, 1), (12, 5)])

    def test_comment_and_blank_lines_are_skipped_not_rejected(self):
        plan = imp.preview(self.db, source('# header\n\n11.0.0.1:8080\n\n# tail\n'),
                           collection_id=self.collection)
        self.assertEqual([row.line for row in plan.rows], [3])
        self.assertEqual(plan.counts['skipped'], 4)

    def test_preview_writes_nothing(self):
        imp.commit(self.db, imp.preview(self.db, source('11.0.0.1:8080\n'), collection_id=self.collection))
        before, changes = dump(self.db), self.db.total_changes
        plan = imp.preview(self.db, source('11.0.0.2:8080\nbad\n', name='second.txt'),
                           collection_id=self.collection, mode='replace')
        self.assertEqual(plan.counts['valid'], 1)
        self.assertEqual(dump(self.db), before)
        self.assertEqual(self.db.total_changes, changes)
        self.assertFalse(self.db.in_transaction, 'preview must not leave a transaction open')

    def test_txt_and_uri_reach_the_same_addresses(self):
        txt = imp.preview(self.db, source('11.0.0.1:8080\n11.0.0.2\n'),
                          collection_id=self.collection)
        uri = imp.preview(self.db, source('http://11.0.0.1:8080\n11.0.0.2\n', name='u.txt'),
                          collection_id=self.collection, fmt='uri')
        self.assertEqual([row.canonical for row in txt.valid], ['http://11.0.0.1:8080'])
        self.assertEqual(txt.rejected[0].reason, imp.CODE_FORMAT)
        self.assertEqual([row.canonical for row in uri.valid], ['http://11.0.0.1:8080'])
        self.assertEqual(uri.rejected[0].reason, imp.CODE_FORMAT, 'a bare address is not a URI line')

    def test_csv_header_is_mapped_and_country_is_kept(self):
        text = ('ip,port,protocol,country\n'
                '11.0.0.1,8080,socks5,de\n'
                '11.0.0.2,3128,http,\n'
                '11.0.0.3,,http,\n')
        plan = imp.preview(self.db, source(text, name='list.csv'), collection_id=self.collection)
        self.assertEqual(plan.format, 'csv')
        self.assertEqual(plan.mapping.to_dict(),
                         {'host': 'ip', 'port': 'port', 'scheme': 'protocol', 'country': 'country'})
        self.assertEqual([row.canonical for row in plan.valid],
                         ['socks5://11.0.0.1:8080', 'http://11.0.0.2:3128'])
        self.assertEqual(plan.valid[0].country, 'DE')
        self.assertEqual([(row.line, row.reason) for row in plan.rejected], [(4, imp.ROW_MISSING_FIELD)])

    def test_csv_without_header_uses_positions(self):
        plan = imp.preview(self.db, source('11.0.0.1,8080\n11.0.0.2,3128\n', name='p.csv'),
                           collection_id=self.collection, fmt='csv')
        self.assertEqual([row.canonical for row in plan.valid],
                         ['http://11.0.0.1:8080', 'http://11.0.0.2:3128'])

    def test_semicolon_and_tab_delimiters(self):
        for separator in (';', '\t'):
            text = separator.join(('host', 'port')) + '\n' + separator.join(('11.0.0.1', '8080')) + '\n'
            plan = imp.preview(self.db, source(text, name='s.csv'), collection_id=self.collection)
            self.assertEqual([row.canonical for row in plan.valid], ['http://11.0.0.1:8080'],
                             f'delimiter {separator!r}')

    def test_json_list_of_objects_and_of_strings(self):
        objects = json.dumps({'proxies': [{'ip': '11.0.0.1', 'port': 1080, 'protocol': 'socks5'},
                                          {'ip': '11.0.0.2', 'port': 8080}]})
        plan = imp.preview(self.db, source(objects, name='o.json'), collection_id=self.collection)
        self.assertEqual(plan.format, 'json')
        self.assertEqual([(row.line, row.canonical) for row in plan.valid],
                         [(1, 'socks5://11.0.0.1:1080'), (2, 'http://11.0.0.2:8080')])
        strings = json.dumps(['11.0.0.3:8080', 'socks5://11.0.0.4:1080'])
        plan = imp.preview(self.db, source(strings, name='s.json'), collection_id=self.collection)
        self.assertEqual([(row.line, row.canonical) for row in plan.valid],
                         [(1, 'http://11.0.0.3:8080'), (2, 'socks5://11.0.0.4:1080')])

    def test_json_needs_mapping_when_roles_are_missing(self):
        plan = imp.preview(self.db, source(json.dumps([{'host': '11.0.0.1', 'note': 'x'}]),
                                           name='j.json'), collection_id=self.collection)
        self.assertTrue(plan.needs_mapping)
        self.assertEqual(plan.mapping_problem[0], ('port',))
        self.assertEqual(plan.mapping_suggestion.found, {'host': 'host'})
        with self.assertRaises(imp.ImportFormatError) as caught:
            imp.commit(self.db, plan)
        self.assertEqual(caught.exception.code, imp.CODE_FORMAT)
        self.assertIn('suggestion', caught.exception.detail)

    def test_csv_ambiguous_column_is_not_guessed(self):
        plan = imp.preview(self.db, source('host,ip,port\n11.0.0.1,11.0.0.2,8080\n', name='a.csv'),
                           collection_id=self.collection)
        self.assertTrue(plan.needs_mapping)
        self.assertEqual(plan.mapping_suggestion.ambiguous, ('host',))
        decided = imp.preview(self.db, source('host,ip,port\n11.0.0.1,11.0.0.2,8080\n', name='a.csv'),
                              collection_id=self.collection,
                              mapping=imp.ColumnMapping(host='ip', port='port'))
        self.assertEqual([row.canonical for row in decided.valid], ['http://11.0.0.2:8080'])

    def test_mapping_naming_a_missing_column_is_refused(self):
        with self.assertRaises(imp.ImportFormatError) as caught:
            imp.preview(self.db, source('ip,port\n11.0.0.1,8080\n', name='m.csv'),
                        collection_id=self.collection,
                        mapping=imp.ColumnMapping(host='ip', port='нет'))
        self.assertEqual(caught.exception.code, imp.CODE_FORMAT)
        self.assertIn('нет', str(caught.exception))

    def test_unknown_collection_and_unknown_format_are_refused(self):
        with self.assertRaises(imp.UnknownCollection) as unknown:
            imp.preview(self.db, source('11.0.0.1:8080\n'), collection_id='нет-такой')
        self.assertEqual(unknown.exception.code, imp.CODE_NO_COLLECTION)
        with self.assertRaises(imp.ImportFormatError) as unknown_format:
            imp.preview(self.db, source('11.0.0.1:8080\n'), collection_id=self.collection, fmt='yaml')
        self.assertEqual(unknown_format.exception.code, imp.CODE_FORMAT)
        with self.assertRaises(imp.ImportProblem) as bad_mode:
            imp.preview(self.db, source('11.0.0.1:8080\n'), collection_id=self.collection, mode='upsert')
        self.assertEqual(bad_mode.exception.code, imp.CODE_FIELD)

    def test_format_detection(self):
        cases = {'11.0.0.1:8080\n11.0.0.2:8080\n': 'txt',
                 'socks5://11.0.0.1:1080\n': 'uri',
                 'ip,port\n11.0.0.1,8080\n': 'csv',
                 'host\tport\n11.0.0.1\t8080\n': 'csv',
                 '["11.0.0.1:8080"]': 'json',
                 '{"proxies": []}': 'json'}
        for text, expected in cases.items():
            self.assertEqual(imp.detect_format(source(text)), expected, text)

    def test_file_drag_and_clipboard_reach_the_same_plan(self):
        text = '11.0.0.1:8080\nbad\n11.0.0.1:8080\n'
        dropped = imp.ImportSource.from_drop(text.encode(), name='dropped.txt')
        plans = [imp.preview(self.db, imp.ImportSource.from_text(text), collection_id=self.collection),
                 imp.preview(self.db, dropped, collection_id=self.collection),
                 imp.preview(self.db, imp.ImportSource.from_text(text, name='dropped.txt',
                                                                 channel='drop'),
                             collection_id=self.collection)]
        counts = [plan.counts for plan in plans]
        self.assertEqual(counts[0], counts[1])
        self.assertEqual(counts[1], counts[2])
        self.assertEqual({plan.source_digest for plan in plans},
                         {plans[0].source_digest}, 'same bytes, same digest')

    def test_utf8_bom_and_crlf_are_read(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bom.txt'
            path.write_bytes('﻿11.0.0.1:8080\r\n11.0.0.2:8080\r\n'.encode())
            plan = imp.preview(self.db, imp.ImportSource.from_path(path), collection_id=self.collection)
        self.assertEqual([row.canonical for row in plan.valid],
                         ['http://11.0.0.1:8080', 'http://11.0.0.2:8080'])
        self.assertEqual([row.line for row in plan.valid], [1, 2])

    def test_non_utf8_file_is_named_not_guessed(self):
        with self.assertRaises(imp.ImportEncodingError) as caught:
            imp.ImportSource.from_bytes(b'11.0.0.1:8080\n\xff\xfe\x00broken', name='cp1251.txt')
        self.assertEqual(caught.exception.code, imp.CODE_ENCODING)
        self.assertIn('cp1251.txt', str(caught.exception))

    def test_oversized_import_is_refused(self):
        with self.assertRaises(imp.ImportTooLarge) as caught:
            imp.ImportSource.from_text('x' * (imp.MAX_IMPORT_BYTES + 1))
        self.assertEqual(caught.exception.code, imp.CODE_SIZE)

    def test_suggest_mapping_reports_ambiguity(self):
        suggestion = imp.suggest_mapping(['IP Address', 'Port Number', 'Country Code'])
        self.assertEqual(suggestion.found['host'], 'IP Address')
        self.assertEqual(suggestion.found['port'], 'Port Number')
        self.assertEqual(suggestion.found['country'], 'Country Code')
        self.assertEqual(imp.suggest_mapping(['host', 'ip', 'port']).ambiguous, ('host',))
        self.assertEqual(imp.suggest_mapping(['Server', 'Port']).found['host'], 'Server')
        self.assertEqual(imp.suggest_mapping(['alpha', 'beta']).missing, ('host', 'port'))

    def test_preview_dict_is_json_serializable(self):
        plan = imp.preview(self.db, source('11.0.0.1:8080\nbad\n'), collection_id=self.collection)
        data = json.loads(json.dumps(plan.to_dict(), ensure_ascii=False))
        self.assertEqual(data['counts']['rejected'], 1)
        self.assertEqual(data['rows'][1]['line'], 2)
        self.assertEqual(data['state'], 'preview')


if __name__ == '__main__':
    unittest.main()

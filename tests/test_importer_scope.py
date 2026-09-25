"""Defect 10 / R06: one endpoint decision for every input path and format.

The rule under test: a hostname, a private address and credentials are either
supported end to end or refused at once, and the refusal is the same code in
TXT, URI, CSV, JSON, in a file, in a drop and in the paste buffer.  Accepting
such a row and losing it later in the collector is the defect itself.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import importer as imp
from tests.test_importer_fixtures import Schema, members, source

#: one shape per transport, all carrying the same four rows
HOSTNAME = 'my.proxy.example:3128'
PRIVATE = '10.0.0.7:8080'
CREDENTIALS = 'http://user:secret@11.0.0.1:8080'
BROKEN = 'not a proxy at all'

SHAPES = {
    'txt': '\n'.join([HOSTNAME, PRIVATE, CREDENTIALS, BROKEN, '11.0.0.1:8080']),
    # the same addresses, each written in the syntax its format requires
    'uri': '\n'.join([f'http://{HOSTNAME}', f'http://{PRIVATE}', CREDENTIALS, BROKEN,
                      'http://11.0.0.1:8080']),
    'csv': 'host,port\n' + '\n'.join([f'{HOSTNAME.split(":")[0]},{HOSTNAME.split(":")[1]}',
                                       '10.0.0.7,8080', CREDENTIALS, f'{BROKEN},0',
                                       '11.0.0.1,8080']),
    'json': json.dumps([HOSTNAME, PRIVATE, CREDENTIALS, BROKEN, '11.0.0.1:8080']),
}

EXPECTED = {
    'host': imp.ROW_HOSTNAME,
    'private': imp.ROW_PRIVATE,
    'credentials': imp.ROW_CREDENTIALS,
    'broken': imp.CODE_FORMAT,
}


class UniformRefusalTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema()
        self.addCleanup(self.schema.close)
        self.db = self.schema.conn
        self.collection = imp.create_collection(self.db, 'Мои прокси')

    def plan(self, text, collection=None, **kwargs):
        kwargs.setdefault('fmt', 'txt')
        return imp.preview(self.db, source(text, name='list.' + kwargs['fmt']),
                           collection_id=collection or self.collection, **kwargs)

    def own_collection(self, name):
        return imp.create_collection(self.db, name)

    def test_every_format_gives_the_same_reason_per_row(self):
        for fmt, text in SHAPES.items():
            plan = self.plan(text, collection=self.own_collection(f'Формат {fmt}'), fmt=fmt)
            self.assertEqual(sorted(plan.reasons), sorted(EXPECTED.values()), fmt)
            # only the plain public address survives, in every transport
            self.assertEqual([row.canonical for row in plan.valid], ['http://11.0.0.1:8080'], fmt)
            self.assertEqual(plan.counts['valid'], 1, fmt)

    def test_every_channel_gives_the_same_result(self):
        text = SHAPES['txt']
        plans = {
            'clipboard': imp.ImportSource.from_text(text, name='list.txt', channel='clipboard'),
            'drop-bytes': imp.ImportSource.from_drop(text.encode(), name='list.txt'),
            'drop-path': imp.ImportSource.from_drop(None, path=self.written(text)),
        }
        for name, src in plans.items():
            plan = imp.preview(self.db, src, collection_id=self.collection)
            self.assertEqual(plan.counts['rejected'], 4, name)
            self.assertEqual(plan.counts['valid'], 1, name)
            self.assertEqual(plan.reasons, dict(sorted(
                {imp.ROW_HOSTNAME: 1, imp.ROW_PRIVATE: 1,
                 imp.ROW_CREDENTIALS: 1, imp.CODE_FORMAT: 1}.items())), name)

    def written(self, text):
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'dropped.txt'
        path.write_text(text)
        return path

    def test_a_refused_row_is_never_written(self):
        for fmt, text in SHAPES.items():
            collection = self.own_collection(f'Запись {fmt}')
            report = imp.commit(self.db, self.plan(text, collection=collection, fmt=fmt),
                                allow_partial=True)
            self.assertEqual(report.added, ('http://11.0.0.1:8080',), fmt)
            self.assertEqual(members(self.db, collection), ['http://11.0.0.1:8080'], fmt)
        stored = '\n'.join(str(row) for row in self.db.execute('SELECT canonical FROM endpoints'))
        for forbidden in ('my.proxy.example', '10.0.0.7', 'user'):
            self.assertNotIn(forbidden, stored)

    def test_private_and_hostname_are_off_by_default_and_auditable_when_on(self):
        text = f'{PRIVATE}\n{HOSTNAME}\n11.0.0.1:8080\n'
        refused = self.plan(text)
        self.assertEqual([row.reason for row in refused.rejected],
                         [imp.ROW_PRIVATE, imp.ROW_HOSTNAME])
        accepted = self.plan(text, policy=imp.EndpointPolicy(public_only=False))
        self.assertEqual([row.canonical for row in accepted.valid],
                         ['http://10.0.0.7:8080', 'http://my.proxy.example:3128',
                          'http://11.0.0.1:8080'])
        self.assertFalse(accepted.to_dict()['policy']['public_only'])
        report = imp.commit(self.db, accepted)
        self.assertFalse(report.public_only)
        self.assertEqual(report.counts['added'], 3)

    def test_credentials_column_refuses_the_row_instead_of_dropping_the_field(self):
        text = 'ip,port,username,password\n11.0.0.1,8080,bob,secret\n11.0.0.2,8080,,\n'
        plan = self.plan(text, fmt='csv')
        self.assertEqual([(row.line, row.reason) for row in plan.rows],
                         [(2, imp.ROW_CREDENTIALS), (3, imp.ROW_CREDENTIALS)])
        objects = json.dumps([{'ip': '11.0.0.1', 'port': 8080, 'password': 'secret'}])
        plan = self.plan(objects, fmt='json')
        self.assertEqual([row.reason for row in plan.rejected], [imp.ROW_CREDENTIALS])

    def test_a_file_of_only_unsupported_rows_commits_nothing(self):
        report = imp.commit(self.db, self.plan(f'{HOSTNAME}\n{PRIVATE}\n{CREDENTIALS}\n'),
                            allow_partial=True)
        self.assertEqual(report.counts['added'], 0)
        self.assertEqual(members(self.db, self.collection), [])
        self.assertEqual(report.counts['rejected'], 3)


class SecretHygieneTests(unittest.TestCase):
    """F03: passwords must not reach raw input, logs or provenance on disk."""

    CANARY = 'CanaryPwd-4f2a9c'

    def setUp(self):
        self.schema = Schema()
        self.addCleanup(self.schema.close)
        self.db = self.schema.conn
        self.collection = imp.create_collection(self.db, 'Мои прокси')

    def test_canary_is_nowhere_after_a_full_import(self):
        texts = {
            'uri': f'http://user:{self.CANARY}@11.0.0.1:8080\n11.0.0.2:8080\n',
            'csv': f'ip,port,username,password\n11.0.0.3,8080,bob,{self.CANARY}\n11.0.0.4,8080,,\n',
            'json': json.dumps([{'ip': '11.0.0.5', 'port': 8080, 'password': self.CANARY}]),
            'txt': f'11.0.0.6:8080 {self.CANARY}\n11.0.0.7:8080\n',
        }
        for fmt, text in texts.items():
            plan = imp.preview(self.db, imp.ImportSource.from_text(text, name=f'{fmt}.txt'),
                               collection_id=self.collection, fmt=fmt)
            report = imp.commit(self.db, plan, allow_partial=True)
            self.assertNotIn(self.CANARY, report.to_json(), fmt)
            self.assertNotIn(self.CANARY, json.dumps(plan.to_dict(), ensure_ascii=False), fmt)
            for row in plan.rows:
                self.assertNotIn(self.CANARY, row.sample, f'{fmt} line {row.line}')
        stored = '\n'.join(str(value) for row in self.db.execute(
            'SELECT * FROM import_batch') for value in row)
        self.assertNotIn(self.CANARY, stored)

    def test_canary_is_absent_from_the_database_file_and_its_wal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            schema = Schema(Path(folder))
            self.addCleanup(schema.close)
            db, path = schema.conn, schema.path
            collection = imp.create_collection(db, 'Мои прокси')
            plan = imp.preview(db, imp.ImportSource.from_text(
                f'http://user:{self.CANARY}@11.0.0.1:8080\n11.0.0.2:8080\n', name='u.txt'),
                collection_id=collection)
            imp.commit(db, plan, allow_partial=True)
            db.close()
            for candidate in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
                if candidate.exists():
                    self.assertNotIn(self.CANARY.encode(), candidate.read_bytes(), candidate.name)

    def test_import_writes_no_file_of_its_own(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            drop = Path(folder) / 'supplied.txt'
            drop.write_text(f'http://user:{self.CANARY}@11.0.0.1:8080\n11.0.0.2:8080\n')
            plan = imp.preview(self.db, imp.ImportSource.from_path(drop), collection_id=self.collection)
            imp.commit(self.db, plan, allow_partial=True)
            self.assertEqual(sorted(path.name for path in Path(folder).iterdir()), ['supplied.txt'])

    def test_import_writes_no_log_line_with_the_secret(self):
        import logging
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        plan = imp.preview(self.db, imp.ImportSource.from_text(
            f'http://user:{self.CANARY}@11.0.0.1:8080\nbroken\n', name='u.txt'),
            collection_id=self.collection)
        imp.commit(self.db, plan, allow_partial=True)
        self.assertNotIn(self.CANARY, '\n'.join(records))
        self.assertNotIn(self.CANARY, str([record for record in records]))

    def test_source_repr_never_shows_the_text(self):
        src = imp.ImportSource.from_text(f'http://user:{self.CANARY}@11.0.0.1:8080')
        self.assertNotIn(self.CANARY, repr(src))
        self.assertNotIn(self.CANARY, str(src.args) if getattr(src, 'args', None) else '')
        self.assertNotIn(self.CANARY, f'{src}')
        self.assertNotIn(self.CANARY, src.digest)
        self.assertIn('ImportSource', repr(src))

    def test_redact_masks_userinfo_query_and_trailing_text(self):
        self.assertEqual(imp.redact(f'http://user:{self.CANARY}@11.0.0.1:8080'),
                         'http://***@11.0.0.1:8080')
        self.assertEqual(imp.redact(f'11.0.0.1:8080?token={self.CANARY}'), '11.0.0.1:8080 …')
        self.assertEqual(imp.redact(f'11.0.0.1:8080 {self.CANARY}'), '11.0.0.1:8080 …')
        self.assertEqual(imp.redact('11.0.0.1:8080'), '11.0.0.1:8080')
        self.assertEqual(imp.redact(''), '')

    def test_import_cannot_reach_a_shell(self):
        # no process, no shell: the import reads bytes and writes SQL, nothing else
        code = Path(imp.__file__).read_text()
        for forbidden in ('subprocess', 'os.system', 'Popen', 'shell=True', 'eval('):
            self.assertNotIn(forbidden, code, forbidden)


if __name__ == '__main__':
    unittest.main()

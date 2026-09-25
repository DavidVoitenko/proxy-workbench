"""Commit: merge/replace scope, cancellation, revision conflict, idempotency, report."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import importer as imp
from tests.test_importer_fixtures import Schema, members, source


class CommitTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema()
        self.addCleanup(self.schema.close)
        self.db = self.schema.conn
        self.first = imp.create_collection(self.db, 'Первая')
        self.second = imp.create_collection(self.db, 'Вторая')

    def plan(self, text, collection=None, **kwargs):
        return imp.preview(self.db, source(text, name=kwargs.pop('name', 'list.txt')),
                           collection_id=collection or self.first, **kwargs)

    def test_merge_adds_membership_and_provenance(self):
        report = imp.commit(self.db, self.plan('11.0.0.1:8080\nsocks5://11.0.0.2:1080\n'))
        self.assertEqual(report.state, 'committed')
        self.assertEqual(members(self.db, self.first),
                         ['http://11.0.0.1:8080', 'socks5://11.0.0.2:1080'])
        self.assertEqual(report.counts['added'], 2)
        origins = {row[0] for row in self.db.execute('SELECT origin FROM membership')}
        self.assertEqual(origins, {imp.ORIGIN_IMPORT})
        row = tuple(self.db.execute('SELECT host, port, scheme, ip_version, first_seen_at, last_seen_at '
                                     'FROM endpoints WHERE canonical = ?',
                                     ('socks5://11.0.0.2:1080',)).fetchone())
        self.assertEqual(row[:4], ('11.0.0.2', 1080, 'socks5', 4))
        self.assertIsNotNone(row[4])

    def test_merge_touches_only_the_chosen_collection(self):
        imp.commit(self.db, self.plan('11.0.0.1:8080\n', collection=self.first))
        imp.commit(self.db, self.plan('11.0.0.1:8080\n11.0.0.2:8080\n', collection=self.second))
        self.assertEqual(members(self.db, self.first), ['http://11.0.0.1:8080'])
        self.assertEqual(members(self.db, self.second),
                         ['http://11.0.0.1:8080', 'http://11.0.0.2:8080'])
        report = imp.commit(self.db, self.plan('11.0.0.3:8080\n', collection=self.first))
        self.assertEqual(report.counts['removed'], 0, 'merge removes nothing')

    def test_replace_shows_a_diff_and_replaces_only_its_collection(self):
        imp.commit(self.db, self.plan('11.0.0.1:8080\n11.0.0.2:8080\n11.0.0.3:8080\n',
                                      collection=self.first))
        imp.commit(self.db, self.plan('11.0.0.1:8080\n11.0.0.9:8080\n', collection=self.second))
        plan = self.plan('11.0.0.1:8080\n11.0.0.5:8080\n', mode='replace')
        self.assertEqual(plan.added, ('http://11.0.0.5:8080',))
        self.assertEqual(plan.unchanged, ('http://11.0.0.1:8080',))
        self.assertEqual(plan.removed, ('http://11.0.0.2:8080', 'http://11.0.0.3:8080'))
        report = imp.commit(self.db, plan)
        self.assertEqual(members(self.db, self.first),
                         ['http://11.0.0.1:8080', 'http://11.0.0.5:8080'])
        self.assertEqual(members(self.db, self.second),
                         ['http://11.0.0.1:8080', 'http://11.0.0.9:8080'])

    def test_replace_keeps_members_the_file_lists_again(self):
        imp.commit(self.db, self.plan('11.0.0.1:8080\n11.0.0.2:8080\n'))
        plan = self.plan('11.0.0.1:8080\n11.0.0.5:8080\n', mode='replace')
        self.assertEqual([row.reason for row in plan.duplicates], [imp.ROW_ALREADY_MEMBER])
        imp.commit(self.db, plan)
        self.assertEqual(members(self.db, self.first),
                         ['http://11.0.0.1:8080', 'http://11.0.0.5:8080'])

    def test_replace_that_would_empty_the_collection_is_refused(self):
        imp.commit(self.db, self.plan('11.0.0.1:8080\n'))
        plan = self.plan('my.host:3128\n', mode='replace')
        with self.assertRaises(imp.ImportProblem) as caught:
            imp.commit(self.db, plan)
        self.assertEqual(caught.exception.code, imp.CODE_EMPTY)
        self.assertEqual(members(self.db, self.first), ['http://11.0.0.1:8080'])
        report = imp.commit(self.db, plan, allow_empty=True, allow_partial=True)
        self.assertEqual(report.counts['removed'], 1)
        self.assertEqual(members(self.db, self.first), [])

    def test_cancellation_leaves_no_half_replace(self):
        imp.commit(self.db, self.plan('11.0.0.1:8080\n11.0.0.2:8080\n'))
        before = members(self.db, self.first)
        plan = self.plan('\n'.join(f'11.1.0.{index}:8080' for index in range(1, 9)), mode='replace')
        seen = []

        def cancel_after_four():
            seen.append(1)
            return len(seen) > 4

        with self.assertRaises(imp.ImportCancelled) as caught:
            imp.commit(self.db, plan, should_cancel=cancel_after_four)
        self.assertEqual(caught.exception.code, imp.CODE_CANCELLED)
        self.assertEqual(members(self.db, self.first), before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM import_batch WHERE state = "cancelled"')
                         .fetchone()[0], 1)
        # and the same batch can still be committed afterwards
        report = imp.commit(self.db, plan)
        self.assertEqual(report.state, 'committed')
        self.assertEqual(len(members(self.db, self.first)), 8)

    def test_crash_mid_commit_rolls_back_everything(self):
        imp.commit(self.db, self.plan('11.0.0.1:8080\n11.0.0.2:8080\n'))
        before = members(self.db, self.first)
        plan = self.plan('\n'.join(f'11.2.0.{index}:8080' for index in range(1, 6)), mode='replace')
        written = []

        def crash(phase, done, total):
            if phase == 'endpoints' and done == 3:
                written.append(done)
                raise RuntimeError('процесс упал')

        with self.assertRaises(RuntimeError):
            imp.commit(self.db, plan, on_progress=crash)
        self.assertEqual(written, [3], 'the crash happened inside the write loop')
        self.assertEqual(members(self.db, self.first), before)
        self.assertEqual(self.db.execute('SELECT state FROM import_batch ORDER BY created_at DESC')
                         .fetchone()[0], 'failed')

    def test_stale_preview_is_refused_with_the_revision(self):
        stale = self.plan('11.0.0.7:8080\n')
        self.assertEqual(stale.collection_revision, 0)
        imp.commit(self.db, self.plan('11.0.0.8:8080\n'))
        with self.assertRaises(imp.ImportRevisionConflict) as caught:
            imp.commit(self.db, stale)
        self.assertEqual(caught.exception.code, imp.CODE_REVISION)
        self.assertEqual(caught.exception.detail['expected'], 0)
        self.assertEqual(caught.exception.detail['current'], 1)
        self.assertEqual(members(self.db, self.first), ['http://11.0.0.8:8080'])
        fresh = self.plan('11.0.0.7:8080\n')
        self.assertEqual(fresh.collection_revision, 1)
        imp.commit(self.db, fresh)
        self.assertEqual(imp.collection_revision(self.db, self.first), 2)

    def test_membership_changed_behind_our_back_is_refused(self):
        imp.commit(self.db, self.plan('11.0.0.1:8080\n11.0.0.2:8080\n'))
        plan = self.plan('11.0.0.1:8080\n11.0.0.5:8080\n', mode='replace')
        self.db.execute('INSERT OR IGNORE INTO membership(collection_id, endpoint_id, added_at, origin)'
                        ' SELECT ?, id, 0, "manual" FROM endpoints WHERE canonical = ?',
                        (self.first, 'http://11.0.0.2:8080'))
        self.db.execute('INSERT OR IGNORE INTO endpoints(id, canonical, host, port, scheme)'
                        " VALUES ('manual', 'http://11.0.0.42:8080', '11.0.0.42', 8080, 'http')")
        self.db.execute('INSERT OR IGNORE INTO membership(collection_id, endpoint_id, added_at, origin)'
                        " VALUES (?, 'manual', 0, 'manual')", (self.first,))
        self.db.commit()
        with self.assertRaises(imp.ImportRevisionConflict) as caught:
            imp.commit(self.db, plan)
        self.assertEqual(caught.exception.code, imp.CODE_REVISION)
        self.assertIn('http://11.0.0.42:8080', members(self.db, self.first))

    def test_repeated_commit_does_not_duplicate(self):
        plan = self.plan('11.0.0.1:8080\n11.0.0.2:8080\n')
        first = imp.commit(self.db, plan)
        second = imp.commit(self.db, plan)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(second.batch_id, first.batch_id)
        self.assertEqual(len(members(self.db, self.first)), 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM membership').fetchone()[0], 2)
        self.assertEqual(imp.collection_revision(self.db, self.first), 1)

    def test_explicit_idempotency_key_groups_two_previews(self):
        first = imp.preview(self.db, source('11.0.0.1:8080\n'), collection_id=self.first,
                            idempotency_key='supply-42')
        imp.commit(self.db, first)
        second = imp.preview(self.db, source('11.0.0.2:8080\n', name='later.txt'),
                             collection_id=self.first, idempotency_key='supply-42')
        self.assertEqual(second.batch_id, first.batch_id)
        report = imp.commit(self.db, second)
        self.assertTrue(report.replayed)
        self.assertEqual(members(self.db, self.first), ['http://11.0.0.1:8080'])
        again = imp.preview(self.db, source('11.0.0.1:8080\n11.0.0.2:8080\n', name='both.txt'),
                            collection_id=self.first)
        imp.commit(self.db, again)
        self.assertEqual(len(members(self.db, self.first)), 2)

    def test_partial_file_needs_an_explicit_decision(self):
        plan = self.plan('11.0.0.1:8080\nbroken\n')
        with self.assertRaises(imp.ImportPartialBlocked) as caught:
            imp.commit(self.db, plan)
        self.assertEqual(caught.exception.code, imp.CODE_PARTIAL)
        self.assertEqual(caught.exception.detail['reasons'], {imp.CODE_FORMAT: 1})
        self.assertEqual(members(self.db, self.first), [])
        report = imp.commit(self.db, plan, allow_partial=True)
        self.assertEqual(report.counts['added'], 1)
        self.assertEqual(report.counts['rejected'], 1)

    def test_a_partially_bad_file_can_be_fixed_and_imported_again(self):
        broken = self.plan('11.0.0.1:8080\n11.0.0.2:70000\nbroken\n')
        self.assertEqual([(row.line, row.reason) for row in broken.rejected],
                         [(2, imp.CODE_FORMAT), (3, imp.CODE_FORMAT)])
        with self.assertRaises(imp.ImportPartialBlocked):
            imp.commit(self.db, broken)
        self.assertEqual(members(self.db, self.first), [])
        # the same file with the two bad lines repaired
        fixed = self.plan('11.0.0.1:8080\n11.0.0.2:8080\n11.0.0.3:8080\n', name='fixed.txt')
        self.assertEqual(fixed.counts['rejected'], 0)
        report = imp.commit(self.db, fixed)
        self.assertEqual(report.counts['added'], 3)
        self.assertEqual(report.counts['rejected'], 0)
        self.assertEqual(len(members(self.db, self.first)), 3)

    def test_partial_import_keeps_the_rejected_lines_for_the_report(self):
        plan = self.plan('11.0.0.1:8080\nmy.host:3128\n10.0.0.1:8080\n')
        report = imp.commit(self.db, plan, allow_partial=True)
        self.assertEqual(report.reasons, {imp.ROW_HOSTNAME: 1, imp.ROW_PRIVATE: 1})
        self.assertEqual([item['line'] for item in report.rejected], [2, 3])
        self.assertEqual(members(self.db, self.first), ['http://11.0.0.1:8080'])

    def test_busy_collection_is_deferred(self):
        plan = self.plan('11.0.0.1:8080\n')
        with self.assertRaises(imp.ImportBusy) as caught:
            imp.commit(self.db, plan, busy=lambda: 'идёт проверка')
        self.assertEqual(caught.exception.code, imp.CODE_BUSY)
        self.assertEqual(members(self.db, self.first), [])
        report = imp.commit(self.db, plan, busy=lambda: None)
        self.assertEqual(report.state, 'committed')

    def test_commit_inside_a_foreign_transaction_is_refused(self):
        plan = self.plan('11.0.0.1:8080\n')
        self.db.execute('BEGIN IMMEDIATE')
        self.addCleanup(self.db.rollback)
        with self.assertRaises(imp.ImportBusy) as caught:
            imp.commit(self.db, plan)
        self.assertEqual(caught.exception.code, imp.CODE_BUSY)

    def test_report_is_stored_and_readable_back(self):
        plan = self.plan('11.0.0.1:8080\nbad\nhttp://u:p@11.0.0.3:80\n')
        report = imp.commit(self.db, plan, allow_partial=True)
        stored = imp.load_report(self.db, report.batch_id)
        self.assertEqual(stored.batch_id, report.batch_id)
        self.assertEqual(stored.state, 'committed')
        self.assertEqual(stored.revision_after, 1)
        self.assertEqual([item['line'] for item in stored.rejected], [2, 3])
        self.assertEqual(stored.counts['added'], 1)
        row = tuple(self.db.execute('SELECT state, revision, collection_id FROM import_batch').fetchone())
        self.assertEqual(row, ('committed', 1, self.first))
        json.loads(stored.to_json())

    def test_ipv6_and_endpoint_columns(self):
        report = imp.commit(self.db, self.plan('[2606:4700:4700::1111]:8443\n'))
        self.assertEqual(report.added, ('http://[2606:4700:4700::1111]:8443',))
        self.assertEqual(tuple(self.db.execute('SELECT host, ip_version FROM endpoints').fetchone()),
                         ('2606:4700:4700::1111', 6))

    def test_socks4_over_ipv6_is_refused_by_the_shared_parser(self):
        plan = self.plan('socks4://[2606:4700:4700::1111]:1080\n')
        self.assertEqual([(row.state, row.reason) for row in plan.rows],
                         [('rejected', imp.CODE_FORMAT)])

    def test_private_policy_accepts_hostnames_and_private_addresses(self):
        plan = self.plan('10.0.0.1:8080\nmy.host:3128\n',
                         policy=imp.EndpointPolicy(public_only=False))
        self.assertEqual([row.canonical for row in plan.valid],
                         ['http://10.0.0.1:8080', 'http://my.host:3128'])
        report = imp.commit(self.db, plan)
        self.assertFalse(report.public_only)
        self.assertEqual([tuple(row) for row in
                          self.db.execute('SELECT ip_version FROM endpoints ORDER BY host')],
                         [(4,), (None,)])


if __name__ == '__main__':
    unittest.main()

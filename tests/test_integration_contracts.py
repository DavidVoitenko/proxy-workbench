"""Contracts the integration pass added, kept as tests of their own.

Every case here corresponds to a defect that was reproduced before the fix and
would reproduce again if the fix were reverted.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from proxy_workbench import (api, apikeys, apiv1, core, db as store, importer, jobs,
                             proxytool, scheduler, source_catalog)
from proxy_workbench import geo as geo_module


def _workbench(home):
    workbench = proxytool.Workbench(home)
    return workbench


class OneShotSecretTests(unittest.TestCase):
    """CONTRACTS §5.1: a full secret is shown once, and an idempotent replay is
    not a second issuance."""

    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        store.migrate(self.home)
        self.manager = apikeys.ApiKeyManager(store.connect(self.home))
        self.admin = self.manager.bootstrap_admin(
            local_trusted=True, permissions=sorted(apiv1.PERMISSIONS))
        service = api.WorkbenchService(self.home, exports=api.Exports(self.home / 'exports'),
                                      key_store=self.manager)
        self.control = apiv1.ApiV1(service, apiv1.ApiKeyStore(self.manager))

    def post(self, path, body, idem):
        return self.control.handle(apiv1.Request(
            'POST', path,
            headers={'Host': '127.0.0.1', 'Authorization': f'Bearer {self.admin.secret}',
                     'Idempotency-Key': idem, 'Content-Type': 'application/json'},
            body=json.dumps(body).encode()))

    def test_a_replay_of_the_same_issue_returns_the_same_key_without_the_secret(self):
        payload = {'name': 'reader', 'purpose': 'app', 'permissions': ['read.results']}
        first = self.post('/v1/keys', payload, 'one-shot').json()
        again = self.post('/v1/keys', payload, 'one-shot').json()
        self.assertTrue(first['secret'].startswith(apikeys.SECRET_MARK))
        self.assertNotIn('secret', again, 'the secret is shown exactly once')
        self.assertTrue(again.get('secret_already_shown'),
                        'an answer without a field reads as "this key has no secret"')
        # Idempotency itself is untouched: same key, same status, one row.
        self.assertEqual(first['id'], again['id'])
        rows = self.manager.conn.execute(
            "SELECT count(*) FROM api_keys WHERE name='reader'").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_a_second_key_under_another_idempotency_key_is_a_second_issuance(self):
        payload = {'name': 'other', 'purpose': 'app', 'permissions': ['read.results']}
        first = self.post('/v1/keys', payload, 'k1').json()
        second = self.post('/v1/keys', payload, 'k2').json()
        self.assertNotEqual(first['id'], second['id'])
        self.assertTrue(second['secret'].startswith(apikeys.SECRET_MARK),
                        'a different request is a new key and gets its own secret')


class RetryScopeTests(unittest.TestCase):
    """Defect D: a retry must not re-measure an endpoint that left the scope."""

    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        store.migrate(self.home)
        conn = store.connect(self.home)
        proxytool.ensure_public_collection(conn)
        conn.execute("INSERT OR IGNORE INTO collections(id, name, kind, created_at)"
                     " VALUES ('col-a','A','private',0)")
        self.endpoints = [f'http://11.0.0.{n}:8080' for n in (1, 2, 3)]
        for value in self.endpoints:
            identifier = store.upsert_endpoint(conn, value)
            store.add_member(conn, 'col-a', identifier, origin='import')
        conn.commit()
        self.workbench = _workbench(self.home)
        self.addCleanup(self.workbench.close)
        self.store = self.workbench.jobs()
        job = self.store.submit('scan', jobs.Scope(collection_id='col-a', profile_id='p',
                                                   profile_revision=1),
                                 [jobs.QueueItem(endpoint_id=store.endpoint_id(value),
                                                 access_id='public', access_revision=1)
                                  for value in self.endpoints])
        self.job = job
        self.store.start(job.id)
        for item in self.store.items(job.id):
            self.store.claim(job.id, item_id=item.item_id)
        for item in self.store.items(job.id):
            if item.endpoint_id == store.endpoint_id(self.endpoints[0]):
                self.store.finish_item(job.id, item.item_id, 'done', observation_id='obs-1')
            elif item.endpoint_id == store.endpoint_id(self.endpoints[1]):
                self.store.finish_item(job.id, item.item_id, 'blocked',
                                       error_code=jobs.CODE_OBSOLETE_MEMBERSHIP)
        conn.execute('DELETE FROM membership WHERE collection_id=? AND endpoint_id=?',
                     ('col-a', store.endpoint_id(self.endpoints[1])))
        conn.commit()

    def test_retry_never_queues_an_item_that_left_the_collection(self):
        self.assertFalse(hasattr(jobs.JobStore, 'RETRY_SKIPS_BLOCKED'),
                         'the module keeps its own default; the caller decides')
        pending = [item.item_id for item in self.store.items(self.job.id)
                   if item.state not in ('done', 'blocked')]
        retried = self.store.retry(self.job.id, items=pending)
        queued = {item.endpoint_id for item in self.store.items(retried.id)}
        self.assertNotIn(store.endpoint_id(self.endpoints[1]), queued)
        self.assertIn(store.endpoint_id(self.endpoints[2]), queued)

    def test_retrying_only_blocked_items_is_refused_with_the_operation_to_use(self):
        manager = self.workbench.keys()
        admin = manager.bootstrap_admin(local_trusted=True, permissions=sorted(apiv1.PERMISSIONS))
        service = api.WorkbenchService(self.home, exports=api.Exports(self.home / 'exports'),
                                      key_store=manager)
        control = apiv1.ApiV1(service, apiv1.ApiKeyStore(manager))
        # Finish the last unfinished item too, so the only one left is blocked.
        for item in self.store.items(self.job.id):
            if item.state in ('probing', 'pending', 'prefiltered'):
                self.store.finish_item(self.job.id, item.item_id, 'done',
                                       observation_id='obs-2')
        answer = control.handle(apiv1.Request(
            'POST', f'/v1/jobs/{self.job.id}/retry',
            headers={'Host': '127.0.0.1', 'Authorization': f'Bearer {admin.secret}',
                     'Idempotency-Key': 'r1', 'Content-Type': 'application/json'},
            body=b'{}'))
        self.assertEqual(answer.status_code, 409, answer.body)
        self.assertEqual(answer.json()['error']['code'], 'E_CONFLICT_REVISION')
        self.assertTrue(answer.json()['error']['details']['blocked_only'])


class CountryParityTests(unittest.TestCase):
    """F08: the export engine and the admission contract answer the same question."""

    COUNTRIES = {'de.example': 'DE', 'nl.example': 'NL', 'exit-de.example': 'DE'}
    NOW = 2000.0

    def base_row(self, **overrides):
        row = dict(checked_at=1000.0, valid_until=99999.0, collection_id='c', profile_id='pr',
                   profile_revision=1, network_id='net', access_id='acc', access_revision=1,
                   reliability=1.0, min_target_reliability=1.0, successes=1, requests=1,
                   score=1.0, protocol='http', reputation={'status': 'clean'})
        row.update(overrides)
        return row

    def admit(self, row, **kwargs):
        lookup = self.COUNTRIES.get
        policy = core.Policy(country_of=lookup, exit_country_of=lookup, **kwargs)
        return core.admit(row, core.Scope('c', 'pr', 1, 'net'), core.Access('acc', 1),
                          policy, self.NOW)

    def test_basis_exit_keeps_a_german_endpoint_wanted_for_a_dutch_exit(self):
        row = self.base_row(proxy='http://de.example:81', country='DE', exit_ip='nl.example')
        criterion = proxytool.country_criterion(['NL'], basis='exit')
        self.assertTrue(self.admit(row, country_criterion=criterion).admitted)
        self.assertTrue(proxytool.matches_selection(
            row, countries=('NL',), country_of=self.COUNTRIES.get,
            criterion=criterion, exit_country_of=self.COUNTRIES.get))
        # The same two surfaces agree on the row that must NOT be published.
        other = self.base_row(proxy='http://de.example:81', country='DE')
        self.assertFalse(self.admit(other, country_criterion=criterion).admitted)
        self.assertFalse(proxytool.matches_selection(
            other, countries=('NL',), country_of=self.COUNTRIES.get,
            criterion=criterion, exit_country_of=self.COUNTRIES.get))

    def test_an_exclusion_applies_whatever_the_basis(self):
        criterion = proxytool.country_criterion(['NL'], ['DE'], basis='exit')
        row = self.base_row(proxy='http://de.example:81', country='DE', exit_ip='nl.example')
        self.assertFalse(self.admit(row, country_criterion=criterion).admitted,
                         'geo.evaluate applies exclude to every known country, exit included')

    def test_the_unknown_policy_is_the_criterion_s_own(self):
        row = self.base_row(proxy='http://de.example:81')
        self.assertFalse(self.admit(
            row, country_criterion=proxytool.country_criterion(['NL'])).admitted)
        self.assertTrue(self.admit(
            row, country_criterion=proxytool.country_criterion(
                ['NL'], unknown='include_unverified')).admitted)

    def test_the_legacy_countries_argument_still_works(self):
        self.assertTrue(self.admit(self.base_row(proxy='http://de.example:81', country='DE'),
                                   countries=frozenset({'DE'})).admitted)
        self.assertFalse(self.admit(self.base_row(proxy='http://de.example:81'),
                                    countries=frozenset({'NL'})).admitted)

    def test_the_engine_builds_the_criterion_module_own_object(self):
        built = proxytool.country_criterion(['NL'], ['DE'], 'exit', 'include_unverified')
        self.assertIsInstance(built, geo_module.CountryCriterion)
        self.assertEqual(built.digest(), geo_module.CountryCriterion(
            include=('NL',), exclude=('DE',), basis='exit', unknown='include_unverified').digest())


class ImportPolicyTests(unittest.TestCase):
    """Defect 10: the same explicit choice on every path, and no secret on disk."""

    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        store.migrate(self.home)
        self.conn = store.connect(self.home)
        proxytool.ensure_public_collection(self.conn)
        self.conn.execute("INSERT OR IGNORE INTO collections(id, name, kind, created_at)"
                          " VALUES ('mine','Mine','private',0)")
        self.conn.commit()

    def preview(self, text, policy):
        source = importer.ImportSource.from_text(text, name='t', channel='clipboard')
        return importer.preview(self.conn, source, collection_id='mine', policy=policy)

    def test_the_default_refuses_hostname_private_and_credentials(self):
        plan = self.preview('http://localhost:8080\nhttp://198.51.100.7:8080\n'
                            'http://user:pw@203.0.113.9:3128\n', importer.DEFAULT_POLICY)
        self.assertEqual([row.reason for row in plan.rows],
                         ['E_IMPORT_HOSTNAME', 'E_IMPORT_PRIVATE', 'E_IMPORT_CREDENTIALS'])

    def test_a_private_collection_accepts_what_it_explicitly_asked_for(self):
        plan = self.preview('http://localhost:8080\n', importer.EndpointPolicy(public_only=False))
        self.assertEqual(plan.rows[0].state, 'valid')

    def test_credentials_go_to_the_secret_store_and_not_into_the_address(self):
        policy = importer.DestinationPolicy(public_only=False, credentials='store')
        plan = self.preview('http://user:pw@203.0.113.9:3128\n', policy)
        self.assertEqual(plan.rows[0].state, 'valid')
        self.assertNotIn('pw', json.dumps(plan.to_dict()), 'a preview never carries the value')
        report = importer.commit(self.conn, plan, allow_partial=True)
        self.assertEqual(report.to_dict()['counts'].get('credentials_stored'), 1)
        stored = self.conn.execute('SELECT canonical FROM endpoints').fetchall()
        self.assertEqual([row[0] for row in stored], ['http://203.0.113.9:3128'])
        accesses = self.conn.execute('SELECT endpoint_id, mode, secret_ref FROM accesses').fetchall()
        self.assertEqual(len(accesses), 1)
        self.assertTrue(accesses[0][2], 'the access points at a vault reference')
        on_disk = json.dumps([dict(row) for row in
                              self.conn.execute('SELECT * FROM import_batch')])
        self.assertNotIn('pw', on_disk)

    def test_the_policy_rejects_a_credential_mode_it_does_not_have(self):
        with self.assertRaises(importer.ImportProblem):
            importer.DestinationPolicy(credentials='guess')


class SchedulePauseTests(unittest.TestCase):
    """F15: a pause and a spent budget survive a restart *and* a spec edit."""

    def test_both_doors(self):
        import dataclasses
        home = pathlib.Path(tempfile.mkdtemp())
        store.migrate(home)
        conn = store.connect(home)
        self.addCleanup(conn.close)
        engine = scheduler.Scheduler(store=scheduler.SqliteScheduleStore(conn))
        engine.add({'id': 'nightly', 'kind': 'interval', 'interval_minutes': 30,
                    'timezone': 'UTC',
                    'budgets': {'requests': 3, 'reset': 'daily', 'timezone': 'UTC'}})
        state = engine.state('nightly')
        state.counters = scheduler.BudgetCounters.from_dict({'requests': 3})
        engine.store.save_state('nightly', state)
        engine.pause('nightly')
        self.assertEqual(engine.persistence()['lost_on_restart'], [])
        spec = next(item for item in engine.list() if item.id == 'nightly')
        engine.store.save_spec(dataclasses.replace(spec, interval_minutes=60))
        self.assertTrue(engine.state('nightly').paused)
        self.assertEqual(engine.state('nightly').counters.requests, 3)


class EndpointColumnCacheTests(unittest.TestCase):
    """Item 12: the column set is read once, and the answer is the same."""

    def test_the_cached_writer_writes_exactly_what_the_module_writes(self):
        home = pathlib.Path(tempfile.mkdtemp())
        store.migrate(home)
        one = store.connect(home)
        two = store.connect(home)
        self.addCleanup(one.close)
        self.addCleanup(two.close)
        values = ['http://11.0.0.%d:8080' % n for n in range(1, 40)]
        expected = {store.upsert_endpoint(one, value, country='NL') for value in values}
        actual = {proxytool.upsert_endpoint(two, value, country='NL') for value in values}
        self.assertEqual(actual, expected)
        self.assertEqual(
            [row[0] for row in one.execute('SELECT canonical FROM endpoints ORDER BY canonical')],
            [row[0] for row in two.execute('SELECT canonical FROM endpoints ORDER BY canonical')])
        self.assertEqual(
            [row[0] for row in one.execute('SELECT country FROM endpoints ORDER BY canonical')],
            [row[0] for row in two.execute('SELECT country FROM endpoints ORDER BY canonical')])

    def test_a_column_that_does_not_exist_is_still_ignored(self):
        home = pathlib.Path(tempfile.mkdtemp())
        store.migrate(home)
        conn = store.connect(home)
        self.addCleanup(conn.close)
        proxytool.upsert_endpoint(conn, 'http://11.0.0.1:8080', not_a_column='x')
        self.assertTrue(conn.execute(
            'SELECT 1 FROM endpoints WHERE canonical=?', ('http://11.0.0.1:8080',)).fetchone())


if __name__ == '__main__':
    unittest.main()

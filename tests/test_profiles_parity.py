"""One profile, one verdict in GUI, CLI and API - and interchange without secrets.

The surfaces differ only in how they build the document: the typed object the
GUI holds, the dictionary the CLI assembles from its arguments, the JSON body the
API parses.  All of them end in `evaluate()` / `run_request()`, which is what this
file proves.  The interchange half checks that a document can leave and re-enter
the library without carrying a credential and without losing the revision chain.
"""
from pathlib import Path
import argparse
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import profiles  # noqa: E402
from tests import profiles_support as support  # noqa: E402

START = support.START

#: A value that must never reach the database, a document, a log line or argv.
#: Nothing in this module ever handles a real credential; this one only has to
#: prove that credential-shaped data is refused and never written.
CANARY = 'canary-pw-4f81c0'


def fingerprint_of(verdict):
    return json.dumps(verdict, sort_keys=True, ensure_ascii=False)


def gui_document(spec):
    """What a form sends: the profile as an object, evidence as an object per target."""
    return {'profile': spec.as_dict(),
            'evidence': {item.target_id: item.as_dict() for item in evidence_of(spec)}}


def cli_namespace(spec):
    """What a CLI parser produces for the same profile, as `argparse` hands it over."""
    return argparse.Namespace(
        targets=[dict(id=item.id, kind=item.kind, enabled=item.enabled,
                      min_success=item.min_success, max_latency_ms=item.max_latency_ms)
                 for item in spec.targets],
        optional_rule=spec.optional_rule.mode,
        k=spec.optional_rule.k,
        attempts=spec.attempts,
        max_probes=spec.budget.max_probes,
        max_duration_s=spec.budget.max_duration_s,
        description=spec.description)


def cli_document(spec):
    args = cli_namespace(spec)
    return {'profile': {'version': profiles.CONFIG_VERSION,
                        'targets': args.targets,
                        'optional_rule': args.optional_rule,
                        'k': args.k,
                        'attempts': args.attempts,
                        'budget': {'max_probes': args.max_probes,
                                   'max_duration_s': args.max_duration_s},
                        'description': args.description},
            'evidence': [item.as_dict() for item in evidence_of(spec)]}


def api_document(spec):
    """What a JSON body parses into, after a round trip through the wire format."""
    return json.loads(json.dumps(gui_document(spec), ensure_ascii=False))


def evidence_of(spec):
    """One working and one partly working check, stamped for this revision."""
    return [support.ok(spec, support.BASIC, latency_ms=120.0),
            support.mixed(spec, support.SEARCH, 1, 2)]


class ParityTests(unittest.TestCase):
    def setUp(self):
        self.spec = support.spec(optional_rule='any', k=1, attempts=2,
                                 budget={'max_probes': 8},
                                 targets=[support.target(support.BASIC),
                                          support.target(support.SEARCH),
                                          support.target(support.VIDEO, 'optional')])

    def test_three_surfaces_produce_the_very_same_verdict(self):
        verdicts = [
            profiles.run_request(gui_document(self.spec)),
            profiles.run_request(cli_document(self.spec)),
            profiles.run_request(api_document(self.spec)),
            profiles.run_request({'profile': self.spec, 'evidence': evidence_of(self.spec)}),
        ]
        self.assertEqual(len({fingerprint_of(item) for item in verdicts}), 1, 'surfaces disagreed')
        self.assertFalse(verdicts[0]['pass'])
        self.assertEqual(verdicts[0]['reason'], 'E_VERDICT_REQUIRED_NOT_PASSED')
        # ``run_request`` is ``evaluate`` plus the dry-run plan, nothing else.
        without_plan = {key: value for key, value in verdicts[0].items() if key != 'profile'}
        self.assertEqual(fingerprint_of(without_plan),
                         fingerprint_of(profiles.evaluate(self.spec, evidence_of(self.spec))))
        self.assertEqual(verdicts[0]['profile'], self.spec.plan())

    def test_a_passing_profile_is_also_identical_everywhere(self):
        good = support.spec(targets=[support.target(support.BASIC),
                                    support.target(support.SEARCH)])
        documents = [{'profile': good.as_dict(),
                      'evidence': [support.ok(good, support.BASIC),
                                   support.ok(good, support.SEARCH)]},
                     json.loads(json.dumps({'profile': good.as_dict(),
                                            'evidence': [support.ok(good, support.BASIC).as_dict(),
                                                         support.ok(good, support.SEARCH).as_dict()]}))]
        verdicts = [profiles.run_request(document) for document in documents]
        self.assertTrue(all(item['pass'] for item in verdicts))
        self.assertEqual(len({fingerprint_of(item) for item in verdicts}), 1)

    def test_a_profile_addressed_by_id_gives_the_same_verdict_as_its_document(self):
        good = support.spec()
        evidence = [support.ok(good, support.BASIC), support.ok(good, support.SEARCH)]
        by_document = profiles.run_request({'profile': good.as_dict(), 'evidence': evidence})
        self.assertTrue(by_document['pass'])
        self.assertEqual(by_document['profile']['digest'], good.digest)

    def test_an_unusable_request_is_refused_the_same_way_for_every_surface(self):
        for document in ({'profile': 'API', 'evidence': []},
                         {'profile': support.spec().as_dict(), 'evidence': 'ok'},
                         {'profile_id': 'p_0000000000000000', 'evidence': []},
                         {'profile': support.spec().as_dict(), 'evidence': [{'target': 'x'}]},
                         {'profile': support.spec().as_dict(), 'evidence': [{'target': support.BASIC,
                                                                           'ok': 1, 'weight': 2}]}):
            with self.subTest(document=document):
                with self.assertRaises(profiles.ProfileError) as raised:
                    profiles.run_request(document)
                self.assertTrue(raised.exception.code.startswith('E_'))

    def test_the_public_record_is_one_shape(self):
        """GUI, CLI and API get the same fields, so no surface invents its own row."""
        self.assertEqual(
            {'profile_id', 'name', 'profile_revision', 'parent_id', 'digest', 'created_at',
             'archived_at', 'archived', 'is_default', 'config'},
            set(self._record().as_dict()))
        with_plan = self._record().as_dict(include_plan=True)
        self.assertEqual(set(with_plan) - set(self._record().as_dict()), {'plan'})

    def test_the_plan_travels_with_the_verdict(self):
        verdict = profiles.run_request(gui_document(self.spec))
        self.assertEqual(verdict['profile']['required'], [support.BASIC, support.SEARCH])
        self.assertEqual(verdict['profile']['optional'], [support.VIDEO])
        self.assertEqual(verdict['profile']['min_probes'], 6)
        self.assertEqual(verdict['profile']['budget'], {'max_probes': 8, 'max_duration_s': None})
        self.assertEqual(json.loads(json.dumps(verdict)), verdict)

    def _record(self):
        import tempfile

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = profiles.open_store(Path(temp.name) / 'parity.sqlite3')
        self.addCleanup(store.conn.close)
        ref = store.create('API', self.spec, at=START)
        return store.get(ref.profile_id)


class StoreParityTests(support.StoreFixture):
    def test_a_stored_profile_runs_identically_by_document_and_by_id(self):
        spec = support.spec(attempts=2,
                            targets=[support.target(support.BASIC, min_success=0.5),
                                     support.target(support.SEARCH, min_success=0.5)])
        ref = self.store.create('API', spec, at=START)
        evidence = [support.mixed(spec, support.BASIC, 2, 2), support.mixed(spec, support.SEARCH, 1, 2)]
        by_id = profiles.run_request({'profile_id': ref.profile_id,
                                      'profile_revision': 1,
                                      'evidence': [item.as_dict() for item in evidence]},
                                     store=self.store)
        by_document = profiles.run_request({'profile': spec.as_dict(), 'evidence': evidence})
        self.assertEqual(fingerprint_of(by_id), fingerprint_of(by_document))
        self.assertTrue(by_id['pass'])

    def test_a_stored_profile_runs_the_same_for_an_explicit_revision(self):
        spec = support.spec()
        ref = self.store.create('API', spec, at=START)
        relaxed_spec = spec.copy_of(targets=[support.target(support.BASIC, min_success=0.5),
                                             support.target(support.SEARCH)])
        self.store.update(ref.profile_id, relaxed_spec, base_revision=1, at=START + 1)
        evidence = [support.ok(relaxed_spec, support.BASIC), support.ok(relaxed_spec, support.SEARCH)]
        head = profiles.run_request({'profile_id': ref.profile_id, 'evidence': evidence},
                                    store=self.store)
        pinned = profiles.run_request({'profile_id': ref.profile_id, 'profile_revision': 2,
                                       'evidence': evidence}, store=self.store)
        self.assertEqual(fingerprint_of(head), fingerprint_of(pinned))
        self.assertEqual(pinned['profile']['digest'], relaxed_spec.digest)
        with self.assertRaises(profiles.ProfileError):
            profiles.run_request({'profile_id': ref.profile_id, 'profile_revision': 3,
                                  'evidence': evidence}, store=self.store)
        with self.assertRaises(profiles.ProfileError):
            profiles.run_request({'profile_id': ref.profile_id, 'evidence': evidence})


class InterchangeTests(support.StoreFixture):
    def setUp(self):
        super().setUp()
        self.spec = support.spec(optional_rule='at_least', k=1, attempts=2,
                                 budget={'max_probes': 12},
                                 targets=[support.target(support.BASIC),
                                          support.target(support.VIDEO, 'optional')])
        self.ref = self.store.create('API проекта', self.spec, at=START)
        self.store.update(self.ref.profile_id, self.spec.copy_of(attempts=3), base_revision=1,
                          at=START + 60)

    def test_export_carries_no_credential(self):
        document = profiles.export_profile(self.store, self.ref.profile_id, history=True,
                                           at=START + 120)
        text = json.dumps(document, ensure_ascii=False)
        self.assertEqual(document['kind'], profiles.EXPORT_KIND)
        self.assertEqual(document['secrets'], 'none')
        self.assertEqual(document['name'], 'API проекта')
        self.assertEqual([item['revision'] for item in document['revisions']], [1, 2])
        self.assertNotIn(CANARY, text)
        for forbidden in ('password', 'token', 'authorization', 'credential', 'username'):
            self.assertNotIn(forbidden, text.lower())
        # The stored rows carry the same document and no credential either.
        for row in self.rows():
            self.assertNotIn(CANARY, row['config'])
            self.assertNotIn('password', row['config'].lower())

    def test_a_document_with_a_credential_is_refused_instead_of_exported(self):
        polluted = profiles.export_profile(self.store, self.ref.profile_id, at=START + 120)
        polluted['revisions'][0]['config']['targets'][0]['password'] = CANARY
        with self.assertRaises(profiles.ProfileError) as raised:
            profiles.assert_secret_free(polluted)
        self.assertEqual(raised.exception.code, 'E_SECRET_IN_PROFILE')
        with self.assertRaises(profiles.ProfileError):
            profiles.import_profile(self.store, polluted)
        # A target address that carries userinfo is refused as well.
        addressed = profiles.export_profile(self.store, self.ref.profile_id, at=START + 120)
        addressed['revisions'][0]['config']['description'] = f'http://user:{CANARY}@host.invalid/'
        with self.assertRaises(profiles.ProfileError):
            profiles.assert_secret_free(addressed)
        self.assertEqual(len(self.rows()), 2)

    def test_an_export_of_one_revision_carries_only_that_revision(self):
        document = profiles.export_profile(self.store, self.ref.profile_id, revision=1,
                                           at=START + 120)
        self.assertEqual([item['revision'] for item in document['revisions']], [1])
        self.assertEqual(document['revisions'][0]['config']['attempts'], 2)
        self.assertIsNone(document['revisions'][0]['parent_revision'])
        restored = profiles.import_profile(self.store, document, name='Из экспорта', at=START + 180)
        record = self.store.get(restored.profile_id)
        self.assertEqual(record.revision, 1)
        self.assertEqual(record.spec.digest, self.spec.digest)

    def test_history_survives_a_round_trip(self):
        document = profiles.export_profile(self.store, self.ref.profile_id, history=True,
                                           at=START + 120)
        restored = profiles.import_profile(self.store, document, name='Восстановленный',
                                           at=START + 180)
        history = self.store.history(restored.profile_id)
        self.assertEqual([item.revision for item in history], [1, 2])
        self.assertEqual([item.spec.digest for item in history],
                         [item['digest'] for item in document['revisions']])
        self.assertIsNone(history[0].parent_id)
        self.assertEqual(history[1].parent_id, history[0].row_id)
        self.assertEqual(self.store.head(restored.profile_id).digest, document['digest'])

    def test_import_refuses_to_overwrite_and_can_rename_or_reuse(self):
        document = profiles.export_profile(self.store, self.ref.profile_id, history=True,
                                           at=START + 120)
        with self.assertRaises(profiles.ProfileError) as raised:
            profiles.import_profile(self.store, document)
        self.assertEqual(raised.exception.code, 'E_CONFLICT_NAME')
        renamed = profiles.import_profile(self.store, document, on_conflict='rename',
                                          at=START + 180)
        self.assertEqual(self.store.get(renamed.profile_id).name, 'API проекта (2)')
        same = profiles.import_profile(self.store, document, on_conflict='rename',
                                       at=START + 181)
        self.assertEqual(self.store.get(same.profile_id).name, 'API проекта (3)')

    def test_importing_the_same_document_twice_is_idempotent(self):
        document = profiles.export_profile(self.store, self.ref.profile_id, history=True,
                                           at=START + 120)
        first = profiles.import_profile(self.store, document, name='Один раз', at=START + 180)
        rows = len(self.rows())
        second = profiles.import_profile(self.store, document, name='Один раз', at=START + 181,
                                         on_conflict='reuse')
        self.assertEqual(first, second)
        self.assertEqual(len(self.rows()), rows)

    def test_a_repeated_import_of_different_content_points_at_update(self):
        """History is not replaced behind the user's back: only update() appends."""
        document = profiles.export_profile(self.store, self.ref.profile_id, at=START + 120)
        profiles.import_profile(self.store, document, name='Повтор', at=START + 180)
        other = profiles.export_profile(self.store, self.ref.profile_id, at=START + 120)
        other['revisions'][0]['config']['description'] = 'другое'
        with self.assertRaises(profiles.ProfileError) as raised:
            profiles.import_profile(self.store, other, name='Повтор', on_conflict='reuse',
                                    at=START + 181)
        self.assertEqual(raised.exception.code, 'E_CONFLICT_NAME')
        self.assertIn('update', str(raised.exception))

    def test_a_foreign_or_newer_document_is_refused(self):
        document = profiles.export_profile(self.store, self.ref.profile_id, at=START + 120)
        foreign = dict(document, kind='proxy-workbench/collection')
        with self.assertRaises(profiles.ProfileError) as raised:
            profiles.import_profile(self.store, foreign)
        self.assertEqual(raised.exception.code, 'E_IMPORT_FORMAT')
        newer = dict(document, version=profiles.EXPORT_VERSION + 1)
        with self.assertRaises(profiles.ProfileError) as raised:
            profiles.import_profile(self.store, newer)
        self.assertEqual(raised.exception.code, 'E_IMPORT_REVISION')
        empty = dict(document, revisions=[])
        with self.assertRaises(profiles.ProfileError):
            profiles.import_profile(self.store, empty)
        with self.assertRaises(profiles.ProfileError):
            profiles.import_profile(self.store, '{not json')
        with self.assertRaises(profiles.ProfileError):
            profiles.import_profile(self.store, document, on_conflict='replace')
        with self.assertRaises(profiles.ProfileError):
            profiles.import_profile(self.store.conn, document)

    def test_an_unusable_document_never_creates_a_profile(self):
        """A document that cannot pass is refused on the way in, not after."""
        document = profiles.export_profile(self.store, self.ref.profile_id, at=START + 120)
        document['revisions'][0]['config']['targets'] = []
        before = len(self.rows())
        with self.assertRaises(profiles.ProfileError) as raised:
            profiles.import_profile(self.store, document, name='Пустой')
        self.assertEqual(raised.exception.code, 'E_VALIDATION_FIELD')
        self.assertEqual(len(self.rows()), before)

    def test_export_accepts_a_bare_connection(self):
        document = profiles.export_profile(self.conn, self.ref.profile_id, at=START + 120)
        self.assertEqual(document['digest'], self.store.head(self.ref.profile_id).digest)
        with self.assertRaises(profiles.ProfileError):
            profiles.export_profile({'not': 'a store'}, self.ref.profile_id)


if __name__ == '__main__':
    unittest.main()

"""Secret references and redaction for user sources (F27).

A canary value is used throughout: the point of these tests is that it can
never reach the database, a serialized structure, a log line or a public view.
"""
import dataclasses
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import sourcedesk as sd

#: Canary, not a credential of anything.  It must not appear anywhere below
#: except in the test's own construction of the request.
CANARY = 'CANARY-4f19b7c2d8e0'
VAULT = {}


def put(name, value):
    """Stand-in for the secret store: keeps the value, returns a reference."""
    ref = sd.make_ref('href', 'test-vault', name)
    VAULT[ref] = value
    return ref


def resolve(ref):
    return VAULT[ref]


class RefTests(unittest.TestCase):
    def test_ref_is_stable_and_opaque(self):
        first = sd.make_ref('url', 'binding-1', 'https://example.invalid/list')
        second = sd.make_ref('url', 'binding-1', 'https://example.invalid/list')
        self.assertEqual(first, second)
        self.assertTrue(first.startswith('url-'))
        self.assertNotIn('example', first)
        self.assertNotIn('binding', first)
        self.assertTrue(sd.is_ref(first))

    def test_ref_differs_per_binding_and_per_value(self):
        base = sd.make_ref('url', 'binding-1', 'https://example.invalid/list')
        self.assertNotEqual(base, sd.make_ref('url', 'binding-2', 'https://example.invalid/list'))
        self.assertNotEqual(base, sd.make_ref('url', 'binding-1', 'https://example.invalid/other'))

    def test_ref_rejects_unknown_prefix_and_empty_parts(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.make_ref('password', 'binding-1')
        with self.assertRaises(sd.SourceDeskError):
            sd.make_ref('url', '')

    def test_is_ref_rejects_plain_values(self):
        self.assertFalse(sd.is_ref(CANARY))
        self.assertFalse(sd.is_ref('url-short'))
        self.assertFalse(sd.is_ref(None))


class RedactionTests(unittest.TestCase):
    def test_query_fragment_and_userinfo_are_dropped(self):
        value = sd.redact_url(f'https://user:{CANARY}@sub.example.invalid/list?token={CANARY}#frag')
        self.assertEqual(value, 'https://sub.example.invalid/list')
        self.assertEqual(sd.find_secret_leaks(value, [CANARY]), [])

    def test_capability_path_segment_is_replaced(self):
        value = sd.redact_url(f'https://sub.example.invalid/v1/{CANARY}/list')
        self.assertEqual(value, 'https://sub.example.invalid/v1/…/list')
        self.assertNotIn(CANARY, value)

    def test_ordinary_path_survives(self):
        value = sd.redact_url('https://raw.githubusercontent.invalid/a/b/main/proxies.txt')
        self.assertEqual(value, 'https://raw.githubusercontent.invalid/a/b/main/proxies.txt')

    def test_port_and_ipv6_host_are_kept(self):
        self.assertEqual(sd.redact_url('http://[2001:db8::1]:8080/x'),
                         'http://[2001:db8::1]:8080/x')
        self.assertEqual(sd.redact_url('https://h.invalid:8443/x'), 'https://h.invalid:8443/x')

    def test_unparsable_url_redacts_to_empty(self):
        self.assertEqual(sd.redact_url('http://[bad'), '')
        self.assertEqual(sd.redact_url(None), '')

    def test_header_values_never_survive_redaction(self):
        headers = {'Authorization': f'Bearer {CANARY}', 'X-Trace': CANARY}
        redacted = sd.redact_headers(headers)
        self.assertEqual(sorted(redacted), ['Authorization', 'X-Trace'])
        self.assertEqual(set(redacted.values()), {'<redacted>'})
        self.assertEqual(sd.find_secret_leaks(redacted, [CANARY]), [])

    def test_redact_headers_ignores_non_mappings(self):
        self.assertEqual(sd.redact_headers(['Authorization']), {})


class ResolveTests(unittest.TestCase):
    def test_resolve_requires_a_reference_prefix(self):
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.resolve_url(put('h', CANARY), resolve)
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_resolve_needs_a_resolver(self):
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.resolve_url(sd.make_ref('url', 'b', 'https://x.invalid/list'), None)
        self.assertEqual(caught.exception.code, 'E_SECRET_VAULT_LOCKED')

    def test_resolve_url_and_headers_return_values_only_on_demand(self):
        url_ref = sd.make_ref('url', 'b', f'https://x.invalid/{CANARY}')
        header_ref = put('Authorization', f'Bearer {CANARY}')
        VAULT[url_ref] = f'https://x.invalid/{CANARY}'
        self.assertEqual(sd.resolve_url(url_ref, resolve), f'https://x.invalid/{CANARY}')
        headers = sd.resolve_headers({'Authorization': header_ref}, resolve)
        self.assertEqual(headers, {'Authorization': f'Bearer {CANARY}'})

    def test_unresolvable_reference_is_reported(self):
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.resolve_headers({'Authorization': sd.make_ref('href', 'missing')}, resolve)
        self.assertEqual(caught.exception.code, 'E_SECRET_NOT_PROVIDED')

    def test_header_reference_of_the_wrong_kind_is_refused(self):
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.resolve_headers({'Authorization': sd.make_ref('url', 'b')}, resolve)
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')


class UserSourceTests(unittest.TestCase):
    def setUp(self):
        VAULT.clear()

    def test_header_value_is_only_ever_handed_to_the_store(self):
        source = sd.user_source(binding_id='b1', url='https://sub.invalid/list',
                                source_format='clash',
                                headers={'Authorization': f'Bearer {CANARY}'}, header_put=put)
        self.assertEqual(len(source.header_refs), 1)
        self.assertEqual(source.header_refs[0][0], 'Authorization')
        self.assertTrue(sd.is_ref(source.header_refs[0][1]))
        self.assertEqual(VAULT[source.header_refs[0][1]], f'Bearer {CANARY}')
        self.assertEqual(sd.find_secret_leaks(source, [CANARY]), [])

    def test_raw_header_value_without_a_store_is_refused(self):
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.user_source(binding_id='b1', url='https://sub.invalid/list',
                           headers={'Authorization': f'Bearer {CANARY}'})
        self.assertEqual(caught.exception.code, 'E_SECRET_VAULT_LOCKED')

    def test_store_returning_a_non_reference_is_refused(self):
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.user_source(binding_id='b1', url='https://sub.invalid/list',
                           headers={'Authorization': CANARY}, header_put=lambda name, value: value)
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_public_view_has_no_secret(self):
        source = sd.user_source(binding_id='b1', url=f'https://sub.invalid/{CANARY}?t={CANARY}',
                                source_format='clash',
                                headers={'Cookie': CANARY}, header_put=put,
                                access_ref=sd.make_ref('auth', 'b1', 'user'),
                                access_id='access-1')
        view = source.public_view()
        self.assertEqual(sd.find_secret_leaks(view, [CANARY]), [])
        self.assertEqual(json.dumps(view, ensure_ascii=False).count(CANARY), 0)
        self.assertTrue(view['has_credentials'])
        self.assertIn('…', view['public_url'])

    def test_credentials_require_an_access_id(self):
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.user_source(binding_id='b1', url='https://sub.invalid/list',
                           access_ref=sd.make_ref('auth', 'b1', 'user'))
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_url_forms_that_cannot_be_a_source_are_refused(self):
        for url in ('', 'ftp://h.invalid/x', f'https://u:{CANARY}@h.invalid/x',
                    'https://h.invalid/x#f', 'https://..invalid/x', None, 5):
            with self.subTest(url=url), self.assertRaises(sd.SourceDeskError):
                sd.user_source(binding_id='b1', url=url)

    def test_source_id_is_stable_for_the_same_binding_and_url(self):
        first = sd.user_source(binding_id='b1', url='https://sub.invalid/list')
        second = sd.user_source(binding_id='b1', url='https://sub.invalid/list')
        other_binding = sd.user_source(binding_id='b2', url='https://sub.invalid/list')
        self.assertEqual(first.id, second.id)
        self.assertNotEqual(first.id, other_binding.id)

    def test_unsupported_format_is_refused(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.user_source(binding_id='b1', url='https://sub.invalid/list', source_format='yaml-exec')

    def test_name_defaults_to_the_public_url(self):
        source = sd.user_source(binding_id='b1', url='https://sub.invalid/list')
        self.assertEqual(source.name, 'https://sub.invalid/list')


class PersistedSecretTests(unittest.TestCase):
    """The canary must not reach the database, a row dump or a JSON payload."""

    def setUp(self):
        VAULT.clear()
        self.db = sqlite3.connect(':memory:')
        for statement in sd.REQUESTED_DDL:
            self.db.execute(statement)
        self.desk = sd.SourceDesk(self.db)
        self.source = sd.user_source(binding_id='b1', url=f'https://sub.invalid/v1/{CANARY}/list?token={CANARY}',
                                     source_format='clash',
                                     headers={'Authorization': f'Bearer {CANARY}'}, header_put=put)

    def tearDown(self):
        self.db.close()
        VAULT.clear()

    def _everything_persisted(self):
        rows = [str(row) for row in self.db.execute('SELECT * FROM source_feed')]
        rows += [str(row) for row in self.db.execute('SELECT * FROM membership_source')]
        return rows

    def test_binding_row_keeps_references_only(self):
        self.desk.bind(self.source, collection_id='c1', mode='replace')
        row = self.desk.feed_row(self.source.id, 'c1')
        self.assertEqual(row['url_ref'], self.source.url_ref)
        self.assertEqual(row['header_refs'], {'Authorization': self.source.header_refs[0][1]})
        self.assertEqual(sd.find_secret_leaks(row, [CANARY]), [])
        self.assertEqual(sd.find_secret_leaks(self._everything_persisted(), [CANARY]), [])
        self.assertEqual(json.dumps(row, ensure_ascii=False).count(CANARY), 0)

    def test_listed_feeds_carry_no_secret(self):
        self.desk.bind(self.source, collection_id='c1')
        listed = self.desk.list_feeds()
        self.assertEqual(len(listed), 1)
        self.assertEqual(sd.find_secret_leaks(listed, [CANARY]), [])

    def test_editing_the_url_creates_a_new_source(self):
        self.desk.bind(self.source, collection_id='c1')
        edited = sd.user_source(binding_id='b1', url='https://sub.invalid/v1/other/list')
        self.assertNotEqual(edited.id, self.source.id)
        self.desk.bind(edited, collection_id='c1')
        self.assertEqual(len(self.desk.list_feeds()), 2)

    def test_repointing_an_existing_id_at_another_url_is_a_conflict(self):
        # A migrated record keeps its stable id; the URL behind it may not
        # change, or the id would start naming different data.
        self.desk.bind(self.source, collection_id='c1')
        repointed = dataclasses.replace(self.source,
                                        url_ref=sd.make_ref('url', 'b1', 'https://sub.invalid/other'))
        with self.assertRaises(sd.SourceDeskError) as caught:
            self.desk.bind(repointed, collection_id='c1')
        self.assertEqual(caught.exception.code, 'E_CONFLICT_REVISION')

    def test_rebinding_the_same_url_is_idempotent(self):
        self.desk.bind(self.source, collection_id='c1')
        state = self.desk.bind(self.source, collection_id='c1')
        self.assertEqual(state.source_id, self.source.id)
        self.assertEqual(len(self.desk.list_feeds()), 1)

    def test_rebinding_does_not_reset_lifecycle_state(self):
        self.desk.bind(self.source, collection_id='c1')
        self.desk.add_membership('c1', self.source.id, ['http://198.51.100.7:8080'], now=10.0)
        self.desk.bind(self.source, collection_id='c1')
        self.assertEqual(self.desk.membership(self.source.id, 'c1'), ('http://198.51.100.7:8080',))


class LeakFinderTests(unittest.TestCase):
    def test_finds_nested_positions(self):
        payload = {'a': [{'b': f'x{CANARY}'}]}
        self.assertEqual(sd.find_secret_leaks(payload, [CANARY]), [('/a/0/b', CANARY)])

    def test_bytes_are_decoded_before_matching(self):
        self.assertEqual(len(sd.find_secret_leaks({'a': CANARY.encode()}, [CANARY])), 1)

    def test_no_needles_means_nothing_to_find(self):
        self.assertEqual(sd.find_secret_leaks({'a': CANARY}, []), [])
        self.assertEqual(sd.find_secret_leaks({'a': CANARY}, None), [])


if __name__ == '__main__':
    unittest.main()

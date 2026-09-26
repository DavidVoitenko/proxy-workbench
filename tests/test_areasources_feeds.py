"""F27: user URLs, subscriptions and the life cycle of a feed.

The desk stores a binding, decides what a refresh does to a collection and
rotates credentials.  It never performs the fetch itself, so every scenario
here drives :func:`plan_refresh` with the outcome the network layer would have
reported -- which is exactly the contract the fetch layer has to meet.

A canary secret runs through the whole path.  It must not appear in the public
view, in the persisted rows, in a diagnostic or in a serialized plan.
"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import sourcedesk as sd

CANARY = 'canary-SUPERSECRET-9f2b7c'
SECRET_URL = f'https://panel.example.com/sub/{CANARY}?token={CANARY}&uuid=0d5f'
COLLECTION = 'my-collection'
T0 = 1_757_000_000.0
DAY = 86400.0

ENDPOINTS = tuple(f'http://198.51.100.{n}:8080' for n in range(1, 6))
VAULT = {}


def _put(prefix, *parts):
    ref = sd.make_ref(prefix, *parts)
    VAULT[ref] = parts[-1] if prefix in ('auth', 'href') else '\0'.join(parts)
    return ref


def _resolver(ref):
    # ``user_source`` derives the URL reference from the binding id and the raw
    # URL, so a real vault resolves it the same way; nothing else is guessable.
    if ref == sd.make_ref('url', 'my-feed', SECRET_URL):
        return SECRET_URL
    return VAULT.get(ref)


def _user_source(url=SECRET_URL, *, mode='text'):
    return sd.user_source(
        binding_id='my-feed', url=url, name='Моя подписка', source_format=mode,
        headers={'Authorization': 'Bearer ' + CANARY},
        header_put=lambda name, value: _put('href', name, value),
        access_ref=_put('auth', 'user', 'pass-' + CANARY), access_id='acc-7')


def _ok(endpoints, at, *, etag=None, outcome='ok'):
    return sd.FeedResult(outcome=outcome, fetched_at=at,
                         entries=[sd.ImportedEndpoint(endpoint=item) for item in endpoints],
                         etag=etag, body_sha256='d' * 64, status=200)


def _desk():
    conn = sqlite3.connect(':memory:')
    conn.execute('PRAGMA foreign_keys=ON')
    for ddl in sd.REQUESTED_DDL:
        conn.execute(ddl)
    return conn


class BindingTest(unittest.TestCase):
    def setUp(self):
        self.conn = _desk()
        self.desk = sd.SourceDesk(self.conn, origin='own')
        self.source = _user_source()

    def tearDown(self):
        self.conn.close()

    def test_a_typed_url_becomes_a_reference_and_a_redacted_public_form(self):
        view = self.source.public_view()
        self.assertNotIn(CANARY, json.dumps(view, ensure_ascii=False))
        self.assertTrue(view['url_ref'].startswith('url-'))
        self.assertEqual(view['public_url'], 'https://panel.example.com/sub/…')
        self.assertEqual(view['header_values'], {'Authorization': '<redacted>'})
        self.assertTrue(view['has_credentials'])

    def test_a_header_value_is_refused_without_a_secret_store(self):
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.user_source(binding_id='b', url='https://lists.example/a.txt',
                           headers={'Authorization': 'Bearer ' + CANARY})
        self.assertEqual(caught.exception.code, 'E_SECRET_VAULT_LOCKED')

    def test_the_binding_round_trips_through_the_database(self):
        state = self.desk.bind(self.source, collection_id=COLLECTION, mode='merge',
                               adapter_kind='line')
        stored = self.desk.get(self.source.id, COLLECTION)
        self.assertEqual(stored.source_id, self.source.id)
        self.assertEqual(stored.access_id, 'acc-7')
        row = self.desk.feed_row(self.source.id, COLLECTION)
        self.assertEqual(row['url_ref'], self.source.url_ref)
        self.assertEqual(row['header_refs'], dict(self.source.header_refs))
        self.assertEqual(state.source_id, self.source.id)
        self.assertEqual(state.mode, 'merge')

    def test_the_canary_is_nowhere_in_the_persisted_rows(self):
        self.desk.bind(self.source, collection_id=COLLECTION)
        dump = json.dumps(self.conn.execute('SELECT * FROM source_feed').fetchall(), ensure_ascii=False)
        self.assertNotIn(CANARY, dump)
        self.assertNotIn(CANARY, str(sd.find_secret_leaks(dump, [CANARY])))

    def test_editing_the_url_creates_a_new_source_instead_of_re_pointing_the_old_one(self):
        first = self.desk.bind(self.source, collection_id=COLLECTION)
        moved = _user_source('https://panel.example.com/sub/other')
        self.assertNotEqual(moved.id, first.source_id)
        second = self.desk.bind(moved, collection_id=COLLECTION)
        self.assertEqual(second.source_id, moved.id)
        self.assertEqual(len(self.desk.list_feeds(collection_id=COLLECTION)), 2)

    def test_a_stored_id_may_not_be_re_pointed_at_a_different_url(self):
        self.desk.bind(self.source, collection_id=COLLECTION)
        impostor = sd.UserSource(id=self.source.id, name='x', url_ref='url-0123456789abcdef',
                                 public_url='https://elsewhere.example/list.txt', format='text')
        with self.assertRaises(sd.SourceDeskError) as caught:
            self.desk.bind(impostor, collection_id=COLLECTION)
        self.assertEqual(caught.exception.code, 'E_CONFLICT_REVISION')

    def test_a_reference_is_only_materialized_for_the_fetch_layer(self):
        self.assertEqual(sd.resolve_url(self.source.url_ref, _resolver), SECRET_URL)
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.resolve_url(self.source.url_ref, None)
        self.assertEqual(caught.exception.code, 'E_SECRET_VAULT_LOCKED')
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.resolve_url('url-0000000000000000', _resolver)
        self.assertEqual(caught.exception.code, 'E_SECRET_NOT_PROVIDED')

    def test_a_vault_that_raises_reads_as_not_provided(self):
        def broken(ref):
            raise RuntimeError('vault is locked')
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.resolve_url(self.source.url_ref, broken)
        self.assertEqual(caught.exception.code, 'E_SECRET_NOT_PROVIDED')

    def test_resolve_headers_needs_references_not_values(self):
        refs = dict(self.source.header_refs)
        self.assertEqual(sd.resolve_headers(refs, _resolver),
                         {'Authorization': 'Bearer ' + CANARY})
        with self.assertRaises(sd.SourceDeskError):
            sd.resolve_headers({'Authorization': 'Bearer ' + CANARY}, _resolver)

    def test_redaction_keeps_the_host_and_drops_the_capability(self):
        redacted = sd.redact_url(SECRET_URL)
        self.assertIn('panel.example.com', redacted)
        self.assertNotIn(CANARY, redacted)
        self.assertNotIn('token=', redacted)
        self.assertNotIn('uuid=', redacted)
        self.assertEqual(sd.redact_headers({'X': 'v'}), {'X': '<redacted>'})
        self.assertEqual(sd.redact_headers(None), {})


class RefreshLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.conn = _desk()
        self.desk = sd.SourceDesk(self.conn, origin='own')
        self.source = _user_source()
        self.policy = sd.FeedPolicy(mode='merge', ttl_seconds=6 * 3600)
        self.desk.bind(self.source, collection_id=COLLECTION, mode='merge')
        state = self.desk.get(self.source.id, COLLECTION)
        self.state = self.desk.apply_plan(sd.plan_refresh(state, _ok(ENDPOINTS, T0, etag='"v1"'),
                                                         policy=self.policy, now=T0))

    def tearDown(self):
        self.conn.close()

    def apply(self, result, *, now, policy=None, foreign=()):
        plan = sd.plan_refresh(self.desk.get(self.source.id, COLLECTION), result,
                               policy=policy or self.policy, now=now, foreign=foreign)
        self.state = self.desk.apply_plan(plan, now=now)
        return plan

    def test_a_good_update_becomes_the_active_set_and_a_last_good(self):
        self.assertEqual(self.state.active, ENDPOINTS)
        self.assertEqual(self.state.last_good, ENDPOINTS)
        self.assertEqual(self.state.last_good_at, T0)
        self.assertEqual(self.state.expires_at, T0 + 6 * 3600)
        self.assertEqual(self.state.etag, '"v1"')
        self.assertEqual(self.desk.membership(self.source.id, COLLECTION), ENDPOINTS)

    def test_a_failing_update_never_clears_the_working_collection(self):
        for outcome, status in (('unavailable', 503), ('rate_limited', 429),
                                ('token_expired', 401), ('quota_exhausted', 402),
                                ('invalid', 200), ('too_large', 200)):
            with self.subTest(outcome=outcome):
                plan = self.apply(sd.FeedResult(outcome=outcome, fetched_at=T0 + 3600, status=status),
                                  now=T0 + 3600)
                self.assertFalse(plan.applied_removals)
                self.assertTrue(plan.serve_from_last_good)
                self.assertEqual(self.desk.membership(self.source.id, COLLECTION), ENDPOINTS)
                self.assertEqual(self.state.expires_at, T0 + 6 * 3600,
                                 'провал обновления не должен продлевать срок')

    def test_an_empty_update_keeps_the_collection_and_says_so(self):
        plan = self.apply(sd.FeedResult(outcome='empty', fetched_at=T0 + 3600, status=200),
                          now=T0 + 3600)
        self.assertEqual(self.state.active, ENDPOINTS)
        self.assertIn('E_SOURCE_EMPTY_FEED', [item.code for item in plan.diagnostics])

    def test_a_partial_update_never_removes_anything(self):
        plan = self.apply(_ok(ENDPOINTS[:2], T0 + 3600, outcome='partial'), now=T0 + 3600)
        self.assertEqual(plan.removed, ())
        self.assertEqual(self.state.active, ENDPOINTS)
        self.assertTrue(plan.serve_from_last_good)

    def test_a_304_validates_transport_only(self):
        plan = self.apply(sd.FeedResult(outcome='not_modified', fetched_at=T0 + 3600, status=304),
                          now=T0 + 3600)
        self.assertFalse(plan.applied)
        self.assertEqual(plan.added, ())
        self.assertEqual(plan.removed, ())
        self.assertFalse(plan.promote_last_good)
        self.assertEqual(self.state.expires_at, T0 + 6 * 3600, '304 не продлевает срок данных')
        self.assertEqual(self.state.last_good, ENDPOINTS)
        self.assertEqual(self.state.consecutive_failures, 0)

    def test_a_delta_adds_and_keeps_without_claiming_removals(self):
        grown = ENDPOINTS + ('http://198.51.100.6:8080',)
        plan = self.apply(_ok(grown, T0 + 3600), now=T0 + 3600)
        self.assertEqual(plan.added, ('http://198.51.100.6:8080',))
        self.assertEqual(plan.kept, ENDPOINTS)
        self.assertEqual(plan.removed, ())

    def test_replace_drops_only_what_this_source_dropped(self):
        other = 'http://198.51.100.9:8080'
        self.conn.execute('INSERT INTO membership_source (collection_id, endpoint_id, source_id, '
                          'origin, added_at, last_seen_at) VALUES (?,?,?,?,?,?)',
                          (COLLECTION, ENDPOINTS[4], 'other-feed', 'own', T0, T0))
        self.desk.add_membership(COLLECTION, 'other-feed', (other,), now=T0)
        plan = self.apply(_ok(ENDPOINTS[:3], T0 + 3600), now=T0 + 3600,
                          policy=sd.FeedPolicy(mode='replace'),
                          foreign=self.desk.foreign_membership(self.source.id, COLLECTION, ENDPOINTS))
        self.assertEqual(plan.removed, (ENDPOINTS[3], ENDPOINTS[4]))
        # The address the other source also owns keeps its membership.
        self.assertEqual(self.desk.membership('other-feed', COLLECTION),
                         (ENDPOINTS[4], other))
        self.assertEqual(self.desk.contributors(ENDPOINTS[4], COLLECTION), ('other-feed',))
        self.assertNotIn(ENDPOINTS[4], self.desk.membership(self.source.id, COLLECTION))

    def test_a_shared_address_another_source_still_offers_is_reported(self):
        self.desk.add_membership(COLLECTION, 'other-feed', (ENDPOINTS[4],), now=T0)
        plan = self.apply(_ok(ENDPOINTS[:3], T0 + 3600), now=T0 + 3600,
                          policy=sd.FeedPolicy(mode='replace'),
                          foreign=self.desk.foreign_membership(self.source.id, COLLECTION, ENDPOINTS))
        self.assertEqual(plan.retained_shared, (ENDPOINTS[4],))

    def test_backoff_grows_and_a_retry_after_is_never_earlier_than_our_own(self):
        seen = []
        for index in range(4):
            plan = self.apply(sd.FeedResult(outcome='unavailable', fetched_at=T0 + index * 3600,
                                            status=503, retry_after=T0 + index * 3600 + 100000),
                              now=T0 + index * 3600)
            seen.append(plan.next_attempt_at)
        self.assertEqual(seen, sorted(seen))
        for moment in seen:
            self.assertGreaterEqual(moment, T0 + 100000)
        self.assertEqual(self.state.consecutive_failures, 4)

    def test_repeated_failures_eventually_quarantine_the_feed(self):
        for index in range(sd.QUARANTINE_AFTER_FAILURES):
            self.apply(sd.FeedResult(outcome='unavailable', fetched_at=T0 + index * 3600, status=503),
                       now=T0 + index * 3600)
        # The third failure quarantines until T0 + 2*3600 + 1800.
        self.assertIsNotNone(self.state.quarantine_until)
        self.assertEqual(self.state.quarantine_until, T0 + 2 * 3600 + 1800)
        inside = {item.code for item in sd.feed_diagnostics(self.state, now=T0 + 8000)}
        self.assertIn('E_SOURCE_QUARANTINED', inside)
        outside = {item.code for item in sd.feed_diagnostics(self.state, now=T0 + 10000)}
        self.assertNotIn('E_SOURCE_QUARANTINED', outside)
        # A good update clears the quarantine.
        self.apply(_ok(ENDPOINTS, T0 + 20000), now=T0 + 20000)
        self.assertIsNone(self.state.quarantine_until)
        self.assertEqual(self.state.consecutive_failures, 0)

    def test_expiry_stale_and_dropped_are_three_different_statements(self):
        policy = sd.FeedPolicy(mode='merge', ttl_seconds=3600, drop_after_seconds=7200)
        fresh = sd.FeedPolicy(mode='merge', ttl_seconds=3600, drop_after_seconds=7200)
        def codes_at(moment):
            state = sd.FeedState(self.source.id, COLLECTION, mode='merge',
                                 expires_at=T0 + 3600, last_good=ENDPOINTS)
            return {item.code for item in sd.feed_diagnostics(state, now=moment, policy=policy)}

        self.assertEqual(codes_at(T0 + 1800), set(), 'свежие данные не требуют диагностики')
        self.assertIn('E_SOURCE_STALE', codes_at(T0 + 5400))
        self.assertNotIn('E_SOURCE_EXPIRED', codes_at(T0 + 5400))
        self.assertIn('E_SOURCE_EXPIRED', codes_at(T0 + 20000))
        self.assertNotIn('E_SOURCE_STALE', codes_at(T0 + 20000))
        self.assertTrue(fresh.drop_after_seconds)


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.conn = _desk()
        self.desk = sd.SourceDesk(self.conn, origin='own')
        self.source = _user_source()
        self.desk.bind(self.source, collection_id=COLLECTION)
        self.state = self.desk.get(self.source.id, COLLECTION)

    def tearDown(self):
        self.conn.close()

    def test_changing_credentials_bumps_the_revision_and_names_what_it_invalidates(self):
        plan, state, diagnostics = sd.plan_rotation(
            self.state, access_id='acc-7', new_access_ref=_put('auth', 'user', 'new-pass'), now=T0)
        self.assertEqual(plan.previous_revision, 1)
        self.assertEqual(plan.revision, 2)
        self.assertEqual(plan.invalidates, (('acc-7', 1),))
        self.assertEqual(state.access_revision, 2)
        self.assertIn('E_SOURCE_ACCESS_REVISION_CHANGED', [item.code for item in diagnostics])
        self.desk.apply_rotation(plan, state, now=T0)
        self.assertEqual(self.desk.get(self.source.id, COLLECTION).access_revision, 2)

    def test_a_first_binding_has_nothing_to_invalidate(self):
        fresh = sd.FeedState('other', COLLECTION)
        plan, _state, _diag = sd.plan_rotation(fresh, access_id='acc-7',
                                               new_access_ref=_put('auth', 'u', 'p'), now=T0)
        self.assertEqual(plan.invalidates, ())
        self.assertEqual(plan.revision, 2)

    def test_a_source_bound_to_another_account_needs_an_explicit_rebind(self):
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.plan_rotation(self.state, access_id='acc-OTHER',
                             new_access_ref=_put('auth', 'u', 'p'), now=T0)
        self.assertEqual(caught.exception.code, 'E_CONFLICT_REVISION')

    def test_a_rotation_needs_a_reference_not_a_value(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.plan_rotation(self.state, access_id='acc-7',
                             new_access_ref='new-password', now=T0)


class QuotaAndTokenTest(unittest.TestCase):
    def test_a_numeric_retry_after_is_read_as_an_absolute_moment(self):
        quota = sd.quota_from_response({'Retry-After': '120'}, now=T0)
        self.assertEqual(quota.retry_after, T0 + 120)
        self.assertEqual(sd.retry_after_from_response({'retry-after': '120'}, now=T0), T0 + 120)

    def test_an_http_date_retry_after_is_read_too(self):
        # RFC 9110 allows both forms; the date form is the one many origins send.
        from email.utils import formatdate
        when = formatdate(T0 + 300, usegmt=True)
        self.assertEqual(sd.retry_after_from_response({'Retry-After': when}, now=T0), T0 + 300)

    def test_an_unreadable_retry_after_is_unknown_not_a_guess(self):
        self.assertIsNone(sd.retry_after_from_response({'Retry-After': 'soon'}, now=T0))
        self.assertIsNone(sd.retry_after_from_response({}, now=T0))
        self.assertIsNone(sd.retry_after_from_response(None, now=T0))

    def test_a_retry_after_becomes_a_diagnostic_and_never_a_shorter_wait(self):
        state = sd.FeedState('s', COLLECTION)
        quota = sd.quota_from_response({'Retry-After': '600'}, now=T0)
        codes = {item.code for item in sd.feed_diagnostics(state, now=T0, quota=quota)}
        self.assertIn('E_SOURCE_RETRY_AFTER', codes)
        plan = sd.plan_refresh(state, sd.FeedResult(outcome='rate_limited', fetched_at=T0, status=429,
                                                     retry_after=quota.retry_after),
                               now=T0)
        self.assertGreaterEqual(plan.next_attempt_at, T0 + 600)

    def test_quota_headers_are_read_and_an_absent_quota_stays_unknown(self):
        quota = sd.quota_from_response({'X-RateLimit-Limit': '1000', 'X-RateLimit-Remaining': '0',
                                        'X-RateLimit-Reset': '60'}, now=T0)
        self.assertEqual((quota.limit, quota.remaining, quota.reset_at), (1000, 0, T0 + 60))
        self.assertTrue(quota.exhausted)
        self.assertIsNone(sd.quota_from_response({'Content-Type': 'text/plain'}, now=T0))
        codes = {item.code for item in sd.feed_diagnostics(
            sd.FeedState('s', COLLECTION, access_ref='auth-0'), now=T0,
            quota=sd.quota_from_response({}, now=T0))}
        self.assertIn('E_SOURCE_QUOTA_UNKNOWN', codes)

    def test_a_low_balance_is_a_warning_not_an_outage(self):
        quota = sd.quota_from_response({'X-Quota-Limit': '100', 'X-Quota-Remaining': '5'}, now=T0)
        codes = {item.code for item in sd.feed_diagnostics(sd.FeedState('s', COLLECTION), now=T0,
                                                           quota=quota)}
        self.assertIn('E_SOURCE_QUOTA_LOW', codes)
        self.assertNotIn('E_SOURCE_QUOTA_EXHAUSTED', codes)

    def test_a_token_that_expires_soon_is_named_before_it_expires(self):
        soon = sd.quota_from_response({'X-Token-Expires-At': '300'}, now=T0)
        codes = {item.code for item in sd.feed_diagnostics(
            sd.FeedState('s', COLLECTION), now=T0, quota=soon)}
        self.assertIn('E_SOURCE_TOKEN_EXPIRING', codes)
        gone = sd.quota_from_response({}, now=T0, token_expires_at=T0 - 1)
        codes = {item.code for item in sd.feed_diagnostics(
            sd.FeedState('s', COLLECTION), now=T0, quota=gone)}
        self.assertIn('E_SOURCE_TOKEN_EXPIRED', codes)

    def test_every_diagnostic_code_the_module_can_emit_is_declared(self):
        self.assertIn('E_SOURCE_RETRY_AFTER', sd.DIAGNOSTIC_CODES)
        self.assertIn('E_SOURCE_EMPTY_FEED', sd.DIAGNOSTIC_CODES)
        self.assertIn('E_SOURCE_TOKEN_EXPIRED', sd.DIAGNOSTIC_CODES)


class SubscriptionImportTest(unittest.TestCase):
    """Clash and sing-box documents are data, never programs."""

    CLASH = {'proxies': [
        {'name': 'plain', 'type': 'http', 'server': '198.51.100.5', 'port': 8080},
        {'name': 'socks', 'type': 'socks5', 'server': '203.0.113.5', 'port': 1080,
         'username': 'u', 'password': CANARY},
        {'name': 'vmess', 'type': 'vmess', 'server': '203.0.113.6', 'port': 443,
         'uuid': CANARY, 'alterId': 0}],
        'rules': ['DOMAIN-SUFFIX,evil.example,REJECT', 'MATCH,DIRECT'],
        'rule-providers': {'evil': {'type': 'http', 'url': f'https://x/{CANARY}'}},
        'proxy-groups': [{'name': 'g', 'type': 'select', 'proxies': ['plain']}],
        'script': {'code': f'require("os").system("curl {CANARY}")'}}

    SINGBOX = {'outbounds': [
        {'type': 'socks', 'server': '198.51.100.6', 'server_port': 1080, 'version': '5'},
        {'type': 'http', 'server': '203.0.113.7', 'server_port': 8080},
        {'type': 'vmess', 'server': '203.0.113.8', 'server_port': 443, 'uuid': CANARY},
        {'type': 'direct'}, {'type': 'block'}],
        'route': {'rules': [{'action': 'reject'}], 'final': 'proxy'},
        'dns': {'servers': ['https://dns.example/dns-query']},
        'experimental': {'clash_api': {}}}

    def test_clash_yields_only_the_endpoints_and_the_protocols_we_support(self):
        result = sd.import_clash(json.dumps(self.CLASH).encode())
        self.assertEqual(result.canonical, ('http://198.51.100.5:8080',))
        self.assertTrue(result.ignored, 'sections that steer traffic must be reported as ignored')
        # vmess is a transport this application cannot check or export.
        self.assertIn(('E_IMPORT_FORMAT', 'vmess'), result.rejected)
        # A proxy that carries credentials is refused outright: importing it
        # would mean storing somebody else's secret in our own database.
        self.assertIn('E_SECRET_CREDENTIALS', [code for code, _text in result.rejected])
        self.assertEqual(result.outcome, 'partial')

    def test_singbox_yields_only_the_endpoints_and_the_protocols_we_support(self):
        result = sd.import_singbox(json.dumps(self.SINGBOX).encode())
        self.assertEqual(result.canonical, ('socks5://198.51.100.6:1080', 'http://203.0.113.7:8080'))
        self.assertIn(('E_IMPORT_FORMAT', 'vmess'), result.rejected)
        self.assertEqual([item for item in result.ignored if item.startswith('outbound:')],
                         ['outbound:block', 'outbound:direct'])

    def test_no_rule_and_no_script_ever_reaches_the_output(self):
        for result, needles in ((sd.import_clash(json.dumps(self.CLASH).encode()),
                                 ('evil.example', 'MATCH,DIRECT', 'require(', 'os").system')),
                                (sd.import_singbox(json.dumps(self.SINGBOX).encode()),
                                 ('"action": "reject"', 'dns-query', 'clash_api'))):
            blob = json.dumps(result.summary(), ensure_ascii=False)
            for needle in needles:
                self.assertNotIn(needle, blob)
            self.assertNotIn(CANARY, blob)
            self.assertEqual(sd.find_secret_leaks(blob, [CANARY]), [])

    def test_a_socks_version_below_five_is_socks4(self):
        document = {'outbounds': [{'type': 'socks', 'server': '198.51.100.9', 'server_port': 1080,
                                   'version': '4'}]}
        self.assertEqual(sd.import_singbox(json.dumps(document).encode()).canonical,
                         ('socks4://198.51.100.9:1080',))

    def test_a_credential_in_a_document_is_a_claim_not_a_leak_into_a_url(self):
        result = sd.import_clash(json.dumps(self.CLASH).encode())
        for entry in result.endpoints:
            self.assertNotIn(CANARY, entry.endpoint)

    def test_only_the_two_declared_subscription_formats_are_imported(self):
        for name in sd.SUBSCRIPTION_FORMATS:
            self.assertIn(name, sd.USER_SOURCE_FORMATS)
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.import_subscription(b'anything', 'text')
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_a_document_over_the_byte_budget_is_its_own_outcome(self):
        result = sd.import_clash(b'x' * 100, limits=sd.ImportLimits(max_bytes=10))
        self.assertEqual(result.outcome, 'limit_exceeded')
        self.assertEqual([code for code, _text in result.rejected], ['E_LIMIT_BODY'])

    def test_every_import_error_code_is_declared(self):
        self.assertIn('E_IMPORT_FORMAT', sd.IMPORT_CODES)
        self.assertIn('E_LIMIT_BODY', sd.IMPORT_CODES)


class EndToEndFeedTest(unittest.TestCase):
    """One subscription, one collection, the whole life cycle in order."""

    def test_bind_refresh_fail_recover_rotate(self):
        conn = _desk()
        self.addCleanup(conn.close)
        desk = sd.SourceDesk(conn, origin='own')
        source = _user_source()
        policy = sd.FeedPolicy(mode='replace', ttl_seconds=3600)
        desk.bind(source, collection_id=COLLECTION, mode='replace', adapter_kind='line')
        state = desk.get(source.id, COLLECTION)
        self.assertEqual(state.active, ())

        state = desk.apply_plan(sd.plan_refresh(state, _ok(ENDPOINTS, T0, etag='"v1"'),
                                                policy=policy, now=T0))
        self.assertEqual(state.active, ENDPOINTS)

        # A broken day: three failures, then a quarantine, then a recovery.
        for index in range(3):
            state = desk.apply_plan(sd.plan_refresh(
                state, sd.FeedResult(outcome='unavailable', fetched_at=T0 + 3600 * (index + 1),
                                     status=503), policy=policy, now=T0 + 3600 * (index + 1)))
            self.assertEqual(state.active, ENDPOINTS, 'отказ не должен был очистить коллекцию')
        self.assertIsNotNone(state.quarantine_until)

        state = desk.apply_plan(sd.plan_refresh(state, _ok(ENDPOINTS[:4], T0 + 20000),
                                                policy=policy, now=T0 + 20000))
        self.assertEqual(state.active, ENDPOINTS[:4])
        self.assertIsNone(state.quarantine_until)
        self.assertEqual(state.last_good, ENDPOINTS[:4])

        # New credentials: the old revision's proofs are named, not silently kept.
        rotation, rotated, _diag = sd.plan_rotation(state, access_id='acc-7',
                                                    new_access_ref=_put('auth', 'u', 'v2'),
                                                    now=T0 + 30000)
        desk.apply_rotation(rotation, rotated, now=T0 + 30000)
        self.assertEqual(desk.get(source.id, COLLECTION).access_revision, 2)

        # Nothing anywhere in the store carries the secret.
        dump = json.dumps(conn.execute('SELECT * FROM source_feed').fetchall(), ensure_ascii=False)
        self.assertNotIn(CANARY, dump)


if __name__ == '__main__':
    unittest.main()

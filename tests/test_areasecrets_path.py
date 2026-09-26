"""F04 through the module that owns it: the access path, end to end and offline.

The acceptance line of F04 is "импорт → worker check → scoped gateway →
поддерживаемый explicit-secret export работают; обычные логи/экспорты/БД не
содержат canary secret", and the next one is scenario 7 of the master prompt:
"Hostname/auth проходят весь путь; два доступа к одному endpoint не сливаются;
смена пароля отзывает старое доказательство."

These tests drive `secrets.py` the way a transport will: an import line is split
into an address and a credential, the credential becomes an access identity, the
identity is resolved into wire material, a measurement is admitted only for the
exact `(access_id, access_revision)` pair, and a rotation makes the old evidence
and the old password stop working.  Nothing here opens a socket: every address is
RFC 5737 documentation space, so a failure is always the module's answer and
never a request to somebody's proxy.

The canary is a fixed, obviously fake string.  Every test that touches it also
asserts where it must not appear, so a regression that starts writing it out
fails here rather than in a report.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import secrets as s
from proxy_workbench import core

try:
    from proxy_workbench import db
except ImportError:  # db.py has not landed yet
    db = None

#: Documentation addresses (RFC 5737 / RFC 3849).  Nothing here is reachable.
HOSTNAME = 'proxy.example.com'
IPV4 = '198.51.100.7'
IPV6 = '[2001:db8::1]'
CANARY = 'CANARY-PROXY-PASSWORD-do-not-leak'
CANARY_2 = 'CANARY-SECOND-PASSWORD-do-not-leak'
CANARY_ROTATED = 'CANARY-ROTATED-PASSWORD-do-not-leak'
FAST = 1000


def open_db(path):
    if db is None:
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            'CREATE TABLE endpoints (id TEXT PRIMARY KEY, canonical TEXT UNIQUE NOT NULL);'
            'CREATE TABLE accesses (id TEXT PRIMARY KEY, endpoint_id TEXT NOT NULL'
            ' REFERENCES endpoints(id), mode TEXT NOT NULL, secret_ref TEXT,'
            ' access_revision INTEGER NOT NULL, created_at REAL, rotated_at REAL);')
        return conn
    return db.open_db(path)[0]


def add_endpoint(conn, canonical, endpoint_id):
    if db is not None:
        return db.upsert_endpoint(conn, canonical)
    conn.execute('INSERT INTO endpoints (id, canonical) VALUES (?, ?)', (endpoint_id, canonical))
    conn.commit()
    return endpoint_id


class AccessPathTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / 'db.sqlite3'
        self.conn = open_db(self.path)
        self.addCleanup(self.conn.close)
        self.vault = s.MemoryVault()
        self.store = s.AccessStore(self.conn, self.vault)

    def endpoint(self, canonical):
        return add_endpoint(self.conn, canonical, 'ep-%d' % (len(canonical),))

    def make_access(self, canonical, *, username, password, mode=None, access_id=None):
        endpoint = s.parse_endpoint(canonical)
        endpoint_id = self.endpoint(canonical)
        return self.store.create(endpoint_id, endpoint.scheme, username=username,
                                 password=password, mode=mode, access_id=access_id)

    def rows(self):
        return [dict(row) for row in self.conn.execute('SELECT %s FROM accesses' % s.COLUMNS)]

    def assertNoCanary(self, *needles, where=''):
        blob = self.path.read_bytes()
        for needle in needles:
            self.assertNotIn(needle.encode(), blob, 'canary reached the database %s' % where)


class ImportLineTests(AccessPathTestCase):
    """The first step of F04: an import line becomes an address plus a vault entry."""

    def test_a_credentialed_line_splits_into_an_address_and_a_stored_credential(self):
        for scheme, host in (('http', HOSTNAME), ('https', HOSTNAME),
                             ('socks5', HOSTNAME), ('socks5h', IPV4)):
            with self.subTest(scheme=scheme, host=host):
                authority = host if host.startswith('[') else host
                line = '%s://alice:%s@%s:%d' % (scheme, CANARY, authority, 1080 if 'socks' in scheme else 8080)
                bare, username, password = s.split_userinfo(line)
                self.assertEqual(username, 'alice')
                self.assertEqual(password, CANARY)
                self.assertNotIn(CANARY, bare, 'the address the collection sees carries no userinfo')
                endpoint = s.parse_endpoint(bare)
                self.assertEqual(endpoint.scheme, scheme)
                self.assertEqual(endpoint.canonical, bare)

    def test_percent_encoded_credentials_are_decoded_once(self):
        _bare, username, password = s.split_userinfo(
            'http://alice%40corp:p%40ss%2Fword@' + HOSTNAME + ':8080')
        self.assertEqual((username, password), ('alice@corp', 'p@ss/word'))

    def test_hostname_idna_ipv4_and_ipv6_all_parse(self):
        cases = {
            'http://%s:8080' % HOSTNAME: ('proxy.example.com', 8080, None),
            'https://xn--e1afmkfd.xn--p1ai:443': ('xn--e1afmkfd.xn--p1ai', 443, None),
            'http://%s:8080' % IPV4: ('198.51.100.7', 8080, 4),
            'http://%s:8080' % IPV6: ('2001:db8::1', 8080, 6),
        }
        for value, (host, port, version) in cases.items():
            with self.subTest(value=value):
                endpoint = s.parse_endpoint(value)
                self.assertEqual((endpoint.host, endpoint.port, endpoint.ip_version), (host, port, version))
                self.assertNotIn(CANARY, endpoint.canonical)

    def test_an_address_with_userinfo_is_refused_and_the_password_is_not_echoed(self):
        with self.assertRaises(s.SecretValidationError) as caught:
            s.parse_endpoint('http://alice:%s@%s:8080' % (CANARY, HOSTNAME))
        self.assertNotIn(CANARY, str(caught.exception.describe()))
        self.assertTrue(caught.exception.describe()['action'])

    def test_an_idna_name_and_its_punycode_form_are_one_address(self):
        unicode_form = s.parse_endpoint('https://пример.рф:443')
        punycode = s.parse_endpoint('https://xn--e1afmkfd.xn--p1ai:443')
        self.assertEqual(unicode_form, punycode)


class TwoCredentialsOneEndpointTests(AccessPathTestCase):
    """CONTRACTS §1.2(1): an access is not an endpoint."""

    def test_two_passwords_of_one_address_are_two_rows_and_two_refs(self):
        first = self.make_access('http://%s:8080' % HOSTNAME, username='alice',
                                 password=CANARY, access_id='acc-a')
        second = self.make_access('http://%s:8080' % HOSTNAME, username='alice',
                                  password=CANARY_2, access_id='acc-b')
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.secret_ref, second.secret_ref)
        self.assertEqual(len(self.rows()), 2, 'the second identity is additive, never an overwrite')
        with self.store.resolve('acc-a') as a, self.store.resolve('acc-b') as b:
            self.assertEqual((a.password, b.password), (CANARY, CANARY_2))
            self.assertNotEqual(a.password, b.password)
        self.assertNoCanary(CANARY, CANARY_2)

    def test_the_verifier_of_one_password_does_not_accept_the_other(self):
        self.make_access('http://%s:8080' % HOSTNAME, username='alice',
                         password=CANARY, access_id='acc-a')
        self.make_access('http://%s:8080' % HOSTNAME, username='alice',
                         password=CANARY_2, access_id='acc-b')
        self.assertTrue(self.store.verify_password('acc-a', CANARY))
        self.assertFalse(self.store.verify_password('acc-a', CANARY_2))
        self.assertTrue(self.store.verify_password('acc-b', CANARY_2))
        self.assertFalse(self.store.verify_password('acc-b', CANARY))

    def test_find_by_username_offers_the_conflict_instead_of_overwriting(self):
        self.make_access('http://%s:8080' % HOSTNAME, username='alice',
                         password=CANARY, access_id='acc-a')
        endpoint_id = self.rows()[0]['endpoint_id']
        found = self.store.find_by_username(endpoint_id, 'alice')
        self.assertIsNotNone(found)
        self.assertEqual(found.id, 'acc-a')
        self.assertIsNone(self.store.find_by_username(endpoint_id, 'bob'))
        self.assertIsNone(self.store.find_by_username(endpoint_id, None))

    def test_an_open_proxy_keeps_its_own_credential_free_identity(self):
        endpoint_id = self.endpoint('http://%s:8081' % HOSTNAME)
        open_access = self.store.create(endpoint_id, 'http', access_id='acc-open')
        self.assertIsNone(open_access.secret_ref)
        self.assertTrue(self.store.usable(open_access))
        self.assertEqual(open_access.mode, s.MODE_NONE)
        credentialed = self.make_access('http://%s:8080' % HOSTNAME, username='alice',
                                        password=CANARY, access_id='acc-a')
        self.assertTrue(self.store.usable(credentialed))
        self.assertNotEqual(credentialed.mode, open_access.mode)


class RotationRevokesTheOldEvidenceTests(AccessPathTestCase):
    """The F04 rule: a new password cannot inherit a successful check of the old one."""

    def setUp(self):
        super().setUp()
        self.access = self.make_access('socks5://%s:1080' % HOSTNAME, username='alice',
                                      password=CANARY, access_id='acc-1')
        self.scope = core.Scope(collection_id='c1', profile_id='fx', profile_revision=1,
                                network_id='default')
        self.policy = core.Policy()

    def observation(self, access_revision):
        return {'collection_id': 'c1', 'profile_id': 'fx', 'profile_revision': 1,
                'network_id': 'default', 'access_id': 'acc-1', 'access_revision': access_revision}

    def test_a_rotation_bumps_the_revision_and_the_old_evidence_stops_admitting(self):
        current = core.Access(access_id='acc-1', access_revision=self.access.access_revision)
        self.assertIsNone(core._identity_reason(self.observation(1), self.scope, current, self.policy))
        rotated = self.store.rotate('acc-1', password=CANARY_ROTATED)
        self.assertEqual(rotated.access_revision, 2)
        self.assertIsNotNone(rotated.rotated_at)
        after = core.Access(access_id='acc-1', access_revision=rotated.access_revision)
        code, detail = core._identity_reason(self.observation(1), self.scope, after, self.policy)
        self.assertEqual(code, 'E_CONFLICT_ACCESS_REVISION')
        self.assertEqual(detail['access_revision'], 1)
        self.assertIsNone(core._identity_reason(self.observation(2), self.scope, after, self.policy))

    def test_a_rotation_makes_the_old_password_stop_working(self):
        self.assertTrue(self.store.verify_password('acc-1', CANARY))
        self.store.rotate('acc-1', password=CANARY_ROTATED)
        self.assertFalse(self.store.verify_password('acc-1', CANARY))
        self.assertTrue(self.store.verify_password('acc-1', CANARY_ROTATED))
        with self.store.resolve('acc-1') as resolved:
            self.assertEqual(resolved.password, CANARY_ROTATED)

    def test_resolving_a_superseded_revision_is_a_named_conflict(self):
        self.store.rotate('acc-1', password=CANARY_ROTATED)
        with self.assertRaises(s.SecretConflictError) as caught:
            self.store.resolve('acc-1', access_revision=1)
        self.assertEqual(caught.exception.code, s.E_CONFLICT)
        self.assertIn('check the endpoint again', caught.exception.action)

    def test_a_rotation_against_a_stale_expectation_changes_nothing(self):
        with self.assertRaises(s.SecretConflictError):
            self.store.rotate('acc-1', password=CANARY_ROTATED, expect_revision=99)
        self.assertEqual(self.store.get('acc-1').access_revision, 1)
        with self.store.resolve('acc-1') as resolved:
            self.assertEqual(resolved.password, CANARY, 'the failed rotation left the old secret in place')
        self.assertNoCanary(CANARY, CANARY_ROTATED)

    def test_a_rotation_keeps_the_reference_so_a_superseded_one_is_recognisable(self):
        before = self.store.get('acc-1').secret_ref
        after = self.store.rotate('acc-1', password=CANARY_ROTATED)
        self.assertEqual(after.secret_ref, before)

    def test_the_rotated_secret_is_not_recoverable_from_the_row(self):
        self.store.rotate('acc-1', password=CANARY_ROTATED)
        row = self.rows()[0]
        self.assertNotIn(CANARY_ROTATED, json.dumps(row, default=str))
        self.assertNotIn(CANARY, json.dumps(row, default=str))
        self.assertNoCanary(CANARY, CANARY_ROTATED)


class VaultAndDatabaseAreNotOneTransactionTests(AccessPathTestCase):
    def test_a_failed_insert_compensates_the_staged_reference(self):
        class Failing(sqlite3.Connection):
            pass

        class Broken:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if 'INSERT INTO accesses' in sql:
                    raise sqlite3.OperationalError('disk I/O error (simulated)')
                return self._conn.execute(sql, *args)

            def commit(self):
                return self._conn.commit()

        endpoint_id = self.endpoint('http://%s:8080' % HOSTNAME)
        store = s.AccessStore(Broken(self.conn), self.vault)
        with self.assertRaises(sqlite3.OperationalError):
            store.create(endpoint_id, 'http', username='alice', password=CANARY)
        self.assertEqual(self.vault.refs(), [], 'a failed create leaves no reference nobody owns')
        self.assertEqual(self.rows(), [])

    def test_a_crash_between_the_vault_and_the_row_is_repaired_by_reconcile(self):
        access = self.make_access('http://%s:8080' % HOSTNAME, username='alice',
                                  password=CANARY, access_id='acc-1')
        # Simulate the crash: the vault holds a reference the row does not own.
        orphan = s.new_reference()
        self.vault.stage(orphan, s.SecretPayload('alice', CANARY_2, s.make_verifier(CANARY_2, iterations=FAST)))
        self.vault.mark_ready(orphan)
        report = self.store.reconcile()
        self.assertIn(orphan, report.removed_orphan_refs)
        self.assertNotIn(orphan, self.vault.refs())
        with self.store.resolve('acc-1') as resolved:
            self.assertEqual(resolved.password, CANARY, 'reconcile did not touch the real credential')

    def test_a_row_left_staged_is_finalized_and_reconcile_is_idempotent(self):
        access = self.make_access('http://%s:8080' % HOSTNAME, username='alice',
                                  password=CANARY, access_id='acc-1')
        self.vault._entries[access.secret_ref]['state'] = s.STATE_STAGED
        first = self.store.reconcile()
        self.assertIn('acc-1', first.finalized_refs)
        self.assertEqual(self.store.state(self.store.get('acc-1')), s.STATE_READY)
        second = self.store.reconcile()
        self.assertTrue(second.clean)

    def test_a_row_whose_reference_vanished_is_reported_and_not_invented(self):
        self.make_access('http://%s:8080' % HOSTNAME, username='alice',
                         password=CANARY, access_id='acc-1')
        self.vault.close()
        report = self.store.reconcile()
        self.assertIn('acc-1', report.missing_refs)
        self.assertFalse(report.clean)
        self.assertFalse(self.store.usable(self.store.get('acc-1')))

    def test_a_locked_vault_blocks_every_read_and_reconciliation_defers(self):
        self.make_access('http://%s:8080' % HOSTNAME, username='alice',
                         password=CANARY, access_id='acc-1')
        self.vault.lock()
        access = self.store.get('acc-1')
        self.assertEqual(self.store.state(access), s.STATE_LOCKED)
        self.assertFalse(self.store.usable(access))
        with self.assertRaises(s.SecretVaultLocked) as caught:
            self.store.resolve('acc-1')
        self.assertEqual(caught.exception.code, s.E_VAULT_LOCKED)
        report = self.store.reconcile()
        self.assertIn('acc-1', report.locked_refs)
        self.assertTrue(self.vault.locked)
        self.vault.unlock()
        with self.store.resolve('acc-1') as resolved:
            self.assertEqual(resolved.password, CANARY)

    def test_a_purge_removes_the_row_and_the_secret(self):
        self.make_access('http://%s:8080' % HOSTNAME, username='alice',
                         password=CANARY, access_id='acc-1')
        self.assertTrue(self.store.purge('acc-1'))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.vault.refs(), [])
        self.assertFalse(self.store.purge('acc-1'))


class AuthOutcomeTests(AccessPathTestCase):
    """F04: an upstream that wants auth, a credential it rejected and a locked
    store are three different answers, not one."""

    def test_a_407_without_credentials_sent_is_auth_required(self):
        error = s.classify_upstream(407, state=s.STATE_READY, credentials_sent=False)
        self.assertIsInstance(error, s.SecretAuthRequired)
        self.assertEqual(error.code, s.E_AUTH_REQUIRED)

    def test_a_407_with_credentials_sent_is_auth_failed(self):
        error = s.classify_upstream(407, state=s.STATE_READY, credentials_sent=True)
        self.assertIsInstance(error, s.SecretAuthFailed)
        self.assertEqual(error.code, s.E_AUTH_FAILED)

    def test_a_locked_store_is_its_own_outcome(self):
        error = s.classify_upstream(407, state=s.STATE_LOCKED, credentials_sent=True)
        self.assertIsInstance(error, s.SecretVaultLocked)

    def test_a_missing_credential_is_not_an_auth_failure(self):
        error = s.classify_upstream(200, state=s.STATE_MISSING)
        self.assertIsInstance(error, s.SecretNotProvided)
        self.assertNotIsInstance(error, s.SecretAuthFailed)

    def test_a_healthy_upstream_produces_no_error(self):
        self.assertIsNone(s.classify_upstream(200, state=s.STATE_READY, credentials_sent=True))
        self.assertIsNone(s.classify_upstream(None, state=s.STATE_NO_REF))

    def test_a_described_error_never_carries_the_userinfo_of_a_proxy_url(self):
        error = s.SecretAuthFailed('rejected by http://alice:%s@%s:8080' % (CANARY, HOSTNAME))
        described = error.describe()
        self.assertNotIn(CANARY, json.dumps(described, default=str))


class RefusedMechanismTests(unittest.TestCase):
    """F04 excludes these by contract, and they must be refused, not ignored."""

    def test_socks4_credentials_are_refused(self):
        with self.assertRaises(s.SecretUnsupportedError) as caught:
            s.auth_mode_for('socks4', username='alice', password=CANARY)
        self.assertIn('SOCKS4', caught.exception.message)
        self.assertEqual(s.auth_mode_for('socks4'), s.MODE_NONE, 'an open socks4 is not a credential')

    def test_an_unknown_scheme_is_refused_by_name(self):
        for scheme in ('ntlm', 'kerberos', 'gssapi', 'socks5-negotiate'):
            with self.subTest(scheme=scheme):
                with self.assertRaises(s.SecretUnsupportedError):
                    s.auth_mode_for(scheme, username='alice', password=CANARY)

    def test_a_half_credential_is_refused(self):
        with self.assertRaises(s.SecretValidationError):
            s.auth_mode_for('http', username='alice', password=None)
        with self.assertRaises(s.SecretValidationError):
            s.auth_mode_for('socks5', username=None, password=CANARY)

    def test_a_mode_is_checked_against_the_scheme_it_will_be_used_with(self):
        with self.assertRaises(s.SecretValidationError) as caught:
            s.check_mode_scheme(s.MODE_SOCKS5, 'http')
        self.assertIn('separate access identity', caught.exception.action)
        self.assertEqual(s.check_mode_scheme(s.MODE_HTTP_BASIC, 'https'), s.MODE_HTTP_BASIC)


class DestinationPolicyTests(unittest.TestCase):
    """Public discovery stays public; a private address needs an explicit mode."""

    def test_the_public_policy_refuses_private_loopback_and_link_local(self):
        policy = s.DestinationPolicy.public()
        for address in (IPV4, '10.0.0.5', '127.0.0.1', '169.254.169.254', '::1', 'fc00::1'):
            with self.subTest(address=address):
                self.assertFalse(policy.check_address(address))

    def test_a_hostname_needs_a_trusted_collection(self):
        endpoint = s.parse_endpoint('http://%s:8080' % HOSTNAME)
        self.assertFalse(s.DestinationPolicy.public().check_endpoint(endpoint))
        self.assertTrue(s.DestinationPolicy.trusted_private().check_endpoint(endpoint))

    def test_a_trusted_collection_still_refuses_a_host_outside_its_allowlist(self):
        endpoint = s.parse_endpoint('http://%s:8080' % HOSTNAME)
        policy = s.DestinationPolicy.trusted_private(allowed_hosts=['other.example.com'])
        decision = policy.check_endpoint(endpoint)
        self.assertFalse(decision)
        self.assertEqual(decision.reason, s.REASON_HOST_NOT_ALLOWED)
        self.assertTrue(s.DestinationPolicy.trusted_private(allowed_hosts=[HOSTNAME]).check_endpoint(endpoint))

    def test_loopback_and_link_local_are_a_second_explicit_opt_in(self):
        endpoint = s.parse_endpoint('http://127.0.0.1:8080')
        loopback = s.DestinationPolicy.trusted_private()
        self.assertFalse(loopback.check_endpoint(endpoint))
        self.assertTrue(s.DestinationPolicy.trusted_private(allow_loopback=True).check_endpoint(endpoint))
        link = s.DestinationPolicy.trusted_private(allow_private_networks=True, allow_link_local=True)
        self.assertTrue(link.check_address('169.254.169.254'),
                        'the metadata address is still a second, separate opt-in')

    def test_every_address_a_hostname_resolves_to_must_pass(self):
        """A trusted collection may reach a private network, but not by name alone.

        The name is in the allowlist and the private range is allowed, so those
        two pass on their own.  What the guard adds is that *every* answer has to
        pass: one good address and one loopback answer is a refusal, and so is
        one good address and the cloud metadata address.
        """
        policy = s.DestinationPolicy.trusted_private(allowed_hosts=[HOSTNAME])
        self.assertTrue(policy.check_resolved(HOSTNAME, [IPV4, '2001:db8::2']))
        self.assertTrue(policy.check_resolved(HOSTNAME, ['10.1.2.3']),
                        'a private answer is what a trusted collection opted into')
        for answers in ([IPV4, '127.0.0.1'], [IPV4, '169.254.169.254'], ['127.0.0.1']):
            with self.subTest(answers=answers):
                rebind = policy.check_resolved(HOSTNAME, answers)
                self.assertFalse(rebind)
                self.assertEqual(rebind.reason, s.REASON_DNS_FALLBACK)
                self.assertIn(rebind.address, [a.strip('[]') for a in answers])

    def test_authorize_endpoint_consults_the_resolver_before_allowing(self):
        policy = s.DestinationPolicy.trusted_private(allowed_hosts=[HOSTNAME])
        endpoint = s.parse_endpoint('http://%s:8080' % HOSTNAME)
        self.assertFalse(s.authorize_endpoint(policy, endpoint,
                                              resolver=lambda host: [IPV4, '127.0.0.1']))
        self.assertTrue(s.authorize_endpoint(policy, endpoint,
                                             resolver=lambda host: [IPV4, '2001:db8::2']))
        # Without a resolver the name is judged on its own, which is the case a
        # caller must not reach by forgetting to pass one for a trusted mode.
        self.assertTrue(policy.check_endpoint(endpoint))


class RedactionTests(unittest.TestCase):
    def test_a_described_access_carries_the_reference_and_not_the_credential(self):
        access = s.Access(id='acc-1', endpoint_id='ep-1', mode=s.MODE_HTTP_BASIC,
                          secret_ref='sec_abc', access_revision=2, created_at=1.0, rotated_at=2.0)
        described = s.describe(access, state=s.STATE_READY)
        self.assertEqual(described['access_id'], 'acc-1')
        self.assertEqual(described['secret_ref'], 'sec_abc')
        self.assertNotIn(CANARY, json.dumps(described, default=str))

    def test_a_scrubbed_mapping_drops_a_credential_under_any_key(self):
        scrubbed = s.scrub_mapping({'proxy': 'http://alice:%s@h:1' % CANARY,
                                    'password': CANARY, 'note': 'ok',
                                    'nested': {'api_key': CANARY}})
        self.assertNotIn(CANARY, json.dumps(scrubbed, default=str))
        self.assertEqual(scrubbed['note'], 'ok')
        self.assertEqual(scrubbed['password'], s.REDACTED)

    def test_a_known_secret_is_removed_from_a_message(self):
        text = 'proxy http://h:1 refused for user %s' % CANARY
        self.assertNotIn(CANARY, s.redact_text(text, [CANARY]))
        self.assertNotIn(CANARY, s.redact_proxy_url('http://alice:%s@h:1' % CANARY))


if __name__ == '__main__':
    unittest.main()

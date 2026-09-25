"""Destination policy: public discovery stays public, trusted private is explicit."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import secrets as s

PUBLIC = ['93.184.216.34', '8.8.8.8', '2606:4700:4700::1111']
NOT_PUBLIC = ['10.0.0.5', '172.16.0.1', '192.168.1.1', '127.0.0.1', '169.254.1.1', '0.0.0.0',
              '224.0.0.1', '2001:db8::1', '::1', 'fe80::1']


class PublicPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = s.DestinationPolicy.public()

    def test_globally_routable_addresses_pass(self):
        for address in PUBLIC:
            with self.subTest(address=address):
                verdict = self.policy.check_address(address)
                self.assertTrue(verdict)
                self.assertEqual(verdict.reason, s.REASON_ALLOWED)

    def test_private_loopback_and_special_ranges_are_refused(self):
        for address in NOT_PUBLIC:
            with self.subTest(address=address):
                self.assertFalse(self.policy.check_address(address))

    def test_loopback_and_link_local_have_their_own_reasons(self):
        for address in ('127.0.0.1', '::1'):
            self.assertEqual(self.policy.check_address(address).reason, s.REASON_LOOPBACK)
        for address in ('169.254.1.1', '169.254.169.254', 'fe80::1'):
            with self.subTest(address=address):
                self.assertEqual(self.policy.check_address(address).reason, s.REASON_NOT_PUBLIC)

    def test_garbage_is_refused_rather_than_guessed(self):
        for value in ('proxy.example.com', '', '999.1.1.1', None, '0x7f000001'):
            with self.subTest(value=value):
                self.assertFalse(self.policy.check_address(value))

    def test_public_discovery_refuses_hostnames(self):
        endpoint = s.parse_endpoint('http://proxy.example.com:8080')
        verdict = self.policy.check_endpoint(endpoint)
        self.assertFalse(verdict)
        self.assertEqual(verdict.reason, s.REASON_HOSTNAME)
        self.assertEqual(verdict.address, 'proxy.example.com')

    def test_public_discovery_allows_public_ip_endpoints(self):
        self.assertTrue(self.policy.check_endpoint(s.parse_endpoint('http://93.184.216.34:8080')))
        self.assertTrue(self.policy.check_endpoint(s.parse_endpoint('socks5://[2606:4700:4700::1111]:1080')))


class TrustedPrivatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = s.DestinationPolicy.trusted_private()

    def test_private_addresses_pass_in_a_trusted_collection(self):
        for address in ('10.0.0.5', '192.168.1.1', '2001:db8::1'):
            with self.subTest(address=address):
                verdict = self.policy.check_address(address)
                self.assertTrue(verdict)
                self.assertEqual(verdict.reason, s.REASON_PRIVATE_ALLOWED)

    def test_loopback_still_needs_an_explicit_opt_in(self):
        self.assertFalse(self.policy.check_address('127.0.0.1'))
        allowed = s.DestinationPolicy.trusted_private(allow_loopback=True)
        self.assertTrue(allowed.check_address('127.0.0.1'))
        self.assertTrue(allowed.check_address('::1'))

    def test_link_local_needs_its_own_opt_in(self):
        """A trusted collection still does not reach the cloud metadata address."""
        for address in ('169.254.169.254', 'fe80::1'):
            with self.subTest(address=address):
                self.assertFalse(self.policy.check_address(address))
        allowed = s.DestinationPolicy.trusted_private(allow_link_local=True)
        self.assertTrue(allowed.check_address('169.254.169.254'))

    def test_hostnames_are_allowed_in_a_trusted_collection(self):
        endpoint = s.parse_endpoint('http://proxy.example.com:8080')
        self.assertTrue(self.policy.check_endpoint(endpoint))

    def test_an_allowlist_narrows_hostnames(self):
        policy = s.DestinationPolicy.trusted_private(allowed_hosts=['proxy.example.com'])
        self.assertTrue(policy.check_endpoint(s.parse_endpoint('http://proxy.example.com:8080')))
        self.assertTrue(policy.check_endpoint(s.parse_endpoint('http://PROXY.example.com.:8080')))
        refused = policy.check_endpoint(s.parse_endpoint('http://other.example.com:8080'))
        self.assertFalse(refused)
        self.assertEqual(refused.reason, s.REASON_HOST_NOT_ALLOWED)

    def test_private_networks_can_stay_off_in_a_trusted_collection(self):
        policy = s.DestinationPolicy.trusted_private(allow_private_networks=False)
        self.assertFalse(policy.check_address('10.0.0.5'))
        self.assertTrue(policy.check_endpoint(s.parse_endpoint('http://proxy.example.com:8080')))


class DnsGuardTests(unittest.TestCase):
    def setUp(self):
        self.policy = s.DestinationPolicy.trusted_private(allowed_hosts=['proxy.example.com'])

    def test_every_resolved_address_is_checked(self):
        good = self.policy.check_resolved('proxy.example.com', ['93.184.216.34', '8.8.8.8'])
        self.assertTrue(good)
        self.assertEqual(good.reason, s.REASON_ALLOWED)

    def test_a_loopback_fallback_after_a_public_answer_is_refused(self):
        """R1: a hostname may not fall back to loopback or link-local, even trusted."""
        for answers in (['93.184.216.34', '127.0.0.1'], ['93.184.216.34', '::1'],
                        ['93.184.216.34', '169.254.169.254'], ['93.184.216.34', 'fe80::1']):
            with self.subTest(answers=answers):
                verdict = self.policy.check_resolved('proxy.example.com', answers)
                self.assertFalse(verdict)
                self.assertEqual(verdict.reason, s.REASON_DNS_FALLBACK)
                self.assertEqual(verdict.address, answers[-1])

    def test_a_private_fallback_follows_the_collection_policy(self):
        """A private answer is a decision of the collection, not of the resolver."""
        trusted = s.DestinationPolicy.trusted_private()
        self.assertTrue(trusted.check_resolved('proxy.example.com', ['93.184.216.34', '10.0.0.5']))
        closed = s.DestinationPolicy.trusted_private(allow_private_networks=False)
        verdict = closed.check_resolved('proxy.example.com', ['93.184.216.34', '10.0.0.5'])
        self.assertFalse(verdict)
        self.assertEqual(verdict.reason, s.REASON_DNS_FALLBACK)

    def test_a_public_policy_refuses_a_hostname_before_resolving(self):
        verdict = s.DestinationPolicy.public().check_resolved('proxy.example.com', ['93.184.216.34'])
        self.assertFalse(verdict)
        self.assertEqual(verdict.reason, s.REASON_HOSTNAME)

    def test_an_allowlist_is_checked_before_the_answers(self):
        verdict = self.policy.check_resolved('other.example.com', ['93.184.216.34'])
        self.assertFalse(verdict)
        self.assertEqual(verdict.reason, s.REASON_HOST_NOT_ALLOWED)


class AuthorizeBeforeCredentialsTests(unittest.TestCase):
    """F04: a disallowed destination must be refused before the secret is handed over."""

    def test_the_secret_is_only_taken_after_the_decision(self):
        reads = []

        class CountingVault(s.MemoryVault):
            def get(self, ref):
                reads.append(ref)
                return super().get(ref)

        vault = CountingVault()
        vault.stage('sec_a', s.SecretPayload('alice', 'canary-secret-7e2b',
                                             s.make_verifier('canary-secret-7e2b', iterations=1000), 1))
        vault.mark_ready('sec_a')
        endpoint = s.parse_endpoint('http://proxy.example.com:8080')
        policy = s.DestinationPolicy.trusted_private()

        # A hostname that answers with a loopback address is refused, and nothing
        # in this path is allowed to reach for the secret on the way to the refusal.
        refused = s.authorize_endpoint(policy, endpoint, resolver=lambda host: ['93.184.216.34', '127.0.0.1'])
        self.assertFalse(refused)
        self.assertEqual(reads, [], 'no secret may be read for a refused destination')

        allowed = s.authorize_endpoint(policy, endpoint, resolver=lambda host: ['93.184.216.34'])
        self.assertTrue(allowed)
        self.assertEqual(vault.get('sec_a').password, 'canary-secret-7e2b')
        self.assertEqual(reads, ['sec_a'])

    def test_authorize_without_a_resolver_only_checks_the_endpoint(self):
        policy = s.DestinationPolicy.trusted_private()
        self.assertTrue(s.authorize_endpoint(policy, s.parse_endpoint('http://10.0.0.5:8080')))
        self.assertFalse(s.authorize_endpoint(s.DestinationPolicy.public(), s.parse_endpoint('http://10.0.0.5:8080')))
        self.assertTrue(s.authorize_endpoint(policy, s.parse_endpoint('http://proxy.example.com:8080'),
                                            resolved=['10.0.0.5']))


class DecisionTests(unittest.TestCase):
    def test_decision_is_truthy_and_serializable(self):
        verdict = s.DestinationPolicy.public().check_address('93.184.216.34')
        self.assertTrue(verdict)
        self.assertEqual(verdict.as_dict(), {'allowed': True, 'reason': s.REASON_ALLOWED,
                                             'address': '93.184.216.34'})
        refused = s.DestinationPolicy.public().check_address('10.0.0.5')
        self.assertFalse(refused)
        self.assertFalse(refused.as_dict()['allowed'])


if __name__ == '__main__':
    unittest.main()

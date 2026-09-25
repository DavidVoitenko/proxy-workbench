"""Defect 14 / R09: correct IPv6 reverse and honest zone outcomes.

No DNS query leaves this file: the resolver is a local fake, so the tests prove
the mapping and the query string, not somebody's blacklist.
"""
import asyncio
import ipaddress
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import probes as pr

ZONE = pr.generic_zone('bl.example')
ZONES = (ZONE, pr.generic_zone('two.example'))


class ReverseTests(unittest.TestCase):
    def test_ipv4_is_reversed_octet_by_octet(self):
        self.assertEqual(pr.reverse_ip('1.2.3.4'), '4.3.2.1.')
        self.assertEqual(pr.reverse_ip('203.0.113.9'), '9.113.0.203.')

    def test_ipv6_is_reversed_as_dotted_nibbles(self):
        reversed_ipv6 = pr.reverse_ip('2001:db8::1')
        expected = '.'.join(reversed(f'{int(ipaddress.IPv6Address("2001:db8::1")):032x}')) + '.'
        self.assertEqual(reversed_ipv6, expected)
        labels = reversed_ipv6.rstrip('.').split('.')
        self.assertEqual(len(labels), 32)
        self.assertTrue(all(len(label) == 1 and label in '0123456789abcdef' for label in labels))

    def test_the_reversed_form_has_no_colon(self):
        for address in ('2001:db8::1', '::1', 'fe80::1', '2001:0db8:0000:0000:0000:0000:0000:0001'):
            with self.subTest(address=address):
                self.assertNotIn(':', pr.reverse_ip(address))

    def test_compressed_and_expanded_forms_agree(self):
        self.assertEqual(pr.reverse_ip('2001:db8::1'), pr.reverse_ip('2001:0db8:0000:0000:0000:0000:0000:0001'))

    def test_the_nibbles_rebuild_the_address(self):
        for address in ('2001:db8::1', '::1', 'fe80::1', '2001:db8:85a3:8d3:1319:8a2e:370:7348'):
            with self.subTest(address=address):
                labels = pr.reverse_ip(address).rstrip('.').split('.')
                packed = ''.join(reversed(labels))
                self.assertEqual(ipaddress.IPv6Address(int(packed, 16)), ipaddress.ip_address(address))

    def test_a_proxy_url_is_accepted(self):
        self.assertEqual(pr.reverse_host('http://[2001:db8::1]:8080'), pr.reverse_ip('2001:db8::1'))
        self.assertEqual(pr.reverse_host('http://1.2.3.4:3128'), '4.3.2.1.')

    def test_a_broken_address_is_an_error(self):
        for value in ('999.1.1.1', 'not-an-ip', '', 'http://1.2.3.4:80'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    pr.reverse_ip(value)
        with self.assertRaises(ValueError):
            pr.reverse_host('nonsense')


class ZoneAnswerTests(unittest.TestCase):
    def listed(self, address='127.0.0.2', zone=None):
        return pr.classify_dnsbl(zone or ZONE, [address])

    def test_a_match_answer_is_listed(self):
        for address in ('127.0.0.1', '127.0.0.2', '127.0.0.10', '127.0.0.11'):
            with self.subTest(address=address):
                result = self.listed(address)
                self.assertEqual(result.status, 'listed', address)
                self.assertEqual(result.address, address)

    def test_reserved_error_answers_are_not_listings(self):
        # 127.255.255.x is the generic "the query itself was blocked" range:
        # no verdict, and certainly not a listing.
        result = self.listed('127.255.255.254')
        self.assertEqual(result.status, 'unknown')
        self.assertEqual(result.code, pr.DNSBL_ACCESS)

    def test_a_zone_declares_its_own_policy_codes(self):
        zone = pr.DnsblZone(name='bl.example', policy_prefixes=('127.0.0.99',))
        self.assertEqual(pr.classify_dnsbl(zone, ['127.0.0.99']).status, 'unknown')
        self.assertEqual(pr.classify_dnsbl(zone, ['127.0.0.99']).code, pr.DNSBL_ACCESS)
        self.assertEqual(pr.classify_dnsbl(zone, ['127.0.0.2']).status, 'listed')
        self.assertEqual(pr.classify_dnsbl(ZONE, ['127.0.0.99']).status, 'listed')

    def test_nxdomain_is_clear_by_default(self):
        result = pr.classify_dnsbl(ZONE, pr.DnsAnswer('NXDOMAIN', ()))
        self.assertEqual(result.status, 'clear')
        self.assertIsNone(result.code)

    def test_a_zone_may_refuse_to_treat_nxdomain_as_clear(self):
        zone = pr.DnsblZone(name='bl.example', nxdomain='unknown')
        result = pr.classify_dnsbl(zone, pr.DnsAnswer('NXDOMAIN', ()))
        self.assertEqual(result.status, 'unknown')
        self.assertEqual(result.code, pr.NO_DNSBL_ZONES)

    def test_zone_specific_codes_are_honoured(self):
        zone = pr.DnsblZone(name='bl.example', listed_codes=('127.0.0.2',), listed_prefixes=(),
                            blocked_prefixes=('127.255.255.',))
        self.assertEqual(pr.classify_dnsbl(zone, ['127.0.0.2']).status, 'listed')
        other = pr.classify_dnsbl(zone, ['127.0.0.5'])
        self.assertEqual(other.status, 'unknown')
        self.assertEqual(other.code, pr.DNSBL_ERROR)
        self.assertEqual(pr.classify_dnsbl(zone, ['127.0.0.2', '127.0.0.9']).status, 'listed')

    def test_servfail_and_refused_are_unknown(self):
        self.assertEqual(pr.classify_dnsbl(ZONE, pr.DnsAnswer('SERVFAIL', ())).code, pr.DNSBL_ERROR)
        refused = pr.classify_dnsbl(ZONE, pr.DnsAnswer('REFUSED', ()))
        self.assertEqual(refused.status, 'unknown')
        self.assertEqual(refused.code, pr.DNSBL_ACCESS)

    def test_an_empty_noerror_answer_is_not_proof_of_absence(self):
        result = pr.classify_dnsbl(ZONE, pr.DnsAnswer('NOERROR', ()))
        self.assertEqual(result.status, 'unknown')
        self.assertEqual(result.code, pr.DNSBL_ERROR)

    def test_a_plain_none_means_the_name_does_not_exist(self):
        self.assertEqual(pr.classify_dnsbl(ZONE, None).status, 'clear')

    def test_statuses_come_from_a_closed_set(self):
        for answer in ([], ['127.0.0.2'], None, pr.DnsAnswer('NXDOMAIN'), {'rcode': 'SERVFAIL', 'addresses': []}):
            with self.subTest(answer=answer):
                self.assertIn(pr.classify_dnsbl(ZONE, answer).status, pr.DNSBL_STATUSES)


class CheckTests(unittest.IsolatedAsyncioTestCase):
    def resolver(self, mapping, error=None):
        queries = []

        def resolve(query):
            queries.append(query)
            if error is not None:
                raise error
            for fragment, answer in mapping.items():
                if query.endswith(fragment):
                    return answer
            return None

        return resolve, queries

    async def test_every_zone_is_queried_with_the_reverse_prefix(self):
        resolve, queries = self.resolver({'bl.example': ['127.0.0.2'], 'two.example': None})
        report = await pr.check_dnsbl('198.51.100.7', ZONES, resolve=resolve)
        self.assertEqual(queries, ['7.100.51.198.bl.example', '7.100.51.198.two.example'])
        self.assertEqual(report.address, '198.51.100.7')
        self.assertEqual(report.queries, 2)
        self.assertEqual([item.status for item in report.zones], ['listed', 'clear'])
        self.assertEqual(report.status, 'listed')
        self.assertTrue(report.listed)

    async def test_an_ipv6_address_is_queried_as_dotted_nibbles(self):
        resolve, queries = self.resolver({'ip6.example': ['127.0.0.2']})
        report = await pr.check_dnsbl('2001:db8::1', (pr.generic_zone('ip6.example'),), resolve=resolve)
        self.assertTrue(queries[0].startswith('1.0.0.0.0.'))
        self.assertTrue(queries[0].endswith('.ip6.example'))
        self.assertNotIn(':', queries[0])
        self.assertEqual(report.status, 'listed')

    async def test_a_quota_error_is_distinct_from_a_listing(self):
        resolve, _ = self.resolver({}, error=pr.DnsQueryError(pr.DNSBL_QUOTA))
        report = await pr.check_dnsbl('198.51.100.7', ZONES, resolve=resolve)
        self.assertEqual({item.status for item in report.zones}, {'unknown'})
        self.assertEqual({item.code for item in report.zones}, {pr.DNSBL_QUOTA})
        self.assertEqual(report.status, 'unknown')
        self.assertFalse(report.listed)
        self.assertTrue(pr.dnsbl_blocks(report, strict=True))
        self.assertFalse(pr.dnsbl_blocks(report, strict=False))

    async def test_an_access_error_is_its_own_code(self):
        resolve, _ = self.resolver({}, error=pr.DnsQueryError(pr.DNSBL_ACCESS))
        report = await pr.check_dnsbl('198.51.100.7', (ZONE,), resolve=resolve)
        self.assertEqual(report.zones[0].code, pr.DNSBL_ACCESS)

    async def test_a_resolver_failure_is_unknown_not_clear(self):
        def boom(query):
            raise OSError('network down')

        report = await pr.check_dnsbl('198.51.100.7', (ZONE,), resolve=boom)
        self.assertEqual(report.zones[0].status, 'unknown')
        self.assertEqual(report.status, 'unknown')

    async def test_a_hanging_resolver_times_out(self):
        async def hang(query):
            await asyncio.sleep(5)

        report = await pr.check_dnsbl('198.51.100.7', (ZONE,), resolve=hang, timeout_s=0.05)
        self.assertEqual(report.zones[0].code, pr.DNSBL_TIMEOUT)
        self.assertEqual(report.status, 'unknown')

    async def test_a_query_budget_marks_the_rest_instead_of_pretending(self):
        resolve, queries = self.resolver({'bl.example': ['127.0.0.2']})
        report = await pr.check_dnsbl('198.51.100.7', ZONES, resolve=resolve, max_queries=1)
        self.assertEqual(len(queries), 1)
        self.assertTrue(report.truncated)
        skipped = report.zones[1]
        self.assertEqual(skipped.status, 'unknown')
        self.assertEqual(skipped.code, pr.BUDGET_EXHAUSTED)
        self.assertFalse(skipped.queried)

    async def test_clean_is_only_reported_when_every_zone_answered(self):
        resolve, _ = self.resolver({'two.example': pr.DnsAnswer('SERVFAIL')}, error=None)
        clean = await pr.check_dnsbl('198.51.100.7', (ZONE,), resolve=resolve)
        self.assertEqual(clean.status, 'clear')
        self.assertFalse(pr.dnsbl_blocks(clean, strict=True))
        mixed = await pr.check_dnsbl('198.51.100.7', ZONES, resolve=resolve)
        self.assertEqual(mixed.status, 'unknown')
        self.assertTrue(pr.dnsbl_blocks(mixed, strict=True))

    async def test_public_view_carries_the_zone_and_the_code(self):
        resolve, _ = self.resolver({'bl.example': ['127.0.0.2']})
        report = await pr.check_dnsbl('198.51.100.7', ZONES, resolve=resolve)
        value = report.to_public()
        self.assertEqual(value['status'], 'listed')
        self.assertEqual([item['zone'] for item in value['zones']], ['bl.example', 'two.example'])


class ZoneValidationTests(unittest.TestCase):
    def test_zone_names_are_validated(self):
        self.assertEqual(pr.generic_zone(' BL.Example. ').name, 'bl.example')
        for bad in ('', '  ', 'a..b', '-bad.example', 'x' * 300, 'bl example'):
            with self.subTest(bad=bad):
                with self.assertRaises((ValueError, pr.ProbeError)):
                    pr.generic_zone(bad)

    def test_zone_lists_come_in_several_shapes(self):
        self.assertEqual([zone.name for zone in pr.validate_dnsbl_zones(['a.example', 'b.example'])],
                         ['a.example', 'b.example'])
        self.assertEqual([zone.name for zone in pr.validate_dnsbl_zones({'enabled': True, 'zones': ['a.example']})],
                         ['a.example'])
        self.assertEqual(pr.validate_dnsbl_zones({'enabled': False, 'zones': ['a.example']}), ())
        self.assertEqual(pr.validate_dnsbl_zones(None), ())
        self.assertEqual(len(pr.validate_dnsbl_zones(['a.example'] * 3)), 1)
        for bad in ('a.example', {'zones': 'a.example'}, {'unknown': 1}, [1],
                    ['a.example'] * 13, [{'name': 'a.example', 'unknown': True}]):
            with self.subTest(bad=bad):
                with self.assertRaises(pr.ProbeError):
                    pr.validate_dnsbl_zones(bad)

    def test_a_plan_carries_its_zones(self):
        plan = pr.build_plan({'mode': 'basic', 'dnsbl': {'enabled': True, 'zones': ['bl.example']}})
        self.assertEqual([zone.name for zone in plan.zones], ['bl.example'])
        self.assertEqual(plan.to_public()['dnsbl_zones'], ['bl.example'])


if __name__ == '__main__':
    unittest.main()

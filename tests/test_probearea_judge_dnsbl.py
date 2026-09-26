"""Defects 13 and 14, run for real: judge classification and DNSBL outcomes.

The judge half runs against the live self-hosted reference probe over loopback
(the bootstrap request, a challenge page, an echo with no address).  The DNSBL
half runs every outcome separately through the resolver seam: no public zone
is ever queried.
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from probearea_support import LoopbackTransport, reference_probe
from proxy_workbench import anonymity, probes as pr, reputation


class JudgeOnARealEndpoint(unittest.IsolatedAsyncioTestCase):
    """The judge is fetched over a socket, not from a fixture string."""

    async def asyncSetUp(self):
        self._running = pr.serve_reference_probe()
        self.info = self._running.__enter__()
        self.base = self.info['url']
        self.transport = LoopbackTransport()
        self.options = pr.validate_options({'preset': 'frugal'})

    async def asyncTearDown(self):
        await asyncio.to_thread(self._running.__exit__, None, None, None)

    async def fetch(self, path='/echo'):
        response = await self.transport.send(
            pr.ProbeRequest(url=f'{self.base}{path}', max_bytes=65_536), options=self.options)
        return response

    async def test_a_live_bootstrap_gives_our_own_address(self):
        response = await self.fetch()
        self.assertEqual(response.status, 200)
        own = set(pr.extract_addresses(response.body))
        self.assertIn('127.0.0.1', own)
        # A self-hosted judge may echo a loopback address, so the old
        # "only global addresses" filter would have thrown the answer away.
        self.assertEqual(anonymity.extract_public_ips(response.body), set())
        self.assertIn('127.0.0.1', anonymity.extract_public_ips(response.body, global_only=False))

    async def test_elite_only_after_a_confirmed_echo(self):
        response = await self.fetch()
        own = set(pr.extract_addresses(response.body))
        verified = pr.classify_echo(response.body, own, judge_verified=True)
        self.assertEqual(verified.level, 'transparent', 'our own address is in the echo')
        unverified = pr.classify_echo(response.body, own, judge_verified=False)
        self.assertEqual(unverified.level, 'transparent')
        no_baseline = pr.classify_echo(response.body, set())
        self.assertEqual(no_baseline.level, 'unknown')
        self.assertEqual(no_baseline.code, pr.JUDGE_UNVERIFIED)
        self.assertIn('no_baseline', no_baseline.signals)

    async def test_a_challenge_page_is_unknown_not_elite(self):
        response = await self.fetch(pr.REFERENCE_PROBE_NEGATIVE['judge_challenge'])
        self.assertEqual(response.status, 503)
        outcome = pr.classify_echo(response.body, {'127.0.0.1'})
        self.assertEqual(outcome.level, 'unknown')
        self.assertEqual(outcome.code, pr.JUDGE_CHALLENGE)
        self.assertIn('challenge_page', outcome.signals)

    async def test_an_answer_with_no_address_is_unknown_not_elite(self):
        for body in (b'', b'   \n ', b'<html><body>hello</body></html>'):
            with self.subTest(body=body[:20]):
                outcome = pr.classify_echo(body, {'127.0.0.1'})
                self.assertEqual(outcome.level, 'unknown')
                self.assertEqual(outcome.code, pr.JUDGE_INVALID)

    def test_the_shared_classifier_is_the_one_anonymity_uses(self):
        detail = anonymity.classify_detail(b'', {'198.51.100.9'})
        self.assertEqual(detail['level'], 'unknown')
        self.assertEqual(detail['code'], pr.JUDGE_INVALID)
        # The historical two-key shape is unchanged for existing callers.
        self.assertEqual(anonymity.classify(b'', {'198.51.100.9'}),
                         {'level': 'unknown', 'signals': ['empty_body']})
        elite = anonymity.classify_detail(b'{"origin":"203.0.113.7"}', {'198.51.100.9'})
        self.assertEqual(elite['level'], 'elite')
        self.assertTrue(elite['confirmed'])
        leak = anonymity.classify_detail(b'{"origin":"198.51.100.9"}', {'198.51.100.9'})
        self.assertEqual(leak['level'], 'transparent')
        headers = anonymity.classify_detail(b'{"origin":"203.0.113.7","via":"1.1 squid"}',
                                            {'198.51.100.9'})
        self.assertEqual(headers['level'], 'anonymous')
        self.assertIn('via', headers['signals'])

    def test_a_required_level_without_a_judge_is_an_error_not_any(self):
        for minimum in ('transparent', 'anonymous', 'elite'):
            with self.subTest(level=minimum):
                with self.assertRaises(pr.ProbeError) as caught:
                    pr.build_plan({'mode': 'tcp', 'min_anonymity': minimum})
                self.assertEqual(caught.exception.code, pr.E_VALIDATION_ANONYMITY_REQUIRED)
                self.assertIn('Молча проверять без judge нельзя', str(caught.exception))
        plan = pr.build_plan({'mode': 'tcp', 'min_anonymity': 'any'})
        self.assertEqual(plan.min_anonymity, 'any')
        judged = pr.build_plan({'mode': 'tcp', 'min_anonymity': 'elite',
                                'anonymity': {'url': 'http://127.0.0.1:1/echo'}})
        self.assertIsNotNone(judged.judge)


class DnsblOutcomes(unittest.IsolatedAsyncioTestCase):
    ZONE = pr.generic_zone('bl.example')

    async def one(self, answer, zone=None, **kwargs):
        async def resolve(query):
            self.queries.append(query)
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.queries = []
        out = await reputation.check_dnsbl('198.51.100.7', [zone or self.ZONE], 2.0,
                                           resolver=resolve, **kwargs)
        return out[0], self.queries

    async def test_ipv6_reverse_is_thirty_two_dotted_nibbles(self):
        import ipaddress
        for address in ('2001:db8::1', '::1', 'fe80::200:5eff:fe00:5213'):
            with self.subTest(address=address):
                prefix = reputation.reverse_ip(address)
                self.assertNotIn(':', prefix)
                labels = prefix.rstrip('.').split('.')
                self.assertEqual(len(labels), 32)
                self.assertTrue(all(len(label) == 1 for label in labels))
                packed = f'{int(ipaddress.ip_address(address)):032x}'
                self.assertEqual(''.join(reversed(labels)), packed)
        self.assertEqual(reputation.reverse_ip('2001:db8::1'),
                         reputation.reverse_ip('2001:0db8:0000:0000:0000:0000:0000:0001'))
        self.assertEqual(reputation.reverse_ip('1.2.3.4'), '4.3.2.1.')

    async def test_ipv6_query_reaches_the_zone_with_nibbles(self):
        outcome, queries = await self.one({'rcode': 'NOERROR', 'addresses': ['127.0.0.2']})
        self.assertEqual(outcome['status'], 'listed')
        self.assertTrue(queries[0].endswith('.bl.example'))
        self.assertNotIn(':', queries[0])

    async def test_every_outcome_is_distinct(self):
        cases = [
            ('listed', {'rcode': 'NOERROR', 'addresses': ['127.0.0.2']}, 'listed', None),
            ('clear', {'rcode': 'NXDOMAIN', 'addresses': []}, 'clear', None),
            ('blocked 127.255.255.x', {'rcode': 'NOERROR', 'addresses': ['127.255.255.254']},
             'unknown', 'DNSBL_ACCESS'),
            ('quota', pr.DnsQueryError('DNSBL_QUOTA'), 'unknown', 'DNSBL_QUOTA'),
            ('refused by resolver', pr.DnsQueryError('DNSBL_ACCESS'), 'unknown', 'DNSBL_ACCESS'),
            ('SERVFAIL', {'rcode': 'SERVFAIL', 'addresses': []}, 'unknown', 'DNSBL_ERROR'),
            ('REFUSED', {'rcode': 'REFUSED', 'addresses': []}, 'unknown', 'DNSBL_ACCESS'),
            ('NOERROR without addresses', {'rcode': 'NOERROR', 'addresses': []}, 'unknown', 'DNSBL_ERROR'),
        ]
        for label, answer, status, code in cases:
            with self.subTest(case=label):
                outcome, _ = await self.one(answer)
                self.assertEqual(outcome['status'], status, label)
                self.assertEqual(outcome.get('error'), code, label)

    async def test_a_zone_declares_its_own_answer_codes(self):
        zone = pr.DnsblZone(name='bl.example', blocked_codes=('127.0.0.254',),
                            blocked_prefixes=('127.255.255.',), nxdomain='unknown')
        generic, _ = await self.one({'rcode': 'NOERROR', 'addresses': ['127.0.0.254']})
        specific, _ = await self.one({'rcode': 'NOERROR', 'addresses': ['127.0.0.254']}, zone=zone)
        # The same answer means "listed" to a zone that says nothing and
        # "access denied" to a zone that declares the code.
        self.assertEqual(generic['status'], 'listed')
        self.assertEqual(specific['status'], 'unknown')
        self.assertEqual(specific['error'], 'DNSBL_ACCESS')
        nx, _ = await self.one({'rcode': 'NXDOMAIN', 'addresses': []}, zone=zone)
        self.assertEqual(nx['status'], 'unknown')

    async def test_the_query_budget_leaves_later_zones_untouched(self):
        zones = tuple(pr.generic_zone(f'z{index}.example') for index in range(3))
        async def resolve(query):
            return {'rcode': 'NXDOMAIN', 'addresses': []}
        out = await reputation.check_dnsbl('198.51.100.7', zones, 2.0, resolver=resolve,
                                           max_queries=1)
        self.assertEqual(out[0]['status'], 'clear')
        for item in out[1:]:
            self.assertEqual(item['status'], 'unknown')
            self.assertEqual(item['error'], 'BUDGET_EXHAUSTED')
            self.assertFalse(item['queried'])

    async def test_the_legacy_resolver_shape_still_works(self):
        async def listed(query):
            return [(2, 1, 6, '', ('127.0.0.2', 0))]
        async def clear(query):
            return []
        self.assertEqual((await reputation.check_dnsbl('198.51.100.7', ['z.example'], 2.0,
                                                       resolver=listed))[0]['status'], 'listed')
        self.assertEqual((await reputation.check_dnsbl('198.51.100.7', ['z.example'], 2.0,
                                                       resolver=clear))[0]['status'], 'clear')

    async def test_clean_requires_every_zone_to_have_answered_clear(self):
        denylist = reputation.Denylist.empty()
        policy = reputation.make_policy(
            {'dnsbl_enabled': True, 'dnsbl_zones': ['a.example', 'b.example']}, denylist)
        cases = [
            ('all clear', lambda q: {'rcode': 'NXDOMAIN', 'addresses': []}, 'clean', None),
            ('one listed', lambda q: {'rcode': 'NOERROR', 'addresses': ['127.0.0.2']}, 'listed', None),
            ('one blocked', lambda q: {'rcode': 'NOERROR', 'addresses': ['127.255.255.254']},
             'unknown', 'DNSBL_ACCESS'),
            ('one quota', lambda q: pr.DnsQueryError('DNSBL_QUOTA'), 'unknown', 'DNSBL_QUOTA'),
        ]
        for label, resolver, status, error in cases:
            with self.subTest(case=label):
                async def answer(query, _resolver=resolver):
                    value = _resolver(query)
                    if isinstance(value, Exception):
                        raise value
                    return value
                verdict = await reputation.screen_proxy('http://198.51.100.7:8080', policy,
                                                        denylist, resolver=answer)
                self.assertEqual(verdict['status'], status, label)
                self.assertEqual(verdict.get('error'), error, label)

    async def test_strict_mode_blocks_on_unknown_but_plain_does_not(self):
        denylist = reputation.Denylist.empty()
        policy = reputation.make_policy(
            {'dnsbl_enabled': True, 'dnsbl_zones': ['a.example']}, denylist)

        async def blocked(query):
            return {'rcode': 'NOERROR', 'addresses': ['127.255.255.254']}
        verdict = await reputation.screen_proxy('http://198.51.100.7:8080', policy, denylist,
                                                resolver=blocked)
        self.assertFalse(reputation.verdict_blocks(verdict))
        self.assertTrue(reputation.verdict_blocks(verdict, strict=True))

    async def test_without_dnsbl_nothing_is_called_clean(self):
        verdict = await reputation.screen_proxy('http://198.51.100.7:8080',
                                                reputation.make_policy({}, reputation.Denylist.empty()),
                                                reputation.Denylist.empty())
        self.assertEqual(verdict['status'], 'unknown')
        self.assertEqual(verdict['dnsbl'], [])

    def test_local_rules_still_win_without_any_zone(self):
        denylist = reputation.Denylist.from_text('198.51.100.0/24')
        verdict = asyncio.run(reputation.screen_proxy('http://198.51.100.7:8080',
                                                      reputation.make_policy({}, denylist), denylist))
        self.assertEqual(verdict['status'], 'local_denied')
        self.assertEqual(verdict['local_rule'], 'cidr')


def live_report():
    async def main():
        rule = lambda text: print(f'\n--- {text} ' + '-' * max(0, 76 - len(text)))
        rule('дефект 13: judge на живом self-hosted эндпоинте')
        with reference_probe() as info:
            transport = LoopbackTransport()
            options = pr.validate_options({'preset': 'frugal'})
            response = await transport.send(
                pr.ProbeRequest(url=f"{info['url']}/echo", max_bytes=65_536), options=options)
            own = set(pr.extract_addresses(response.body))
            print(f'  ответ judge: {response.body[:80].decode()}...')
            print(f'  own_ips из bootstrap: {sorted(own)}')
            print('  anonymity.classify_detail(bootstrap)        :',
                  anonymity.classify_detail(response.body, own))
            challenge = await transport.send(
                pr.ProbeRequest(url=f"{info['url']}{pr.REFERENCE_PROBE_NEGATIVE['judge_challenge']}",
                                max_bytes=65_536), options=options)
            print(f'  CAPTCHA-страница (HTTP {challenge.status})          :',
                  pr.classify_echo(challenge.body, own).to_public())
            for label, body in (('пустой ответ', b''),
                                ('страница без адреса', b'<html><body>hello</body></html>')):
                print(f'  {label:35}:', pr.classify_echo(body, own).to_public())
            print('  bootstrap без judge_verified              :',
                  pr.classify_echo(response.body, own, judge_verified=False).to_public())
            print('  без baseline (own_ips пуст)               :',
                  pr.classify_echo(response.body, set()).to_public())
            try:
                pr.build_plan({'mode': 'tcp', 'min_anonymity': 'elite'})
            except pr.ProbeError as exc:
                print(f'  требование elite без judge                 : {exc.code}')
                print(f'    {exc.message[:110]}...')

        rule('дефект 14: IPv6 reverse')
        for address in ('2001:db8::1', '::1'):
            prefix = reputation.reverse_ip(address)
            print(f'  {address:16} -> {prefix[:44]}... меток={len(prefix.rstrip(".").split("."))} '
                  f'двоеточие={"да" if ":" in prefix else "нет"}')
        print(f'  compressed == expanded: '
              f'{reputation.reverse_ip("2001:db8::1") == reputation.reverse_ip("2001:0db8:0000:0000:0000:0000:0000:0001")}')

        rule('дефект 14: каждый исход зоны отдельно')
        zone = pr.generic_zone('bl.example')
        cases = [
            ('listed 127.0.0.2', {'rcode': 'NOERROR', 'addresses': ['127.0.0.2']}),
            ('clear NXDOMAIN', {'rcode': 'NXDOMAIN', 'addresses': []}),
            ('blocked 127.255.255.254', {'rcode': 'NOERROR', 'addresses': ['127.255.255.254']}),
            ('quota', pr.DnsQueryError('DNSBL_QUOTA')),
            ('access refused', pr.DnsQueryError('DNSBL_ACCESS')),
            ('SERVFAIL', {'rcode': 'SERVFAIL', 'addresses': []}),
            ('REFUSED', {'rcode': 'REFUSED', 'addresses': []}),
            ('NOERROR без адресов', {'rcode': 'NOERROR', 'addresses': []}),
        ]
        for label, answer in cases:
            async def resolve(query, _answer=answer):
                if isinstance(_answer, Exception):
                    raise _answer
                return _answer
            out = await reputation.check_dnsbl('198.51.100.7', [zone], 2.0, resolver=resolve)
            print(f'  {label:26} -> status={out[0]["status"]:8} code={str(out[0].get("error"))}')

        rule('дефект 14: коды, объявленные самой зоной')
        own_zone = pr.DnsblZone(name='bl.example', blocked_codes=('127.0.0.254',),
                                blocked_prefixes=('127.255.255.',), nxdomain='unknown')
        async def same(query):
            return {'rcode': 'NOERROR', 'addresses': ['127.0.0.254']}
        print('  ответ 127.0.0.254 в общей зоне       :',
              (await reputation.check_dnsbl('198.51.100.7', [zone], 2.0, resolver=same))[0])
        print('  тот же ответ в зоне с блок-кодом    :',
              (await reputation.check_dnsbl('198.51.100.7', [own_zone], 2.0, resolver=same))[0])

        rule('дефект 14: clean только когда ВСЕ зоны clear')
        denylist = reputation.Denylist.empty()
        policy = reputation.make_policy(
            {'dnsbl_enabled': True, 'dnsbl_zones': ['a.example', 'b.example']}, denylist)
        for label, answer in (('все clear', {'rcode': 'NXDOMAIN', 'addresses': []}),
                              ('одна listed', {'rcode': 'NOERROR', 'addresses': ['127.0.0.2']}),
                              ('одна blocked', {'rcode': 'NOERROR', 'addresses': ['127.255.255.254']}),
                              ('одна quota', pr.DnsQueryError('DNSBL_QUOTA'))):
            async def resolve(query, _answer=answer):
                if isinstance(_answer, Exception):
                    raise _answer
                return _answer
            verdict = await reputation.screen_proxy('http://198.51.100.7:8080', policy, denylist,
                                                    resolver=resolve)
            print(f'  {label:16} -> status={verdict["status"]:8} error={str(verdict.get("error"))}')
    asyncio.run(main())


if __name__ == '__main__':
    live_report()

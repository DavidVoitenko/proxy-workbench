"""Quotas and scope: country, protocol, ASN and unique exit IP, with honest unknown.

Every candidate is a local record in the documentation address ranges. Nothing
is dialled, no GeoIP database is consulted, and unknown values are tested as
unknown rather than filled in.
"""
from __future__ import annotations

import unittest

from proxy_workbench import pools

from tests.pools_support import FakeSource, PoolTestCase


def codes(status: pools.PoolStatus) -> dict:
    return {entry['code']: entry['count'] for entry in status.as_dict()['deficit_reasons']}


class CountryQuotaTest(PoolTestCase):
    def test_a_quota_limits_how_many_members_share_a_country(self):
        self.create_pool(desired=4, reserve=0, policy={'quota': {'country': 2}})
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(4, country='DE')})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 2)
        self.assertEqual(codes(status), {pools.REASON_QUOTA[0]: 2})
        self.assertEqual(self.member_states(), {'ep-01': pools.MEMBER_ACTIVE, 'ep-02': pools.MEMBER_ACTIVE})

    def test_a_different_country_is_not_counted_against_the_first_one(self):
        self.create_pool(desired=2, reserve=0, policy={'quota': {'country': 1}})
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, country='DE'), self.candidate(2, country='NL')]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 2)
        self.assertIsNone(status.deficit_reason)

    def test_an_unknown_country_is_reported_as_unknown_and_not_counted(self):
        self.create_pool(desired=3, reserve=0, policy={'quota': {'country': 1}})
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, country='DE'), self.candidate(2, country=None), self.candidate(3, country=None)]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 1, 'strict is the default: an unknown country is refused')
        self.assertEqual(codes(status), {pools.REASON_QUOTA_UNKNOWN[0]: 2})

    def test_unknown_can_be_allowed_and_is_then_reported(self):
        self.create_pool(desired=3, reserve=0,
                         policy={'quota': {'country': 1}, 'quota_unknown': {'country': pools.UNKNOWN_IGNORE}})
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, country='DE'), self.candidate(2, country=None), self.candidate(3, country=None)]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 3)
        self.assertEqual(status.quota_unknown, {'country': 2},
                         'the pool says how much of its quota picture it does not have')
        self.assertIsNone(status.deficit_reason)

    def test_members_the_source_did_not_describe_count_as_unknown(self):
        self.create_pool(desired=2, reserve=0, policy={'quota': {'country': 1}})
        first = FakeSource({pools.SOURCE_SOURCES: [self.candidate(1, country='DE')]})
        self.assertEqual(pools.refill(self.store, 'main', first, now=self.now).served, 1)

        # The second refill gets no description of ep-01, so the pool cannot prove
        # that the second DE is a different value.
        status = pools.refill(self.store, 'main', FakeSource(), now=self.now + 1)

        self.assertEqual(status.served, 1)
        self.assertEqual(status.quota_unknown, {'country': 1, 'protocol': 1, 'asn': 1, 'exit_ip': 1})


class UniqueExitTest(PoolTestCase):
    def test_one_address_per_exit_ip(self):
        self.create_pool(desired=3, reserve=0, policy={'quota': {'exit_ip': 1}})
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, exit_ip='203.0.113.1'),
            self.candidate(2, exit_ip='203.0.113.1'),
            self.candidate(3, exit_ip='203.0.113.9')]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 2)
        self.assertEqual(codes(status), {pools.REASON_QUOTA[3]: 1})
        self.assertEqual(sorted(self.member_states()), ['ep-01', 'ep-03'])

    def test_an_unknown_exit_ip_never_counts_as_a_distinct_address(self):
        self.create_pool(desired=2, reserve=0, policy={'quota': {'exit_ip': 1}})
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, exit_ip='203.0.113.1'), self.candidate(2, exit_ip=None)]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 1)
        self.assertEqual(codes(status), {pools.REASON_QUOTA_UNKNOWN[3]: 1})

    def test_the_pool_reports_a_duplicate_that_is_already_there(self):
        self.create_pool(desired=2, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, exit_ip='203.0.113.1'), self.candidate(2, exit_ip='203.0.113.1')]})
        self.assertEqual(pools.refill(self.store, 'main', source, now=self.now).served, 2)

        # A stricter quota arrives after the two members were admitted; the pool
        # reports the duplicate instead of silently pretending the quota holds.
        self.store.set_policy('main', {'quota': {'exit_ip': 1}})
        status = pools.refill(self.store, 'main', source, now=self.now + 1)

        self.assertIn(('exit_ip', '203.0.113.1', 2), status.quota_conflicts)
        self.assertIn({'dimension': 'exit_ip', 'value': '203.0.113.1', 'count': 2},
                      status.as_dict()['quota_conflicts'])
        self.assertEqual(status.served, 2)


class AsnAndProtocolTest(PoolTestCase):
    def test_an_asn_quota_is_enforced(self):
        self.create_pool(desired=3, reserve=0, policy={'quota': {'asn': 1}})
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, asn=64501), self.candidate(2, asn=64501), self.candidate(3, asn=64502)]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 2)
        self.assertEqual(codes(status), {pools.REASON_QUOTA[2]: 1})

    def test_a_protocol_quota_is_enforced(self):
        self.create_pool(desired=3, reserve=0, policy={'quota': {'protocol': 1}})
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, protocol='http'), self.candidate(2, protocol='http'),
            self.candidate(3, protocol='socks5')]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 2)
        self.assertEqual(codes(status), {pools.REASON_QUOTA[1]: 1})
        self.assertEqual(self.member_states()['ep-03'], pools.MEMBER_ACTIVE)

    def test_every_dimension_is_reported_as_unknown_when_nothing_is_known(self):
        self.create_pool(desired=1, reserve=0, policy={'quota': {'asn': 1}, 'quota_unknown': {'asn': pools.UNKNOWN_IGNORE}})
        source = FakeSource({pools.SOURCE_SOURCES: [self.candidate(1, asn=None)]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 1)
        self.assertEqual(status.quota_unknown['asn'], 1)


class CountryFilterTest(PoolTestCase):
    def test_the_filter_is_never_relaxed_to_fill_the_pool(self):
        self.create_pool(desired=2, reserve=0, policy={'countries': ['DE']})
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, country='DE'), self.candidate(2, country='NL'), self.candidate(3, country=None)]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 1)
        self.assertEqual(codes(status), {pools.REASON_COUNTRY_FILTER: 2})
        self.assertEqual(status.deficit_reason, pools.REASON_COUNTRY_FILTER)

    def test_a_filtered_out_country_does_not_even_consume_a_quota(self):
        self.create_pool(desired=2, reserve=0, policy={'countries': ['NL'], 'quota': {'country': 1}})
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, country='DE'), self.candidate(2, country='NL'), self.candidate(3, country='NL')]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 1, 'one NL is admitted; the second hits the quota')
        self.assertEqual(codes(status), {pools.REASON_COUNTRY_FILTER: 1, pools.REASON_QUOTA[0]: 1})


class ScopeTest(PoolTestCase):
    def test_a_candidate_from_another_collection_is_refused(self):
        self.create_pool(desired=1, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: [self.candidate(1, collection_id='col-someone-else')]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 0)
        self.assertEqual(status.deficit_reason, pools.REASON_SCOPE_COLLECTION)

    def test_a_public_endpoint_never_enters_a_private_collection(self):
        self.add_collection('col-own', 'private')
        self.create_pool('own', collection_id='col-own', desired=3, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, collection_id='col-own', origin_domain=pools.DOMAIN_PUBLIC),
            self.candidate(2, collection_id='col-own', origin_domain=pools.DOMAIN_OWN),
            self.candidate(3, collection_id='col-own', origin_domain=pools.DOMAIN_OWN)]})

        status = pools.refill(self.store, 'own', source, now=self.now)

        self.assertEqual(status.served, 2)
        self.assertEqual(status.state, pools.STATE_DEGRADED)
        self.assertEqual(codes(status), {pools.REASON_PUBLIC_IN_PRIVATE: 1})
        self.assertEqual(status.deficit_reason, pools.REASON_PUBLIC_IN_PRIVATE)
        self.assertNotIn('ep-01', self.member_states())

    def test_the_same_endpoint_is_fine_in_a_public_collection(self):
        self.create_pool('public', desired=1, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: [self.candidate(1, origin_domain=pools.DOMAIN_PUBLIC)]})

        self.assertEqual(pools.refill(self.store, 'public', source, now=self.now).served, 1)

    def test_a_pool_whose_collection_vanished_fails_closed(self):
        self.create_pool('main', desired=1, reserve=0)
        with pools.transaction(self.store.conn):
            self.store.conn.execute('DELETE FROM collections WHERE id = ?', (self.collection_id,))
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(2)})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.state, pools.STATE_ERROR)
        self.assertEqual(status.deficit_reason, pools.REASON_UNKNOWN_COLLECTION)
        self.assertEqual(status.served, 0)
        self.assertEqual(source.asked, [], 'nothing is admitted from a collection that is not there')
        self.assertEqual(status.collection_kind, None)


class FreshnessTest(PoolTestCase):
    def test_a_row_without_a_deadline_is_not_treated_as_fresh(self):
        self.create_pool(desired=1, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: [self.candidate(1, valid_until=None)]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 0)
        self.assertEqual(status.deficit_reason, pools.REASON_TIME_MISSING)

    def test_an_expired_row_is_refused(self):
        self.create_pool(desired=1, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: [self.candidate(1, valid_until=self.now - 1)]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 0)
        self.assertEqual(status.deficit_reason, pools.REASON_TIME_EXPIRED)

    def test_a_measurement_from_the_future_is_refused(self):
        self.create_pool(desired=1, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, checked_at=self.now + 4000, valid_until=self.now + 8000)]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 0)
        self.assertEqual(status.deficit_reason, pools.REASON_TIME_FUTURE)

    def test_a_time_that_is_not_a_number_is_refused(self):
        self.create_pool(desired=1, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: [self.candidate(1, checked_at='yesterday')]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 0)
        self.assertEqual(status.deficit_reason, pools.REASON_TIME_UNKNOWN)

    def test_the_requirement_can_be_turned_off_for_a_caller_that_guarantees_freshness(self):
        self.create_pool(desired=1, reserve=0, policy={'require_valid_until': False})
        source = FakeSource({pools.SOURCE_SOURCES: [self.candidate(1, valid_until=None)]})

        self.assertEqual(pools.refill(self.store, 'main', source, now=self.now).served, 1)


class AdmissionReasonTest(PoolTestCase):
    def test_a_refused_candidate_keeps_the_code_of_the_admission_contract(self):
        self.create_pool(desired=1, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, allowed=False, admission_reason='E_SCOPE_DENYLIST'),
            self.candidate(2, allowed=False, admission_reason=None)]})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 0)
        self.assertEqual(codes(status), {'E_SCOPE_DENYLIST': 1, pools.REASON_DENIED: 1})
        self.assertEqual(status.deficit_reason, 'E_SCOPE_DENYLIST')


class PerPoolLimitTest(PoolTestCase):
    def test_max_members_is_a_hard_limit_on_the_table(self):
        self.create_pool(desired=3, reserve=0, policy={'max_members': 2})
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(5)})

        status = pools.refill(self.store, 'main', source, now=self.now)

        self.assertEqual(status.served, 2)
        self.assertEqual(len(self.store.members('main')), 2)
        self.assertEqual(codes(status), {pools.REASON_AT_CAPACITY: 3})

    def test_a_resting_member_does_not_block_a_new_one(self):
        self.create_pool(desired=1, reserve=0)
        pools.refill(self.store, 'main', FakeSource({pools.SOURCE_SOURCES: [self.candidate(1)]}), now=self.now)
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 1)

        more = FakeSource({pools.SOURCE_SOURCES: [self.candidate(1), self.candidate(2)]})
        status = pools.refill(self.store, 'main', more, now=self.now + 2)

        self.assertEqual(status.served, 1)
        self.assertEqual(self.member_states()['ep-02'], pools.MEMBER_ACTIVE)
        self.assertEqual(self.member_states()['ep-01'], pools.MEMBER_COOLDOWN)

    def test_a_member_that_rested_too_long_is_forgotten_and_can_come_back(self):
        self.create_pool(desired=1, reserve=0, policy={'cooldown_seconds': 60, 'retire_seconds': 120})
        source = FakeSource({pools.SOURCE_SOURCES: [self.candidate(1)]})
        pools.refill(self.store, 'main', source, now=self.now)
        pools.report_health(self.store, 'main', 'ep-01', ok=False, now=self.now + 1)

        kept = pools.refill(self.store, 'main', source, now=self.now + 100)
        self.assertEqual(kept.counts[pools.MEMBER_PROBATION], 1)
        self.assertEqual(len(self.store.members('main')), 1)

        # A source that offers nothing shows the pool has forgotten the endpoint.
        forgotten = pools.refill(self.store, 'main', FakeSource(), now=self.now + 200)
        self.assertEqual(len(self.store.members('main')), 0)
        self.assertEqual(forgotten.served, 0)

        back = pools.refill(self.store, 'main', source, now=self.now + 201)
        self.assertEqual(back.served, 1)
        self.assertEqual(self.member_states()['ep-01'], pools.MEMBER_ACTIVE)


if __name__ == '__main__':
    unittest.main()

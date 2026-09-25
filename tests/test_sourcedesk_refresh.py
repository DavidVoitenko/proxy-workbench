"""Feed lifecycle: delta, merge/replace, last-good, expiry, quota, rotation.

These are the pure decisions: no store, no network.  A failure must never cost
a working collection, and a credential change must invalidate exactly the
admissions that were made with the old credential.
"""
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import sourcedesk as sd

A = 'http://198.51.100.7:8080'
B = 'socks5://198.51.100.11:1080'
C = 'http://198.51.100.21:3128'
SHARED = 'http://198.51.100.33:8080'

T0 = 1_700_000_000.0


def state(**kwargs):
    base = dict(source_id='src-1', collection_id='c1', mode='merge',
                active=(A, B), last_good=(A, B), last_good_at=T0 - 100,
                expires_at=T0 + 100, last_attempt_at=T0 - 100, last_success_at=T0 - 100)
    base.update(kwargs)
    return sd.FeedState(**base)


def ok(entries, **kwargs):
    return sd.FeedResult(outcome='ok', fetched_at=T0, entries=tuple(entries), **kwargs)


def policy(**kwargs):
    base = dict(mode='merge', ttl_seconds=3600)
    base.update(kwargs)
    return sd.FeedPolicy(**base)


class SuccessTests(unittest.TestCase):
    def test_merge_keeps_previous_endpoints(self):
        plan = sd.plan_refresh(state(mode='merge'), ok([A, C]), policy=policy(mode='merge'), now=T0)
        self.assertEqual(plan.added, (C,))
        self.assertEqual(plan.kept, (A,))
        self.assertEqual(plan.removed, ())
        self.assertEqual(plan.next_state.active, tuple(sorted((A, B, C))))
        self.assertTrue(plan.promote_last_good)

    def test_replace_removes_only_what_the_feed_dropped(self):
        plan = sd.plan_refresh(state(mode='replace'), ok([A, C]), policy=policy(mode='replace'), now=T0)
        self.assertEqual(plan.added, (C,))
        self.assertEqual(plan.removed, (B,))
        self.assertEqual(plan.next_state.active, tuple(sorted((A, C))))

    def test_replace_reports_an_overlap_instead_of_pretending_it_vanished(self):
        before = state(mode='replace', active=(A, B, SHARED), last_good=(A, B, SHARED))
        plan = sd.plan_refresh(before, ok([A, C]), policy=policy(mode='replace'), now=T0, foreign=(SHARED,))
        # This source drops both rows; the shared one survives in the
        # collection through the other source, and the plan says so.
        self.assertEqual(plan.removed, tuple(sorted((B, SHARED))))
        self.assertEqual(plan.retained_shared, (SHARED,))

    def test_duplicate_entries_collapse(self):
        plan = sd.plan_refresh(state(mode='merge', active=(A,)), ok([A, A, A]), policy=policy(), now=T0)
        self.assertEqual(plan.added, ())
        self.assertEqual(plan.kept, (A,))

    def test_expiry_is_written_once_from_the_fetch_time(self):
        plan = sd.plan_refresh(state(expires_at=None), ok([A]), policy=policy(ttl_seconds=600), now=T0)
        self.assertEqual(plan.expires_at, T0 + 600)
        self.assertEqual(plan.next_state.expires_at, T0 + 600)

    def test_delta_is_added_plus_removed(self):
        plan = sd.plan_refresh(state(mode='replace'), ok([A, C]), policy=policy(mode='replace'), now=T0)
        self.assertEqual(plan.delta, frozenset({C, B}))


class FailureTests(unittest.TestCase):
    """F27 acceptance: a failed update never clears a working collection."""

    def test_every_failure_outcome_changes_nothing(self):
        for outcome in sd.FAILURE_OUTCOMES:
            with self.subTest(outcome=outcome):
                before = state(mode='replace')
                plan = sd.plan_refresh(before, sd.FeedResult(outcome=outcome, fetched_at=T0, error='x'),
                                       policy=policy(mode='replace'), now=T0)
                self.assertTrue(plan.blocked)
                self.assertEqual(plan.added, ())
                self.assertEqual(plan.removed, ())
                self.assertEqual(plan.expires_at, before.expires_at)
                self.assertEqual(plan.next_state.active, before.active)
                self.assertEqual(plan.next_state.last_good, before.last_good)
                self.assertTrue(plan.serve_from_last_good)

    def test_failure_counts_and_backs_off(self):
        plan = sd.plan_refresh(state(), sd.FeedResult(outcome='unavailable', fetched_at=T0),
                               policy=policy(), now=T0)
        self.assertEqual(plan.next_state.consecutive_failures, 1)
        self.assertEqual(plan.next_attempt_at, T0 + sd.BACKOFF_SECONDS[0])

    def test_retry_after_wins_over_local_backoff(self):
        result = sd.FeedResult(outcome='rate_limited', fetched_at=T0, retry_after=T0 + 7200)
        plan = sd.plan_refresh(state(), result, policy=policy(), now=T0)
        self.assertEqual(plan.next_attempt_at, T0 + 7200)
        self.assertIn('E_SOURCE_RETRY_AFTER', [item.code for item in plan.diagnostics])

    def test_three_failures_quarantine_without_deleting(self):
        before = state(consecutive_failures=2)
        plan = sd.plan_refresh(before, sd.FeedResult(outcome='unavailable', fetched_at=T0),
                               policy=policy(), now=T0)
        self.assertIsNotNone(plan.next_state.quarantine_until)
        self.assertEqual(plan.next_state.active, before.active)

    def test_a_failure_result_may_not_carry_entries(self):
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.FeedResult(outcome='unavailable', fetched_at=T0, entries=('a',))
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_unknown_outcome_is_refused(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.FeedResult(outcome='looks-fine', fetched_at=T0)


class EmptyAndPartialTests(unittest.TestCase):
    def test_empty_body_never_clears_a_collection(self):
        plan = sd.plan_refresh(state(mode='replace'),
                               sd.FeedResult(outcome='empty', fetched_at=T0),
                               policy=policy(mode='replace'), now=T0)
        self.assertEqual(plan.removed, ())
        self.assertEqual(plan.next_state.active, state().active)
        self.assertIn('E_SOURCE_EMPTY_FEED', [item.code for item in plan.diagnostics])
        self.assertNotIn('empty', [item for item in plan.added])

    def test_partial_fetch_adds_but_never_removes(self):
        plan = sd.plan_refresh(state(mode='replace'), sd.FeedResult(outcome='partial', fetched_at=T0,
                                                                    entries=(C,)),
                               policy=policy(mode='replace'), now=T0)
        self.assertEqual(plan.added, (C,))
        self.assertEqual(plan.removed, ())
        self.assertFalse(plan.promote_last_good)
        self.assertEqual(plan.next_state.last_good, state().last_good)

    def test_partial_does_not_extend_expiry(self):
        before = state(expires_at=T0 + 10)
        plan = sd.plan_refresh(before, sd.FeedResult(outcome='partial', fetched_at=T0, entries=(C,)),
                               policy=policy(), now=T0)
        self.assertEqual(plan.expires_at, T0 + 10)

    def test_not_modified_changes_nothing_but_the_failure_counter(self):
        before = state(consecutive_failures=2, expires_at=T0 + 10)
        plan = sd.plan_refresh(before, sd.FeedResult(outcome='not_modified', fetched_at=T0), policy=policy(), now=T0)
        self.assertTrue(plan.blocked)
        self.assertEqual(plan.added, ())
        self.assertEqual(plan.removed, ())
        self.assertEqual(plan.expires_at, before.expires_at)
        self.assertEqual(plan.next_state.consecutive_failures, 0)
        self.assertEqual(plan.next_state.last_validated_at, T0)

    def test_not_modified_keeps_validators_and_adds_new_ones(self):
        plan = sd.plan_refresh(state(etag='W/1', last_modified='Mon, 01 Jan 2024 00:00:00 GMT'),
                               sd.FeedResult(outcome='not_modified', fetched_at=T0, etag='W/2'),
                               policy=policy(), now=T0)
        self.assertEqual(plan.next_state.etag, 'W/2')
        self.assertEqual(plan.next_state.last_modified, 'Mon, 01 Jan 2024 00:00:00 GMT')


class QuotaTests(unittest.TestCase):
    def test_missing_header_means_unknown_not_zero(self):
        self.assertIsNone(sd.quota_from_response({'Content-Type': 'text/plain'}, now=T0))
        state_without_token = state()
        self.assertEqual(sd.feed_diagnostics(state_without_token, now=T0), ())

    def test_remaining_is_read_from_known_headers(self):
        quota = sd.quota_from_response({'X-RateLimit-Limit': '100', 'X-RateLimit-Remaining': '3'}, now=T0)
        self.assertEqual((quota.limit, quota.remaining, quota.exhausted), (100, 3, False))

    def test_exhausted_quota_is_reported(self):
        quota = sd.quota_from_response({'X-Quota-Limit': '10', 'X-Quota-Remaining': '0'}, now=T0)
        codes = [item.code for item in sd.feed_diagnostics(state(), now=T0, quota=quota)]
        self.assertIn('E_SOURCE_QUOTA_EXHAUSTED', codes)

    def test_low_quota_is_reported_before_exhaustion(self):
        quota = sd.quota_from_response({'X-Quota-Limit': '100', 'X-Quota-Remaining': '5'}, now=T0)
        codes = [item.code for item in sd.feed_diagnostics(state(), now=T0, quota=quota,
                                                           policy=policy(quota_low_threshold=0.1))]
        self.assertIn('E_SOURCE_QUOTA_LOW', codes)
        self.assertNotIn('E_SOURCE_QUOTA_EXHAUSTED', codes)

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(sd._parse_retry_after('120', T0), T0 + 120)
        self.assertEqual(sd._parse_retry_after('Mon, 01 Jan 2024 00:00:00 GMT', 0), 1704067200.0)
        self.assertIsNone(sd._parse_retry_after('soon', T0))
        self.assertIsNone(sd._parse_retry_after(None, T0))

    def test_token_expiry_is_reported_before_and_after(self):
        expired = sd.quota_from_response({}, now=T0, token_expires_at=T0 - 1)
        codes = [item.code for item in sd.feed_diagnostics(state(), now=T0, quota=expired)]
        self.assertIn('E_SOURCE_TOKEN_EXPIRED', codes)
        soon = sd.quota_from_response({}, now=T0, token_expires_at=T0 + 60)
        codes = [item.code
                 for item in sd.feed_diagnostics(state(), now=T0, quota=soon,
                                                  policy=policy(token_expiry_slack_seconds=3600))]
        self.assertIn('E_SOURCE_TOKEN_EXPIRING', codes)

    def test_token_feed_without_quota_is_unknown_not_empty(self):
        codes = [item.code for item in sd.feed_diagnostics(state(access_ref='auth-1'), now=T0)]
        self.assertIn('E_SOURCE_QUOTA_UNKNOWN', codes)

    def test_token_expiry_header_is_parsed(self):
        quota = sd.quota_from_response({'X-Token-Expires-At': '3600'}, now=T0)
        self.assertEqual(quota.token_expires_at, T0 + 3600)

    def test_quota_from_non_mapping_is_refused(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.quota_from_response(['X-Quota-Remaining: 0'], now=T0)


class ExpiryTests(unittest.TestCase):
    def test_stale_before_the_drop_window(self):
        before = state(expires_at=T0 - 10)
        codes = [item.code for item in sd.feed_diagnostics(before, now=T0,
                                                           policy=policy(drop_after_seconds=86400))]
        self.assertEqual(codes, ['E_SOURCE_STALE'])

    def test_expired_after_the_drop_window(self):
        before = state(expires_at=T0 - 86400 * 10)
        codes = [item.code for item in sd.feed_diagnostics(before, now=T0,
                                                           policy=policy(drop_after_seconds=86400))]
        self.assertEqual(codes, ['E_SOURCE_EXPIRED'])

    def test_fresh_feed_has_no_expiry_diagnostic(self):
        before = state(expires_at=T0 + 10)
        self.assertEqual(sd.feed_diagnostics(before, now=T0, policy=policy()), ())

    def test_quarantine_and_retry_are_visible(self):
        before = state(quarantine_until=T0 + 60, retry_after=T0 + 30)
        codes = [item.code for item in sd.feed_diagnostics(before, now=T0, policy=policy())]
        self.assertIn('E_SOURCE_QUARANTINED', codes)
        self.assertIn('E_SOURCE_RETRY_AFTER', codes)

    def test_invalid_policy_is_refused(self):
        for bad in (dict(ttl_seconds=0), dict(mode='upsert'), dict(quota_low_threshold=2),
                    dict(drop_after_seconds=0)):
            with self.subTest(bad=bad), self.assertRaises(sd.SourceDeskError):
                policy(**bad)

    def test_invalid_state_is_refused(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.FeedState('src-1', 'c1', mode='append')


class RotationTests(unittest.TestCase):
    """F27 acceptance: a credential change invalidates the admissions it must."""

    def test_first_binding_creates_revision_one_without_invalidation(self):
        before = sd.FeedState('src-1', 'c1', access_revision=1)
        plan, after, diagnostics = sd.plan_rotation(before, access_id='access-1',
                                                     new_access_ref=sd.make_ref('auth', 'b1', 'v1'))
        self.assertEqual((plan.previous_revision, plan.revision), (1, 2))
        self.assertEqual(plan.invalidates, ())
        self.assertEqual(after.access_revision, 2)
        self.assertEqual(diagnostics[0].code, 'E_SOURCE_ACCESS_REVISION_CHANGED')

    def test_second_rotation_invalidates_the_previous_revision(self):
        before = replace(state(), access_id='access-1', access_revision=3)
        plan, after, _ = sd.plan_rotation(before, access_id='access-1',
                                          new_access_ref=sd.make_ref('auth', 'b1', 'v2'), now=T0)
        self.assertEqual(plan.invalidates, (('access-1', 3),))
        self.assertEqual(after.access_revision, 4)

    def test_changing_the_access_id_is_an_explicit_rebind(self):
        before = replace(state(), access_id='access-1')
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.plan_rotation(before, access_id='access-2',
                             new_access_ref=sd.make_ref('auth', 'b1', 'v2'), now=T0)
        self.assertEqual(caught.exception.code, 'E_CONFLICT_REVISION')

    def test_a_plain_value_is_not_an_accepted_credential(self):
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.plan_rotation(state(), access_id='access-1', new_access_ref='hunter2', now=T0)
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_rotation_needs_an_access_id(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.plan_rotation(state(), access_id='', new_access_ref=sd.make_ref('auth', 'b', 'v'), now=T0)


class SummaryTests(unittest.TestCase):
    def test_summary_is_serializable_and_names_the_reason(self):
        plan = sd.plan_refresh(state(), sd.FeedResult(outcome='rate_limited', fetched_at=T0),
                               policy=policy(), now=T0)
        summary = plan.summary()
        self.assertEqual(summary['reason_code'], 'rate_limited')
        self.assertFalse(summary['applied'])
        self.assertTrue(summary['served_from_last_good'])
        self.assertIn('E_SOURCE_RETRY_AFTER', summary['diagnostics'])

    def test_plans_reject_foreign_argument_types(self):
        with self.assertRaises(sd.SourceDeskError):
            sd.plan_refresh({'source_id': 'x'}, ok([A]), policy=policy(), now=T0)


if __name__ == '__main__':
    unittest.main()

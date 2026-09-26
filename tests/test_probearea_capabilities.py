"""F20 bounded capabilities and the speed window, run for real.

Acceptance being checked here: every new kind of measurement has a controlled
endpoint, a budget, a distinct outcome, and local positive *and* negative
scenarios — and cold and reused connections are never mixed.

All traffic is loopback to the reference probe this module ships, through a
local mock proxy.  No public proxy, CDN or streaming service is contacted.
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from probearea_support import LoopbackTransport, MockProxy, reference_probe
from proxy_workbench import probes as pr

# Loopback finishes a megabyte in milliseconds, so the product minimums would
# honestly answer "insufficient" for every local run.  The throttled endpoint
# below gives a real window; the default minimums stay pinned in
# tests.test_probes_speed.
LOCAL_LIMITS = pr.SpeedLimits(min_bytes=4_000, min_seconds=0.0, min_chunks=2, max_bytes=8 << 20)
THROTTLED = '/bytes/2000000?rate=2000000'


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._running = pr.serve_reference_probe()
        self.info = self._running.__enter__()
        self.base = self.info['url']
        self.proxy = MockProxy('connect').start()
        self.transport = LoopbackTransport(self.proxy)
        self.options = pr.validate_options({'preset': 'frugal'})

    async def asyncTearDown(self):
        await asyncio.to_thread(self.proxy.stop)
        await asyncio.to_thread(self._running.__exit__, None, None, None)

    def spec(self, **data):
        return pr.validate_capability(data)

    def negative(self, name):
        return f'{self.base}{pr.REFERENCE_PROBE_NEGATIVE[name]}'


class WebsocketCapability(Base):
    async def test_handshake_and_pong_succeed(self):
        outcome = await pr.run_capability(
            self.spec(kind='websocket', url=f'{self.base}/ws',
                      budget={'handshake_timeout_s': 3.0, 'ping_timeout_s': 3.0, 'max_pings': 2}),
            self.options, self.transport)
        self.assertEqual(outcome.state, 'ok')
        self.assertIsNone(outcome.code)
        self.assertEqual(outcome.metrics['status'], 101)
        self.assertEqual(outcome.metrics['pongs'], 2)
        self.assertEqual(outcome.connection, 'cold')

    async def test_a_plain_http_answer_is_not_a_websocket(self):
        outcome = await pr.run_capability(
            self.spec(kind='websocket', url=self.negative('websocket_no_upgrade'),
                      budget={'ping_timeout_s': 2.0}), self.options, self.transport)
        self.assertEqual(outcome.state, 'not_upgraded')
        self.assertEqual(outcome.code, pr.WS_NOT_UPGRADED)

    async def test_a_handshake_without_a_pong_is_not_a_websocket(self):
        outcome = await pr.run_capability(
            self.spec(kind='websocket', url=self.negative('websocket_no_pong'),
                      budget={'ping_timeout_s': 1.5, 'max_pings': 1}), self.options, self.transport)
        self.assertIn(outcome.state, ('closed', 'no_pong', 'error'))
        self.assertIn(outcome.code, (pr.WS_CLOSED, pr.WS_PONG_MISSING, pr.READ_TIMEOUT))
        self.assertNotEqual(outcome.state, 'ok')

    async def test_the_budget_bounds_the_measurement(self):
        manifest = pr.capabilities_manifest()
        websocket = [item for item in manifest['kinds'] if item['kind'] == 'websocket'][0]
        self.assertEqual(set(websocket['limits']),
                         {'handshake_timeout_s', 'ping_timeout_s', 'max_pings', 'max_frame_bytes'})
        with self.assertRaises(pr.ProbeError):
            self.spec(kind='websocket', url=f'{self.base}/ws', budget={'max_pings': 99})
        with self.assertRaises(pr.ProbeError):
            self.spec(kind='websocket', url=f'{self.base}/ws', budget={'unknown_field': 1})


class DurationCapability(Base):
    async def test_a_held_connection_succeeds(self):
        outcome = await pr.run_capability(
            self.spec(kind='duration', url=f'{self.base}/hold?seconds=2',
                      budget={'hold_s': 3.0, 'min_sustained_s': 1.5, 'min_bytes': 1}),
            self.options, self.transport)
        self.assertEqual(outcome.state, 'ok')
        self.assertGreaterEqual(outcome.metrics['sustained_s'], 1.5)
        self.assertGreater(outcome.metrics['bytes'], 0)

    async def test_a_connection_dropped_early_is_short_not_ok(self):
        flaky = await pr.run_capability(
            self.spec(kind='duration', url=f'{self.base}/hold?seconds=6&flaky=1',
                      budget={'hold_s': 4.0, 'min_sustained_s': 2.0}), self.options, self.transport)
        self.assertEqual(flaky.state, 'short')
        self.assertIn(flaky.code, (pr.CONNECTION_CLOSED, pr.CONNECTION_SHORT))

    async def test_a_connection_shorter_than_required_is_short(self):
        outcome = await pr.run_capability(
            self.spec(kind='duration', url=f'{self.base}/hold?seconds=0.3',
                      budget={'hold_s': 2.0, 'min_sustained_s': 2.0}), self.options, self.transport)
        self.assertEqual(outcome.state, 'short')

    async def test_hold_longer_than_the_whole_probe_is_refused(self):
        fast = pr.validate_options({'preset': 'fast'})
        spec = self.spec(kind='duration', url=f'{self.base}/hold?seconds=2', budget={'hold_s': 60.0})
        with self.assertRaises(pr.ProbeError) as caught:
            await pr.run_duration(spec, fast, self.transport)
        self.assertEqual(caught.exception.code, pr.E_VALIDATION_FIELD)
        self.assertIn('общего срока пробы', str(caught.exception))


class MediaCapability(Base):
    async def test_manifest_and_segment_succeed(self):
        outcome = await pr.run_capability(
            self.spec(kind='media', url=f'{self.base}/manifest.m3u8',
                      budget={'segment_max_bytes': 131_072, 'min_segment_bytes': 16}),
            self.options, self.transport)
        self.assertEqual(outcome.state, 'ok')
        self.assertEqual(outcome.metrics['manifest_status'], 200)
        self.assertIn('segment', outcome.metrics['segment_url'])
        self.assertGreaterEqual(outcome.metrics['segment_bytes'], 16)

    async def test_a_manifest_with_no_segment_is_its_own_outcome(self):
        outcome = await pr.run_capability(
            self.spec(kind='media', url=self.negative('media_no_manifest'),
                      budget={'segment_max_bytes': 65_536}), self.options, self.transport)
        self.assertEqual(outcome.state, 'no_manifest')
        self.assertEqual(outcome.code, pr.MEDIA_NO_MANIFEST)

    async def test_a_missing_manifest_is_not_a_missing_segment(self):
        missing = await pr.run_capability(
            self.spec(kind='media', url=self.negative('media_manifest_missing'),
                      budget={'segment_max_bytes': 65_536}), self.options, self.transport)
        self.assertEqual(missing.state, 'manifest_failed')
        bad_segment = await pr.run_capability(
            self.spec(kind='media', url=self.negative('media_bad_segment'),
                      budget={'segment_max_bytes': 65_536}), self.options, self.transport)
        self.assertEqual(bad_segment.state, 'segment_failed')

    def test_the_manifest_parser_is_bounded(self):
        m3u8 = b'#EXTM3U\n#EXT-X-TARGETDURATION:4\n' + b''.join(
            f'#EXTINF:4.0,\n/segment/{index}\n'.encode() for index in range(500))
        self.assertEqual(len(pr.parse_media_manifest(m3u8, max_segments=1)), 1)
        self.assertEqual(pr.parse_media_manifest(b'#EXTM3U\n'), ())
        self.assertEqual(pr.parse_media_manifest(b'{"segments": [{"url": "/a"}, {"url": "/b"}]}',
                                                  max_segments=1), ('/a',))
        self.assertEqual(pr.parse_media_manifest(b'{not json'), ())


class HttpApiAssertions(Base):
    API = {'id': 'api', 'url': 'REPLACED', 'statuses': [200], 'content_type': 'application/json',
           'min_body_bytes': 10, 'max_body_bytes': 65_536,
           'json_assertions': [
               {'path': 'status', 'op': 'equals', 'value': 'ok'},
               {'path': 'quota.requests_per_minute', 'op': 'min', 'value': 10},
               {'path': 'quota.exhausted', 'op': 'equals', 'value': False},
               {'path': 'regions.0.latency_ms', 'op': 'max', 'value': 100},
               {'path': 'limits.bandwidth_mbps', 'op': 'type', 'value': 'number'},
               {'path': 'build', 'op': 'contains', 'value': 'refer'}]}

    def target(self, **over):
        return pr.validate_target({**self.API, 'url': f'{self.base}/api/status', **over})

    async def test_a_matching_api_succeeds(self):
        outcome = await pr.run_probe(self.target(), self.options, self.transport)
        self.assertTrue(outcome.ok, outcome.to_public())
        self.assertGreater(outcome.bytes, 100)

    async def test_a_degraded_api_fails_with_the_failing_path(self):
        outcome = await pr.run_probe(self.target(url=f'{self.base}/api/status?status=degraded'),
                                     self.options, self.transport)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, pr.JSON_ASSERT)
        self.assertEqual(outcome.stage, 'assert')
        self.assertIn('status', outcome.detail)

    async def test_broken_and_empty_json_bodies_fail_differently(self):
        broken = await pr.run_probe(self.target(url=self.negative('api_broken_json')),
                                    self.options, self.transport)
        self.assertEqual(broken.code, pr.JSON_ASSERT)
        self.assertIn('not valid JSON', broken.detail)
        empty = await pr.run_probe(self.target(url=f'{self.base}/api/empty'), self.options,
                                   self.transport)
        self.assertEqual(empty.code, pr.BODY_TOO_SMALL)


class SpeedWindow(Base):
    async def test_a_throttled_transfer_gives_an_honest_number(self):
        target = pr.validate_speed_target({'url': f'{self.base}{THROTTLED}', 'max_bytes': 2_000_000})
        cold = await pr.run_speed_test(target, self.options, self.transport, limits=pr.SPEED_LIMITS)
        self.assertEqual(cold.state, 'ok')
        self.assertIsNotNone(cold.mbps)
        self.assertEqual(cold.connection, 'cold')
        # The number is bytes over the window from the first byte to the last.
        expected = cold.bytes * 8 / (cold.transfer_ms / 1000) / 1e6
        self.assertLess(abs(expected - cold.mbps), 0.05)

    async def test_one_chunk_never_produces_a_number(self):
        limits = pr.SPEED_LIMITS
        trace = pr.TransferTrace(url='x')
        trace.begin(0.0)
        trace.add(2_000_000, 1.0)
        trace.finish(1.0)
        outcome = pr.measure_speed(trace, limits)
        self.assertEqual(outcome.state, 'insufficient')
        self.assertIsNone(outcome.mbps)
        self.assertEqual(outcome.code, pr.INSUFFICIENT_SAMPLE)

    async def test_an_instant_loopback_transfer_is_insufficient_not_fast(self):
        target = pr.validate_speed_target({'url': f'{self.base}/bytes/2000000', 'max_bytes': 2_000_000})
        outcome = await pr.run_speed_test(target, self.options, self.transport, limits=pr.SPEED_LIMITS)
        self.assertEqual(outcome.state, 'insufficient')
        self.assertIsNone(outcome.mbps)
        self.assertIn('короче минимума', outcome.detail)

    async def test_a_broken_transfer_is_an_error_not_a_zero(self):
        trace = pr.TransferTrace(url='x')
        trace.begin(0.0)
        trace.add(500, 0.1)
        trace.fail('ConnectionReset')
        outcome = pr.measure_speed(trace)
        self.assertEqual(outcome.state, 'error')
        self.assertIsNone(outcome.mbps)

    async def test_ttfb_transfer_and_total_are_separate(self):
        target = pr.validate_speed_target({'url': f'{self.base}{THROTTLED}', 'max_bytes': 2_000_000})
        cold = await pr.run_speed_test(target, self.options, self.transport, limits=LOCAL_LIMITS)
        self.assertGreater(cold.total_ms, cold.transfer_ms)
        self.assertGreaterEqual(cold.transfer_ms, 0)

    async def test_cold_and_reused_are_never_mixed(self):
        target = pr.validate_speed_target({'url': f'{self.base}{THROTTLED}', 'max_bytes': 2_000_000})
        cold = await pr.run_speed_test(target, self.options, self.transport, limits=LOCAL_LIMITS)
        reused = await pr.run_speed_test(target, self.options, self.transport, limits=LOCAL_LIMITS,
                                         reuse=True)
        self.assertEqual(cold.connection, 'cold')
        self.assertEqual(reused.connection, 'reused')
        self.assertTrue(pr.mixed_speed_connections([cold, reused]))
        self.assertFalse(pr.mixed_speed_connections([cold, cold]))
        self.assertEqual(len(pr.comparable_speed([cold, reused], 'cold')), 1)
        self.assertEqual(len(pr.comparable_speed([cold, reused], 'reused')), 1)
        self.assertEqual(pr.comparable_speed([cold, reused], 'cold')[0].connection, 'cold')

    async def test_a_transport_that_ignores_reuse_is_labelled_honestly(self):
        class ColdOnly:
            async def download(self, target, *, options, reuse=False):
                trace = pr.TransferTrace(url=target.url, connection='cold')
                trace.begin(0.0)
                trace.add(8_192, 0.01)
                trace.add(8_192, 0.2)
                return trace.finish(0.4)
        target = pr.validate_speed_target({'url': f'{self.base}/bytes/100000', 'max_bytes': 100_000})
        outcome = await pr.run_speed_test(target, self.options, ColdOnly(), limits=LOCAL_LIMITS,
                                          reuse=True)
        self.assertEqual(outcome.connection, 'cold')
        self.assertIn('вместо повторного', outcome.detail)


class CapabilityMatrixTruth(unittest.TestCase):
    def test_the_matrix_lists_what_is_measured_and_what_is_not(self):
        matrix = {item['id']: item for item in pr.capability_matrix()}
        for name in ('websocket_handshake', 'long_lived_connection', 'media_manifest_segment',
                     'http_api_assertions'):
            with self.subTest(capability=name):
                self.assertTrue(matrix[name]['supported'], name)
                self.assertNotEqual(matrix[name]['endpoint'], '—')
                self.assertNotEqual(matrix[name]['budget'], '—')
                self.assertNotEqual(matrix[name]['outcome'], '')
        for name in ('udp_transport', 'http2_or_http3', 'calls_video_any_service'):
            with self.subTest(capability=name):
                self.assertFalse(matrix[name]['supported'], name)

    def test_there_is_no_blanket_claim(self):
        matrix = {item['id']: item for item in pr.capability_matrix()}
        for item in matrix.values():
            self.assertNotIn('все звонки', str(item).lower())
            self.assertNotIn('всё видео', str(item).lower())
        self.assertIn('запрещено', matrix['calls_video_any_service']['outcome'])


def live_report():
    async def main():
        with reference_probe() as info:
            base = info['url']
            with MockProxy('connect') as proxy:
                transport = LoopbackTransport(proxy)
                options = pr.validate_options({'preset': 'frugal'})
                print(f'\n--- F20: bounded capabilities на живых сокетах ({base}) ' + '-' * 8)
                rows = [
                    ('POSITIVE', 'websocket  /ws', dict(kind='websocket', id='ws-ok',
                     url=f'{base}/ws', budget={'ping_timeout_s': 3.0, 'max_pings': 2})),
                    ('NEGATIVE', 'websocket  /ws-none (нет 101)', dict(kind='websocket', id='ws-no',
                     url=f'{base}{pr.REFERENCE_PROBE_NEGATIVE["websocket_no_upgrade"]}',
                     budget={'ping_timeout_s': 2.0})),
                    ('NEGATIVE', 'websocket  /ws-silent (нет pong)', dict(kind='websocket', id='ws-silent',
                     url=f'{base}{pr.REFERENCE_PROBE_NEGATIVE["websocket_no_pong"]}',
                     budget={'ping_timeout_s': 1.5, 'max_pings': 1})),
                    ('POSITIVE', 'duration   /hold?seconds=2', dict(kind='duration', id='hold-ok',
                     url=f'{base}/hold?seconds=2', budget={'hold_s': 3.0, 'min_sustained_s': 1.5})),
                    ('NEGATIVE', 'duration   /hold-flaky (рвёт)', dict(kind='duration', id='hold-flaky',
                     url=f'{base}/hold?seconds=6&flaky=1', budget={'hold_s': 4.0, 'min_sustained_s': 2.0})),
                    ('NEGATIVE', 'duration   /hold?seconds=0.3', dict(kind='duration', id='hold-short',
                     url=f'{base}/hold?seconds=0.3', budget={'hold_s': 2.0, 'min_sustained_s': 2.0})),
                    ('POSITIVE', 'media      /manifest.m3u8', dict(kind='media', id='media-ok',
                     url=f'{base}/manifest.m3u8', budget={'segment_max_bytes': 131_072})),
                    ('NEGATIVE', 'media      /manifest-empty', dict(kind='media', id='media-empty',
                     url=f'{base}{pr.REFERENCE_PROBE_NEGATIVE["media_no_manifest"]}',
                     budget={'segment_max_bytes': 65_536})),
                    ('NEGATIVE', 'media      /manifest-missing', dict(kind='media', id='media-404',
                     url=f'{base}{pr.REFERENCE_PROBE_NEGATIVE["media_manifest_missing"]}',
                     budget={'segment_max_bytes': 65_536})),
                    ('NEGATIVE', 'media      /manifest-bad-segment', dict(kind='media', id='media-bad',
                     url=f'{base}{pr.REFERENCE_PROBE_NEGATIVE["media_bad_segment"]}',
                     budget={'segment_max_bytes': 65_536})),
                ]
                for tag, name, data in rows:
                    outcome = await pr.run_capability(pr.validate_capability(data), options, transport)
                    print(f'  {tag:8} {name:36} state={outcome.state:16} code={str(outcome.code):20} '
                          f':: {outcome.detail}')

                print('\n--- F20: HTTP API assertions ' + '-' * 45)
                base_api = {'id': 'api', 'url': f'{base}/api/status', 'statuses': [200],
                            'content_type': 'application/json', 'min_body_bytes': 10,
                            'max_body_bytes': 65_536,
                            'json_assertions': [{'path': 'status', 'op': 'equals', 'value': 'ok'},
                                                {'path': 'quota.requests_per_minute', 'op': 'min', 'value': 10},
                                                {'path': 'regions.0.latency_ms', 'op': 'max', 'value': 100},
                                                {'path': 'limits.bandwidth_mbps', 'op': 'type', 'value': 'number'}]}
                good = await pr.run_probe(pr.validate_target(base_api), options, transport)
                print(f'  POSITIVE все утверждения          ok={good.ok} bytes={good.bytes} body_limit={good.body_limit}')
                for over, label in (({'url': f'{base}/api/status?status=degraded'}, 'degraded -> status != ok'),
                                    ({'url': f'{base}{pr.REFERENCE_PROBE_NEGATIVE["api_broken_json"]}'}, 'битый JSON'),
                                    ({'url': f'{base}/api/empty'}, 'пустое тело'),
                                    ({'json_assertions': [{'path': 'nope', 'op': 'exists'}]}, 'нет поля')):
                    outcome = await pr.run_probe(
                        pr.validate_target({**base_api, **over}), options, transport)
                    print(f'  NEGATIVE {label:30} ok={outcome.ok} code={outcome.code} :: {outcome.detail}')

                print('\n--- дефект 15: окно скорости ' + '-' * 41)
                for label, url, mb in (('2 МБ @2 МБ/с', f'{base}{THROTTLED}', 2_000_000),
                                       ('2 МБ без троттлинга', f'{base}/bytes/2000000', 2_000_000)):
                    target = pr.validate_speed_target({'url': url, 'max_bytes': mb})
                    outcome = await pr.run_speed_test(target, options, transport, limits=pr.SPEED_LIMITS)
                    print(f'  {label:22} state={outcome.state:12} mbps={str(outcome.mbps):8} bytes={outcome.bytes:8} '
                          f'chunks={outcome.chunks:4} ttfb={outcome.ttfb_ms}ms transfer={outcome.transfer_ms}ms')
                    print(f'  {'':22} {outcome.detail}')
                trace = pr.TransferTrace(url='x'); trace.begin(0.0); trace.add(2_000_000, 1.0); trace.finish(1.0)
                one = pr.measure_speed(trace)
                print(f'  {"один чанк":22} state={one.state} mbps={one.mbps} :: {one.detail}')
                trace = pr.TransferTrace(url='x'); trace.begin(0.0); trace.add(1_000_000, 0.5)
                trace.add(1_000_000, 1.5); trace.finish(1.5)
                two = pr.measure_speed(trace)
                print(f'  {"два чанка, окно 1 с":22} state={two.state} mbps={two.mbps} ttfb={two.ttfb_ms}ms '
                      f'transfer={two.transfer_ms}ms')

                print('\n--- дефект 15: cold и reused не смешиваются ' + '-' * 27)
                target = pr.validate_speed_target({'url': f'{base}{THROTTLED}', 'max_bytes': 2_000_000})
                cold = await pr.run_speed_test(target, options, transport, limits=LOCAL_LIMITS)
                reused = await pr.run_speed_test(target, options, transport, limits=LOCAL_LIMITS, reuse=True)
                for name, outcome in (('cold  ', cold), ('reused', reused)):
                    print(f'  {name} conn={outcome.connection:7} ttfb={outcome.ttfb_ms}ms '
                          f'transfer={outcome.transfer_ms}ms total={outcome.total_ms}ms')
                print(f'  mixed_speed_connections([cold,reused]) = {pr.mixed_speed_connections([cold, reused])}')
                print(f'  comparable_speed(...,\'cold\')={len(pr.comparable_speed([cold, reused], "cold"))} '
                      f'(...,\'reused\')={len(pr.comparable_speed([cold, reused], "reused"))}')
    asyncio.run(main())


if __name__ == '__main__':
    live_report()

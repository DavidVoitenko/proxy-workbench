"""Defect 15 / R10: an honest throughput window and an explicit insufficient state.

The old code started counting after the first chunk, so a one-chunk answer
produced an invented Mbit/s figure.  These tests pin the corrected arithmetic
with a fake clock: no download is performed anywhere in this file.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import probes as pr

LIMITS = pr.SpeedLimits(min_bytes=1_000_000, min_seconds=0.5, min_chunks=2, max_bytes=200_000_000)


def trace(chunks=((1_000_000, 1.0), (1_000_000, 2.0), (1_000_000, 3.0)), *, start=0.0, first=0.2, end=None,
          code=None, url='https://cdn.invalid/file'):
    """A trace with explicit timestamps: first byte at ``first``, chunks at the given times."""
    item = pr.TransferTrace(url=url).begin(start)
    item.chunks = 0
    item.bytes = 0
    item.first_byte_at = first
    for size, at in chunks:
        item.chunks += 1
        item.bytes += size
    item.finished_at = end if end is not None else (chunks[-1][1] if chunks else first)
    if code:
        item.code = code
    return item


class WindowTests(unittest.TestCase):
    def test_a_complete_transfer_uses_the_whole_body_window(self):
        result = pr.measure_speed(trace(start=0.0, first=0.2), LIMITS)
        self.assertEqual(result.state, 'ok')
        self.assertEqual(result.mbps, round(3_000_000 * 8 / 2.8 / 1e6, 2))
        self.assertEqual(result.bytes, 3_000_000)
        self.assertEqual(result.chunks, 3)

    def test_time_to_first_byte_is_reported_separately(self):
        result = pr.measure_speed(trace(start=0.0, first=0.2), LIMITS)
        self.assertEqual(result.ttfb_ms, 200.0)
        self.assertEqual(result.transfer_ms, 2800.0)
        self.assertEqual(result.total_ms, 3000.0)
        self.assertGreater(result.total_ms, result.transfer_ms)
        self.assertLess(result.transfer_ms, result.total_ms)

    def test_one_chunk_never_produces_a_number(self):
        result = pr.measure_speed(trace(chunks=((50_000_000, 1.0),), start=0.0, first=0.2), LIMITS)
        self.assertEqual(result.state, 'insufficient')
        self.assertIsNone(result.mbps)
        self.assertEqual(result.code, pr.INSUFFICIENT_SAMPLE)
        self.assertIn('чанк', result.detail)
        self.assertEqual(result.bytes, 50_000_000)

    def test_a_zero_length_window_is_not_a_speed(self):
        result = pr.measure_speed(trace(chunks=((600_000, 1.0), (600_000, 1.0)), start=1.0, first=1.0,
                                        end=1.0), LIMITS)
        self.assertEqual(result.state, 'insufficient')
        self.assertIsNone(result.mbps)

    def test_too_few_bytes_is_insufficient(self):
        result = pr.measure_speed(trace(chunks=((1_000, 1.0), (1_000, 3.0)), start=0.0, first=0.2), LIMITS)
        self.assertEqual(result.state, 'insufficient')
        self.assertIn('мало данных', result.detail)

    def test_too_short_a_window_is_insufficient(self):
        # first byte at 0.2 s, last byte at 0.4 s: the transfer window is 0.2 s
        result = pr.measure_speed(trace(chunks=((600_000, 0.3), (600_000, 0.4)), start=0.0, first=0.2), LIMITS)
        self.assertEqual(result.state, 'insufficient')
        self.assertIn('короче минимума', result.detail)

    def test_a_failed_transfer_is_an_error_without_a_number(self):
        result = pr.measure_speed(trace(chunks=((2_000_000, 1.0), (2_000_000, 2.0)), code=pr.READ_TIMEOUT), LIMITS)
        self.assertEqual(result.state, 'error')
        self.assertIsNone(result.mbps)
        self.assertEqual(result.code, pr.READ_TIMEOUT)
        self.assertEqual(result.bytes, 4_000_000)

    def test_an_unfinished_transfer_is_insufficient(self):
        item = trace(start=0.0, first=0.2)
        item.finished_at = None
        result = pr.measure_speed(item, LIMITS)
        self.assertEqual(result.state, 'insufficient')
        self.assertIsNone(result.mbps)
        self.assertIn('не завершён', result.detail)

    def test_a_transfer_over_the_limit_is_refused(self):
        tight = pr.SpeedLimits(min_bytes=1_000_000, min_seconds=0.5, min_chunks=2, max_bytes=2_000_000)
        result = pr.measure_speed(trace(chunks=((2_000_000, 1.0), (2_000_000, 3.0))), tight)
        self.assertEqual(result.state, 'insufficient')
        self.assertIn('максимум', result.detail)

    def test_public_view_has_every_unit_the_contract_needs(self):
        value = pr.measure_speed(trace(start=0.0, first=0.2), LIMITS).to_public()
        for key in ('state', 'mbps', 'bytes', 'ttfb_ms', 'transfer_ms', 'total_ms', 'chunks', 'code'):
            self.assertIn(key, value)
        self.assertEqual(value['state'], 'ok')

    def test_the_default_limits_are_documented_and_not_generous(self):
        self.assertGreaterEqual(pr.SPEED_LIMITS.min_bytes, 1 << 20)
        self.assertGreaterEqual(pr.SPEED_LIMITS.min_seconds, 0.25)
        self.assertGreaterEqual(pr.SPEED_LIMITS.min_chunks, 2)
        self.assertTrue(pr.SPEED_LIMITS.describe())


class SpeedRunTests(unittest.IsolatedAsyncioTestCase):
    def options(self, **extra):
        return pr.validate_options({'attempts': 1, 'whole_probe_timeout_s': 30, **extra})

    def target(self):
        return pr.validate_speed_target({'url': 'https://cdn.invalid/file', 'max_bytes': 5_000_000})

    def wire(self, result, calls=None):
        async def download(target, *, options):
            if calls is not None:
                calls.append((target, options))
            if isinstance(result, Exception):
                raise result
            return result

        return SimpleNamespace(download=download)

    async def test_a_real_transfer_is_measured(self):
        calls = []
        result = await pr.run_speed_test(self.target(), self.options(), self.wire(trace(), calls), limits=LIMITS)
        self.assertEqual(result.state, 'ok')
        self.assertIsNotNone(result.mbps)
        self.assertEqual(calls[0][0].max_bytes, 5_000_000)
        self.assertEqual(calls[0][1].whole_probe_timeout_s, 30)

    async def test_a_broken_proxy_is_an_error_not_a_number(self):
        result = await pr.run_speed_test(self.target(), self.options(),
                                         self.wire(ValueError('socks exploded')), limits=LIMITS)
        self.assertEqual(result.state, 'error')
        self.assertIsNone(result.mbps)
        self.assertEqual(result.code, 'ValueError')

    async def test_a_hanging_download_hits_the_whole_probe_deadline(self):
        async def hang(target, *, options):
            await asyncio.sleep(5)
            return trace()

        result = await pr.run_speed_test(
            self.target(),
            self.options(connect_timeout_s=0.1, handshake_timeout_s=0.1, read_timeout_s=0.1,
                         whole_probe_timeout_s=0.5),
            SimpleNamespace(download=hang), limits=LIMITS)
        self.assertEqual(result.state, 'error')
        self.assertEqual(result.code, pr.WHOLE_PROBE_TIMEOUT)
        self.assertIsNone(result.mbps)

    async def test_a_missing_target_is_refused(self):
        with self.assertRaises(pr.ProbeError):
            await pr.run_speed_test(None, self.options(), self.wire(trace()))
        with self.assertRaises(pr.ProbeError):
            await pr.run_speed_test(self.target(), self.options(whole_probe_timeout_s=0.5,
                                                               connect_timeout_s=4,
                                                               handshake_timeout_s=6,
                                                               read_timeout_s=8),
                                    self.wire(trace()))


if __name__ == '__main__':
    unittest.main()

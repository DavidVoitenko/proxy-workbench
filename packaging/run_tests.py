"""Run local mocks, optionally sharded, retaining tracebacks if CI times out."""
import argparse
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ImmediateResult(unittest.TextTestResult):
    def addError(self, test, err):
        super().addError(test, err)
        self._report_now(test, err)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._report_now(test, err)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._report_now(subtest, err)

    def _report_now(self, test, err):
        self.stream.writeln('\n' + self.separator1)
        self.stream.writeln(self.getDescription(test))
        self.stream.writeln(self._exc_info_to_string(err, test))
        self.stream.flush()


class ImmediateRunner(unittest.TextTestRunner):
    resultclass = ImmediateResult


def shard_suite(suite, shard_index=0, shard_count=1):
    """Keep each class together on one shard, preserving shared scenario state."""
    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError('shard-count must be positive and 0 <= shard-index < shard-count')
    if shard_count == 1:
        return suite
    selected = unittest.TestSuite()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            child = shard_suite(test, shard_index, shard_count)
            if child.countTestCases():
                selected.addTest(child)
        else:
            # Some end-to-end classes intentionally build on earlier methods.
            # Class fixtures and their scenarios must never be split between jobs.
            group = test.id() if isinstance(test, unittest.FunctionTestCase) else (
                f'{type(test).__module__}.{type(test).__qualname__}')
            digest = hashlib.sha256(group.encode('utf-8')).digest()
            if int.from_bytes(digest, 'big') % shard_count == shard_index:
                selected.addTest(test)
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shard-index', type=int, default=0, help='zero-indexed shard number')
    parser.add_argument('--shard-count', type=int, default=1, help='number of disjoint shards')
    args = parser.parse_args(argv)
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        parser.error('shard-count must be positive and 0 <= shard-index < shard-count')
    suite = unittest.defaultTestLoader.discover('tests')
    total = suite.countTestCases()
    suite = shard_suite(suite, args.shard_index, args.shard_count)
    if args.shard_count > 1:
        print(f'Running shard {args.shard_index + 1}/{args.shard_count}: '
              f'{suite.countTestCases()} of {total} tests', flush=True)
    result = ImmediateRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())

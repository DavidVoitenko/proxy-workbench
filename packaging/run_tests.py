"""Run the full local-mock suite, retaining tracebacks if a CI job times out."""
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


if __name__ == '__main__':
    unittest.main(module=None, argv=[sys.argv[0], 'discover', '-s', 'tests', '-v'],
                  testRunner=ImmediateRunner)

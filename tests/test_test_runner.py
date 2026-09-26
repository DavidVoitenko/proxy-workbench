"""CI shards must preserve the full suite and unittest fixture lifecycle."""
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest


spec = importlib.util.spec_from_file_location(
    'workbench_test_runner', Path(__file__).resolve().parents[1] / 'packaging' / 'run_tests.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from cases(test)
        else:
            yield test


class RunnerShardingTests(unittest.TestCase):
    def fixture_suite(self):
        # FunctionTestCase IDs are the unique function names; no fixture test is
        # discovered as an actual repository test.
        tests = []
        for index in range(30):
            def scenario():
                pass
            scenario.__name__ = f'scenario_{index:02d}'
            tests.append(unittest.FunctionTestCase(scenario))
        return unittest.TestSuite([unittest.TestSuite(tests[:15]), unittest.TestSuite(tests[15:])])

    def test_default_runs_the_entire_original_suite(self):
        suite = self.fixture_suite()
        self.assertIs(runner.shard_suite(suite), suite)
        self.assertEqual(suite.countTestCases(), 30)

    def test_shards_are_disjoint_and_cover_every_test_once(self):
        suite = self.fixture_suite()
        expected = {test.id() for test in cases(suite)}
        shards = [{test.id() for test in cases(runner.shard_suite(suite, index, 3))}
                  for index in range(3)]
        self.assertEqual(set.union(*shards), expected)
        self.assertEqual(sum(map(len, shards)), len(expected))
        for index, shard in enumerate(shards):
            self.assertTrue(shard)
            self.assertEqual(shard, {test.id() for test in cases(
                runner.shard_suite(self.fixture_suite(), index, 3))})

    def test_invalid_shard_arguments_are_refused(self):
        for index, count in [(-1, 3), (3, 3), (0, 0), (0, -1)]:
            with self.subTest(index=index, count=count), self.assertRaises(ValueError):
                runner.shard_suite(self.fixture_suite(), index, count)

    def test_selected_tests_keep_module_and_class_fixture_hooks(self):
        events = []
        module = types.ModuleType('_runner_fixture_hooks')
        module.setUpModule = lambda: events.append('module setup')
        module.tearDownModule = lambda: events.append('module teardown')
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__)

        class Scenario(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                events.append('class setup')

            @classmethod
            def tearDownClass(cls):
                events.append('class teardown')

            def test_one(self):
                events.append('test one')

            def test_two(self):
                events.append('test two')

        Scenario.__module__ = module.__name__
        whole = unittest.defaultTestLoader.loadTestsFromTestCase(Scenario)
        selected = next(suite for index in range(3)
                        if (suite := runner.shard_suite(whole, index, 3)).countTestCases())
        selected_count = selected.countTestCases()
        self.assertEqual(selected_count, whole.countTestCases(),
                         'a shared end-to-end class must run together')
        result = unittest.TextTestRunner(stream=io.StringIO()).run(selected)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(events[:2], ['module setup', 'class setup'])
        self.assertEqual(events[-2:], ['class teardown', 'module teardown'])
        self.assertEqual(len(events), selected_count + 4)


if __name__ == '__main__':
    unittest.main()

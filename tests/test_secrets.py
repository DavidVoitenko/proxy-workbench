"""Whole secrets.py suite in one command: `.venv/bin/python -m unittest tests.test_secrets`.

Every case lives in its own file so that a single area can be run on its own
(`.venv/bin/python -m unittest tests.test_secrets_access`, and so on).
"""
import sys
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests import (test_secrets_access, test_secrets_endpoint, test_secrets_policy,
                   test_secrets_redaction, test_secrets_vault, test_secrets_verifier)

MODULES = (test_secrets_verifier, test_secrets_vault, test_secrets_access,
           test_secrets_endpoint, test_secrets_policy, test_secrets_redaction)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for module in MODULES:
        suite.addTests(loader.loadTestsFromModule(module))
    return suite


if __name__ == '__main__':
    unittest.main()

"""HTTPS must verify in a frozen build, where OpenSSL's default paths are gone."""
from __future__ import annotations

import ssl
import unittest
from unittest import mock

import certifi

from proxy_workbench import tls


class DefaultContextTests(unittest.TestCase):
    def test_the_certifi_roots_are_loaded_even_when_the_system_paths_are_missing(self):
        # A frozen app points OpenSSL at a folder of the build machine.  Loading
        # nothing from there must still leave a context that trusts public roots.
        with mock.patch.object(ssl.SSLContext, 'load_default_certs', lambda self, purpose=None: None):
            context = tls.default_context()
        self.assertGreater(context.cert_store_stats()['x509_ca'], 50)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_a_custom_bundle_replaces_the_defaults_and_keeps_verification(self):
        context = tls.default_context(certifi.where())
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_every_outgoing_path_uses_the_shared_context(self):
        from proxy_workbench import probes, proxytool
        self.assertGreater(proxytool.TLS.cert_store_stats()['x509_ca'], 50)
        self.assertGreater(probes.build_ssl_context().cert_store_stats()['x509_ca'], 50)


if __name__ == '__main__':
    unittest.main()

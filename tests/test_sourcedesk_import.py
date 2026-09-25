"""Supported subscription imports: Clash and sing-box (F27).

The rule under test is narrow: a configuration document is data.  Only the
endpoint list is read, nothing that steers traffic is followed, and nothing in
the document is executed.
"""
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import proxytool
from proxy_workbench import sourcedesk as sd

#: Documentation ranges, so no test ever points at a real proxy.
HTTP = '198.51.100.7'
HTTP2 = '198.51.100.21'
SOCKS = '198.51.100.11'

# The user's own subscription may name a private address or a hostname; the
# canonicalization point stays proxytool, and the desk only injects it.
normalize = proxytool.normalize_custom


def clash(*proxies, **sections):
    document = {'proxies': list(proxies)}
    document.update(sections)
    return json.dumps(document)


def http_proxy(name, server=HTTP, port=8080, **extra):
    entry = {'name': name, 'type': 'http', 'server': server, 'port': port}
    entry.update(extra)
    return entry


class ClashImportTests(unittest.TestCase):
    def test_json_document_yields_canonical_endpoints(self):
        result = sd.import_clash(clash(http_proxy('US', HTTP, 8080), {'name': 'S', 'type': 'socks5',
                                                                      'server': SOCKS, 'port': 1080}),
                                 normalize=normalize)
        self.assertEqual(result.outcome, 'ok')
        self.assertEqual(result.canonical, (f'http://{HTTP}:8080', f'socks5://{SOCKS}:1080'))

    def test_block_yaml_document_is_supported(self):
        document = (
            'port: 7890\n'
            'proxies:\n'
            '  - name: "US http"\n'
            '    type: http\n'
            '    server: ' + HTTP + '\n'
            '    port: 8080\n'
            '  - name: socks\n'
            '    type: socks5\n'
            '    server: ' + SOCKS + '\n'
            '    port: 1080\n'
            '    udp: true\n'
            'rules:\n'
            '  - MATCH,auto\n'
        )
        result = sd.import_clash(document, normalize=normalize)
        self.assertEqual(result.canonical, (f'http://{HTTP}:8080', f'socks5://{SOCKS}:1080'))
        self.assertIn('section:rules', result.ignored)

    def test_traffic_sections_are_recorded_and_never_read(self):
        result = sd.import_clash(clash(http_proxy('a'), **{
            'rules': ['MATCH,DIRECT'],
            'proxy-groups': [{'name': 'auto', 'type': 'url-test'}],
            'rule-providers': {'r': {'type': 'http', 'behavior': 'domain'}},
            'proxy-providers': {'p': {'url': 'https://x.invalid/a.yaml'}},
            'script': {'code': 'console.log(1)'},
            'dns': {'enable': True},
            'tun': {'enable': True},
        }), normalize=normalize)
        self.assertEqual(result.canonical, (f'http://{HTTP}:8080',))
        for section in ('rules', 'proxy-groups', 'rule-providers', 'proxy-providers', 'script'):
            self.assertIn(f'section:{section}', result.ignored)
        self.assertIn('script:script', result.ignored)
        self.assertIn('script:rule-providers', result.ignored)

    def test_group_members_are_not_imported_twice(self):
        result = sd.import_clash(clash(http_proxy('a'),
                                       **{'proxy-groups': [{'name': 'auto', 'type': 'url-test',
                                                           'proxies': ['a']}]}),
                                 normalize=normalize)
        self.assertEqual(len(result.endpoints), 1)

    def test_unsupported_transport_is_reported_not_dropped_silently(self):
        result = sd.import_clash(clash(http_proxy('a'), {'name': 'ss', 'type': 'ss', 'server': SOCKS,
                                                         'port': 8388}), normalize=normalize)
        self.assertEqual(result.outcome, 'partial')
        self.assertIn(('E_IMPORT_FORMAT', 'ss'), result.rejected)

    def test_entry_with_credentials_is_rejected(self):
        result = sd.import_clash(clash(http_proxy('a', username='u', password='p')), normalize=normalize)
        self.assertEqual(result.endpoints, ())
        self.assertIn(('E_SECRET_CREDENTIALS', 'a'), result.rejected)

    def test_tls_flag_turns_http_into_https_capability(self):
        result = sd.import_clash(clash(http_proxy('a', tls=True)), normalize=normalize)
        self.assertEqual(result.canonical, (f'https://{HTTP}:8080',))
        self.assertIn(('supports_https', 'true'), result.endpoints[0].declared)

    def test_declared_metadata_is_allowlisted(self):
        result = sd.import_clash(clash(http_proxy('a', country='DE', provider='acme',
                                                 anonymity='elite', some_blob={'x': 1})),
                                 normalize=normalize)
        declared = dict(result.endpoints[0].declared)
        self.assertEqual(declared['country'], 'DE')
        self.assertEqual(declared['provider'], 'acme')
        self.assertEqual(declared['anonymity'], 'elite')
        self.assertNotIn('some_blob', declared)

    def test_unsupported_type_is_reported_before_its_credentials(self):
        result = sd.import_clash(clash({'name': 'ss', 'type': 'ss', 'server': SOCKS,
                                        'port': 8388, 'password': 'p'}), normalize=normalize)
        self.assertEqual(result.rejected, (('E_IMPORT_FORMAT', 'ss'),))

    def test_entry_without_port_is_rejected(self):
        result = sd.import_clash(clash({'name': 'a', 'type': 'http', 'server': HTTP}), normalize=normalize)
        self.assertEqual(result.endpoints, ())
        self.assertEqual(result.rejected, (('E_VALIDATION_FIELD', 'a'),))

    def test_document_without_proxies_is_empty(self):
        result = sd.import_clash(json.dumps({'port': 7890}), normalize=normalize)
        self.assertEqual(result.outcome, 'empty')
        self.assertEqual(result.endpoints, ())

    def test_proxies_of_the_wrong_type_is_invalid(self):
        result = sd.import_clash(json.dumps({'proxies': {'a': 1}}), normalize=normalize)
        self.assertEqual(result.outcome, 'invalid')
        self.assertEqual(result.rejected[0][0], 'E_VALIDATION_SCHEMA')

    def test_yaml_anchors_and_aliases_are_refused(self):
        document = 'defaults: &base\n  type: http\nproxies:\n  - <<: *base\n    name: a\n'
        result = sd.import_clash(document, normalize=normalize)
        self.assertEqual(result.outcome, 'invalid')
        self.assertEqual(result.rejected[0][0], 'E_IMPORT_FORMAT')

    def test_tabs_in_indentation_are_refused(self):
        result = sd.import_clash('proxies:\n\t- name: a\n', normalize=normalize)
        self.assertEqual(result.outcome, 'invalid')

    def test_public_normalizer_refuses_documentation_ranges(self):
        # Proves the injection point is the only canonicalizer in play.
        result = sd.import_clash(clash(http_proxy('a')), normalize=proxytool.normalize)
        self.assertEqual(result.endpoints, ())
        self.assertEqual(result.rejected, (('E_VALIDATION_FIELD', 'a'),))


class SingboxImportTests(unittest.TestCase):
    def test_http_and_socks_outbounds_become_endpoints(self):
        document = json.dumps({'outbounds': [
            {'type': 'http', 'tag': 'a', 'server': HTTP, 'server_port': 8080},
            {'type': 'socks', 'tag': 'b', 'server': SOCKS, 'server_port': 1080, 'version': '5'},
        ]})
        result = sd.import_singbox(document, normalize=normalize)
        self.assertEqual(result.canonical, (f'http://{HTTP}:8080', f'socks5://{SOCKS}:1080'))

    def test_socks_version_is_never_guessed(self):
        document = json.dumps({'outbounds': [{'type': 'socks', 'tag': 'a', 'server': SOCKS,
                                              'server_port': 1080}]})
        result = sd.import_singbox(document, normalize=normalize)
        self.assertEqual(result.endpoints, ())
        self.assertEqual(result.rejected, (('E_VALIDATION_FIELD', 'a'),))
        four = sd.import_singbox(json.dumps({'outbounds': [{'type': 'socks', 'tag': 'a', 'server': SOCKS,
                                                           'server_port': 1080, 'version': '4'}]}),
                                    normalize=normalize)
        self.assertEqual(four.canonical, (f'socks4://{SOCKS}:1080',))

    def test_tls_outbound_is_https_capability(self):
        document = json.dumps({'outbounds': [{'type': 'http', 'tag': 'a', 'server': HTTP,
                                              'server_port': 8080, 'tls': {'enabled': True}}]})
        result = sd.import_singbox(document, normalize=normalize)
        self.assertEqual(result.canonical, (f'https://{HTTP}:8080',))

    def test_groups_are_skipped_without_recursion(self):
        document = json.dumps({'outbounds': [
            {'type': 'selector', 'tag': 'grp', 'outbounds': [{'type': 'http', 'tag': 'inner',
                                                              'server': HTTP2, 'server_port': 3128}]},
            {'type': 'urltest', 'tag': 'auto', 'outbounds': ['inner']},
        ]})
        result = sd.import_singbox(document, normalize=normalize)
        self.assertEqual(result.endpoints, ())
        self.assertEqual(result.ignored, ('outbound:selector:grp', 'outbound:urltest:auto'))

    def test_direct_and_block_are_not_endpoints(self):
        document = json.dumps({'outbounds': [{'type': 'direct', 'tag': 'd'},
                                             {'type': 'block', 'tag': 'b'},
                                             {'type': 'http', 'tag': 'a', 'server': HTTP, 'server_port': 8080}]})
        result = sd.import_singbox(document, normalize=normalize)
        self.assertEqual(result.canonical, (f'http://{HTTP}:8080',))
        self.assertIn('outbound:direct:d', result.ignored)
        self.assertIn('outbound:block:b', result.ignored)

    def test_vmess_and_shadowsocks_are_unsupported_not_guessed(self):
        document = json.dumps({'outbounds': [
            {'type': 'vmess', 'tag': 'v', 'server': HTTP, 'server_port': 443},
            {'type': 'shadowsocks', 'tag': 's', 'server': HTTP, 'server_port': 8388,
             'method': 'aes-128-gcm', 'password': 'p'},
        ]})
        result = sd.import_singbox(document, normalize=normalize)
        self.assertEqual(result.endpoints, ())
        self.assertEqual(result.rejected, (('E_IMPORT_FORMAT', 'v'), ('E_IMPORT_FORMAT', 's')))

    def test_credentials_in_an_outbound_are_rejected(self):
        document = json.dumps({'outbounds': [{'type': 'http', 'tag': 'a', 'server': HTTP,
                                              'server_port': 8080, 'username': 'u', 'password': 'p'}]})
        result = sd.import_singbox(document, normalize=normalize)
        self.assertEqual(result.rejected, (('E_SECRET_CREDENTIALS', 'a'),))

    def test_route_rules_are_never_read(self):
        document = json.dumps({'outbounds': [{'type': 'http', 'tag': 'a', 'server': HTTP, 'server_port': 8080}],
                               'route': {'rules': [{'action': 'hijack-dns'}], 'final': 'a'},
                               'dns': {'servers': [{'tag': 'local'}]}})
        result = sd.import_singbox(document, normalize=normalize)
        self.assertEqual(result.canonical, (f'http://{HTTP}:8080',))
        self.assertIn('section:route', result.ignored)


class NoExecutionTests(unittest.TestCase):
    """A document must never be able to run anything on the user's machine."""

    def test_script_section_is_not_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            canary = Path(temp) / 'executed.txt'
            document = json.dumps({
                'proxies': [http_proxy('a')],
                'script': {'code': f"__import__('pathlib').Path({str(canary)!r}).write_text('x')"},
            })
            result = sd.import_clash(document, normalize=normalize)
            self.assertEqual(result.canonical, (f'http://{HTTP}:8080',))
            self.assertIn('script:script', result.ignored)
            self.assertFalse(canary.exists())

    def test_name_field_is_a_label_not_an_expression(self):
        with tempfile.TemporaryDirectory() as temp:
            canary = Path(temp) / 'label.txt'
            document = json.dumps({
                'proxies': [http_proxy("__import__('pathlib').Path(%r).write_text('x')" % str(canary))],
            })
            result = sd.import_clash(document, normalize=normalize)
            self.assertEqual(result.canonical, (f'http://{HTTP}:8080',))
            self.assertFalse(canary.exists())

    def test_module_never_calls_an_execution_primitive(self):
        # Structural, not textual: a mention in a comment is not a call.
        tree = ast.parse(Path(sd.__file__).read_text(encoding='utf-8'))
        called, imported = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function = node.func
                called.add(function.id if isinstance(function, ast.Name) else getattr(function, 'attr', ''))
            elif isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split('.')[0])
        for forbidden in ('eval', 'exec', '__import__', 'system', 'popen', 'import_module'):
            self.assertNotIn(forbidden, called, forbidden)
        for forbidden in ('subprocess', 'os', 'importlib', 'runpy'):
            self.assertNotIn(forbidden, imported, forbidden)


class LimitTests(unittest.TestCase):
    def test_oversized_document_has_its_own_outcome(self):
        limits = sd.ImportLimits(max_bytes=64)
        result = sd.import_clash(clash(http_proxy('a')), normalize=normalize, limits=limits)
        self.assertEqual(result.outcome, 'limit_exceeded')
        self.assertEqual(result.rejected[0][0], 'E_LIMIT_BODY')

    def test_entry_budget_is_enforced(self):
        limits = sd.ImportLimits(max_entries=1)
        result = sd.import_clash(clash(http_proxy('a'), http_proxy('b', HTTP2, 3128)),
                                 normalize=normalize, limits=limits)
        self.assertEqual(len(result.endpoints), 1)

    def test_invalid_limits_are_refused(self):
        for bad in (sd.ImportLimits(max_bytes=0), sd.ImportLimits(max_depth=-1), 'x', 7):
            with self.subTest(bad=bad), self.assertRaises(sd.SourceDeskError):
                sd.import_clash(clash(http_proxy('a')), normalize=normalize, limits=bad)

    def test_non_utf8_document_is_refused(self):
        result = sd.import_clash(b'proxies:\n  - name: "\xff\xfe"\n', normalize=normalize)
        self.assertEqual(result.outcome, 'invalid')
        self.assertEqual(result.rejected[0][0], 'E_IMPORT_FORMAT')


class DispatchTests(unittest.TestCase):
    def test_dispatch_by_declared_format(self):
        self.assertEqual(sd.import_subscription(clash(http_proxy('a')), 'clash',
                                                 normalize=normalize).canonical,
                         (f'http://{HTTP}:8080',))
        self.assertEqual(sd.import_subscription(json.dumps(
            {'outbounds': [{'type': 'http', 'tag': 'a', 'server': HTTP, 'server_port': 8080}]}),
            'singbox', normalize=normalize).canonical, (f'http://{HTTP}:8080',))

    def test_unknown_format_is_refused(self):
        with self.assertRaises(sd.SourceDeskError) as caught:
            sd.import_subscription('{}', 'vmess-link')
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_summary_is_serializable_and_keeps_claims_apart(self):
        summary = sd.import_clash(clash(http_proxy('a', country='DE')), normalize=normalize).summary()
        self.assertEqual(json.loads(json.dumps(summary))['declared'],
                         {f'http://{HTTP}:8080': {'name': 'a', 'country': 'DE'}})


if __name__ == '__main__':
    unittest.main()

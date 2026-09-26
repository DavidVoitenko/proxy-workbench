"""sing-box: a reject the target client version actually reads.

R14 / defect 20.  The evidence behind the version rules, read while writing the
module:

* https://sing-box.sagernet.org/migration/ — "1.11.0 deprecated the special
  ``block`` and ``dns`` outbounds", migration "Migrate legacy special outbounds
  to rule actions", before ``"outbound": "block"`` → after ``"action": "reject"``.
* https://sing-box.sagernet.org/configuration/route/rule_action/ — ``reject`` is
  a valid rule action, and a ``route`` action requires an ``outbound``.
* https://sing-box.sagernet.org/configuration/outbound/urltest/ — the member is
  ``outbounds`` and ``interval`` takes a duration such as ``"5m"``.
* https://sing-box.sagernet.org/configuration/route/ — ``final`` is present with
  no deprecation notice.

The docs read do not say whether an empty ``outbounds`` list is accepted, so
that one shape is marked unverified and the pinned client decides; a JSON parse
is never treated as client acceptance.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import core, exportsvc as es, formats
from tests.client_check_support import client_checker

NOW = 1_700_000_000.0
ROWS = [dict(proxy='http://203.0.113.7:8080', country='DE', checked_at=NOW, valid_until=NOW + 600),
        dict(proxy='socks5://198.51.100.9:1080', country='NL', checked_at=NOW, valid_until=NOW + 600)]


def scope():
    return es.ExportScope(identity=core.Scope('col-1', 'prof-1', 1))


class TargetTests(unittest.TestCase):
    def test_versions_are_parsed_and_gated(self):
        # 1.15.0 is the newest release the current official docs describe
        # (https://sing-box.sagernet.org/migration/ names it), so it is inside
        # the verified window; 1.99.0 is past what the docs were read for.
        cases = {'1.8.0': (True, False), '1.10.2': (True, False), '1.11.0': (True, True),
                 'v1.12': (True, True), '1.14.0': (True, True), '1.15.0': (True, True),
                 'latest': (True, True), '1.99.0': (False, True), '0.9.0': (False, False)}
        for version, (supported, rule_actions) in cases.items():
            with self.subTest(version=version):
                target = es.singbox_target(version)
                self.assertEqual(target.supported, supported)
                self.assertEqual(target.uses_rule_actions, rule_actions)

    def test_the_legacy_reject_is_a_named_opt_in_and_nothing_else(self):
        target = es.singbox_target(es.SINGBOX_LEGACY_OPTIN)
        self.assertTrue(target.supported)
        self.assertTrue(target.legacy_optin)
        self.assertFalse(target.configured)
        self.assertFalse(target.uses_rule_actions)
        self.assertEqual([item['type'] for item in json.loads(
            es.render_singbox([], target=target))['outbounds']], ['block'])
        for version in (None, '', 'unconfigured'):
            with self.subTest(version=version):
                self.assertFalse(es.singbox_target(version).version_dependent_reject_available)

    def test_an_unparsable_version_is_reported_not_guessed(self):
        for version in ('nightly', 'x.y.z', '1.x', '1.11.0-rc1'):
            with self.subTest(version=version):
                target = es.singbox_target(version)
                self.assertFalse(target.supported)
                self.assertEqual(target.reason, 'E_EXPORT_TARGET_UNKNOWN')

    def test_no_pinned_version_keeps_the_shipped_shape_and_says_it_is_unconfigured(self):
        for version in (None, ''):
            with self.subTest(version=version):
                target = es.singbox_target(version)
                self.assertTrue(target.supported)
                self.assertFalse(target.uses_rule_actions)
                self.assertEqual(target.state, 'unconfigured')
                self.assertFalse(target.configured)

    def test_a_version_newer_than_the_verified_one_is_refused(self):
        target = es.singbox_target('1.99.0')
        self.assertFalse(target.supported)
        self.assertEqual(target.reason, 'E_EXPORT_TARGET_UNVERIFIED')
        with self.assertRaises(es.ExportError) as caught:
            target.require()
        self.assertEqual(caught.exception.code, 'E_EXPORT_TARGET_UNVERIFIED')
        with self.assertRaises(es.ExportError) as caught:
            es.render_singbox(ROWS, target='1.99.0')
        self.assertEqual(caught.exception.code, 'E_EXPORT_TARGET_UNVERIFIED')

    def test_an_old_version_is_refused_rather_than_misrendered(self):
        with self.assertRaises(es.ExportError) as caught:
            es.render_singbox(ROWS, target='0.9.0')
        self.assertEqual(caught.exception.code, 'E_EXPORT_TARGET_UNSUPPORTED')


class LegacyTargetTests(unittest.TestCase):
    def test_before_1_11_the_output_is_the_block_outbound(self):
        # ``formats.singbox`` no longer defaults to the form deprecated in
        # 1.11.0, so a pre-1.11 client is asked for it by name.
        self.assertEqual(es.render_singbox(ROWS, target='1.10.2'), formats.singbox(ROWS))
        empty = es.render_singbox([], target='1.10.2')
        self.assertEqual(empty, formats.singbox([], fail_closed='block'))
        self.assertEqual([item['type'] for item in json.loads(empty)['outbounds']], ['block'])
        self.assertNotIn('action', json.loads(empty)['route'])

    def test_a_set_with_outbounds_carries_no_version_dependent_construct(self):
        # The reject is the only construct whose form depends on the version, so
        # a usable set never needs one: at every target it keeps its outbounds,
        # and no target ever produces the deprecated special outbound.
        for version in (None, '1.10.2', '1.11.0', '1.14.0', 'latest', es.SINGBOX_LEGACY_OPTIN):
            with self.subTest(version=version):
                config = json.loads(es.render_singbox(ROWS, target=version))
                types = [item.get('type') for item in config['outbounds']]
                self.assertEqual(types[0], 'urltest')
                self.assertNotIn('block', types)
                self.assertNotIn('dns', types)
                self.assertNotIn('direct', types)
                self.assertEqual(sorted(types[1:]), ['http', 'socks'])
                self.assertEqual(config['route']['final'], 'auto')

    def test_an_unpinned_target_writes_no_reject_at_all(self):
        # Without a target there is no version, and the two reject forms are
        # both guesses: one deprecated since 1.11.0, one whose acceptance the
        # docs do not state.  The file is refused and the reason is the code.
        with self.assertRaises(es.ExportError) as caught:
            es.render_singbox([], target=None)
        self.assertEqual(caught.exception.code, 'E_EXPORT_TARGET_UNPINNED')

    def test_switching_the_engine_over_keeps_the_file_a_usable_set_produces(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
            artifact = es.write_snapshot(home, ROWS, scope=scope(),
                                         options=es.ExportOptions(published_at=NOW), now=NOW)
            self.assertEqual((artifact.directory / 'singbox.json').read_text(encoding='utf-8'),
                             formats.singbox(ROWS))
            empty = es.write_snapshot(home, [], scope=scope(),
                                      options=es.ExportOptions(published_at=NOW), now=NOW)
            self.assertFalse((empty.directory / 'singbox.json').exists())
            status = json.loads((empty.directory / 'status.json').read_text(encoding='utf-8'))
            singbox = status['compat']['files']['singbox.json']
            self.assertFalse(singbox['written'])
            self.assertEqual(singbox['reasons'], ['E_EXPORT_TARGET_UNPINNED'])
            self.assertTrue(status['client_target_required'])
            # every other file of the fail-closed artifact is still there
            for name in ('proxies.txt', 'clash.yaml', 'proxy.pac', 'ranked.json'):
                self.assertTrue((empty.directory / name).is_file(), name)


class RuleActionTargetTests(unittest.TestCase):
    def rendered(self, rows, version):
        return json.loads(es.render_singbox(rows, target=version))

    def test_from_1_11_the_empty_set_is_refused_by_a_route_rule(self):
        config = self.rendered([], '1.12.0')
        self.assertEqual(config['outbounds'], [])
        self.assertEqual(config['route']['rules'], [{'action': 'reject'}])
        self.assertNotIn('final', config['route'])
        self.assertEqual([item['type'] for item in config['outbounds']], [])

    def test_the_special_outbound_disappears_at_1_11(self):
        config = self.rendered([], '1.11.0')
        self.assertNotIn('block', [item.get('type') for item in config['outbounds']])
        self.assertNotIn('dns', [item.get('type') for item in config['outbounds']])

    def test_a_usable_set_keeps_its_normal_shape_at_every_version(self):
        for version in ('1.10.2', '1.11.0', '1.14.0'):
            with self.subTest(version=version):
                config = self.rendered(ROWS, version)
                types = [item['type'] for item in config['outbounds']]
                self.assertEqual(types[0], 'urltest')
                self.assertEqual(sorted(types[1:]), ['http', 'socks'])
                self.assertEqual(config['route']['final'], 'auto')
                self.assertEqual(config['inbounds'][0]['type'], 'mixed')
                self.assertEqual(config['inbounds'][0]['listen'], '127.0.0.1')
                self.assertNotIn('direct', types)

    def test_socks_version_field_follows_the_scheme(self):
        config = self.rendered(ROWS, '1.12.0')
        versions = {item['tag'].split()[1]: item.get('version') for item in config['outbounds'][1:]}
        self.assertEqual(versions, {'http': None, 'socks5': '5'})
        self.assertEqual(config['outbounds'][0]['outbounds'],
                         [item['tag'] for item in config['outbounds'][1:]])

    def test_the_structural_check_accepts_its_own_output(self):
        for version in ('1.10.2', '1.11.0', '1.14.0'):
            for rows in (ROWS, []):
                with self.subTest(version=version, rows=len(rows)):
                    config = self.rendered(rows, version)
                    ok, reasons = es.check_singbox(config, version)
                    if not ok and rows == [] and version != '1.10.2':
                        # the empty-outbounds question is the documented gap
                        self.assertEqual(reasons, ('E_EXPORT_EMPTY_OUTBOUNDS_UNVERIFIED',))
                    else:
                        self.assertEqual((ok, reasons), (True, ()))

    def test_the_structural_check_refuses_a_shape_the_target_rejects(self):
        legacy_shape = {'outbounds': [{'type': 'block', 'tag': 'blocked'}], 'route': {'final': 'blocked'}}
        self.assertEqual(es.check_singbox(legacy_shape, '1.12.0')[0], False)
        modern_shape = {'outbounds': [], 'route': {'rules': [{'action': 'reject'}]}}
        self.assertEqual(es.check_singbox(modern_shape, '1.10.2')[0], False)
        with_direct = {'outbounds': [{'type': 'direct', 'tag': 'direct'}], 'route': {}}
        ok, reasons = es.check_singbox(with_direct, '1.12.0')
        self.assertFalse(ok)
        self.assertIn('E_EXPORT_DIRECT_FORBIDDEN', reasons)

    def test_a_final_pointing_nowhere_is_refused(self):
        config = {'outbounds': [{'type': 'urltest', 'tag': 'auto', 'outbounds': []}],
                  'route': {'final': 'ghost', 'rules': [{'action': 'reject'}]}}
        self.assertEqual(es.check_singbox(config, '1.12.0')[0], False)


class ClientCheckTests(unittest.TestCase):
    def test_without_a_pinned_client_the_result_is_never_passed(self):
        result, detail = es.client_check('{}', '1.12.0')
        self.assertEqual(result, 'not_run')
        self.assertIn('pinned client', detail)

    def test_a_missing_binary_is_not_run_and_not_passed(self):
        result, detail = es.client_check('{}', '1.12.0', binary='/nonexistent/sing-box')
        self.assertEqual(result, 'not_run')
        self.assertIn('not found', detail)

    def test_the_artifact_says_the_client_check_was_not_performed(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
            artifact = es.write_snapshot(home, ROWS, scope=scope(),
                                         options=es.ExportOptions(published_at=NOW,
                                                                  client_target='1.12.0'),
                                         now=NOW)
            status = json.loads((artifact.directory / 'status.json').read_text(encoding='utf-8'))
            singbox = status['compat']['files']['singbox.json']
            self.assertEqual(singbox['target'], '1.12.0')
            self.assertEqual(singbox['client_check'], 'not_run')
            self.assertEqual(status['client_target'], '1.12.0')

    def test_a_client_that_rejects_the_file_stops_the_generation(self):
        with tempfile.TemporaryDirectory() as home:
            script = client_checker(home, accepts=False)
            with self.assertRaises(es.ExportError) as caught:
                es.write_snapshot(home, ROWS, scope=scope(),
                                  options=es.ExportOptions(published_at=NOW, client_target='1.12.0',
                                                           client_binary=str(script)),
                                  now=NOW)
            self.assertEqual(caught.exception.code, 'E_EXPORT_CLIENT_REJECTED')
            self.assertEqual(list((Path(home) / 'generations').iterdir()), [])

    def test_a_client_that_accepts_the_file_is_recorded_as_passed(self):
        with tempfile.TemporaryDirectory() as home:
            script = client_checker(home, accepts=True)
            artifact = es.write_snapshot(home, ROWS, scope=scope(),
                                         options=es.ExportOptions(published_at=NOW,
                                                                  client_target='1.12.0',
                                                                  client_binary=str(script)),
                                         now=NOW)
            status = json.loads((artifact.directory / 'status.json').read_text(encoding='utf-8'))
            self.assertEqual(status['compat']['files']['singbox.json']['client_check'], 'passed')


if __name__ == '__main__':
    unittest.main()

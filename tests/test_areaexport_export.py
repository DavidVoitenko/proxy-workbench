"""F28 / defect 7 / defect 20: exports and the consistent snapshot.

What the code did before this file existed, proven by running it:

* ``ExportOptions.client_target`` and ``client_binary`` were filled from
  nowhere.  ``singbox_target(None)`` answered ``unconfigured``, so every empty
  or unsupported-only artifact shipped the ``block`` outbound that sing-box
  deprecated in 1.11.0, marked ``client_check: not_run`` - and
  ``check_singbox(config, '1.14.0')`` refused the very file
  ``write_snapshot`` had just written.
* ``formats.singbox`` produced that deprecated form by default, so the
  read-only ``/singbox`` endpoint served it to every caller.

These tests pin the contract that replaces it: a version-dependent construct is
never written without a target that decides its form, the deprecated form is
reachable only by an explicit named request, and a pinned target is settable
from this module alone.
"""
import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import core, exportsvc as es, formats

NOW = 1_700_000_000.0
ROWS = [dict(proxy='http://203.0.113.7:8080', country='DE', latency_ms=120.0, score=88.0,
             reliability=1.0, min_target_reliability=1.0, successes=3, requests=3,
             checked_at=NOW, valid_until=NOW + 600),
        dict(proxy='socks5://198.51.100.9:1080', country='NL', latency_ms=90.0, score=91.0,
             reliability=1.0, min_target_reliability=1.0, successes=3, requests=3,
             checked_at=NOW, valid_until=NOW + 600)]
ONLY_HTTPS = [dict(proxy='https://203.0.113.7:443', country='DE', checked_at=NOW, valid_until=NOW + 600)]
EXPIRED = [dict(proxy='http://203.0.113.7:8080', country='DE', checked_at=NOW - 9000,
                valid_until=NOW - 3600, successes=1, requests=3)]


def scope(**overrides):
    base = dict(identity=core.Scope('col-1', 'prof-1', 1))
    base.update(overrides)
    return es.ExportScope(**base)


def options(**overrides):
    base = dict(published_at=NOW)
    base.update(overrides)
    return es.ExportOptions(**base)


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, rows, **kwargs):
        return es.write_snapshot(self.home, rows, scope=kwargs.pop('scope', scope()),
                                 now=NOW, **kwargs)

    def status(self, artifact):
        return json.loads((artifact.directory / 'status.json').read_text(encoding='utf-8'))


# ---------------------------------------------------------------------------
# defect 7: an exported selection is its own artifact and moves nothing
# ---------------------------------------------------------------------------

class SelectionDoesNotMoveThePoolTests(Temp):
    def setUp(self):
        super().setUp()
        self.published = self.write(ROWS)
        es.publish(self.published, self.home, confirm=True)

    def test_highlighting_rows_keeps_the_active_generation_and_its_rows(self):
        picked = [ROWS[0]]
        before_pointer = es.read_pointer(self.home)
        before = es.load_snapshot(self.home)

        selection = self.write(picked, kind='selection',
                               scope=scope(selection=(ROWS[0]['proxy'],)),
                               options=options(client_target='1.14.0'))

        self.assertEqual(selection.kind, 'selection')
        self.assertNotEqual(selection.generation, self.published.generation)
        self.assertEqual(es.read_pointer(self.home).generation, before_pointer.generation)
        after = es.load_snapshot(self.home)
        self.assertEqual([row['proxy'] for row in after.rows], [row['proxy'] for row in before.rows])
        self.assertEqual(len(after.rows), 2)
        # the selection is a full, separately readable generation
        chosen = es.load_snapshot(self.home, selection.generation)
        self.assertEqual([row['proxy'] for row in chosen.rows], [ROWS[0]['proxy']])

    def test_a_selection_can_never_reach_the_active_pointer(self):
        selection = self.write(ROWS[:1], kind='selection', options=options(client_target='1.14.0'))
        for pointer in (es.POINTER_NAME, es.DIAGNOSTIC_POINTER_NAME):
            with self.subTest(pointer=pointer):
                with self.assertRaises(es.ExportError) as caught:
                    es.publish(selection, self.home, confirm=True, pointer_name=pointer)
                self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')
        self.assertEqual(es.read_pointer(self.home).generation, self.published.generation)

    def test_a_top_n_slice_and_a_search_slice_are_separate_artifacts_too(self):
        top = self.write(ROWS, options=options(top=1, client_target='1.14.0'))
        self.assertEqual(len(es.load_snapshot(self.home, top.generation).rows), 1)
        search = self.write(ROWS, scope=scope(query='socks'), options=options(client_target='1.14.0'))
        self.assertNotEqual(top.generation, search.generation)
        self.assertEqual(es.read_pointer(self.home).generation, self.published.generation)

    def test_the_scope_digest_ignores_the_selection_so_both_describe_the_same_area(self):
        selection = scope(selection=(ROWS[0]['proxy'],), top=1)
        self.assertEqual(selection.digest(), scope().digest())
        self.assertNotEqual(selection.artifact_digest('selection', 'redact', ROWS),
                            scope().artifact_digest('published', 'redact', ROWS))


# ---------------------------------------------------------------------------
# defect 20: empty / unsupported-only / expired never become a DIRECT, and the
# version-dependent reject is never written without a target that decides it
# ---------------------------------------------------------------------------

class UnpinnedTargetTests(Temp):
    def test_no_file_is_written_when_the_reject_form_cannot_be_decided(self):
        artifact = self.write([])
        self.assertFalse((artifact.directory / 'singbox.json').exists())
        singbox = self.status(artifact)['compat']['files']['singbox.json']
        self.assertFalse(singbox['written'])
        self.assertEqual(singbox['reasons'], ['E_EXPORT_TARGET_UNPINNED'])

    def test_the_refusal_is_an_actionable_generation_error_not_a_silent_simplification(self):
        status = self.status(self.write(ONLY_HTTPS))
        singbox = status['compat']['files']['singbox.json']
        self.assertFalse(singbox['written'])
        self.assertEqual(singbox['reasons'], ['E_EXPORT_TARGET_UNPINNED'])
        self.assertTrue(singbox['fail_closed'])
        self.assertTrue(any('client_target' in warning for warning in status['compat']['warnings']))
        # the row is not lost: the formats that can carry it still do
        self.assertEqual(status['exported'], 1)
        self.assertEqual((artifact_path := self.write(ONLY_HTTPS).directory / 'proxies.txt').read_text(),
                         'https://203.0.113.7:443\n')
        self.assertTrue(artifact_path.is_file())

    def test_the_render_call_itself_refuses_rather_than_picking_a_shape(self):
        for rows in ([], ONLY_HTTPS):
            with self.subTest(rows=len(rows)):
                with self.assertRaises(es.ExportError) as caught:
                    es.render_singbox(rows)
                self.assertEqual(caught.exception.code, 'E_EXPORT_TARGET_UNPINNED')

    def test_every_other_file_of_a_fail_closed_artifact_is_still_written(self):
        artifact = self.write([])
        for name in ('proxies.txt', 'hostport.txt', 'ranked.json', 'ranked.csv', 'http.txt',
                     'proxychains.txt', 'proxy.pac', 'clash.yaml', 'snapshot.txt', 'status.json'):
            self.assertTrue((artifact.directory / name).is_file(), name)
        self.assertNotIn('singbox.json', artifact.files)
        self.assertEqual(json.loads((artifact.directory / 'ranked.json').read_text()), [])
        self.assertIn('- MATCH,REJECT', (artifact.directory / 'clash.yaml').read_text())

    def test_a_set_with_usable_outbounds_needs_no_version_and_is_still_written(self):
        artifact = self.write(ROWS)
        config = json.loads((artifact.directory / 'singbox.json').read_text(encoding='utf-8'))
        self.assertEqual([item['type'] for item in config['outbounds']], ['urltest', 'http', 'socks'])
        self.assertEqual(config['route']['final'], 'auto')
        self.assertNotIn('block', [item['type'] for item in config['outbounds']])
        status = self.status(artifact)
        self.assertEqual(status['client_target_source'], 'unconfigured')
        self.assertTrue(status['client_target_required'])
        self.assertEqual(status['compat']['files']['singbox.json']['client_check'], 'not_run')
        self.assertTrue(any('not checked by a target client' in warning
                            for warning in status['compat']['warnings']))

    def test_no_artifact_ever_carries_a_direct_outbound(self):
        for rows, kwargs in ((ROWS, {}), ([], {}), (ONLY_HTTPS, {}), (ROWS, {'client_target': '1.14.0'}),
                             ([], {'client_target': '1.14.0'}), ([], {'client_target': '1.10.0'}),
                             ([], {'client_target': es.SINGBOX_LEGACY_OPTIN})):
            with self.subTest(rows=len(rows), kwargs=kwargs):
                artifact = self.write(rows, options=options(**kwargs))
                path = artifact.directory / 'singbox.json'
                if not path.is_file():
                    continue
                config = json.loads(path.read_text(encoding='utf-8'))
                self.assertNotIn('direct', [item.get('type') for item in config['outbounds']])
                self.assertNotIn('direct', str(config.get('route')))

    def test_the_deprecated_block_outbound_is_reachable_only_by_name(self):
        artifact = self.write([], options=options(client_target=es.SINGBOX_LEGACY_OPTIN))
        config = json.loads((artifact.directory / 'singbox.json').read_text(encoding='utf-8'))
        self.assertEqual([item['type'] for item in config['outbounds']], ['block'])
        status = self.status(artifact)
        self.assertEqual(status['client_target_state'], 'legacy_optin')
        self.assertTrue(any('deprecated since sing-box 1.11.0' in warning
                            for warning in status['compat']['warnings']))

    def test_a_pinned_client_below_1_11_still_gets_the_form_it_reads(self):
        artifact = self.write([], options=options(client_target='1.10.0'))
        config = json.loads((artifact.directory / 'singbox.json').read_text(encoding='utf-8'))
        self.assertEqual([item['type'] for item in config['outbounds']], ['block'])

    def test_a_pinned_modern_client_gets_the_rule_action_reject(self):
        artifact = self.write([], options=options(client_target='1.14.0'))
        config = json.loads((artifact.directory / 'singbox.json').read_text(encoding='utf-8'))
        self.assertEqual(config['outbounds'], [])
        self.assertEqual(config['route']['rules'], [{'action': 'reject'}])
        self.assertNotIn('final', config['route'])
        self.assertEqual(es.check_singbox(config, '1.14.0'),
                         (False, ('E_EXPORT_EMPTY_OUTBOUNDS_UNVERIFIED',)))

    def test_an_expired_set_keeps_its_row_its_own_deadline_and_its_file(self):
        artifact = self.write(EXPIRED, options=options(client_target='1.14.0'))
        self.assertEqual((artifact.directory / 'proxies.txt').read_text(), 'http://203.0.113.7:8080\n')
        # the set's own lifetime is separate from the row's: one expired member
        # does not make the whole set look expired, and vice versa
        status = self.status(artifact)
        self.assertEqual(status['expires_at'], NOW + 900)
        row = json.loads((artifact.directory / 'ranked.json').read_text())[0]
        self.assertEqual(row['valid_until'], NOW - 3600)
        snapshot = (artifact.directory / 'snapshot.txt').read_text(encoding='utf-8')
        self.assertIn(f'# expires_at: {NOW + 900}', snapshot)
        self.assertIn('cannot revoke itself', snapshot)
        self.assertIn('Re-check rows past expires_at', snapshot)

    def test_an_empty_policy_of_error_refuses_the_whole_generation(self):
        with self.assertRaises(es.ExportError) as caught:
            self.write([], options=options(empty_policy='error'))
        self.assertEqual(caught.exception.code, 'E_STATE_NO_PROXIES')
        self.assertEqual(list((self.home / 'generations').iterdir()), [])


# ---------------------------------------------------------------------------
# the target is settable from this module alone
# ---------------------------------------------------------------------------

class ClientTargetResolutionTests(Temp):
    def test_nothing_is_pinned_by_default(self):
        resolved = es.resolve_client_target(es.ExportOptions(), directory=self.home, environ={})
        self.assertEqual(resolved.target.state, 'unconfigured')
        self.assertEqual(resolved.source, 'unconfigured')
        self.assertIsNone(resolved.binary)
        self.assertFalse(resolved.verified_by)

    def test_the_environment_pins_a_target_without_a_caller_change(self):
        resolved = es.resolve_client_target(es.ExportOptions(), directory=self.home, environ={
            es.CLIENT_TARGET_ENV: '1.12.0', es.CLIENT_BINARY_ENV: '/opt/bin/sing-box'})
        self.assertEqual(resolved.target.version, '1.12.0')
        self.assertEqual(resolved.target.state, 'configured')
        self.assertEqual(resolved.source, es.CLIENT_TARGET_ENV)
        self.assertEqual(resolved.binary, '/opt/bin/sing-box')
        self.assertTrue(resolved.verified_by)

    def test_a_sidecar_next_to_the_snapshots_pins_a_target_for_every_run(self):
        (self.home / es.CLIENT_SIDECAR_NAME).write_text(
            json.dumps({'target': '1.14.0', 'binary': '/opt/bin/sing-box'}), encoding='utf-8')
        artifact = self.write([])
        status = self.status(artifact)
        self.assertEqual(status['client_target'], '1.14.0')
        self.assertEqual(status['client_target_source'], es.CLIENT_SIDECAR_NAME)
        self.assertEqual(status['client_binary'], '/opt/bin/sing-box')
        self.assertFalse(status['client_target_required'])
        self.assertTrue((artifact.directory / 'singbox.json').is_file())

    def test_options_win_over_the_environment_and_the_sidecar(self):
        (self.home / es.CLIENT_SIDECAR_NAME).write_text(json.dumps({'target': '1.14.0'}), encoding='utf-8')
        resolved = es.resolve_client_target(es.ExportOptions(client_target='1.11.0'), directory=self.home,
                                            environ={es.CLIENT_TARGET_ENV: '1.12.0'})
        self.assertEqual((resolved.target.version, resolved.source), ('1.11.0', 'options'))

    def test_a_broken_sidecar_is_nothing_pinned_not_a_crash(self):
        (self.home / es.CLIENT_SIDECAR_NAME).write_text('{not json', encoding='utf-8')
        self.assertEqual(es.read_client_sidecar(self.home), {})
        artifact = self.write([])
        self.assertFalse((artifact.directory / 'singbox.json').is_file())
        self.assertTrue(self.status(artifact)['client_target_required'])

    def test_a_version_the_docs_do_not_cover_is_refused_with_the_reason(self):
        for version, code in (('1.99.0', 'E_EXPORT_TARGET_UNVERIFIED'),
                              ('nightly', 'E_EXPORT_TARGET_UNKNOWN'),
                              ('0.9.0', 'E_EXPORT_TARGET_UNSUPPORTED')):
            with self.subTest(version=version):
                with self.assertRaises(es.ExportError) as caught:
                    self.write([], options=options(client_target=version))
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn('{', caught.exception.detail)

    def test_a_client_that_rejects_the_file_stops_the_generation(self):
        script = self.home / 'fake-sing-box'
        script.write_text('#!/bin/sh\necho "unsupported outbound" >&2\nexit 1\n', encoding='utf-8')
        script.chmod(0o755)
        with self.assertRaises(es.ExportError) as caught:
            self.write(ROWS, options=options(client_target='1.14.0', client_binary=str(script)))
        self.assertEqual(caught.exception.code, 'E_EXPORT_CLIENT_REJECTED')
        self.assertEqual(list((self.home / 'generations').iterdir()), [])

    def test_a_client_that_accepts_the_file_is_recorded_as_passed(self):
        script = self.home / 'fake-sing-box'
        script.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        script.chmod(0o755)
        artifact = self.write(ROWS, options=options(client_target='1.14.0', client_binary=str(script)))
        singbox = self.status(artifact)['compat']['files']['singbox.json']
        self.assertEqual(singbox['client_check'], 'passed')
        self.assertEqual(self.status(artifact)['compat']['client_check'], 'passed')

    def test_a_binary_that_is_not_installed_is_never_reported_as_passed(self):
        artifact = self.write(ROWS, options=options(client_target='1.14.0',
                                                     client_binary=str(self.home / 'absent')))
        singbox = self.status(artifact)['compat']['files']['singbox.json']
        self.assertEqual(singbox['client_check'], 'not_run')
        self.assertIn('not found', singbox['client_detail'])


# ---------------------------------------------------------------------------
# the read-only endpoint's own format module
# ---------------------------------------------------------------------------

class FormatModuleTests(unittest.TestCase):
    """``api.py`` calls ``formats.singbox(rows)`` with no target, so the default
    is the only thing that decides what the legacy ``/singbox`` endpoint serves."""

    def test_the_default_reject_is_the_form_the_current_documentation_describes(self):
        config = json.loads(formats.singbox([]))
        self.assertEqual(config['outbounds'], [])
        self.assertEqual(config['route'], {'rules': [{'action': 'reject'}]})
        self.assertNotIn('block', str(config))

    def test_the_deprecated_form_still_exists_for_a_client_that_needs_it(self):
        config = json.loads(formats.singbox([], fail_closed=formats.FAIL_CLOSED_BLOCK))
        self.assertEqual([item['type'] for item in config['outbounds']], ['block'])

    def test_an_unknown_reject_form_is_refused(self):
        with self.assertRaises(ValueError):
            formats.singbox([], fail_closed='direct')

    def test_a_usable_set_is_the_same_file_either_way(self):
        self.assertEqual(formats.singbox(ROWS), formats.singbox(ROWS, fail_closed=formats.FAIL_CLOSED_BLOCK))

    def test_clash_and_pac_never_grow_a_direct(self):
        for rows in (ROWS, [], ONLY_HTTPS, EXPIRED):
            self.assertNotIn('DIRECT', formats.clash(rows))
            self.assertNotIn('DIRECT', formats.pac([row['proxy'] for row in rows]))


# ---------------------------------------------------------------------------
# every format of one artifact is valid and every dropped row is explained
# ---------------------------------------------------------------------------

class EveryFormatIsValidTests(Temp):
    def setUp(self):
        super().setUp()
        self.artifact = self.write(ROWS, options=options(client_target='1.14.0'))
        self.directory = self.artifact.directory

    def read(self, name):
        return (self.directory / name).read_text(encoding='utf-8')

    def test_the_file_set_is_the_contracted_one(self):
        self.assertEqual({'proxies.txt', 'snapshot.txt', 'hostport.txt', 'ranked.json', 'ranked.csv',
                          'http.txt', 'https.txt', 'socks4.txt', 'socks5.txt', 'proxychains.txt',
                          'proxy.pac', 'clash.yaml', 'singbox.json'},
                         {path.name for path in self.directory.iterdir() if path.name != 'status.json'})

    def test_txt_and_the_protocol_files_carry_one_address_per_line(self):
        self.assertEqual(self.read('proxies.txt'),
                         'http://203.0.113.7:8080\nsocks5://198.51.100.9:1080\n')
        self.assertEqual(self.read('hostport.txt'), '203.0.113.7:8080\n198.51.100.9:1080\n')
        self.assertEqual(self.read('http.txt'), '203.0.113.7:8080\n')
        self.assertEqual(self.read('socks5.txt'), '198.51.100.9:1080\n')
        self.assertEqual(self.read('https.txt'), '')
        self.assertEqual(self.read('socks4.txt'), '')

    def test_csv_parses_with_the_legacy_columns_intact(self):
        table = list(csv.DictReader(io.StringIO(self.read('ranked.csv'))))
        self.assertEqual([row['proxy'] for row in table], [row['proxy'] for row in ROWS])
        for name in ('proxy', 'score', 'latency_ms', 'reliability', 'successes', 'checked_at',
                     'valid_until', 'reputation_status', 'country', 'asn', 'hosting', 'mbps',
                     'protocol', 'age_seconds', 'admission_reason', 'access_id'):
            self.assertIn(name, table[0], name)

    def test_json_round_trips_and_hides_no_row(self):
        rows = json.loads(self.read('ranked.json'))
        self.assertEqual([row['proxy'] for row in rows], [row['proxy'] for row in ROWS])

    def test_pac_is_a_function_browsers_can_load_without_direct(self):
        pac = self.read('proxy.pac')
        self.assertIn('function FindProxyForURL(url, host)', pac)
        self.assertIn('PROXY 203.0.113.7:8080; SOCKS5 198.51.100.9:1080', pac)
        self.assertNotIn('DIRECT', pac)

    def test_clash_is_yaml_that_names_every_member(self):
        clash = self.read('clash.yaml')
        body = clash.split('proxy-groups:', 1)[0]
        members = [json.loads(line.strip()[2:]) for line in body.splitlines() if line.startswith('  - {')]
        self.assertEqual(len(members), 2)
        group_line = next(line for line in clash.splitlines()
                          if line.startswith('  - {') and 'url-test' in line)
        group = json.loads(group_line.strip()[2:])
        self.assertEqual(group['proxies'], [member['name'] for member in members])
        self.assertIn('- MATCH,auto', clash)

    def test_proxychains_lists_what_it_supports_with_its_header(self):
        chains = self.read('proxychains.txt')
        self.assertTrue(chains.startswith('#'))
        self.assertIn('http 203.0.113.7 8080', chains)
        self.assertIn('socks5 198.51.100.9 1080', chains)

    def test_singbox_parses_and_names_every_outbound(self):
        config = json.loads(self.read('singbox.json'))
        urltest = config['outbounds'][0]
        self.assertEqual(urltest['outbounds'], [item['tag'] for item in config['outbounds'][1:]])
        self.assertEqual(config['route']['final'], 'auto')

    def test_an_unsupported_row_is_explained_in_every_format_that_cannot_carry_it(self):
        artifact = self.write(ONLY_HTTPS, options=options(client_target='1.14.0'))
        status = self.status(artifact)
        reasons = {item['format']: item['reasons'] for item in status['compat']['unsupported']}
        self.assertEqual(reasons['clash'], ['E_EXPORT_TLS_UNSUPPORTED'])
        self.assertEqual(reasons['singbox'], ['E_EXPORT_TLS_UNSUPPORTED'])
        self.assertNotIn('txt', reasons)   # txt can carry it, so it is not "unsupported" anywhere
        self.assertTrue(status['compat']['files']['clash.yaml']['fail_closed'])
        self.assertEqual(status['compat']['files']['clash.yaml']['state_detail'], 'empty_no_match')
        # a dropped row is not a lost row
        self.assertEqual(status['exported'], 1)
        self.assertEqual((artifact.directory / 'proxies.txt').read_text(), 'https://203.0.113.7:443\n')

    def test_the_static_txt_says_it_cannot_revoke_itself(self):
        text = self.read('snapshot.txt')
        self.assertIn('# Proxy Workbench snapshot.', text)
        self.assertIn(f'# generation: {self.artifact.generation}', text)
        self.assertIn('cannot revoke itself', text)
        for line in text.splitlines():
            if not line.startswith('#'):
                self.assertIn(line, [row['proxy'] for row in ROWS])

    def test_the_manifest_covers_every_file_and_a_tampered_one_is_refused(self):
        status = self.status(self.artifact)
        self.assertEqual(status['manifest'], self.artifact.manifest)
        self.assertEqual(set(status['manifest']), set(self.artifact.files) - {'status.json'})
        for name, entry in status['manifest'].items():
            self.assertEqual(entry['bytes'], (self.directory / name).stat().st_size, name)
        os.chmod(self.directory / 'proxies.txt', 0o644)
        (self.directory / 'proxies.txt').write_text('http://198.51.100.1:1\n', encoding='utf-8')
        with self.assertRaises(es.ExportError) as caught:
            es.load_snapshot(self.home, self.artifact.generation)
        self.assertEqual(caught.exception.code, 'E_STATE_SNAPSHOT_MANIFEST')


if __name__ == '__main__':
    unittest.main()

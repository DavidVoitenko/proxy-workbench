"""Browser form regressions that do not require a live proxy scan."""

import json
from pathlib import Path
import shutil
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import web_support as ws
from proxy_workbench import gui


PERMISSIONS_DOM = r"""
const esc = value => String(value);
const t = key => key;
const fieldset = (items) => ({querySelectorAll: () => items});
const grid = {
  groups: [], permissions: [],
  set innerHTML(html) {
    this.groups = [];
    this.permissions = [];
    for (const block of html.matchAll(/<fieldset class="perm-group">([\s\S]*?)<\/fieldset>/g)) {
      const group = {checked: false, indeterminate: false, dataset: {
        permGroup: block[1].match(/data-perm-group="([^"]+)"/)[1]
      }};
      const items = [];
      for (const match of block[1].matchAll(/<input type="checkbox" data-permission="([^"]+)"([^>]*)>/g)) {
        const valueMatch = match[2].match(/\bvalue="([^"]+)"/);
        const node = {value: valueMatch ? valueMatch[1] : 'on',
          checked: /\bchecked\b/.test(match[2])};
        node.closest = () => fieldset(items);
        items.push(node);
        this.permissions.push(node);
      }
      group.closest = () => fieldset(items);
      this.groups.push(group);
    }
  },
  querySelectorAll(selector) {
    if (selector === '[data-perm-group]') return this.groups;
    if (selector === '[data-permission]') return this.permissions;
    return [];
  }
};
const $ = id => id === 'keys-permissions' ? grid : null;
let keysState = {view: null, permissions: [], groups: {}};
"""


class PermissionFormTests(unittest.TestCase):
    def test_named_permissions_survive_refresh_and_group_toggles(self):
        source = ws.js_slice('function renderPermissions(view)', 'function showKeySecret(issued, message)')
        script = PERMISSIONS_DOM + source + r"""
const view = {permissions: ['read.results', 'write.views', 'export.secret'],
  read_permissions: ['read.results'], write_permissions: ['write.views'], admin_permissions: []};
renderPermissions(view);
setPermissions(['read.results', 'export.secret']);
const first = selectedPermissions();
renderPermissions(view);
const refreshed = selectedPermissions();
const write = grid.groups.find(group => group.dataset.permGroup === 'write.*');
write.checked = true;
write.onchange();
const afterGroup = selectedPermissions();
const read = grid.permissions.find(item => item.value === 'read.results');
read.checked = false;
read.onchange();
process.stdout.write(JSON.stringify({first, refreshed, afterGroup,
  afterSingle: selectedPermissions(),
  readGroup: grid.groups.find(group => group.dataset.permGroup === 'read.*').checked}));
"""
        value = json.loads(ws.node_ok(script))
        self.assertEqual(value['first'], ['read.results', 'export.secret'])
        self.assertEqual(value['refreshed'], value['first'])
        self.assertEqual(value['afterGroup'], ['read.results', 'write.views', 'export.secret'])
        self.assertEqual(value['afterSingle'], ['write.views', 'export.secret'])
        self.assertFalse(value['readGroup'])

    def test_a_source_action_restores_the_button(self):
        source = ws.js_slice('async function sourceAction(path, button, message)', "if ($('prune-sources'))")
        script = r"""
const t = key => key;
const button = {disabled: false, textContent: 'Update', dataset: {}};
const sources = {value: ''};
const $ = id => id === 'sources' ? sources : null;
const getSettings = () => ({});
const updateSourceCount = () => {};
const updateCodeEditors = () => {};
const toast = () => {};
let settings = {};
let fail = false;
const api = async () => {if (fail) throw Error('network'); return {settings: {sources: ['https://example.org/list']}};};
""" + source + r"""
(async () => {
  await sourceAction('/api/sources/update', button, () => 'done');
  const success = [button.textContent, button.disabled, sources.value];
  fail = true;
  await sourceAction('/api/sources/update', button, () => 'done');
  process.stdout.write(JSON.stringify({success, failure: [button.textContent, button.disabled]}));
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        value = json.loads(ws.node_ok(script))
        self.assertEqual(value['success'], ['Update', False, 'https://example.org/list'])
        self.assertEqual(value['failure'], ['Update', False])

    def test_editing_import_text_invalidates_inflight_preview(self):
        source = ws.js_slice('let importState =', '// Pools and schedules')
        script = r"""
const t = key => key;
const toast = () => {};
const fields = {
  'import-preview': {disabled: false},
  'import-commit': {disabled: true, dataset: {}},
  'import-text': {value: '198.51.100.7:8080'},
  'import-collection': {value: 'first'},
  'import-format': {value: ''},
  'import-mode': {value: 'merge'},
  'import-mapping-grid': {closest: () => ({hidden: true})},
  'import-preview-body': {innerHTML: ''},
  'imp-count': {textContent: '0'},
  'import-note': {textContent: ''},
};
const $ = id => fields[id] || null;
let finish;
const api = async () => new Promise(resolve => {finish = resolve;});
""" + source + r"""
(async () => {
  setupImportListeners();
  const pending = runImportPreview();
  fields['import-text'].value = '198.51.100.8:8080';
  fields['import-text'].oninput();
  finish({counts: {added: 1}, batch_id: 'old-preview'});
  await pending;
  process.stdout.write(JSON.stringify({plan: importState.plan, busy: importState.busy,
    applyDisabled: fields['import-commit'].disabled, previewDisabled: fields['import-preview'].disabled,
    count: fields['imp-count'].textContent}));
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        value = json.loads(ws.node_ok(script))
        self.assertIsNone(value['plan'])
        self.assertFalse(value['busy'])
        self.assertTrue(value['applyDisabled'])
        self.assertFalse(value['previewDisabled'])
        self.assertEqual(value['count'], '0')

    def test_import_mapping_offers_every_column_and_accepts_index_zero(self):
        home = ws.build_data([], publish=False)
        app = gui.App(home)
        try:
            plan = app.import_preview({
                'collection': 'public-base', 'format': 'csv', 'name': 'ambiguous.csv',
                'text': 'host,ip,port\n11.0.0.1,11.0.0.2,8080\n'
            })
        finally:
            app.close()
            shutil.rmtree(home)
        self.assertTrue(plan['needs_mapping'])
        self.assertEqual(plan['columns'], ['host', 'ip', 'port'])
        self.assertEqual(plan['mapping_suggestion']['columns'], plan['columns'])

        render = ws.js_slice('function renderImportMapping(suggestion, problem)', 'function renderImportPreview(plan)')
        mapping = ws.js_slice('function mappingFromForm()', 'async function runImportPreview()')
        script = r"""
const esc = value => String(value);
const t = key => key;
const box = {hidden: true};
const grid = {innerHTML: '', closest: () => box, querySelectorAll: () => [
  {dataset: {mappingRole: 'host'}, value: '0'},
  {dataset: {mappingRole: 'port'}, value: '2'},
  {dataset: {mappingRole: 'scheme'}, value: ''},
  {dataset: {mappingRole: 'country'}, value: ''},
]};
const $ = id => id === 'import-mapping' ? box : id === 'import-mapping-grid' ? grid : null;
""" + render + mapping + "\n" + "const plan = " + json.dumps(plan) + ";\n" + r"""
renderImportMapping(plan.mapping_suggestion, plan.mapping_problem);
process.stdout.write(JSON.stringify({html: grid.innerHTML, hidden: box.hidden, mapping: mappingFromForm()}));
"""
        result = json.loads(ws.node_ok(script))
        self.assertFalse(result['hidden'])
        self.assertIn('<option value="1">ip</option>', result['html'])
        self.assertEqual(result['mapping'], {'host': 0, 'port': 2, 'scheme': None, 'country': None})


if __name__ == '__main__':
    unittest.main()

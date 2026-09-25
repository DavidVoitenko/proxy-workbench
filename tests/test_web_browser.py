"""The browser side of the interface, exercised under node.

Defect 21 (download), defect 24 (scenario presets), defect 25 (live feed) and
the F19 client helpers are pure functions of the page, so they are run with a
small stub environment instead of a browser.
"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import web_support as ws

PRELUDE = """
const t = (key, values={}) => String(key).replace(/\\{(\\w+)\\}/g, (m, n) => n in values ? values[n] : m);
const esc = value => String(value == null ? '' : value);
const fmt = value => String(value);
globalThis.__toasts = [];
const lang = 'en';
const token = 'test-token';
const latencyBadge = value => value + 'ms';
const toast = message => { globalThis.__toasts.push(String(message)); };
async function api(path, body) { return globalThis.__api(path, body); }
const activeQuickFilter = () => '';
const resultHostingFilter = () => '';
let offset = 0;
const selectedProxies = new Set();
const state = {all_columns: ['check','num','proxy','score','latency','jitter','speed','success','uptime','cleanliness','anonymity','country','provider','age','actions']};
function updateSelectionUI() {}
const document = {
  _nodes: {},
  getElementById() { return null; },
  createElement() { return {click() { globalThis.__clicked = (globalThis.__clicked || 0) + 1; }, remove() {}, dataset: {}, style: {}}; },
  body: {appendChild() {}},
  querySelectorAll() { return []; }
};
globalThis.__lookup = null;
const $ = id => globalThis.__lookup ? globalThis.__lookup(id) : document.getElementById(id);
const URL = {createObjectURL: () => 'blob:stub', revokeObjectURL() {}};
const setTimeout = (fn) => { };
"""


def run(source, body, prelude=PRELUDE):
    # node -e runs as CommonJS, so an async body needs its own function.
    wrapped = '(async () => {\n' + body + '\n})().catch(error => { console.error(error); process.exitCode = 1; });'
    return ws.node_ok(prelude + '\n' + source + '\n' + wrapped)


class DownloadTests(unittest.TestCase):
    """Defect 21: picker inside the activation, one close, cancel is not an error."""

    def source(self):
        return ws.js_slice('function isPickerCancel(error)', 'function renderTheme()')

    def harness(self, *, picker=None):
        prelude = PRELUDE + f"""
globalThis.__pickerCalls = 0;
globalThis.__closes = 0;
globalThis.__pipeOptions = [];
globalThis.__fetchOrder = [];
window = {{}};
window.showSaveFilePicker = {picker or 'undefined'};
globalThis.__failRequest = false;
const fetch = async (path) => {{
  globalThis.__fetchOrder.push(path);
  if (globalThis.__failRequest) return {{ok: false}};
  return {{
    ok: true,
    body: {{
      pipeTo: async (writable, options) => {{
        globalThis.__pipeOptions.push(options || null);
        globalThis.__writable = writable;
      }}
    }},
    blob: async () => 'blob-data'
  }};
}};
async function writableOf(handle) {{ return handle._writable; }}
"""
        return prelude

    def test_the_picker_is_opened_before_any_await(self):
        prelude = self.harness(picker="() => { globalThis.__pickerCalls += 1; return Promise.resolve({createWritable: () => ({close: async () => { globalThis.__closes += 1; }})}); }")
        out = run(self.source(), """
const node = {disabled: false, dataset: {}, textContent: 'Download'};
globalThis.__result = await downloadFile('proxies.txt', node);
process.stdout.write(JSON.stringify({picker: globalThis.__pickerCalls, closes: globalThis.__closes,
  order: globalThis.__fetchOrder, options: globalThis.__pipeOptions, toasts: globalThis.__toasts}));
""", prelude)
        answer = json.loads(out)
        self.assertEqual(answer['picker'], 1, 'the picker was not opened')
        self.assertEqual(answer['order'], ['/api/download/proxies.txt'])
        self.assertEqual(answer['closes'], 1, 'the stream must be closed exactly once')
        self.assertEqual(answer['options'], [{'preventClose': True}],
                         'pipeTo must not close the destination itself')
        self.assertEqual(answer['toasts'], [])

    def test_cancelling_the_picker_is_not_an_error(self):
        prelude = self.harness(picker="() => { const e = new Error('cancelled'); e.name = 'AbortError'; return Promise.reject(e); }")
        out = run(self.source(), """
const node = {disabled: false, dataset: {}, textContent: 'Download'};
await downloadFile('proxies.txt', node);
process.stdout.write(JSON.stringify({closes: globalThis.__closes, toasts: globalThis.__toasts,
  disabled: node.disabled}));
""", prelude)
        answer = json.loads(out)
        self.assertEqual(answer['closes'], 0)
        self.assertEqual(answer['toasts'], [], 'a cancelled picker must stay silent')
        self.assertFalse(answer['disabled'], 'the button stayed disabled after a cancel')

    def test_a_browser_without_the_picker_still_saves_the_file(self):
        out = run(self.source(), """
const node = {disabled: false, dataset: {}, textContent: 'Download'};
await downloadFile('proxies.txt', node);
process.stdout.write(JSON.stringify({closes: globalThis.__closes, clicked: globalThis.__clicked || 0,
  toasts: globalThis.__toasts}));
""", self.harness())
        answer = json.loads(out)
        self.assertEqual(answer['closes'], 0)
        self.assertEqual(answer['clicked'], 1, 'the fallback anchor never fired')
        self.assertEqual(answer['toasts'], [])

    def test_a_failed_request_reports_a_message_and_keeps_the_button_usable(self):
        out = run(self.source(), """
const node = {disabled: false, dataset: {}, textContent: 'Download'};
globalThis.__failRequest = true;
await downloadFile('proxies.txt', node);
process.stdout.write(JSON.stringify({toasts: globalThis.__toasts, disabled: node.disabled}));
""", self.harness())
        answer = json.loads(out)
        self.assertEqual(len(answer['toasts']), 1)
        self.assertFalse(answer['disabled'])


class ScenarioPresetTests(unittest.TestCase):
    """Defect 24: a scenario owns every field it changes and claims nothing extra."""

    def source(self):
        parts = ws.js_slice('const SCENARIO_FIELDS = [', 'function setupEnhancedListeners()')
        return parts

    def harness(self):
        return PRELUDE + """
const fields = {
  'protocol': {value: 'all'}, 'connect_timeout': {value: 4}, 'timeout': {value: 8},
  'prefilter': {value: 512}, 'workers': {value: 128}, 'watch': {value: 0},
  'min_anonymity': {value: 'any'}, 'request-profile': {value: 'workbench'},
  'fail_fast': {value: true, checked: true, tagName: 'INPUT'}, 'speedtest-url': {value: ''},
  'judge-url': {value: ''}, 'dnsbl-zones': {value: ''},
  'dnsbl-enabled': {value: false, checked: false, tagName: 'INPUT'},
  'strict-clean': {value: false, checked: false, tagName: 'INPUT'},
  'min_success': {value: '0.6666666666666666', tagName: 'SELECT', options: [{value: '0.6666666666666666'}, {value: '1'}, {value: '0'}]},
  'max_latency': {value: 0}, 'countries': {value: ''}, 'dnsbl-fields': {classList: {toggle() {}}}
};
globalThis.__lookup = id => fields[id] || null;
function syncQuickServiceChips() {}
function syncAllPresetChips() {}
function updateIdentity() {}
function setScenarioTargets(list) { globalThis.__targets = list; }
function highlightChangedInputs(list) {}
"""

    def test_every_scenario_declares_a_complete_field_set(self):
        out = run(self.source(), """
process.stdout.write(JSON.stringify({
  managed: SCENARIO_FIELDS.map(entry => entry[0]),
  scenarios: SCENARIOS.map(s => ({id: s.id, fields: Object.keys(s.fields || {}),
    targets: (s.targets || []).map(t => t.url)}))
}));
""", self.harness())
        payload = json.loads(out)
        scenarios = {item['id']: item for item in payload['scenarios']}
        for name in ('telegram', 'youtube', 'anon', 'scrape'):
            self.assertIn(name, scenarios)
            self.assertTrue(scenarios[name]['targets'], f'{name} must state its services')
        for item in scenarios.values():
            for field in item['fields']:
                self.assertIn(field, payload['managed'],
                              f'{item["id"]} changes a field no reset knows about')

    def test_switching_scenarios_leaves_no_parameter_of_the_previous_one(self):
        out = run(self.source(), """
function applyScenario(id) {
  const scenario = scenarioById(id);
  if (scenario.targets) setScenarioTargets(scenario.targets);
  if (scenario.id !== 'custom') {
    resetScenarioFields();
    const apply = (fields) => { for (const [id, value] of Object.entries(fields)) {
      const el = $(id); if (!el) continue;
      if (typeof value === 'boolean') el.checked = value; else el.value = String(value);
    }};
    apply(scenario.fields);
  }
  return null;
}
applyScenario('anon');
const snapshot = () => Object.fromEntries(Object.entries(fields).map(([key, value]) => [key,
  typeof value === 'object' && 'checked' in value ? value.checked : String(value.value)]));
const afterAnon = snapshot();
applyScenario('youtube');
const afterYoutube = snapshot();
process.stdout.write(JSON.stringify({afterAnon, afterYoutube, targets: globalThis.__targets}));
""", self.harness())
        after_anon, after_youtube = json.loads(out)['afterAnon'], json.loads(out)['afterYoutube']
        self.assertEqual(after_anon['judge-url'], 'http://azenv.net/')
        self.assertTrue(after_anon['dnsbl-enabled'])
        self.assertTrue(after_anon['strict-clean'])
        self.assertEqual(after_anon['min_anonymity'], 'elite')
        # the YouTube scenario must not inherit any of them
        self.assertEqual(after_youtube['judge-url'], '')
        self.assertFalse(after_youtube['dnsbl-enabled'])
        self.assertFalse(after_youtube['strict-clean'])
        self.assertEqual(after_youtube['min_anonymity'], 'any')
        self.assertEqual(after_youtube['watch'], '0')

    def test_switching_scenarios_replaces_the_services(self):
        out = run(self.source(), """
const applied = [];
function setScenarioTargets(list) { applied.push(list.map(item => item.name)); }
function resetScenarioFields() {}
const youtube = scenarioById('youtube');
setScenarioTargets(youtube.targets);
const anon = scenarioById('anon');
setScenarioTargets(anon.targets);
process.stdout.write(JSON.stringify(applied));
""", self.harness())
        applied = json.loads(out)
        self.assertEqual(applied[0], ['YouTube web'])
        self.assertEqual(applied[1], ['Anonymity check'])
        self.assertNotIn('YouTube', ' '.join(applied[1]))

    def test_no_scenario_name_promises_calls_video_or_the_whole_service(self):
        out = run(self.source(), """
process.stdout.write(JSON.stringify(SCENARIOS.map(s => ({id: s.id, name: s.name, note: s.note,
  targets: (s.targets || []).map(t => ({name: t.name, url: t.url}))}))));
""", self.harness())
        scenarios = json.loads(out)
        names = ' '.join(item['name']['en'] + ' ' + item['name']['ru'] for item in scenarios).lower()
        names += ' ' + ' '.join(target['name'] for item in scenarios for target in item['targets']).lower()
        for promise in ('4k', '1080p', 'звонк', 'call', 'whole service', 'весь сервис', 'все звонки'):
            self.assertNotIn(promise, names, f'a scenario name promises {promise}')
        for item in scenarios:
            for target in item['targets']:
                self.assertIn(target['name'].lower(), ('telegram web', 'youtube web', 'anonymity check', 'website response'))
            # Each scenario states in words what it does not measure.
            self.assertTrue(item['note']['en'] and item['note']['ru'])


class LiveFeedClientTests(unittest.TestCase):
    """Defect 25 on the page: events in, rows out; no regex over the log."""

    def source(self):
        return ws.js_slice('const LIVE_FEED_LIMIT = 40;', 'function snapshotView(report)')

    def harness(self):
        return PRELUDE + """
const container = {innerHTML: '', children: []};
globalThis.__lookup = id => (id === 'live-ticker-list' ? container : null);
globalThis.__events = [];
globalThis.__api = async () => ({events: globalThis.__events, cursor: 'job:abc:3'});
"""

    def test_one_row_per_event_with_its_code(self):
        out = run(self.source(), """
globalThis.__events = [
  {stream: 'job:abc', seq: 1, type: 'item.observation', item_id: 'http://11.0.0.1:8080', code: 'OK',
   data: {proxy: 'http://11.0.0.1:8080', latency_ms: 120, reliability: 1}},
  {stream: 'job:abc', seq: 2, type: 'item.observation', item_id: 'http://11.0.0.2:8080', code: 'UNREACHABLE',
   data: {proxy: 'http://11.0.0.2:8080', latency_ms: null, error: 'UNREACHABLE'}}
];
await pollEvents();
renderLiveFeedItems();
process.stdout.write(JSON.stringify({items: liveFeed.items.length, html: container.innerHTML, cursor: liveFeed.cursor}));
""", self.harness())
        answer = json.loads(out)
        self.assertEqual(answer['items'], 2)
        self.assertIn('http://11.0.0.1:8080', answer['html'])
        self.assertIn('data-code="UNREACHABLE"', answer['html'])
        self.assertIn('measured', answer['html'])
        self.assertEqual(answer['cursor'], 'job:abc:3')

    def test_the_page_no_longer_scans_the_log_for_ok_or_pass(self):
        text = ws.APP_JS.read_text(encoding='utf-8')
        self.assertNotIn(r'/\b(OK|PASS|SUCCESS|FAIL|ERR|ERROR)\b/i', text)
        self.assertIn('/api/events', text)

    def test_an_unknown_code_is_shown_as_the_code_itself(self):
        out = run(self.source(), """
process.stdout.write(JSON.stringify({known: codeLabel('OK'), unknown: codeLabel('E_SOMETHING_NEW')}));
""", self.harness())
        answer = json.loads(out)
        self.assertEqual(answer['known'], 'measured')
        self.assertEqual(answer['unknown'], 'E_SOMETHING_NEW')


class ResultListClientTests(unittest.TestCase):
    """F19 on the page: scopes, compact columns, selection that cannot move."""

    def source(self):
        return ws.js_slice('const COLUMN_LABELS = {', 'function setupResultList()')

    def harness(self):
        return PRELUDE + """
const cells = ['check','num','proxy','score','latency','jitter','speed','success','uptime','cleanliness','anonymity','country','provider','age','actions']
  .map(name => { const cell = {name, hidden: false};
    cell.classList = {toggle(name, force) { cell.hidden = Boolean(force); }};
    return cell; });
const menu = {innerHTML: '', querySelectorAll() { return []; }, classList: {toggle() {}}};
document.querySelectorAll = selector => {
  const match = /data-col="col-([a-z]+)"/.exec(selector);
  return match ? cells.filter(cell => cell.name === match[1]) : [];
};
globalThis.__lookup = id => ({
  'columns-menu': menu,
  'columns-toggle': {classList: {toggle() {}}},
  'result-sort': {value: 'quality'}, 'result-min': {value: '0.5'}, 'result-anon': {value: 'any'},
  'result-protocol': {value: 'all'}, 'result-max-latency': {value: 0}, 'result-country': {value: 'NL'},
  'result-hosting': {value: ''}, 'result-search': {value: ''}, 'result-rows': {querySelectorAll() { return []; }}
}[id] || null);
globalThis.__localStorage = {};
const localStorage = {getItem: k => globalThis.__localStorage[k] || null,
  setItem: (k, v) => { globalThis.__localStorage[k] = v; }};
function loadResults() { globalThis.__loads = (globalThis.__loads || 0) + 1; }
function updateSelectionUI() {}
"""

    def test_the_default_column_set_is_compact(self):
        out = run(self.source(), """
resultState.columns = null;
const compact = compactColumns(state.all_columns);
process.stdout.write(JSON.stringify({compact, hidden: cells.filter(c => c.hidden).map(c => c.name)}));
""", self.harness())
        answer = json.loads(out)
        self.assertNotIn('jitter', answer['compact'])
        self.assertNotIn('speed', answer['compact'])
        self.assertNotIn('anonymity', answer['compact'])
        for name in ('proxy', 'latency', 'score', 'country'):
            self.assertIn(name, answer['compact'])

    def test_applying_columns_hides_the_cells_it_does_not_contain(self):
        out = run(self.source(), """
applyColumns(['check','num','proxy','latency','actions']);
const hidden = cells.filter(c => c.hidden).map(c => c.name);
process.stdout.write(JSON.stringify({hidden, stored: resultState.columns}));
""", self.harness())
        answer = json.loads(out)
        self.assertNotIn('latency', answer['hidden'])
        for name in ('jitter', 'speed', 'provider', 'age', 'uptime', 'cleanliness', 'anonymity'):
            self.assertIn(name, answer['hidden'])
        self.assertIn('latency', answer['stored'])

    def test_a_scope_change_clears_the_selection_instead_of_moving_it(self):
        out = run(self.source(), """
selectedProxies.add('http://11.0.0.1:8080');
resultState.selectionScope = 'scope-a';
syncSelectionScope('scope-a');
const kept = selectedProxies.size;
syncSelectionScope('scope-b');
process.stdout.write(JSON.stringify({kept, after: selectedProxies.size, toasts: globalThis.__toasts.length,
  selectionScope: resultState.selectionScope}));
""", self.harness())
        answer = json.loads(out)
        self.assertEqual(answer['kept'], 1)
        self.assertEqual(answer['after'], 0, 'the selection silently moved to another scope')
        self.assertGreaterEqual(answer['toasts'], 1)

    def test_the_bulk_action_carries_the_scope_digest(self):
        out = run(self.source(), """
resultState.digest = 'digest-1';
resultState.scope = 'all_matching';
selectedProxies.add('http://11.0.0.1:8080');
globalThis.__api = async (path, body) => { globalThis.__body = body; return {count: body.query ? 1 : 0, text: ''}; };
const baseLookup = globalThis.__lookup;
globalThis.__lookup = id => (id === 'bulk-tag-input' ? {value: 'fast'} : baseLookup(id));
globalThis.navigator = {clipboard: {writeText: async () => {}}};
await bulkAction('tag');
process.stdout.write(JSON.stringify(globalThis.__body));
""", self.harness())
        body = json.loads(out)
        self.assertEqual(body['scope'], 'all_matching')
        self.assertEqual(body['scope_digest'], 'digest-1')
        self.assertEqual(body['tag'], 'fast')
        self.assertIn('query', body)
        self.assertNotIn('proxies', body, 'an all-matching action must not ship the rows')


if __name__ == '__main__':
    unittest.main()

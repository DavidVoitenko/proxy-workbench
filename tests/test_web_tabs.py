"""Navigation smoke for every shipped page and its first data load."""

import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import web_support as ws


class TabNavigationTests(unittest.TestCase):
    def test_each_nav_opens_its_page_and_starts_its_data_load(self):
        html = ws.INDEX_HTML.read_text(encoding='utf-8')
        navs = re.findall(r'<button\b[^>]*\bdata-tab="([^"]+)"', html)
        pages = re.findall(r'<section\b[^>]*\bclass="page(?: [^"]*)?"[^>]*\bid="page-([^"]+)"', html)
        expected = ['scan', 'results', 'gateway', 'mobile', 'sources', 'pools', 'keys', 'help']
        self.assertEqual(navs, expected)
        self.assertEqual(pages, expected)

        source = ws.js_slice('function showTab(name)', '// Final URLs only')
        script = r"""
let currentTab = 'scan';
const t = key => key;
const calls = [];
const pageNames = """ + json.dumps(expected) + r""";
const mkClassList = () => {
  const names = new Set();
  return {toggle(name, value) {if (value) names.add(name); else names.delete(name);},
    add(name) {names.add(name);}, remove(name) {names.delete(name);}, contains(name) {return names.has(name);}};
};
const pages = pageNames.map(name => ({id: 'page-' + name, classList: mkClassList(), offsetWidth: 1}));
const navs = pageNames.map(name => ({dataset: {tab: name}, classList: mkClassList()}));
const label = {textContent: ''};
const document = {
  querySelectorAll(selector) {
    if (selector === '.page') return pages;
    if (selector === '.nav' || selector === '[data-tab]') return navs;
    if (selector === '[data-go]') return [];
    return [];
  },
  getElementById(id) {return id === 'page-label' ? label : pages.find(page => page.id === id);}
};
const $ = id => document.getElementById(id);
const window = {location: {hash: ''}, scrollTo() {}};
const history = {replaceState(_state, _title, hash) {window.location.hash = hash;}};
function loadResults() {calls.push('results');}
function reloadCatalog() {calls.push('catalog');}
function loadPools() {calls.push('pools');}
function loadSchedules() {calls.push('schedules');}
function loadJobs() {calls.push('jobs');}
function loadGatewayOptions() {calls.push('gatewayOptions');}
function loadKeys() {calls.push('keys');}
function loadDiagnostics() {calls.push('diagnostics');}
function loadDesktop() {calls.push('desktop');}
""" + source + r"""
const results = [];
for (const nav of navs) {
  calls.length = 0;
  nav.onclick();
  results.push({tab: nav.dataset.tab, active: pages.filter(page => page.classList.contains('active')).map(page => page.id),
    label: label.textContent, hash: window.location.hash, loaders: [...calls]});
}
process.stdout.write(JSON.stringify(results));
"""
        results = json.loads(ws.node_ok(script))
        loaders = {'scan': [], 'results': ['results'], 'gateway': [], 'mobile': [],
                   'sources': ['catalog'],
                   'pools': ['pools', 'schedules', 'jobs', 'gatewayOptions'],
                   'keys': ['keys'], 'help': ['diagnostics', 'desktop']}
        for row in results:
            name = row['tab']
            self.assertEqual(row['active'], ['page-' + name])
            self.assertEqual(row['label'], 'nav.' + name)
            self.assertEqual(row['hash'], '#' + name)
            self.assertEqual(row['loaders'], loaders[name])


if __name__ == '__main__':
    unittest.main()

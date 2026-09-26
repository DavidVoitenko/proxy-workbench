"""Performance surface: conditional polling, gzip and static revalidation.

The interface polls ``/api/state`` twice a second; an idle bench must answer
with an empty 304 so the page has nothing to repaint and the server ships no
body.  Static assets revalidate the same way on every navigation, and large
compressible answers ride gzip instead of raw bytes.
"""
import gzip
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import web_support as ws


class PerfSurface(unittest.TestCase):
    def setUp(self):
        self.home = ws.build_data([ws.measurement('http://10.0.0.10:8080', latency=120.0)])
        self.fixture = ws.ServerFixture(self.home)
        self.client = self.fixture.client

    def tearDown(self):
        self.fixture.close()


class ConditionalStateTests(PerfSurface):
    def state(self, **headers):
        return self.client.get('/api/state', headers=headers or None)

    def test_unchanged_state_answers_304_with_an_empty_body(self):
        first = self.state()
        self.assertEqual(first.status_code, 200)
        etag = first.headers['ETag'].strip('"')
        self.assertTrue(etag)
        # The poll manages revalidation itself, so the answer stays no-store
        # exactly like the other API routes.
        self.assertEqual(first.headers['Cache-Control'], 'no-store')
        again = self.state(**{'If-None-Match': f'"{etag}"'})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b'')
        self.assertEqual(again.headers['ETag'].strip('"'), etag)
        self.assertEqual(again.headers['Cache-Control'], 'no-store')

    def test_a_changed_log_breaks_the_etag(self):
        first = self.state()
        etag = first.headers['ETag'].strip('"')
        log = self.fixture.app.data / 'gui-run.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as handle:
            handle.write('perf: one more line the tail must carry\n')
        second = self.state(**{'If-None-Match': f'"{etag}"'})
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(second.headers['ETag'].strip('"'), etag)

    def test_weak_and_listed_validators_match(self):
        etag = self.state().headers['ETag']
        for variant in ('W/' + etag, f'{etag}, "{etag}"'):
            self.assertEqual(self.state(**{'If-None-Match': variant}).status_code, 304)


class StaticRevalidationTests(PerfSurface):
    def test_scripts_and_styles_revalidate_to_304(self):
        for route in ('/app.js', '/style.css', '/i18n/de.js'):
            first = self.client.get(route)
            self.assertEqual(first.status_code, 200, route)
            self.assertEqual(first.headers['Cache-Control'], 'no-cache', route)
            again = self.client.get(route, headers={'If-None-Match': first.headers['ETag']})
            self.assertEqual(again.status_code, 304, route)
            self.assertEqual(again.content, b'')

    def test_the_served_page_and_scripts_carry_no_placeholders(self):
        page = self.client.get('/')
        self.assertNotIn(b'__TOKEN__', page.content)
        self.assertNotIn(b'__PRODUCT_VERSION__', page.content)
        script = self.client.get('/app.js')
        self.assertNotIn(b'__PRODUCT_VERSION__', script.content)
        self.assertIn('no-store', page.headers['Cache-Control'])


class CompressionTests(PerfSurface):
    def test_state_ships_gzipped_when_the_browser_asks(self):
        plain = self.client.get('/api/state', headers={'Accept-Encoding': 'identity'})
        self.assertNotIn('Content-Encoding', plain.headers)
        squeezed = self.client.get('/api/state', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(squeezed.headers['Content-Encoding'], 'gzip')
        self.assertEqual(json.loads(squeezed.content), json.loads(plain.content))

    def test_statics_ship_gzipped_when_the_browser_asks(self):
        plain = self.client.get('/style.css', headers={'Accept-Encoding': 'identity'})
        self.assertNotIn('Content-Encoding', plain.headers)
        squeezed = self.client.get('/style.css', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(squeezed.headers['Content-Encoding'], 'gzip')
        self.assertEqual(squeezed.text, plain.text)
        self.assertLess(int(squeezed.headers['Content-Length']),
                        int(plain.headers['Content-Length']))


class PollSkipTests(unittest.TestCase):
    """The JS poll: an unchanged state must not repaint anything."""

    def setUp(self):
        self.source = ws.js_slice('let stateTag = null;', 'const selectedProxies')

    def run_poll(self, script):
        return ws.run_node(
            'const assert = require("assert");\n'
            'let polling = false;\n'
            'const UNCHANGED = Symbol("unchanged");\n'
            'const stateTagHolder = {tag: null};\n'
            'const document = {hidden: false, querySelector: () => null};\n'
            'const t = (key) => key;\n'
            'const $ = () => null;\n'
            'let rendered = [];\n'
            'let renderState = (value) => { rendered.push(value); };\n'
            'let apiImpl = null;\n'
            'const api = (...args) => apiImpl(...args);\n'
            + script +
            '\n;(async () => {\n' +
            self.scenario +
            '\n})().then(() => console.log("OK")).catch((error) => { console.error(error); process.exit(1); });\n'
        )

    def test_unchanged_state_skips_the_render(self):
        # One process, two polls: the first answer carries an ETag, the second
        # is the 304 sentinel, so nothing is repainted the second time.
        self.scenario = (
            'let calls = 0;'
            'let painted = 0;'
            'renderState = (value) => { painted += 1; };'
            'apiImpl = async (path, body, opts) => {'
            '  calls += 1;'
            '  if (calls === 1) return {value: {gateway: null}, etag: \'"aaa"\'};'
            '  assert.strictEqual(opts.etag, \'"aaa"\');'
            '  return UNCHANGED;'
            '};'
            'await poll();'
            'assert.strictEqual(painted, 1);'
            'await poll();'
            'assert.strictEqual(painted, 1);'
        )
        done = self.run_poll(self.source)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_changed_state_renders_once_and_remembers_the_etag(self):
        self.scenario = (
            'let calls = 0;'
            'apiImpl = async (path, body, opts) => {'
            '  calls += 1;'
            '  assert.strictEqual(path, "/api/state");'
            '  assert.strictEqual(opts.etag, calls === 1 ? null : \'"aaa"\');'
            '  return {value: {gateway: null}, etag: \'"aaa"\'};'
            '};'
            'await poll();'
            'assert.strictEqual(rendered.length, 1);'
            'await poll();'
            'assert.strictEqual(rendered.length, 2);'
        )
        done = self.run_poll(self.source)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == '__main__':
    unittest.main()

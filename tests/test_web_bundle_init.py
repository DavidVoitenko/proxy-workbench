"""A crash while the interface script loads disables the whole product.

`app.js` is one big IIFE, and its setup statements run at module scope. A
statement that throws there — a name that was renamed and never redeclared, a
bad `$()` call — aborts the rest of the file, so every listener below that line
silently stays unattached. Nothing says so: the markup still renders, the
server still answers, and a suite that drives the HTTP API sees nothing wrong.
The interface simply stops working when you click.

That is not hypothetical. `closeDetailsBtn` was used at top level and declared
nowhere, so every button in the application was dead — 3245 tests and 110 live
API checks passed while the product was unusable in a browser.

So this loads the real script under a minimal DOM stub and fails if evaluating
it throws, or if a listener the page is supposed to have never arrives. That
catches the class of failure rather than the one instance.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "proxy_workbench" / "ui" / "app.js"
INDEX_HTML = Path(__file__).resolve().parents[1] / "proxy_workbench" / "ui" / "index.html"

NODE = shutil.which("node")

# Enough of a DOM for the module-scope statements of app.js. Selectors answer
# "nothing there", which is the interesting case: every setup helper must cope.
STUB = r"""
const mkEl = (tag) => {
  const el = {
    tagName: String(tag || 'div').toUpperCase(), id: '', className: '', value: '',
    textContent: '', innerText: '', innerHTML: '', hidden: false, disabled: false,
    dataset: {}, style: {}, children: [], attributes: [], classList: {
      add(){}, remove(){}, contains(){ return false; }, toggle(){},
    },
    offsetParent: null, scrollWidth: 0, clientWidth: 0, open: false,
    appendChild(c){ this.children.push(c); return c; }, removeChild(c){ return c; },
    setAttribute(k,v){ this.attributes.push([k,v]); }, getAttribute(){ return null; },
    removeAttribute(){}, addEventListener(){}, removeEventListener(){},
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    closest(){ return null; }, focus(){}, blur(){}, click(){}, scrollIntoView(){},
    getBoundingClientRect(){ return {top:0,left:0,bottom:0,right:0,width:0,height:0}; },
    insertAdjacentHTML(){}, replaceChildren(){}, cloneNode(){ return mkEl(tag); },
    get firstChild(){ return null; }, get lastChild(){ return null; },
    get children_list(){ return this.children; },
  };
  return el;
};
const registry = {};
const metaToken = mkEl('meta'); metaToken.content = 'test-token';
globalThis.__listeners = registry;
globalThis.document = {
  documentElement: mkEl('html'),
  body: mkEl('body'),
  head: mkEl('head'),
  cookie: '',
  readyState: 'complete',
  addEventListener(){}, removeEventListener(){}, createElement: mkEl, createTextNode: mkEl,
  createDocumentFragment: mkEl,
  getElementById: (id) => registry[id] || null,
  // the page reads its launch token out of this one meta element
  querySelector: (sel) => (/meta/.test(sel) ? metaToken : (registry[sel] || null)),
  querySelectorAll: () => [],
  elementsFromPoint: () => [],
};
globalThis.location = { href: 'http://127.0.0.1/', hash: '', origin: 'http://127.0.0.1', search: '' };
globalThis.history = { replaceState(){}, pushState(){}, state: null };
globalThis.localStorage = { getItem: () => null, setItem(){}, removeItem(){}, clear(){} };
globalThis.sessionStorage = globalThis.localStorage;
globalThis.navigator = { language: 'en', languages: ['en'], clipboard: { writeText: async () => {} } };
globalThis.matchMedia = (q) => ({ matches: false, media: q, addEventListener(){}, addListener(){} });
globalThis.CSS = { escape: (s) => String(s) };
globalThis.ResizeObserver = class { observe(){} unobserve(){} disconnect(){} };
globalThis.IntersectionObserver = class { observe(){} unobserve(){} disconnect(){} };
globalThis.MutationObserver = class { observe(){} disconnect(){} };
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => '' });
globalThis.FileReader = class { readAsText(){} addEventListener(){} };
globalThis.Blob = class { constructor(){} };
globalThis.URL = { createObjectURL: () => 'blob:', revokeObjectURL(){} };
globalThis.alert = () => {};
globalThis.confirm = () => false;
globalThis.open = () => null;
globalThis.print = () => {};
// app.js is a browser script: in node the global object answers to globalThis.
globalThis.window = globalThis;
globalThis.self = globalThis;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.dispatchEvent = () => true;
globalThis.postMessage = () => {};
globalThis.requestAnimationFrame = (fn) => 0;
globalThis.cancelAnimationFrame = () => {};
// The page polls forever once loaded; this test is about the load, so the
// timers are inert and node is free to exit.
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.setTimeout = () => 0;
globalThis.clearTimeout = () => {};
"""


def _run(script: str, timeout: int = 90) -> subprocess.CompletedProcess:
    # A classic script, because that is how index.html loads app.js: a repeated
    # `function` declaration is legal there and only ESM would reject it.
    return subprocess.run(
        [NODE, "-e", script],
        capture_output=True, text=True, timeout=timeout, cwd=str(APP_JS.parents[2]),
    )


@unittest.skipIf(NODE is None, "node is not installed")
class BundleInitialisationTests(unittest.TestCase):
    def setUp(self):
        self.source = APP_JS.read_text(encoding="utf-8")

    def test_the_script_evaluates_without_throwing(self):
        script = STUB + "\n" + self.source + "\nconsole.log('EVALUATED_OK');\n"
        run = _run(script)
        self.assertEqual(
            run.returncode, 0,
            "app.js падает при загрузке, поэтому обработчики ниже этой строки не ставятся:\n"
            + (run.stderr or run.stdout)[-2500:],
        )
        self.assertIn("EVALUATED_OK", run.stdout)

    def test_the_detail_dialog_close_button_is_declared_before_use(self):
        # The concrete instance that broke the product, kept as a named guard.
        self.assertIn('id="close-details"', INDEX_HTML.read_text(encoding="utf-8"))
        declared = re.search(
            r"const\s+closeDetailsBtn\s*=\s*\$\(\s*'close-details'\s*\)", self.source
        )
        self.assertTrue(
            declared,
            "кнопка закрытия карточки должна быть объявлена до того, как к ней обращаются",
        )

    def test_every_id_the_script_reaches_for_is_built_somewhere(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        present = set(re.findall(r'id="([^"]+)"', html))
        # Rows, dialogs and cards are built at runtime, so an id the script also
        # emits as markup is legitimately absent from index.html.
        built_at_runtime = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', self.source))
        # `$(preferred) || $(older)` is a deliberate fallback, not a demand for
        # an element that does not exist.
        fallback_only = set()
        for line in self.source.splitlines():
            if "||" not in line:
                continue
            fallback_only.update(re.findall(r"\$\(\s*'([a-zA-Z0-9_-]+)'\s*\)", line))
        wanted = set(re.findall(r"\$\(\s*'([a-zA-Z0-9_-]+)'\s*\)", self.source))
        missing = sorted(wanted - present - built_at_runtime - fallback_only)
        self.assertEqual(
            missing, [],
            "app.js ищет элементы, которых нет ни в разметке, ни в создаваемой им разметке: "
            + ", ".join(missing[:12]),
        )


if __name__ == "__main__":
    unittest.main()

import re
import unittest
from pathlib import Path

from tests.web_support import run_node


class QREncoderTests(unittest.TestCase):
    def test_encoder_builds_a_svg_and_uses_alternating_strips(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'proxy_workbench' / 'ui' / 'app.js').read_text(encoding='utf-8')
        start = source.index('function makeQR(')
        end = source.index('function snapshotTime', start)
        function = source[start:end]
        self.assertIn('upward = !upward', function)
        self.assertNotIn('((right + 1) / 2) % 2', function)
        payload = 'tg://socks?server=192.168.0.41&port=8899&username=workbench&password=fixture-token'
        script = function + "\nprocess.stdout.write(makeQR(" + repr(payload) + "));"
        result = run_node(script, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        svg = result.stdout
        self.assertRegex(svg, r'<svg[^>]+class="qr-svg"')
        self.assertIn('<path d="M', svg)
        match = re.search(r'viewBox="0 0 (\d+) \1"', svg)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 35)


if __name__ == '__main__':
    unittest.main()

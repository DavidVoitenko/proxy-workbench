"""Public-list address encodings are data; arbitrary JavaScript never runs."""
import unittest

from proxy_workbench.source_literals import decode_address_expression, decode_html_literals
from proxy_workbench import source_adapters


class AddressLiteralTests(unittest.TestCase):
    def test_table_filters_are_not_reported_as_proxy_addresses(self):
        body = (b'<table><tr><th>IP Address</th><th>Port</th></tr>'
                b'<tr><td>--All--</td><td>--All--</td></tr>'
                b'<tr><td>198.51.100.1</td><td>8080</td></tr></table>')
        result = source_adapters.parse_page(body, {'kind': 'html-table', 'config': {
            'columns': {'ip': 'IP Address', 'port': 'Port'}}})
        self.assertEqual([row['value'] for row in result['records']], ['198.51.100.1:8080'])
        self.assertEqual(result['rejects'].get('invalid_address'), 1)

    def test_provider_string_operations_are_decoded(self):
        examples = {
            '"8.141.022.8".split("").reverse().join("")': '8.220.141.8',
            '".6437.28.220.136.174.6437.20.".substring(9-2, 35-15)': '8.220.136.174',
            'atob("NDcuOTIuMTUyLg==").concat("34".split("").reverse().join(""))': '47.92.152.43',
            '"103.247.1".repeat(3).substring(18).concat("4.".repeat(1).substring(0))'
            '.concat("222222".substring(1+1, 7-2))': '103.247.14.222',
            '"12.34.56.78".substring(0, 8+3)': '12.34.56.78',
            '"12.34.56.78".substr(0, 1+10)': '12.34.56.78',
            '[53,51,57,51,54,58,61,51,54,62,64].map((code) => String.fromCharCode(code-5))'
            '.join("")': '0.4.159.19;',
        }
        for expression, expected in examples.items():
            with self.subTest(expression=expression):
                # A non-address decoded value must still be rejected.
                answer = decode_address_expression(expression)
                self.assertEqual(answer, None if ';' in expected else expected)

    def test_ascii_array_encoding_is_supported(self):
        address = '198.51.100.12'
        numbers = ','.join(str(ord(char) + 7) for char in address)
        value = '[' + numbers + '].map((code) => String.fromCharCode(code-7)).join("")'
        self.assertEqual(decode_address_expression(value), address)

    def test_large_or_executable_expressions_are_rejected(self):
        for expression in (
            'process.exit()', 'fetch("https://untrusted.invalid")',
            '"1.2.3.4"; doAnything()', 'atob("aHR0cDovL2V2aWw=")',
            '"1.2.3.4".constructor("return process")()',
            '"1".repeat(4097)', '"x"' * 2000,
            'atob("not-valid-base64!!!")',
            'atob(' * 30 + '"MS4yLjMuNA=="' + ')' * 30,
        ):
            with self.subTest(expression=expression[:60]):
                self.assertIsNone(decode_address_expression(expression))

    def test_only_known_address_prints_change_in_html(self):
        body = (b'<li class="proxy"><script>Proxy("MTk4LjUxLjEwMC4xOjgwODA=")</script></li>'
                b'<script>document.write("2.001.15.891".split("").reverse().join(""))</script>'
                b'<script>document.write(fetch("/secret"))</script>'
                b'<script>unrelated()</script>')
        decoded = decode_html_literals(body)
        self.assertIn(b'<li class="proxy">198.51.100.1:8080</li>', decoded)
        self.assertIn(b'198.51.100.2', decoded)
        self.assertIn(b'<script>document.write(fetch("/secret"))</script>', decoded)
        self.assertIn(b'<script>unrelated()</script>', decoded)

"""Decode the bounded string-literal formats used by public proxy lists.

This is a parser for a small data grammar, never a JavaScript evaluator. Names,
property access and calls outside this grammar are rejected before execution.
"""
from __future__ import annotations

import base64
import binascii
import json
import re

MAX_EXPRESSION = 4096
MAX_VALUE = 4096
_STRING = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', re.X)
_MAP = re.compile(r'\.map\(\(code\)\s*=>\s*String\.fromCharCode\(code([+-])([0-9]{1,3})\)\)')


class _LiteralParser:
    def __init__(self, text):
        if len(text) > MAX_EXPRESSION:
            raise ValueError('expression limit')
        self.text, self.pos, self.steps = text, 0, 0

    def token(self, value):
        self.space()
        if not self.text.startswith(value, self.pos):
            raise ValueError('unsupported expression')
        self.pos += len(value)

    def space(self):
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def integer(self):
        self.space()
        match = re.match(r'[0-9]{1,6}(?:\s*[+-]\s*[0-9]{1,6})*', self.text[self.pos:])
        if match is None:
            raise ValueError('expected integer')
        self.pos += len(match[0])
        pieces = re.findall(r'[+-]?[0-9]+', re.sub(r'\s+', '', match[0]))
        value = sum(map(int, pieces))
        if not -MAX_VALUE <= value <= MAX_VALUE:
            raise ValueError('integer limit')
        return value

    def string(self):
        self.space()
        match = _STRING.match(self.text, self.pos)
        if match is None:
            raise ValueError('expected string')
        self.pos = match.end()
        literal = match[0]
        if literal.startswith('"'):
            return json.loads(literal)
        # The providers use single quotes for Base64 only; avoid treating
        # JavaScript escape sequences as arbitrary Python/string escapes.
        body = literal[1:-1]
        if '\\' in body:
            raise ValueError('unsupported escape')
        return body

    def expression(self, depth=0):
        self.steps += 1
        if depth > 12 or self.steps > 128:
            raise ValueError('expression depth')
        self.space()
        if self.text.startswith('atob(', self.pos):
            self.token('atob(')
            value = base64.b64decode(self.string(), validate=True).decode('ascii')
            self.token(')')
        elif self.text.startswith('[', self.pos):
            self.token('[')
            numbers = []
            while True:
                numbers.append(self.integer())
                if len(numbers) > 256:
                    raise ValueError('array limit')
                self.space()
                if self.text.startswith(']', self.pos):
                    self.pos += 1
                    break
                self.token(',')
            self.space()
            match = _MAP.match(self.text, self.pos)
            if match is None:
                raise ValueError('unsupported array expression')
            self.pos = match.end()
            delta = int(match[2]) * (1 if match[1] == '+' else -1)
            value = ''.join(chr(number + delta) for number in numbers)
            self.token('.join(')
            if self.string() != '':
                raise ValueError('unsupported separator')
            self.token(')')
        else:
            value = self.string()
        while True:
            self.space()
            if self.pos >= len(self.text) or self.text[self.pos] != '.':
                break
            self.pos += 1
            name = re.match(r'[a-z]+', self.text[self.pos:])
            if name is None:
                raise ValueError('unsupported method')
            self.pos += len(name[0])
            self.token('(')
            if name[0] == 'concat':
                value += self.expression(depth + 1)
                self.token(')')
            elif name[0] == 'repeat':
                count = self.integer()
                self.token(')')
                if count < 0 or len(value) * count > MAX_VALUE:
                    raise ValueError('repeat limit')
                value *= count
            elif name[0] in ('substring', 'substr'):
                start = self.integer()
                self.space()
                end = None
                if self.text.startswith(',', self.pos):
                    self.pos += 1
                    end = self.integer()
                self.token(')')
                if name[0] == 'substring':
                    start = min(len(value), max(0, start))
                    end = len(value) if end is None else min(len(value), max(0, end))
                    value = value[min(start, end):max(start, end)]
                else:
                    start = max(0, len(value) + start) if start < 0 else start
                    value = value[start:] if end is None else value[start:start + max(0, end)]
            elif name[0] == 'split':
                if self.string() != '':
                    raise ValueError('unsupported split')
                self.token(').reverse().join(')
                if self.string() != '':
                    raise ValueError('unsupported join')
                self.token(')')
                value = value[::-1]
            else:
                raise ValueError('unsupported method')
            if len(value) > MAX_VALUE:
                raise ValueError('value limit')
        if len(value) > MAX_VALUE:
            raise ValueError('value limit')
        return value


def decode_address_expression(value):
    """Return a numeric address/host:port literal, or None for any other code."""
    if not isinstance(value, str):
        return None
    if re.fullmatch(r'[0-9A-Fa-f:.\[\]]{3,64}', value):
        return value
    try:
        parser = _LiteralParser(value)
        decoded = parser.expression()
        parser.space()
        if parser.pos != len(value) or not re.fullmatch(r'[0-9A-Fa-f:.\[\]]{3,64}', decoded):
            return None
        return decoded
    except (ValueError, TypeError, UnicodeError, binascii.Error, OverflowError, RecursionError):
        return None


_SCRIPT = re.compile(r'<script\b[^>]*>(.*?)</script\s*>', re.I | re.S)
_PRINT = re.compile(r'^\s*(document\.write|Proxy)\((.*)\)\s*;?\s*$', re.S)


def decode_html_literals(body):
    """Replace only recognized address-printing script elements with plain data."""
    text = body.decode('utf-8', 'replace') if isinstance(body, bytes) else str(body)

    def replace(match):
        printed = _PRINT.fullmatch(match[1])
        if printed is None or len(printed[2]) > MAX_EXPRESSION:
            return match[0]
        expression = 'atob(' + printed[2] + ')' if printed[1] == 'Proxy' else printed[2]
        return decode_address_expression(expression) or match[0]

    return _SCRIPT.sub(replace, text).encode('utf-8')

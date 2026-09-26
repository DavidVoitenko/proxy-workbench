"""Shared fixtures for the web-surface tests (gui.py, ui/app.js, i18n).

The database built here follows the schema the current worker creates:
``results(profile, proxy, payload)`` with the measurement fields inside the
JSON payload.  No test touches the network, and no canary secret is written
into a file, a log or a JSON document.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

import httpx

from proxy_workbench import gui
from proxy_workbench import proxytool as core
from tests import workbench_support as support

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / 'proxy_workbench' / 'ui' / 'app.js'
INDEX_HTML = ROOT / 'proxy_workbench' / 'ui' / 'index.html'

PROFILE_CONFIG = {
    'targets': [
        {'name': 'Example service', 'url': 'https://example.com/', 'method': 'GET',
         'statuses': [200], 'contains': 'Example', 'headers': {}},
        {'name': 'Second service', 'url': 'https://example.org/', 'method': 'GET',
         'statuses': [200], 'contains': None, 'headers': {}},
    ],
    'request_profile': 'workbench',
    'attempts': 3,
    'timeout': 8,
    'min_success': 2 / 3,
    'reputation': {'local_enabled': True, 'dnsbl_enabled': False, 'dnsbl_zones': [], 'strict': False},
    'anonymity': None,
}


def profile_id(config=None):
    payload = json.dumps(config or PROFILE_CONFIG, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]


def measurement(proxy, *, latency=120.0, reliability=1.0, age=60.0, now=None, error=None,
                checked_at=None, valid_for=900.0, samples=None, country=None, history=None):
    now = time.time() if now is None else now
    checked = now - age if checked_at is None else checked_at
    body = {
        'proxy': proxy,
        'latency_ms': latency,
        'jitter_ms': 8.0,
        'score': 70.0,
        'reliability': reliability,
        'min_target_reliability': reliability,
        'successes': 3,
        'requests': 3,
        'checked_at': checked,
        'valid_until': checked + valid_for,
        'history': history or {'checks': 2, 'passes': 2},
        'samples': samples if samples is not None else [
            {'target': 0, 'attempt': 1, 'ok': True, 'status': 200, 'elapsed_ms': latency},
            {'target': 1, 'attempt': 1, 'ok': True, 'status': 200, 'elapsed_ms': latency},
        ],
    }
    # A row the engine could actually have written carries its scope: the
    # collection, the profile revision, the network and the access identity
    # (CONTRACTS §1.2, §2.3).  A fixture without them describes a measurement no
    # reader would admit.
    body['collection_id'] = 'public-base'
    body['profile_id'] = profile_id()
    body['profile_revision'] = 1
    body['network_id'] = 'default'
    body['access_id'] = 'default'
    body['access_revision'] = 1
    if country:
        body['country'] = country
    if error:
        body['error'] = error
        body['min_target_reliability'] = 0.0
        body['reliability'] = 0.0
        body['valid_until'] = checked + valid_for
        body['samples'] = [{'target': 0, 'attempt': 1, 'ok': False, 'error': error}]
    return body


def build_data(rows, *, now=None, config=None, publish=True, profile=None, extra_status=None):
    """A temporary data folder with rows for the active profile."""
    now = time.time() if now is None else now
    home = Path(tempfile.mkdtemp(prefix='webtest-'))
    config = config or PROFILE_CONFIG
    pid = profile or profile_id(config)
    conn = core.open_db(home / 'proxies.sqlite3')
    try:
        with conn:
            conn.execute('INSERT OR IGNORE INTO profiles (id, config) VALUES (?,?)', (pid, json.dumps(config)))
            for body in rows:
                support.store_result(conn, (pid, body['proxy'], json.dumps(body)))
    finally:
        conn.close()
    (home / 'last-profile.txt').write_text(pid, encoding='utf-8')
    if publish:
        publish_generation(home, [row for row in rows if row.get('published', True)], pid, now=now, extra=extra_status)
    return home


def publish_generation(home, rows, pid, *, now=None, name='.generation-webtest1', extra=None, exported=None):
    now = time.time() if now is None else now
    generation = home / 'exports' / 'generations' / name
    generation.mkdir(parents=True, exist_ok=True)
    status = {
        'schema_version': 2, 'profile': pid, 'profile_revision': 1,
        'collection_id': 'public-base', 'network_id': 'default',
        'generation': name, 'state': 'complete',
        'checked': len(rows), 'scope_candidates': max(len(rows), 1), 'exported': len(rows) if exported is None else exported,
        'generated_at': now, 'min_success': 2 / 3, 'sort': 'recommended', 'complete': True,
        'scope': {'protocol': 'all', 'countries': [], 'exclude_hosting': False},
        'max_age_seconds': 900, 'stop_reason': 'complete',
    }
    if extra:
        status.update(extra)
    (generation / 'ranked.json').write_text(json.dumps(rows), encoding='utf-8')
    (generation / 'status.json').write_text(json.dumps(status), encoding='utf-8')
    (generation / 'proxies.txt').write_text('\n'.join(row['proxy'] for row in rows) + ('\n' if rows else ''), encoding='utf-8')
    (generation / 'ranked.csv').write_text('proxy\n' + '\n'.join(row['proxy'] for row in rows) + '\n', encoding='utf-8')
    (generation / 'ranked.json').write_text(json.dumps(rows), encoding='utf-8')
    pointer = home / 'exports' / 'current.json'
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({'generation': name, 'files': ['proxies.txt'], 'state': status['state']}), encoding='utf-8')
    return generation


class ServerFixture:
    """A real loopback server, so the tests exercise the shipped routes."""

    def __init__(self, home):
        self.home = Path(home)
        self.server = gui.make_server(self.home)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.server.server_port}', trust_env=False,
                                   headers={'X-Workbench-Token': self.server.app.token}, timeout=10)

    @property
    def app(self):
        return self.server.app

    def close(self):
        self.client.close()
        self.server.app.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


# ---------------------------------------------------------------------------
# JS helpers: run a slice of app.js under node with a stub environment
# ---------------------------------------------------------------------------

def js_slice(start_marker, end_marker, source=None):
    text = (source or APP_JS.read_text(encoding='utf-8'))
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def run_node(script, *, timeout=30, cwd=None, executable='node'):
    """Run CommonJS over stdin, without OS command-line size or locale limits."""
    return subprocess.run([executable, '--input-type=commonjs', '-'], input=script,
                          capture_output=True, text=True, encoding='utf-8',
                          timeout=timeout, cwd=cwd)


def node_ok(script, *, timeout=30):
    done = run_node(script, timeout=timeout)
    if done.returncode != 0:
        raise AssertionError('node failed: ' + (done.stderr or done.stdout)[-4000:])
    return done.stdout


# ---------------------------------------------------------------------------
# QR: a decoder, so the round trip is really checked
# ---------------------------------------------------------------------------

RS_BLOCKS_M = {
    1: [(1, 26, 16)], 2: [(1, 44, 28)], 3: [(1, 70, 44)], 4: [(2, 50, 32)],
    5: [(2, 67, 43)], 6: [(4, 43, 27)], 7: [(4, 49, 31)],
    8: [(2, 60, 38), (2, 61, 39)], 9: [(3, 58, 36), (2, 59, 37)], 10: [(4, 69, 43), (1, 70, 44)],
}
ALIGN_M = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
           7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}


def _gf_tables():
    exp = [0] * 512
    log = [0] * 256
    value = 1
    for i in range(255):
        exp[i] = value
        exp[i + 255] = value
        log[value] = i
        value = (value << 1) ^ (0x11D if value >= 128 else 0)
    return exp, log


EXP, LOG = _gf_tables()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def _syndromes_zero(block, errors):
    """True when the Reed-Solomon parity of a received block is consistent."""
    for i in range(errors):
        total = 0
        for power in range(len(block)):
            total ^= _gf_mul(block[power], EXP[(i * (len(block) - 1 - power)) % 255])
        if total:
            return False
    return True


def _function_map(size, version):
    """Function patterns in the order the encoder creates them.

    The order matters: an alignment pattern is skipped when its centre is
    already a finder, so building the map in a different order would mark
    real data cells as function and shift every codeword after them.
    """
    mask = [[False] * size for _ in range(size)]

    def mark(r, c):
        if 0 <= r < size and 0 <= c < size:
            mask[r][c] = True

    for base_r, base_c in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                mark(base_r + dr, base_c + dc)
    for row in ALIGN_M[version]:
        for column in ALIGN_M[version]:
            if mask[row][column]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    mark(row + dr, column + dc)
    for i in range(8, size - 8):
        mark(6, i)
        mark(i, 6)
    for i in range(15):
        if i < 6:
            mark(i, 8)
        elif i < 8:
            mark(i + 1, 8)
        else:
            mark(size - 15 + i, 8)
        if i < 8:
            mark(8, size - i - 1)
        elif i < 9:
            mark(8, 15 - i)
        else:
            mark(8, 15 - i - 1)
    mark(size - 8, 8)
    if version >= 7:
        for i in range(18):
            mark(i // 3, i % 3 + size - 11)
            mark(i % 3 + size - 11, i // 3)
    return mask


def qr_grid(svg_text):
    """The module matrix of the SVG the page renders."""
    match = re.search(r'viewBox="0 0 (\d+) \1"', svg_text)
    if not match:
        raise AssertionError('no qr-svg viewBox')
    full = int(match.group(1))
    quiet = 3
    size = full - quiet * 2
    grid = [[False] * size for _ in range(size)]
    for x, y in re.findall(r'M(\d+),(\d+)h1v1h-1z', svg_text):
        column, row = int(x) - quiet, int(y) - quiet
        grid[row][column] = True
    return grid


def decode_qr(svg_text):
    """Decode the QR the page produces back into its text.

    A real decode, not a structural check: format information, mask, the
    zigzag data path, block de-interleaving and the Reed-Solomon parity are all
    exercised, and the payload bytes must come back exactly.
    """
    grid = qr_grid(svg_text)
    size = len(grid)
    version = (size - 17) // 4
    if version < 1 or version > 10 or 17 + 4 * version != size:
        raise AssertionError(f'unexpected qr size {size}')
    function = _function_map(size, version)

    format_bits = 0
    for i in range(15):
        if i < 6:
            bit = grid[i][8]
        elif i < 8:
            bit = grid[i + 1][8]
        else:
            bit = grid[size - 15 + i][8]
        if i < 8:
            other = grid[8][size - i - 1]
        elif i < 9:
            other = grid[8][15 - i]
        else:
            other = grid[8][15 - i - 1]
        if bit != other:
            raise AssertionError('format information is not mirrored')
        format_bits |= int(bit) << i
    # The transmitted format is masked with 0x5412, so the five data bits are
    # only readable after removing that mask.
    data_bits = ((format_bits ^ 0x5412) >> 10) & 0x1F
    level_bits = (data_bits >> 3) & 0b11
    mask = data_bits & 0b111
    if format_bits != _bch_format(data_bits):
        raise AssertionError('format information failed its own BCH check')
    if level_bits != 0b00:
        raise AssertionError(f'expected error-correction level M, got bits {level_bits:02b}')

    modules = [[grid[r][c] for c in range(size)] for r in range(size)]
    if mask == 0:
        for r in range(size):
            for c in range(size):
                if not function[r][c] and (r + c) % 2 == 0:
                    modules[r][c] = not modules[r][c]

    bits = []
    upward = True
    right = size - 1
    while right > 0:
        if right == 6:
            right -= 1
        for vertical in range(size):
            row = size - 1 - vertical if upward else vertical
            for column in range(right, right - 2, -1):
                if function[row][column]:
                    continue
                bits.append(1 if modules[row][column] else 0)
        upward = not upward
        right -= 2

    codewords = []
    for index in range(0, len(bits) - 7, 8):
        value = 0
        for offset in range(8):
            value = (value << 1) | bits[index + offset]
        codewords.append(value)

    # De-interleave: the stream holds all data codewords of all blocks first,
    # then all error codewords, round by round.
    data_sizes = []
    error_sizes = []
    for count, total, data_count in RS_BLOCKS_M[version]:
        data_sizes.extend([data_count] * count)
        error_sizes.extend([total - data_count] * count)
    data_parts = [[] for _ in data_sizes]
    error_parts = [[] for _ in data_sizes]
    cursor = 0
    for index in range(max(data_sizes)):
        for block, size in enumerate(data_sizes):
            if index < size:
                data_parts[block].append(codewords[cursor])
                cursor += 1
    for index in range(max(error_sizes)):
        for block, size in enumerate(error_sizes):
            if index < size:
                error_parts[block].append(codewords[cursor])
                cursor += 1
    data = []
    for block in range(len(data_sizes)):
        block_words = data_parts[block] + error_parts[block]
        if not _syndromes_zero(block_words, error_sizes[block]):
            raise AssertionError('Reed-Solomon parity mismatch in a decoded block')
        data.extend(data_parts[block])

    stream = []
    for byte in data:
        for shift in range(7, -1, -1):
            stream.append((byte >> shift) & 1)
    if not stream or stream[0:4] != [0, 1, 0, 0]:
        raise AssertionError('payload is not in byte mode')
    index = 4
    count_bits = 8 if version < 10 else 16
    length = 0
    for offset in range(count_bits):
        length = (length << 1) | stream[index]
        index += 1
    payload = bytearray()
    for _ in range(length):
        if index + 8 > len(stream):
            raise AssertionError('payload is truncated')
        value = 0
        for offset in range(8):
            value = (value << 1) | stream[index]
            index += 1
        payload.append(value)
    return payload.decode('utf-8')


def _bch_format(value):
    remainder = value << 10
    degree = lambda n: n.bit_length()
    while degree(remainder) - degree(0x537) >= 0:
        remainder ^= 0x537 << (degree(remainder) - degree(0x537))
    return ((value << 10) | remainder) ^ 0x5412


def make_qr(payload, source=None):
    """Render a QR with the page's own encoder, under node."""
    text = source or APP_JS.read_text(encoding='utf-8')
    start = text.index('function makeQR(')
    end = text.index('function snapshotTime', start)
    function = text[start:end]
    script = function + '\nprocess.stdout.write(makeQR(' + json.dumps(payload) + '));'
    return node_ok(script)
